# -*- coding: utf-8 -*-
"""
core/extraction_quality.py — Per-call extraction quality tracker.

Accumulates field-level quality data in memory during a call and flushes
to the field_quality_log table once at call-end.  Zero DB writes on the
hot path; zero risk to call behaviour if Postgres is unavailable.

Usage (inside gemini_handler):
    tracker = ExtractionQualityTracker(caller_id)

    # When Gemini fires save_service_request successfully:
    tracker.record_gemini_fields(args)

    # Inside _handle_local_stage_response:
    tracker.record_local_candidate("address", value, confidence)
    tracker.record_local_correction("address", merged_value, None)
    tracker.record_local_confirmed("address", confirmed_value)

    # At call-end (in finally block):
    tracker.mark_call_saved(save_executed)
    records = tracker.flush()            # list[dict] ready for DB insert
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Fields handled by Gemini extraction (no local validator confidence)
_GEMINI_FIELDS = frozenset({"category", "subcategory", "issue_type", "brand", "model"})

# Fields handled by local validators (have confidence scores, correction history)
_LOCAL_FIELDS = frozenset({"address", "preferred_time", "customer_name"})


@dataclass
class FieldRecord:
    """Accumulates quality data for one field within one call."""
    field:          str
    source:         str              # "gemini" | "local"
    first_value:    str = ""
    final_value:    str = ""
    num_attempts:   int = 0          # incremented each time a candidate is set
    num_corrections: int = 0         # attempts beyond the first (= num_attempts - 1 after confirm)
    confidence:     Optional[float] = None  # last validator confidence; None for Gemini fields
    call_saved:     bool = False

    @property
    def was_corrected(self) -> bool:
        return self.num_corrections > 0

    def to_dict(self, caller_id: str) -> dict:
        return {
            "caller_id":       caller_id,
            "field":           self.field,
            "source":          self.source,
            "first_value":     self.first_value,
            "final_value":     self.final_value,
            "num_attempts":    self.num_attempts,
            "num_corrections": self.num_corrections,
            "confidence":      self.confidence,
            "call_saved":      self.call_saved,
        }


class ExtractionQualityTracker:
    """
    In-memory extraction quality accumulator for one call lifetime.

    All methods silently catch exceptions so they can never disrupt call flow.
    """

    def __init__(self, caller_id: str) -> None:
        self._caller_id = caller_id
        self._records: dict[str, FieldRecord] = {}

    # ── Recording ──────────────────────────────────────────────────────────────

    def record_gemini_fields(self, args: dict) -> None:
        """
        Record category/subcategory/issue_type/brand/model from a successful
        save_service_request tool call.  Called once per call at save time.
        """
        try:
            for f in _GEMINI_FIELDS:
                val = (args.get(f) or "").strip()
                if not val:
                    continue
                if f not in self._records:
                    self._records[f] = FieldRecord(
                        field       = f,
                        source      = "gemini",
                        first_value = val,
                        final_value = val,
                        num_attempts = 1,
                    )
                else:
                    # Should not normally happen (save fires once), but handle
                    self._records[f].final_value  = val
        except Exception:
            pass

    def record_local_candidate(
        self,
        fld: str,
        value: str,
        confidence: Optional[float],
    ) -> None:
        """
        Called each time _validate_field accepts a value and it enters set_pending.
        First call sets first_value; subsequent calls are corrections.
        """
        try:
            if not value:
                return
            if fld not in self._records:
                self._records[fld] = FieldRecord(
                    field        = fld,
                    source       = "local",
                    first_value  = value,
                    final_value  = value,
                    num_attempts = 1,
                    confidence   = confidence,
                )
            else:
                rec = self._records[fld]
                rec.num_attempts  += 1
                rec.num_corrections += 1
                rec.final_value   = value
                if confidence is not None:
                    rec.confidence = confidence
        except Exception:
            pass

    def record_local_correction(
        self,
        fld: str,
        new_value: str,
        confidence: Optional[float],
    ) -> None:
        """
        Called when a correction replaces a pending value
        (the address-merge path or the full-replacement path in correction mode).
        """
        try:
            if not new_value:
                return
            if fld not in self._records:
                self._records[fld] = FieldRecord(
                    field           = fld,
                    source          = "local",
                    first_value     = "",
                    final_value     = new_value,
                    num_attempts    = 1,
                    num_corrections = 1,
                    confidence      = confidence,
                )
            else:
                rec = self._records[fld]
                rec.num_attempts    += 1
                rec.num_corrections += 1
                rec.final_value     = new_value
                if confidence is not None:
                    rec.confidence = confidence
        except Exception:
            pass

    def record_local_confirmed(self, fld: str, confirmed_value: str) -> None:
        """
        Called when confirm_pending() succeeds for a local field.
        Locks final_value; does not increment correction counter.
        """
        try:
            if fld in self._records:
                self._records[fld].final_value = confirmed_value
        except Exception:
            pass

    def mark_call_saved(self, saved: bool) -> None:
        """Stamp call_saved on all records before flush."""
        try:
            for rec in self._records.values():
                rec.call_saved = saved
        except Exception:
            pass

    # ── Flush ──────────────────────────────────────────────────────────────────

    def flush(self) -> list[dict]:
        """
        Return list of dicts ready for INSERT into field_quality_log.
        Only includes fields that have at least one attempt recorded.
        """
        try:
            return [
                r.to_dict(self._caller_id)
                for r in self._records.values()
                if r.num_attempts > 0
            ]
        except Exception:
            return []

    def snapshot(self) -> list[dict]:
        """Return current state for logging (does not clear records)."""
        return self.flush()
