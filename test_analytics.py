import os
import json
from datetime import datetime
from pharmacy_functions import update_booking_sheet, send_call_summary_email
from dotenv import load_dotenv

load_dotenv()

def test_analytics_system():
    print("--- STARTING ANALYTICS TEST ---")
    
    # 1. Test Google Sheets
    print("\n[1/2] Testing Google Sheets Update...")
    test_data = {
        "name": "Test Caller",
        "patient_name": "Test Child",
        "problems": "Test Fever",
        "parents_name": "Test Parent",
        "is_booked": True,
        "booking_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    sheet_result = update_booking_sheet(**test_data)
    if sheet_result.get("success"):
        print("SUCCESS: Google Sheet updated! Check your spreadsheet.")
    else:
        print(f"FAILED: Google Sheet update failed. Error: {sheet_result.get('error')}")
        print("ADVICE: Make sure 'voice-ai-calendar@pdfdrive-461614.iam.gserviceaccount.com' is an EDITOR on your sheet.")

    # 2. Test Email
    print("\n[2/2] Testing Email Summary...")
    test_summary = "This is a test summary from the analytics script."
    test_transcript = "User: Initializing test. Priya: Responding to test."
    
    email_result = send_call_summary_email(test_summary, test_transcript)
    if email_result.get("success"):
        print("SUCCESS: Test email sent! Check your inbox.")
    else:
        print(f"FAILED: Email sending failed. Error: {email_result.get('error')}")
        print("ADVICE: Check GMAIL_USER and GMAIL_APP_PASSWORD in your .env file.")

if __name__ == "__main__":
    test_analytics_system()
