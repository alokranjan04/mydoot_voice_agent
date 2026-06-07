# -*- coding: utf-8 -*-
"""
Gemini Live Multimodal pipeline for Mydoot Customer Care.
Uses websockets library + BidiGenerateContent (v1beta) — audio in / audio out.
"""
import asyncio, audioop, base64, io, json, os, re, time, traceback, wave
from datetime import datetime
import aiohttp
import websockets
from aiohttp import web

from config.settings import APP_CONFIG, GEMINI_API_KEY, GEMINI_WS_URL, SARVAM_API_KEY
from core.state_engine import ConversationStateEngine
from core.service_graph import ServiceGraph, ServiceState
from core.field_validators import (
    validate_customer_name, validate_address, validate_preferred_time, ValidationResult,
    AddressFields, parse_address_fields, format_address_fields,
    is_address_correction, merge_address_correction,
)
import core.local_tts as local_tts
from mydoot_functions import (FUNCTION_MAP, send_call_summary_email, upload_recording_to_gcs,
                              save_call_log, save_turn_latency, save_field_quality_log)
from core.latency_tracker import LatencyTracker

# ── Sarvam Saaras v3 STT ─────────────────────────────────────────────────────
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"

# ── VAD (Voice Activity Detection) — local, before STT ───────────────────────
# RMS threshold: packets below this are silence / line noise, not forwarded.
VAD_SPEECH_THRESHOLD  = int(os.getenv("VAD_SPEECH_THRESHOLD",  "100"))
# Seconds of silence after speech that signals end-of-utterance.
# 0.3s keeps latency low; raise to 0.5 via env if callers get cut off.
VAD_END_SECS          = float(os.getenv("VAD_END_SECS",          "0.3"))
# Minimum utterance duration to bother sending to STT (avoids noise blips).
# 0.3s catches short responses like "LG", "haan", "kal" (0.5 would drop these).
VAD_MIN_SPEECH_SECS   = float(os.getenv("VAD_MIN_SPEECH_SECS",  "0.3"))
# Hard ceiling — force-flush utterance if customer speaks this long non-stop.
VAD_MAX_SPEECH_SECS   = float(os.getenv("VAD_MAX_SPEECH_SECS",  "30.0"))

# ── Barge-in detection ────────────────────────────────────────────────────────
# RMS threshold for barge-in — much higher than VAD_SPEECH_THRESHOLD so that
# fan noise and background sounds do NOT trigger it; only nearby human speech.
BARGE_IN_RMS_THRESHOLD = int(os.getenv("BARGE_IN_RMS_THRESHOLD", "350"))
# Seconds of sustained above-threshold RMS required to confirm barge-in.
# Prevents single loud noises (door slam, cough) from cutting off the agent.
BARGE_IN_SUSTAIN_SECS  = float(os.getenv("BARGE_IN_SUSTAIN_SECS", "0.3"))

# ── Call recording ────────────────────────────────────────────────────────────
# Set RECORD_CALLS=1 to save each call's inbound PSTN audio as a WAV file.
# Files are written to RECORDINGS_DIR (default: ./recordings/).
# Use these WAV files with test_asr_compare.py to benchmark ASR services.
RECORD_CALLS               = os.getenv("RECORD_CALLS", "0").lower() in ("1", "true", "yes")
RECORDINGS_DIR             = os.getenv("RECORDINGS_DIR", "recordings")
# Hard time ceiling on audio forwarding after save. The confirmation is ~6s.
# 8s gives enough room for the message to complete and cuts off any Gemini
# repetition (the model occasionally repeats the closing line twice).
MAX_CONFIRMATION_AUDIO_SECS = 7.0
# The byte cap is the primary guard against duplicate audio reaching the
# customer — it fires based on audio CONTENT bytes forwarded to Vobiz,
# which tracks closely with playback at ~1:1 generation speed.
# Confirmation message ≈ 6-7 s; 7 s cap fires right at the end of the first
# message, blocking the duplicate before it starts forwarding.
# End-marker transcript detection below acts as a belt-and-suspenders:
# sends {"event":"clear"} when "shukriya!" text arrives (~200-500 ms after
# the audio, flushing any small amount of duplicate already in Vobiz's buffer).
# Minimum post-save audio that must have played before a turnComplete is
# allowed to close the call. The wait message ("Ek second...") is ~2s.
# Requiring 2.5s ensures the wait-message's own turnComplete is NOT treated
# as the confirmation-done signal, even when the tool call arrives early.
CONFIRMATION_MIN_AUDIO_SECS = 2.5

# ── Stages handled entirely by local code (no Gemini invocation) ──────────────
# For these stages: Sarvam TTS plays the fixed prompt, field_validators validates
# the customer's response, and echo/confirmation are handled in-process.
# Gemini is bypassed completely — zero LLM latency for these turns.
_LOCAL_PROMPT_STAGES: frozenset = frozenset({"address", "preferred_time", "customer_name"})

# Minimum confidence to skip the "X, sahi hai?" echo and auto-confirm.
# High-confidence values are accepted directly and the flow moves to the next
# stage (or triggers save if all fields are collected).  Below this threshold
# the two-step echo → confirm cycle is used.
_AUTO_CONFIRM_CONFIDENCE = float(os.getenv("AUTO_CONFIRM_CONFIDENCE", "0.85"))

# ── STT hint phrases injected per-stage to reduce misrecognition ─────────────
# "brand" stage: common appliance / vehicle brands that Saaras v3 often mishears
# when embedded in Hindi speech ("Hitachi" → "पिताची" / "Zetac", etc.)
_BRAND_HINT_PHRASES: list[str] = [
    "Hitachi", "LG", "Samsung", "Daikin", "Voltas", "Blue Star", "Carrier",
    "Haier", "Godrej", "Panasonic", "Whirlpool", "Bosch", "IFB", "Lloyd",
    "Onida", "Videocon", "Bajaj", "Havells", "Orient", "Usha", "Crompton",
    "Toshiba", "Mitsubishi", "Fujitsu", "Gree", "Midea",
    "Honda", "Yamaha", "Suzuki", "Bajaj", "TVS", "Hero", "Royal Enfield",
    "Maruti", "Hyundai", "Toyota", "Tata", "Mahindra", "Ford", "Kia",
    "हिताची", "एलजी", "सैमसंग", "डाइकिन", "वोल्टास",
]

_ADDRESS_HINT_PHRASES: list[str] = [
    "sector", "society", "colony", "apartment", "flat", "tower", "block",
    "road", "street", "avenue", "lane", "gali", "mohalla", "nagar",
    "Noida", "Gurgaon", "Gurugram", "Delhi", "Ghaziabad", "Faridabad",
    "Greater Noida", "Dwarka", "Rohini", "Vasant Kunj", "Saket",
    "सेक्टर", "सोसाइटी", "कॉलोनी", "नगर", "गली", "मोहल्ला",
]

_TIME_HINT_PHRASES: list[str] = [
    "subah", "shaam", "dopahar", "raat", "morning", "evening",
    "kal", "aaj", "parso", "baje", "ghante",
    "सुबह", "शाम", "दोपहर", "कल", "आज", "बजे", "घंटे",
]

_HINT_PHRASES_BY_STAGE: dict[str, list[str]] = {
    "brand": _BRAND_HINT_PHRASES,
    "address": _ADDRESS_HINT_PHRASES,
    "preferred_time": _TIME_HINT_PHRASES,
}


def _ts():
    """Short HH:MM:SS.mmm timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def _close_after(vobiz_ws, gemini_ws, delay: float, log_fn):
    """Close both WebSockets after `delay` seconds — ends the call gracefully."""
    await asyncio.sleep(delay)
    log_fn(f"📴 Closing call after {delay}s post-confirmation delay")
    log_fn(f"EVT ws_close_gemini delay={delay}s")
    try:
        await gemini_ws.close()
    except Exception:
        pass
    await asyncio.sleep(0.1)  # let g_receiver clean up before dropping Vobiz
    log_fn(f"EVT ws_close_vobiz")
    try:
        if not vobiz_ws.closed:
            await vobiz_ws.close()
    except Exception:
        pass


_RE_MULTI_UNCLEAR = re.compile(r"(\[unclear\]\s*)+")


def _clean_transcript(text: str) -> str:
    """
    Keep only Latin, Devanagari, digits, and common punctuation.
    Replaces runs of unsupported characters (Urdu, Tamil, Japanese, etc.)
    with [unclear] so the transcript stays in English/Hindi/Hinglish only.
    """
    result = []
    run = []
    for ch in text:
        cp = ord(ch)
        allowed = (
            cp <= 0x024F            # Latin + Latin Extended
            or 0x0900 <= cp <= 0x097F  # Devanagari (Hindi)
            or ch in " \t\n.,!?-–—()[]\":;'/"
        )
        if allowed:
            if run:
                result.append("[unclear]")
                run = []
            result.append(ch)
        else:
            run.append(ch)
    if run:
        result.append("[unclear]")
    cleaned = "".join(result).strip()
    # Collapse multiple [unclear] tags
    cleaned = _RE_MULTI_UNCLEAR.sub("[unclear] ", cleaned).strip()
    return cleaned


async def _sarvam_stt(pcm8_bytes: bytes,
                      session: "aiohttp.ClientSession | None" = None,
                      hint_phrases: "list[str] | None" = None) -> str:
    """
    Transcribe 8 kHz 16-bit mono PCM via Sarvam Saaras v3.
    Pass a persistent `session` (created once per call) to avoid
    TCP + TLS handshake overhead on every utterance (~200-300ms saved).
    Optional `hint_phrases` biases recognition toward specific words (e.g. brand names).
    Returns the transcript string, or "" on failure / empty result.
    """
    if not pcm8_bytes or not SARVAM_API_KEY:
        return ""
    try:
        # Normalise amplitude — PSTN calls are often quiet (peak RMS 300–800).
        # Boosting to a target peak near 24000 (75% of 16-bit max) improves
        # Sarvam's recognition of soft speech and short Hindi phonemes.
        peak = audioop.max(pcm8_bytes, 2)
        if 0 < peak < 12000:
            gain = min(24000 / peak, 8.0)   # cap at 8× to avoid noise blowup
            pcm8_bytes = audioop.mul(pcm8_bytes, 2, gain)

        # Upsample 8 kHz → 16 kHz before encoding.
        # Sarvam Saaras is trained on 16 kHz telephony audio; providing a
        # 16 kHz WAV avoids the model's internal re-sampling artefacts and
        # improves recognition of proper nouns and Hindi phonemes on PSTN.
        pcm16k, _ = audioop.ratecv(pcm8_bytes, 2, 1, 8000, 16000, None)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm16k)
        wav_bytes = buf.getvalue()

        form = aiohttp.FormData()
        form.add_field("file", wav_bytes,
                       filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "saaras:v3")
        form.add_field("language_code", "hi-IN")
        if hint_phrases:
            form.add_field("hints", json.dumps(hint_phrases))

        timeout = aiohttp.ClientTimeout(total=10)

        async def _post(sess):
            async with sess.post(
                SARVAM_STT_URL,
                data=form,
                headers={"api-subscription-key": SARVAM_API_KEY},
                timeout=timeout,
            ) as r:
                if r.status == 200:
                    result = await r.json()
                    return result.get("transcript", "").strip()
                else:
                    txt = await r.text()
                    print(f"[STT] Sarvam Saaras error {r.status}: {txt[:200]}")
                    return ""

        if session is not None:
            return await _post(session)
        else:
            async with aiohttp.ClientSession() as s:
                return await _post(s)

    except Exception as e:
        print(f"[STT ERROR] {e}")
    return ""


# ── Quick keyword extraction for stage advancement ────────────────────────────
# Pattern tables: detect category/subcategory from raw STT transcript so we can
# advance the ServiceGraph BEFORE building [STAGE CONTEXT] for Gemini.
# Gemini's NLU handles edge cases; these patterns handle the common case where
# the customer states category + subcategory in a single opening message.

_CAT_PATTERNS: list[tuple[str, list[str]]] = [
    ("Vehicle Service", [
        r"\bvehicle\b", r"व्हीकल", r"\bcar\b", r"कार\b",
        r"\bbike\b", r"बाइक", r"\bscooter\b", r"स्कूटर",
        r"two.?wheel", r"टू.?व्हीलर", r"\bgaadi\b", r"गाड़ी",
    ]),
    ("Appliance Repair", [
        r"\bfridge\b", r"refrigerator", r"फ्रिज",
        r"\bac\b", r"air.?condit", r"एसी",
        r"washing.?machine", r"वाशिंग.?मशीन",
        r"\btv\b", r"\btelevision\b", r"टीवी",
        r"\bgeyser\b", r"गीज़र",
        r"\blaptop\b", r"लैपटॉप",
        r"water.?purifier", r"\bmicrowave\b", r"\binverter\b",
    ]),
    ("Plumbing", [
        r"\bpipe\b", r"पाइप", r"\bleak\b", r"लीक",
        r"\btap\b", r"नल\b", r"\btoilet\b", r"टॉयलेट",
        r"water.?tank", r"\bseelan\b", r"\bseepage\b", r"waterproof",
    ]),
    ("Electrical", [
        r"\belectric", r"बिजली", r"\bwiring\b", r"वायरिंग",
        r"\bmcb\b", r"\bfuse\b", r"short.?circuit",
    ]),
    ("Carpentry", [
        r"\bcarpent", r"\bdoor\b", r"\bfurniture\b",
        r"\bwardrobe\b", r"\bcabinet\b",
    ]),
    ("Cleaning", [
        r"pest.?control", r"deep.?clean", r"home.?clean",
    ]),
]

_SUBCAT_PATTERNS: dict[str, list[tuple[str, list[str]]]] = {
    "Vehicle Service": [
        ("Bike / Scooter Service", [
            r"two.?wheel", r"टू.?व्हीलर",
            r"\bbike\b", r"बाइक",
            r"\bscooter\b", r"स्कूटर",
            r"\bmotorcycle\b",
        ]),
        # Car Wash BEFORE Car Service — longer/more-specific match wins first
        ("Car Wash / Detailing", [
            r"car.?wash", r"कार.?वॉश", r"wash.?car",
            r"gaadi.?saaf", r"गाड़ी.?साफ",
            r"car.?saaf", r"car.?clean",
            r"vehicle.?clean", r"vehicle.?wash",
        ]),
        ("Car Service / Repair", [r"\bcar\b", r"कार\b"]),
    ],
    "Appliance Repair": [
        ("Refrigerator",         [r"\bfridge\b", r"refrigerator", r"फ्रिज"]),
        ("AC / Air Conditioner",  [r"\bac\b", r"air.?condit", r"एसी"]),
        ("Washing Machine",      [r"washing.?machine", r"वाशिंग.?मशीन"]),
        ("TV / Television",      [r"\btv\b", r"\btelevision\b", r"टीवी"]),
        ("Geyser",               [r"\bgeyser\b", r"गीज़र"]),
        ("Laptop / Computer",    [r"\blaptop\b", r"लैपटॉप"]),
        ("Water Purifier",       [r"water.?purifier"]),
    ],
    "Plumbing": [
        ("Tap / Faucet", [r"\btap\b", r"नल\b", r"\bfaucet\b"]),
        ("Toilet / WC",  [r"\btoilet\b", r"टॉयलेट"]),
        ("Pipe Leak",    [r"pipe.?leak", r"पाइप.?लीक"]),
    ],
}

# Stage-order list (mirrors STAGE_ORDER in service_graph.py) used for index
# comparisons when advancing stage from agent speech.
_STAGE_ORDER_LIST = [
    "category", "subcategory", "diagnosis", "brand",
    "address", "preferred_time", "customer_name", "done",
]

# Patterns to detect from Gemini's own speech which field it just asked for.
# When the agent says "apna address batayein", the ServiceGraph stage advances
# to "address" so the NEXT customer turn's [STAGE CONTEXT] is correct and the
# late-stage field advancement code fires on the customer's answer.
_AGENT_STAGE_TRIGGERS: list[tuple[str, list[str]]] = [
    ("address", [
        r"\baddress\b", r"\bpata\b", r"\bsociety\b", r"\blocality\b",
        r"\bkahan\b.{0,30}\bservice\b", r"\bkahan\b.{0,30}\btechnician\b",
        r"address\s*batayein", r"apna\s+pata", r"ghar\s+ka\s+pata",
    ]),
    ("preferred_time", [
        r"\bkab\b.{0,50}\b(aaye|aana|visit|chahte|bulana|time|samay|bhejein)\b",
        r"\bpreferred\s+time\b", r"\bkab\b.{0,30}\btechnician\b",
        r"\bsamay\b.{0,30}batayein", r"\bdate\b.{0,20}\btime\b",
        r"\bwhen\b.{0,30}\b(visit|come|technician|would you like)\b",
    ]),
    ("customer_name", [
        r"\bapna\s+(?:poora\s+)?naam\b", r"\baapka\s+(?:poora\s+)?naam\b",
        r"\byour\s+(?:full\s+)?name\b",
        r"\bpoora\s+naam\b.{0,40}(batayein|chahiye|kya\s+hai|jaan\s+sak)",
        r"\bname\b.{0,20}(please|register|may i have|what is)",
    ]),
]

# Confirmation words — customer agreeing that the agent's echo is correct.
# Used in the pending-confirmation check to decide whether to commit a field.
_CONFIRM_WORDS: frozenset = frozenset({
    "हाँ", "हां", "ha", "haan", "han", "yes", "yeah", "yep",
    "okay", "ok", "sure", "ji", "bilkul", "theek", "sahi", "correct",
    "right", "absolutely", "perfect", "हाँ जी", "जी हाँ", "जी हां",
    "theek hai", "sahi hai", "bilkul sahi", "haan ji", "ji haan",
    "yes correct", "yes that's right", "yes that is correct",
})

def _is_confirmation(text: str) -> bool:
    """
    True if `text` is a short affirmative with no correction/denial content.
    Confirmation: 'haan', 'theek hai', 'sahi hai', 'yes correct' (≤5 words).
    NOT a confirmation: 'nahi', 'galat', 'actually', 'different', corrections.
    """
    t = text.strip().rstrip("।.?! ").lower()
    words = t.split()
    if len(words) > 5:
        return False
    # Strip commas from each word so "हाँ, ..." still registers as affirmative
    clean_words = [w.strip(",") for w in words]
    has_affirm = any(w in _CONFIRM_WORDS for w in clean_words) or t in _CONFIRM_WORDS
    has_denial = any(re.search(p, t) for p in [
        r"\bnahi\b", r"\bnahin\b", r"\bno\b", r"\bnot\b",
        r"galat", r"गलत", r"नहीं", r"correction", r"different",
        r"change", r"wrong", r"actually", r"instead",
    ])
    return has_affirm and not has_denial


# NOTE: _looks_like_name / _extract_name / _NAME_AFFIRMATIONS removed.
# Name validation is now handled by core.field_validators.validate_customer_name.


def _update_stage_from_customer(transcript: str, sg: ServiceGraph) -> None:
    """
    Best-effort keyword extraction from the STT transcript to advance the
    ServiceGraph stage BEFORE building the [STAGE CONTEXT] block for Gemini.
    If the customer mentions both category and subcategory in one message,
    both are recorded so Gemini sees the correct stage for this turn.
    """
    t = transcript.lower()

    if sg.current_stage() == "category":
        for cat, patterns in _CAT_PATTERNS:
            if any(re.search(p, t) for p in patterns):
                sg.on_field_collected("category", cat)
                break  # one category only

    if sg.current_stage() == "subcategory":
        cat = sg.state.get("category", "")
        for subcat, patterns in _SUBCAT_PATTERNS.get(cat, []):
            if any(re.search(p, t) for p in patterns):
                sg.on_field_collected("subcategory", subcat)
                break


async def gemini_handler(request):
    ws = web.WebSocketResponse(protocols=["audio.drachtio.org"])
    await ws.prepare(request)

    caller_id      = request.query.get("caller_id", "Unknown")
    caller_id      = re.sub(r'[^a-zA-Z0-9_+\-]', '', caller_id)
    state_engine   = ConversationStateEngine()
    service_graph  = ServiceGraph()
    lt             = LatencyTracker(caller_id)
    transcript_log = []     # ["Agent: ...", "Customer: ..."]
    call_ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    pcm8_frames    = []     # raw 8kHz PCM16 frames for WAV recording (RECORD_CALLS=1)

    def log(msg):
        print(f"[{_ts()}] caller={caller_id} | {msg}", flush=True)

    def get_system_prompt():
        return (
            f"{APP_CONFIG['agent']['system_prompt']}\n\n"
            f"REAL-TIME: {datetime.now().strftime('%I:%M %p')} on {datetime.now().strftime('%A')}."
        )

    model = APP_CONFIG.get("parameters", {}).get("google", {}).get(
        "model", "models/gemini-2.5-flash-native-audio-latest"
    )
    log(f"🚀 Gemini Live connecting | model={model}")
    log(f"   API key: {'SET len=' + str(len(GEMINI_API_KEY)) if GEMINI_API_KEY else '*** MISSING ***'}")

    g_ws = None    # defined at function level so g_receiver can reassign via nonlocal on reconnect
    setup: dict = {}  # populated after connect; accessible to g_receiver for reconnect

    # Lightweight per-call observability tracker.
    # Mutated in-place (no nonlocal needed) from nested functions.
    _call_track: dict = {
        "stt_latencies_ms": [],   # ms per successful STT call
        "stt_dropped": 0,         # utterances dropped (Gemini busy or too short)
        "barge_ins": 0,           # confirmed barge-in events
        "reconnects": 0,          # Gemini WS reconnect attempts
        "gcs_uri": "",            # recording GCS URI (set at end if RECORD_CALLS=1)
        "local_wav": "",          # local WAV filename (set at end if RECORD_CALLS=1)
    }
    # Per-call extraction quality tracker.  Guarded so any import failure
    # cannot disrupt the call — all tracking calls check `if _eq:` first.
    try:
        from core.extraction_quality import ExtractionQualityTracker as _EQT
        _eq = _EQT(caller_id)
    except Exception:
        _eq = None
    # Initialized here (not inside async with) so the finally block can always
    # reference it, even when Gemini fails during connect (e.g. credits depleted).
    save_executed = False

    # ── Structured event log — proves duplicate confirmation sequence ────────────
    # Each entry: {"t_ms": <ms since call start>, "evt": str, ...}
    # Filter Cloud Run logs with: grep EVT | python -c "import sys,json;[print(json.dumps(json.loads(l.split('EVT ',1)[1]))) for l in sys.stdin if 'EVT ' in l]"
    _evt_t0  = time.perf_counter()
    _evt_log: list[dict] = []

    def evt(name: str, **kw):
        _ms = round((time.perf_counter() - _evt_t0) * 1000)
        _evt_log.append({"t_ms": _ms, "evt": name, **kw})
        log("EVT t={}ms {}{}".format(
            _ms, name,
            " " + " ".join(f"{k}={v}" for k, v in kw.items()) if kw else ""))

    try:
        async with websockets.connect(
            GEMINI_WS_URL,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
        ) as g_ws:

            # ── 1. Setup ────────────────────────────────────────────────────
            setup = {
                "setup": {
                    "model": model,
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                    },
                    "outputAudioTranscription": {},
                    "systemInstruction": {
                        "parts": [{"text": get_system_prompt()}],
                    },
                    "tools": APP_CONFIG["tools"]["gemini"],
                }
            }
            await g_ws.send(json.dumps(setup))
            log("📤 Setup sent — waiting for Gemini confirmation...")

            # ── 2. Wait for setup confirmation ──────────────────────────────
            try:
                raw = await asyncio.wait_for(g_ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise Exception("Gemini setup timed out — check model name and API key")

            resp = json.loads(raw)
            if resp.get("error"):
                raise Exception(f"Gemini setup error: {resp['error']}")
            log(f"✅ Gemini Live Ready: {json.dumps(resp)[:120]}")

            # ── 3. Kick off greeting ─────────────────────────────────────────
            await g_ws.send(json.dumps({
                "clientContent": {
                    "turns": [{
                        "role": "user",
                        "parts": [{"text": "[CALL_STARTED]"}],
                    }],
                    "turnComplete": True,
                }
            }))
            log("📨 [CALL_STARTED] trigger sent — awaiting greeting audio")

            # ── 4. Receive loop: audio + tool calls from Gemini ─────────────
            downsample_state   = None
            last_ai_audio_ts   = 0.0   # timestamp of last audio packet from Gemini
            gemini_turn_end_ts = 0.0   # timestamp when Gemini signalled turnComplete
            greeting_done      = False  # True after first turnComplete (greeting finished)
            greeting_started   = False
            save_done_ts            = 0.0   # timestamp when save_customer_feedback succeeded
            save_executed           = False  # prevent duplicate save calls per session
            confirmation_done       = False  # True once confirmation audio is blocked
            confirmation_audio_secs = 0.0   # seconds of audio forwarded after save_done_ts set
            waiting_for_gemini = False  # True while Gemini is processing; blocks stacked noise utterances
            agent_buf          = ""    # accumulate agent speech chunks per turn
            customer_buf       = ""    # accumulate customer speech chunks per utterance
            # Set by _handle_local_stage_response when validation rejects input AND
            # Sarvam TTS for the retry prompt is unavailable; injected into Gemini's
            # stage context so Gemini knows to re-ask rather than accept invalid input.
            _local_validation_hint = ""

            async def g_receiver():
                nonlocal g_ws
                nonlocal downsample_state, last_ai_audio_ts, gemini_turn_end_ts
                nonlocal greeting_started, greeting_done, save_done_ts, save_executed
                nonlocal confirmation_done, confirmation_audio_secs, waiting_for_gemini
                nonlocal agent_buf, customer_buf
                _g_reconnects  = 0
                _reconnected   = False
                _lt_first_audio = True   # reset each turn to capture first audio packet
                _tc_seq             = 0     # turnComplete sequence number (call-level)
                _post_save_burst    = 0     # counts audio bursts received after save
                _post_save_in_burst = False # True while audio packets are flowing post-save
                try:
                    async for raw_msg in g_ws:
                        data = json.loads(raw_msg)

                        sc = data.get("serverContent", {})

                        # ── Transcript: buffer chunks, flush on turn boundary ─
                        in_t = (data.get("inputAudioTranscription")
                                or data.get("inputTranscription")
                                or sc.get("inputAudioTranscription")
                                or sc.get("inputTranscription")
                                or {})
                        if in_t and in_t.get("text"):
                            customer_buf += in_t["text"]

                        out_t = (data.get("outputAudioTranscription")
                                 or data.get("outputTranscription")
                                 or sc.get("outputAudioTranscription")
                                 or sc.get("outputTranscription")
                                 or {})
                        if out_t and out_t.get("text"):
                            # Agent started speaking — flush pending customer line
                            if customer_buf.strip():
                                line = f"[{_ts()}] Customer: {_clean_transcript(customer_buf.strip())}"
                                transcript_log.append(line)
                                log(f"🗣  {line}")
                                customer_buf = ""
                            agent_buf += out_t["text"]
                            # ── Detect repeated confirmation via end-marker ───────
                            # Look for the confirmation end-marker ("shukriya" /
                            # "thank you for calling") in the agent buffer. Once
                            # found, any text that follows means Gemini has started
                            # repeating — set confirmation_done to block the audio
                            # for that repetition before it reaches Vobiz.
                            # (Customer-name counting was unreliable: the name can
                            # appear earlier in the same turn, firing too early.)
                            if save_done_ts > 0 and not confirmation_done:
                                _buf_l = agent_buf.lower()
                                _end_pos = -1
                                for _marker, _mlen in [
                                    ("shukriya", 8),
                                    ("thank you for calling", 20),
                                ]:
                                    _p = _buf_l.find(_marker)
                                    if _p != -1:
                                        _end_pos = _p + _mlen
                                        break
                                if _end_pos != -1:
                                    # End-marker reached ("shukriya" / "thank you for
                                    # calling") — the confirmation message is complete.
                                    # Fire clear + close immediately so any duplicate
                                    # audio Gemini queued AFTER this marker is flushed
                                    # from Vobiz's buffer before it plays.
                                    # (Audio leads text by ~200-500ms, so by the time
                                    # this text arrives the first message has already
                                    # played; clear only removes the repeat audio.)
                                    evt("confirmation_done", reason="end_marker",
                                        tc_seq=_tc_seq, burst=_post_save_burst,
                                        audio_secs=round(confirmation_audio_secs, 2))
                                    confirmation_done = True
                                    log("🔇 End-marker reached — "
                                        "clearing Vobiz buffer + closing in 0.5s")
                                    try:
                                        if not ws.closed:
                                            await ws.send_str(json.dumps({"event": "clear"}))
                                    except Exception:
                                        pass
                                    asyncio.create_task(
                                        _close_after(ws, g_ws, 0.5, log))

                        server_content = data.get("serverContent", {})

                        # ── Audio output → Vobiz ────────────────────────────
                        for part in server_content.get("modelTurn", {}).get("parts", []):
                            if part.get("inlineData"):
                                if confirmation_done:
                                    continue  # hard-block any audio after confirmation
                                if barge_in_active:
                                    continue  # customer interrupted — drop remaining agent audio
                                if _lt_first_audio:
                                    lt.mark("gemini_first_audio")
                                    _lt_first_audio = False
                                # Decode first so we can count audio duration before forwarding.
                                pcm24 = base64.b64decode(part["inlineData"]["data"])
                                # Accumulate post-save audio seconds. Requires
                                # CONFIRMATION_MIN_AUDIO_SECS before any turnComplete
                                # can close the call — prevents the wait-message's own
                                # turnComplete from triggering a close when the Sheets
                                # API returns in <80ms (faster than the audio arrives).
                                if save_done_ts > 0:
                                    confirmation_audio_secs += len(pcm24) / 48000
                                    # Log the start of each audio burst after save.
                                    # burst=1 → first confirmation message
                                    # burst=2 → second confirmation (the duplicate bug)
                                    if not _post_save_in_burst:
                                        _post_save_burst    += 1
                                        _post_save_in_burst  = True
                                        evt("response_start",
                                            burst=_post_save_burst,
                                            after_tc_seq=_tc_seq,
                                            audio_so_far_secs=round(confirmation_audio_secs, 3))
                                # Audio-duration cutoff: Gemini streams audio faster than
                                # real-time so a wall-clock check is too slow. We count
                                # PCM bytes forwarded and stop after MAX_CONFIRMATION_AUDIO_SECS
                                # (one confirmation message) so the second repetition never
                                # reaches Vobiz. Also sends {"event":"clear"} to flush any
                                # already-buffered audio.
                                if (save_done_ts > 0 and
                                        confirmation_audio_secs > MAX_CONFIRMATION_AUDIO_SECS):
                                    if not confirmation_done:
                                        evt("confirmation_done", reason="byte_cap",
                                            tc_seq=_tc_seq, burst=_post_save_burst,
                                            audio_secs=round(confirmation_audio_secs, 2))
                                        confirmation_done = True
                                        log(f"🔇 Post-save audio cutoff ({confirmation_audio_secs:.1f}s) — clearing + blocking")
                                        try:
                                            if not ws.closed:
                                                await ws.send_str(json.dumps({"event": "clear"}))
                                        except Exception:
                                            pass
                                        asyncio.create_task(_close_after(ws, g_ws, 0.0, log))
                                    continue
                                if not greeting_started:
                                    greeting_started = True
                                    log("🔊 Greeting audio started streaming to caller")
                                pcm8, downsample_state = audioop.ratecv(
                                    pcm24, 2, 1, 24000, 8000, downsample_state
                                )
                                mulaw = audioop.lin2ulaw(pcm8, 2)
                                last_ai_audio_ts = time.time()
                                if not ws.closed:
                                    await ws.send_str(json.dumps({
                                        "event": "playAudio",
                                        "media": {
                                            "contentType": "audio/x-mulaw",
                                            "sampleRate":  8000,
                                            "payload": base64.b64encode(mulaw).decode("utf-8"),
                                        },
                                    }))

                        # ── turnComplete: agent finished speaking ────────────
                        if server_content.get("turnComplete"):
                            lt.mark("gemini_turn_end")
                            lt.complete_turn()
                            _lt_first_audio = True  # reset for next turn
                            gemini_turn_end_ts = time.time()
                            waiting_for_gemini = False  # customer may speak again
                            _tc_seq += 1
                            evt("turn_complete",
                                tc_seq=_tc_seq,
                                save_done=(save_done_ts > 0),
                                confirmation_done=confirmation_done,
                                audio_secs=round(confirmation_audio_secs, 2))
                            if save_done_ts > 0:
                                evt("response_complete",
                                    tc_seq=_tc_seq,
                                    burst=_post_save_burst,
                                    audio_secs=round(confirmation_audio_secs, 2))
                            _post_save_in_burst = False  # next audio packet = new burst
                            ai_dur = gemini_turn_end_ts - last_ai_audio_ts if last_ai_audio_ts else 0
                            # Flush agent buffer as one clean line
                            _flushed = agent_buf.strip()
                            if _flushed:
                                line = f"[{_ts()}] Agent: {_flushed}"
                                transcript_log.append(line)
                                log(f"🤖  {line}")
                                agent_buf = ""
                                # ── Stage advancement from agent speech ─────────────
                                # When Gemini asks for address / preferred_time / name,
                                # advance ServiceGraph so the NEXT customer turn's
                                # [STAGE CONTEXT] is correct.  Without this, stage stays
                                # stuck at "subcategory" for categories where keyword
                                # extraction has no subcat patterns (e.g. Electrical,
                                # Plumbing), so Gemini never gets stage="done" context
                                # and sometimes hallucinates the confirmation without
                                # calling save_service_request.
                                if save_done_ts == 0:
                                    _t = _flushed.lower()
                                    _cur = service_graph.current_stage()
                                    _cur_idx = (_STAGE_ORDER_LIST.index(_cur)
                                                if _cur in _STAGE_ORDER_LIST else 0)
                                    for _tgt, _pats in _AGENT_STAGE_TRIGGERS:
                                        _tgt_idx = _STAGE_ORDER_LIST.index(_tgt)
                                        if _tgt_idx <= _cur_idx:
                                            continue
                                        # Guard: don't skip past a local-prompt stage
                                        # whose field hasn't been collected yet.
                                        # e.g. if address is empty, don't jump to
                                        # customer_name just because Gemini said "naam".
                                        _skip = False
                                        for _mid_idx in range(_cur_idx, _tgt_idx):
                                            _mid = _STAGE_ORDER_LIST[_mid_idx]
                                            if (_mid in _LOCAL_PROMPT_STAGES
                                                    and not service_graph.state.get(_mid)):
                                                _skip = True
                                                break
                                        if _skip:
                                            continue
                                        if any(re.search(p, _t) for p in _pats):
                                            service_graph.state = ServiceState(
                                                **{**service_graph.state, "stage": _tgt})
                                            log(f"📋 Stage ← agent asked for "
                                                f"{_tgt!r}: {_cur} → {_tgt}")
                                            break
                            if not greeting_done:
                                greeting_done = True
                                log(f"🔔 turnComplete — GREETING DONE, customer audio now live "
                                    f"(last audio {ai_dur:.2f}s ago)")
                            elif save_done_ts > 0:
                                if confirmation_audio_secs >= CONFIRMATION_MIN_AUDIO_SECS and not confirmation_done:
                                    # Enough confirmation audio has played — close now.
                                    # Also clear Vobiz's audio buffer: if Gemini generated
                                    # two turns (or a second repetition starts before this
                                    # turnComplete), the duplicate audio is already in
                                    # Vobiz's buffer — "clear" stops it playing.
                                    evt("confirmation_done", reason="turn_complete",
                                        tc_seq=_tc_seq, burst=_post_save_burst,
                                        audio_secs=round(confirmation_audio_secs, 2))
                                    confirmation_done = True
                                    log(f"✅ Confirmation turnComplete (audio={confirmation_audio_secs:.1f}s) — clearing + closing")
                                    try:
                                        if not ws.closed:
                                            await ws.send_str(json.dumps({"event": "clear"}))
                                    except Exception:
                                        pass
                                    asyncio.create_task(_close_after(ws, g_ws, 0.0, log))
                                elif confirmation_done:
                                    log("⚠️  Extra turnComplete after confirmation — ignoring")
                                else:
                                    # Not enough audio yet — this is the wait-message's turnComplete
                                    # (save completed before wait-message audio arrived). Do NOT close.
                                    log(f"ℹ️  turnComplete after save — audio {confirmation_audio_secs:.1f}s < {CONFIRMATION_MIN_AUDIO_SECS}s, waiting for confirmation")
                            else:
                                log(f"🔔 turnComplete — echo guard releases in 1.0s "
                                    f"(last audio {ai_dur:.2f}s ago)")

                        # ── Interrupted: agent cut off mid-speech ────────────
                        if server_content.get("interrupted"):
                            log("⚡ serverContent.interrupted — agent speech was interrupted by customer")

                        # ── Tool calls ───────────────────────────────────────
                        tool_call = data.get("toolCall")
                        if tool_call:
                            for fc in tool_call.get("functionCalls", []):
                                fn   = fc.get("name", "")
                                args = fc.get("args", {})
                                cid  = fc.get("id", "")
                                log(f"🔧 Tool call: {fn} | args={json.dumps(args)}")
                                evt("tool_call_start", fn=fn, call_id=cid,
                                    save_already=save_executed, tc_seq=_tc_seq)

                                if fn == "save_customer_feedback":
                                    # Normalize warranty_status — customers often say "1","2","3"
                                    # when the agent presents numbered options verbally.
                                    # Mapping prevents the raw digit from confusing the model
                                    # and causing the "1111...1111" audio preamble bug.
                                    w = args.get("warranty_status", "")
                                    if w in ("1", "yes", "Yes", "YES", "haan", "ha", "ha ji"):
                                        args["warranty_status"] = "Yes - Under Warranty"
                                    elif w in ("2", "no", "No", "NO", "nahi", "nahin", "nahi ji"):
                                        args["warranty_status"] = "No - Out of Warranty"
                                    elif w in ("3", "pata nahi", "don't know", "dont know",
                                               "unknown", "not sure", "nahi pata"):
                                        args["warranty_status"] = "Customer Does Not Know"
                                    for k in ["customer_name", "brand", "item",
                                              "product_used_since", "usage_duration",
                                              "warranty_status", "complaint"]:
                                        if args.get(k):
                                            state_engine.set_data(k, args[k])
                                    args.setdefault("caller_id", caller_id)

                                if fn == "save_service_request":
                                    service_graph.on_tool_call(args)
                                    args.setdefault("caller_id", caller_id)

                                is_save_fn = fn in ("save_customer_feedback", "save_service_request")
                                if is_save_fn and save_executed:
                                    log(f"⚠️  Duplicate {fn} call — skipping. "
                                        "Returning success so Gemini doesn't retry.")
                                    await g_ws.send(json.dumps({
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id":       cid,
                                                "name":     fn,
                                                "response": {"result": {"success": True}},
                                            }]
                                        }
                                    }))
                                elif fn in FUNCTION_MAP:
                                    _t_tc_start = time.time()
                                    res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)
                                    evt("tool_call_complete", fn=fn, success=res.get("success"),
                                        took_ms=round((time.time() - _t_tc_start) * 1000))
                                    log(f"🔧 Tool result: {res}")
                                    _trigger_local_conf = False
                                    if is_save_fn and res.get("success"):
                                        save_executed     = True
                                        save_done_ts      = time.time()
                                        confirmation_done = True  # block any Gemini audio
                                        # Minimal ack to Gemini — local TTS handles confirmation
                                        tool_result         = {"success": True}
                                        _trigger_local_conf = True
                                        log("🔒 Save OK — local TTS confirmation queued, Gemini closing")
                                        # Hook B: record Gemini-handled fields (category/subcategory/
                                        # issue_type/brand/model) from the successful save args.
                                        if _eq and fn == "save_service_request":
                                            _eq.record_gemini_fields(args)
                                    elif is_save_fn and not res.get("success"):
                                        # Save failed — send explicit error so Gemini knows
                                        # not to say "request registered" and instead apologises.
                                        err = res.get("message", "Unknown error")
                                        log(f"❌ {fn} FAILED — sheet not written: {err}")
                                        tool_result = {
                                            "success": False,
                                            "status": "SAVE_FAILED",
                                            "instruction": (
                                                "SAVE FAILED. Do NOT say the request was registered. "
                                                "Apologise to the customer: "
                                                "Hinglish: 'Maafi chahti hoon, abhi ek technical problem aa rahi hai. "
                                                "Kripya thodi der mein dobara call karein.' "
                                                "English: 'I apologise, there was a technical issue. "
                                                "Please call again in a few minutes.'"
                                            ),
                                        }
                                    else:
                                        tool_result = res
                                    await g_ws.send(json.dumps({
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id":       cid,
                                                "name":     fn,
                                                "response": {"result": tool_result},
                                            }]
                                        }
                                    }))
                                    evt("tool_result_sent", fn=fn, save_done=(save_done_ts > 0),
                                        tc_seq=_tc_seq)
                                    if _trigger_local_conf:
                                        # Close Gemini now — local code owns the confirmation audio.
                                        # Any audio Gemini generates before the WS closes is blocked
                                        # by confirmation_done=True (set above).
                                        evt("ws_close_gemini",
                                            reason="local_confirmation", delay=0)
                                        try:
                                            await g_ws.close()
                                        except Exception:
                                            pass
                                        asyncio.create_task(_local_final_confirmation())
                                else:
                                    log(f"⚠️  Unknown tool: {fn}")

                        # ── Log unexpected error fields ──────────────────────
                        if data.get("error"):
                            log(f"❌ Gemini error message: {data['error']}")

                except websockets.exceptions.ConnectionClosedError as ex:
                    if _g_reconnects < 1 and not save_executed and not ws.closed:
                        _g_reconnects += 1
                        _call_track["reconnects"] += 1
                        log(f"🔄 Gemini WS dropped — reconnecting (1/1): {type(ex).__name__}: {ex}")
                        waiting_for_gemini = True
                        try:
                            _old_g_ws = g_ws
                            g_ws = await websockets.connect(
                                GEMINI_WS_URL,
                                open_timeout=15,
                                ping_interval=20,
                                ping_timeout=20,
                            )
                            await g_ws.send(json.dumps(setup))
                            _r2 = await asyncio.wait_for(g_ws.recv(), timeout=10.0)
                            if json.loads(_r2).get("error"):
                                raise Exception(f"Reconnect setup error: {json.loads(_r2)['error']}")
                            try:
                                await _old_g_ws.close()
                            except Exception:
                                pass
                            _resume_ctx = service_graph.get_context()
                            await g_ws.send(json.dumps({
                                "clientContent": {
                                    "turns": [{"role": "user", "parts": [{"text": _resume_ctx + "\n\n[Continue the conversation from the current stage. Do not mention any interruption.]"}]}],
                                    "turnComplete": True,
                                }
                            }))
                            waiting_for_gemini = True
                            log("✅ Gemini reconnected — scheduling new receiver task")
                            _reconnected = True
                            asyncio.create_task(g_receiver())
                            return  # exit this g_receiver instance; new one takes over
                        except Exception as _re_err:
                            log(f"❌ Gemini reconnect failed: {_re_err}")
                            traceback.print_exc()
                            # fall through to finally → close call
                    else:
                        log(f"❌ g_receiver ConnectionClosedError (no retry): {type(ex).__name__}: {ex}")
                        traceback.print_exc()
                except Exception as ex:
                    log(f"❌ g_receiver error: {type(ex).__name__}: {ex}")
                    traceback.print_exc()
                else:
                    # Loop ended without exception = Gemini closed connection normally
                    try:
                        close_code = g_ws.protocol.close_code
                        close_reason = g_ws.protocol.close_reason
                        log(f"⚠️  Gemini closed connection — code={close_code} reason={close_reason!r}")
                    except Exception:
                        log("⚠️  Gemini closed connection (no close code available)")
                finally:
                    # Flush any partial buffers that didn't get a turnComplete
                    if agent_buf.strip():
                        transcript_log.append(f"[{_ts()}] Agent: {agent_buf.strip()}")
                    if customer_buf.strip():
                        transcript_log.append(f"[{_ts()}] Customer: {_clean_transcript(customer_buf.strip())}")
                    log("🔁 g_receiver exiting — releasing echo guard")
                    gemini_turn_end_ts = time.time() - 1.0
                    waiting_for_gemini = False  # unstick VAD loop
                    # If Gemini closed before the call was intentionally ended
                    # (no save + confirmation yet), AND this is not a reconnect,
                    # close Vobiz WS immediately so the call ends cleanly.
                    if not ws.closed and not confirmation_done and save_done_ts == 0 and not _reconnected:
                        log("📴 Gemini WS closed unexpectedly — closing call now")
                        asyncio.create_task(ws.close())

            asyncio.create_task(g_receiver())

            # ── 5. Vobiz audio → VAD → Sarvam Saaras v3 STT → Gemini text ──────
            # Audio is NOT sent to Gemini as raw audio. Instead:
            #   • Local VAD accumulates audio above RMS threshold
            #   • Silence after speech triggers Sarvam Saaras v3 transcription
            #   • Transcript is sent to Gemini Live as a text clientContent turn
            #   • Gemini Live responds with audio (native TTS voice)
            call_start_ts     = time.time()
            MAX_CALL_SECS     = 600
            last_customer_ts  = 0.0   # time last transcript was sent to Gemini
            # VAD state
            speech_buf          = []    # accumulated 8 kHz PCM bytes for current utterance
            speech_start_ts     = 0.0
            speech_start_perf   = 0.0  # perf_counter at VAD onset (for latency tracking)
            vad_last_speech     = 0.0  # time of last above-threshold packet
            in_speech           = False
            # Barge-in state
            barge_in_buf      = []    # accumulate frames during potential barge-in
            barge_in_start_ts = 0.0   # when high-RMS streak started
            barge_in_active   = False  # True = drop Gemini audio; customer interrupted

            # Persistent HTTP session reuses TCP connection across all STT
            # calls in this call, avoiding TCP+TLS handshake overhead (~200ms)
            # per utterance.
            sarvam_session = aiohttp.ClientSession()

            async def _stt_and_send(pcm8_bytes: bytes, _vad_start_perf: float = 0.0,
                                    _task_queued_perf: float = 0.0):
                """Transcribe utterance and send text turn to Gemini Live."""
                nonlocal last_customer_ts, waiting_for_gemini, barge_in_active, _local_validation_hint
                # Queue latency: time from asyncio.create_task() to this coroutine
                # actually starting. Reveals event-loop scheduling backpressure.
                if _task_queued_perf:
                    _q_ms = round((time.perf_counter() - _task_queued_perf) * 1000)
                    log(f"EVT queue_latency_ms={_q_ms}")
                _span = lt.new_turn()
                if _vad_start_perf:
                    _span.vad_start = _vad_start_perf
                lt.mark("vad_end")
                lt.mark("stt_start")
                t0 = time.time()
                _hints = _HINT_PHRASES_BY_STAGE.get(service_graph.current_stage())
                transcript = await _sarvam_stt(pcm8_bytes, session=sarvam_session,
                                               hint_phrases=_hints)
                lt.mark("stt_end")
                stt_ms = int((time.time() - t0) * 1000)
                if not transcript:
                    log(f"📝 STT: empty ({stt_ms}ms) — ignoring")
                    lt.discard_turn()
                    return
                log(f"📝 STT ({stt_ms}ms) → {transcript!r}")
                _call_track["stt_latencies_ms"].append(stt_ms)
                lt.set_text(transcript)
                # Re-check after STT: a concurrent task (started before
                # waiting_for_gemini was set) may have already sent a turn.
                # Sending two clientContent turns before Gemini responds
                # triggers 1008 "operation not supported".
                if waiting_for_gemini:
                    log(f"📝 STT concurrent-drop (Gemini busy): {transcript!r}")
                    _call_track["stt_dropped"] += 1
                    lt.discard_turn()
                    return
                line = f"[{_ts()}] Customer: {transcript}"
                transcript_log.append(line)
                log(f"🗣  {line}")
                last_customer_ts = time.time()
                # Keyword-extract category/subcategory so [STAGE CONTEXT] is
                # accurate for this turn (e.g. customer said "टू व्हीलर" in
                # their first message → advance past subcategory stage now).
                _update_stage_from_customer(transcript, service_graph)
                # ── Late-stage field collection with confirmation ──────────
                # Each of address / preferred_time / customer_name goes through
                # a two-step cycle:
                #   1. Customer provides value → set_pending (stage unchanged)
                #   2. Gemini echoes value ("X, sahi hai?") → customer confirms
                #      → confirm_pending() → stage advances
                # If customer corrects instead of confirming, we store the new
                # value as the next pending and confirm that one instead.
                _cur     = service_graph.current_stage()
                _clean_t = transcript.strip().rstrip("।.?! ").lower()

                # ── Local-prompt stages: address / preferred_time / customer_name ──────
                # The workflow engine owns validation, echo, and confirmation for these
                # stages.  Gemini is NOT invoked — Sarvam TTS handles all audio.
                # Falls back to Gemini only when Sarvam TTS is unavailable.
                if _cur in _LOCAL_PROMPT_STAGES and save_done_ts == 0:
                    _local_validation_hint = ""  # reset before each local stage attempt
                    _handled = await _handle_local_stage_response(transcript, _cur, _clean_t)
                    if _handled:
                        lt.discard_turn()
                        return
                    # TTS unavailable — fall through to Gemini with current pending state
                    log(f"⚠️ Local TTS unavailable for stage={_cur} — falling back to Gemini")

                lt.mark("langgraph_start")
                stage_ctx = service_graph.get_context()
                lt.mark("langgraph_end")
                _hint_line = (f"\n\n[VALIDATION_NOTE: {_local_validation_hint}]"
                              if _local_validation_hint else "")
                full_text = f"{stage_ctx}{_hint_line}\n\nCustomer: {transcript}"
                _local_validation_hint = ""  # consumed
                log(f"📋 Stage: {service_graph.current_stage()}")
                try:
                    lt.mark("gemini_send")
                    await g_ws.send(json.dumps({
                        "clientContent": {
                            "turns": [{"role": "user", "parts": [{"text": full_text}]}],
                            "turnComplete": True,
                        }
                    }))
                    waiting_for_gemini = True   # block new utterances until Gemini responds
                    barge_in_active   = False   # clear interrupt flag — new response incoming
                    log(f"📤 Gemini send OK (+{int((time.time()-t0)*1000)}ms total)")
                except Exception as send_err:
                    log(f"❌ Gemini WS send failed after STT: {send_err}")
                    lt.discard_turn()

            # ── Local-stage helpers (closures; no Gemini involved) ──────────────────
            # These run for address / preferred_time / customer_name stages.
            # Sarvam TTS synthesizes audio, field_validators validates the response.
            # Gemini is not invoked — latency for these turns = STT + TTS only.

            async def _play_local_audio(mulaw_bytes: bytes) -> None:
                """Stream μ-law 8 kHz audio to Vobiz at realtime pace.

                Each 800-byte chunk = 100 ms of 8 kHz μ-law audio.
                Sleeping 0.1s per chunk keeps Vobiz's buffer near-empty so that
                when the WS closes, little or no audio is discarded.
                """
                nonlocal waiting_for_gemini, last_ai_audio_ts
                if not mulaw_bytes:
                    return
                waiting_for_gemini = True
                CHUNK = 800  # 100 ms at 8 kHz μ-law
                for i in range(0, len(mulaw_bytes), CHUNK):
                    if ws.closed:
                        break
                    chunk = mulaw_bytes[i:i + CHUNK]
                    await ws.send_str(json.dumps({
                        "event": "playAudio",
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate":  8000,
                            "payload":     base64.b64encode(chunk).decode(),
                        },
                    }))
                    last_ai_audio_ts = time.time()
                    await asyncio.sleep(0.1)  # realtime pace: 800 bytes = 100 ms at 8 kHz
                waiting_for_gemini = False

            def _validate_field(stage: str, transcript: str) -> ValidationResult:
                """Dispatch to the appropriate field validator."""
                if stage == "customer_name":
                    return validate_customer_name(transcript)
                if stage == "address":
                    return validate_address(transcript)
                if stage == "preferred_time":
                    return validate_preferred_time(transcript)
                log(f"WARNING: no validator for stage={stage}, accepting with low confidence")
                from core.field_validators import ValidationResult as VR
                return VR(True, 0.5, transcript, "no_validator", stage, transcript)

            def _log_field_confidence(vr: ValidationResult) -> None:
                """Emit structured JSON confidence log for analytics."""
                log(json.dumps({
                    "field":      vr.field,
                    "value":      vr.extracted[:80],
                    "confidence": round(vr.confidence, 2),
                    "accepted":   vr.accepted,
                    "reason":     vr.reason,
                    "raw":        vr.raw[:80],
                }, ensure_ascii=False))

            async def _local_final_confirmation() -> None:
                """
                Synthesize and play the final confirmation, then close both WebSockets.
                Called after save_service_request succeeds — replaces Gemini-generated
                confirmation entirely, eliminating the duplicate-audio bug.
                """
                confirm_text = local_tts.build_confirmation_text(service_graph.state)
                log(f"EVT final_confirmation_started text_len={len(confirm_text)}")
                log(f"💬 Confirmation: {confirm_text!r}")
                mulaw = await local_tts.synthesize(confirm_text, SARVAM_API_KEY, sarvam_session)
                if mulaw:
                    audio_secs = len(mulaw) / 8000  # μ-law 8 kHz: 1 byte = 1/8000 s
                    await _play_local_audio(mulaw)
                    log(f"EVT final_confirmation_completed audio_secs={audio_secs:.1f}")
                    # _play_local_audio now paces at realtime (0.1s per 100 ms chunk),
                    # so Vobiz buffer is near-empty when streaming ends.  A small safety
                    # margin lets any residual buffer drain before we close the WS.
                    await asyncio.sleep(1.0)
                else:
                    log("⚠️ EVT local_tts_failed — trying short fallback TTS")
                    # Retry with a shorter message — Sarvam might handle a simpler string
                    short_msg = (
                        f"{service_graph.state.get('customer_name', '')} ji, "
                        "aapki request register ho gayi hai. Shukriya!"
                    )
                    mulaw_fb = await local_tts.synthesize(short_msg, SARVAM_API_KEY, sarvam_session)
                    if mulaw_fb:
                        await _play_local_audio(mulaw_fb)
                        await asyncio.sleep(1.0)
                    else:
                        # Both TTS attempts failed — try Gemini as last resort
                        log("⚠️ Short TTS also failed — trying Gemini fallback")
                        try:
                            await g_ws.send(json.dumps({
                                "clientContent": {
                                    "turns": [{"role": "user", "parts": [{"text": (
                                        "[SAVE ALREADY DONE — do NOT call save_service_request again]\n"
                                        f"Speak this confirmation to the customer exactly:\n{confirm_text}"
                                    )}]}],
                                    "turnComplete": True,
                                }
                            }))
                            await asyncio.sleep(6.0)
                        except Exception as _fb_err:
                            log(f"⚠️ Gemini fallback also failed: {_fb_err} — closing without confirmation audio")
                log("EVT ws_close_vobiz reason=local_confirmation_complete")
                if not ws.closed:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            async def _trigger_local_save() -> None:
                """
                Directly invoke save_service_request without Gemini.
                Called when stage reaches 'done' via local-stage collection path
                (address + time + name all handled locally).
                """
                nonlocal save_executed, save_done_ts, confirmation_done, _local_validation_hint
                if save_executed:
                    log("⚠️ LOCAL SAVE: already executed — skipping")
                    return
                # Set guard flags BEFORE the blocking save operation.
                # If Gemini's WS closes while the save is in progress,
                # g_receiver's finally block checks these flags to decide
                # whether to close the Vobiz WS.  Without setting them early,
                # g_receiver sees confirmation_done=False + save_done_ts=0
                # and kills the call before the confirmation audio plays.
                save_executed     = True
                save_done_ts      = time.time()
                confirmation_done = True
                args = {
                    "caller_id":      caller_id,
                    "category":       service_graph.state.get("category")       or "",
                    "subcategory":    service_graph.state.get("subcategory")    or "",
                    "issue_type":     service_graph.state.get("issue_type")     or "",
                    "brand":          service_graph.state.get("brand")          or "",
                    "model":          service_graph.state.get("model")          or "",
                    "severity":       service_graph.state.get("severity")       or "",
                    "error_code":     service_graph.state.get("error_code")     or "",
                    "address":        service_graph.state.get("address")        or "",
                    "preferred_time": service_graph.state.get("preferred_time") or "",
                    "customer_name":  service_graph.state.get("customer_name")  or "",
                }
                log(f"🔧 LOCAL SAVE args: {json.dumps(args, ensure_ascii=False)}")
                evt("tool_call_start", fn="save_service_request", call_id="local", save_already=False)
                t0 = time.time()
                res = await asyncio.to_thread(FUNCTION_MAP["save_service_request"], **args)
                evt("tool_call_complete", fn="save_service_request",
                    success=res.get("success"),
                    took_ms=round((time.time() - t0) * 1000))
                if res.get("success"):
                    log("✅ LOCAL SAVE OK")
                    # Record Gemini-handled fields from local-save args
                    if _eq:
                        _eq.record_gemini_fields(args)
                    await _local_final_confirmation()
                else:
                    err = res.get("message", "unknown error")
                    log(f"❌ LOCAL SAVE FAILED: {err}")
                    mulaw = await local_tts.synthesize(
                        "Maafi chahti hoon, abhi ek technical problem aa rahi hai. "
                        "Kripya thodi der mein dobara call karein.",
                        SARVAM_API_KEY, sarvam_session,
                    )
                    if mulaw:
                        await _play_local_audio(mulaw)
                    await asyncio.sleep(2.0)
                    if not ws.closed:
                        await ws.close()

            def _addr_instrumentation(
                raw_stt: str,
                previous: str,
                parsed: "AddressFields | None",
                final: str,
                correction_detected: bool,
            ) -> None:
                """Emit structured address instrumentation log."""
                rec = {
                    "raw_stt":            raw_stt,
                    "previous_address":   previous,
                    "parsed_address": {
                        "society":  parsed.society  if parsed else "",
                        "sector":   parsed.sector   if parsed else "",
                        "city":     parsed.city     if parsed else "",
                        "landmark": parsed.landmark if parsed else "",
                    },
                    "final_address":      final,
                    "correction_detected": correction_detected,
                }
                log(f"ADDR_ENTITY {json.dumps(rec, ensure_ascii=False)}")
                evt("addr_entity",
                    correction=correction_detected,
                    sector=(parsed.sector if parsed else ""),
                    city=(parsed.city if parsed else ""),
                    final=final[:60])

            async def _auto_confirm_and_advance(stage: str, value: str):
                """Auto-confirm a high-confidence field and advance to the next stage.

                Skips the "X, sahi hai?" echo — the value is accepted directly.
                Returns True if handled, False if TTS failed (fall through to Gemini),
                or None if TTS failed and caller should fall back to the echo path.
                """
                nonlocal save_executed, save_done_ts, confirmation_done
                service_graph.confirm_pending()
                new_stage = service_graph.current_stage()
                log(f"⚡ AUTO-CONFIRM {stage}: {value!r} → stage={new_stage}")
                evt("local_field_auto_confirmed", field=stage, value=value[:40],
                    next_stage=new_stage)
                if _eq:
                    _eq.record_local_confirmed(stage, value)
                if new_stage in _LOCAL_PROMPT_STAGES:
                    prompt = local_tts.PROMPTS.get(new_stage, "")
                    if prompt:
                        mulaw = await local_tts.synthesize(prompt, SARVAM_API_KEY, sarvam_session)
                        if mulaw is None:
                            return None  # TTS down — let caller fall through
                        await _play_local_audio(mulaw)
                elif new_stage == "done":
                    # Set guard flags SYNCHRONOUSLY before scheduling the task.
                    # asyncio processes I/O callbacks (Gemini close frame) before
                    # newly-scheduled tasks, so g_receiver's finally block could
                    # run before _trigger_local_save's first line executes.
                    # NOTE: do NOT set save_executed here — _trigger_local_save
                    # checks it to avoid double-execution and must see False.
                    save_done_ts      = time.time()
                    confirmation_done = True
                    asyncio.create_task(_trigger_local_save())
                return True

            async def _handle_local_stage_response(
                transcript: str, stage: str, clean_t: str,
            ) -> bool:
                """
                Handle a customer turn for address / preferred_time / customer_name
                without invoking Gemini.

                Flow:
                  No pending → validate → if conf ≥ threshold: auto-confirm + advance
                                        → if conf < threshold: set_pending + play echo
                                        → if fail: play retry prompt
                  Pending     → if confirm: confirm_pending → play next prompt / trigger save
                              → if correction:
                                  address stage → field-level merge (ADDRESS_CORRECTION mode)
                                  other stages  → clear + re-validate

                Returns True if the turn was handled locally.
                Returns False if Sarvam TTS is unavailable (fall through to Gemini).
                """
                nonlocal _local_validation_hint, save_executed, save_done_ts, confirmation_done
                pend = service_graph.pending

                # ── Confirmation turn ─────────────────────────────────────────────
                if pend is not None and pend["field"] == stage:
                    pend_value = pend["value"]
                    if _is_confirmation(clean_t):
                        service_graph.confirm_pending()
                        new_stage = service_graph.current_stage()
                        log(f"✅ LOCAL {stage} confirmed: {pend_value!r} → stage={new_stage}")
                        evt("local_field_confirmed", field=stage, value=pend_value[:40],
                            next_stage=new_stage)
                        # Hook C3 — field confirmed
                        if _eq:
                            _eq.record_local_confirmed(stage, pend_value)
                        if new_stage in _LOCAL_PROMPT_STAGES:
                            prompt = local_tts.PROMPTS.get(new_stage, "")
                            if prompt:
                                mulaw = await local_tts.synthesize(prompt, SARVAM_API_KEY, sarvam_session)
                                if mulaw is None:
                                    return False  # TTS down, let Gemini handle
                                await _play_local_audio(mulaw)
                        elif new_stage == "done":
                            # Set guard flags SYNCHRONOUSLY — same reason as in
                            # _auto_confirm_and_advance (see comment there).
                            # NOTE: do NOT set save_executed — _trigger_local_save
                            # checks it to avoid double-execution.
                            save_done_ts      = time.time()
                            confirmation_done = True
                            asyncio.create_task(_trigger_local_save())
                    else:
                        # ── Correction path ───────────────────────────────────────
                        service_graph.clear_pending()
                        log(f"🔄 LOCAL {stage} correction: {transcript!r}")

                        if stage == "address":
                            # ADDRESS_CORRECTION mode: attempt field-level merge
                            # so partial corrections ("nahi, sector 71") preserve
                            # the rest of the original address.
                            correction_detected = is_address_correction(clean_t)
                            merged = merge_address_correction(pend_value, transcript)
                            if merged:
                                parsed = parse_address_fields(merged)
                                _addr_instrumentation(
                                    raw_stt=transcript,
                                    previous=pend_value,
                                    parsed=parsed,
                                    final=merged,
                                    correction_detected=correction_detected,
                                )
                                service_graph.set_pending(stage, merged)
                                # Hook C2a — address merge correction
                                if _eq:
                                    _eq.record_local_correction(stage, merged, None)
                                echo = local_tts.build_echo_text(stage, merged)
                                mulaw = await local_tts.synthesize(echo, SARVAM_API_KEY, sarvam_session)
                                if mulaw is None:
                                    return False
                                await _play_local_audio(mulaw)
                                return True
                            # No specific fields detected in correction — full replacement
                            log(f"🔄 ADDRESS_CORRECTION: no field match → full replacement")

                        vr = _validate_field(stage, transcript)
                        _log_field_confidence(vr)

                        if stage == "address":
                            parsed = parse_address_fields(vr.extracted if vr.accepted else transcript)
                            # On first collection, store structured address when parseable
                            pending_val = format_address_fields(parsed) if (
                                vr.accepted and (parsed.sector or parsed.city)
                            ) else (vr.extracted if vr.accepted else "")
                            _addr_instrumentation(
                                raw_stt=transcript,
                                previous=pend_value,
                                parsed=parsed,
                                final=pending_val,
                                correction_detected=is_address_correction(clean_t),
                            )
                        else:
                            pending_val = vr.extracted

                        if vr.accepted:
                            _corr_val = pending_val or vr.extracted
                            service_graph.set_pending(stage, _corr_val)
                            # Hook C2b — validate-path correction
                            if _eq:
                                _eq.record_local_correction(stage, _corr_val, vr.confidence)

                            if vr.confidence >= _AUTO_CONFIRM_CONFIDENCE:
                                result = await _auto_confirm_and_advance(stage, _corr_val)
                                if result is not None:
                                    return result
                            # Low confidence or TTS failed — echo for manual confirm
                            echo = local_tts.build_echo_text(stage, _corr_val)
                            mulaw = await local_tts.synthesize(echo, SARVAM_API_KEY, sarvam_session)
                            if mulaw is None:
                                return False
                            await _play_local_audio(mulaw)
                        else:
                            retry = local_tts.PROMPTS.get(f"{stage}_retry",
                                                          local_tts.PROMPTS.get(stage, ""))
                            if retry:
                                mulaw = await local_tts.synthesize(retry, SARVAM_API_KEY, sarvam_session)
                                if mulaw is None:
                                    _local_validation_hint = (
                                        f"Customer's {stage} correction was INVALID "
                                        f"(reason: {vr.reason}). Please re-ask in Hinglish."
                                    )
                                    return False
                                await _play_local_audio(mulaw)
                    return True

                # ── First collection ──────────────────────────────────────────────
                vr = _validate_field(stage, transcript)
                _log_field_confidence(vr)

                if stage == "address" and vr.accepted:
                    parsed = parse_address_fields(vr.extracted)
                    # Store structured address (more merge-friendly) when sector/city detected
                    structured = format_address_fields(parsed)
                    pending_val = structured if (parsed.sector or parsed.city) else vr.extracted
                    _addr_instrumentation(
                        raw_stt=transcript,
                        previous="",
                        parsed=parsed,
                        final=pending_val,
                        correction_detected=False,
                    )
                else:
                    parsed      = None
                    pending_val = vr.extracted

                if vr.accepted:
                    service_graph.set_pending(stage, pending_val)
                    log(f"📝 LOCAL {stage} candidate: {pending_val!r} "
                        f"(conf={vr.confidence:.2f})")
                    evt("local_field_candidate", field=stage,
                        value=pending_val[:40], confidence=round(vr.confidence, 2))
                    # Hook C1 — first candidate for local field
                    if _eq:
                        _eq.record_local_candidate(stage, pending_val, vr.confidence)

                    if vr.confidence >= _AUTO_CONFIRM_CONFIDENCE:
                        # High confidence — skip "sahi hai?" and auto-confirm
                        result = await _auto_confirm_and_advance(stage, pending_val)
                        if result is not None:
                            return result
                        # _auto_confirm_and_advance returned None → TTS failed, fall through
                    else:
                        echo = local_tts.build_echo_text(stage, pending_val)
                        mulaw = await local_tts.synthesize(echo, SARVAM_API_KEY, sarvam_session)
                        if mulaw is None:
                            return False
                        await _play_local_audio(mulaw)
                else:
                    log(f"❌ LOCAL {stage} rejected: {transcript!r} "
                        f"reason={vr.reason} conf={vr.confidence:.2f}")
                    evt("local_field_rejected", field=stage,
                        reason=vr.reason, confidence=round(vr.confidence, 2))
                    if stage == "address":
                        _addr_instrumentation(
                            raw_stt=transcript,
                            previous="",
                            parsed=parse_address_fields(transcript),
                            final="",
                            correction_detected=False,
                        )
                    retry = local_tts.PROMPTS.get(f"{stage}_retry",
                                                  local_tts.PROMPTS.get(stage, ""))
                    if retry:
                        mulaw = await local_tts.synthesize(retry, SARVAM_API_KEY, sarvam_session)
                        if mulaw is None:
                            _local_validation_hint = (
                                f"Customer's {stage} input was INVALID "
                                f"(reason: {vr.reason}). Please re-ask in Hinglish."
                            )
                            return False
                        await _play_local_audio(mulaw)
                return True

            async for msg in ws:
                if time.time() - call_start_ts > MAX_CALL_SECS:
                    log(f"⏱ Call timeout ({MAX_CALL_SECS}s) — closing.")
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("event") == "media":
                        now = time.time()

                        raw_mulaw = base64.b64decode(data["media"]["payload"])
                        pcm8      = audioop.ulaw2lin(raw_mulaw, 2)
                        if RECORD_CALLS:
                            pcm8_frames.append(pcm8)

                        # ── Post-save guard: block all input after save ────
                        if save_done_ts > 0:
                            continue

                        # ── Greeting guard: wait for Gemini to finish greeting ─
                        if not greeting_done:
                            # Safety release at 20s in case turnComplete never fires
                            if now - call_start_ts > 20.0:
                                greeting_done = True
                                log(f"🔔 Greeting guard safety-released at "
                                    f"{now - call_start_ts:.1f}s")
                            else:
                                continue

                        # ── Safety: release echo guard if turnComplete missing ─
                        if (last_ai_audio_ts > gemini_turn_end_ts and
                                now - last_ai_audio_ts > 8.0):
                            log(f"⚠️  Echo guard safety timeout — forcing release")
                            gemini_turn_end_ts = now - 1.0

                        # ── Echo guard: 0.3s buffer after agent turn ──────────
                        if now - gemini_turn_end_ts < 0.3:
                            continue

                        # ── Inactivity timeout (25s no speech after agent turn) ─
                        if (save_done_ts == 0
                                and gemini_turn_end_ts > 0
                                and last_customer_ts < gemini_turn_end_ts
                                and now - gemini_turn_end_ts > 25.0):
                            log("⏱ Inactivity — no customer speech for 25s, closing")
                            break

                        # ── VAD + Barge-in ────────────────────────────────────
                        rms = audioop.rms(pcm8, 2)

                        # ── Barge-in: stop agent audio when customer interrupts ─
                        # Only fires while agent is actively speaking. Requires
                        # BARGE_IN_SUSTAIN_SECS of sustained high-RMS so fan
                        # noise and background sounds do NOT trigger it.
                        _agent_speaking = (
                            waiting_for_gemini
                            and last_ai_audio_ts > 0
                            and now - last_ai_audio_ts < 2.0
                        )
                        if _agent_speaking and not barge_in_active:
                            if rms >= BARGE_IN_RMS_THRESHOLD:
                                if barge_in_start_ts == 0.0:
                                    barge_in_start_ts = now
                                    barge_in_buf.clear()
                                barge_in_buf.append(pcm8)
                                if now - barge_in_start_ts >= BARGE_IN_SUSTAIN_SECS:
                                    barge_in_active = True
                                    _call_track["barge_ins"] += 1
                                    log(f"⚡ Barge-in (rms={rms}, "
                                        f"sustained={now - barge_in_start_ts:.2f}s) — stopping agent audio")
                                    barge_in_start_ts = 0.0
                                    if not ws.closed:
                                        await ws.send_str(json.dumps({"event": "clear"}))
                                    waiting_for_gemini = False  # let this utterance through
                                    # Transfer barge-in frames → speech_buf (don't lose start)
                                    speech_buf.clear()
                                    speech_buf.extend(barge_in_buf)
                                    barge_in_buf.clear()
                                    in_speech           = True
                                    speech_start_ts     = now - BARGE_IN_SUSTAIN_SECS
                                    speech_start_perf   = time.perf_counter() - BARGE_IN_SUSTAIN_SECS
                                    vad_last_speech     = now
                            else:
                                # RMS dropped below threshold — reset accumulator
                                if barge_in_start_ts != 0.0:
                                    barge_in_start_ts = 0.0
                                    barge_in_buf.clear()

                        if rms >= VAD_SPEECH_THRESHOLD:
                            if not in_speech:
                                in_speech           = True
                                speech_start_ts     = now
                                speech_start_perf   = time.perf_counter()
                                speech_buf.clear()
                                log(f"🎙 Speech start (rms={rms})")
                            speech_buf.append(pcm8)
                            vad_last_speech = now
                            # Hard ceiling — force-flush if utterance runs too long
                            if now - speech_start_ts >= VAD_MAX_SPEECH_SECS:
                                combined   = b"".join(speech_buf)
                                speech_buf.clear()
                                in_speech  = False
                                log(f"🔁 Utterance max duration — flushing to STT")
                                asyncio.create_task(_stt_and_send(
                                    combined, speech_start_perf, time.perf_counter()))

                        elif in_speech:
                            speech_buf.append(pcm8)  # keep silence tail for natural phrase end
                            if now - vad_last_speech >= VAD_END_SECS:
                                combined  = b"".join(speech_buf)
                                speech_buf.clear()
                                in_speech = False
                                duration  = now - speech_start_ts
                                log(f"🔇 Speech end — {duration:.2f}s")
                                if duration >= VAD_MIN_SPEECH_SECS:
                                    if waiting_for_gemini:
                                        log(f"⏭ Gemini busy — utterance dropped ({duration:.2f}s)")
                                        _call_track["stt_dropped"] += 1
                                    else:
                                        asyncio.create_task(_stt_and_send(
                                            combined, speech_start_perf, time.perf_counter()))
                                else:
                                    log(f"⏭ Too short ({duration:.2f}s) — ignoring")

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log(f"❌ Vobiz WS error: {ws.exception()}")
                    break

    except Exception as e:
        print(f"[{_ts()}] ❌ Gemini Live Error: {e}", flush=True)
        traceback.print_exc()
    finally:
        # Close reconnected Gemini WS (the async with block only closes the initial one).
        # Do NOT check .closed — ClientConnection (websockets v13+) has no such attribute.
        if g_ws:
            try:
                await g_ws.close()
            except Exception:
                pass
        # Close persistent Sarvam HTTP session (if it was created)
        try:
            if 'sarvam_session' in dir():
                await sarvam_session.close()
        except Exception:
            pass
        elapsed = time.time() - (call_start_ts if 'call_start_ts' in dir() else time.time())
        log(f"📞 Call ended | duration={elapsed:.1f}s | transcript={len(transcript_log)} lines")
        if RECORD_CALLS and pcm8_frames:
            try:
                os.makedirs(RECORDINGS_DIR, exist_ok=True)
                wav_path = os.path.join(RECORDINGS_DIR, f"{caller_id}_{call_ts}.wav")
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)   # 16-bit PCM
                    wf.setframerate(8000)
                    wf.writeframes(b"".join(pcm8_frames))
                _call_track["local_wav"] = f"{caller_id}_{call_ts}.wav"
                log(f"🎙️  Recording saved → {wav_path} ({len(pcm8_frames)} frames, {elapsed:.0f}s)")
                gcs_uri = await asyncio.to_thread(upload_recording_to_gcs, wav_path, caller_id)
                if gcs_uri:
                    _call_track["gcs_uri"] = gcs_uri
                    log(f"☁️  Recording uploaded → {gcs_uri}")
            except Exception as rec_err:
                log(f"⚠️  Recording save failed: {rec_err}")
        log("📋 TRANSCRIPT:\n" + ("\n".join(transcript_log) if transcript_log else "  (empty)"))
        # Dump full event sequence — paste into any JSON viewer or filter with grep EVT_SUMMARY
        log(f"EVT_SUMMARY {json.dumps(_evt_log, default=str)}")
        log("📧 Attempting transcript email ...")
        await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
        # ── Write observability call log (non-blocking) ───────────────────────
        try:
            _stt_lats = _call_track["stt_latencies_ms"]
            _stt_avg  = sum(_stt_lats) / len(_stt_lats) if _stt_lats else 0
            asyncio.create_task(asyncio.to_thread(
                save_call_log,
                caller_id    = caller_id,
                duration_secs= elapsed,
                stage_reached= service_graph.current_stage(),
                saved        = save_executed,
                category     = service_graph.state.get("category", "") or "",
                subcategory  = service_graph.state.get("subcategory", "") or "",
                issue_type   = service_graph.state.get("issue_type", "") or "",
                customer_name= service_graph.state.get("customer_name", "") or "",
                address      = service_graph.state.get("address", "") or "",
                preferred_time= service_graph.state.get("preferred_time", "") or "",
                stt_count    = len(_stt_lats),
                stt_avg_ms   = _stt_avg,
                stt_drops    = _call_track["stt_dropped"],
                barge_ins    = _call_track["barge_ins"],
                reconnects   = _call_track["reconnects"],
                audio_gcs    = _call_track["gcs_uri"],
                local_wav    = _call_track["local_wav"],
                transcript   = transcript_log,
            ))
        except Exception as _log_err:
            log(f"⚠️  Call log write failed: {_log_err}")
        if lt.completed:
            asyncio.create_task(asyncio.to_thread(
                save_turn_latency,
                caller_id = caller_id,
                turns     = lt.completed,
            ))
        # Hook D — flush extraction quality records (non-blocking)
        if _eq:
            try:
                _eq.mark_call_saved(save_executed)
                _qual_records = _eq.flush()
                if _qual_records:
                    log(f"QUAL_SNAPSHOT {json.dumps(_qual_records, ensure_ascii=False, default=str)}")
                    asyncio.create_task(asyncio.to_thread(
                        save_field_quality_log, records=_qual_records,
                    ))
            except Exception as _eq_err:
                log(f"⚠️  Quality log flush failed: {_eq_err}")
        if not ws.closed:
            await ws.close()

    return ws
