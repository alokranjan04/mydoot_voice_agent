import asyncio
import base64
import json
import sys
import websockets
import ssl

from dotenv import load_dotenv
from pharmacy_functions import FUNCTION_MAP
import os   
load_dotenv()

def sts_conect():
    # 1. Corrected the spelling of DEEPGRAM (check your .env file matches this)
    api_key = os.getenv("DEEPGRAM_API_KEY") 
    
    if not api_key:
        raise Exception("Error: DEEPGRAM_API_KEY not found in environment variables.")
    
    sts_ws = websockets.connect(    
       "wss://agent.deepgram.com/v1/agent/converse",
        # 2. Corrected "toekn" to "token"
        subprotocols=["token", api_key], 
    )

    return sts_ws


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)
    
    # 1. FIX: Deepgram expects 'instructions', not 'prompt'
    # Wait! In the nested provider structure, it might still take 'prompt'.
    # We'll use prompt as a sibling to provider first.
    
    # 2. Inject OpenAI Key into think.endpoint (Bearer token)
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key and "endpoint" in config["agent"]["think"]:
        config["agent"]["think"]["endpoint"]["headers"]["Authorization"] = f"Bearer {openai_key}"
    
    # 3. Inject ElevenLabs Key into speak.provider
    eleven_labs_key = os.getenv("ELEVEN_LABS_API_KEY")
    if eleven_labs_key and "speak" in config["agent"]:
        if "provider" in config["agent"]["speak"]:
            config["agent"]["speak"]["provider"]["api_key"] = eleven_labs_key
             
    return config

async def handle_barge_in(decoded, twilio_ws, streamsid):
    if decoded["type"] == "UserStartedSpeaking":
        clear_message = {
            "event": "clear",
            "streamSid": streamsid
        }
        await twilio_ws.send(json.dumps(clear_message))


def execute_function_call(func_name, arguments):
    if func_name in FUNCTION_MAP:
        result = FUNCTION_MAP[func_name](**arguments)
        print(f"Function call result: {result}")
        return result
    else:
        result = {"error": f"Unknown function: {func_name}"}
        print(result)
        return result


def create_function_call_response(func_id, func_name, result):
    return {
        "type": "FunctionCallResponse",
        "id": func_id,
        "name": func_name,
        "content": json.dumps(result)
    }


async def handle_function_call_request(decoded, sts_ws):
    try:
        for function_call in decoded["functions"]:
            func_name = function_call["name"]
            func_id = function_call["id"]
            arguments = json.loads(function_call["arguments"])

            print(f"Function call: {func_name} (ID: {func_id}), arguments: {arguments}")

            result = execute_function_call(func_name, arguments)

            function_result = create_function_call_response(func_id, func_name, result)
            await sts_ws.send(json.dumps(function_result))
            print(f"Sent function result: {function_result}")

    except Exception as e:
        print(f"Error calling function: {e}")
        error_result = create_function_call_response(
            func_id if "func_id" in locals() else "unknown",
            func_name if "func_name" in locals() else "unknown",
            {"error": f"Function call failed with: {str(e)}"}
        )
        await sts_ws.send(json.dumps(error_result))



async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    await handle_barge_in(decoded, twilio_ws, streamsid)
    if decoded["type"] == "FunctionCallRequest":
        await handle_function_call_request(decoded, sts_ws)

    
async def sts_sender(sts_ws, audoa_queue):
    print("sts_sender started")
    while True:
        chunk = await audoa_queue.get()
       
        await sts_ws.send(chunk)    
            

async def sts_reciever(sts_ws, twilio_ws, streamsid_queue):
    print("sts_receiver started")
    streamsid = await streamsid_queue.get()

    # Buffer output and send in fixed 160-byte (20ms mulaw) chunks to prevent audio breaks
    OUT_CHUNK = 160
    out_buffer = bytearray()

    async for message in sts_ws:
        if type(message) is str:
            print(message)
            decoded = json.loads(message)
            await handle_text_message(decoded, twilio_ws, sts_ws, streamsid)
            continue

        out_buffer.extend(message)

        while len(out_buffer) >= OUT_CHUNK:
            chunk = bytes(out_buffer[:OUT_CHUNK])
            out_buffer = out_buffer[OUT_CHUNK:]
            media_message = {
                "event": "media",
                "streamSid": streamsid,
                "media": {"payload": base64.b64encode(chunk).decode("ascii")}
            }
            await twilio_ws.send(json.dumps(media_message))

async def twilio_receiver( twilio_ws, audio_queue,  streamsid_queue):
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray(b"")

    async for message in twilio_ws:
        try:
            data = json.loads(message)
            event = data["event"]

            if event == "start":
                print("get our streamsid")
                start = data["start"]
                streamsid = start["streamSid"]
                streamsid_queue.put_nowait(streamsid)
            elif event == "connected":
                continue
            elif event == "media":
                media = data["media"]
                chunk = base64.b64decode(media["payload"])
                if media["track"] == "inbound":
                    inbuffer.extend(chunk)
            elif event == "stop":
                break

            while len(inbuffer) >= BUFFER_SIZE:
                chunk = inbuffer[:BUFFER_SIZE]
                audio_queue.put_nowait(chunk)
                inbuffer = inbuffer[BUFFER_SIZE:]
        except:
            break
    pass

async def twilio_handler(twilio_ws):  
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()   

    async with sts_conect() as sts_ws: 
        # 1. Receive Welcome
        welcome_message = await sts_ws.recv()
        print(f"DEBUG: Received from Deepgram: {welcome_message}")
        
        # 2. Send Config
        config_message = load_config()
        await sts_ws.send(json.dumps(config_message))
        
        # 3. Wait for Configuration Response
        config_response = await sts_ws.recv()
        print(f"DEBUG: Config Response: {config_response}")
        
        # Check if the config was actually accepted before proceeding
        resp_data = json.loads(config_response)
        if resp_data.get("type") == "Error":
            print(f"CRITICAL ERROR: Deepgram rejected config: {resp_data.get('description')}")
            return # Exit early so we don't spam binary data into a closed socket

        await asyncio.gather(
            sts_sender(sts_ws, audio_queue),
            sts_reciever(sts_ws, twilio_ws, streamsid_queue),
            twilio_receiver(twilio_ws, audio_queue, streamsid_queue) 
        )
async def main():

    await websockets.serve(twilio_handler, host="localhost", port=5000)
    print("Started Server..")
    await asyncio.Future()

if __name__ == "__main__":    
    try:
        asyncio.run(main()) 
    except KeyboardInterrupt:
        print("Shutting down server...")
