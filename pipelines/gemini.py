# -*- coding: utf-8 -*-
"""
Gemini Live Multimodal pipeline for Mydoot Customer Care.
Uses Gemini BidiGenerateContent (v1beta) — audio in / audio out, no STT/TTS needed.
"""
import asyncio, base64, json, traceback
import audioop
from datetime import datetime
import aiohttp
from aiohttp import web

from config.settings import APP_CONFIG, GEMINI_API_KEY, GEMINI_WS_URL
from core.state_engine import ConversationStateEngine
from mydoot_functions import FUNCTION_MAP


async def gemini_handler(request):
    ws = web.WebSocketResponse(protocols=["audio.drachtio.org"])
    await ws.prepare(request)

    caller_id    = request.query.get("caller_id", "Unknown")
    state_engine = ConversationStateEngine()

    def get_system_prompt():
        return (
            f"{APP_CONFIG['agent']['system_prompt']}\n\n"
            f"REAL-TIME: {datetime.now().strftime('%I:%M %p')} on {datetime.now().strftime('%A')}."
            f"\n\n{state_engine.get_prompt_injection()}"
        )

    model = APP_CONFIG.get("parameters", {}).get("google", {}).get(
        "model", "models/gemini-2.0-flash-live-001"
    )
    print(f"🚀 Gemini Live connecting | model={model} | caller={caller_id}")
    print(f"   WS URL: {GEMINI_WS_URL[:80]}...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(GEMINI_WS_URL) as g_ws:

                # ── 1. Setup ────────────────────────────────────────────────
                setup = {
                    "setup": {
                        "model": model,
                        "generation_config": {
                            "response_modalities": ["AUDIO"],
                        },
                        "system_instruction": {
                            "role": "system",
                            "parts": [{"text": get_system_prompt()}],
                        },
                        "tools": APP_CONFIG["tools"]["gemini"],
                    }
                }
                await g_ws.send_json(setup)
                print("📤 Setup sent — waiting for Gemini confirmation...")

                # ── 2. Wait for setup confirmation (10s timeout) ─────────────
                try:
                    setup_msg = await asyncio.wait_for(g_ws.receive(), timeout=10.0)
                except asyncio.TimeoutError:
                    raise Exception("Gemini setup confirmation timed out after 10s — check model name and API key")

                if setup_msg.type == aiohttp.WSMsgType.TEXT:
                    resp = json.loads(setup_msg.data)
                    print(f"📥 Setup response: {json.dumps(resp)[:300]}")
                    if "error" in resp:
                        raise Exception(f"Gemini setup error: {resp['error']}")
                elif setup_msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    raise Exception(f"Gemini closed connection during setup: {setup_msg.data}")

                print("✅ Gemini Live Ready")

                # ── 3. Kick off greeting ─────────────────────────────────────
                await g_ws.send_json({
                    "client_content": {
                        "turns": [{
                            "role": "user",
                            "parts": [{"text": APP_CONFIG["scripts"]["greeting"]}],
                        }],
                        "turn_complete": True,
                    }
                })

                # ── 4. Receive loop: audio + tool calls from Gemini ──────────
                async def g_receiver():
                    try:
                        async for msg in g_ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                print(f"⚠️ Gemini WS non-text msg: {msg.type}")
                                break
                            data = json.loads(msg.data)

                            # Audio
                            for part in (
                                data.get("serverContent", {})
                                    .get("modelTurn", {})
                                    .get("parts", [])
                            ):
                                if part.get("inlineData"):
                                    await ws.send_str(json.dumps({
                                        "event": "playAudio",
                                        "media": {
                                            "payload":    part["inlineData"]["data"],
                                            "sampleRate": 24000,
                                        },
                                    }))

                            # Tool calls
                            for fc in data.get("toolCall", {}).get("functionCalls", []):
                                fn   = fc.get("name", "")
                                args = fc.get("args", {})
                                cid  = fc.get("id", "")
                                print(f"🔧 Tool call: {fn}({args})")

                                if fn == "save_customer_feedback":
                                    for k in ["customer_name", "product_name",
                                              "usage_duration", "warranty_status", "complaint"]:
                                        if args.get(k):
                                            state_engine.set_data(k, args[k])
                                    args.setdefault("caller_id", caller_id)

                                if fn in FUNCTION_MAP:
                                    res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)
                                    await g_ws.send_json({
                                        "tool_response": {
                                            "function_responses": [{
                                                "id":       cid,
                                                "name":     fn,
                                                "response": {"result": res},
                                            }]
                                        }
                                    })

                    except Exception as ex:
                        print(f"❌ g_receiver error: {ex}")

                asyncio.create_task(g_receiver())

                # ── 5. Forward Vobiz audio → Gemini ─────────────────────────
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("event") == "media":
                            raw_mulaw = base64.b64decode(data["media"]["payload"])
                            pcm       = audioop.ulaw2lin(raw_mulaw, 2)
                            await g_ws.send_json({
                                "realtime_input": {
                                    "media_chunks": [{
                                        "data":      base64.b64encode(pcm).decode("utf-8"),
                                        "mime_type": "audio/pcm",
                                    }]
                                }
                            })
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        print(f"❌ Vobiz WS error: {ws.exception()}")
                        break

    except Exception as e:
        print(f"❌ Gemini Live Error: {e}")
        traceback.print_exc()
    finally:
        if not ws.closed:
            await ws.close()

    return ws
