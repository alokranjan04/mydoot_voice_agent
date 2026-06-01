# Technical Architecture: Mydoot Customer Care Voice Agent

**Agent Name:** Mydoot Customer Care Representative
**Active Pipeline:** Google Gemini Live (native audio)
**Stack:** Gemini 2.5 Flash Native Audio, Vobiz SIP, Google Sheets, Gmail SMTP, Google Cloud Run

---

## 1. System Overview

Mydoot Customer Care is an AI voice agent that handles inbound phone calls on the Vobiz SIP platform. It uses Google Gemini Live for end-to-end audio understanding and synthesis (no separate STT/TTS steps), collects structured complaint data conversationally in Hinglish or English (auto-detected from the customer's first response), and persists results to Google Sheets.

---

## 2. End-to-End Call Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. Customer dials +917971542939 (Vobiz SIP number)                  │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. Vobiz HTTP POST → https://<cloud-run-url>/answer                 │
│    Body: From=<caller_phone>                                        │
│    Handler: routes/webhook.py → handle_answer()                     │
│    Returns: XML with bidirectional WebSocket URL:                   │
│    <Stream bidirectional="true">wss://.../gemini-stream?caller_id=  │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. Vobiz opens bidirectional WebSocket to /gemini-stream            │
│    Audio format: mu-law 8kHz (base64-encoded JSON frames)           │
│    Handler: pipelines/gemini.py → gemini_handler()                  │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. gemini_handler opens second WebSocket to Gemini Live API         │
│    wss://generativelanguage.googleapis.com/.../BidiGenerateContent  │
│    Sends setup: model, systemInstruction, tools                     │
│    (No speechConfig, no VAD config — native audio model only)       │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
           ┌──────────────┴──────────────┐
           │                             │
           ▼                             ▼
┌──────────────────────┐    ┌────────────────────────┐
│ Audio IN loop        │    │ g_receiver task         │
│ (Vobiz → Gemini)     │    │ (Gemini → Vobiz)        │
│                      │    │                         │
│ mu-law 8kHz          │    │ PCM 24kHz               │
│ → ulaw2lin           │    │ → ratecv(24000→8000)    │
│ → [noise gate]       │    │ → lin2ulaw              │
│ → ratecv(8k→16k)     │    │ → mu-law 8kHz           │
│ → PCM 16kHz          │    │ → Vobiz playAudio       │
│ → batch 4 frames     │    │                         │
│ → Gemini realtimeIn  │    │ Tracks turnComplete:    │
│                      │    │ flushes transcript buf  │
│ BLOCKED until 2s     │    │ sets gemini_turn_end_ts │
│ after call connect   │    │                         │
│ (startup guard)      │    │                         │
│                      │    │                         │
│ Echo guard:          │    │                         │
│ 0.3s after each      │    │                         │
│ turnComplete         │    │                         │
│                      │    │                         │
│ Post-save guard:     │    │                         │
│ 15s after successful │    │                         │
│ save_customer_feed-  │    │                         │
│ back call            │    │                         │
└──────────────────────┘    └────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. Tool Call: save_customer_feedback                                │
│    Triggered when Gemini has all 7 fields                           │
│    Handler: mydoot_functions.py                                     │
│    - Appends row to Google Sheets                                   │
│    - Returns success to Gemini                                      │
│    - Gemini speaks confirmation once, then goes silent              │
│    - Call auto-closes 8s after success (_close_after task)          │
│    - save_executed flag prevents duplicate execution per session    │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 6. Call ends (auto-close or customer hangs up)                      │
│    - WAV recording saved if RECORD_CALLS=1                          │
│    - finally block → send_call_summary_email()                      │
│    - Gmail SMTP SSL → transcript email to admin                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Audio Pipeline

### Inbound (Customer Voice → Gemini)

```
Vobiz WebSocket frame
  └── event: "media"
  └── payload: base64(mu-law PCM, 8kHz, mono, 16-bit)
        │
        ▼ audioop.ulaw2lin(data, 2)
  Linear PCM, 8kHz, mono, 16-bit
        │
        ├── [if RECORD_CALLS=1] append to pcm8_frames buffer
        │
        ▼ Noise gate: audioop.rms(pcm8, 2)
  if rms < threshold → inject silence or drop (see Section 4)
        │
        ▼ audioop.ratecv(pcm8, 2, 1, 8000, 16000, state)
  Linear PCM, 16kHz, mono, 16-bit
        │
        ▼ batch 4 × 20ms frames (80ms total)
  Gemini realtimeInput { audio: { data, mimeType: "audio/pcm;rate=16000" } }
```

### Outbound (Gemini Voice → Customer)

```
Gemini serverContent.modelTurn.parts[].inlineData
  └── data: base64(PCM, 24kHz, mono, 16-bit)
        │
        ▼ audioop.ratecv(pcm24, 2, 1, 24000, 8000, state)
  Linear PCM, 8kHz, mono, 16-bit
        │
        ▼ audioop.lin2ulaw(pcm8, 2)
  mu-law, 8kHz, mono
        │
        ▼ base64.b64encode()
  Vobiz playAudio { contentType: "audio/x-mulaw", sampleRate: 8000, payload }
```

---

## 4. Noise Gate and Audio Blocking Logic

PSTN phone calls carry constant background noise (fan hiss, line noise, ~RMS 80–150). Without filtering, Gemini's VAD never detects silence and waits 20–30 seconds before responding. The noise gate solves this.

### Noise Gate (energy threshold)

Every inbound packet's RMS energy is measured. Packets below `NOISE_GATE_RMS` (default 100) are treated as background noise, not speech.

**Fallback mode** — after `NOISE_GATE_FALLBACK_AFTER_S` (2.0s) with no speech detected in the current turn and at least 50 noise-blocked packets: the threshold drops to `NOISE_GATE_FALLBACK_RMS` (default 20). This handles very quiet speech without permanently suppressing soft voices.

**Full disable** — after `NOISE_GATE_FALLBACK_FULL_DISABLE_S` (7.0s) with no speech and `NOISE_GATE_FALLBACK_FULL_COUNT` (40) more noise packets: the gate is fully disabled. This is a last-resort fallback for extremely quiet calls.

**Per-turn reset** — `turn_fwd_count` and `turn_noise_blocked` are reset every time `gemini_turn_end_ts` changes (i.e., each time the agent finishes a turn). This ensures the fallback evaluates the current turn independently, not the entire call history.

### Silence Injection

When a packet is noise-gated, instead of dropping it, the pipeline replaces the audio data with zero-amplitude PCM and forwards it to Gemini. This provides an explicit silence signal to Gemini's VAD, allowing it to detect end-of-speech in ~1–2 seconds rather than hanging indefinitely.

The silence window: inject for `SPEECH_TAIL_SECS + SILENCE_SEND_SECS` (0.4 + 5.0 = 5.4s) after the last speech packet or the agent's `turnComplete + 0.3s` (whichever is later). After this window, packets are dropped entirely.

```python
_silence_ref = max(
    last_speech_ts,
    gemini_turn_end_ts + 0.3 if gemini_turn_end_ts > 0 else 0.0,
)
since_ref = now - _silence_ref
if since_ref < SPEECH_TAIL_SECS + SILENCE_SEND_SECS:
    pcm16 = bytes(len(pcm16))  # zero-amplitude silence
else:
    continue  # drop entirely
```

### Three Audio Blocking Layers

```
# Precedence order (highest to lowest):
# 1. Post-save guard:   save_done_ts set → block all customer audio for 15s
# 2. Startup guard:     now - call_start_ts < 2.0s → block
# 3. Echo guard:        now - gemini_turn_end_ts < 0.3s → block
# 4. Inactivity check:  no speech for 20s after agent turn → close call
# 5. Noise gate:        rms < threshold → inject silence or drop
# 6. Forward to Gemini
```

**Layer 1 — Post-Save Guard (15s)**
After `save_customer_feedback` succeeds, all customer audio is blocked for 15 seconds. This ensures the confirmation message plays completely without interruption.

**Layer 2 — Startup Guard (2s)**
All customer audio is blocked for 2 seconds after the call connects. Prevents connection burst noise from interrupting the agent before the greeting starts.

**Layer 3 — Echo Guard (0.3s per turn)**
After every `turnComplete`, a 0.3-second buffer blocks customer audio. Prevents the tail of Gemini's own audio from echoing back as customer speech. Safety release: if `turnComplete` never arrives within 8 seconds of the last AI audio, the guard is force-released.

**Inactivity Timeout (20s)**
If no customer speech is detected for 20 seconds after the agent's last `turnComplete`, the call is closed. Prevents indefinite hangs when the customer goes silent or disconnects without hanging up.

### Audio Batching

Inbound packets (20ms each) are accumulated into batches of 4 (80ms) before sending to Gemini. This reduces the Gemini send rate from 50/s to ~12/s, lowering WebSocket overhead without increasing perceptible latency.

---

## 5. Gemini Live Configuration

```json
{
  "setup": {
    "model": "models/gemini-2.5-flash-native-audio-latest",
    "generationConfig": {
      "responseModalities": ["AUDIO"]
    },
    "inputAudioTranscription":  {},
    "outputAudioTranscription": {},
    "systemInstruction": { "parts": [{ "text": "<system_prompt>" }] },
    "tools": [{ "function_declarations": [...] }]
  }
}
```

| Setting | Value | Reason |
|---------|-------|--------|
| Model | gemini-2.5-flash-native-audio-latest | Native audio — no separate STT/TTS |
| Modalities | AUDIO only | No text output needed |
| speechConfig | Not set | Causes deferred 1008 errors on this model |
| VAD config | Not set | Causes 1008 policy violations on native audio model |
| Transcription | Both directions | For post-call transcript email |
| ping_interval | 20s | Prevents mid-call WebSocket timeout |
| ping_timeout | 20s | Drops dead connections quickly |

---

## 6. Conversation Flow and Data Collection

### Greeting and Language Detection (Hinglish-First)

On `[CALL_STARTED]`, the agent delivers a Hinglish greeting (one of 3 random options) and immediately asks about the problem — no language selection question.

Language is auto-detected silently from the customer's **first complete response**:
- Customer responds exclusively in English with no Hindi words → entire call in English
- Customer responds in Hindi, Hinglish, mixed, unclear, garbled, or silent → entire call in Hinglish

**Why Hinglish-first (not bilingual):** PSTN 8kHz mu-law codec makes phonetically similar words indistinguishable. "Hindi" sounds like "Hello" to speech recognition through this codec. Removing the language question eliminates the need to transcribe "Hindi" at all — the most common case (Hinglish) requires no language input from the customer.

### Clarifying Questions (Never Silent)

When any response is unclear, garbled, or ambiguous, the agent asks a short one-sentence clarifying question:
- Noisy/inaudible audio: "Maafi, thoda clearly bol sakte hain?"
- Ambiguous device: "Kya yeh tube light hai, LED bulb hai, ya koi aur fitting?"
- Brand unclear: asks to spell or repeat
- Completely inaudible: "Main sun nahi paayi, kripya phir se bolein"

The agent never goes blank or silent mid-conversation.

### Field Collection Order (Step 2)
After the greeting, fields are collected in this fixed order:

| Step | Field(s) | Notes |
|------|----------|-------|
| 1 | complaint + device | The greeting already asks this — skip if customer answered in first reply |
| 2 | brand | Skipped if mentioned anywhere in prior speech |
| 3 | item | Skipped if mentioned anywhere in prior speech |
| 4 | product_used_since + usage_duration | Single question: "How long have you been using it?" — fills both fields |
| 5 | warranty_status | Enum value selected from three options |
| 6 | customer_name | Always collected LAST |

### Extract, Don't Re-Ask
The agent extracts information from any point in the conversation. Examples:
- "my LG TV is not working" → brand=LG, item=TV
- "MacBook" → brand=Apple, item=MacBook Laptop
- "I'm Rohit" → customer_name=Rohit
- "tube or bulb, drawing room" → item=Light/Tube Light, complaint includes "drawing room"

### Agent Gender
The agent is female. Feminine Hindi verb forms are used for self-reference (`kar sakti hoon`, `karungi`). Gender-neutral forms are used when addressing the customer (`kar rahe hain`).

---

## 7. Tool Call: save_customer_feedback

Defined in `app_config.json` under `tools.gemini`, executed in `mydoot_functions.py`.

### Tool Schema

```json
{
  "name": "save_customer_feedback",
  "description": "MANDATORY: Call immediately after collecting ALL 7 fields.",
  "parameters": {
    "type": "OBJECT",
    "properties": {
      "customer_name":      { "type": "STRING" },
      "brand":              { "type": "STRING" },
      "item":               { "type": "STRING" },
      "product_used_since": { "type": "STRING" },
      "usage_duration":     { "type": "STRING" },
      "warranty_status":    { "type": "STRING" },
      "complaint":          { "type": "STRING" }
    },
    "required": ["customer_name","brand","item","product_used_since",
                 "usage_duration","warranty_status","complaint"]
  }
}
```

### Execution Guards
- `save_executed` flag: if Gemini calls the tool a second time in the same session, the pipeline returns a synthetic `success: true` response without re-executing. This prevents duplicate Sheet rows.
- `caller_id` is added to the arguments by the pipeline before execution (not passed by Gemini).

### Google Sheets Write

```python
service.spreadsheets().values().append(
    spreadsheetId=SPREADSHEET_ID,
    range="Sheet1!A2",
    valueInputOption="RAW",
    insertDataOption="INSERT_ROWS",   # always appends new row, never overwrites
    body={"values": [[name, brand, item, since, duration, warranty,
                      complaint, timestamp, caller_id]]}
)
```

### Auto-Close
On successful save: `asyncio.create_task(_close_after(vobiz_ws, gemini_ws, 8.0))` closes both WebSockets 8 seconds later, allowing the confirmation message to finish.

---

## 8. Audio Tuning Parameters

All noise gate and audio pipeline parameters are configurable via environment variables — no code changes needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `NOISE_GATE_RMS` | `100` | Primary RMS threshold — packets below this are background noise |
| `NOISE_GATE_FALLBACK_RMS` | `20` | Fallback threshold for quiet speech / quiet environments |
| `NOISE_GATE_FALLBACK_AFTER_S` | `2.0` | Seconds after agent turn before fallback threshold activates |
| `NOISE_GATE_FALLBACK_FULL_DISABLE_S` | `7.0` | Seconds after which gate fully disables if still no speech |
| `NOISE_GATE_FALLBACK_FULL_COUNT` | `40` | Minimum noise-blocked packets before full disable |
| `NOISE_GATE_FALLBACK` | `1` | Set to `0` to disable the fallback mechanism entirely |
| `SPEECH_TAIL_SECS` | `0.4` | Seconds to keep forwarding after last speech packet |
| `SILENCE_SEND_SECS` | `5.0` | Seconds of injected silence after speech tail |
| `NOISE_GATE_DEBUG` | `0` | Set to `1` for per-packet RMS log (use for threshold tuning) |
| `RECORD_CALLS` | `0` | Set to `1` to save inbound PSTN audio as WAV per call |
| `RECORDINGS_DIR` | `recordings` | Directory for call recording WAV files |

**Tuning guidance:**
- If calls have long silences before agent responds: lower `NOISE_GATE_RMS`
- If background noise leaks through and Gemini responds to nothing: raise `NOISE_GATE_RMS`
- Use `NOISE_GATE_DEBUG=1` to see per-packet RMS values and find the right threshold
- Check `recordings/` WAV files with `test_asr_compare.py` to hear what Gemini is receiving

---

## 9. Transcript Logging

Transcripts are buffered per turn and flushed as single lines on turn boundaries:

- Agent speech chunks accumulate in `agent_buf` during a turn
- Customer speech chunks accumulate in `customer_buf`
- On `turnComplete`: agent buffer is flushed as one `"Agent: ..."` line
- When agent starts speaking: any pending customer buffer is flushed first as `"Customer: ..."` line
- Partial buffers are flushed in the `finally` block of `g_receiver`

All log lines are prefixed with `HH:MM:SS.mmm` timestamps and the caller ID. The full transcript is printed at call end before the email is sent.

---

## 10. State Engine

`core/state_engine.py` tracks which of the 7 fields have been collected. It is used only at tool call time — `set_data()` is called for each argument received in the `save_customer_feedback` tool call. The state engine does **not** inject prompts into the conversation during the call.

### Field Mapping

| State Engine Key | Tool Parameter | Sheet Column |
|-----------------|----------------|--------------|
| customer_name | customer_name | A: Customer Name |
| brand | brand | B: Brand |
| item | item | C: Item |
| product_used_since | product_used_since | D: Product Used Since |
| usage_duration | usage_duration | E: Usage Duration |
| warranty_status | warranty_status | F: Warranty Status |
| complaint | complaint | G: Complaint |
| _(auto)_ | _(timestamp)_ | H: Timestamp |
| _(auto)_ | caller_id | I: Caller ID |

---

## 11. Post-Call Email

```python
# pipelines/gemini.py — finally block (always runs, even on error)
await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
```

`transcript_log` contains `"Agent: ..."` and `"Customer: ..."` lines accumulated during the call via Gemini's transcription events:
- `inputAudioTranscription.text` → customer speech
- `outputAudioTranscription.text` → agent speech

Sent via Gmail SMTP SSL (port 465). Spaces are stripped from the App Password automatically. Sent to `GMAIL_USER` (admin email).

---

## 12. Call Recording

When `RECORD_CALLS=1`, the pipeline saves the inbound PSTN audio for each call:

```python
# Collected during the call:
pcm8_frames.append(audioop.ulaw2lin(raw_mulaw, 2))   # 8kHz PCM16

# Written in the finally block:
with wave.open(f"recordings/{caller_id}_{call_ts}.wav", "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)    # 16-bit PCM
    wf.setframerate(8000)
    wf.writeframes(b"".join(pcm8_frames))
```

Format: 8kHz mono 16-bit PCM WAV — this is the raw PSTN audio before any upsampling or filtering.

These files are used with `test_asr_compare.py` to benchmark ASR services on real phone call audio.

---

## 13. ASR Comparison Testing

`test_asr_compare.py` sends a recorded WAV file to three ASR services simultaneously and prints a side-by-side comparison of transcriptions and latency.

**Services tested:**

| Service | API | Hindi/Hinglish WER | Notes |
|---------|-----|-------------------|-------|
| Azure OpenAI Whisper | `ENDPINTS` + `AZURE_KEY1` | ~20% / ~29% | Batch only; India-hosted via southindia region |
| Sarvam Saaras V3 | `SARVAM_API_KEY` | **~8% / ~11.5%** | Best for Hindi; India servers |
| Deepgram Nova-2 Phonecall | `DEEPGRAM_API_KEY` | ~21% / ~32% | Good latency; US servers only |

**Usage:**
```bash
# Record a call first:
RECORD_CALLS=1   # in .env, restart server, make a call

# Run comparison:
python test_asr_compare.py
# or with specific file:
python test_asr_compare.py recordings/917971542939_20260601_143022.wav
```

---

## 14. Credentials Architecture

```
GitHub Secret: GCP_SA_KEY (full service account JSON)
        │
        └── deploy.yml sets GOOGLE_CREDENTIALS env var on Cloud Run
                │
                ▼
        app.py startup: parses JSON → writes google-credentials.json
                │
                ▼
        mydoot_functions._get_sheets_service()
        service_account.Credentials.from_service_account_info()
                │
                ▼
        Google Sheets API (sheets v4)
```

Service account: `mydoot-voice@testcnx-169610.iam.gserviceaccount.com`
Required permission: Editor on sheet `1uW39kklQKc4rhf5REATgKqgwbvSNAhlDVKXyAzOMKCk`

The Sheets service is cached after first initialization (`_CACHED_SERVICES`) and reused for all calls on the same instance.

---

## 15. Infrastructure

### Docker Image (Multi-stage)

```dockerfile
FROM python:3.11-slim AS builder
# pip install --prefix=/install -r requirements.txt

FROM python:3.11-slim
# Copies only: app.py, mydoot_functions.py, app_config.json,
#              config/, core/, pipelines/, routes/
# Excludes: .env, credentials, recordings, venv, test files
USER priya (non-root, uid 1000)
EXPOSE 8080
CMD ["python", "app.py"]
```

### Cloud Run

| Property | Value |
|----------|-------|
| Service | mydoot-voice-agent |
| Region | us-central1 |
| Project | testcnx-169610 |
| Memory | 512Mi |
| CPU | 1 |
| Min instances | 1 (always warm) |
| Max instances | 10 |
| Timeout | 3600s |
| Concurrency | 80 |

### CI/CD

```
git push main → GitHub Actions (deploy.yml)
    ├── Auth to GCP (GCP_SA_KEY)
    ├── Docker build + push to Artifact Registry
    ├── Write /tmp/env.yaml from GitHub secrets (mydoot_env environment)
    └── gcloud run deploy --env-vars-file=/tmp/env.yaml
```

---

## 16. Key Files Reference

| File | Purpose |
|------|---------|
| `app.py` | Entry point — registers routes, reconstructs google-credentials.json at startup |
| `app_config.json` | System prompt, greeting scripts, tool schema, model config |
| `mydoot_functions.py` | `save_customer_feedback()`, `send_call_summary_email()`, Sheets client |
| `pipelines/gemini.py` | Gemini Live WebSocket pipeline — audio I/O, noise gate, silence injection, echo guard, tool dispatch, transcript, call recording |
| `routes/webhook.py` | Vobiz inbound call handler — returns Stream XML with wss:// URL |
| `core/state_engine.py` | 7-field state tracker — used only at tool call time |
| `config/settings.py` | API keys and WebSocket URLs from environment variables |
| `Dockerfile` | Multi-stage build — builder + minimal runtime image |
| `.github/workflows/deploy.yml` | GitHub Actions → Cloud Run CI/CD pipeline |
| `test_asr_compare.py` | Offline ASR benchmarking — Azure / Sarvam / Deepgram on recorded WAV |
| `recordings/` | PSTN call WAV files (saved when `RECORD_CALLS=1`, gitignored) |
