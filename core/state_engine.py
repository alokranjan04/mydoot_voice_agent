# -*- coding: utf-8 -*-
"""
state_engine.py — Manages the multi-step flow for Mydoot Customer Care Agent.
"""


class MydootState:
    COLLECTING_NAME             = "collecting_name"
    COLLECTING_COMPLAINT        = "collecting_complaint"
    COLLECTING_BRAND            = "collecting_brand"
    COLLECTING_ITEM             = "collecting_item"
    COLLECTING_PRODUCT_SINCE    = "collecting_product_used_since"
    COLLECTING_USAGE            = "collecting_usage_duration"
    COLLECTING_WARRANTY         = "collecting_warranty"
    COMPLETED                   = "completed"


class ConversationStateEngine:
    def __init__(self):
        self.current_state = MydootState.COLLECTING_COMPLAINT
        self.data = {
            "customer_name":      None,
            "brand":              None,
            "item":               None,
            "product_used_since": None,
            "usage_duration":     None,
            "warranty_status":    None,
            "complaint":          None,
        }

    def update_state(self, transcript, assistant_message=None):
        pass

    def get_prompt_injection(self) -> str:
        collected = [k for k, v in self.data.items() if v]
        missing   = [k for k, v in self.data.items() if not v]
        if missing:
            status = (
                f"COLLECTED: {', '.join(collected) if collected else 'nothing yet'}\n"
                f"STILL NEED: {', '.join(missing)}"
            )
        else:
            status = "ALL 7 FIELDS COLLECTED — call save_customer_feedback NOW."
        return f"\n\nCURRENT STATE: {self.current_state.upper()}\n{status}"

    def set_data(self, key: str, value: str):
        if key not in self.data:
            return
        self.data[key] = value
        if not self.data["complaint"]:
            self.current_state = MydootState.COLLECTING_COMPLAINT
        elif not self.data["customer_name"]:
            self.current_state = MydootState.COLLECTING_NAME
        elif not self.data["brand"]:
            self.current_state = MydootState.COLLECTING_BRAND
        elif not self.data["item"]:
            self.current_state = MydootState.COLLECTING_ITEM
        elif not self.data["product_used_since"]:
            self.current_state = MydootState.COLLECTING_PRODUCT_SINCE
        elif not self.data["usage_duration"]:
            self.current_state = MydootState.COLLECTING_USAGE
        elif not self.data["warranty_status"]:
            self.current_state = MydootState.COLLECTING_WARRANTY
        else:
            self.current_state = MydootState.COMPLETED
