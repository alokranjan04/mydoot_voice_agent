# -*- coding: utf-8 -*-
"""
Gemini Live Multimodal pipeline for Acharya Ji.
"""
import asyncio, base64, json, os, time, traceback
from datetime import datetime
import aiohttp
from aiohttp import web
from config.settings import APP_CONFIG, GEMINI_API_KEY, GEMINI_WS_URL
from core.state_engine import ConversationStateEngine
from mydoot_functions import FUNCTION_MAP

async def gemini_handler(request):
    ws = web.WebSocketResponse(protocols=["audio.drachtio.org"])
    await ws.prepare(request)
    
    caller_id = request.query.get("caller_id", "Unknown")
    state_engine = ConversationStateEngine()

    def get_system_prompt():
        return (
            f"{APP_CONFIG['agent']['system_prompt']}\n\n"
            f"REAL-TIME: {datetime.now().strftime('%I:%M %p')} on {datetime.now().strftime('%A')}."
            f"\n\n{state_engine.get_prompt_injection()}"
        )

    # Gemini expects models/gemini-1.5-flash-8b or models/gemini-1.5-flash
    model = "models/gemini-1.5-flash"
    url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"
    
    print(f"🚀 Connecting to Gemini Live ({model})...")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as g_ws:
                # 1. Setup message
                setup = {
                    "setup": {
                        "model": model,
                        "generation_config": {"response_modalities": ["AUDIO"]},
                        "system_instruction": {"role": "system", "parts": [{"text": get_system_prompt()}]}
                    }
                }
                await g_ws.send_json(setup)
                
                # Receive setup confirmation
                setup_resp = await g_ws.recv()
                print(f"✅ Gemini Live Ready")

                # Start greeting
                greeting = {"client_content": {"turns": [{"role": "user", "parts": [{"text": APP_CONFIG["scripts"]["greeting"]}]}], "turn_complete": True}}
                await g_ws.send_json(greeting)

                async def g_receiver():
                    try:
                        async for msg in g_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                # Audio response
                                for part in data.get("serverContent", {}).get("modelTurn", {}).get("parts", []):
                                    if part.get("inlineData"):
                                        # Gemini returns 24kHz PCM, but Voice Lab expects 16kHz Mu-law
                                        # We'll send it as is and let Voice Lab handle the 24kHz float
                                        await ws.send_str(json.dumps({
                                            "event": "playAudio",
                                            "media": {"payload": part["inlineData"]["data"], "sampleRate": 24000}
                                        }))
                                
                                # Tool calls
                                for tc in data.get("serverContent", {}).get("modelTurn", {}).get("toolCalls", []):
                                    fn, args, cid = tc["name"], tc["args"], tc["callId"]
                                    print(f"🔧 Gemini Tool: {fn}({args})")
                                    res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)
                                    # Send tool response back
                                    tool_res = {"tool_response": {"function_responses": [{"name": fn, "response": {"result": res}, "id": cid}]}}
                                    await g_ws.send_json(tool_res)

                            elif msg.type == aiohttp.WSMsgType.ERROR: break
                    except Exception: pass

                asyncio.create_task(g_receiver())

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("event") == "media":
                            # Gemini Live expects raw PCM16 at 16kHz
                            # Our Voice Lab sends Mu-law 16kHz, so we must decode it back
                            import audioop
                            raw_mulaw = base64.b64decode(data["media"]["payload"])
                            pcm = audioop.ulaw2lin(raw_mulaw, 2)
                            audio_msg = {"realtime_input": {"media_chunks": [{"data": base64.b64encode(pcm).decode("utf-8"), "mime_type": "audio/pcm"}]}}
                            await g_ws.send_json(audio_msg)
    except Exception as e:
        print(f"❌ Gemini Live Error: {e}")
        traceback.print_exc()
    finally:
        if not ws.closed: await ws.close()
    return ws
