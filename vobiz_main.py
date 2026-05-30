import asyncio
import base64
import json
import os
import time
import websockets
import requests
import audioop
import traceback
from aiohttp import web
import aiohttp
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pharmacy_functions import FUNCTION_MAP, send_call_summary_email, update_booking_sheet, book_appointment

load_dotenv()

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

def load_app_config():
    try:
        with open('app_config.json', 'r') as f:
            return json.load(f)
    except:
        return {"agent": {"system_prompt": "You are Priya, a professional clinic assistant."}, "analytics": {"summary_prompt": ""}}

APP_CONFIG = load_app_config()
SYSTEM_PROMPT = APP_CONFIG["agent"]["system_prompt"]
GREETING = APP_CONFIG.get("scripts", {}).get("greeting", "Namaste! Main Priya hoon. Kaise madad kar sakti hoon?")

# Strict Gemini Multimodal Live Tools
TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "check_available_slots",
                "description": "Check available appointment slots for Monday-Saturday.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "preferred_day": {"type": "STRING"}
                    },
                    "required": ["preferred_day"]
                }
            },
            {
                "name": "book_appointment",
                "description": "Book a doctor appointment. Call this after collecting child name, age, reason, and preferred time slot. contact_number and parent_name are filled automatically.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_name": {"type": "STRING", "description": "Child's name"},
                        "patient_age": {"type": "STRING", "description": "Child's age"},
                        "parent_name": {"type": "STRING", "description": "Parent name, use child name if unknown"},
                        "contact_number": {"type": "STRING", "description": "Caller phone number, use the patient number from context"},
                        "preferred_day": {"type": "STRING", "description": "Day of appointment e.g. Monday"},
                        "preferred_time": {"type": "STRING", "description": "Time slot e.g. 10:00 AM"},
                        "reason": {"type": "STRING", "description": "Reason for visit"}
                    },
                    "required": ["patient_name", "patient_age", "preferred_day", "preferred_time", "reason"]
                }
            },
            {
                "name": "cancel_appointment",
                "description": "Cancel an existing appointment for a patient.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_name": {"type": "STRING", "description": "Child's name"},
                        "contact_number": {"type": "STRING", "description": "Caller phone number from context"}
                    },
                    "required": ["patient_name"]
                }
            },
            {
                "name": "reschedule_appointment",
                "description": "Reschedule an existing appointment to a new day and time.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_name": {"type": "STRING", "description": "Child's name"},
                        "contact_number": {"type": "STRING", "description": "Caller phone number from context"},
                        "new_day": {"type": "STRING", "description": "New preferred day e.g. Monday"},
                        "new_time": {"type": "STRING", "description": "New time slot e.g. 10:00 AM"}
                    },
                    "required": ["patient_name", "new_day", "new_time"]
                }
            }
        ]
    }
]

async def run_startup_diagnostics():
    """Forces an integration check as soon as the server starts."""
    print("\n" + "╔" + "═"*58 + "╗")
    print("║" + " "*14 + "🚀 STARTUP SYSTEM INTEGRITY CHECK" + " "*12 + "║")
    print("╚" + "═"*58 + "╝")
    test_id = int(time.time())
    
    try:
        # 1. Test Digital Logic
        print("[DIAGNOSTIC]: Testing Tool Chain...")
        test_day = (datetime.now() + timedelta(days=2)).strftime("%A")
        booking = book_appointment(
            patient_name=f"STARTUP_TEST_{test_id}", 
            patient_age="5 Years", parent_name="MOCK_PARENT", contact_number="9999999999",
            preferred_day=test_day, preferred_time="10:00 AM", reason="System Startup Verification"
        )
        print(f"✅ BOOKING RESULT: {booking.get('message', 'Check logs')}")
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR: {e}")
    
    print("═"*60 + "\n")

async def handle_answer(request):
    """Answer the call and extract Caller ID from Vobiz POST body."""
    try:
        post_data = await request.post()
        raw_num = post_data.get("From") or post_data.get("CallerName") or "Unknown"
        caller_id = str(raw_num).replace("+", "").strip()
        if "sip:" in caller_id: caller_id = caller_id.split("sip:")[1].split("@")[0]
        
        host = request.headers.get("X-Forwarded-Host") or request.host
        ws_url = f"wss://{host}/vobiz-stream?caller_id={caller_id}"
        
        xml_response = f'<?xml version="1.0" encoding="UTF-8"?><Response><Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">{ws_url}</Stream></Response>'
        print(f"\n[INCOMING] -> Caller ID: {caller_id}")
        return web.Response(text=xml_response, content_type='text/xml')
    except Exception:
        return web.Response(text="Error", status=500)

async def vobiz_handler(request):
    caller_id = request.query.get("caller_id", "Unknown")
    ws = web.WebSocketResponse(protocols=['audio.drachtio.org'])
    await ws.prepare(request)
    print(f"--- [BRIDGE]: Ready ---")
    
    transcript_history = []
    start_time = time.time()
    state = {"last_ai_audio_time": 0} # Using dict for nonlocal mutate
    
    try:
        async with websockets.connect(GEMINI_URL) as gemini_ws:
            # Step 1: Handshake
            current_date_str = datetime.now().strftime("%A, %B %d, %Y")
            dynamic_prompt = f"{SYSTEM_PROMPT}\n\nIMPORTANT: Be calm, speak slowly. Patient number: {caller_id}. Today is: {current_date_str}."
            
            setup_msg = {
                "setup": {
                    "model": "models/gemini-3.1-flash-live-preview",
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}}}
                    },
                    "systemInstruction": {"parts": [{"text": dynamic_prompt}]},
                    "tools": TOOLS,
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {}
                }
            }
            await gemini_ws.send(json.dumps(setup_msg))
            setup_response = await gemini_ws.recv()
            print(f"--- [AI ENGINE]: Setup response: {setup_response[:300]}")
            print(f"--- [AI ENGINE]: Connected ---")

            # Step 2: Trigger greeting
            await gemini_ws.send(json.dumps({"realtimeInput": {"text": "Hello"}}))
            
            stream_sid = None
            upsample_state = None  # preserve ratecv state across chunks

            async def from_vobiz():
                nonlocal stream_sid, upsample_state
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            current_id = data.get("streamId") or data.get("streamSid") or (data.get("start", {}).get("streamId") if data.get("event") == "start" else None)
                            if current_id and not stream_sid: stream_sid = current_id

                            if data.get("event") == "media" and stream_sid:
                                if time.time() - state["last_ai_audio_time"] < 1.5: continue

                                payload = data.get("media", {}).get("payload") or data.get("payload")
                                if payload:
                                    mulaw_data = base64.b64decode(payload)
                                    pcm_8k = audioop.ulaw2lin(mulaw_data, 2)
                                    pcm_16k, upsample_state = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, upsample_state)
                                    await gemini_ws.send(json.dumps({"realtimeInput": {"audio": {"data": base64.b64encode(pcm_16k).decode("utf-8"), "mimeType": "audio/pcm;rate=16000"}}}))
                        elif msg.type == aiohttp.WSMsgType.CLOSE: break
                except Exception as e:
                    print(f"[ERROR] from_vobiz: {e}")

            downsample_state = None  # preserve ratecv state for AI output

            async def from_gemini():
                nonlocal downsample_state
                print("[GEMINI] Listener started")
                try:
                    async for message in gemini_ws:
                        resp = json.loads(message)

                        # Debug: log any keys we haven't seen before
                        known_keys = {"serverContent", "toolCall", "tool_call", "inputAudioTranscription", "setupComplete", "usageMetadata"}
                        new_keys = set(resp.keys()) - known_keys
                        if new_keys:
                            print(f"\n[DEBUG] Unknown resp keys: {new_keys} => {str(resp)[:300]}")

                        # User speech transcription
                        transcription = (
                            resp.get("inputAudioTranscription") or
                            resp.get("serverContent", {}).get("inputTranscription") or
                            resp.get("serverContent", {}).get("inputAudioTranscription")
                        )
                        if transcription:
                            text = transcription.get("text", "") if isinstance(transcription, dict) else ""
                            if text:
                                print(f"\n[USER]: {text}")
                                transcript_history.append(f"Caller: {text}")

                        # Priya's output transcription
                        output_transcription = (
                            resp.get("outputAudioTranscription") or
                            resp.get("serverContent", {}).get("outputTranscription") or
                            resp.get("serverContent", {}).get("outputAudioTranscription")
                        )
                        if output_transcription:
                            text = output_transcription.get("text", "") if isinstance(output_transcription, dict) else ""
                            if text:
                                print(f"\n[PRIYA]: {text}")
                                transcript_history.append(f"Priya: {text}")

                        server_content = resp.get("serverContent")
                        if server_content:
                            model_turn = server_content.get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    if "text" in part: transcript_history.append(f"Priya: {part['text']}")
                                    if "inlineData" in part:
                                        state["last_ai_audio_time"] = time.time()
                                        pcm_24k = base64.b64decode(part["inlineData"]["data"])
                                        pcm_8k, downsample_state = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, downsample_state)
                                        pcm_8k = audioop.mul(pcm_8k, 2, 1.4)
                                        mulaw_data = audioop.lin2ulaw(pcm_8k, 2)
                                        if stream_sid:
                                            print("🔊", end="", flush=True)
                                            await ws.send_str(json.dumps({
                                                "event": "playAudio", "streamId": stream_sid, 
                                                "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000, "payload": base64.b64encode(mulaw_data).decode("utf-8")}
                                            }))

                        tool_call = resp.get("toolCall") or resp.get("tool_call")
                        if tool_call:
                            function_calls = tool_call.get("functionCalls") or tool_call.get("function_calls")
                            tool_responses = []
                            for call in function_calls:
                                name, args, cid = call["name"], call.get("args") or call.get("arguments"), call.get("id") or call.get("call_id")
                                if name in ("book_appointment", "cancel_appointment", "reschedule_appointment"):
                                    if not args.get("contact_number") or args.get("contact_number") in ("Unknown", ""):
                                        args["contact_number"] = caller_id
                                if name == "book_appointment":
                                    if not args.get("parent_name") or args.get("parent_name") in ("Unknown", ""):
                                        args["parent_name"] = args.get("patient_name", "Parent")

                                print(f"\n[TOOL]: Calling {name}...")
                                if func := FUNCTION_MAP.get(name):
                                    try:
                                        result = func(**args)
                                        print(f"[TOOL]: Success!")
                                        tool_responses.append({"name": name, "id": cid, "response": {"result": result}})
                                    except Exception as e:
                                        print(f"[TOOL]: Error: {e}")
                                        tool_responses.append({"name": name, "id": cid, "response": {"error": str(e)}})
                            
                            await gemini_ws.send(json.dumps({"toolResponse": {"functionResponses": tool_responses}}))
                except Exception as e:
                    print(f"[ERROR] from_gemini: {e}")
                print("[GEMINI] Listener exited")

            v_task = asyncio.create_task(from_vobiz())
            g_task = asyncio.create_task(from_gemini())
            await asyncio.wait([v_task, g_task], return_when=asyncio.FIRST_COMPLETED)

    except Exception as e:
        print(f"[ERROR] vobiz_handler: {e}")
    finally:
        await asyncio.sleep(1.0)
        duration = int(time.time() - start_time)
        print(f"\n\n[LOG]: Call Finished ({duration}s). Updating clinic systems...")
        full_transcript = "\n".join(transcript_history) if transcript_history else "No voice activity detected."
        summary = f"Caller: {caller_id} | Duration: {duration}s | Lines: {len(transcript_history)}"
        try:
            send_call_summary_email(summary=summary, transcript=full_transcript)
        except Exception: pass
        if not ws.closed: await ws.close()

    return ws

async def main():
    # MANDATORY STARTUP VERIFICATION
    await run_startup_diagnostics()

    app = web.Application()
    app.router.add_post('/answer', handle_answer)
    app.router.add_get('/vobiz-stream', vobiz_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.1.0.0' if os.name != 'nt' else '0.0.0.0', 5050)
    await site.start()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  VOBIZ-GEMINI BRIDGE ONLINE (PRODUCTION GOLD RELEASE)    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
