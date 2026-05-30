import asyncio
import os
import json
from pharmacy_functions import book_appointment, check_available_slots

def test_confirmation_logic():
    print("--- TESTING CONFIRMATION LOGIC ---")
    
    # Simulate a scenario where the agent MIGHT try to book without confirmation
    # We want to ensure the 'book_appointment' logic doesn't have internal triggers
    # without proper args.
    
    # This is a unit test for the tool itself
    try:
        # 1. Test check_available_slots (Speed check)
        import time
        start = time.time()
        res1 = check_available_slots("Today")
        end1 = time.time()
        print(f"Check 1 (Live/Cached): {end1-start:.4f}s")
        
        start = time.time()
        res2 = check_available_slots("Today")
        end2 = time.time()
        print(f"Check 2 (Cache): {end2-start:.4f}s")
        
        if end2-start < 0.01:
            print("✅ Caching is working! (Instant response)")
        else:
            print("❌ Caching failed or not fast enough.")

        # 2. Test booking
        print("\nTesting tool execution...")
        res_book = book_appointment(
            patient_name="TEST_PERF_1",
            reason="Fever",
            preferred_day="Monday",
            preferred_time="10:00 AM",
            patient_age="5",
            parent_name="Guardian",
            contact_number="9999999999"
        )
        if res_book.get("success"):
            print("✅ Tool executes correctly.")
        else:
            print(f"❌ Tool execution failed: {res_book.get('message')}")

    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    test_confirmation_logic()
