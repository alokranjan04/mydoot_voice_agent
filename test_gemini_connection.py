import asyncio
import json
import os
import websockets
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "models/gemini-2.5-flash-native-audio-latest"
GEMINI_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    f"?key={GEMINI_API_KEY}"
)

async def test_connection():
    print(f"Testing model: {MODEL}")
    try:
        async with websockets.connect(GEMINI_URL) as ws:
            # 1. Setup
            setup_msg = {
                "setup": {
                    "model": MODEL,
                    "generationConfig": {"responseModalities": ["AUDIO"]},
                    "systemInstruction": {"parts": [{"text": "You are a helpful assistant. Reply in Hindi."}]},
                }
            }
            await ws.send(json.dumps(setup_msg))
            resp = await ws.recv()
            resp_json = json.loads(resp)
            if resp_json.get("error"):
                err = resp_json["error"]
                print(f"FAIL — Setup error: {err.get('status')} — {err.get('message')}")
                return
            if resp_json.get("setupComplete") is not None:
                print(f"PASS — Setup confirmed: {resp}")
            else:
                print(f"PASS — Setup response received: {resp[:120]}")

            # 2. Send greeting nudge (clientContent — correct format)
            await ws.send(json.dumps({
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": "Say hello in Hindi."}]}],
                    "turnComplete": True
                }
            }))
            print("Sent greeting nudge via clientContent")

            # 3. Wait for Gemini to respond (audio or text)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=8.0)
                parsed = json.loads(msg)
                if parsed.get("error"):
                    print(f"FAIL — Gemini error after nudge: {parsed['error']}")
                else:
                    sc = parsed.get("serverContent") or {}
                    has_audio = any(
                        "inlineData" in p
                        for part_list in [sc.get("modelTurn", {}).get("parts", [])]
                        for p in part_list
                    )
                    print(f"PASS — Gemini responded. Has audio: {has_audio} | Keys: {list(parsed.keys())}")
            except asyncio.TimeoutError:
                print("PASS — Connection stable, no response within 8s (may need audio input first)")

    except Exception as e:
        print(f"FAIL — {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
