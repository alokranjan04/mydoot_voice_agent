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
from core.service_graph import ServiceGraph
from mydoot_functions import FUNCTION_MAP, send_call_summary_email, upload_recording_to_gcs

# ── Sarvam Saaras v3 STT ─────────────────────────────────────────────────────
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"

# ── VAD (Voice Activity Detection) — local, before STT ───────────────────────
# RMS threshold: packets below this are silence / line noise, not forwarded.
VAD_SPEECH_THRESHOLD  = int(os.getenv("VAD_SPEECH_THRESHOLD",  "100"))
# Seconds of silence after speech that signals end-of-utterance.
# 0.5s balances responsiveness vs. cutting off slow speakers.
# Raise to 0.7 via env if callers are frequently getting cut off.
VAD_END_SECS          = float(os.getenv("VAD_END_SECS",          "0.5"))
# Minimum utterance duration to bother sending to STT (avoids noise blips).
VAD_MIN_SPEECH_SECS   = float(os.getenv("VAD_MIN_SPEECH_SECS",  "0.5"))
# Hard ceiling — force-flush utterance if customer speaks this long non-stop.
VAD_MAX_SPEECH_SECS   = float(os.getenv("VAD_MAX_SPEECH_SECS",  "30.0"))

# ── Call recording ────────────────────────────────────────────────────────────
# Set RECORD_CALLS=1 to save each call's inbound PSTN audio as a WAV file.
# Files are written to RECORDINGS_DIR (default: ./recordings/).
# Use these WAV files with test_asr_compare.py to benchmark ASR services.
RECORD_CALLS               = os.getenv("RECORD_CALLS", "0").lower() in ("1", "true", "yes")
RECORDINGS_DIR             = os.getenv("RECORDINGS_DIR", "recordings")
# Hard time ceiling on audio forwarding after save. The confirmation is ~6s.
# Set large enough to survive a slow Sheets API (cold-start can be 5-8s).
MAX_CONFIRMATION_AUDIO_SECS = 12.0
# Minimum post-save audio that must have played before a turnComplete is
# allowed to close the call. The wait message ("Ek second...") is ~2s.
# Requiring 2.5s ensures the wait-message's own turnComplete is NOT treated
# as the confirmation-done signal, even when the tool call arrives early.
CONFIRMATION_MIN_AUDIO_SECS = 2.5


def _ts():
    """Short HH:MM:SS.mmm timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def _close_after(vobiz_ws, gemini_ws, delay: float, log_fn):
    """Close both WebSockets after `delay` seconds — ends the call gracefully."""
    await asyncio.sleep(delay)
    log_fn(f"📴 Closing call after {delay}s post-confirmation delay")
    try:
        await gemini_ws.close()
    except Exception:
        pass
    try:
        if not vobiz_ws.closed:
            await vobiz_ws.close()
    except Exception:
        pass


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
    import re
    cleaned = re.sub(r"(\[unclear\]\s*)+", "[unclear] ", cleaned).strip()
    return cleaned


async def _sarvam_stt(pcm8_bytes: bytes,
                      session: "aiohttp.ClientSession | None" = None) -> str:
    """
    Transcribe 8 kHz 16-bit mono PCM via Sarvam Saaras v3.
    Pass a persistent `session` (created once per call) to avoid
    TCP + TLS handshake overhead on every utterance (~200-300ms saved).
    Returns the transcript string, or "" on failure / empty result.
    """
    if not pcm8_bytes or not SARVAM_API_KEY:
        return ""
    try:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(pcm8_bytes)
        wav_bytes = buf.getvalue()

        form = aiohttp.FormData()
        form.add_field("file", wav_bytes,
                       filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "saaras:v3")
        form.add_field("language_code", "hi-IN")

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
    state_engine   = ConversationStateEngine()
    service_graph  = ServiceGraph()
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

            async def g_receiver():
                nonlocal downsample_state, last_ai_audio_ts, gemini_turn_end_ts
                nonlocal greeting_started, greeting_done, save_done_ts, save_executed
                nonlocal confirmation_done, confirmation_audio_secs, waiting_for_gemini
                nonlocal agent_buf, customer_buf
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

                        server_content = data.get("serverContent", {})

                        # ── Audio output → Vobiz ────────────────────────────
                        for part in server_content.get("modelTurn", {}).get("parts", []):
                            if part.get("inlineData"):
                                if confirmation_done:
                                    continue  # hard-block any audio after confirmation
                                # Time-based cutoff: block audio > MAX_CONFIRMATION_AUDIO_SECS
                                # after save. Prevents double-confirmation even when both
                                # utterances arrive before a single turnComplete fires.
                                if (save_done_ts > 0 and
                                        time.time() - save_done_ts > MAX_CONFIRMATION_AUDIO_SECS):
                                    if not confirmation_done:
                                        confirmation_done = True
                                        log(f"🔇 Post-save audio cutoff ({MAX_CONFIRMATION_AUDIO_SECS}s) — blocking")
                                        asyncio.create_task(_close_after(ws, g_ws, 0.0, log))
                                    continue
                                # Accumulate post-save audio seconds. Requires
                                # CONFIRMATION_MIN_AUDIO_SECS before any turnComplete
                                # can close the call — prevents the wait-message's own
                                # turnComplete from triggering a close when the Sheets
                                # API returns in <80ms (faster than the audio arrives).
                                pcm24 = base64.b64decode(part["inlineData"]["data"])
                                if save_done_ts > 0:
                                    confirmation_audio_secs += len(pcm24) / 48000
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
                            gemini_turn_end_ts = time.time()
                            waiting_for_gemini = False  # customer may speak again
                            ai_dur = gemini_turn_end_ts - last_ai_audio_ts if last_ai_audio_ts else 0
                            # Flush agent buffer as one clean line
                            if agent_buf.strip():
                                line = f"[{_ts()}] Agent: {agent_buf.strip()}"
                                transcript_log.append(line)
                                log(f"🤖  {line}")
                                agent_buf = ""
                            if not greeting_done:
                                greeting_done = True
                                log(f"🔔 turnComplete — GREETING DONE, customer audio now live "
                                    f"(last audio {ai_dur:.2f}s ago)")
                            elif save_done_ts > 0:
                                if confirmation_audio_secs >= CONFIRMATION_MIN_AUDIO_SECS and not confirmation_done:
                                    # Enough confirmation audio has played — close now.
                                    confirmation_done = True
                                    log(f"✅ Confirmation turnComplete (audio={confirmation_audio_secs:.1f}s) — closing")
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
                                    res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)
                                    log(f"🔧 Tool result: {res}")
                                    if is_save_fn and res.get("success"):
                                        save_executed = True
                                        save_done_ts = time.time()
                                        log("🔒 Post-save guard active — blocking audio for 15s")
                                        # Fallback close: if confirmation turnComplete never fires
                                        # (e.g. Gemini silent after save), close after 15s.
                                        asyncio.create_task(_close_after(ws, g_ws, 15.0, log))
                                        tool_result = res
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
                                else:
                                    log(f"⚠️  Unknown tool: {fn}")

                        # ── Log unexpected error fields ──────────────────────
                        if data.get("error"):
                            log(f"❌ Gemini error message: {data['error']}")

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
                    # (no save + confirmation yet), close Vobiz WS immediately
                    # so the call ends cleanly instead of hanging for 25s.
                    if not ws.closed and not confirmation_done and save_done_ts == 0:
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
            speech_buf        = []    # accumulated 8 kHz PCM bytes for current utterance
            speech_start_ts   = 0.0
            vad_last_speech   = 0.0   # time of last above-threshold packet
            in_speech         = False

            # Persistent HTTP session reuses TCP connection across all STT
            # calls in this call, avoiding TCP+TLS handshake overhead (~200ms)
            # per utterance.
            sarvam_session = aiohttp.ClientSession()

            async def _stt_and_send(pcm8_bytes: bytes):
                """Transcribe utterance and send text turn to Gemini Live."""
                nonlocal last_customer_ts, waiting_for_gemini
                t0 = time.time()
                transcript = await _sarvam_stt(pcm8_bytes, session=sarvam_session)
                stt_ms = int((time.time() - t0) * 1000)
                if not transcript:
                    log(f"📝 STT: empty ({stt_ms}ms) — ignoring")
                    return
                log(f"📝 STT ({stt_ms}ms) → {transcript!r}")
                # Re-check after STT: a concurrent task (started before
                # waiting_for_gemini was set) may have already sent a turn.
                # Sending two clientContent turns before Gemini responds
                # triggers 1008 "operation not supported".
                if waiting_for_gemini:
                    log(f"📝 STT concurrent-drop (Gemini busy): {transcript!r}")
                    return
                line = f"[{_ts()}] Customer: {transcript}"
                transcript_log.append(line)
                log(f"🗣  {line}")
                last_customer_ts = time.time()
                # Keyword-extract category/subcategory so [STAGE CONTEXT] is
                # accurate for this turn (e.g. customer said "टू व्हीलर" in
                # their first message → advance past subcategory stage now).
                _update_stage_from_customer(transcript, service_graph)
                stage_ctx = service_graph.get_context()
                full_text = f"{stage_ctx}\n\nCustomer: {transcript}"
                log(f"📋 Stage: {service_graph.current_stage()}")
                try:
                    await g_ws.send(json.dumps({
                        "clientContent": {
                            "turns": [{"role": "user", "parts": [{"text": full_text}]}],
                            "turnComplete": True,
                        }
                    }))
                    waiting_for_gemini = True  # block new utterances until Gemini responds
                    log(f"📤 Gemini send OK (+{int((time.time()-t0)*1000)}ms total)")
                except Exception as send_err:
                    log(f"❌ Gemini WS send failed after STT: {send_err}")

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

                        # ── VAD ───────────────────────────────────────────────
                        rms = audioop.rms(pcm8, 2)

                        if rms >= VAD_SPEECH_THRESHOLD:
                            if not in_speech:
                                in_speech       = True
                                speech_start_ts = now
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
                                asyncio.create_task(_stt_and_send(combined))

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
                                    else:
                                        asyncio.create_task(_stt_and_send(combined))
                                else:
                                    log(f"⏭ Too short ({duration:.2f}s) — ignoring")

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log(f"❌ Vobiz WS error: {ws.exception()}")
                    break

    except Exception as e:
        print(f"[{_ts()}] ❌ Gemini Live Error: {e}", flush=True)
        traceback.print_exc()
    finally:
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
                log(f"🎙️  Recording saved → {wav_path} ({len(pcm8_frames)} frames, {elapsed:.0f}s)")
                gcs_uri = await asyncio.to_thread(upload_recording_to_gcs, wav_path, caller_id)
                if gcs_uri:
                    log(f"☁️  Recording uploaded → {gcs_uri}")
            except Exception as rec_err:
                log(f"⚠️  Recording save failed: {rec_err}")
        log("📋 TRANSCRIPT:\n" + ("\n".join(transcript_log) if transcript_log else "  (empty)"))
        log("📧 Attempting transcript email ...")
        await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
        if not ws.closed:
            await ws.close()

    return ws
