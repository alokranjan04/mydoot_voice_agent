# -*- coding: utf-8 -*-
"""
Gemini Live Multimodal pipeline for Mydoot Customer Care.
Uses websockets library + BidiGenerateContent (v1beta) — audio in / audio out.
"""
import asyncio, audioop, base64, json, traceback
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
            f"\n\n{state_engine.get_prompt_injection()}"
        )

    model = APP_CONFIG.get("parameters", {}).get("google", {}).get(
        "model", "models/gemini-2.5-flash-native-audio-latest"
    )
    print(f"🚀 Gemini Live connecting | model={model} | caller={caller_id}")
    print(f"   API key: {'SET len=' + str(len(GEMINI_API_KEY)) if GEMINI_API_KEY else '*** MISSING ***'}")

    try:
        async with websockets.connect(GEMINI_WS_URL, open_timeout=15) as g_ws:

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
            await g_ws.send(json.dumps({
                "clientContent": {
                    "turns": [{
                        "role": "user",
                        "parts": [{"text": APP_CONFIG["scripts"]["greeting"]}],
                    }],
                    "turnComplete": True,
                }
            }))

            # ── 4. Receive loop: audio + tool calls from Gemini ─────────────
            downsample_state = None

            async def g_receiver():
                nonlocal downsample_state
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

                        # Audio output — convert Gemini's 24kHz PCM → 8kHz mu-law
                        for part in (
                            data.get("serverContent", {})
                                .get("modelTurn", {})
                                .get("parts", [])
                        ):
                            if part.get("inlineData"):
                                pcm24 = base64.b64decode(part["inlineData"]["data"])
                                pcm8, downsample_state = audioop.ratecv(
                                    pcm24, 2, 1, 24000, 8000, downsample_state
                                )
                                pcm8 = audioop.mul(pcm8, 2, 1.4)
                                mulaw = audioop.lin2ulaw(pcm8, 2)
                                if not ws.closed:
                                    await ws.send_str(json.dumps({
                                        "event": "playAudio",
                                        "media": {
                                            "contentType": "audio/x-mulaw",
                                            "sampleRate":  8000,
                                            "payload": base64.b64encode(mulaw).decode("utf-8"),
                                        },
                                    }))

                        # Tool calls
                        tool_call = data.get("toolCall")
                        if tool_call:
                            for fc in tool_call.get("functionCalls", []):
                                fn   = fc.get("name", "")
                                args = fc.get("args", {})
                                cid  = fc.get("id", "")
                                print(f"🔧 Tool call: {fn}({args})")

                                if fn == "save_customer_feedback":
                                    for k in ["customer_name", "company_name",
                                              "product_name", "usage_duration",
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

            asyncio.create_task(g_receiver())

            # ── 5. Forward Vobiz audio → Gemini ─────────────────────────────
            # Vobiz sends mu-law 8kHz; Gemini Live expects PCM 16kHz.
            upsample_state = None
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("event") == "media":
                        raw_mulaw = base64.b64decode(data["media"]["payload"])
                        pcm8  = audioop.ulaw2lin(raw_mulaw, 2)
                        pcm16, upsample_state = audioop.ratecv(
                            pcm8, 2, 1, 8000, 16000, upsample_state
                        )
                        await g_ws.send(json.dumps({
                            "realtimeInput": {
                                "audio": {
                                    "data":     base64.b64encode(pcm16).decode("utf-8"),
                                    "mimeType": "audio/pcm;rate=16000",
                                }
                            }
                        }))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"❌ Vobiz WS error: {ws.exception()}")
                    break

    except Exception as e:
        print(f"❌ Gemini Live Error: {e}")
        traceback.print_exc()
    finally:
        # Send email transcript
        if transcript_log:
            try:
                await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
            except Exception as mail_err:
                print(f"[EMAIL WARN]: {mail_err}")
        if not ws.closed:
            await ws.close()

    return ws
