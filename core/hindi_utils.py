# -*- coding: utf-8 -*-
"""
Hindi/Hinglish text utilities.

- time_to_hindi()         '06:10 PM'  → 'शाम के छह बजकर दस मिनट'
- day_to_hindi()          'Tuesday'   → 'मंगलवार'
- CONFIRMATION_WORDS      frozenset of words that count as slot confirmation
- JUNK_RE                 strips leaked tool-call tokens from TTS text
- SENT_RE                 sentence boundary splitter for chunked TTS
"""
import re
from datetime import datetime

# ── Regex constants ───────────────────────────────────────────────────────────

JUNK_RE = re.compile(
    r"\b(book_appointment|check_available_slots|arg_key|arg_value|tool_call|tool_response"
    r"|patient_name|preferred_day|preferred_time|patient_age|parent_name|contact_number"
    r"|reason|patient name|preferred day|preferred time|patient age|parent name|contact number"
    r"|बेचारा|bechara)\b",
    re.IGNORECASE,
)
SENT_RE = re.compile(r"(?<=[।.!?])\s*")

# ── Confirmation vocabulary ───────────────────────────────────────────────────

CONFIRMATION_WORDS = frozenset({
    "हाँ", "हां", "हाँ जी", "जी हाँ", "जी हां",
    "haan", "han", "yes", "yeah",
    "ठीक", "theek", "ठीक है", "ठीक रहेगा", "ठीक बात", "ठीक बात है",
    "okay", "ok", "bilkul", "sure", "बिल्कुल",
    "चलेगा", "चलेगा जी", "हो जाए", "कर दो", "बुक कर दो",
    "मंज़ूर", "मंजूर", "बढ़िया", "अच्छा",
})

# ── Internal lookup tables ────────────────────────────────────────────────────

_HI_HOUR = {
    1: "एक",  2: "दो",    3: "तीन",    4: "चार",   5: "पाँच",  6: "छह",
    7: "सात", 8: "आठ",    9: "नौ",     10: "दस",  11: "ग्यारह", 12: "बारह",
}
_HI_MIN = {
    5: "पाँच",  10: "दस",    15: "पंद्रह",  20: "बीस",     25: "पच्चीस",
    30: "तीस", 35: "पैंतीस", 40: "चालीस",  45: "पैंतालीस", 50: "पचास", 55: "पचपन",
}
_HI_DAY = {
    "Monday":   "सोमवार",  "Tuesday":  "मंगलवार", "Wednesday": "बुधवार",
    "Thursday": "गुरुवार", "Friday":   "शुक्रवार", "Saturday":  "शनिवार",
    "Sunday":   "रविवार",  "Today":    "आज",       "Tomorrow":  "कल",
}

# Inverse lookups used by hindi_to_time()
_HI_HOUR_INV: dict[str, int] = {v: k for k, v in _HI_HOUR.items()}
_HI_HOUR_INV.update({"पांच": 5})   # alternate spelling
_HI_MIN_INV:  dict[str, int] = {v: k for k, v in _HI_MIN.items()}

# ── Public API ────────────────────────────────────────────────────────────────

def day_to_hindi(day_str: str) -> str:
    """'Tuesday' → 'मंगलवार'.  Today's weekday → 'आज'. Tomorrow's → 'कल'."""
    from datetime import timedelta
    today    = datetime.now().strftime("%A")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%A")
    if day_str in ("Today", "today", "आज") or day_str == today:
        return "आज"
    if day_str in ("Tomorrow", "tomorrow", "कल") or day_str == tomorrow:
        return "कल"
    return _HI_DAY.get(day_str, day_str)


def time_to_hindi(time_str: str) -> str:
    """'06:10 PM' → 'शाम के छह बजकर दस मिनट'."""
    try:
        dt     = datetime.strptime(time_str.strip(), "%I:%M %p")
        h24, m = dt.hour, dt.minute
        h12    = h24 % 12 or 12
        period = (
            "सुबह"  if h24 < 12 else
            "दोपहर" if h24 < 17 else
            "शाम"   if h24 < 20 else "रात"
        )
        if m == 0:
            return f"{period} के {_HI_HOUR[h12]} बजे"
        if m == 15:
            return f"{period} के सवा {_HI_HOUR[h12]} बजे"
        if m == 30:
            return f"{period} के साढ़े {_HI_HOUR[h12]} बजे"
        if m == 45:
            nxt = h12 % 12 + 1
            return f"{period} के पौने {_HI_HOUR[nxt]} बजे"
        min_hi = _HI_MIN.get(m, str(m))
        return f"{period} के {_HI_HOUR[h12]} बजकर {min_hi} मिनट"
    except Exception:
        return time_str


def hindi_to_time(text: str, default_period: str = "PM") -> str | None:
    """
    Parse a spoken Hindi/Hinglish time expression → 'HH:MM AM/PM'.

    Handles:
      साढ़े छह          → 06:30 PM
      सवा सात           → 07:15 PM
      पौने आठ           → 07:45 PM
      छह बजे            → 06:00 PM
      छह बजकर दस मिनट  → 06:10 PM
      6:30              → 06:30 PM  (numeric fallback)

    Returns None if no time pattern is found.
    """
    if not text:
        return None

    # ── Determine AM/PM from period keywords ────────────────────────────────
    tl = text.lower()
    if any(w in tl for w in ("सुबह", "subah", "morning", "savere")):
        period = "AM"
    elif any(w in tl for w in ("शाम", "sham", "evening", "रात", "night")):
        period = "PM"
    else:
        period = default_period

    def _hour(word: str) -> int | None:
        w = word.strip().rstrip("।.")
        if w.isdigit():
            h = int(w)
            return h if 1 <= h <= 12 else None
        return _HI_HOUR_INV.get(w)

    def _minute(word: str) -> int | None:
        w = word.strip().rstrip("।.")
        if w.isdigit():
            m = int(w)
            return m if 0 <= m < 60 else None
        return _HI_MIN_INV.get(w)

    def _fmt(h: int, m: int) -> str:
        # Clinic-aware AM/PM: if no explicit period keyword was found in text,
        # infer from hour — morning slot 10-12 → AM, evening slot 1-8 → PM.
        p = period
        if p == default_period and not any(
            w in tl for w in ("सुबह", "subah", "morning", "savere",
                              "शाम", "sham", "evening", "रात", "night")
        ):
            if 10 <= h <= 12:
                p = "AM"
            elif 1 <= h <= 8:
                p = "PM"
        return f"{h:02d}:{m:02d} {p}"

    # ── साढ़े X  (X hours 30 min) — MUST check before bare 'बजे' ───────────
    m = re.search(r'साढ़े\s+(\S+)', text)
    if m:
        h = _hour(m.group(1))
        if h:
            return _fmt(h, 30)

    # ── सवा X  (X hours 15 min) ─────────────────────────────────────────────
    m = re.search(r'सवा\s+(\S+)', text)
    if m:
        h = _hour(m.group(1))
        if h:
            return _fmt(h, 15)

    # ── पौने X  (X-1 hours 45 min) ──────────────────────────────────────────
    m = re.search(r'पौने\s+(\S+)', text)
    if m:
        h = _hour(m.group(1))
        if h and h > 1:
            return _fmt(h - 1, 45)

    # ── X बजकर Y मिनट ───────────────────────────────────────────────────────
    m = re.search(r'(\S+)\s+बजकर\s+(\S+)\s+मिनट', text)
    if m:
        h = _hour(m.group(1))
        mn = _minute(m.group(2))
        if h is not None and mn is not None:
            return _fmt(h, mn)

    # ── X बज Y मिनट (alternate phrasing) ───────────────────────────────────
    m = re.search(r'(\S+)\s+बज\s+(\S+)\s+मिनट', text)
    if m:
        h = _hour(m.group(1))
        mn = _minute(m.group(2))
        if h is not None and mn is not None:
            return _fmt(h, mn)

    # ── X बजे  (X:00) ───────────────────────────────────────────────────────
    m = re.search(r'(\S+)\s+बजे', text)
    if m:
        h = _hour(m.group(1))
        if h:
            return _fmt(h, 0)

    # ── Numeric HH:MM fallback ───────────────────────────────────────────────
    m = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 1 <= h <= 12 and 0 <= mn < 60:
            return _fmt(h, mn)

    return None
