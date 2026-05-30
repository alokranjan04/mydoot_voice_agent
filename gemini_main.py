import asyncio
import base64
import json
import os
import websockets
import audioop
import requests
from datetime import datetime
from dotenv import load_dotenv
from pharmacy_functions import FUNCTION_MAP, send_call_summary_email, update_booking_sheet

load_dotenv()

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

# Load Dynamic Config (Prompts & Persona)
def load_app_config():
    with open('app_config.json', 'r') as f:
        return json.load(f)

APP_CONFIG = load_app_config()
SYSTEM_PROMPT = APP_CONFIG["agent"]["system_prompt"]
ANALYTICS_PROMPT = APP_CONFIG["analytics"]["summary_prompt"]


# Gemini Tool Definitions (Strict 2026 camelCase)
TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "check_available_slots",
                "description": "Check available appointment slots for a given day (Monday to Saturday).",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "preferred_day": {"type": "STRING", "description": "The day of the week (e.g., 'Monday')."}
                    },
                    "required": ["preferred_day"]
                }
            },
            {
                "name": "book_appointment",
                "description": "Book a doctor appointment. Call this ONLY after collecting all patient details.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "patient_name": {"type": "STRING"},
                        "patient_age": {"type": "STRING"},
                        "parent_name": {"type": "STRING"},
                        "contact_number": {"type": "STRING"},
                        "preferred_day": {"type": "STRING"},
                        "preferred_time": {"type": "STRING"},
                        "reason": {"type": "STRING"}
                    },
                    "required": ["patient_name", "patient_age", "parent_name", "contact_number", "preferred_day", "preferred_time", "reason"]
                }
            },
            {
                "name": "check_appointment",
                "description": "Check details of an existing appointment by its ID.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "appointment_id": {"type": "INTEGER"}
                    },
                    "required": ["appointment_id"]
                }
            },
            {
                "name": "cancel_appointment",
                "description": "Cancel an existing appointment by its ID.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "appointment_id": {"type": "INTEGER"}
                    },
                    "required": ["appointment_id"]
                }
            }
        ]
    }
]

async def gemini_handler(twilio_ws):
    """Bridge Twilio and Gemini Multimodal Live API."""
    # Startup Diagnostic: Log Service Account Email
    try:
        with open('google-credentials.json', 'r') as f:
            creds_data = json.load(f)
            print("--- GOOGLE CONNECTION DIAGNOSTIC ---")
            print(f"Service Account Email: {creds_data.get('client_email')}")
            print("Action: Share your Google Sheet with the email above as 'EDITOR'")
            print("------------------------------------\n")
    except:
        print("WARNING: Could not load google-credentials.json for startup diagnostic.")

    async with websockets.connect(GEMINI_URL) as gemini_ws:
        print("Connected to Gemini Multimodal Live API")

        # 1. Send Initial Working Setup to Gemini (Now with Current Date)
        current_date_str = datetime.now().strftime("%A, %B %d, %Y (%I:%M %p)")
        dynamic_prompt = f"{SYSTEM_PROMPT}\n\nIMPORTANT CONTEXT: Today is {current_date_str}. Use this to calculate 'tomorrow' or 'next week' correctly."
        
        setup_msg = {
            "setup": {
                "model": "models/gemini-3.1-flash-live-preview",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": "Aoede" 
                            }
                        }
                    }
                },
                "systemInstruction": {
                    "parts": [{"text": dynamic_prompt}]
                },
                "tools": TOOLS,
                "inputAudioTranscription": {} 
            }
        }
        await gemini_ws.send(json.dumps(setup_msg))
        
        # Wait for setup response
        msg = await gemini_ws.recv()
        print(f"Gemini Setup Response: {msg}")

        stream_sid = None
        stream_sid_event = asyncio.Event()
        transcript_history = ["Call session started."]  # Ensure history is never empty

        async def from_twilio():
            nonlocal stream_sid
            try:
                async for message in twilio_ws:
                    data = json.loads(message)
                    if data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        print(f"Twilio: Started stream {stream_sid}")
                        stream_sid_event.set()
                    elif data["event"] == "media":
                        # Twilio (8kHz mulaw) -> Gemini (16kHz PCM)
                        mulaw_audio = base64.b64decode(data["media"]["payload"])
                        pcm_audio = audioop.ulaw2lin(mulaw_audio, 2)
                        pcm_16k, _ = audioop.ratecv(pcm_audio, 2, 1, 8000, 16000, None)
                        
                        audio_msg = {
                            "realtimeInput": {
                                "audio": {
                                    "data": base64.b64encode(pcm_16k).decode("utf-8"),
                                    "mimeType": "audio/pcm;rate=16000"
                                }
                            }
                        }
                        await gemini_ws.send(json.dumps(audio_msg))
                    elif data["event"] == "stop":
                        print("Twilio: stream stopped")
                        break
            except Exception as e:
                print(f"Twilio: Error in stream loop: {e}")
            finally:
                print("Twilio: Closing connection handler.")

        async def from_gemini():
            try:
                first_audio_received = False
                async for message in gemini_ws:
                    response = json.loads(message)
                    
                    # 1. Handle Tool Calls
                    if "toolCall" in response:
                        print(f"DEBUG: Tool Call Attempted.")

                    # 2. Handle INPUT Transcription (Your Voice - Top Level)
                    if "inputTranscription" in response:
                        text = response["inputTranscription"].get("text", "")
                        if text:
                            print(f"User (Transcript): {text}")
                            transcript_history.append(f"User: {text}")

                    # 3. Handle OUTPUT Transcription (Priya's Voice - Top Level)
                    if "outputTranscription" in response:
                        text = response["outputTranscription"].get("text", "")
                        if text:
                            print(f"Priya (Transcript): {text}")
                            transcript_history.append(f"Priya: {text}")

                    # 4. Handle Server Content (Audio)
                    server_content = response.get("serverContent")
                    if server_content:
                        model_turn = server_content.get("modelTurn")
                        if model_turn:
                            for part in model_turn.get("parts", []):
                                inline_data = part.get("inlineData")
                                if inline_data:
                                    if not first_audio_received:
                                        print("Gemini: Received first audio frame! Priya is talking.")
                                        first_audio_received = True

                                    pcm_24k = base64.b64decode(inline_data["data"])
                                    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
                                    mulaw_audio = audioop.lin2ulaw(pcm_8k, 2)
                                    
                                    if stream_sid:
                                        media_msg = {
                                            "event": "media",
                                            "streamSid": stream_sid,
                                            "media": {"payload": base64.b64encode(mulaw_audio).decode("utf-8")}
                                        }
                                        await twilio_ws.send(json.dumps(media_msg))
                                
                                # TEXT HANDLING
                                if "text" in part:
                                    # print(f"Priya (Text Part): {part['text']}")
                                    pass

                        if server_content.get("turnComplete"):
                            print("Gemini: Turn complete")

                    # Handle Tool Calls
                    tool_call = response.get("toolCall") or response.get("tool_call")
                    if tool_call:
                        function_calls = tool_call.get("functionCalls") or tool_call.get("function_calls")
                        tool_responses = []
                        
                        for call in function_calls:
                            name = call["name"]
                            args = call.get("args") or call.get("arguments")
                            call_id = call.get("id") or call.get("call_id")
                            
                            print(f"Gemini: Executing tool: {name} with args: {args}")
                            func = FUNCTION_MAP.get(name)
                            if func:
                                try:
                                    result = func(**args)
                                    tool_responses.append({
                                        "name": name,
                                        "id": call_id,
                                        "response": {"result": result}
                                    })
                                except Exception as e:
                                    print(f"Gemini: Tool Error ({name}): {e}")
                                    tool_responses.append({
                                        "name": name,
                                        "id": call_id,
                                        "response": {"error": str(e)}
                                    })
                        
                        # Send response back to Gemini (matching the server's expected casing)
                        resp_msg = {
                            "toolResponse": {
                                "functionResponses": tool_responses
                            }
                        }
                        await gemini_ws.send(json.dumps(resp_msg))
            except websockets.exceptions.ConnectionClosed as e:
                print(f"Gemini: WebSocket closed: {e}")
            except Exception as e:
                print(f"Gemini: Error in stream loop: {e}")

        # Use the core audio loops; Priya greets naturally upon first activity
        try:
            # Create tasks instead of a simple gather
            twilio_task = asyncio.create_task(from_twilio())
            gemini_task = asyncio.create_task(from_gemini())
            
            # Wait for either to finish (usually Twilio when user hangs up)
            done, pending = await asyncio.wait(
                [twilio_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel the remaining task
            for task in pending:
                task.cancel()
                
        finally:
            # 1. Final WebSocket Close
            print("\nGemini: Call connection closed. Starting post-call analytics...")
            
            # CALL ENDS: Generate Summary & Email
            if len(transcript_history) >= 1: # Force analytics attempt
                print(f"DEBUG: Processing analytics for {len(transcript_history)} turns...")
                try:
                    full_transcript = "\n".join(transcript_history)
                    
                    # 1. Generate Summary & Structured Data using Gemini REST API
                    api_key = os.getenv("GEMINI_API_KEY")
                    analysis_prompt = f"""Analyze the Hinglish call between a pediatric clinic AI (Priya) and a parent. 
                    1. Provide a concise medical summary in English.
                    2. Extract these fields in JSON format, being very careful to find names even if they are mixed with Hindi: 
                       {{
                         "caller_name": "...",
                         "patient_name": "...",
                         "problems": "...",
                         "parents_name": "...",
                         "is_booked": true/false
                       }}
                    
                    Transcript:
                    {full_transcript}"""
                    
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
                    headers = {'Content-Type': 'application/json'}
                    data = {
                        "contents": [{"parts": [{"text": analysis_prompt}]}]
                    }
                    
                    analysis_resp = requests.post(url, headers=headers, json=data)
                    summary_text = "Summary could not be generated."
                    extracted_data = {}
                    
                    if analysis_resp.status_code == 200:
                        resp_json = analysis_resp.json()
                        raw_text = resp_json['candidates'][0]['content']['parts'][0]['text']
                        
                        # Separate summary and JSON
                        if "```json" in raw_text:
                            parts = raw_text.split("```json")
                            summary_text = parts[0].strip()
                            json_str = parts[1].split("```")[0].strip()
                            try:
                                extracted_data = json.loads(json_str)
                            except: pass
                        else:
                            summary_text = raw_text
                    
                    print(f"DEBUG: Google Analytics Extraction Result: {summary_text[:50]}...")
                    
                    # 2. Update Google Sheet
                    print(f"DEBUG: Attempting to log call to Google Sheets for {extracted_data.get('patient_name')}...")
                    update_booking_sheet(
                        name=extracted_data.get("caller_name"),
                        patient_name=extracted_data.get("patient_name"),
                        problems=extracted_data.get("problems"),
                        parents_name=extracted_data.get("parents_name"),
                        is_booked=extracted_data.get("is_booked", False),
                        booking_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    )
                    
                    # 3. Send the Email
                    print("DEBUG: Dispatching post-call summary email...")
                    send_call_summary_email(summary_text, full_transcript)
                except Exception as e:
                    print(f"CRITICAL ERROR in post-call analytics: {e}")

async def main():
    server = await websockets.serve(gemini_handler, "localhost", 5000)
    print("Gemini Voice Agent Server started on port 5000")
    await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down...")
