# -*- coding: utf-8 -*-
"""
Voice Lab — Interactive browser-based voice interface for Acharya Ji.
"""
from aiohttp import web
from config.settings import PORT

_VOICE_LAB_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Voice Lab — Talk to Acharya Ji</title>
    <style>
        :root {
            --bg: #0f172a;
            --card: #1e293b;
            --primary: #8b5cf6;
            --text: #f8fafc;
            --mangal: #ef4444;
        }
        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            overflow: hidden;
        }
        .orb-container {
            position: relative;
            width: 300px;
            height: 300px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .orb {
            width: 150px;
            height: 150px;
            background: radial-gradient(circle, #a78bfa 0%, #7c3aed 100%);
            border-radius: 50%;
            box-shadow: 0 0 50px #7c3aed;
            transition: all 0.3s ease;
            position: relative;
            z-index: 2;
        }
        .orb.active {
            transform: scale(1.2);
            box-shadow: 0 0 80px #a78bfa;
            animation: pulse 1.5s infinite ease-in-out;
        }
        @keyframes pulse {
            0% { transform: scale(1.1); box-shadow: 0 0 40px #a78bfa; }
            50% { transform: scale(1.3); box-shadow: 0 0 100px #7c3aed; }
            100% { transform: scale(1.1); box-shadow: 0 0 40px #a78bfa; }
        }
        .status {
            margin-top: 40px;
            font-size: 1.2rem;
            font-weight: 500;
            color: #94a3b8;
        }
        .controls {
            margin-top: 40px;
            display: flex;
            gap: 20px;
        }
        .btn {
            padding: 12px 32px;
            border-radius: 30px;
            border: none;
            cursor: pointer;
            font-weight: 600;
            font-size: 1rem;
            transition: all 0.2s;
        }
        .btn-start { background: var(--primary); color: white; }
        .btn-stop { background: #334155; color: white; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        
        #transcript {
            margin-top: 40px;
            max-width: 600px;
            text-align: center;
            color: #cbd5e1;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="orb-container">
        <div id="orb" class="orb"></div>
    </div>
    
    <div id="status" class="status">Ready to consult the stars...</div>
    <div id="transcript">Click 'Start Session' and say "Pranam Acharya Ji"</div>
    
    <div style="width: 100%; max-width: 400px; background: #1e293b; height: 10px; border-radius: 5px; margin: 20px 0; overflow: hidden;">
        <div id="micLevel" style="width: 0%; height: 100%; background: #10b981; transition: width 0.1s;"></div>
    </div>
    
    <div class="controls">
        <select id="pipelineSelect" class="btn" style="background: #1e293b; color: white; border: 1px solid #334155;">
            <option value="sarvam-stream?tts=sarvam" selected>Acharya Ji (Sarvam Voice)</option>
            <option value="sarvam-stream?tts=eleven">Acharya Ji (ElevenLabs Premium Voice)</option>
            <option value="gemini-stream">Acharya Ji (Gemini Live — Fastest)</option>
        </select>
        <button id="startBtn" class="btn btn-start">Start Session</button>
        <button id="stopBtn" class="btn btn-stop" disabled>End Session</button>
        <button id="testBtn" class="btn" style="background: #fbbf24; color: black;">Test Audio</button>
    </div>

    <script>
        const startBtn = document.getElementById('startBtn');
        const stopBtn = document.getElementById('stopBtn');
        const orb = document.getElementById('orb');
        const status = document.getElementById('status');
        const transcript = document.getElementById('transcript');
        const micBar = document.getElementById('micLevel');

        let audioContext;
        let processor;
        let ws;
        let source;

        // Mu-law encoding table for 16-bit PCM
        const encodeTable = new Uint8Array(65536);
        for (let i = -32768; i <= 32767; i++) {
            let pcm = i;
            let mask = 0x7F;
            if (pcm < 0) {
                pcm = -pcm;
                mask = 0xFF;
            }
            if (pcm > 32635) pcm = 32635;
            pcm += 0x84;
            let exponent = 7;
            for (let expMask = 0x4000; (pcm & expMask) === 0 && exponent > 0; expMask >>= 1) exponent--;
            let mantissa = (pcm >> (exponent + 3)) & 0x0F;
            encodeTable[i + 32768] = ~(mask ^ ((exponent << 4) | mantissa));
        }

        async function startSession() {
            status.textContent = "Initializing Microphone...";
            startBtn.disabled = true;
            stopBtn.disabled = false;
            
            try {
                // 1. Setup AudioContext and Mic FIRST
                audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
                if (audioContext.state === 'suspended') await audioContext.resume();
                await setupMic();
                
                // 2. Only then connect to WebSocket
                status.textContent = "Connecting to Acharya Ji...";
                const pipeline = document.getElementById('pipelineSelect').value;
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                ws = new WebSocket(`${protocol}//${window.location.host}/${pipeline}&caller_id=BrowserUser`, ["audio.drachtio.org"]);
                
                ws.onopen = () => {
                    status.textContent = "Acharya Ji is listening...";
                    orb.classList.add('active');
                    ws.send(JSON.stringify({ event: "start", streamSid: "lab", start: { streamSid: "lab" } }));
                    
                    // Keep-alive heartbeat
                    window.heartbeat = setInterval(() => {
                        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({event: "heartbeat"}));
                    }, 5000);
                };

                ws.onmessage = async (e) => {
                    try {
                        const msg = JSON.parse(e.data);
                        if (msg.event === 'playAudio') {
                            status.textContent = "Acharya Ji is speaking...";
                            await playMuLaw(msg.media.payload);
                            status.textContent = "Acharya Ji is listening...";
                        }
                    } catch (err) {}
                };

                ws.onclose = () => stopSession();
                ws.onerror = () => { alert("Connection Error"); stopSession(); };
            } catch (err) {
                alert("Start Error: " + err.message);
                stopSession();
            }
        }

        async function setupMic() {
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                source = audioContext.createMediaStreamSource(stream);
                processor = audioContext.createScriptProcessor(2048, 1, 1);

                processor.onaudioprocess = (e) => {
                    if (!ws || ws.readyState !== WebSocket.OPEN) return;
                    const input = e.inputBuffer.getChannelData(0);
                    
                    // Update Mic Bar
                    let sum = 0;
                    for(let i=0; i<input.length; i++) sum += Math.abs(input[i]);
                    const level = Math.min(100, (sum / input.length) * 1000);
                    micBar.style.width = level + '%';

                    const mulaw = new Uint8Array(input.length);
                    for (let i = 0; i < input.length; i++) {
                        let s = Math.max(-1, Math.min(1, input[i])) * 32767;
                        mulaw[i] = encodeTable[Math.floor(s) + 32768];
                    }
                    ws.send(JSON.stringify({
                        event: "media",
                        media: { payload: btoa(String.fromCharCode.apply(null, mulaw)) }
                    }));
                };

                source.connect(processor);
                processor.connect(audioContext.destination);
            } catch (err) {
                alert("Mic Access Denied: " + err.message);
            }
        }

        const decodeTable = new Int16Array(256);
        for (let i = 0; i < 256; i++) {
            let u = ~i;
            let sign = (u & 0x80);
            let exponent = (u >> 4) & 0x07;
            let mantissa = u & 0x0F;
            let sample = ((mantissa << 3) + 0x84) << exponent;
            sample -= 0x84;
            decodeTable[i] = sign ? -sample : sample;
        }

        async function playMuLaw(b64) {
            if (!audioContext) return;
            const binary = atob(b64);
            const pcm = new Float32Array(binary.length);
            for (let i = 0; i < binary.length; i++) {
                pcm[i] = decodeTable[binary.charCodeAt(i)] / 32768;
            }
            const buffer = audioContext.createBuffer(1, pcm.length, 16000);
            buffer.getChannelData(0).set(pcm);
            const node = audioContext.createBufferSource();
            node.buffer = buffer;
            node.connect(audioContext.destination);
            return new Promise(res => { node.onended = res; node.start(); });
        }

        function stopSession() {
            if (window.heartbeat) clearInterval(window.heartbeat);
            if (ws) ws.close();
            if (source) source.disconnect();
            if (processor) processor.disconnect();
            orb.classList.remove('active');
            status.textContent = "Session ended.";
            startBtn.disabled = false;
            stopBtn.disabled = true;
            micBar.style.width = '0%';
        }

        startBtn.onclick = startSession;
        stopBtn.onclick = stopSession;
        
        document.getElementById('testBtn').onclick = () => {
            const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
            const osc = ctx.createOscillator();
            osc.connect(ctx.destination);
            osc.start();
            setTimeout(() => osc.stop(), 500);
            alert("Did you hear the beep? If yes, audio is working!");
        };
    </script>
</body>
</html>
"""

async def voice_lab_page(request: web.Request) -> web.Response:
    return web.Response(text=_VOICE_LAB_TEMPLATE, content_type="text/html")
