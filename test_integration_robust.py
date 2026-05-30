import os
import json
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pharmacy_functions import book_appointment, send_call_summary_email

load_dotenv()

def run_robust_test():
    print("="*50)
    print("🚀 STARTING ROBUST CLINIC INTEGRATION TEST")
    print("="*50)
    
    # Mock Data
    test_patient = f"TEST_PATIENT_{int(time.time())}"
    test_contact = "9999999999"
    test_day = (datetime.now() + timedelta(days=2)).strftime("%A") # 2 days from now
    test_time = "10:30 AM"
    
    # 1. TEST GOOGLE CALENDAR & SHEETS
    print(f"\n[STEP 1] Testing 'book_appointment' for {test_patient}...")
    try:
        booking_result = book_appointment(
            patient_name=test_patient,
            patient_age="5 Years",
            parent_name="MOCK_PARENT",
            contact_number=test_contact,
            preferred_day=test_day,
            preferred_time=test_time,
            reason="Routine Checkup (Robust Test)"
        )
        print(f"✅ BOOKING RESULT: {json.dumps(booking_result, indent=2)}")
    except Exception as e:
        print(f"❌ BOOKING FAILED: {e}")

    # 2. TEST EMAIL SUMMARIES
    print(f"\n[STEP 2] Testing 'send_call_summary_email'...")
    try:
        email_result = send_call_summary_email(
            summary="ROBUST INTEGRATION TEST SUCCESSFUL",
            transcript=f"System: This is a diagnostic test run at {datetime.now()}.\nUser: Verify connectivity.\nAI: Systems operational."
        )
        print(f"✅ EMAIL RESULT: {json.dumps(email_result, indent=2)}")
    except Exception as e:
        print(f"❌ EMAIL FAILED: {e}")

    print("\n" + "="*50)
    print("🏁 TEST COMPLETE")
    print("Please check your Google Calendar, Google Sheet, and Email Inbox.")
    print("="*50)

if __name__ == "__main__":
    run_robust_test()
