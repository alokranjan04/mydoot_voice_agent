# -*- coding: utf-8 -*-
"""
service_graph.py — LangGraph-based conversation orchestrator for Mydoot.

Stage flow:
  category → subcategory → diagnosis → brand → address → preferred_time → customer_name → done

LangGraph tracks collected fields and injects a [STAGE CONTEXT] block into every
Gemini Live turn. The diagnosis stage provides category-specific diagnostic questions
and issue-type routing hints so Gemini can identify the exact fault before booking.
Gemini handles all NLU, language generation, and TTS.
"""

import json
from typing import Optional
from typing_extensions import TypedDict

try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    print("[ServiceGraph] WARNING: langgraph not installed — using fallback state tracker")


# ── Service taxonomy ──────────────────────────────────────────────────────────

CATEGORIES = {
    "Appliance Repair": {
        "subcategories": [
            "Refrigerator", "AC / Air Conditioner", "Washing Machine",
            "TV / Television", "Geyser", "Microwave Oven", "Laptop / Computer",
            "Inverter / UPS", "Water Purifier", "Mixer / Grinder", "Other Appliance",
        ],
        "needs_brand": True,
        "hinglish_label": "Appliance Repair",
    },
    "Plumbing": {
        "subcategories": [
            "Pipe Leak", "Tap / Faucet", "Water Tank", "Toilet / WC",
            "Bathroom Fitting", "Seelan / Dampness / Seepage", "Waterproofing",
            "Other Plumbing",
        ],
        "needs_brand": False,
        "hinglish_label": "Plumbing",
    },
    "Electrical": {
        "subcategories": [
            "Wiring", "MCB / Fuse / DB", "Fan Fitting", "Light / Tube Light Fitting",
            "Switch / Socket", "Short Circuit", "Other Electrical",
        ],
        "needs_brand": False,
        "hinglish_label": "Electrical Work",
    },
    "Carpentry": {
        "subcategories": [
            "Door / Window Repair", "Furniture Repair", "Wardrobe / Cabinet",
            "Lock / Hinge / Handle", "Wooden Flooring", "Other Carpentry",
        ],
        "needs_brand": False,
        "hinglish_label": "Carpentry",
    },
    "Cleaning": {
        "subcategories": [
            "Home / Deep Cleaning", "Sofa / Carpet Cleaning", "AC Deep Clean",
            "Kitchen / Chimney Clean", "Pest Control", "Other Cleaning",
        ],
        "needs_brand": False,
        "hinglish_label": "Cleaning",
    },
    "Vehicle Service": {
        "subcategories": [
            "Car Service / Repair", "Bike / Scooter Service", "Tyre Change",
            "Battery Replacement", "Car Wash / Detailing", "Other Vehicle",
        ],
        "needs_brand": True,
        "hinglish_label": "Vehicle Service",
    },
    "Other": {
        "subcategories": [],
        "needs_brand": False,
        "hinglish_label": "Other Service",
    },
}

STAGE_ORDER = [
    "category",
    "subcategory",
    "diagnosis",       # ask diagnostic questions → identify issue_type + severity
    "brand",           # skipped if category doesn't need brand
    "address",
    "preferred_time",
    "customer_name",
    "done",
]


# ── Diagnostic flows per subcategory ──────────────────────────────────────────
# Each entry provides:
#   issue_types  — possible fault classifications for this subcategory
#   questions    — 2–4 diagnostic questions to narrow down the issue_type
#   hints        — routing rules: which answers map to which issue_type
#   severity_map — rough severity by issue_type: High / Medium / Low

DIAGNOSTIC_FLOWS = {
    # ── Appliance Repair ─────────────────────────────────────────────────────
    "TV / Television": {
        "issue_types": [
            "Power Failure", "Display Failure", "No Sound",
            "Remote Issue", "Input / HDMI Issue", "Other TV Issue",
        ],
        "questions": [
            "Does the power indicator light glow when you plug it in?",
            "Can you hear sound when the screen is black or blank?",
            "Is the issue on all channels and apps, or only some?",
        ],
        "hints": (
            "Power Failure: no indicator light, no response to remote or panel buttons. "
            "Display Failure: sound present but screen is black, blank, or severely distorted. "
            "No Sound: picture visible but audio absent on all sources. "
            "Remote Issue: TV responds to panel buttons but not the remote. "
            "Input/HDMI: specific source (e.g. set-top box) not showing but others work."
        ),
        "severity_map": {
            "Power Failure": "High",
            "Display Failure": "High",
            "No Sound": "Medium",
            "Remote Issue": "Low",
            "Input / HDMI Issue": "Low",
        },
    },
    "Refrigerator": {
        "issue_types": [
            "Cooling Failure", "Water Leakage", "Compressor Noise",
            "Ice Maker Issue", "Door Seal Issue", "Other Fridge Issue",
        ],
        "questions": [
            "Are both the fridge and freezer warm, or only one compartment?",
            "Do you hear the compressor (motor hum) running?",
            "Is water collecting inside the fridge or pooling outside on the floor?",
        ],
        "hints": (
            "Cooling Failure: not cooling at all or insufficiently; compressor may be silent or running non-stop. "
            "Water Leakage: water dripping or pooling inside drawers or outside under the fridge. "
            "Compressor Noise: loud knocking, vibration, or excessive humming from rear. "
            "Ice Maker Issue: freezer cools but ice maker does not produce ice."
        ),
        "severity_map": {
            "Cooling Failure": "High",
            "Water Leakage": "Medium",
            "Compressor Noise": "High",
            "Ice Maker Issue": "Low",
            "Door Seal Issue": "Low",
        },
    },
    "Washing Machine": {
        "issue_types": [
            "Drum Not Spinning", "Drainage Failure", "Water Leakage",
            "Won't Start", "Vibration / Noise", "Error Code", "Other WM Issue",
        ],
        "questions": [
            "Does the drum rotate at all during the wash cycle?",
            "Is water still inside the drum after the cycle ends?",
            "Is any error code shown on the display panel?",
        ],
        "hints": (
            "Drum Not Spinning: drum stationary; motor or belt issue — E-series codes common. "
            "Drainage Failure: water stays in drum; drain pump may be silent. "
            "Water Leakage: water on floor during or after washing. "
            "Won't Start: no response to start button — check door latch and power supply first. "
            "Error Code: record exact code (e.g. E3, F1) for technician."
        ),
        "severity_map": {
            "Drum Not Spinning": "High",
            "Drainage Failure": "High",
            "Water Leakage": "Medium",
            "Won't Start": "High",
            "Vibration / Noise": "Medium",
            "Error Code": "High",
        },
    },
    "AC / Air Conditioner": {
        "issue_types": [
            "Cooling Problem", "Water Leakage Indoor", "No Power",
            "Noise Issue", "Remote Issue", "Other AC Issue",
        ],
        "questions": [
            "Is it a split AC or a window AC?",
            "Is the unit running but not cooling, or not turning on at all?",
            "Is water dripping from the indoor unit (inside the room)?",
            "Is any error code shown on the display?",
        ],
        "hints": (
            "Cooling Problem: AC runs but room temperature does not drop; gas top-up or filter cleaning may be needed. "
            "Water Leakage Indoor: clogged drain pipe; outdoor dripping is normal condensation. "
            "No Power: unit does not respond to remote or panel at all. "
            "Noise Issue: rattling, grinding, or squealing sounds during operation."
        ),
        "severity_map": {
            "Cooling Problem": "High",
            "Water Leakage Indoor": "Medium",
            "No Power": "High",
            "Noise Issue": "Medium",
            "Remote Issue": "Low",
        },
    },
    "Water Purifier": {
        "issue_types": [
            "No Water Output", "Slow Flow", "Bad Taste / Smell",
            "Leakage", "Filter Replacement", "Other RO Issue",
        ],
        "questions": [
            "Is the machine powered on and do you see any indicator light?",
            "Is there no water at all, or just very slow flow?",
            "Which indicator light is on or blinking — red, yellow, or green?",
        ],
        "hints": (
            "No Water Output: red light usually means membrane or filter blockage, or low inlet pressure. "
            "Slow Flow: partially blocked filter; yellow light signals filter replacement due. "
            "Bad Taste/Smell: post-carbon filter exhausted or storage tank needs sanitisation."
        ),
        "severity_map": {
            "No Water Output": "High",
            "Slow Flow": "Medium",
            "Bad Taste / Smell": "Medium",
            "Leakage": "Medium",
            "Filter Replacement": "Low",
        },
    },
    "Geyser": {
        "issue_types": [
            "No Hot Water", "Water Leakage", "Heating Too Slow",
            "MCB Tripping", "Other Geyser Issue",
        ],
        "questions": [
            "Is the pilot or indicator light on when you switch it on?",
            "Is there no hot water at all, or is it heating very slowly?",
            "Is there any water dripping from the geyser body, pipe, or relief valve?",
        ],
        "hints": (
            "No Hot Water: element may be fused; check if the MCB for the geyser circuit is tripped. "
            "Water Leakage: from pressure relief valve (normal if inlet pressure too high), pipe joint, or tank body. "
            "MCB Tripping: possible element short circuit — do not reset without inspection."
        ),
        "severity_map": {
            "No Hot Water": "High",
            "Water Leakage": "High",
            "Heating Too Slow": "Medium",
            "MCB Tripping": "High",
        },
    },
    "Laptop / Computer": {
        "issue_types": [
            "Won't Power On", "Overheating / Shutdown", "Screen Issue",
            "Keyboard / Trackpad Issue", "Slow Performance", "Virus / Software Issue",
            "Battery Not Charging", "Other Laptop Issue",
        ],
        "questions": [
            "Does any indicator light come on when you press the power button?",
            "Is the screen completely blank, or does it show something before going off?",
            "Does the laptop feel very hot before shutting down?",
        ],
        "hints": (
            "Won't Power On: no lights at all — could be battery or power board. "
            "Overheating: shuts down after a few minutes; fan may be clogged. "
            "Screen Issue: lines, blank screen, backlight failure. "
            "Battery Not Charging: stays at same percentage or shows 'not charging'."
        ),
        "severity_map": {
            "Won't Power On": "High",
            "Overheating / Shutdown": "High",
            "Screen Issue": "High",
            "Battery Not Charging": "Medium",
            "Slow Performance": "Low",
        },
    },
    # ── Plumbing ─────────────────────────────────────────────────────────────
    "Pipe Leak": {
        "issue_types": [
            "Tap / Faucet Leakage", "Pipe Joint Leak", "Main Supply Leak",
            "Seepage / Hidden Leak", "Other Plumbing Issue",
        ],
        "questions": [
            "Is the leak from a tap or faucet, or from a pipe joint or wall?",
            "Is it dripping continuously or only when taps are open?",
            "Is there any dampness or water stain on the wall or ceiling nearby?",
        ],
        "hints": (
            "Tap/Faucet Leakage: worn washer or O-ring — relatively easy fix. "
            "Pipe Joint Leak: fitting loose or cracked; check under sinks and behind walls. "
            "Seepage/Hidden Leak: water seeping through wall — test by closing all taps and watching water meter."
        ),
        "severity_map": {
            "Tap / Faucet Leakage": "Low",
            "Pipe Joint Leak": "Medium",
            "Main Supply Leak": "High",
            "Seepage / Hidden Leak": "Medium",
        },
    },
    "Tap / Faucet": {
        "issue_types": [
            "Dripping Tap", "Low Water Pressure", "Tap Won't Close",
            "Tap Handle Broken", "Other Tap Issue",
        ],
        "questions": [
            "Is the tap dripping even when turned fully off?",
            "Is the water pressure fine in other taps in the house?",
        ],
        "hints": (
            "Dripping Tap: washer or cartridge worn. "
            "Low Pressure: check aerator for blockage — often a quick clean fixes it. "
            "Tap Won't Close: valve seat damaged; needs replacement."
        ),
        "severity_map": {
            "Dripping Tap": "Low",
            "Low Water Pressure": "Medium",
            "Tap Won't Close": "Medium",
            "Tap Handle Broken": "Low",
        },
    },
    "Toilet / WC": {
        "issue_types": [
            "Flush Not Working", "Continuous Running Water", "Leakage at Base",
            "Blockage / Choke", "Flush Handle Broken", "Other WC Issue",
        ],
        "questions": [
            "Is the flush not working at all, or is water running into the bowl continuously?",
            "Is there any water on the floor near the base of the toilet?",
        ],
        "hints": (
            "Flush Not Working: float valve stuck or flush button disconnected. "
            "Continuous Running: float set too high or flapper valve worn. "
            "Leakage at Base: wax ring seal damaged — needs replacement. "
            "Blockage: toilet not flushing fully; may need drain rod."
        ),
        "severity_map": {
            "Flush Not Working": "High",
            "Continuous Running Water": "Medium",
            "Leakage at Base": "High",
            "Blockage / Choke": "High",
        },
    },
    "Seelan / Dampness / Seepage": {
        "issue_types": [
            "Wall Seepage", "Ceiling Dampness", "Floor Damp / Rising Damp",
            "Balcony / Terrace Leakage", "Other Seepage Issue",
        ],
        "questions": [
            "Is the dampness on an internal wall, an external wall, or the ceiling?",
            "Is it getting worse during or after rain?",
            "Is there a bathroom, kitchen, or water tank above the affected area?",
        ],
        "hints": (
            "Wall Seepage: from above (tank/bathroom) or from outside (rain/external wall crack). "
            "Ceiling Dampness: usually a plumbing leak from the floor above. "
            "Balcony/Terrace: waterproofing membrane failed."
        ),
        "severity_map": {
            "Wall Seepage": "Medium",
            "Ceiling Dampness": "High",
            "Balcony / Terrace Leakage": "High",
        },
    },
    # ── Electrical ───────────────────────────────────────────────────────────
    "Wiring": {
        "issue_types": [
            "Socket / Light Point Failure", "MCB Tripping", "Short Circuit",
            "New Point / Extension", "Wiring Damage", "Other Electrical Issue",
        ],
        "questions": [
            "Kya sirf ek light ya socket kaam nahi kar rahi, ya poore kamre mein power nahi hai? (e.g. ek bulb band, ya pura room dark ho gaya)",
            "Kya distribution board (DB/switch board) mein koi switch neeche gir gaya hai — MCB trip hua hai?",
            "Kya koi jalne ki smell aa rahi hai, ya wire/socket mein koi daag ya damage dikh raha hai?",
        ],
        "hints": (
            "Socket/Light Point Failure: if other points in the room work, isolated point fault — likely loose connection or blown fuse point. "
            "MCB Tripping: MCB switch in DB board fell down — overload or short in that circuit. "
            "Short Circuit: burn marks, sparks, or burning smell — safety risk."
        ),
        "severity_map": {
            "Socket / Light Point Failure": "Medium",
            "MCB Tripping": "High",
            "Short Circuit": "High",
            "New Point / Extension": "Low",
            "Wiring Damage": "High",
        },
    },
    "Light / Tube Light Fitting": {
        "issue_types": [
            "Light Not Working", "Flickering Light", "Fitting Replacement",
            "New Light Installation", "Starter / Choke Issue", "Other Light Issue",
        ],
        "questions": [
            "Kya sirf ek light band hai, ya us room ki saari lights nahi chal rahi? (e.g. ek bulb fused, ya pura kamra dark)",
            "Kya switch press karne par light bilkul nahi aati, ya flicker karti hai / kam roshan hai?",
            "Kya aapne bulb/tube already badal ke dekha, ya abhi tak nahi badla?",
        ],
        "hints": (
            "Light Not Working: single bulb fused or holder loose — simple fix. "
            "Multiple lights out: check MCB in DB board — may have tripped. "
            "Flickering Light: loose wire in switch or holder, or starter/choke issue in tube light. "
            "Fitting Replacement: holder or batten damaged — needs new fitting."
        ),
        "severity_map": {
            "Light Not Working": "Medium",
            "Flickering Light": "Low",
            "Fitting Replacement": "Low",
            "New Light Installation": "Low",
            "Starter / Choke Issue": "Low",
        },
    },
    "MCB / Fuse / DB": {
        "issue_types": [
            "MCB Keeps Tripping", "Fuse Blown", "DB Board Issue",
            "Complete Power Loss", "Other MCB Issue",
        ],
        "questions": [
            "Does the MCB trip immediately when switched on, or only after a while?",
            "Does it trip when a specific appliance (like AC) is used?",
            "Is the issue affecting the whole house or only certain rooms?",
        ],
        "hints": (
            "MCB Keeps Tripping: overload (too many appliances) or fault in a specific circuit. "
            "DB Board Issue: main isolator tripping — all circuits dead. "
            "Complete Power Loss: check if utility supply is present on mains."
        ),
        "severity_map": {
            "MCB Keeps Tripping": "High",
            "Fuse Blown": "High",
            "DB Board Issue": "High",
            "Complete Power Loss": "High",
        },
    },
    "Switch / Socket": {
        "issue_types": [
            "Socket Not Working", "Switch Not Working", "Spark from Socket",
            "Socket Burnt", "New Socket Installation", "Other Switch Issue",
        ],
        "questions": [
            "Is the socket or switch completely dead, or does it work intermittently?",
            "Is there any burning smell, spark, or discolouration on the socket?",
        ],
        "hints": (
            "Socket Not Working: check MCB first; if others work → isolated socket fault. "
            "Spark from Socket: immediate safety risk; switch off MCB for that circuit."
        ),
        "severity_map": {
            "Socket Not Working": "Medium",
            "Spark from Socket": "High",
            "Socket Burnt": "High",
            "Switch Not Working": "Low",
        },
    },
    # ── Carpentry ────────────────────────────────────────────────────────────
    "Door / Window Repair": {
        "issue_types": [
            "Hinge Issue", "Lock / Handle Broken", "Frame Damage",
            "Alignment Issue", "Glass Broken / Cracked", "Other Carpentry Issue",
        ],
        "questions": [
            "Is the issue with the hinges, the lock, the handle, or the door frame?",
            "Does the door not close at all, or does it close but not latch properly?",
        ],
        "hints": (
            "Hinge Issue: door sags or scrapes floor; screws loose or hinge broken. "
            "Lock/Handle Broken: handle moves but latch or bolt does not engage. "
            "Alignment Issue: door does not meet the frame squarely — rehinging or frame planing needed."
        ),
        "severity_map": {
            "Hinge Issue": "Medium",
            "Lock / Handle Broken": "High",
            "Frame Damage": "High",
            "Alignment Issue": "Low",
            "Glass Broken / Cracked": "High",
        },
    },
    "Furniture Repair": {
        "issue_types": [
            "Assembly Required", "Joint / Leg Broken", "Drawer Issue",
            "Hinge / Hardware Replacement", "Polishing / Refinishing", "Other Furniture Issue",
        ],
        "questions": [
            "Is it a new piece that needs assembling, or an existing piece that is damaged?",
            "Which part is the problem — joint, leg, drawer, hinge, or surface finish?",
        ],
        "hints": (
            "Assembly Required: new flat-pack furniture; need tools and all hardware included. "
            "Joint/Leg Broken: structural damage — wood glue, dowels, or replacement part needed."
        ),
        "severity_map": {
            "Assembly Required": "Low",
            "Joint / Leg Broken": "Medium",
            "Drawer Issue": "Low",
        },
    },
    "Wardrobe / Cabinet": {
        "issue_types": [
            "Shutter / Door Issue", "Hinge / Slider Broken", "Assembly / Installation",
            "Lock Issue", "Shelf Collapse", "Other Wardrobe Issue",
        ],
        "questions": [
            "Is the wardrobe door not closing, off track, or falling?",
            "Is the problem with a hinge, sliding track, or internal shelf?",
        ],
        "hints": (
            "Shutter/Door Issue: door not closing — hinge loose or warped door. "
            "Hinge/Slider Broken: sliding wardrobe off track — track alignment or roller replacement needed."
        ),
        "severity_map": {
            "Shutter / Door Issue": "Medium",
            "Shelf Collapse": "High",
            "Assembly / Installation": "Low",
        },
    },
    # ── Vehicle Service ───────────────────────────────────────────────────────
    "Car Service / Repair": {
        "issue_types": [
            "Won't Start", "Engine Warning Light", "Brake Problem",
            "Battery Dead", "Tyre Issue", "AC Not Cooling", "Other Car Issue",
        ],
        "questions": [
            "Is the car not starting at all, or is there a problem while driving?",
            "Are any warning lights showing on the dashboard?",
            "Is it a petrol, diesel, or electric vehicle?",
        ],
        "hints": (
            "Won't Start: completely silent → battery; cranks but won't start → fuel or ignition. "
            "Brake Problem: squealing/grinding → worn pads; spongy pedal → brake fluid issue. "
            "Engine Warning Light: OBD scan required to read fault code."
        ),
        "severity_map": {
            "Won't Start": "High",
            "Engine Warning Light": "High",
            "Brake Problem": "High",
            "Battery Dead": "High",
            "Tyre Issue": "Medium",
            "AC Not Cooling": "Medium",
        },
    },
    "Bike / Scooter Service": {
        "issue_types": [
            "Won't Start", "Engine Noise", "Brake Issue",
            "Puncture", "Electrical Fault", "Other Bike Issue",
        ],
        "questions": [
            "Is the bike not starting, or is there a problem while riding?",
            "Any unusual sounds from the engine — knocking, rattling, or misfiring?",
            "Is it a petrol bike or an electric scooter?",
        ],
        "hints": (
            "Won't Start: check fuel, battery, and choke setting. "
            "Engine Noise: knocking usually means low oil; rattling may be valve chain or loose heat shield."
        ),
        "severity_map": {
            "Won't Start": "High",
            "Brake Issue": "High",
            "Engine Noise": "Medium",
            "Puncture": "Medium",
        },
    },
    # ── Cleaning ─────────────────────────────────────────────────────────────
    "Home / Deep Cleaning": {
        "issue_types": [
            "Full Home Cleaning", "Kitchen Deep Clean", "Bathroom Deep Clean",
            "Post-Construction Cleaning", "Move-In / Move-Out Cleaning", "Other Cleaning",
        ],
        "questions": [
            "How many rooms need cleaning?",
            "Is this a regular clean or a deep/post-construction clean?",
        ],
        "hints": (
            "Post-Construction: heavy dust and cement residue; more time needed. "
            "Move-In/Move-Out: often requires full scrub including inside cupboards and fixtures."
        ),
        "severity_map": {},
    },
    "Pest Control": {
        "issue_types": [
            "Cockroach Treatment", "Termite Treatment", "Bed Bug Treatment",
            "Rodent Control", "Mosquito Control", "General Pest Control",
        ],
        "questions": [
            "Which pest is the main problem — cockroaches, termites, bed bugs, or rodents?",
            "Is the infestation in the whole home or a specific area (kitchen, bedroom)?",
        ],
        "hints": (
            "Termite Treatment: check wooden furniture, flooring, and skirting for mud tunnels. "
            "Bed Bug Treatment: check mattress seams and headboard."
        ),
        "severity_map": {
            "Termite Treatment": "High",
            "Bed Bug Treatment": "High",
        },
    },
}

# Default flow for subcategories not explicitly listed
DEFAULT_DIAGNOSTIC = {
    "issue_types": ["Repair Required", "Installation / Fitting", "Inspection", "Other"],
    "questions": ["Can you describe the exact problem in more detail?"],
    "hints": "Identify the specific issue type from the customer's description.",
    "severity_map": {},
}


# ── Simple services: skip diagnosis and brand stages ─────────────────────────
# These subcategories have a self-evident issue type. No diagnostic questions
# and no brand are needed — stage jumps directly from subcategory to address.
#
# key   = subcategory name (must match CATEGORIES subcategories list exactly)
# value = (issue_type, severity) to auto-fill when subcategory is detected

AUTO_ISSUE_TYPES: dict[str, tuple[str, str]] = {
    "Car Wash / Detailing":    ("Car Wash / Detailing",        "Low"),
    "Tyre Change":             ("Tyre Change",                 "Medium"),
    "Battery Replacement":     ("Battery Replacement",         "High"),
    "Home / Deep Cleaning":    ("Full Home Cleaning",          "Low"),
    "Sofa / Carpet Cleaning":  ("Sofa / Carpet Cleaning",      "Low"),
    "AC Deep Clean":           ("AC Deep Clean",               "Low"),
    "Kitchen / Chimney Clean": ("Kitchen / Chimney Cleaning",  "Low"),
    "Pest Control":            ("General Pest Control",        "Low"),
    "Other Cleaning":          ("Cleaning Service",            "Low"),
}

# Subcategory names that skip both diagnosis and brand stages.
SIMPLE_SERVICES: frozenset[str] = frozenset(AUTO_ISSUE_TYPES)


# ── State definition ──────────────────────────────────────────────────────────

class ServiceState(TypedDict):
    stage:            str
    category:         Optional[str]    # Appliance Repair, Plumbing, Electrical, etc.
    subcategory:      Optional[str]    # Refrigerator, Pipe Leak, Wiring, etc.
    issue_type:       Optional[str]    # Power Failure, Cooling Failure, etc.
    brand:            Optional[str]
    model:            Optional[str]
    severity:         Optional[str]    # High / Medium / Low (derived from issue_type)
    error_code:       Optional[str]    # E3, F1, etc. — for appliances with error displays
    warranty_status:  Optional[str]
    address:          Optional[str]
    preferred_time:   Optional[str]
    customer_name:    Optional[str]
    save_done:        bool


def initial_state() -> ServiceState:
    return ServiceState(
        stage="category",
        category=None, subcategory=None, issue_type=None,
        brand=None, model=None,
        severity=None, error_code=None, warranty_status=None,
        address=None, preferred_time=None, customer_name=None,
        save_done=False,
    )


# ── State update helpers ──────────────────────────────────────────────────────

def update_from_tool_args(state: ServiceState, args: dict) -> ServiceState:
    """
    Merge save_service_request tool arguments into state.
    Called when Gemini fires the save_service_request tool.
    """
    new = dict(state)
    for field in ["category", "subcategory", "issue_type", "brand", "model",
                  "severity", "error_code", "warranty_status",
                  "address", "preferred_time", "customer_name"]:
        val = args.get(field)
        if val and str(val).strip():
            new[field] = str(val).strip()
    # Auto-derive severity if not provided but issue_type is known
    if not new.get("severity") and new.get("subcategory") and new.get("issue_type"):
        flow = DIAGNOSTIC_FLOWS.get(new["subcategory"], DEFAULT_DIAGNOSTIC)
        new["severity"] = flow.get("severity_map", {}).get(new["issue_type"], "Medium")
    new["save_done"] = True
    new["stage"] = "done"
    return ServiceState(**new)


def advance_stage(state: ServiceState) -> ServiceState:
    """Move to the next stage after a field is confirmed collected."""
    new = dict(state)
    cat_info  = CATEGORIES.get(state.get("category", "Other"), {})
    needs_brand = cat_info.get("needs_brand", False)
    current = state.get("stage", "category")

    if current == "category":
        if cat_info.get("subcategories"):
            new["stage"] = "subcategory"
        else:
            new["stage"] = "diagnosis"

    elif current == "subcategory":
        subcat = state.get("subcategory", "")
        if subcat in SIMPLE_SERVICES:
            # Auto-fill issue_type + severity; skip diagnosis and brand entirely
            it, sv = AUTO_ISSUE_TYPES[subcat]
            if not new.get("issue_type"):
                new["issue_type"] = it
            if not new.get("severity"):
                new["severity"] = sv
            new["stage"] = "address"
        else:
            new["stage"] = "diagnosis"

    elif current == "diagnosis":
        subcat = state.get("subcategory", "")
        if needs_brand and subcat not in SIMPLE_SERVICES:
            new["stage"] = "brand"
        else:
            new["stage"] = "address"

    elif current == "brand":
        new["stage"] = "address"

    elif current == "address":
        new["stage"] = "preferred_time"

    elif current == "preferred_time":
        new["stage"] = "customer_name"

    elif current == "customer_name":
        new["stage"] = "done"

    return ServiceState(**new)


# ── Context injection ─────────────────────────────────────────────────────────

# Fields included in the [STAGE CONTEXT] "Collected" summary
_COLLECTED_FIELDS = (
    "category", "subcategory", "issue_type", "brand", "model",
    "severity", "error_code", "address", "preferred_time", "customer_name",
)


def _build_collected(state: ServiceState) -> dict:
    """Return dict of non-empty collected fields for stage context display."""
    return {k: state[k] for k in _COLLECTED_FIELDS if state.get(k)}


def get_stage_context(state: ServiceState) -> str:
    """
    Generate the [STAGE CONTEXT] block prepended to every Gemini clientContent turn.
    Tells Gemini the current stage, collected fields, and exactly what to do next.
    """
    stage       = state.get("stage", "category")
    cat         = state.get("category")
    subcat      = state.get("subcategory")
    issue_type  = state.get("issue_type")
    cat_info    = CATEGORIES.get(cat or "Other", {})
    subcats     = cat_info.get("subcategories", [])
    needs_brand = cat_info.get("needs_brand", False)

    collected = _build_collected(state)

    # ── Stage-specific instruction ─────────────────────────────────────────
    if stage == "category":
        cat_list = " / ".join(CATEGORIES.keys())
        instruction = (
            f"ASK: What type of service does the customer need? "
            f"Router — classify from description into one of: {cat_list}. "
            f"Confidence threshold: detect even from partial descriptions "
            f"('fridge nahi chal raha' → Appliance Repair / Refrigerator). "
            f"Once category detected, immediately move to subcategory or diagnosis."
        )

    elif stage == "subcategory":
        if subcats:
            opts = ", ".join(subcats[:7])
            instruction = (
                f"Category confirmed: '{cat}'. "
                f"ASK: Which specific type? Give a brief example to help the customer understand. "
                f"Options: {opts}. "
                f"Frame as a natural Hinglish question with 2-3 examples, e.g. "
                f"'Kaunsa kaam chahiye — wiring, MCB/fuse, light fitting, ya switch-socket?'"
            )
        else:
            instruction = f"Category confirmed: '{cat}'. ASK: Describe the specific problem with an example."

    elif stage == "diagnosis":
        what = subcat or cat or "this"
        flow = DIAGNOSTIC_FLOWS.get(subcat or "", DEFAULT_DIAGNOSTIC)
        issue_opts = " / ".join(flow["issue_types"])
        q_list     = " | ".join(flow["questions"][:3])
        hints      = flow.get("hints", "")
        instruction = (
            f"Subcategory: '{what}'. Run DIAGNOSTIC to identify the issue type. "
            f"Possible issue types: {issue_opts}. "
            f"Ask these diagnostic questions ONE AT A TIME in simple Hinglish. "
            f"IMPORTANT: Each question must include 2-3 concrete examples in brackets so the customer "
            f"understands — do NOT ask abstract questions like 'kya specific dikkat hai' or "
            f"'diagnosis batao'. Ask specific yes/no or choice questions with examples: {q_list}. "
            f"If customer says 'pata nahi', 'electrician dekhega', or cannot answer → "
            f"pick the most likely issue type from routing hints and say "
            f"'Main sabse common problem select kar rahi hoon — [issue type].' then move on. "
            f"Routing hints: {hints} "
            f"If error code mentioned (e.g. E3, F1), record it. "
            f"Once issue type is clear, record issue_type (and severity if obvious) then proceed."
        )

    elif stage == "brand":
        what = subcat or cat or "it"
        instruction = f"ASK: Which brand is {what}? (e.g. Samsung, LG, Maruti, Honda)"

    elif stage == "address":
        instruction = (
            "ASK: What is your address? We need your society/building name and "
            "area or locality to send a technician."
        )

    elif stage == "preferred_time":
        instruction = (
            "ASK: When would you like the technician to visit? "
            "Preferred date and time? (e.g. 'kal subah 10 baje', 'aaj shaam 5 baje', 'Sunday morning')"
        )

    elif stage == "customer_name":
        instruction = (
            "ASK the customer's name directly — do NOT ask yes/no permission. "
            "Hinglish: 'Aapka naam kya hai?' / English: 'What is your name?' "
            "One direct question only. If customer responds with only 'haan', 'ji', 'yes', or "
            "any affirmative without stating a name — ask again: 'Kripya apna naam batayein.'"
        )

    elif stage == "done":
        summary = json.dumps(collected, ensure_ascii=False)
        instruction = (
            f"ALL FIELDS COLLECTED: {summary}. "
            "Call save_service_request tool IMMEDIATELY — do NOT say anything to the customer "
            "before calling the tool. After tool success, speak the confirmation — "
            "first word must be the customer's name: "
            "'[name] ji, aapki request register ho gayi hai. Hamari team jald se jald aapse sampark karegi. MyDoot ko call karne ke liye shukriya!' "
            "Say it ONCE only. Then go COMPLETELY SILENT. Do not repeat."
        )

    else:
        instruction = "Continue the conversation to collect remaining service request details."

    lines = [
        "[STAGE CONTEXT — SILENT INTERNAL INSTRUCTIONS — do NOT speak these lines]",
        f"Stage       : {stage}",
    ]
    if collected:
        lines.append(f"Collected   : {json.dumps(collected, ensure_ascii=False)}")
    lines.append(f"Instruction : {instruction}")
    lines.append("[END STAGE CONTEXT — your spoken response begins here]")

    return "\n".join(lines)


def get_confirmation_context(state: ServiceState, field: str, value: str) -> str:
    """
    Generate [STAGE CONTEXT] for the confirmation sub-step.
    Injected when the pipeline has captured a value but is waiting for
    the customer to say haan/yes before the stage advances.
    """
    collected = _build_collected(state)

    field_display = {
        "address": "address",
        "preferred_time": "preferred time",
        "customer_name": "name",
    }.get(field, field)

    next_hint = {
        "address": "advance to preferred_time",
        "preferred_time": "advance to customer_name",
        "customer_name": "advance to done, then call save_service_request immediately",
    }.get(field, "advance to next step")

    instruction = (
        f"You just collected {field_display} = \"{value}\". "
        f"CONFIRM with a brief echo — one sentence only: "
        f"Hinglish — \"{value}, sahi hai?\" / English — \"{value} — is that correct?\" "
        f"If customer says haan/yes/theek/bilkul/sahi/correct → {next_hint}. "
        f"If customer says nahi/no or gives a different value → note the correction and confirm the new value before advancing."
    )

    lines = [
        "[STAGE CONTEXT — SILENT INTERNAL INSTRUCTIONS — do NOT speak these lines]",
        f"Stage       : confirming {field_display}",
    ]
    if collected:
        lines.append(f"Collected   : {json.dumps(collected, ensure_ascii=False)}")
    lines.append(f"Instruction : {instruction}")
    lines.append("[END STAGE CONTEXT — your spoken response begins here]")
    return "\n".join(lines)


# ── Lightweight state machine ─────────────────────────────────────────────────

class ServiceGraph:
    """
    Thin wrapper holding per-call ServiceState with context injection.

    If langgraph is installed, a StateGraph is built for explicit routing;
    otherwise the same advance_stage() logic runs directly.
    """

    def __init__(self):
        self.state: ServiceState = initial_state()
        self._graph = self._build_graph() if LANGGRAPH_AVAILABLE else None
        self._pending: Optional[dict] = None  # {"field": str, "value": str} — awaiting confirmation

    def _build_graph(self):
        from langgraph.graph import StateGraph as SG, END as LG_END

        def _category_node(s):     return advance_stage(s) if s.get("category") else s
        def _subcategory_node(s):  return advance_stage(s) if s.get("subcategory") else s
        def _diagnosis_node(s):    return advance_stage(s) if s.get("issue_type") else s
        def _brand_node(s):        return advance_stage(s) if s.get("brand") else s
        def _address_node(s):      return advance_stage(s) if s.get("address") else s
        def _time_node(s):         return advance_stage(s) if s.get("preferred_time") else s
        def _name_node(s):         return advance_stage(s) if s.get("customer_name") else s
        def _done_node(s):         return s

        g = SG(ServiceState)
        for name, fn in [
            ("category",       _category_node),
            ("subcategory",    _subcategory_node),
            ("diagnosis",      _diagnosis_node),
            ("brand",          _brand_node),
            ("address",        _address_node),
            ("preferred_time", _time_node),
            ("customer_name",  _name_node),
            ("done",           _done_node),
        ]:
            g.add_node(name, fn)

        g.set_entry_point("category")
        g.add_edge("category",       "subcategory")
        g.add_edge("subcategory",    "diagnosis")
        g.add_edge("diagnosis",      "brand")
        g.add_edge("brand",          "address")
        g.add_edge("address",        "preferred_time")
        g.add_edge("preferred_time", "customer_name")
        g.add_edge("customer_name",  "done")
        g.add_edge("done",           LG_END)

        return g.compile()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def pending(self) -> Optional[dict]:
        """Currently pending field/value awaiting customer confirmation, or None."""
        return self._pending

    def set_pending(self, field: str, value: str) -> None:
        """Store a captured value pending confirmation. Does NOT advance stage yet."""
        self._pending = {"field": field, "value": value}

    def confirm_pending(self) -> None:
        """Customer confirmed — commit the pending value and advance the stage."""
        if self._pending:
            field, value = self._pending["field"], self._pending["value"]
            self._pending = None
            self.on_field_collected(field, value)

    def clear_pending(self) -> None:
        """Customer corrected or unclear — discard pending without advancing."""
        self._pending = None

    def get_context(self) -> str:
        """Return the [STAGE CONTEXT] block to prepend to this Gemini turn."""
        if self._pending:
            return get_confirmation_context(
                self.state, self._pending["field"], self._pending["value"]
            )
        return get_stage_context(self.state)

    def on_field_collected(self, field: str, value: str):
        """Call when Gemini mentions or implies a field value mid-conversation."""
        if field in self.state and value:
            self.state = ServiceState(**{**self.state, field: value})
            stage_to_field = {
                "category": "category", "subcategory": "subcategory",
                "diagnosis": "issue_type", "brand": "brand",
                "address": "address", "preferred_time": "preferred_time",
                "customer_name": "customer_name",
            }
            if stage_to_field.get(self.state["stage"]) == field:
                self.state = advance_stage(self.state)

    def on_tool_call(self, args: dict):
        """Call when save_service_request tool fires. Merges all args into state."""
        self.state = update_from_tool_args(self.state, args)

    def current_stage(self) -> str:
        return self.state.get("stage", "category")

    def is_done(self) -> bool:
        return self.state.get("save_done", False)

    def get_diagnostic_flow(self) -> dict:
        """Return the diagnostic flow for the current subcategory (for logging)."""
        subcat = self.state.get("subcategory", "")
        return DIAGNOSTIC_FLOWS.get(subcat, DEFAULT_DIAGNOSTIC)
