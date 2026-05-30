import asyncio
import websockets
import json
import base64
import audioop

async def simulate_vobiz_call():
    uri = "ws://localhost:5050/vobiz-stream"
    print(f"Connecting to local Vobiz bridge: {uri}")
    
    try:
        async with websockets.connect(uri) as websocket:
            # 1. Send simulated 'start' event
            start_msg = {
                "event": "start",
                "start": {
                    "streamId": "test-sim-id-123",
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}
                }
            }
            await websocket.send(json.dumps(start_msg))
            print("✅ Sent 'start' event.")

            # 2. Send 1 second of simulated Mu-law silence (all 0xFF in Mu-law is silence)
            silence_payload = base64.b64encode(b'\xff' * 160).decode('utf-8')
            media_msg = {
                "event": "media",
                "streamId": "test-sim-id-123",
                "media": {"payload": silence_payload}
            }
            await websocket.send(json.dumps(media_msg))
            print("✅ Sent 'media' packet (Simulated Voice).")

            # 3. Wait for 'playAudio' response from our server
            print("⌛ Waiting for AI response (this confirms the Gemini bridge is active)...")
            try:
                # Wait up to 10 seconds for the first audio chunk from Gemini
                response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                data = json.loads(response)
                
                if data.get("event") == "playAudio":
                    print("🎉 SUCCESS! Server sent 'playAudio' event.")
                    print(f"   - StreamID: {data.get('streamId')}")
                    print(f"   - Encoding: {data.get('media', {}).get('encoding')}")
                    print(f"   - Payload Length: {len(data.get('media', {}).get('payload', ''))}")
                    
                    if data['media']['encoding'] == 'audio/x-mulaw':
                        print("✅ VERIFIED: Audio is in the correct Telephony Mu-law format.")
                    else:
                        print(f"❌ ERROR: Wrong encoding returned: {data['media']['encoding']}")
                else:
                    print(f"❓ Got unexpected event: {data.get('event')}")
            except asyncio.TimeoutError:
                print("❌ TIMEOUT: No response from AI. Check if GEMINI_API_KEY is working and server is running.")

    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        print("Make sure you have run 'python vobiz_main.py' in another terminal first!")

if __name__ == "__main__":
    asyncio.run(simulate_vobiz_call())
