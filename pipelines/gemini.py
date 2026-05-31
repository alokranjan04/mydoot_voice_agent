# -*- coding: utf-8 -*-
"""
Gemini Live Multimodal pipeline for Mydoot Customer Care.
Uses websockets library + BidiGenerateContent (v1beta) — audio in / audio out.
"""
import asyncio, audioop, base64, json, time, traceback
from datetime import datetime
import aiohttp
import websockets
from aiohttp import web

from config.settings import APP_CONFIG, GEMINI_API_KEY, GEMINI_WS_URL
from core.state_engine import ConversationStateEngine
from mydoot_functions import FUNCTION_MAP, send_call_summary_email

# ── Audio pipeline tuning ────────────────────────────────────────────────────
# Packets below this RMS are treated as background noise (fan, line hiss, etc.)
# and not forwarded to Gemini. Increase if fan noise still leaks through;
# decrease if soft speech is being filtered out.
NOISE_GATE_RMS     = 400
# Keep forwarding audio for this many seconds after the last speech packet,
# so the tail of each utterance reaches Gemini intact.
SPEECH_TAIL_SECS   = 0.8
# Accumulate this many 20ms frames before sending one Gemini message.
# 4 × 20ms = 80ms chunks → ~12 sends/s instead of 50.
AUDIO_BATCH_FRAMES = 4


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


async def gemini_handler(request):
    ws = web.WebSocketResponse(protocols=["audio.drachtio.org"])
    await ws.prepare(request)

    caller_id      = request.query.get("caller_id", "Unknown")
    state_engine   = ConversationStateEngine()
    transcript_log = []     # ["Agent: ...", "Customer: ..."]

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
                    "inputAudioTranscription":  {},
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
            save_done_ts       = 0.0   # timestamp when save_customer_feedback succeeded
            save_executed      = False  # prevent duplicate save calls per session
            guard_log_ts       = 0.0   # throttle echo-guard log spam
            agent_buf          = ""    # accumulate agent speech chunks per turn
            customer_buf       = ""    # accumulate customer speech chunks per utterance

            async def g_receiver():
                nonlocal downsample_state, last_ai_audio_ts, gemini_turn_end_ts
                nonlocal greeting_started, greeting_done, save_done_ts, save_executed
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
                                if not greeting_started:
                                    greeting_started = True
                                    log("🔊 Greeting audio started streaming to caller")
                                pcm24 = base64.b64decode(part["inlineData"]["data"])
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
                                # First turnComplete after save = confirmation finished
                                # Close call immediately so Gemini can't repeat it
                                log("✅ Confirmation turnComplete — closing call now")
                                asyncio.create_task(_close_after(ws, g_ws, 0.5, log))
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
                                    for k in ["customer_name", "brand", "item",
                                              "product_used_since", "usage_duration",
                                              "warranty_status", "complaint"]:
                                        if args.get(k):
                                            state_engine.set_data(k, args[k])
                                    args.setdefault("caller_id", caller_id)

                                if fn == "save_customer_feedback" and save_executed:
                                    log("⚠️  Duplicate save_customer_feedback call — skipping. "
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
                                    if fn == "save_customer_feedback" and res.get("success"):
                                        save_executed = True
                                        save_done_ts = time.time()
                                        log("🔒 Post-save guard active — blocking audio for 15s")
                                        # Schedule call close after 8s (confirmation takes ~5s)
                                        asyncio.create_task(_close_after(ws, g_ws, 8.0, log))
                                    await g_ws.send(json.dumps({
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id":       cid,
                                                "name":     fn,
                                                "response": {"result": res},
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

            asyncio.create_task(g_receiver())

            # ── 5. Forward Vobiz audio → Gemini ─────────────────────────────
            upsample_state       = None
            call_start_ts        = time.time()
            MAX_CALL_SECS        = 600   # 10 min hard limit
            fwd_count            = 0     # Gemini sends (each = AUDIO_BATCH_FRAMES packets)
            blocked_count        = 0     # packets dropped by echo/greeting/save guards
            noise_blocked_count  = 0     # packets filtered by noise gate
            last_speech_ts       = 0.0   # timestamp of last above-threshold audio packet
            audio_batch          = []    # accumulate frames before sending
            last_stat_ts         = time.time()

            async for msg in ws:
                # Hard timeout
                if time.time() - call_start_ts > MAX_CALL_SECS:
                    log(f"⏱ Call timeout ({MAX_CALL_SECS}s) — closing.")
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("event") == "media":
                        now = time.time()

                        # Block audio for 15s after save so confirmation plays uninterrupted.
                        if save_done_ts and now - save_done_ts < 15.0:
                            blocked_count += 1
                            if now - guard_log_ts > 3.0:
                                guard_log_ts = now
                                log(f"🔒 Post-save guard — {15.0 - (now - save_done_ts):.0f}s remaining")
                            continue

                        # Block ALL audio until greeting turnComplete fires.
                        # This prevents background noise from interrupting the greeting.
                        # Safety: if greeting turnComplete never arrives within 20s,
                        # force-release so the call doesn't hang forever.
                        if not greeting_done:
                            if now - call_start_ts > 20.0:
                                log("⚠️  Greeting turnComplete missing after 20s — force-releasing")
                                greeting_done = True
                            else:
                                blocked_count += 1
                                if now - guard_log_ts > 3.0:
                                    guard_log_ts = now
                                    log(f"🔇 Greeting in progress — blocking customer audio "
                                        f"| blocked={blocked_count} pkts so far")
                                continue

                        # Safety: if Gemini sent audio but turnComplete never
                        # arrived, unblock after 8s
                        if (last_ai_audio_ts > gemini_turn_end_ts and
                                now - last_ai_audio_ts > 8.0):
                            log(f"⚠️  Echo guard safety timeout — forcing release "
                                f"(turnComplete missing for {now - last_ai_audio_ts:.1f}s)")
                            gemini_turn_end_ts = now - 1.0

                        # Echo guard: 0.5s buffer after turnComplete
                        guard_active = now - gemini_turn_end_ts < 0.5
                        if guard_active:
                            blocked_count += 1
                            continue

                        raw_mulaw = base64.b64decode(data["media"]["payload"])
                        pcm8  = audioop.ulaw2lin(raw_mulaw, 2)
                        pcm16, upsample_state = audioop.ratecv(
                            pcm8, 2, 1, 8000, 16000, upsample_state
                        )

                        # ── Noise gate: filter fan/background noise ──────────
                        # Only forward audio that exceeds the speech RMS threshold,
                        # or falls within SPEECH_TAIL_SECS after the last speech
                        # packet (so the end of each utterance reaches Gemini).
                        rms = audioop.rms(pcm16, 2)
                        is_speech = rms > NOISE_GATE_RMS
                        speech_tail_ok = (now - last_speech_ts) < SPEECH_TAIL_SECS
                        if is_speech:
                            last_speech_ts = now
                        if not is_speech and not speech_tail_ok:
                            # Flush any partially-accumulated batch so Gemini
                            # receives the complete tail of the last utterance.
                            if audio_batch:
                                combined = b"".join(audio_batch)
                                audio_batch.clear()
                                try:
                                    await g_ws.send(json.dumps({
                                        "realtimeInput": {"audio": {
                                            "data":     base64.b64encode(combined).decode("utf-8"),
                                            "mimeType": "audio/pcm;rate=16000",
                                        }}
                                    }))
                                    fwd_count += 1
                                except Exception:
                                    log("❌ Gemini WS closed mid-send — ending call")
                                    break
                            noise_blocked_count += 1
                            if now - guard_log_ts > 5.0:
                                guard_log_ts = now
                                log(f"🔇 Noise gate — rms={rms} < {NOISE_GATE_RMS} | "
                                    f"{noise_blocked_count} pkts filtered so far")
                            continue

                        # ── Batch 4 frames (80ms) before sending to Gemini ───
                        audio_batch.append(pcm16)
                        if len(audio_batch) < AUDIO_BATCH_FRAMES:
                            continue

                        combined = b"".join(audio_batch)
                        audio_batch.clear()
                        try:
                            await g_ws.send(json.dumps({
                                "realtimeInput": {
                                    "audio": {
                                        "data":     base64.b64encode(combined).decode("utf-8"),
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }))
                            fwd_count += 1
                        except Exception:
                            log("❌ Gemini WS closed mid-send — ending call")
                            break

                        # Periodic stats log every 10s
                        if now - last_stat_ts > 10.0:
                            last_stat_ts = now
                            elapsed = now - call_start_ts
                            log(f"📊 Stats @{elapsed:.0f}s — "
                                f"sends={fwd_count} (80ms/ea) | "
                                f"guard_blocked={blocked_count} | "
                                f"noise_filtered={noise_blocked_count} | "
                                f"transcript={len(transcript_log)} lines")

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log(f"❌ Vobiz WS error: {ws.exception()}")
                    break

    except Exception as e:
        print(f"[{_ts()}] ❌ Gemini Live Error: {e}", flush=True)
        traceback.print_exc()
    finally:
        elapsed = time.time() - (call_start_ts if 'call_start_ts' in dir() else time.time())
        log(f"📞 Call ended | duration={elapsed:.1f}s | transcript={len(transcript_log)} lines")
        log("📋 TRANSCRIPT:\n" + ("\n".join(transcript_log) if transcript_log else "  (empty)"))
        log("📧 Attempting transcript email ...")
        await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
        if not ws.closed:
            await ws.close()

    return ws
