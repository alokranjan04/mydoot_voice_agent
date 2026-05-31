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


def _get_sheets_service():
    if "sheets" in _CACHED_SERVICES:
        return _CACHED_SERVICES["sheets"], SPREADSHEET_ID

    creds_data = get_google_creds()
    if not creds_data:
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
                           caller_id=""):
    """
    Save customer feedback to Google Sheets.

    Sheet columns (A–I):
      A: Customer Name | B: Brand  | C: Item
      D: Product Used Since | E: Usage Duration | F: Warranty Status
      G: Complaint | H: Timestamp | I: Caller ID
    """
    print(f"[FEEDBACK]: Saving — Customer={customer_name}, Brand={brand}, Item={item}")
    print(f"[FEEDBACK]: SpreadsheetID={SPREADSHEET_ID!r} | GOOGLE_CREDENTIALS set={bool(os.getenv('GOOGLE_CREDENTIALS'))}")
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
        return {
            "success": True,
            "message": (
                f"{customer_name} ji, aapki complaint humne register kar li hai. "
                "Hamari service team agle 24 ghanton mein aapse sampark karegi. "
                "Shukriya MyDoot Customer Care ko call karne ke liye!"
            ),
        }
    except Exception as e:
        print(f"[FEEDBACK ERROR]: {e}")
        return {"success": False, "message": f"Feedback save karne mein error aya: {e}"}


def send_call_summary_email(caller_id: str, transcript_lines: list):
    """Send full call transcript to the configured admin email after each call."""
    gmail_user     = os.getenv("GMAIL_USER", "").strip()
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not gmail_user or not gmail_password:
        print("[EMAIL]: GMAIL_USER or GMAIL_APP_PASSWORD not set — skipping email.")
        return

    ts      = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    subject = f"MyDoot Call Transcript — Caller: {caller_id} — {ts}"
    body_lines = [
        f"MyDoot Customer Care — Call Summary",
        f"Caller ID : {caller_id}",
        f"Time      : {ts}",
        f"",
        "─── TRANSCRIPT ───",
        "",
    ]
    if transcript_lines:
        body_lines.extend(transcript_lines)
    else:
        body_lines.append("(No transcript captured — transcription may not be supported for this call)")
    body = "\n".join(body_lines)

    try:
        msg = MIMEMultipart()
        msg["From"]    = gmail_user
        msg["To"]      = gmail_user
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.send_message(msg)

        print(f"[EMAIL]: Transcript sent to {gmail_user}")
    except Exception as e:
        print(f"[EMAIL ERROR]: {e}")


FUNCTION_MAP = {
    "save_customer_feedback": save_customer_feedback,
}
