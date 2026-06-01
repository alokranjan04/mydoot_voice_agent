# -*- coding: utf-8 -*-
"""
Sarvam pipeline WebSocket handler for Mydoot Customer Care.

ASR  : Sarvam Saaras v3  (REST API, local VAD-based chunking)
LLM  : Sarvam 30B        (streaming text)
TTS  : Sarvam Bulbul v2  (8 kHz mu-law audio)
"""
import asyncio
import audioop
import base64
import io
import json
import os
import random
import re
import time
import traceback
import wave
from datetime import datetime

import aiohttp
from aiohttp import web

from config.settings import APP_CONFIG, SARVAM_API_KEY, SARVAM_CHAT_URL, SARVAM_TTS_URL
from core.hindi_utils import JUNK_RE, SENT_RE
from core.recorder import _TimelineRecorder
from core.state_engine import ConversationStateEngine
from metrics.collector import store, resource_poller
from metrics.cost_calculator import calculate_cost
from mydoot_functions import FUNCTION_MAP, send_call_summary_email
from pipelines.http_client import get_http, reset_http

ELEVEN_LABS_API_KEY = os.getenv("ELEVEN_LABS_API_KEY", "").strip()

# Sarvam Saaras v3 STT endpoint
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"

# ── VAD tuning ────────────────────────────────────────────────────────────────
# Packets with RMS below this are treated as silence (background noise).
VAD_SPEECH_THRESHOLD   = int(os.getenv("VAD_SPEECH_THRESHOLD", "100"))
# Silence after speech (seconds) that marks end-of-utterance.
VAD_END_SECS           = float(os.getenv("VAD_END_SECS", "0.7"))
# Minimum speech duration (seconds) to bother sending to STT.
VAD_MIN_SPEECH_SECS    = float(os.getenv("VAD_MIN_SPEECH_SECS", "0.3"))
# Hard ceiling on a single utterance before forcing a STT call.
VAD_MAX_SPEECH_SECS    = float(os.getenv("VAD_MAX_SPEECH_SECS", "30.0"))


# ── API helpers ───────────────────────────────────────────────────────────────

async def _sarvam_stt(pcm8_bytes: bytes) -> str:
    """
    Transcribe 8 kHz 16-bit mono PCM via Sarvam Saaras v3.
    Returns the transcript string, or "" on failure.
    """
    if not pcm8_bytes or not SARVAM_API_KEY:
        return ""
    try:
        # Wrap raw PCM in a minimal WAV container so Sarvam accepts it.
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
        async with get_http().post(
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
    except Exception as e:
        print(f"[STT ERROR] {e}")
    return ""


async def _sarvam_tts(text: str) -> str | None:
    """Convert text to base64 PCM audio via Sarvam Bulbul v2 at 8 kHz."""
    if not text:
        return None
    payload = {
        "inputs": [text],
        "target_language_code": "hi-IN",
        "speaker": "amrit",
        "speech_sample_rate": 8000,
        "model": "bulbul:v2",
    }
    try:
        async with get_http().post(
            SARVAM_TTS_URL,
            json=payload,
            headers={"api-subscription-key": SARVAM_API_KEY},
        ) as r:
            if r.status == 200:
                return (await r.json())["audios"][0]
    except Exception:
        pass
    return None


async def _eleven_tts(text: str) -> str | None:
    """High-quality Hindi TTS via ElevenLabs (optional fallback)."""
    if not ELEVEN_LABS_API_KEY or not text:
        return None
    url = "https://api.elevenlabs.io/v1/text-to-speech/iP95p4xo8unXCcR7shA8"
    headers = {"xi-api-key": ELEVEN_LABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
    }
    try:
        async with get_http().post(url, json=payload, headers=headers) as r:
            if r.status == 200:
                return base64.b64encode(await r.read()).decode("utf-8")
    except Exception:
        pass
    return None


async def _sarvam_stream_once(messages: list):
    """Single streaming attempt against Sarvam 30B."""
    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": SARVAM_API_KEY,
    }
    params  = APP_CONFIG.get("parameters", {}).get("sarvam", {})
    payload = {
        "model":       params.get("model", "sarvam-30b"),
        "messages":    messages,
        "tools":       APP_CONFIG["tools"]["sarvam"],
        "temperature": params.get("temperature", 0.4),
        "stream":      True,
    }
    timeout   = aiohttp.ClientTimeout(total=10, sock_read=10)
    tool_bufs: dict = {}
    try:
        async with get_http().post(
            SARVAM_CHAT_URL, json=payload, headers=headers, timeout=timeout
        ) as r:
            if r.status != 200:
                print(f"[LLM] Sarvam error {r.status}: {await r.text()}")
                return
            async for raw in r.content:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                s = line[6:]
                if s == "[DONE]":
                    break
                try:
                    chunk = json.loads(s)
                    delta = chunk["choices"][0]["delta"]
                    if delta.get("content"):
                        yield ("text", delta["content"])
                    for tc in delta.get("tool_calls", []):
                        i = tc.get("index", 0)
                        if i not in tool_bufs:
                            tool_bufs[i] = {"id": "", "name": "", "arguments": ""}
                        if tc.get("id"):
                            tool_bufs[i]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            tool_bufs[i]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_bufs[i]["arguments"] += fn["arguments"]
                except Exception:
                    pass
    except Exception as e:
        print(f"[STREAM ERROR] {e}")
        reset_http()
    for i in sorted(tool_bufs):
        buf = tool_bufs[i]
        if buf["name"]:
            yield ("tool", {
                "id": buf["id"],
                "type": "function",
                "function": {"name": buf["name"], "arguments": buf["arguments"]},
            })


async def _sarvam_stream(messages: list):
    """Stream with one automatic retry on empty response."""
    had_output = False
    async for kind, val in _sarvam_stream_once(messages):
        had_output = True
        yield kind, val
    if not had_output:
        async for kind, val in _sarvam_stream_once(messages):
            yield kind, val


# ── Main handler ──────────────────────────────────────────────────────────────

async def sarvam_handler(request):
    ws = web.WebSocketResponse(protocols=["audio.drachtio.org"])
    await ws.prepare(request)

    caller_id    = request.query.get("caller_id", "Unknown")
    tts_provider = request.query.get("tts", "sarvam")

    sid             = None
    is_responding   = False
    is_speaking     = False
    speak_task      = None
    pending_transcript = None
    save_executed   = False
    call_metrics    = None
    poll_task       = None
    tts_chars       = 0
    recorder        = _TimelineRecorder()
    call_start_time = time.time()
    state_engine    = ConversationStateEngine()
    transcript_log  = []

    # VAD state
    speech_buf      = []      # accumulated 8 kHz PCM bytes during utterance
    speech_start_ts = 0.0     # time first speech packet arrived in this utterance
    last_speech_ts  = 0.0     # time of last above-threshold packet
    in_speech       = False   # True while an utterance is in progress

    def log(msg):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] caller={caller_id} | {msg}",
              flush=True)

    def get_system_prompt():
        return (
            f"{APP_CONFIG['agent']['system_prompt']}\n\n"
            f"REAL-TIME: {datetime.now().strftime('%I:%M %p')} on {datetime.now().strftime('%A')}."
            f"\n\n{state_engine.get_prompt_injection()}"
        )

    history = [{"role": "system", "content": get_system_prompt()}]

    async def clear_audio():
        if sid and not ws.closed:
            try:
                await ws.send_str(json.dumps({"event": "clearAudio", "streamId": sid}))
            except Exception:
                pass

    async def speak(t: str):
        nonlocal is_speaking, tts_chars
        if not t:
            return
        t = re.sub(r"<[^>]+>", "", t).strip()
        t = JUNK_RE.sub("", t)
        if not t or len(t) < 2:
            return
        tts_chars += len(t)

        audio = None
        if tts_provider == "eleven":
            audio = await _eleven_tts(t)
        if not audio:
            audio = await _sarvam_tts(t)

        if audio and sid:
            is_speaking = True
            try:
                log(f"🤖 Agent: {t}")
                transcript_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Agent: {t}")
                pcm = base64.b64decode(audio)
                # Strip WAV header if present
                if pcm.startswith(b"RIFF"):
                    pcm = pcm[44:]
                recorder.write_priya(pcm)
                if not ws.closed:
                    mulaw = audioop.lin2ulaw(pcm, 2)
                    await ws.send_str(json.dumps({
                        "event": "playAudio",
                        "streamId": sid,
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate":  8000,
                            "payload": base64.b64encode(mulaw).decode("utf-8"),
                        },
                    }))
                    # Wait for audio to finish (8 kHz → 8000 bytes/sec)
                    await asyncio.sleep(len(mulaw) / 8000.0)
            finally:
                is_speaking = False

    async def handle_transcript(transcript: str):
        nonlocal is_responding, speak_task, pending_transcript, save_executed

        if is_responding:
            pending_transcript = transcript
            return
        is_responding = True
        try:
            log(f"👤 Customer: {transcript}")
            transcript_log.append(
                f"[{datetime.now().strftime('%H:%M:%S')}] Customer: {transcript}"
            )
            history.append({"role": "user", "content": transcript})
            state_engine.update_state(transcript)
            history[0]["content"] = get_system_prompt()

            full_text, tool_calls, sent_buf = "", [], ""

            async def flush_sent(s: str):
                nonlocal speak_task
                s = s.strip()
                if s:
                    speak_task = asyncio.create_task(speak(s))
                    await speak_task  # speak sentences sequentially for correct ordering

            async for kind, val in _sarvam_stream(history):
                if not is_responding:
                    return
                if kind == "text":
                    full_text += val
                    sent_buf  += val
                    parts = SENT_RE.split(sent_buf)
                    for p in parts[:-1]:
                        await flush_sent(p)
                    sent_buf = parts[-1]
                elif kind == "tool":
                    tool_calls.append(val)

            if sent_buf.strip() and not tool_calls:
                await flush_sent(sent_buf)
            if not full_text and not tool_calls:
                await flush_sent("Ji, batayein.")
                return

            history.append({"role": "assistant", "content": full_text or None})

            if tool_calls:
                save_succeeded = False
                for i, tc in enumerate(tool_calls):
                    fn  = tc["function"]["name"]
                    cid = tc.get("id", f"tc_{i}")
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        args = {}
                    log(f"🔧 Tool call: {fn} args={json.dumps(args)}")

                    if fn == "save_customer_feedback":
                        # Normalize warranty_status
                        w = args.get("warranty_status", "")
                        if w in ("1", "yes", "Yes", "YES", "haan", "ha", "ha ji"):
                            args["warranty_status"] = "Yes - Under Warranty"
                        elif w in ("2", "no", "No", "NO", "nahi", "nahin", "nahi ji"):
                            args["warranty_status"] = "No - Out of Warranty"
                        elif w in ("3", "pata nahi", "don't know", "dont know",
                                   "unknown", "not sure", "nahi pata"):
                            args["warranty_status"] = "Customer Does Not Know"
                        for k in ["customer_name", "brand", "item", "product_used_since",
                                  "usage_duration", "warranty_status", "complaint"]:
                            if args.get(k):
                                state_engine.set_data(k, args[k])
                        args.setdefault("caller_id", caller_id)

                        if save_executed:
                            log("⚠️  Duplicate save_customer_feedback — skipping")
                            res = {"success": True}
                            history.append({
                                "role": "tool", "tool_call_id": cid,
                                "name": fn, "content": json.dumps(res),
                            })
                            continue

                    if fn in FUNCTION_MAP:
                        res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)
                        log(f"🔧 Tool result: {res}")
                        if fn == "save_customer_feedback" and res.get("success"):
                            save_executed  = True
                            save_succeeded = True
                    else:
                        res = {"error": f"Unknown tool: {fn}"}
                    history.append({
                        "role": "tool", "tool_call_id": cid,
                        "name": fn, "content": json.dumps(res),
                    })

                # Generate and speak confirmation / follow-up
                state_engine.update_state(transcript, full_text)
                history[0]["content"] = get_system_prompt()
                followup, f_buf = "", ""
                async for kind, val in _sarvam_stream(history):
                    if kind == "text":
                        followup += val
                        f_buf    += val
                        parts = SENT_RE.split(f_buf)
                        for p in parts[:-1]:
                            await flush_sent(p)
                        f_buf = parts[-1]
                if f_buf.strip():
                    await flush_sent(f_buf)
                history.append({"role": "assistant", "content": followup or None})

                # Close the call after the confirmation finishes playing
                if save_succeeded:
                    log("📴 Save successful — closing call after confirmation")
                    await asyncio.sleep(0.5)
                    if not ws.closed:
                        await ws.close()

        except Exception as e:
            log(f"❌ handle_transcript error: {e}")
            traceback.print_exc()
        finally:
            is_responding = False
            if pending_transcript:
                pt, pending_transcript = pending_transcript, None
                asyncio.create_task(handle_transcript(pt))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)

                # ── Session start ──────────────────────────────────────────
                if data.get("event") == "start":
                    sid = (data.get("streamSid")
                           or data.get("streamId")
                           or data.get("start", {}).get("streamSid"))
                    log(f"🚀 Session started: sid={sid}")
                    call_metrics = store.start_call(sid, "sarvam", caller_id)
                    poll_task    = asyncio.create_task(resource_poller(call_metrics))
                    greeting     = random.choice(APP_CONFIG["scripts"]["greetings"])
                    asyncio.create_task(speak(greeting))

                # ── Inbound audio from Vobiz ───────────────────────────────
                elif data.get("event") == "media" and sid:
                    now = time.time()
                    raw  = base64.b64decode(data["media"]["payload"])
                    pcm8 = audioop.ulaw2lin(raw, 2)
                    recorder.write_caller(pcm8)
                    rms  = audioop.rms(pcm8, 2)

                    # Skip audio while agent is speaking (prevent echo)
                    if is_speaking:
                        continue

                    if rms >= VAD_SPEECH_THRESHOLD:
                        # ── Speech packet ──────────────────────────────────
                        if not in_speech:
                            in_speech       = True
                            speech_start_ts = now
                            speech_buf.clear()
                            log(f"🎙 Speech start (rms={rms})")
                        speech_buf.append(pcm8)
                        last_speech_ts = now

                        # Force-flush if utterance exceeds max duration
                        if now - speech_start_ts >= VAD_MAX_SPEECH_SECS:
                            combined     = b"".join(speech_buf)
                            speech_buf.clear()
                            in_speech    = False
                            log(f"🔁 Utterance max duration hit — flushing {len(combined)} bytes")
                            asyncio.create_task(_dispatch_stt(combined, handle_transcript, log))

                    elif in_speech:
                        # ── Silence after speech ───────────────────────────
                        # Include silence tail so STT gets natural end-of-phrase
                        speech_buf.append(pcm8)

                        if now - last_speech_ts >= VAD_END_SECS:
                            combined     = b"".join(speech_buf)
                            speech_buf.clear()
                            in_speech    = False
                            duration     = now - speech_start_ts
                            log(f"🔇 Speech end — {duration:.2f}s, {len(combined)} bytes")
                            if duration >= VAD_MIN_SPEECH_SECS:
                                asyncio.create_task(_dispatch_stt(combined, handle_transcript, log))
                            else:
                                log(f"⏭ Too short ({duration:.2f}s < {VAD_MIN_SPEECH_SECS}s) — ignoring")

                elif data.get("event") == "heartbeat":
                    pass

            elif msg.type == aiohttp.WSMsgType.ERROR:
                log(f"❌ WS error: {ws.exception()}")
                break

    except Exception as e:
        log(f"❌ WS handler crash: {e}")
        traceback.print_exc()
    finally:
        if sid and call_metrics:
            cost = calculate_cost("sarvam", time.time() - call_start_time, tts_chars=tts_chars)
            recorder.save(f"recordings/{sid[:8]}.wav")
            store.end_call(sid, cost.total_usd)
        log("📋 TRANSCRIPT:\n" + ("\n".join(transcript_log) if transcript_log else "  (empty)"))
        log("📧 Sending transcript email...")
        await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
        if not ws.closed:
            await ws.close()
    return ws


async def _dispatch_stt(pcm8_bytes: bytes, handle_fn, log_fn):
    """Transcribe audio via Sarvam Saaras v3, dispatch to handle_fn if non-empty."""
    log_fn(f"📡 Sending {len(pcm8_bytes)} bytes to Sarvam Saaras v3...")
    transcript = await _sarvam_stt(pcm8_bytes)
    if transcript:
        log_fn(f"📝 STT: {transcript!r}")
        await handle_fn(transcript)
    else:
        log_fn("📝 STT: empty result — ignoring")
