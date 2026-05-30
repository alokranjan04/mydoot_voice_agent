import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

def get_astrology_insights(dob, tob, pob):
    """
    Mock function to simulate getting astrology insights.
    In a real app, this would call a Kundli/Astrology API.
    """
    print(f"[ASTROLOGY]: Analyzing chart for DOB: {dob}, TOB: {tob}, POB: {pob}")
    
    # Mock validation points based on the user's requirement
    validation_points = [
        "Aapka nature interesting hai... aap logical bhi hain, lekin decisions lete waqt emotions interfere kar jaate hain... is wajah se kabhi kabhi delay hota hai...",
        "Aap thoda overthinking ki taraf bhi jaate hain... specially jab future ya stability ki baat ho... is wajah se aap khud hi apne decisions delay kar dete hain",
        "Aap paisa kama lete hain... lekin usko hold karna ya grow karna thoda inconsistent hota hai... specially jab emotional decisions involved hote hain"
    ]
    
    # Mock future windows
    future_windows = {
        "near_term": "Next 6 months: Adjustment phase. Focus on routine discipline.",
        "mid_term": "Next 1-2 years: Growth window. Mid-2025 shows a significant shift in responsibility."
    }
    
    return {
        "success": True,
        "validation_points": validation_points,
        "future_windows": future_windows,
        "remedy": "Routine discipline pe kaam karein. Panna (Emerald) helpful ho sakta hai par detailed check ke baad hi confirm hoga."
    }

def book_consultation(name, contact, consultation_type="roadmap"):
    """
    Mock function to book a detailed astrology consultation.
    """
    print(f"[ASTROLOGY]: Booking consultation for {name} ({contact}) - Type: {consultation_type}")
    
    return {
        "success": True,
        "message": f"Ji, {name} ji, maine aapki detailed {consultation_type} consultation ke liye request note kar li hai. Hum jald hi aapse sampark karenge.",
        "confirmation_id": "ASTRO-" + datetime.now().strftime("%Y%m%d%H%M%S")
    }

def send_call_summary_email(summary, transcript):
    """
    Mock function to send call summary email.
    """
    print(f"[EMAIL]: Sending summary for call: {summary}")
    return {"success": True}

FUNCTION_MAP = {
    "get_astrology_insights": get_astrology_insights,
    "book_consultation": book_consultation
}
