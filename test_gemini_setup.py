import asyncio
import json
import os
import websockets
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

async def test_setup():
    print(f"Connecting to: {GEMINI_URL}")
    try:
        async with websockets.connect(GEMINI_URL) as ws:
            print("Connected. Sending Setup Message...")
            
            # STAGE 3: Testing Voice + Transcription
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
                    "tools": [
                        {
                            "functionDeclarations": [
                                {
                                    "name": "check_available_slots",
                                    "description": "Check slots.",
                                    "parameters": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "preferred_day": {"type": "STRING"}
                                        },
                                        "required": ["preferred_day"]
                                    }
                                }
                            ]
                        }
                    ],
                    "inputAudioTranscription": {} 
                }
            }
            
            await ws.send(json.dumps(setup_msg))
            print("Setup sent (Stage 3: Full Extras). Waiting for response...")
            
            response = await ws.recv()
            
            # Robust check for byte strings
            if isinstance(response, bytes):
                response_str = response.decode('utf-8')
            else:
                response_str = response

            print(f"RAW RESPONSE: {response_str}")
            
            if "setupComplete" in response_str:
                print("\nSUCCESS! Protocol is 100% stable. I will now update the main script.")
            else:
                print("\nFAILED. We have found the culprit. I will investigate this specific field.")
            
    except Exception as e:
        print(f"Handshake Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_setup())
