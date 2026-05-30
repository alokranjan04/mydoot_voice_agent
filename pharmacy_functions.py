import os
import json
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

# ── Time → Hindi speech ───────────────────────────────────────────────────────
_HI_HOUR = {1:"एक", 2:"दो", 3:"तीन", 4:"चार", 5:"पाँच", 6:"छह",
             7:"सात", 8:"आठ", 9:"नौ", 10:"दस", 11:"ग्यारह", 12:"बारह"}
_HI_MIN  = {5:"पाँच", 10:"दस", 15:"पंद्रह", 20:"बीस", 25:"पच्चीस",
             30:"तीस", 35:"पैंतीस", 40:"चालीस", 45:"पैंतालीस", 50:"पचास", 55:"पचपन"}
_HI_DAY  = {
    "Monday":"सोमवार", "Tuesday":"मंगलवार", "Wednesday":"बुधवार",
    "Thursday":"गुरुवार", "Friday":"शुक्रवार", "Saturday":"शनिवार", "Sunday":"रविवार",
}

def _day_to_hindi(day_str: str) -> str:
    today    = datetime.now().strftime("%A")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%A")
    if day_str == today:
        return "आज"
    if day_str == tomorrow:
        return "कल"
    return _HI_DAY.get(day_str, day_str)

def _time_to_hindi(time_str: str) -> str:
    """'06:10 PM' → 'शाम के छह बजकर दस मिनट'"""
    try:
        dt  = datetime.strptime(time_str.strip(), "%I:%M %p")
        h24, m = dt.hour, dt.minute
        h12 = h24 % 12 or 12
        period = ("सुबह"  if h24 < 12 else
                  "दोपहर" if h24 < 17 else
                  "शाम"   if h24 < 20 else "रात")
        if m == 0:
            return f"{period} के {_HI_HOUR[h12]} बजे"
        elif m == 15:
            return f"{period} के सवा {_HI_HOUR[h12]} बजे"
        elif m == 30:
            return f"{period} के साढ़े {_HI_HOUR[h12]} बजे"
        elif m == 45:
            nxt = h12 % 12 + 1
            return f"{period} के पौने {_HI_HOUR[nxt]} बजे"
        else:
            min_hi = _HI_MIN.get(m, str(m))
            return f"{period} के {_HI_HOUR[h12]} बजकर {min_hi} मिनट"
    except Exception:
        return time_str

# Spreadsheet ID from environment (never hardcode in source)
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")

# In-memory storage
APPOINTMENTS_DB = {"appointments": {}, "next_id": 1}

# Available slots now generated dynamically in check_available_slots()

def get_google_creds():
    """Load and normalize Google service account credentials."""
    data = None
    source = ""

    # 1. Try file first
    creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google-credentials.json")
    if os.path.exists(creds_file):
        try:
            with open(creds_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                source = "google-credentials.json"
        except Exception as e:
            print(f"WARNING: Could not load google-credentials.json: {e}")

    # 2. Try env if file failed
    if not data:
        creds_json = os.getenv("GOOGLE_CREDENTIALS", "").strip()
        if creds_json:
            try:
                if (creds_json.startswith("'") and creds_json.endswith("'")) or (creds_json.startswith('"') and creds_json.endswith('"')):
                    creds_json = creds_json[1:-1]
                data = json.loads(creds_json)
                source = "ENVIRONMENT"
            except Exception as e:
                print(f"WARNING: Credential Parse Error: {e}")

    if not data:
        return None

    # 3. Aggressive Normalize & Rebuild
    pk = data.get("private_key", "")
    if pk:
        # Clean quotes and resolve escape sequences
        pk = pk.strip().strip("'").strip('"')
        pk = pk.replace("\\n", "\n").replace("\\\\n", "\n")
        
        # Extract the body (everything between headers)
        body = pk.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "")
        # Remove all whitespace, newlines, and non-base64 noise
        body = "".join(body.split())
        
        # Re-wrap to 64 character lines (Standard RSA Format)
        wrapped_body = "\n".join(body[i:i+64] for i in range(0, len(body), 64))
        
        # Final reconstruction
        final_pk = f"-----BEGIN PRIVATE KEY-----\n{wrapped_body}\n-----END PRIVATE KEY-----\n"
        data["private_key"] = final_pk
        
        # Diagnostic
        start = final_pk[:30].replace("\n", "\\n")
        print(f"[AUTH]: Loaded creds via {source}. Key starts: '{start}'...")

    return data

def generate_ics(appt):
    """Generate a standard iCalendar (.ics) string."""
    start_dt = get_appointment_datetime(appt["preferred_day"], appt["preferred_time"])
    end_dt = start_dt + timedelta(minutes=10)
    
    dtstamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    dtstart = start_dt.strftime("%Y%m%dT%H%M%S")
    dtend = end_dt.strftime("%Y%m%dT%H%M%S")
    
    ics = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Neha Child Care//Clinic Assistant//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{appt['id']}@nehachildcare.com",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID=Asia/Kolkata:{dtstart}",
        f"DTEND;TZID=Asia/Kolkata:{dtend}",
        f"SUMMARY:Appointment: {appt['patient_name']}",
        f"DESCRIPTION:Parent: {appt['parent_name']}\\nReason: {appt['reason']}\\nContact: {appt['contact_number']}",
        "LOCATION:Neha Child Care Clinic",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        "DESCRIPTION:Reminder",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR"
    ]
    return "\n".join(ics)

def _normalize_day(day_name: str) -> str:
    """Resolve Hindi/relative day words to English day name.
    Handles compound phrases like 'कल सुबह', 'Friday morning', 'next Monday'.
    """
    # Devanagari digit → ASCII
    deva_map = str.maketrans("०१२३४५६७८९", "0123456789")
    day_name = day_name.translate(deva_map).strip()

    # Strip time-of-day qualifiers so 'कल सुबह' → 'कल', 'Friday morning' → 'Friday'
    strip_words = [
        "सुबह", "शाम", "रात", "दोपहर",         # Hindi
        "morning", "evening", "night", "noon",    # English
        "को", "का", "के", "next", "आने वाले",
    ]
    cleaned = day_name
    for w in strip_words:
        cleaned = cleaned.replace(w, " ")
    cleaned = " ".join(cleaned.split()).strip()   # collapse whitespace

    # Relative words → actual day
    hindi_relative = {
        "कल": 1, "kal": 1, "tomorrow": 1,
        "परसों": 2, "parson": 2, "day after tomorrow": 2,
        "आज": 0, "aaj": 0, "today": 0,
    }
    hindi_days = {
        "सोमवार": "Monday",   "monday": "Monday",
        "मंगलवार": "Tuesday",  "tuesday": "Tuesday",
        "बुधवार": "Wednesday", "wednesday": "Wednesday",
        "गुरुवार": "Thursday", "thursday": "Thursday",
        "शुक्रवार": "Friday",  "friday": "Friday",
        "शनिवार": "Saturday",  "saturday": "Saturday",
        "रविवार": "Sunday",    "sunday": "Sunday",
    }

    key = cleaned.lower()
    if key in hindi_relative:
        target_dt = datetime.now() + timedelta(days=hindi_relative[key])
        return target_dt.strftime("%A")
    if key in hindi_days:
        return hindi_days[key]
    # Try capitalize for already-English names
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if cleaned.capitalize() in days:
        return cleaned.capitalize()
    # Partial match fallback
    for eng in days:
        if eng.lower().startswith(key[:3]):
            return eng
    raise ValueError(f"Cannot resolve day: '{day_name}'")

def _normalize_time(time_str: str) -> str:
    """Normalise time string: Devanagari digits → ASCII, handle missing AM/PM."""
    deva_map = str.maketrans("०१२३४५६७८९", "0123456789")
    t = time_str.translate(deva_map).strip().upper()
    
    # If no AM/PM, try to infer from hour
    if "AM" not in t and "PM" not in t:
        try:
            # Handle HH:MM or just HH
            hour_part = t.split(":")[0]
            # Strip any non-numeric noise
            hour_part = "".join(filter(str.isdigit, hour_part))
            hour = int(hour_part)
            
            # Clinic logic: 10-12 is always AM, 6-8 is always PM
            if 6 <= hour < 12:
                t += " AM"
            else:
                t += " PM"
        except Exception:
            t += " AM"
    return t.replace(".", "").strip()

def get_appointment_datetime(day_name, time_str):
    """Convert day name and time string to a datetime object for the upcoming week.
    Always uses IST (UTC+5:30) for the base 'now' reference.
    """
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    resolved_day  = _normalize_day(day_name)
    resolved_time = _normalize_time(time_str)

    target_day  = days.index(resolved_day)
    # Ensure we use IST even if the server is in UTC
    current_dt  = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_day = current_dt.weekday()
    
    days_ahead  = target_day - current_day
    if days_ahead < 0:
        days_ahead += 7

    target_date = current_dt + timedelta(days=days_ahead)
    time_dt     = datetime.strptime(resolved_time, "%I:%M %p")
    return target_date.replace(hour=time_dt.hour, minute=time_dt.minute, second=0, microsecond=0)

def update_booking_sheet(patient_name, problems, parent_name, contact_number, booking_time, patient_age, booking_slot):
    """Upsert a row in the Google Sheets booking log.
    If an active entry exists for the same patient or contact, update it instead of appending.

    Sheet column layout (A–H):
    A: Patient Name | B: Patient Problems | C: Parents Name
    D: Is appointment Booked | E: Booking time | F: Child Age
    G: Booking Slot | H: Contact Number
    """
    try:
        service, spreadsheet_id = _get_sheets_service()
        if not service: return {"error": "Credentials missing"}

        # Search for existing active rows
        existing_rows = _find_sheet_rows(patient_name, contact_number)
        
        values = [[
            patient_name,    # A: Patient Name
            problems,        # B: Patient Problems
            parent_name,     # C: Parents Name
            "Yes",           # D: Is appointment Booked
            booking_time,    # E: Booking time
            patient_age,     # F: Child Age
            booking_slot,    # G: Booking Slot
            contact_number,  # H: Contact Number
        ]]

        if existing_rows:
            # Update the most recent matching row
            row_idx, _ = existing_rows[-1]
            print(f"[SHEETS]: Updating existing row {row_idx} for {patient_name} ({contact_number})")
            result = service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"Sheet1!A{row_idx}:H{row_idx}",
                valueInputOption="RAW",
                body={'values': values}
            ).execute()
            return {"success": True, "updated": 1, "action": "update", "row_index": row_idx}
        else:
            # Append new row
            print(f"[SHEETS]: Appending new row for {patient_name} ({contact_number})")
            result = service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range="Sheet1!A2",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={'values': values}
            ).execute()
            return {"success": True, "updated": result.get('updates', {}).get('updatedCells'), "action": "append"}

    except Exception as e:
        print(f"SHEETS ERROR: {e}")
        return {"error": str(e)}

def create_google_calendar_event(appt):
    """Invite the doctor via Service Account to ensure calendar visibility."""
    try:
        creds_data = get_google_creds()
        if not creds_data: return {"error": "Credentials missing"}
        
        scopes = ['https://www.googleapis.com/auth/calendar']
        creds = service_account.Credentials.from_service_account_info(creds_data, scopes=scopes)
        service = build('calendar', 'v3', credentials=creds)
        
        start_dt = get_appointment_datetime(appt["preferred_day"], appt["preferred_time"])
        end_dt = start_dt + timedelta(minutes=10)
        
        event = {
            'summary': f'Appointment: {appt["patient_name"]}',
            'description': f'Parent: {appt["parent_name"]}\nContact: {appt["contact_number"]}\nReason: {appt["reason"]}',
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Kolkata'},
        }

        calendar_id = _get_calendar_id()
        event_result = service.events().insert(calendarId=calendar_id, body=event).execute()
        return event_result
    except Exception as e:
        print(f"CALENDAR ERROR: {e}")
        return {"error": str(e)}

def book_appointment(patient_name, patient_age, parent_name, contact_number, preferred_day, preferred_time, reason):
    """Master booking function for Neha Child Care with honest reporting."""
    print(f"\n[DIGITAL_LOG]: Processing booking for {patient_name}...")

    # Normalise day and time so calendar/ICS never crash on Hindi input
    try:
        preferred_day  = _normalize_day(preferred_day)
        preferred_time = _normalize_time(preferred_time)
    except Exception as e:
        print(f"[NORMALIZE ERROR]: {e}")

    # ── PAST-TIME GUARD: reject bookings for times that have already passed ──
    try:
        appt_dt = get_appointment_datetime(preferred_day, preferred_time)
        # Ensure 'now' is in IST for comparison
        ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
        if appt_dt <= ist_now:
            msg = (f"यह समय ({preferred_time} on {preferred_day}) बीत चुका है। "
                   "कोई और समय चुनें।")
            print(f"[PAST-TIME BLOCK]: {msg}")
            return {"success": False, "message": msg, "past_time": True}
    except Exception as e:
        print(f"[PAST-TIME CHECK ERROR]: {e}")

    # ── CONFLICT CHECK: Local DB only (Blazing Fast) ──
    # Prevents double-booking from same agent/simultaneous calls
    for existing in APPOINTMENTS_DB["appointments"].values():
        if (_normalize_day(existing["preferred_day"]) == preferred_day and 
            _normalize_time(existing["preferred_time"]) == preferred_time):
            print(f"[CONFLICT]: Slot {preferred_time} on {preferred_day} is already in Local DB.")
            return {"success": False, "message": f"CONFLICT: {preferred_time} is already booked."}

    appt_id = APPOINTMENTS_DB["next_id"]
    APPOINTMENTS_DB["next_id"] += 1

    appt = {
        "id": appt_id, "patient_name": patient_name, "patient_age": patient_age,
        "parent_name": parent_name, "contact_number": contact_number,
        "preferred_day": preferred_day, "preferred_time": preferred_time,
        "reason": reason, "clinic": "Neha Child Care"
    }

    # Reserve the slot immediately so any concurrent check_available_slots call
    # sees it as taken before the Google APIs finish.
    APPOINTMENTS_DB["appointments"][appt_id] = appt

    # ── UPDATE CHECK: If patient/number exists, clean up old calendar events ──
    try:
        existing = _find_sheet_rows(patient_name, contact_number)
        if existing:
            print(f"[BOOKING UPDATE]: Existing entry found. Cleaning up old calendar events.")
            _delete_calendar_events(patient_name)
    except Exception as e:
        print(f"[UPDATE CHECK ERROR]: {e}")

    # Run Sheets + Calendar + Email concurrently to cut booking time ~3x
    booking_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    sheet_res = {"error": "not run"}
    cal_res   = {"error": "not run"}

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_sheet = pool.submit(update_booking_sheet, patient_name, reason, parent_name, contact_number, booking_time_str, patient_age, preferred_time)
        fut_cal   = pool.submit(create_google_calendar_event, appt)
        fut_email = pool.submit(send_confirmation_email_with_ics, appt)
        for fut in as_completed([fut_sheet, fut_cal, fut_email]):
            try:
                fut.result()
            except Exception as _fe:
                print(f"[BOOKING TASK ERROR]: {_fe}")

    sheet_res = fut_sheet.result() if not fut_sheet.exception() else {"error": str(fut_sheet.exception())}
    cal_res   = fut_cal.result()   if not fut_cal.exception()   else {"error": str(fut_cal.exception())}

    # If SHEET succeeded, we count it as a success for the user/agent flow
    # even if Calendar or Email had a secondary error. 
    # We report the secondary error in the logs.
    if "error" in sheet_res:
        # Roll back the in-memory reservation if the primary record failed
        APPOINTMENTS_DB["appointments"].pop(appt_id, None)
        return {
            "success": False,
            "message": "BOOKING_FAILED: Sheet update failed.",
            "sheets_status": sheet_res.get("error"),
            "calendar_status": cal_res.get("error", "OK"),
        }

    # If we are here, Sheets logic passed!
    if "error" in cal_res:
         print(f"⚠️  MINOR CALENDAR SYNC ERROR: {cal_res['error']}")
    
    day_hi  = _day_to_hindi(preferred_day)
    time_hi = _time_to_hindi(preferred_time)
    confirmation_message = (
        f"{day_hi} {time_hi} {patient_name} का appointment मैंने book कर दिया है। "
        "आप please 15 minutes पहले आ जाइए। तब तक बच्चे का ध्यान रखिएगा।"
    )
    return {
        "success": True,
        "message": f"Appointment booked! ID: {appt_id}.",
        "confirmation_message": confirmation_message,
        "details": appt,
    }

def _send_email(subject: str, body: str, sender_name: str = "Clinic Assistant", ics_content: str = None) -> dict:
    """Shared email sender. Attaches .ics calendar invite if ics_content is provided."""
    try:
        gmail_user     = os.getenv("GMAIL_USER")
        gmail_password = os.getenv("GMAIL_APP_PASSWORD")
        doctor_email   = os.getenv("DOCTOR_EMAIL")

        if not all([gmail_user, gmail_password, doctor_email]):
            missing = [k for k, v in {"GMAIL_USER": gmail_user, "GMAIL_APP_PASSWORD": gmail_password, "DOCTOR_EMAIL": doctor_email}.items() if not v]
            print(f"EMAIL SKIP: Missing env vars: {missing}")
            return {"error": "Email credentials missing from environment"}

        msg = MIMEMultipart()
        msg['From']    = f"{sender_name} <{gmail_user}>"
        msg['To']      = doctor_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        if ics_content:
            part = MIMEBase('text', 'calendar', method='REQUEST', name='invite.ics')
            part.set_payload(ics_content)
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment; filename="invite.ics"')
            msg.attach(part)

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(gmail_user, gmail_password)
            server.send_message(msg)
        print(f"EMAIL SENT: '{subject}' → {doctor_email}")
        return {"success": True}
    except Exception as e:
        print(f"EMAIL ERROR: {e}")
        return {"error": str(e)}

def send_confirmation_email_with_ics(appt):
    """Send appointment confirmation email with an attached .ics calendar invite."""
    body = (
        f"Appointment confirmed for {appt['patient_name']} "
        f"on {appt['preferred_day']} at {appt['preferred_time']}."
    )
    return _send_email(
        subject=f"Appointment Booked: {appt['patient_name']}",
        body=body,
        ics_content=generate_ics(appt),
    )

def send_call_summary_email(summary, transcript):
    """Send call summary email after each call."""
    body = f"Summary: {summary}\n\nTranscript:\n{transcript}"
    return _send_email(
        subject="Call Summary: Neha Child Care",
        body=body,
        sender_name="Priya Assistant",
    )

import time

_AVAILABLE_SLOTS_CACHE = {}

def check_available_slots(preferred_day):
    """Dynamically generate available 10-minute slots with a 30-second cache."""
    try:
        day = _normalize_day(preferred_day)
    except Exception:
        day = preferred_day.strip().capitalize()
    
    # ── CACHE CHECK ──
    now_time = time.time()
    cache_key = day
    if cache_key in _AVAILABLE_SLOTS_CACHE:
        entry = _AVAILABLE_SLOTS_CACHE[cache_key]
        if now_time - entry["timestamp"] < 30: # 30 second TTL
            print(f"🚀 [CACHE HIT]: Serving slots for {day} from memory.")
            return entry["data"]

    # If clinic is closed on Sunday, auto-redirect to Monday
    if day == "Sunday":
        day = "Monday"

    # 1. Load Configured Windows from app_config.json
    try:
        with open('app_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            morning_range = config['clinic']['hours']['morning'].split(" to ")
            evening_range = config['clinic']['hours']['evening'].split(" to ")
    except (OSError, json.JSONDecodeError, KeyError, UnicodeDecodeError):
        # Fallback to defaults if config load fails
        morning_range = ["10:00 AM", "12:00 PM"]
        evening_range = ["06:00 PM", "08:00 PM"]

    def generate_range_slots(start_str, end_str):
        slots = []
        start = datetime.strptime(start_str, "%I:%M %p")
        end = datetime.strptime(end_str, "%I:%M %p")
        curr = start
        while curr < end:
            slots.append(curr.strftime("%I:%M %p"))
            curr += timedelta(minutes=10)
        return slots

    total_slots = generate_range_slots(morning_range[0], morning_range[1]) + \
                  generate_range_slots(evening_range[0], evening_range[1])

    # 2. Identify Booked Times for this day
    booked_times = []
    
    # A. Check Local DB
    for appt in APPOINTMENTS_DB["appointments"].values():
        try:
            appt_day = _normalize_day(appt["preferred_day"])
        except Exception:
            appt_day = appt["preferred_day"].capitalize()
        if appt_day == day:
            booked_times.append(datetime.strptime(_normalize_time(appt["preferred_time"]), "%I:%M %p"))

    # B. Check Google Calendar (Real-time Sync)
    try:
        service = _get_calendar_service()
        if service:
            calendar_id = _get_calendar_id()
            # Get the date for the target day
            target_dt = get_appointment_datetime(day, "12:00 PM")
            time_min = target_dt.replace(hour=0, minute=0).isoformat() + 'Z'
            time_max = target_dt.replace(hour=23, minute=59).isoformat() + 'Z'
            
            events_result = service.events().list(
                calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy='startTime').execute()
            
            for event in events_result.get('items', []):
                start_raw = event['start'].get('dateTime') or event['start'].get('date')
                if 'T' in start_raw:
                    # Convert calendar time string to our internal %I:%M %p format for comparison
                    evt_dt = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
                    booked_times.append(datetime.strptime(evt_dt.strftime("%I:%M %p"), "%I:%M %p"))
    except Exception as e:
        print(f"CALENDAR SYNC WARNING: {e}")

    # 3. Filter Slots (Ensure 10-minute gap)
    available_slots = []
    for slot_str in total_slots:
        slot_dt = datetime.strptime(slot_str, "%I:%M %p")
        is_free = True
        for booked_dt in booked_times:
            # Check for conflict within 10 minutes either side
            diff = abs((slot_dt - booked_dt).total_seconds()) / 60
            if diff < 10:
                is_free = False
                break
        if is_free:
            available_slots.append(slot_str)

    # 4. Filter past slots if today & detect urgent slots
    # Use IST (UTC+5:30) for comparison if server is in UTC
    now_dt = datetime.utcnow() + timedelta(hours=5, minutes=30)
    is_today = (day == now_dt.strftime("%A"))

    if is_today:
        now_min = now_dt.hour * 60 + now_dt.minute
        available_slots = [
            s for s in available_slots
            if (datetime.strptime(s, "%I:%M %p").hour * 60 +
                datetime.strptime(s, "%I:%M %p").minute) > now_min
        ]

    result = {
        "day": day,
        "available_slots": available_slots[:20],
        "booked_slots": [t.strftime("%I:%M %p") for t in booked_times]
    }

    # ── CACHE UPDATE ──
    _AVAILABLE_SLOTS_CACHE[cache_key] = {
        "timestamp": time.time(),
        "data": result
    }

    if is_today and available_slots:
        first_slot = available_slots[0]
        slot_dt    = datetime.strptime(first_slot, "%I:%M %p")
        mins_away  = (slot_dt.hour * 60 + slot_dt.minute) - now_min
        if 0 < mins_away <= 30:
            first_hi = _time_to_hindi(first_slot)
            mins_hi  = _HI_MIN.get(mins_away, str(mins_away))
            result["urgent_slot"]    = first_slot
            result["urgent_minutes"] = mins_away
            result["urgent_message"] = (
                f"आज {first_hi} slot available है — "
                f"सिर्फ {mins_hi} मिनट में। "
                f"क्या आप इतनी जल्दी आ सकते हैं?"
            )

    return result

_CACHED_SERVICES = {}

def _get_calendar_id() -> str:
    """Return validated Google Calendar ID. Guards against the env var being
    set to the full credentials JSON instead of a calendar ID string."""
    cal_id = os.getenv("GOOGLE_CALENDAR_ID", "primary").strip()
    if not cal_id or cal_id.startswith("{") or len(cal_id) > 150:
        print("[CALENDAR] GOOGLE_CALENDAR_ID invalid — falling back to 'primary'")
        return "primary"
    return cal_id

def _get_sheets_service():
    if 'sheets' in _CACHED_SERVICES:
        return _CACHED_SERVICES['sheets'], SPREADSHEET_ID
    
    creds_data = get_google_creds()
    if not creds_data: return None, None
    creds = service_account.Credentials.from_service_account_info(
        creds_data, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    service = build('sheets', 'v4', credentials=creds)
    _CACHED_SERVICES['sheets'] = service
    return service, SPREADSHEET_ID

def _get_calendar_service():
    if 'calendar' in _CACHED_SERVICES:
        return _CACHED_SERVICES['calendar']
        
    creds_data = get_google_creds()
    if not creds_data: return None
    creds = service_account.Credentials.from_service_account_info(
        creds_data, scopes=['https://www.googleapis.com/auth/calendar'])
    service = build('calendar', 'v3', credentials=creds)
    _CACHED_SERVICES['calendar'] = service
    return service

def _get_gmail_service():
    if 'gmail' in _CACHED_SERVICES:
        return _CACHED_SERVICES['gmail']
    
    creds_data = get_google_creds()
    if not creds_data: return None
    creds = service_account.Credentials.from_service_account_info(
        creds_data, scopes=['https://www.googleapis.com/auth/gmail.send'])
    service = build('gmail', 'v1', credentials=creds)
    _CACHED_SERVICES['gmail'] = service
    return service

def _find_sheet_rows(patient_name, contact_number):
    """Return list of (row_index, row_data) matching patient_name or contact_number.

    Column layout: A=Patient Name, B=Problems, C=Parent, D=Is Booked,
                   E=Booking time, F=Child Age, G=Booking Slot, H=Contact Number
    """
    service, spreadsheet_id = _get_sheets_service()
    if not service: return []
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range="Sheet1!A:H").execute()
    rows = result.get('values', [])
    matches = []
    for i, row in enumerate(rows[1:], start=2):  # skip header row
        # col A (index 0) = patient_name; col H (index 7) = contact_number
        name_match  = len(row) > 0 and patient_name.lower() in row[0].lower()
        phone_match = len(row) > 7 and contact_number and row[7] == str(contact_number)
        # Skip rows already cancelled
        is_cancelled = len(row) > 3 and row[3].strip().lower() == "cancelled"
        # Match only when BOTH name AND phone match — different children from
        # the same parent phone number must get separate rows, not overwrite.
        if (name_match and phone_match) and not is_cancelled:
            matches.append((i, row))
    return matches

def _delete_calendar_events(patient_name):
    """Delete all upcoming calendar events matching patient name."""
    service = _get_calendar_service()
    if not service: return 0
    calendar_id = _get_calendar_id()
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    events = service.events().list(
        calendarId=calendar_id, q=f"Appointment: {patient_name}",
        timeMin=now, maxResults=10, singleEvents=True).execute()
    deleted = 0
    for event in events.get('items', []):
        if f"Appointment: {patient_name}" in event.get('summary', ''):
            service.events().delete(calendarId=calendar_id, eventId=event['id']).execute()
            deleted += 1
    return deleted

def cancel_appointment(patient_name, contact_number):
    """Cancel an existing appointment by patient name."""
    print(f"\n[DIGITAL_LOG]: Cancelling appointment for {patient_name}...")
    try:
        service, spreadsheet_id = _get_sheets_service()
        rows = _find_sheet_rows(patient_name, contact_number)
        if not rows:
            return {"success": False, "message": f"Koi appointment nahi mili '{patient_name}' ke liye. Kya naam sahi hai?"}

        # Mark as Cancelled in sheet — column D is "Is appointment Booked"
        for row_idx, _ in rows:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"Sheet1!D{row_idx}",
                valueInputOption="RAW",
                body={'values': [["Cancelled"]]}
            ).execute()

        # Delete from calendar
        deleted = _delete_calendar_events(patient_name)
        print(f"[DIGITAL_LOG]: Cancelled {len(rows)} sheet rows, {deleted} calendar events.")
        return {"success": True, "message": f"{patient_name} ki appointment cancel ho gayi hai."}
    except Exception as e:
        print(f"CANCEL ERROR: {e}")
        return {"error": str(e)}

def reschedule_appointment(patient_name, contact_number, new_day, new_time):
    """Reschedule an existing appointment to a new day and time."""
    print(f"\n[DIGITAL_LOG]: Rescheduling appointment for {patient_name} to {new_day} {new_time}...")
    try:
        # Check if appointment exists
        rows = _find_sheet_rows(patient_name, contact_number)
        if not rows:
            return {"success": False, "message": f"Koi appointment nahi mili '{patient_name}' ke liye."}

        _, old_row = rows[0]
        reason = old_row[1] if len(old_row) > 1 else "Reschedule"
        age = old_row[5] if len(old_row) > 5 else "5"
        parent = old_row[2] if len(old_row) > 2 else "Guardian"

        # book_appointment will now handle UPSERT in sheets and replacement in calendar
        result = book_appointment(
            patient_name=patient_name,
            patient_age=age,
            parent_name=parent,
            contact_number=contact_number,
            preferred_day=new_day,
            preferred_time=new_time,
            reason=reason
        )
        if result.get("success"):
            return {"success": True, "message": f"{patient_name} ki appointment reschedule ho gayi! {new_day} को {new_time} बजे।"}
        return result
    except Exception as e:
        print(f"RESCHEDULE ERROR: {e}")
        return {"error": str(e)}

FUNCTION_MAP = {
    "check_available_slots": check_available_slots,
    "book_appointment": book_appointment,
    "cancel_appointment": cancel_appointment,
    "reschedule_appointment": reschedule_appointment
}
