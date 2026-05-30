# -*- coding: utf-8 -*-
"""
Sarvam pipeline WebSocket handler for Astrology Agent.
"""
import asyncio
import audioop
import base64
import json
import os
import re
import time
import traceback
import wave
from datetime import datetime

import aiohttp
import websockets
from aiohttp import web

from config.settings import (
    APP_CONFIG, SARVAM_API_KEY, DEEPGRAM_API_KEY,
    SARVAM_CHAT_URL, SARVAM_TTS_URL, DG_URL,
)
print("✅ Audioop module loaded successfully.")
ELEVEN_LABS_API_KEY = os.getenv("ELEVEN_LABS_API_KEY", "").strip()

from core.recorder import _TimelineRecorder
from core.hindi_utils import JUNK_RE, SENT_RE, day_to_hindi, time_to_hindi, hindi_to_time, _HI_DAY
from pipelines.http_client import get_http, reset_http
from mydoot_functions import FUNCTION_MAP, send_call_summary_email
from core.state_engine import ConversationStateEngine
from metrics.collector import store, resource_poller, TurnLatency
from metrics.cost_calculator import calculate_cost

# ── API helpers ────────────────────────────────────────────────────────

async def _sarvam_tts(text: str) -> str | None:
    """Convert text to base64 PCM audio via Sarvam Bulbul v2."""
    if not text: return None
    payload = {
        "inputs": [text],
        "target_language_code": "hi-IN",
        "speaker": "amrit",
        "speech_sample_rate": 16000,
        "model": "bulbul:v2",
    }
    try:
        async with get_http().post(
            SARVAM_TTS_URL, json=payload,
            headers={"api-subscription-key": SARVAM_API_KEY},
        ) as r:
            if r.status == 200:
                return (await r.json())["audios"][0]
    except Exception: pass
    return None

async def _eleven_tts(text: str) -> str | None:
    """High-quality Hindi TTS via ElevenLabs."""
    if not ELEVEN_LABS_API_KEY or not text: return None
    url = "https://api.elevenlabs.io/v1/text-to-speech/iP95p4xo8unXCcR7shA8" # Professional Male
    headers = {"xi-api-key": ELEVEN_LABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}
    }
    try:
        async with get_http().post(url, json=payload, headers=headers) as r:
            if r.status == 200:
                return base64.b64encode(await r.read()).decode("utf-8")
    except Exception: pass
    return None

async def _sarvam_stream_once(messages: list):
    """Single streaming attempt against Sarvam 30B."""
    headers   = {"Content-Type": "application/json", "api-subscription-key": SARVAM_API_KEY}
    params    = APP_CONFIG.get("parameters", {}).get("sarvam", {})
    payload   = {
        "model": params.get("model", "sarvam-30b"),
        "messages": messages,
        "tools": APP_CONFIG["tools"]["sarvam"],
        "temperature": params.get("temperature", 0.4),
        "stream": True,
    }
    timeout   = aiohttp.ClientTimeout(total=10, sock_read=10)
    tool_bufs: dict = {}
    try:
        async with get_http().post(
            SARVAM_CHAT_URL, json=payload, headers=headers, timeout=timeout
        ) as r:
            if r.status != 200:
                print(f"❌ Sarvam API Error: {r.status} - {await r.text()}")
                return
            async for raw in r.content:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: "): continue
                s = line[6:]
                if s == "[DONE]": break
                try:
                    chunk = json.loads(s)
                    delta = chunk["choices"][0]["delta"]
                    if delta.get("content"): yield ("text", delta["content"])
                    for tc in delta.get("tool_calls", []):
                        i = tc.get("index", 0)
                        if i not in tool_bufs: tool_bufs[i] = {"id": "", "name": "", "arguments": ""}
                        if tc.get("id"): tool_bufs[i]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"): tool_bufs[i]["name"] = fn["name"]
                        if fn.get("arguments"): tool_bufs[i]["arguments"] += fn["arguments"]
                except Exception: pass
    except Exception as e:
        print(f"[STREAM ERROR] {e}")
        reset_http()
    for i in sorted(tool_bufs):
        buf = tool_bufs[i]
        if buf["name"]:
            yield ("tool", {"id": buf["id"], "type": "function", "function": {"name": buf["name"], "arguments": buf["arguments"]}})

async def _sarvam_stream(messages: list):
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
    caller_id = request.query.get("caller_id", "Unknown")
    tts_provider = request.query.get("tts", "sarvam")
    
    sid, dg_ws = None, None
    is_responding, is_speaking = False, False
    speak_task, partial_hyp, pending_transcript = None, "", None
    call_metrics, poll_task = None, None
    tts_chars = 0
    recorder = _TimelineRecorder()
    call_start_time = time.time()
    state_engine = ConversationStateEngine()

    def get_system_prompt():
        return (
            f"{APP_CONFIG['agent']['system_prompt']}\n\n"
            f"REAL-TIME: {datetime.now().strftime('%I:%M %p')} on {datetime.now().strftime('%A')}."
            f"\n\n{state_engine.get_prompt_injection()}"
        )

    history = [{"role": "system", "content": get_system_prompt()}]

    async def clear_audio():
        if sid and not ws.closed:
            try: await ws.send_str(json.dumps({"event": "clearAudio", "streamId": sid}))
            except Exception: pass

    async def speak(t: str):
        nonlocal is_speaking, tts_chars
        if not t: return
        t = re.sub(r"<[^>]+>", "", t).strip()
        t = JUNK_RE.sub("", t)
        if not t or len(t) < 2: return
        tts_chars += len(t)
        
        audio = None
        if tts_provider == "eleven":
            audio = await _eleven_tts(t)
        if not audio:
            audio = await _sarvam_tts(t)
            
        if audio and sid:
            is_speaking = True
            try:
                print(f"🔮 Acharya Ji: {t}")
                pcm = base64.b64decode(audio)
                if pcm.startswith(b"RIFF"): pcm = pcm[44:]
                recorder.write_priya(pcm)
                if not ws.closed:
                    mulaw = audioop.lin2ulaw(pcm, 2)
                    await ws.send_str(json.dumps({
                        "event": "playAudio", "streamId": sid,
                        "media": {"contentType": "audio/x-mulaw", "sampleRate": 16000, "payload": base64.b64encode(mulaw).decode("utf-8")},
                    }))
                    await asyncio.sleep(len(mulaw) / 16000.0)
            finally: is_speaking = False

    async def handle_transcript(transcript: str):
        nonlocal is_responding, speak_task, pending_transcript
        if is_responding:
            pending_transcript = transcript
            return
        is_responding = True
        try:
            print(f"👤 User: {transcript}")
            history.append({"role": "user", "content": transcript})
            if call_metrics: call_metrics.record_turn("user", transcript)
            state_engine.update_state(transcript)
            history[0]["content"] = get_system_prompt()

            full_text, tool_calls, sent_buf = "", [], ""
            async def flush_sent(s: str):
                nonlocal speak_task
                s = s.strip()
                if s: speak_task = asyncio.create_task(speak(s))

            async for kind, val in _sarvam_stream(history):
                if not is_responding: return
                if kind == "text":
                    full_text += val
                    sent_buf += val
                    parts = SENT_RE.split(sent_buf)
                    for p in parts[:-1]: await flush_sent(p)
                    sent_buf = parts[-1]
                elif kind == "tool": tool_calls.append(val)

            if sent_buf.strip() and not tool_calls: await flush_sent(sent_buf)
            if not full_text and not tool_calls:
                await flush_sent("Ji, batayein.")
                return

            history.append({"role": "assistant", "content": full_text or None})
            if call_metrics and full_text: call_metrics.record_turn("assistant", full_text)

            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    fn, args, cid = tc["function"]["name"], json.loads(tc["function"]["arguments"]), tc.get("id", f"tc_{i}")
                    print(f"🔧 Tool: {fn}({args})")
                    if fn == "save_customer_feedback":
                        for k in ["customer_name", "product_name", "usage_duration", "warranty_status", "complaint"]:
                            if args.get(k): state_engine.set_data(k, args[k])
                        args.setdefault("caller_id", caller_id)
                    res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)
                    history.append({"role": "tool", "tool_call_id": cid, "name": fn, "content": json.dumps(res)})
                
                state_engine.update_state(transcript, full_text)
                history[0]["content"] = get_system_prompt()
                followup, f_buf = "", ""
                async for kind, val in _sarvam_stream(history):
                    if kind == "text":
                        followup += val
                        f_buf += val
                        parts = SENT_RE.split(f_buf)
                        for p in parts[:-1]: await flush_sent(p)
                        f_buf = parts[-1]
                if f_buf.strip(): await flush_sent(f_buf)
                history.append({"role": "assistant", "content": followup or None})
        except Exception as e: print(f"Error in handler: {e}")
        finally:
            is_responding = False
            if pending_transcript:
                pt, pending_transcript = pending_transcript, None
                asyncio.create_task(handle_transcript(pt))

    async def dg_receiver():
        nonlocal partial_hyp
        try:
            async for raw in dg_ws:
                d = json.loads(raw)
                if d.get("type") == "UtteranceEnd":
                    ph = partial_hyp.strip()
                    partial_hyp = ""
                    if ph: asyncio.create_task(handle_transcript(ph))
                    continue
                tr = d.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "").strip()
                if not tr: continue
                if not d.get("is_final"):
                    partial_hyp = tr
                    if (is_speaking or is_responding) and len(tr.split()) >= 2:
                        is_responding = False
                        if speak_task: speak_task.cancel()
                        asyncio.create_task(clear_audio())
                else: asyncio.create_task(handle_transcript(tr))
        except Exception: pass

    dg_ws = None
    try:
        dg_ws = await websockets.connect(DG_URL, additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"})
        asyncio.create_task(dg_receiver())
        print(f"✅ STT Connected (URL: {DG_URL})")
    except Exception as e:
        print(f"❌ STT Failed: {e}")

    try:
        async for msg in ws:
            print(f"📥 Received WS Msg: {msg.type}")
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("event") == "start":
                    sid = data.get("streamSid") or data.get("streamId") or data.get("start", {}).get("streamSid")
                    print(f"🚀 Session Started: {sid}")
                    call_metrics = store.start_call(sid, "sarvam", caller_id)
                    poll_task = asyncio.create_task(resource_poller(call_metrics))
                    asyncio.create_task(speak(APP_CONFIG["scripts"]["greeting"]))
                elif data.get("event") == "media" and sid and dg_ws:
                    raw = base64.b64decode(data["media"]["payload"])
                    await dg_ws.send(raw)
                    recorder.write_caller(audioop.ulaw2lin(raw, 2))
                elif data.get("event") == "heartbeat":
                    pass
            elif msg.type == aiohttp.WSMsgType.ERROR:
                print(f"❌ WS Error: {ws.exception()}")
    except Exception as e:
        print(f"❌ WS Handler Crash: {e}")
        traceback.print_exc()
    finally:
        if sid and call_metrics:
            cost = calculate_cost("sarvam", time.time() - call_start_time, tts_chars=tts_chars)
            recorder.save(f"recordings/{sid[:8]}.wav")
            store.end_call(sid, cost.total_usd)
            asyncio.create_task(asyncio.to_thread(send_call_summary_email, f"Mydoot Customer Care Call: {caller_id}", "Transcript..."))
        if dg_ws: await dg_ws.close()
        if not ws.closed: await ws.close()
    return ws
