import asyncio
import websockets
import json

async def mock_twilio():
    uri = "ws://localhost:5000"
    try:
        async with websockets.connect(uri) as websocket:
            print("Successfully connected to main.py")
            
            # Send Connected event
            connected_event = {"event": "connected"}
            await websocket.send(json.dumps(connected_event))
            print("Sent 'connected' event")

            # Send Start event
            start_event = {
                "event": "start",
                "start": {
                    "streamSid": "mock_stream_123"
                }
            }
            await websocket.send(json.dumps(start_event))
            print("Sent 'start' event with streamSid: mock_stream_123")

            # Keep connection open for a bit to see the handshake in main.py logs
            print("Waiting 10 seconds for main.py to process handshake...")
            await asyncio.sleep(10)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(mock_twilio())
