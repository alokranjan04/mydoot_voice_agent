# -*- coding: utf-8 -*-
"""
service_graph.py — LangGraph-based conversation orchestrator for Mydoot.

Manages the structured conversation flow:
  Category → Subcategory → Problem → Details (brand/model/address/time) → Name → Save

LangGraph tracks which fields are collected and injects stage-specific instructions
into every Gemini Live turn via a [STAGE CONTEXT] block. Gemini handles all NLU,
language generation, and TTS — LangGraph provides the routing rails.
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
    "problem",
    "brand",       # skipped if category doesn't need brand
    "address",
    "preferred_time",
    "customer_name",
    "done",
]


# ── State definition ──────────────────────────────────────────────────────────

class ServiceState(TypedDict):
    stage:          str              # current stage (see STAGE_ORDER)
    category:       Optional[str]
    subcategory:    Optional[str]
    problem:        Optional[str]
    brand:          Optional[str]    # None means "not yet asked / not applicable"
    model:          Optional[str]    # always optional — Gemini may or may not collect
    address:        Optional[str]
    preferred_time: Optional[str]
    customer_name:  Optional[str]
    save_done:      bool


def initial_state() -> ServiceState:
    return ServiceState(
        stage="category",
        category=None, subcategory=None, problem=None,
        brand=None, model=None, address=None,
        preferred_time=None, customer_name=None,
        save_done=False,
    )


# ── State update helpers ──────────────────────────────────────────────────────

def update_from_tool_args(state: ServiceState, args: dict) -> ServiceState:
    """
    Merge tool-call arguments into the state.
    Called when Gemini fires the save_service_request tool.
    """
    new = dict(state)
    for field in ["category", "subcategory", "problem", "brand", "model",
                  "address", "preferred_time", "customer_name"]:
        val = args.get(field)
        if val and str(val).strip():
            new[field] = str(val).strip()
    new["save_done"] = True
    new["stage"] = "done"
    return ServiceState(**new)


def advance_stage(state: ServiceState) -> ServiceState:
    """
    Move to the next stage after a field is confirmed collected.
    Call this after the Gemini response confirms the field was given.
    """
    new = dict(state)
    cat_info = CATEGORIES.get(state.get("category", "Other"), {})
    needs_brand = cat_info.get("needs_brand", False)

    current = state.get("stage", "category")

    if current == "category":
        if cat_info.get("subcategories"):
            new["stage"] = "subcategory"
        else:
            new["stage"] = "problem"

    elif current == "subcategory":
        new["stage"] = "problem"

    elif current == "problem":
        if needs_brand:
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

def get_stage_context(state: ServiceState) -> str:
    """
    Generate the [STAGE CONTEXT] block that is prepended to every Gemini
    clientContent turn. Tells Gemini exactly what stage it's in, what is
    already collected, and what single question to ask next.
    """
    stage   = state.get("stage", "category")
    cat     = state.get("category")
    subcat  = state.get("subcategory")
    cat_info = CATEGORIES.get(cat or "Other", {})
    subcats = cat_info.get("subcategories", [])
    needs_brand = cat_info.get("needs_brand", False)

    # Collected summary
    collected = {}
    for k in ["category", "subcategory", "problem", "brand", "model",
              "address", "preferred_time", "customer_name"]:
        if state.get(k):
            collected[k] = state[k]

    # Stage-specific next instruction
    if stage == "category":
        cat_list = " / ".join(CATEGORIES.keys())
        instruction = (
            f"ASK: What type of service is needed? "
            f"Detect from customer response: {cat_list}. "
            f"Once detected, update your understanding and move to subcategory or problem."
        )

    elif stage == "subcategory":
        if subcats:
            opts = ", ".join(subcats[:6])
            instruction = (
                f"Category is '{cat}'. "
                f"ASK: Which specific type? Options include: {opts}."
            )
        else:
            instruction = f"Category is '{cat}'. ASK: Describe the problem."

    elif stage == "problem":
        what = subcat or cat or "this"
        instruction = f"ASK: What exactly is the problem with {what}?"

    elif stage == "brand":
        what = subcat or cat or "it"
        instruction = f"ASK: Which brand is {what}? (e.g. Samsung, LG, Maruti, Honda)"

    elif stage == "address":
        instruction = (
            "ASK: What is your address? We need your society name and area/locality "
            "to send a technician."
        )

    elif stage == "preferred_time":
        instruction = (
            "ASK: When would you like the technician to visit? "
            "Preferred date and time? (e.g. 'kal subah 10 baje', 'aaj shaam 5 baje')"
        )

    elif stage == "customer_name":
        instruction = "ASK: May I have your name to register the complaint?"

    elif stage == "done":
        instruction = (
            "ALL FIELDS COLLECTED. "
            "Say the WAIT message ('Ek second, register ho raha hai.') then "
            "IMMEDIATELY call save_service_request tool. "
            "After tool success, speak confirmation and stop."
        )

    else:
        instruction = "Continue the conversation."

    lines = [
        "[STAGE CONTEXT — follow these instructions for this turn]",
        f"Stage       : {stage}",
    ]
    if collected:
        lines.append(f"Collected   : {json.dumps(collected, ensure_ascii=False)}")
    lines.append(f"Instruction : {instruction}")
    lines.append("[END STAGE CONTEXT]")

    return "\n".join(lines)


# ── Lightweight state machine (works without langgraph installed) ─────────────

class ServiceGraph:
    """
    Thin wrapper that holds per-call ServiceState and provides context injection.

    If langgraph is installed the graph is built for explicit routing;
    if not, the same advance_stage() logic runs directly.
    """

    def __init__(self):
        self.state: ServiceState = initial_state()
        self._graph = self._build_graph() if LANGGRAPH_AVAILABLE else None

    def _build_graph(self):
        """Build the LangGraph StateGraph for explicit stage routing."""
        from langgraph.graph import StateGraph as SG, END as LG_END

        def _category_node(s):     return advance_stage(s) if s.get("category") else s
        def _subcategory_node(s):  return advance_stage(s) if s.get("subcategory") else s
        def _problem_node(s):      return advance_stage(s) if s.get("problem") else s
        def _brand_node(s):        return advance_stage(s) if s.get("brand") else s
        def _address_node(s):      return advance_stage(s) if s.get("address") else s
        def _time_node(s):         return advance_stage(s) if s.get("preferred_time") else s
        def _name_node(s):         return advance_stage(s) if s.get("customer_name") else s
        def _done_node(s):         return s

        g = SG(ServiceState)
        for name, fn in [
            ("category",       _category_node),
            ("subcategory",    _subcategory_node),
            ("problem",        _problem_node),
            ("brand",          _brand_node),
            ("address",        _address_node),
            ("preferred_time", _time_node),
            ("customer_name",  _name_node),
            ("done",           _done_node),
        ]:
            g.add_node(name, fn)

        g.set_entry_point("category")
        g.add_edge("category",       "subcategory")
        g.add_edge("subcategory",    "problem")
        g.add_edge("problem",        "brand")
        g.add_edge("brand",          "address")
        g.add_edge("address",        "preferred_time")
        g.add_edge("preferred_time", "customer_name")
        g.add_edge("customer_name",  "done")
        g.add_edge("done",           LG_END)

        return g.compile()

    # ── Public API ──────────────────────────────────────────────────────────

    def get_context(self) -> str:
        """Return the [STAGE CONTEXT] block to prepend to this Gemini turn."""
        return get_stage_context(self.state)

    def on_field_collected(self, field: str, value: str):
        """Call when Gemini mentions or implies a field value mid-conversation."""
        if field in self.state and value:
            self.state = ServiceState(**{**self.state, field: value})
            # Auto-advance stage if the just-collected field matches current stage
            stage_to_field = {
                "category": "category", "subcategory": "subcategory",
                "problem": "problem", "brand": "brand",
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
