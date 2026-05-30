"""
Test script for Neha Child Care appointment booking agent.
Simulates real call scenarios to verify functions and conversation flow.
"""

import json
from pharmacy_functions import (
    check_available_slots,
    book_appointment,
    check_appointment,
    cancel_appointment,
    APPOINTMENTS_DB,
)

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
INFO = "\033[94m[INFO]\033[0m"
SECTION = "\033[93m"
RESET = "\033[0m"

passed = 0
failed = 0


def assert_test(label, condition, result):
    global passed, failed
    status = PASS if condition else FAIL
    print(f"  {status} {label}")
    if not condition:
        print(f"         Got: {result}")
        failed += 1
    else:
        passed += 1


def section(title):
    print(f"\n{SECTION}{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}{RESET}")


def show_agent_says(text):
    print(f"\n  {INFO} Agent bolegi: \"{text}\"")


# ─────────────────────────────────────────────
# RESET DB before each major scenario
def reset_db():
    APPOINTMENTS_DB["appointments"].clear()
    APPOINTMENTS_DB["next_id"] = 1


# ─────────────────────────────────────────────
section("TEST 1: Available Slots Check")

r = check_available_slots("Monday")
assert_test("Monday slots exist", "available_slots" in r and len(r["available_slots"]) > 0, r)
show_agent_says("Haan ji, Monday ko yeh slots available hain: " + ", ".join(r.get("available_slots", [])))

r = check_available_slots("sunday")
assert_test("Sunday returns closed message", "error" in r, r)
show_agent_says("Sorry ji, Sunday ko hamaari clinic band rehti hai.")

r = check_available_slots("saturday")
assert_test("Saturday slots exist", "available_slots" in r, r)
show_agent_says("Saturday ko yeh time available hai: " + ", ".join(r.get("available_slots", [])))

r = check_available_slots("xyz")
assert_test("Invalid day returns error", "error" in r, r)


# ─────────────────────────────────────────────
section("TEST 2: Full Appointment Booking (Happy Path)")
reset_db()

# Simulate conversation:
# Caller: "Mera beta Rohan hai, 4 saal ka, uske liye appointment chahiye"
show_agent_says("Kis ke liye appointment book karni hai?")
show_agent_says("Bacche ki umar kya hai?")
show_agent_says("Aapka naam kya hai?")
show_agent_says("Contact number please?")

slots = check_available_slots("Wednesday")
show_agent_says(f"Wednesday ko yeh slots available hain: {slots['available_slots']}. Kaunsa time theek rahega?")

r = book_appointment(
    patient_name="Rohan Sharma",
    patient_age="4 saal",
    parent_name="Amit Sharma",
    contact_number="9876543210",
    preferred_day="Wednesday",
    preferred_time="11:00 AM",
    reason="Bukhar aur khansi",
)
assert_test("Appointment booked successfully", "appointment_id" in r, r)
assert_test("Correct patient name saved", r.get("patient_name") == "Rohan Sharma", r)
assert_test("Correct day saved", r.get("day") == "Wednesday", r)
assert_test("Correct time saved", r.get("time") == "11:00 AM", r)
show_agent_says(
    f"Bilkul ji! Rohan Sharma ji ki appointment book ho gayi hai. "
    f"Appointment ID hai {r.get('appointment_id')}. "
    f"Wednesday 11 baje aana. 15 minute pehle aa jaiyega.\n"
    f"        [STATUS] Calendar: {r.get('calendar_status')} | Email: {r.get('email_status')}"
)
booked_id = r["appointment_id"]


# ─────────────────────────────────────────────
section("TEST 3: Double Booking Same Slot")

r2 = book_appointment(
    patient_name="Priya Verma",
    patient_age="2 saal",
    parent_name="Sunita Verma",
    contact_number="9123456789",
    preferred_day="Wednesday",
    preferred_time="11:00 AM",
    reason="Routine checkup",
)
assert_test("Double booking blocked", "error" in r2, r2)
show_agent_says(
    "Sorry ji, Wednesday 11 baje ka slot already book hai. "
    "Koi aur time choose karein - " + ", ".join(r2.get("available_slots", []))
)


# ─────────────────────────────────────────────
section("TEST 4: Invalid Time Slot")

r3 = book_appointment(
    patient_name="Ananya Singh",
    patient_age="6 saal",
    parent_name="Ravi Singh",
    contact_number="9001234567",
    preferred_day="Monday",
    preferred_time="3:00 PM",  # not a valid slot
    reason="Skin rash",
)
assert_test("Invalid time slot blocked", "error" in r3, r3)
show_agent_says("Ji, 3 baje ka slot available nahi hai. Available times hain: " + str(r3.get("available_slots")))


# ─────────────────────────────────────────────
section("TEST 5: Check Existing Appointment")

r4 = check_appointment(booked_id)
assert_test("Appointment found by ID", "appointment_id" in r4, r4)
assert_test("Patient name matches", r4.get("patient_name") == "Rohan Sharma", r4)
assert_test("Status is confirmed", r4.get("status") == "confirmed", r4)
show_agent_says(
    f"Haan ji, appointment number {booked_id} hai. "
    f"Patient: {r4['patient_name']}, {r4['day']} ko {r4['time']} baje. Status: {r4['status']}. \n"
    f"        [STATUS] Calendar: {r4.get('calendar_status', 'N/A')} | Email: {r4.get('email_status', 'N/A')}"
)

r5 = check_appointment(999)
assert_test("Non-existent appointment returns error", "error" in r5, r5)
show_agent_says("Sorry ji, yeh appointment ID nahi mila. Kripya sahi number check karein.")


# ─────────────────────────────────────────────
section("TEST 6: Cancel Appointment")

r6 = cancel_appointment(booked_id)
assert_test("Appointment cancelled successfully", "message" in r6 and "cancel" in r6["message"], r6)
show_agent_says(
    f"Ji, appointment number {booked_id} cancel ho gayi hai. "
    f"Agar dobara appointment chahiye toh hume call karein."
)

# Verify slot is now free again
slots_after = check_available_slots("Wednesday")
assert_test("Slot freed after cancellation", "11:00 AM" in slots_after.get("available_slots", []), slots_after)

# Cancel already-cancelled
r7 = cancel_appointment(booked_id)
assert_test("Double-cancel handled gracefully", "message" in r7, r7)

# Cancel non-existent
r8 = cancel_appointment(999)
assert_test("Cancel non-existent returns error", "error" in r8, r8)


# ─────────────────────────────────────────────
section("TEST 7: Multiple Bookings + Full Day")
reset_db()

days_times = [
    ("Thursday", "10:00 AM"), ("Thursday", "11:00 AM"),
    ("Thursday", "12:00 PM"), ("Thursday", "5:00 PM"), ("Thursday", "6:00 PM"),
]
for i, (day, time) in enumerate(days_times):
    print(f"  Booking slot {i+1}/5 for Thursday...")
    book_appointment(f"Child {i}", "3 saal", f"Parent {i}", f"900000000{i}", day, time, "checkup")

full_day = check_available_slots("Thursday")
assert_test("Thursday fully booked - no slots left", full_day.get("available_slots") == [], full_day)
show_agent_says("Sorry ji, Thursday ko koi slot available nahi hai. Kisi aur din try karein.")


# ─────────────────────────────────────────────
section("TEST 8: Conversation Flow Validation")
print(f"\n  {INFO} Sample call script (Agent should speak like this):\n")

script = [
    ("Agent",  "Namaste! Aap Neha Child Care clinic mein aayi hain. Kis ke liye appointment book karni hai?"),
    ("Caller", "Meri beti Meera hai, 2 saal ki, uske liye appointment chahiye."),
    ("Agent",  "Ji bilkul. Meera ki umar 2 saal hai, theek hai. Aapka naam kya hai?"),
    ("Caller", "Main Sunita Kapoor bol rahi hoon."),
    ("Agent",  "Haan ji Sunita ji. Apna contact number bata dijiye."),
    ("Caller", "9876543210."),
    ("Agent",  "Theek hai. Kaunsa din aur time convenient rahega? Monday se Saturday available hai."),
    ("Caller", "Saturday ko aana chahungi."),
    ("Agent",  "Saturday ko yeh slots available hain: 9 baje, 10 baje, 11 baje, 12 baje. Kaunsa theek rahega?"),
    ("Caller", "10 baje theek hai."),
    ("Agent",  "Aur Meera ko kya problem ho rahi hai - doctor ko kya batana hai?"),
    ("Caller", "Bukhar aa raha hai 2 din se."),
    ("Agent",  "Theek hai. Toh main confirm karti hoon - Meera Kapoor, 2 saal, Saturday 10 baje, bukhar ke liye. Sahi hai?"),
    ("Caller", "Haan bilkul sahi."),
    ("Agent",  "Bahut accha! Appointment book ho gayi. Appointment ID hai 1. Saturday 10 baje 15 minute pehle aa jaiyega. Koi aur kaam?"),
]

for speaker, line in script:
    color = "\033[96m" if speaker == "Agent" else "\033[97m"
    print(f"  {color}{speaker:7}{RESET}: {line}")


# ─────────────────────────────────────────────
section("RESULTS")
total = passed + failed
print(f"\n  Total: {total}  |  {PASS} {passed}  |  {FAIL} {failed}\n")

if failed == 0:
    print(f"  \033[92mSaare tests pass ho gaye! Agent ready hai.\033[0m\n")
else:
    print(f"  \033[91m{failed} test(s) fail hue. Upar dekho kya galat hai.\033[0m\n")
