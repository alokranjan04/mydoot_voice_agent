# -*- coding: utf-8 -*-
"""
state_engine.py — Manages the multi-step flow for Mydoot Customer Care Agent.
"""


class MydootState:
    COLLECTING_NAME     = "collecting_name"
    COLLECTING_COMPANY  = "collecting_company"
    COLLECTING_PRODUCT  = "collecting_product"
    COLLECTING_USAGE    = "collecting_usage"
    COLLECTING_WARRANTY = "collecting_warranty"
    COLLECTING_COMPLAINT= "collecting_complaint"
    COMPLETED           = "completed"


class ConversationStateEngine:
    def __init__(self):
        self.current_state = MydootState.COLLECTING_NAME
        self.data = {
            "customer_name":   None,
            "company_name":    None,
            "product_name":    None,
            "usage_duration":  None,
            "warranty_status": None,
            "complaint":       None,
        }

    def update_state(self, transcript, assistant_message=None):
        """LLM drives the conversation; state auto-advances via set_data."""
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
            status = "ALL DATA COLLECTED — call save_customer_feedback NOW immediately."
        return f"\n\nCURRENT STATE: {self.current_state.upper()}\n{status}"

    def set_data(self, key: str, value: str):
        if key not in self.data:
            return
        self.data[key] = value
        if not self.data["customer_name"]:
            self.current_state = MydootState.COLLECTING_NAME
        elif not self.data["company_name"]:
            self.current_state = MydootState.COLLECTING_COMPANY
        elif not self.data["product_name"]:
            self.current_state = MydootState.COLLECTING_PRODUCT
        elif not self.data["usage_duration"]:
            self.current_state = MydootState.COLLECTING_USAGE
        elif not self.data["warranty_status"]:
            self.current_state = MydootState.COLLECTING_WARRANTY
        elif not self.data["complaint"]:
            self.current_state = MydootState.COLLECTING_COMPLAINT
        else:
            self.current_state = MydootState.COMPLETED
