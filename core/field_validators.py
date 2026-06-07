# -*- coding: utf-8 -*-
"""
core/field_validators.py — Strict entity validation for the Mydoot voice pipeline.

Every extracted field passes through a validator before being accepted into the
ServiceGraph state. Validators return a :class:`ValidationResult` with:

  accepted    — bool: whether the value should be committed
  confidence  — 0.0–1.0 score
  extracted   — cleaned / normalised value (intro phrases stripped for names)
  reason      — machine-readable acceptance / rejection code
  field       — which field was validated
  raw         — original untouched STT transcript

Sections
--------
1. CUSTOMER NAME  — validate_customer_name()
2. ADDRESS        — validate_address(), parse_address_fields(),
                    merge_address_correction()
3. PREFERRED TIME — validate_preferred_time()
4. ADDRESS ENTITY — AddressFields dataclass + all extraction helpers

All regexes are compiled at **module load** time (never inside loops or
repeated function calls) so there is no per-call compilation overhead.

Usage::

    from core.field_validators import (
        validate_customer_name, validate_address, validate_preferred_time,
        AddressFields, parse_address_fields, format_address_fields,
        is_address_correction, merge_address_correction,
    )
    vr = validate_customer_name("mera naam Alok Ranjan hai")
    # ValidationResult(accepted=True, confidence=0.95, extracted="Alok Ranjan", ...)
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
    # Devanagari filler / interjections (never valid names)
    "हम्म", "उम्म", "उम", "आहा", "अच्छा", "वाह", "अरे",
    "हाँ सही है", "हां सही है", "हाँ सही", "हां सही",
    "ये है", "यह है", "ये सही है", "यह सही है",
    # Single chars
    "a", "b", "c", "e", "g", "h", "i", "j", "k", "m", "n",
    "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
})

_DIGIT_RE       = re.compile(r'\d')
_DEVA_RE        = re.compile(r'[\u0900-\u097F]{2,}')   # ≥2 Devanagari chars
_LATIN_ALPHA_RE = re.compile(r'[A-Za-z]{2,}')

# Address-like markers inside a supposed name
_ADDR_IN_NAME_RE = re.compile(
    r'(?:\b(?:sector|plot|flat|floor|building|block|'
    r'gali|nagar|marg|road|street|phase|ward|area|locality|'
    r'society|apartment|address)\b'
    r'|सेक्टर|प्लॉट|फ्लैट|गली|नगर|में\s+है|रहता\s+है|रहती\s+है|पता'
    r'|\d{3,})',
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
    # At this point has_deva or has_latin is always True (returned earlier if
    # False), so the alphabetic bonus always applies.
    # Multi-word names: 0.50 base + 0.30 (multi-word) + 0.15 (alpha) = 0.95
    # Single-word names: 0.50 base + 0.15 (alpha) = 0.65
    confidence = 0.5
    if len(extracted_words) >= 2:
        confidence += 0.30   # full name raises confidence
    confidence += 0.15       # alphabetic content (always True here)

    return ValidationResult(
        True, min(confidence, 1.0), extracted.strip(), "accepted", "customer_name", raw
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. ADDRESS
# ─────────────────────────────────────────────────────────────────────────────

# Location keywords that indicate real address content
_ADDR_KEYWORD_RE = re.compile(
    r'(?:\b(?:sector|block|plot|flat|'
    r'society|colony|nagar|vihar|'
    r'garden|enclave|park|puram|'
    r'locality|area|lane|road|marg|'
    r'noida|gurgaon|gurugram|delhi|ncr|faridabad|ghaziabad|'
    r'apartment|tower|residency|heights|phase|'
    r'house|ghar|building|floor|'
    r'mohalla|bazaar|chowk|gate|'
    r'sector\s*\d|block\s*[a-z])\b'
    r'|सेक्टर|ब्लॉक|प्लॉट|फ्लैट|सोसाइटी|कॉलोनी|नगर|विहार|गली)',
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

# (compiled_pattern, confidence_if_matched) — compiled at module load, never inside loops
# \b doesn't work reliably with Devanagari (vowel signs like ी are non-\w
# marks, so \b fires mid-word).  For Devanagari alternatives we use
# (?:(?:^|\s)) / (?:(?:\s|$)) instead; for Latin-only rules \b is fine.
_TIME_PATTERN_RULES: list[tuple[re.Pattern, float]] = [
    # Numeric time: "10 baje", "5:30 PM", "10 AM"
    (re.compile(r'\b\d{1,2}(?::\d{2})?\s*(?:baje|am|pm)\b', re.IGNORECASE),                0.95),
    # Numeric range: "4 se 7", "10 se 12"
    (re.compile(r'\b\d{1,2}\s*(?:se|से)\s*\d{1,2}\b', re.IGNORECASE),                      0.90),
    # Explicit day + time: "kal subah", "aaj shaam", "कल सुबह", "आज शाम"
    (re.compile(r'(?:(?:^|\s|\b)(?:kal|aaj|parso|kal\s+ko|is\s+hafte)|'
                r'(?:कल|आज|परसों|अगले))[\w\s,।]{0,20}'
                r'(?:\b(?:subah|shaam|dopahar|raat|morning|evening|afternoon|night)\b|'
                r'(?:सुबह|शाम|दोपहर|रात))', re.IGNORECASE),                                0.90),
    # Just day words (Romanized + Devanagari)
    (re.compile(r'(?:\bkal\b|\baaj\b|\bparso\b|\bkal\s+ko\b|कल|आज|परसों)',
                re.IGNORECASE),                                                              0.75),
    # Time-of-day words (Romanized + Devanagari)
    (re.compile(r'(?:\bsubah\b|\bshaam\b|\bdopahar\b|\braat\b|'
                r'\bmorning\b|\bafternoon\b|\bevening\b|\bnight\b|'
                r'सुबह|शाम|दोपहर|रात)', re.IGNORECASE),                                    0.70),
    # Weekday names
    (re.compile(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
                r'somvar|mangalvar|budhvar|guruvar|shukravar|shanivar|ravivar)\b',
                re.IGNORECASE),                                                              0.75),
    # Relative time: "next week", "agle hafte", "अगले हफ्ते", "अगले एक घंटे"
    (re.compile(r'(?:\bnext\s+(?:week|day|monday|morning)\b|'
                r'\bagle\s+(?:hafte|ghante|din)\b|\bis\s+hafte\b|\bweekend\b|'
                r'अगले\s*(?:हफ्ते|घंटे|दिन|एक))', re.IGNORECASE),                         0.70),
    # Hour/minute references: "ek ghante", "do ghante", "एक घंटे", "30 minute"
    (re.compile(r'(?:(?:\b(?:\d{1,2}|ek|do|teen)\b|(?:एक|दो|तीन))\s*'
                r'(?:\b(?:ghante|ghanta|minute|mint|hour)\b|(?:घंटे|घंटा|मिनट)))',
                re.IGNORECASE),                                                              0.75),
    # Anytime / flexible
    (re.compile(r'(?:\banytime\b|\bkoi\s+bhi\s+(?:time|samay|din)\b|\bconvenient\b|'
                r'\bflexible\b|\bjo\s+bhi\b|\bjo\s+time\b|\bsuvidha\s+anusaar\b|'
                r'कोई\s+भी\s*(?:टाइम|समय))', re.IGNORECASE),                               0.65),
    # Urgency (Romanized + Devanagari)
    (re.compile(r'(?:\bjaldi\b|\basap\b|\burgent\b|\bturant\b|\babhi\b|\bimmediately\b|'
                r'\bjitna\s+jaldi\b|\bas\s+soon\b|'
                r'जल्दी|तुरंत|अभी)', re.IGNORECASE),                                       0.70),
    # Month/week references
    (re.compile(r'(?:\bmahine\b|\bmonth\b|\bhafte\b|\bweek\b|\bsaptaah\b|'
                r'महीने|हफ्ते|सप्ताह)', re.IGNORECASE),                                    0.60),
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
        if pattern.search(clean):
            if conf > best_conf:
                best_conf   = conf
                best_reason = "pattern_match"

    if best_conf >= 0.60:
        return ValidationResult(True, best_conf, raw, best_reason, "preferred_time", raw)

    # Last resort: ≥2 words containing a digit → could be a date/time
    if len(words) >= 2 and re.search(r'\d', raw):
        return ValidationResult(True, 0.50, raw, "digit_multiword", "preferred_time", raw)

    return ValidationResult(False, best_conf, raw, best_reason, "preferred_time", raw)


# ─────────────────────────────────────────────────────────────────────────────
# 4. ADDRESS ENTITY PARSING & CORRECTION MERGE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AddressFields:
    """Structured address entity extracted from free-form text."""
    society:  str = ""   # "Aditya Urban Casa", "Green Valley Society"
    sector:   str = ""   # digit string: "71", "62"
    city:     str = ""   # "Noida", "Gurgaon"
    landmark: str = ""   # "near metro station"


_KNOWN_CITIES: frozenset = frozenset({
    "noida", "gurgaon", "gurugram", "delhi", "new delhi",
    "faridabad", "ghaziabad", "greater noida",
    "नोएडा", "गुड़गांव", "दिल्ली", "गाजियाबाद", "फरीदाबाद",
})

# Sector: digit form — "sector 71", "sec-71", "सेक्टर 71"
_SECTOR_DIGIT_RE = re.compile(
    r'\b(?:sector|sec|सेक्टर)\s*[–\-]?\s*(\d+)\b',
    re.IGNORECASE,
)

# Sector: English word form — "sector seventy one", "sector seventy-one"
_EN_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_EN_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}

# Sector: Devanagari transliteration of English number words.
# Sarvam STT often returns English number words written in Devanagari
# when the speaker says "sector seventy-one" in Hinglish speech.
# e.g. "सेवेंटी एक" (seventy one), "एटी टू" (eighty two)
_DEVA_TENS = {
    "ट्वेंटी": 20, "थर्टी": 30, "फोर्टी": 40, "फिफ्टी": 50,
    "सिक्सटी": 60, "सेवेंटी": 70, "एटी": 80, "नाइनटी": 90,
}
_DEVA_ONES = {
    # English ones written in Devanagari script
    "वन": 1, "टू": 2, "थ्री": 3, "फोर": 4, "फाइव": 5,
    "सिक्स": 6, "सेवन": 7, "एट": 8, "नाइन": 9,
    "टेन": 10, "इलेवन": 11, "ट्वेल्व": 12,
    # Hindi native unit words (used in Hinglish: "सेवेंटी एक" = seventy-one)
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4,
    "पाँच": 5, "पांच": 5, "छह": 6, "सात": 7, "आठ": 8, "नौ": 9,
}

# Build combined lookup for _word_to_sector_int
_ALL_TENS = {**_EN_TENS, **_DEVA_TENS}
_ALL_ONES = {**_EN_ONES, **_DEVA_ONES}

# Latin sector word pattern (e.g. "sector seventy one")
_SECTOR_WORD_RE = re.compile(
    r'(?<![a-zA-Z\u0900-\u097F])(?:sector|sec|सेक्टर)\s+'
    r'((?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)'
    r'(?:\s*[\-–]?\s*(?:one|two|three|four|five|six|seven|eight|nine))?'
    r'|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
    r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)',
    re.IGNORECASE,
)

# Devanagari transliteration pattern (e.g. "सेक्टर सेवेंटी एक")
# Supports both Devanagari-script English ("वन") and Hindi native units ("एक")
_SECTOR_DEVA_WORD_RE = re.compile(
    r'(?<![a-zA-Z\u0900-\u097F])(?:sector|sec|सेक्टर)\s+'
    r'((?:ट्वेंटी|थर्टी|फोर्टी|फिफ्टी|सिक्सटी|सेवेंटी|एटी|नाइनटी)'
    r'(?:\s+(?:वन|टू|थ्री|फोर|फाइव|सिक्स|सेवन|एट|नाइन'
    r'|एक|दो|तीन|चार|पाँच|पांच|छह|सात|आठ|नौ))?'
    r'|वन|टू|थ्री|फोर|फाइव|सिक्स|सेवन|एट|नाइन|टेन|इलेवन|ट्वेल्व'
    r'|एक|दो|तीन|चार|पाँच|पांच|छह|सात|आठ|नौ)',
)

_CITY_CANONICAL: dict = {
    "gurugram": "Gurgaon", "new delhi": "Delhi",
    "greater noida": "Greater Noida",
    "नोएडा": "Noida", "गुड़गांव": "Gurgaon",
    "दिल्ली": "Delhi", "गाजियाबाद": "Ghaziabad",
    "फरीदाबाद": "Faridabad",
}

# Latin filler words — \b is safe for ASCII
_ADDR_FILLER_LATIN_RE = re.compile(
    r'\b(?:mein|hai|ka|ki|ke|aur|main|technician|ko\s+aana|jahan|se|'
    r'tak|ko|par|pe|wala|wali|waale|address|pata|poora|apna|'
    r'bataiye|batao|batayein|rehna|rahna|rehta|rahta|'
    r'nahi|nahin|sahi|galat|yaar|actually|correction)\b',
    re.IGNORECASE,
)
# Devanagari filler words — use Unicode-safe boundaries (\b fails for Devanagari)
_ADDR_FILLER_DEVA_RE = re.compile(
    r'(?<![a-zA-Z\u0900-\u097F])'
    r'(?:है|में|का|की|के|और|पर|से|तक|को|पास|वाला|वाली|नहीं|नही|गलत)'
    r'(?![a-zA-Z\u0900-\u097F])',
)


def _strip_addr_fillers(text: str) -> str:
    """Remove filler words from address text, respecting script boundaries."""
    t = _ADDR_FILLER_LATIN_RE.sub("", text)
    t = _ADDR_FILLER_DEVA_RE.sub("", t)
    return t

_LANDMARK_RE = re.compile(
    r'\b(?:near|opp(?:osite)?|behind|adj(?:acent)?|next\s+to|'
    r'paas\s+(?:mein|wala)|saamne|piche)\b.{3,40}',
    re.IGNORECASE,
)

# Unicode-safe word boundaries: negative lookbehind/lookahead for any letter
# (Latin or Devanagari) so Devanagari words like नहीं are matched correctly.
_CORRECTION_TRIGGER_RE = re.compile(
    r'(?<![a-zA-Z\u0900-\u097F])'
    r'(?:nahi|nahin|no|galat|wrong|actually|correction|change|नहीं|नही|गलत)'
    r'(?![a-zA-Z\u0900-\u097F])',
    re.IGNORECASE,
)


def _word_to_sector_int(s: str) -> Optional[int]:
    """Convert number word(s) — Latin or Devanagari transliteration — to int."""
    s = s.strip().replace("-", " ")
    # Try as-is (Devanagari) then lower-cased (Latin)
    for key in (s, s.lower()):
        parts = key.split()
        if len(parts) == 1:
            v = _ALL_TENS.get(parts[0]) or _ALL_ONES.get(parts[0])
            if v:
                return v
        if len(parts) == 2:
            t = _ALL_TENS.get(parts[0])
            o = _ALL_ONES.get(parts[1])
            if t and o:
                return t + o
    return None


def _extract_sector(text: str) -> str:
    """Return sector number as digit string, or '' if not found."""
    # 1. Digit form: "sector 71", "सेक्टर-71"
    m = _SECTOR_DIGIT_RE.search(text)
    if m:
        return m.group(1)
    # 2. Latin word form: "sector seventy one"
    m2 = _SECTOR_WORD_RE.search(text)
    if m2:
        n = _word_to_sector_int(m2.group(1))
        if n is not None:
            return str(n)
    # 3. Devanagari transliteration: "सेक्टर सेवेंटी एक"
    m3 = _SECTOR_DEVA_WORD_RE.search(text)
    if m3:
        n = _word_to_sector_int(m3.group(1))
        if n is not None:
            return str(n)
    return ""


# Unicode-safe city pattern cache: \b doesn't work for Devanagari so we use
# character-class boundaries that cover both Latin and Devanagari scripts.
_CITY_PATTERNS: dict = {
    city: re.compile(
        r'(?<![a-zA-Z\u0900-\u097F])' + re.escape(city) + r'(?![a-zA-Z\u0900-\u097F])',
        re.IGNORECASE,
    )
    for city in _KNOWN_CITIES
}

# Society / building type keywords — compiled at module level (used in _extract_society)
_SOC_KW_RE = re.compile(
    r'\b(?:society|villa|casa|enclave|heights|towers?|residency|'
    r'apartments?|complex|park|garden|nagar|vihar|puram|colony)\b',
    re.IGNORECASE,
)

# Devanagari words that are never valid society name starters
_DEVA_FILLER_STARTS: frozenset = frozenset({
    "तो", "और", "या", "भी", "ही", "न", "ना", "नहीं", "नही",
    "क्या", "कितना", "कितनी", "कहाँ", "कहां", "कब", "कैसे", "कैसा",
    "कौन", "क्यों", "किसका", "किसकी", "किसके",
    "मेरा", "मेरी", "मेरे", "हमारा", "हमारी", "आपका", "आपकी",
    "यह", "ये", "वह", "वो", "इस", "उस", "उनका",
    "हाँ", "हां", "नहीं", "ठीक", "बहुत", "थोड़ा", "अभी",
})


def _extract_city(text: str) -> str:
    """Return canonical city name, or '' if not found."""
    # Check longer names first to avoid "noida" matching inside "greater noida"
    for city in sorted(_KNOWN_CITIES, key=len, reverse=True):
        if _CITY_PATTERNS[city].search(text):
            return _CITY_CANONICAL.get(city, city.title())
    return ""


def _extract_society(text: str, sector: str, city: str) -> str:
    """Extract society/building name: meaningful text before sector/city."""
    t = text.strip()
    if city:
        pat = _CITY_PATTERNS.get(city)
        if pat:
            t = pat.sub("", t)
        else:
            t = re.sub(re.escape(city), "", t, flags=re.IGNORECASE)
    if sector:
        t = re.sub(
            r'(?<![a-zA-Z\u0900-\u097F])(?:sector|sec|सेक्टर)\s*[–\-]?\s*'
            + re.escape(sector) + r'(?![a-zA-Z\u0900-\u097F\d])',
            "", t, flags=re.IGNORECASE,
        )
    # Strip sector word-form too (both Latin and Devanagari)
    t = _SECTOR_WORD_RE.sub("", t)
    t = _SECTOR_DEVA_WORD_RE.sub("", t)
    t = _strip_addr_fillers(t).strip(" ,।")
    # Take first comma-separated part with ≥ 2 words that starts like a proper noun
    # (first char uppercase, or Devanagari, or contains a society/building keyword)
    for part in re.split(r'[,،]', t):
        part = part.strip(" ,।")
        words = part.split()
        if len(words) < 2:
            continue
        first_char = words[0][0] if words[0] else ""
        is_deva = "\u0900" <= first_char <= "\u097F"
        # Reject Devanagari phrases starting with question/filler/pronoun words
        if is_deva and words[0] in _DEVA_FILLER_STARTS:
            continue
        if words[0][:1].isupper() or is_deva or _SOC_KW_RE.search(part):
            return part
    return ""


def parse_address_fields(text: str) -> AddressFields:
    """Parse free-form address into structured fields. Best-effort."""
    sector   = _extract_sector(text)
    city     = _extract_city(text)
    landmark = ""
    m = _LANDMARK_RE.search(text)
    if m:
        landmark = m.group(0).strip()
    society  = _extract_society(text, sector, city)
    return AddressFields(society=society, sector=sector, city=city, landmark=landmark)


def format_address_fields(f: AddressFields) -> str:
    """Render AddressFields to a human-readable address string."""
    parts = []
    if f.society:  parts.append(f.society)
    if f.sector:   parts.append(f"Sector {f.sector}")
    if f.city:     parts.append(f.city)
    if f.landmark: parts.append(f.landmark)
    return ", ".join(parts)


def is_address_correction(text: str) -> bool:
    """True if text contains a correction trigger word (nahi, galat, actually…)."""
    return bool(_CORRECTION_TRIGGER_RE.search(text))


def merge_address_correction(
    original_text: str,
    correction_text: str,
) -> Optional[str]:
    """
    Field-level merge of `correction_text` into `original_text`.

    - Parse both texts into AddressFields.
    - Replace fields that the correction explicitly provides.
    - Preserve all other original fields.
    - Return merged address string, or None if no specific fields were updated
      (caller should treat correction as a full address replacement).
    """
    orig = parse_address_fields(original_text)
    corr = parse_address_fields(correction_text)

    merged  = AddressFields(
        society  = orig.society,
        sector   = orig.sector,
        city     = orig.city,
        landmark = orig.landmark,
    )
    updated = False

    if corr.sector and corr.sector != orig.sector:
        merged.sector  = corr.sector
        updated = True
    if corr.city and corr.city.lower() != orig.city.lower():
        merged.city    = corr.city
        updated = True
    if corr.society and corr.society.lower() != orig.society.lower():
        merged.society = corr.society
        updated = True
    if corr.landmark and corr.landmark != orig.landmark:
        merged.landmark = corr.landmark
        updated = True

    if not updated:
        return None
    result = format_address_fields(merged)
    return result or None
