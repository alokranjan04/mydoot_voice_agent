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
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")

_CACHED_SERVICES = {}


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


def _get_sheets_service():
    if "sheets" in _CACHED_SERVICES:
        return _CACHED_SERVICES["sheets"], SPREADSHEET_ID

    creds_data = get_google_creds()
    if not creds_data:
        print("[FEEDBACK ERROR]: Unable to initialize Google Sheets service because credentials are missing or invalid.")
        return None, None

    creds = service_account.Credentials.from_service_account_info(
        creds_data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    _CACHED_SERVICES["sheets"] = service
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

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

    ts      = datetime.now().strftime("%Y-%m-%d %H:%M IST")
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
        date_str   = datetime.now().strftime("%Y-%m-%d")
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
    try:
        service, spreadsheet_id = _get_sheets_service()
        if not service:
            print("[SERVICE REQUEST ERROR]: No Sheets service — check GOOGLE_CREDENTIALS")
            return {"success": False, "message": "Google Sheets credentials not found."}

        # Write header row if sheet is empty
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range="Sheet1!A1:K1"
            ).execute()
            if not result.get("values"):
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
        except Exception as e:
            print(f"[SHEETS HEADER WARNING]: {e}")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        values = [[
            customer_name,
            category,
            subcategory,
            issue_type,
            brand or "",
            model or "",
            severity or "",
            address,
            preferred_time,
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
        print(f"[SERVICE REQUEST]: Saved — {cells} cells updated.")
        return {"success": True}
    except Exception as e:
        print(f"[SERVICE REQUEST ERROR]: {e}")
        return {"success": False, "message": str(e)}


FUNCTION_MAP = {
    "save_customer_feedback": save_customer_feedback,
    "save_service_request":   save_service_request,
}
