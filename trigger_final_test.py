import os
import json
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pharmacy_functions import book_appointment

load_dotenv()

def trigger_manual_test():
    print("🚀 Triggering Final Integration Test...")
    test_patient = "FINAL_VERIFIED_BOOKING"
    test_day = (datetime.now() + timedelta(days=3)).strftime("%A")
    
    try:
        result = book_appointment(
            patient_name=test_patient,
            patient_age="10",
            parent_name="VERIFICATION_BOT",
            contact_number="9876543210",
            preferred_day=test_day,
            preferred_time="11:00 AM",
            reason="Thorough Technical Verification"
        )
        
        with open("final_test_result.json", "w") as f:
            json.dump(result, f, indent=2)
            
        print(f"✅ Success! Result saved to final_test_result.json")
        print(f"Message: {result.get('message')}")
        
    except Exception as e:
        error_msg = traceback.format_exc()
        with open("final_test_result.json", "w") as f:
            f.write(error_msg)
        print(f"❌ Failed! Check final_test_result.json for details.")

if __name__ == "__main__":
    trigger_manual_test()
