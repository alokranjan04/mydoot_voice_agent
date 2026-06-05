# -*- coding: utf-8 -*-
"""
core/field_validators.py — Strict entity validation for Mydoot voice agent.

Every extracted field passes through a validator before being accepted into the
ServiceGraph.  Validators return a ValidationResult with:
  accepted    — bool: whether the value should be committed
  confidence  — 0.0–1.0 score
  extracted   — cleaned/normalised value (name extraction strips intro phrases)
  reason      — machine-readable rejection/acceptance code
  field       — which field was validated
  raw         — original untouched transcript

Usage:
  from core.field_validators import validate_customer_name, validate_address, validate_preferred_time
  vr = validate_customer_name("mera naam Alok Ranjan hai")
  # ValidationResult(accepted=True, confidence=0.9, extracted="Alok Ranjan", ...)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Optional


# ── Common result type ────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    accepted:    bool
    confidence:  float          # 0.0 – 1.0
    extracted:   str            # cleaned / normalised value
    reason:      str            # machine-readable code
    field:       str = ""       # which field was validated
    raw:         str = ""       # original transcript


# ─────────────────────────────────────────────────────────────────────────────
# 1. CUSTOMER NAME
# ─────────────────────────────────────────────────────────────────────────────

# Hard-reject words — these are never valid names
_NAME_REJECT_WORDS: frozenset = frozenset({
    # Greetings
    "hello", "hi", "hey", "namaste", "namaskar", "vanakkam",
    # Two-word greetings
    "hi there", "hello there", "namaste ji", "hello ji", "hi ji",
    # Affirmatives
    "haan", "ha", "haa", "han", "haan ji", "ji haan", "ji han",
    "ji", "yes", "yeah", "yep", "ok", "okay", "hmm",
    "theek", "theek hai", "theek hain", "sahi", "sahi hai",
    "bilkul", "bilkul sahi", "sure", "correct", "right", "perfect",
    # Negatives
    "nahi", "nahin", "nah", "no", "nope", "galat", "nahi ji",
    # Questions / confusion
    "kya hai", "kya", "kya bola", "kuch nahi", "pata nahi",
    "samajh nahi", "samjha nahi", "kya bol rahe", "kya bolein",
    "sorry", "maafi", "pardon",
    # Hindi pronouns / filler
    "main", "hum", "aap", "woh", "yeh", "mera", "meri", "hamara",
    "ek second", "ruko", "wait", "hold on", "ek minute",
    # Devanagari affirmatives / greetings
    "हाँ", "हां", "हाँ जी", "हां जी", "जी हाँ", "जी हां",
    "नहीं", "नही", "जी", "ठीक है", "सही है", "बिल्कुल",
    "नमस्ते", "हेलो",
    # Single chars
    "a", "b", "c", "e", "g", "h", "i", "j", "k", "m", "n",
    "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
})

_DIGIT_RE       = re.compile(r'\d')
_DEVA_RE        = re.compile(r'[\u0900-\u097F]{2,}')   # ≥2 Devanagari chars
_LATIN_ALPHA_RE = re.compile(r'[A-Za-z]{2,}')

# Address-like markers inside a supposed name
_ADDR_IN_NAME_RE = re.compile(
    r'\b(sector|सेक्टर|plot|प्लॉट|flat|फ्लैट|floor|building|block|'
    r'gali|गली|nagar|नगर|marg|road|street|phase|ward|area|locality|'
    r'society|apartment|में\s+है|रहता\s+है|रहती\s+है|address|पता)\b|\d{3,}',
    re.IGNORECASE,
)

# Intro phrases to strip before checking the name itself
_NAME_INTRO_RE = re.compile(
    r'^(?:'
    r'(?:मेरा|mera|my)\s+(?:नाम|naam|name)\s+(?:है\s+|hai\s+|is\s+)?'
    r'|(?:नाम|naam|name)\s+(?:है\s+|hai\s+)?'
    r'|(?:मैं|main|i\s+am|i\'m)\s+'
    r'|(?:aap\s+)?(?:mujhe|mujhe|hum)\s+(?:bula|pukar|bol)\s*(?:te|sakte)?\s+'
    r')',
    re.IGNORECASE,
)
_NAME_SUFFIX_RE = re.compile(
    r'\s+(?:है|hai|he|hain|is|hoon|hun|houn|bolte|kahte|se\s+hain?|se\s+hai)$',
    re.IGNORECASE,
)


def _strip_name_intro(text: str) -> str:
    """Remove 'mera naam X hai' wrappers, returning just X."""
    t = text.strip().rstrip("।.!? ")
    t = _NAME_INTRO_RE.sub("", t).strip()
    t = _NAME_SUFFIX_RE.sub("", t).strip()
    return t.rstrip("।.!? ")


def validate_customer_name(text: str) -> ValidationResult:
    """
    Validate and extract a customer name from an STT transcript.

    Rejects: greetings, affirmatives, questions, address-like strings, digits.
    Requires: person-like alphabetic name in Latin or Devanagari.

    Returns ValidationResult with extracted = stripped name (e.g. "Alok Ranjan")
    even when the raw transcript was "mera naam Alok Ranjan hai".
    """
    raw   = text.strip()
    clean = raw.strip("।.!? ").lower()

    # ── Hard word list ────────────────────────────────────────────────────────
    if clean in _NAME_REJECT_WORDS:
        return ValidationResult(False, 0.0, raw, "rejection_list", "customer_name", raw)

    # ── Too long (>6 words) ───────────────────────────────────────────────────
    if len(clean.split()) > 7:
        return ValidationResult(False, 0.0, raw, "too_many_words", "customer_name", raw)

    # ── Contains digits ───────────────────────────────────────────────────────
    if _DIGIT_RE.search(raw):
        return ValidationResult(False, 0.0, raw, "contains_digits", "customer_name", raw)

    # ── Address-like content ──────────────────────────────────────────────────
    if _ADDR_IN_NAME_RE.search(raw):
        return ValidationResult(False, 0.0, raw, "looks_like_address", "customer_name", raw)

    # ── Strip intro phrases ───────────────────────────────────────────────────
    extracted = _strip_name_intro(raw)
    extracted_clean = extracted.strip("।.!? ").lower()

    if not extracted or extracted_clean in _NAME_REJECT_WORDS:
        return ValidationResult(False, 0.0, raw, "empty_after_extraction", "customer_name", raw)

    extracted_words = extracted.strip().split()
    if not extracted_words:
        return ValidationResult(False, 0.0, raw, "empty_words", "customer_name", raw)

    # ── Require alphabetic content ────────────────────────────────────────────
    has_deva  = bool(_DEVA_RE.search(extracted))
    has_latin = bool(_LATIN_ALPHA_RE.search(extracted))
    if not has_deva and not has_latin:
        return ValidationResult(False, 0.0, raw, "no_alphabetic_content", "customer_name", raw)

    # ── Confidence scoring ────────────────────────────────────────────────────
    confidence = 0.5
    if len(extracted_words) >= 2:
        confidence += 0.30   # full name raises confidence
    if has_deva or has_latin:
        confidence += 0.15
    if len(extracted_words) == 1 and extracted_clean in _NAME_REJECT_WORDS:
        return ValidationResult(False, 0.0, raw, "single_word_rejection", "customer_name", raw)

    return ValidationResult(
        True, min(confidence, 1.0), extracted.strip(), "accepted", "customer_name", raw
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. ADDRESS
# ─────────────────────────────────────────────────────────────────────────────

# Location keywords that indicate real address content
_ADDR_KEYWORD_RE = re.compile(
    r'\b(sector|सेक्टर|block|ब्लॉक|plot|प्लॉट|flat|फ्लैट|'
    r'society|सोसाइटी|colony|कॉलोनी|nagar|नगर|vihar|विहार|'
    r'garden|enclave|park|puram|'
    r'locality|area|गली|lane|road|marg|'
    r'noida|gurgaon|gurugram|delhi|ncr|faridabad|ghaziabad|'
    r'apartment|tower|residency|heights|phase|'
    r'house|ghar|building|floor|'
    r'mohalla|bazaar|chowk|gate|'
    r'sector\s*\d|block\s*[a-z])\b',
    re.IGNORECASE,
)
_NUM_IN_ADDR_RE = re.compile(r'\b\d+\b')

_ADDR_AFFIRM_ONLY: frozenset = frozenset({
    "haan", "ha", "haa", "ji", "yes", "ok", "okay",
    "theek", "theek hai", "sahi", "sahi hai", "bilkul", "correct",
})


def validate_address(text: str) -> ValidationResult:
    """
    Validate an address string.

    Accepts:
      - At least one location keyword (sector, society, nagar, noida, etc.)
      - OR digit (house/flat/plot number) + ≥3 words

    Rejects:
      - Single words
      - Pure affirmatives ("haan", "ok")
      - No location markers at all
    """
    raw   = text.strip()
    clean = raw.strip("।.!? ").lower()
    words = raw.strip().split()

    if len(words) < 2:
        return ValidationResult(False, 0.0, raw, "too_short_min_2_words", "address", raw)

    if clean in _ADDR_AFFIRM_ONLY:
        return ValidationResult(False, 0.0, raw, "is_affirmative", "address", raw)

    has_keyword = bool(_ADDR_KEYWORD_RE.search(clean))
    has_number  = bool(_NUM_IN_ADDR_RE.search(raw))

    if has_keyword:
        conf = 0.60
        if len(words) >= 4:
            conf += 0.15
        if has_number:
            conf += 0.10
        return ValidationResult(True, min(conf, 1.0), raw, "accepted", "address", raw)

    if has_number and len(words) >= 3:
        return ValidationResult(True, 0.55, raw, "accepted_number_multiword", "address", raw)

    # Has multiple words but no clear location marker
    if len(words) >= 4:
        return ValidationResult(
            False, 0.30, raw, "missing_location_keyword", "address", raw
        )

    return ValidationResult(False, 0.20, raw, "missing_location_keyword", "address", raw)


# ─────────────────────────────────────────────────────────────────────────────
# 3. PREFERRED TIME
# ─────────────────────────────────────────────────────────────────────────────

# (pattern_regex, confidence_if_matched)
_TIME_PATTERN_RULES: list[tuple[str, float]] = [
    # Numeric time: "10 baje", "5:30 PM", "10 AM"
    (r'\b\d{1,2}(?::\d{2})?\s*(?:baje|am|pm|AM|PM)\b',          0.95),
    # Explicit day + time: "kal subah", "aaj shaam"
    (r'\b(kal|aaj|parso|kal\s+ko|is\s+hafte)\b.{0,20}'
     r'\b(subah|shaam|dopahar|raat|morning|evening|afternoon|night)\b', 0.90),
    # Just day words
    (r'\b(kal|aaj|parso|kal\s+ko)\b',                            0.75),
    # Time-of-day words
    (r'\b(subah|shaam|dopahar|raat|morning|afternoon|evening|night)\b', 0.70),
    # Weekday names
    (r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
     r'somvar|mangalvar|budhvar|guruvar|shukravar|shanivar|ravivar)\b', 0.75),
    # Relative time: "next week", "agle hafte", "is weekend"
    (r'\b(next\s+(?:week|day|monday|morning)|agle\s+hafte|is\s+hafte|'
     r'weekend|is\s+weekend|agle\s+hafte)\b',                     0.70),
    # Anytime / flexible
    (r'\b(anytime|koi\s+bhi\s+(?:time|samay|din)|convenient|'
     r'flexible|jo\s+bhi|jo\s+time|suvidha\s+anusaar)\b',         0.65),
    # Urgency
    (r'\b(jaldi|asap|urgent|turant|abhi|immediately|'
     r'jitna\s+jaldi|as\s+soon)\b',                               0.70),
    # Month/week references
    (r'\b(mahine|month|hafte|week|saptaah)\b',                    0.60),
]

_TIME_REJECT_SET: frozenset = frozenset({
    "haan", "ha", "haa", "ji", "yes", "ok", "okay",
    "theek", "sahi", "bilkul", "nahi", "no",
    "kya", "kya hai", "pata nahi", "samajh nahi",
    "hello", "hi", "namaste",
})


def validate_preferred_time(text: str) -> ValidationResult:
    """
    Validate a preferred time slot.

    Accepts: date/time expressions (kal, subah 10 baje, Monday, anytime, etc.)
    Rejects: affirmatives, questions, random words with no time reference.
    """
    raw   = text.strip()
    clean = raw.strip("।.!? ").lower()
    words = clean.split()

    if not words:
        return ValidationResult(False, 0.0, raw, "empty", "preferred_time", raw)

    if clean in _TIME_REJECT_SET:
        return ValidationResult(False, 0.0, raw, "rejection_list", "preferred_time", raw)

    best_conf   = 0.0
    best_reason = "no_time_pattern"

    for pattern, conf in _TIME_PATTERN_RULES:
        if re.search(pattern, clean, re.IGNORECASE):
            if conf > best_conf:
                best_conf   = conf
                best_reason = "pattern_match"

    if best_conf >= 0.60:
        return ValidationResult(True, best_conf, raw, best_reason, "preferred_time", raw)

    # Last resort: ≥2 words containing a digit → could be a date/time
    if len(words) >= 2 and re.search(r'\d', raw):
        return ValidationResult(True, 0.50, raw, "digit_multiword", "preferred_time", raw)

    return ValidationResult(False, best_conf, raw, best_reason, "preferred_time", raw)
