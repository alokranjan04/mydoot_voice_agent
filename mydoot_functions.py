# -*- coding: utf-8 -*-
"""
mydoot_functions.py — Mydoot Customer Care tool functions.

Collects customer feedback via voice and saves to Google Sheets.

Expected Google Sheet columns (Sheet1):
  A: Customer Name | B: Brand    | C: Item
  D: Product Used Since | E: Usage Duration | F: Warranty Status
  G: Complaint | H: Timestamp | I: Caller ID
"""
import os
import json
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))
from google.oauth2 import service_account
from googleapiclient.discovery import build

from config.database import get_conn, put_conn
from config.settings import INSTANCE_ID

SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")

# Cached Sheets service — rebuilt when TTL expires or on stale-connection error.
# Avoids ~500ms discovery-doc + TCP handshake cost on every save call.
_SHEETS_CACHE: dict = {"service": None, "built_at": 0.0, "headers_written": False}
_SHEETS_TTL_SECS = 3000  # 50 minutes


def _validate_google_creds(data):
    if not isinstance(data, dict):
        return ["type", "project_id", "private_key", "client_email", "token_uri"]

    required = ["type", "project_id", "private_key", "client_email", "token_uri"]
    missing = [key for key in required if not data.get(key)]
    return missing


def get_google_creds():
    """Load and normalize Google service account credentials."""
    data = None
    source = ""

    creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google-credentials.json")
    if os.path.exists(creds_file):
        try:
            with open(creds_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                source = "google-credentials.json"
        except Exception as e:
            print(f"WARNING: Could not load google-credentials.json: {e}")

    if not data:
        creds_json = os.getenv("GOOGLE_CREDENTIALS", "").strip()
        if creds_json:
            try:
                if (creds_json.startswith("'") and creds_json.endswith("'")) or \
                   (creds_json.startswith('"') and creds_json.endswith('"')):
                    creds_json = creds_json[1:-1]
                data = json.loads(creds_json)
                source = "ENVIRONMENT"
            except Exception as e:
                print(f"WARNING: Credential Parse Error: {e}")

    if not data:
        return None

    missing_keys = _validate_google_creds(data)
    if missing_keys:
        print("[AUTH ERROR]: Google service account credentials are invalid or incomplete.")
        print("[AUTH ERROR]: Missing required fields: " + ", ".join(missing_keys))
        print("[AUTH ERROR]: Ensure your service account JSON is a full Google key file and that the target sheet is shared with the service account email.")
        return None

    pk = data.get("private_key", "")
    if pk:
        pk = pk.strip().strip("'").strip('"')
        pk = pk.replace("\\n", "\n").replace("\\\\n", "\n")
        body = pk.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "")
        body = "".join(body.split())
        wrapped_body = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
        final_pk = f"-----BEGIN PRIVATE KEY-----\n{wrapped_body}\n-----END PRIVATE KEY-----\n"
        data["private_key"] = final_pk
        start = final_pk[:30].replace("\n", "\\n")
        print(f"[AUTH]: Loaded creds via {source}. Key starts: '{start}'...")

    return data


def get_google_creds_health():
    creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google-credentials.json")
    file_exists = os.path.exists(creds_file)
    creds_json = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    env_present = bool(creds_json)
    load_error = None
    source = None
    data = None

    if file_exists:
        try:
            with open(creds_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                source = "google-credentials.json"
        except Exception as e:
            load_error = f"google-credentials.json parse error: {e}"

    if data is None and env_present:
        try:
            if (creds_json.startswith("'") and creds_json.endswith("'")) or \
               (creds_json.startswith('"') and creds_json.endswith('"')):
                creds_json = creds_json[1:-1]
            data = json.loads(creds_json)
            source = "ENVIRONMENT"
        except Exception as e:
            load_error = f"GOOGLE_CREDENTIALS parse error: {e}"

    missing_keys = []
    valid = False
    if data is not None:
        missing_keys = _validate_google_creds(data)
        valid = not missing_keys and load_error is None

    return {
        "file_exists": file_exists,
        "env_present": env_present,
        "valid": valid,
        "missing_keys": missing_keys,
        "load_error": load_error,
        "source": source,
    }


def get_gmail_health():
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    return {
        "user_set": bool(gmail_user),
        "app_password_set": bool(gmail_password),
        "valid": bool(gmail_user and gmail_password),
    }


def _get_sheets_service(force_rebuild: bool = False):
    """Return a Google Sheets service, reusing cached instance if still fresh.

    Rebuilds when:
    - No cached service yet
    - TTL expired (3000 s / 50 min)
    - force_rebuild=True (caller detected a stale-connection error)

    Caching avoids the ~500 ms discovery-doc + TCP/TLS handshake cost on every
    save.  Cloud Run instances are kept warm between calls so this is safe.
    """
    now = time.time()
    if (not force_rebuild
            and _SHEETS_CACHE["service"]
            and (now - _SHEETS_CACHE["built_at"]) < _SHEETS_TTL_SECS):
        return _SHEETS_CACHE["service"], SPREADSHEET_ID

    creds_data = get_google_creds()
    if not creds_data:
        print("[FEEDBACK ERROR]: Unable to initialize Google Sheets service — credentials missing or invalid.")
        return None, None

    creds = service_account.Credentials.from_service_account_info(
        creds_data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    _SHEETS_CACHE["service"] = service
    _SHEETS_CACHE["built_at"] = now
    label = "force-rebuilt" if force_rebuild else "built"
    print(f"[SHEETS] Service {label} (TTL={_SHEETS_TTL_SECS}s).")
    return service, SPREADSHEET_ID


def _ensure_header_row(service, spreadsheet_id):
    """Write the header row if the sheet is empty."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range="Sheet1!A1:I1"
        ).execute()
        existing = result.get("values", [])
        if not existing:
            headers = [["Customer Name", "Brand", "Item",
                        "Product Used Since", "Usage Duration",
                        "Warranty Status", "Complaint",
                        "Timestamp", "Caller ID"]]
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="Sheet1!A1:I1",
                valueInputOption="RAW",
                body={"values": headers}
            ).execute()
            print("[SHEETS]: Header row written.")
    except Exception as e:
        print(f"[SHEETS HEADER WARNING]: {e}")


def save_customer_feedback(customer_name, brand, item,
                           product_used_since, usage_duration,
                           warranty_status, complaint,
                           caller_id="", area=""):
    """
    Save customer feedback to Google Sheets.

    Sheet columns (A–I):
      A: Customer Name | B: Brand  | C: Item
      D: Product Used Since | E: Usage Duration | F: Warranty Status
      G: Complaint (includes Area for home service jobs) | H: Timestamp | I: Caller ID
    """
    print(f"[FEEDBACK]: Saving — Customer={customer_name}, Brand={brand}, Item={item}")
    print(f"[FEEDBACK]: SpreadsheetID={SPREADSHEET_ID!r} | GOOGLE_CREDENTIALS set={bool(os.getenv('GOOGLE_CREDENTIALS'))}")
    # Append area to complaint for home service jobs
    if area and area.strip():
        complaint = f"{complaint} | Area: {area.strip()}"
    try:
        service, spreadsheet_id = _get_sheets_service()
        if not service:
            print("[FEEDBACK ERROR]: No Sheets service — check GOOGLE_CREDENTIALS env var")
            return {
                "success": False,
                "message": "Google Sheets credentials not found.",
            }

        _ensure_header_row(service, spreadsheet_id)

        timestamp = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")
        values = [[
            customer_name,
            brand,
            item,
            product_used_since,
            usage_duration,
            warranty_status,
            complaint,
            timestamp,
            caller_id,
        ]]

        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="Sheet1!A2",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()

        cells = result.get("updates", {}).get("updatedCells", "?")
        print(f"[FEEDBACK]: Saved successfully — {cells} cells updated.")
        return {"success": True}
    except Exception as e:
        print(f"[FEEDBACK ERROR]: {e}")
        return {"success": False, "message": f"Feedback save karne mein error aya: {e}"}


def send_call_summary_email(caller_id: str, transcript_lines: list):
    """Send full call transcript to the configured admin email after each call."""
    import traceback as _tb

    gmail_user     = os.getenv("GMAIL_USER", "").strip()
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")

    # ── Credential diagnostics ──────────────────────────────────────────────
    print(f"[EMAIL]: GMAIL_USER={'SET (' + gmail_user[:4] + '...)' if gmail_user else '*** NOT SET ***'}")
    print(f"[EMAIL]: GMAIL_APP_PASSWORD={'SET (len=' + str(len(gmail_password)) + ')' if gmail_password else '*** NOT SET ***'}")
    print(f"[EMAIL]: transcript lines = {len(transcript_lines)}")

    if not gmail_user:
        print("[EMAIL ERROR]: GMAIL_USER env var is missing — cannot send email.")
        return
    if not gmail_password:
        print("[EMAIL ERROR]: GMAIL_APP_PASSWORD env var is missing — cannot send email.")
        return

    ts      = datetime.now(_IST).strftime("%Y-%m-%d %H:%M IST")
    subject = f"MyDoot Call Transcript — Caller: {caller_id} — {ts}"
    body_lines = [
        "MyDoot Customer Care — Call Summary",
        f"Caller ID : {caller_id}",
        f"Time      : {ts}",
        "",
        "─── TRANSCRIPT ───",
        "",
    ]
    if transcript_lines:
        body_lines.extend(transcript_lines)
    else:
        body_lines.append("(No transcript captured — call may have been very short or dropped before any speech)")
    body = "\n".join(body_lines)

    try:
        print(f"[EMAIL]: Connecting to smtp.gmail.com:465 ...")
        msg = MIMEMultipart()
        msg["From"]    = gmail_user
        msg["To"]      = gmail_user
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            print(f"[EMAIL]: SMTP connected — logging in ...")
            server.login(gmail_user, gmail_password)
            print(f"[EMAIL]: Login successful — sending message ...")
            server.send_message(msg)

        print(f"[EMAIL]: ✅ Transcript email sent successfully to {gmail_user} "
              f"| subject: {subject}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"[EMAIL ERROR]: ❌ Authentication failed — check GMAIL_APP_PASSWORD "
              f"(must be a Gmail App Password, not your account password): {e}")
        _tb.print_exc()
    except smtplib.SMTPException as e:
        print(f"[EMAIL ERROR]: ❌ SMTP error: {e}")
        _tb.print_exc()
    except Exception as e:
        print(f"[EMAIL ERROR]: ❌ Unexpected error: {e}")
        _tb.print_exc()


def upload_recording_to_gcs(local_path: str, caller_id: str) -> str:
    """
    Upload a call recording WAV to Google Cloud Storage.

    Returns the gs:// URI on success, empty string on failure or if not configured.

    Required env var:
        GCS_RECORDINGS_BUCKET  — GCS bucket name (e.g. "mydoot-call-recordings")

    Optional env var:
        GCS_DELETE_LOCAL       — set to "1" to delete the local WAV after upload
    """
    bucket_name = os.getenv("GCS_RECORDINGS_BUCKET", "").strip()
    if not bucket_name:
        return ""

    try:
        from google.cloud import storage as gcs
    except ImportError:
        print("[GCS] google-cloud-storage not installed — skipping upload")
        return ""

    creds_data = get_google_creds()
    if not creds_data:
        print("[GCS] No credentials — skipping upload")
        return ""

    try:
        creds = service_account.Credentials.from_service_account_info(
            creds_data,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client = gcs.Client(credentials=creds, project=creds_data.get("project_id"))
        bucket = client.bucket(bucket_name)

        filename   = os.path.basename(local_path)
        date_str   = datetime.now(_IST).strftime("%Y-%m-%d")
        blob_name  = f"recordings/{caller_id}/{date_str}/{filename}"

        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_path, content_type="audio/wav")

        gcs_uri = f"gs://{bucket_name}/{blob_name}"
        print(f"[GCS] Recording uploaded: {gcs_uri}")

        if os.getenv("GCS_DELETE_LOCAL", "0").lower() in ("1", "true", "yes"):
            os.remove(local_path)
            print(f"[GCS] Local file deleted: {local_path}")

        return gcs_uri

    except Exception as e:
        print(f"[GCS ERROR] Upload failed for {local_path}: {e}")
        return ""


def save_service_request(customer_name, category, subcategory, issue_type,
                          address, preferred_time,
                          brand="", model="",
                          severity="", error_code="", warranty_status="",
                          caller_id=""):
    """
    Save a structured service request to Google Sheets (LangGraph pipeline).

    Sheet columns (A–K):
      A: Customer Name | B: Category   | C: Subcategory | D: Issue Type
      E: Brand         | F: Model      | G: Severity    | H: Address
      I: Preferred Time | J: Timestamp | K: Caller ID
    """
    print(f"[SERVICE REQUEST]: Saving — Customer={customer_name}, "
          f"Category={category}, Sub={subcategory}, Issue={issue_type}")
    print(f"[SERVICE REQUEST]: SpreadsheetID={SPREADSHEET_ID!r}")

    timestamp = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. PostgreSQL (primary, soft-fail) ────────────────────────────────────
    pg_ok = False
    conn = get_conn()
    if conn is not None:
        try:
            raw = {
                "customer_name": customer_name, "category": category,
                "subcategory": subcategory, "issue_type": issue_type,
                "brand": brand, "model": model, "severity": severity,
                "address": address, "preferred_time": preferred_time,
                "caller_id": caller_id, "error_code": error_code,
                "warranty_status": warranty_status,
            }
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO service_requests
                        (instance_id, caller_id, customer_name, category,
                         subcategory, issue_type, brand, model, severity,
                         address, preferred_time, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        INSTANCE_ID, caller_id or "", customer_name,
                        category, subcategory, issue_type,
                        brand or "", model or "", severity or "",
                        address, preferred_time,
                        json.dumps(raw, ensure_ascii=False),
                    ),
                )
            conn.commit()
            put_conn(conn)
            pg_ok = True
            print(f"[SERVICE REQUEST][PG]: Saved — instance={INSTANCE_ID}")
        except Exception as pg_err:
            print(f"[SERVICE REQUEST][PG ERROR]: {pg_err}")
            put_conn(conn, discard=True)
    else:
        print("[SERVICE REQUEST][PG]: No pool — Sheets only")

    # ── 2. Google Sheets (secondary) ─────────────────────────────────────────
    values = [[
        customer_name, category, subcategory, issue_type,
        brand or "", model or "", severity or "",
        address, preferred_time, timestamp, caller_id,
    ]]

    def _append(svc, sid):
        return svc.spreadsheets().values().append(
            spreadsheetId=sid,
            range="Sheet1!A2",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()

    try:
        service, spreadsheet_id = _get_sheets_service()
        if not service:
            print("[SERVICE REQUEST ERROR]: No Sheets service — check GOOGLE_CREDENTIALS")
            return {"success": False, "message": "Google Sheets credentials not found."}

        # Write header row only if not yet confirmed.
        # Cached flag avoids an extra ~300 ms GET on every save.
        if not _SHEETS_CACHE["headers_written"]:
            try:
                hdr = service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id, range="Sheet1!A1:K1"
                ).execute()
                if not hdr.get("values"):
                    headers = [[
                        "Customer Name", "Category", "Subcategory", "Issue Type",
                        "Brand", "Model", "Severity", "Address", "Preferred Time",
                        "Timestamp", "Caller ID",
                    ]]
                    service.spreadsheets().values().update(
                        spreadsheetId=spreadsheet_id,
                        range="Sheet1!A1:K1",
                        valueInputOption="RAW",
                        body={"values": headers},
                    ).execute()
                    print("[SHEETS]: Header row written.")
                _SHEETS_CACHE["headers_written"] = True
            except Exception as e:
                print(f"[SHEETS HEADER WARNING]: {e}")

        result = _append(service, spreadsheet_id)
        cells = result.get("updates", {}).get("updatedCells", "?")
        print(f"[SERVICE REQUEST]: Saved — {cells} cells updated.")
        return {"success": True}

    except Exception as e:
        import traceback as _tb
        _err_s = str(e).lower()
        _is_stale = any(s in _err_s for s in
                        ("connection", "reset", "eof", "broken pipe", "transport", "peer"))
        if _is_stale:
            print(f"[SERVICE REQUEST]: Stale connection — rebuilding Sheets service and retrying: {e}")
            _SHEETS_CACHE["service"] = None
            _SHEETS_CACHE["built_at"] = 0.0
            try:
                svc2, sid2 = _get_sheets_service()
                if svc2:
                    result2 = _append(svc2, sid2)
                    cells2 = result2.get("updates", {}).get("updatedCells", "?")
                    print(f"[SERVICE REQUEST]: Saved (retry) — {cells2} cells updated.")
                    return {"success": True}
            except Exception as retry_e:
                print(f"[SERVICE REQUEST ERROR] (retry): {retry_e}")
        print(f"[SERVICE REQUEST ERROR]: {e}")
        _tb.print_exc()
        return {"success": pg_ok, "message": str(e)}


FUNCTION_MAP = {
    "save_customer_feedback": save_customer_feedback,
    "save_service_request":   save_service_request,
}


# ── Call log (observability) ───────────────────────────────────────────────────
# Writes one row per call to a "Call_Logs" sheet in the same spreadsheet.
# Used by the /calls observability dashboard.

_CALL_LOGS_CACHE: dict = {"sheet_ensured": False}

_CALL_LOGS_HEADERS = [
    "Timestamp (IST)", "Caller ID", "Duration (s)", "Stage Reached", "Saved",
    "Category", "Subcategory", "Issue Type", "Customer Name", "Address",
    "Preferred Time", "STT Count", "STT Avg (ms)", "STT Drops",
    "Barge-Ins", "Reconnects", "Audio GCS", "Local Recording", "Transcript",
]


def _ensure_call_logs_sheet(service, spreadsheet_id: str) -> bool:
    """Create the Call_Logs sheet and write headers if it doesn't exist yet."""
    if _CALL_LOGS_CACHE.get("sheet_ensured"):
        return True
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        if "Call_Logs" not in existing:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": "Call_Logs"}}}]},
            ).execute()
            print("[CALL LOG] Created 'Call_Logs' sheet.")
        # Write headers if row 1 is empty
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range="Call_Logs!A1:S1"
        ).execute()
        if not result.get("values"):
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="Call_Logs!A1:S1",
                valueInputOption="RAW",
                body={"values": [_CALL_LOGS_HEADERS]},
            ).execute()
            print("[CALL LOG] Header row written.")
        _CALL_LOGS_CACHE["sheet_ensured"] = True
        return True
    except Exception as e:
        print(f"[CALL LOG WARNING] _ensure_call_logs_sheet: {e}")
        return False


def save_call_log(
    caller_id: str,
    duration_secs: float,
    stage_reached: str,
    saved: bool,
    category: str,
    subcategory: str,
    issue_type: str,
    customer_name: str,
    address: str,
    preferred_time: str,
    stt_count: int,
    stt_avg_ms: float,
    stt_drops: int,
    barge_ins: int,
    reconnects: int,
    audio_gcs: str,
    local_wav: str,
    transcript: list,
) -> dict:
    """Append one row to call_logs. Writes PostgreSQL first, then Google Sheets."""
    transcript_str = "\n".join(transcript)[:3000]

    # ── 1. PostgreSQL (primary, soft-fail) ────────────────────────────────────
    pg_ok = False
    conn = get_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO call_logs
                        (instance_id, caller_id, duration_secs, stage_reached,
                         saved, category, subcategory, issue_type,
                         customer_name, address, preferred_time,
                         stt_count, stt_avg_ms, stt_drops,
                         barge_ins, reconnects, audio_gcs, local_wav, transcript)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        INSTANCE_ID, caller_id, round(duration_secs, 1),
                        stage_reached, saved,
                        category or "", subcategory or "", issue_type or "",
                        customer_name or "", address or "", preferred_time or "",
                        stt_count,
                        round(stt_avg_ms) if stt_avg_ms else None,
                        stt_drops, barge_ins, reconnects,
                        audio_gcs or "", local_wav or "", transcript_str,
                    ),
                )
            conn.commit()
            put_conn(conn)
            pg_ok = True
            print(f"[CALL LOG][PG]: Saved — caller={caller_id} instance={INSTANCE_ID}")
        except Exception as pg_err:
            print(f"[CALL LOG][PG ERROR]: {pg_err}")
            put_conn(conn, discard=True)
    else:
        print("[CALL LOG][PG]: No pool — Sheets only")

    # ── 2. Google Sheets (secondary) ─────────────────────────────────────────
    try:
        service, spreadsheet_id = _get_sheets_service()
        if not service:
            return {"success": pg_ok}
        _ensure_call_logs_sheet(service, spreadsheet_id)
        timestamp = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
        values = [[
            timestamp, caller_id, round(duration_secs, 1),
            stage_reached, "TRUE" if saved else "FALSE",
            category or "", subcategory or "", issue_type or "",
            customer_name or "", address or "", preferred_time or "",
            stt_count, round(stt_avg_ms) if stt_avg_ms else "",
            stt_drops, barge_ins, reconnects,
            audio_gcs or "", local_wav or "", transcript_str,
        ]]
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="Call_Logs!A2",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        print(f"[CALL LOG] Saved — caller={caller_id} stage={stage_reached} saved={saved}")
        return {"success": True}
    except Exception as e:
        print(f"[CALL LOG ERROR] {e}")
        return {"success": pg_ok}


def save_turn_latency(caller_id: str, turns: list) -> None:
    """Bulk-insert completed TurnSpan records into turn_latency_metrics (PostgreSQL only)."""
    if not turns:
        return
    conn = get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            for span in turns:
                cur.execute(
                    """
                    INSERT INTO turn_latency_metrics
                        (instance_id, caller_id, turn_id, customer_text,
                         vad_ms, stt_ms, langgraph_ms,
                         llm_first_token_ms, llm_total_ms,
                         tts_first_audio_ms, tts_total_ms, end_to_end_turn_ms)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        INSTANCE_ID, caller_id, span.turn_id, span.customer_text,
                        span.vad_ms, span.stt_ms, span.langgraph_ms,
                        span.llm_first_token_ms, span.llm_total_ms,
                        span.tts_first_audio_ms, span.tts_total_ms,
                        span.end_to_end_turn_ms,
                    ),
                )
        conn.commit()
        put_conn(conn)
        print(f"[LATENCY][PG]: Saved {len(turns)} turns — caller={caller_id}")
    except Exception as e:
        print(f"[LATENCY][PG ERROR]: {e}")
        put_conn(conn, discard=True)


def save_field_quality_log(records: list) -> None:
    """
    Bulk-insert FieldRecord dicts into field_quality_log (PostgreSQL only).
    `records` is the list returned by ExtractionQualityTracker.flush().
    """
    if not records:
        return
    conn = get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            for r in records:
                cur.execute(
                    """
                    INSERT INTO field_quality_log
                        (instance_id, caller_id, field,
                         first_value, final_value,
                         num_attempts, num_corrections,
                         confidence, source, call_saved)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        INSTANCE_ID,
                        r["caller_id"],
                        r["field"],
                        r.get("first_value", ""),
                        r.get("final_value", ""),
                        r.get("num_attempts", 1),
                        r.get("num_corrections", 0),
                        r.get("confidence"),
                        r.get("source", "gemini"),
                        r.get("call_saved", False),
                    ),
                )
        conn.commit()
        put_conn(conn)
        print(f"[QUAL][PG]: Saved {len(records)} quality records")
    except Exception as exc:
        print(f"[QUAL][PG ERROR]: {exc}")
        put_conn(conn, discard=True)


def get_quality_analytics(lookback_days: int = 30) -> dict:
    """
    Return extraction quality aggregates for the analytics dashboard.
    Queries field_quality_log; returns {} when PostgreSQL not configured.

    Returned keys:
      window_days, total_records,
      field_accuracy   — list of per-field dicts
      top_corrections  — dict keyed by field, each a list of {heard_as, corrected_to, count}
      confusion_pairs  — top-30 ASR confusion pairs across all fields
      daily_trend      — per-day per-field totals/correction counts (last 14 days)
    """
    conn = get_conn()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            # ── Field-level accuracy (saved calls only) ────────────────────────
            cur.execute(
                """
                SELECT
                    field,
                    COUNT(*)                                                  AS total,
                    SUM(CASE WHEN num_corrections = 0 THEN 1 ELSE 0 END)     AS first_pass,
                    SUM(CASE WHEN num_corrections > 0 THEN 1 ELSE 0 END)     AS corrected,
                    ROUND(AVG(num_attempts)::numeric, 2)                     AS avg_attempts,
                    ROUND(AVG(confidence)::numeric, 3)                       AS avg_confidence,
                    ROUND(
                        100.0 * SUM(CASE WHEN num_corrections = 0 THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*), 0), 1
                    )                                                         AS first_pass_pct,
                    ROUND(
                        100.0 * SUM(CASE WHEN num_corrections > 0 THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*), 0), 1
                    )                                                         AS correction_rate_pct
                FROM field_quality_log
                WHERE instance_id  = %s
                  AND call_saved   = TRUE
                  AND created_at  >= NOW() - (%s || ' days')::INTERVAL
                GROUP BY field
                ORDER BY correction_rate_pct DESC NULLS LAST
                """,
                (INSTANCE_ID, lookback_days),
            )
            cols = [d[0] for d in cur.description]
            field_accuracy = [dict(zip(cols, row)) for row in cur.fetchall()]
            total_records  = sum(r["total"] for r in field_accuracy)

            # ── Top 20 corrections per field ───────────────────────────────────
            top_corrections: dict = {}
            for fld in ("brand", "address", "customer_name",
                        "category", "subcategory", "issue_type", "preferred_time"):
                cur.execute(
                    """
                    SELECT first_value AS heard_as,
                           final_value AS corrected_to,
                           COUNT(*)    AS count
                    FROM field_quality_log
                    WHERE instance_id  = %s
                      AND field        = %s
                      AND call_saved   = TRUE
                      AND num_corrections > 0
                      AND first_value <> ''
                      AND final_value <> ''
                      AND first_value <> final_value
                      AND created_at  >= NOW() - (%s || ' days')::INTERVAL
                    GROUP BY first_value, final_value
                    ORDER BY count DESC
                    LIMIT 20
                    """,
                    (INSTANCE_ID, fld, lookback_days),
                )
                rows = cur.fetchall()
                if rows:
                    top_corrections[fld] = [
                        {"heard_as": r[0], "corrected_to": r[1], "count": r[2]}
                        for r in rows
                    ]

            # ── Top-30 ASR confusion pairs across all fields ───────────────────
            cur.execute(
                """
                SELECT field,
                       first_value AS heard_as,
                       final_value AS corrected_to,
                       COUNT(*)    AS count
                FROM field_quality_log
                WHERE instance_id  = %s
                  AND call_saved   = TRUE
                  AND num_corrections > 0
                  AND first_value <> ''
                  AND final_value <> ''
                  AND first_value <> final_value
                  AND created_at  >= NOW() - (%s || ' days')::INTERVAL
                GROUP BY field, first_value, final_value
                ORDER BY count DESC
                LIMIT 30
                """,
                (INSTANCE_ID, lookback_days),
            )
            confusion_pairs = [
                {"field": r[0], "heard_as": r[1], "corrected_to": r[2], "count": r[3]}
                for r in cur.fetchall()
            ]

            # ── Daily trend (last 14 days) ─────────────────────────────────────
            cur.execute(
                """
                SELECT created_at::date       AS day,
                       field,
                       COUNT(*)               AS total,
                       SUM(CASE WHEN num_corrections > 0 THEN 1 ELSE 0 END) AS corrected
                FROM field_quality_log
                WHERE instance_id = %s
                  AND call_saved  = TRUE
                  AND created_at >= NOW() - INTERVAL '14 days'
                GROUP BY day, field
                ORDER BY day DESC, field
                """,
                (INSTANCE_ID,),
            )
            daily_trend = [
                {"date": str(r[0]), "field": r[1], "total": r[2], "corrected": r[3]}
                for r in cur.fetchall()
            ]

        put_conn(conn)
        return {
            "window_days":    lookback_days,
            "total_records":  total_records,
            "field_accuracy": field_accuracy,
            "top_corrections": top_corrections,
            "confusion_pairs": confusion_pairs,
            "daily_trend":    daily_trend,
        }
    except Exception as exc:
        print(f"[QUAL ANALYTICS ERROR]: {exc}")
        put_conn(conn, discard=True)
        return {}


def get_latency_stats(lookback_hours: int = 24) -> dict:
    """Return P50/P95/P99 for each latency metric over the last N hours from PostgreSQL."""
    conn = get_conn()
    if conn is None:
        return {}
    try:
        metrics = [
            "vad_ms", "stt_ms", "langgraph_ms",
            "llm_first_token_ms", "llm_total_ms",
            "tts_first_audio_ms", "tts_total_ms", "end_to_end_turn_ms",
        ]
        selects = []
        for m in metrics:
            selects += [
                f"PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY {m}) AS {m}_p50",
                f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {m}) AS {m}_p95",
                f"PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY {m}) AS {m}_p99",
            ]
        sql = (
            f"SELECT COUNT(*) AS sample_count, {', '.join(selects)} "
            f"FROM turn_latency_metrics "
            f"WHERE instance_id = %s "
            f"  AND created_at >= NOW() - INTERVAL '{lookback_hours} hours' "
            f"  AND end_to_end_turn_ms IS NOT NULL"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (INSTANCE_ID,))
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        put_conn(conn)
        if not row:
            return {}
        result = dict(zip(cols, row))
        return {k: (float(v) if v is not None else None) for k, v in result.items()}
    except Exception as e:
        print(f"[LATENCY STATS ERROR]: {e}")
        put_conn(conn, discard=True)
        return {}


def get_call_logs(n: int = 200) -> list:
    """Return the last n rows from Call_Logs sheet as list of dicts."""
    try:
        service, spreadsheet_id = _get_sheets_service()
        if not service:
            return []
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="Call_Logs!A1:R",
        ).execute()
        rows = result.get("values", [])
        if not rows or len(rows) < 2:
            return []
        headers = rows[0]
        records = []
        for row in rows[1:][-n:]:
            padded = row + [""] * (len(headers) - len(row))
            records.append(dict(zip(headers, padded)))
        records.reverse()   # newest first
        return records
    except Exception as e:
        print(f"[CALL LOG ERROR] get_call_logs: {e}")
        return []
