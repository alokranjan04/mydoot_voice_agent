# -*- coding: utf-8 -*-
"""
Gemini Live Multimodal pipeline for Mydoot Customer Care.
Uses websockets library + BidiGenerateContent (v1beta) — audio in / audio out.
"""
import asyncio, audioop, base64, json, random, time, traceback
from datetime import datetime
import aiohttp
import websockets
from aiohttp import web

from config.settings import APP_CONFIG, GEMINI_API_KEY, GEMINI_WS_URL
from core.state_engine import ConversationStateEngine
from mydoot_functions import FUNCTION_MAP, send_call_summary_email


async def gemini_handler(request):
    ws = web.WebSocketResponse(protocols=["audio.drachtio.org"])
    await ws.prepare(request)

    caller_id      = request.query.get("caller_id", "Unknown")
    state_engine   = ConversationStateEngine()
    transcript_log = []     # ["Agent: ...", "Customer: ..."]

    def get_system_prompt():
        return (
            f"{APP_CONFIG['agent']['system_prompt']}\n\n"
            f"REAL-TIME: {datetime.now().strftime('%I:%M %p')} on {datetime.now().strftime('%A')}."
        )

    model = APP_CONFIG.get("parameters", {}).get("google", {}).get(
        "model", "models/gemini-2.5-flash-native-audio-latest"
    )
    print(f"🚀 Gemini Live connecting | model={model} | caller={caller_id}")
    print(f"   API key: {'SET len=' + str(len(GEMINI_API_KEY)) if GEMINI_API_KEY else '*** MISSING ***'}")

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
                    "realtimeInputConfig": {
                        "automaticActivityDetection": {
                            "disabled": False,
                            # LOW start sensitivity: ignore fan/background noise,
                            # only trigger on clear human speech
                            "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                            # LOW end sensitivity: wait longer before cutting off
                            # the customer (avoids clipping mid-sentence)
                            "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                            # 800ms silence required before declaring end-of-turn
                            "silenceDurationMs": 800,
                            "prefixPaddingMs": 20,
                        }
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
            print("📤 Setup sent — waiting for Gemini confirmation...")

            # ── 2. Wait for setup confirmation ──────────────────────────────
            try:
                raw = await asyncio.wait_for(g_ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise Exception("Gemini setup timed out — check model name and API key")

            resp = json.loads(raw)
            if resp.get("error"):
                raise Exception(f"Gemini setup error: {resp['error']}")
            print(f"✅ Gemini Live Ready: {json.dumps(resp)[:120]}")

            # ── 3. Kick off greeting ─────────────────────────────────────────
            # Send a silent trigger so Gemini speaks its opening greeting.
            # The actual greeting scripts are in the system prompt — Gemini
            # picks one randomly as instructed there.
            await g_ws.send(json.dumps({
                "clientContent": {
                    "turns": [{
                        "role": "user",
                        "parts": [{"text": "[CALL_STARTED]"}],
                    }],
                    "turnComplete": True,
                }
            }))

            # ── 4. Receive loop: audio + tool calls from Gemini ─────────────
            downsample_state   = None
            last_ai_audio_ts   = 0.0   # timestamp of last audio packet from Gemini
            gemini_turn_end_ts = 0.0   # timestamp when Gemini signalled turnComplete

            async def g_receiver():
                nonlocal downsample_state, last_ai_audio_ts, gemini_turn_end_ts
                try:
                    async for raw_msg in g_ws:
                        data = json.loads(raw_msg)

                        # Capture customer speech transcript
                        in_t = data.get("inputAudioTranscription", {})
                        if in_t and in_t.get("text"):
                            transcript_log.append(f"Customer: {in_t['text']}")

                        # Capture agent speech transcript
                        out_t = data.get("outputAudioTranscription", {})
                        if out_t and out_t.get("text"):
                            transcript_log.append(f"Agent: {out_t['text']}")

                        server_content = data.get("serverContent", {})

                        # Audio output — convert Gemini's 24kHz PCM → 8kHz mu-law
                        for part in server_content.get("modelTurn", {}).get("parts", []):
                            if part.get("inlineData"):
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

                        # Gemini signals it finished speaking this turn
                        if server_content.get("turnComplete"):
                            gemini_turn_end_ts = time.time()
                            print("🔔 turnComplete received")

                        # Tool calls
                        tool_call = data.get("toolCall")
                        if tool_call:
                            for fc in tool_call.get("functionCalls", []):
                                fn   = fc.get("name", "")
                                args = fc.get("args", {})
                                cid  = fc.get("id", "")
                                print(f"🔧 Tool call: {fn}({args})")

                                if fn == "save_customer_feedback":
                                    for k in ["customer_name", "brand", "item",
                                              "product_used_since", "usage_duration",
                                              "warranty_status", "complaint"]:
                                        if args.get(k):
                                            state_engine.set_data(k, args[k])
                                    args.setdefault("caller_id", caller_id)

                                if fn in FUNCTION_MAP:
                                    res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)
                                    print(f"🔧 Tool result: {res}")
                                    await g_ws.send(json.dumps({
                                        "toolResponse": {
                                            "functionResponses": [{
                                                "id":       cid,
                                                "name":     fn,
                                                "response": {"result": res},
                                            }]
                                        }
                                    }))

                except Exception as ex:
                    print(f"❌ g_receiver error: {ex}")
                    traceback.print_exc()
                finally:
                    # Release echo guard so the main loop never stays blocked
                    gemini_turn_end_ts = time.time() - 1.0

            asyncio.create_task(g_receiver())

            # ── 5. Forward Vobiz audio → Gemini ─────────────────────────────
            # Vobiz sends mu-law 8kHz; Gemini Live expects PCM 16kHz.
            # ECHO GUARD: only block for a short window after Gemini finishes
            # speaking (clears reverb/echo). Do NOT block during speech —
            # that prevents barge-in and drops the customer's first words.
            # Safety: if no turnComplete for 8s after last AI audio, auto-release.
            upsample_state = None
            call_start_ts  = time.time()
            MAX_CALL_SECS  = 600  # 10 min hard limit — prevents infinite hang

            async for msg in ws:
                # Hard timeout: kill call if it hangs beyond 10 minutes
                if time.time() - call_start_ts > MAX_CALL_SECS:
                    print(f"⏱ Call timeout ({MAX_CALL_SECS}s) — closing.")
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("event") == "media":
                        now = time.time()
                        # Safety: if Gemini sent audio but turnComplete never
                        # arrived (g_receiver died/crashed), unblock after 8s
                        if (last_ai_audio_ts > gemini_turn_end_ts and
                                now - last_ai_audio_ts > 8.0):
                            gemini_turn_end_ts = now - 1.0
                        # Short post-turn buffer: 1.0s after turnComplete
                        if now - gemini_turn_end_ts < 1.0:
                            continue
                        raw_mulaw = base64.b64decode(data["media"]["payload"])
                        pcm8  = audioop.ulaw2lin(raw_mulaw, 2)
                        pcm16, upsample_state = audioop.ratecv(
                            pcm8, 2, 1, 8000, 16000, upsample_state
                        )
                        try:
                            await g_ws.send(json.dumps({
                                "realtimeInput": {
                                    "audio": {
                                        "data":     base64.b64encode(pcm16).decode("utf-8"),
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }))
                        except Exception:
                            break  # Gemini closed — end gracefully
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"❌ Vobiz WS error: {ws.exception()}")
                    break

    except Exception as e:
        print(f"❌ Gemini Live Error: {e}")
        traceback.print_exc()
    finally:
        # Always send transcript email (even if empty — confirms call happened)
        try:
            await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
        except Exception as mail_err:
            print(f"[EMAIL WARN]: {mail_err}")
        if not ws.closed:
            await ws.close()

    return ws
