# Technical Architecture: Mydoot Customer Care Voice Agent

**Agent Name:** Mydoot Customer Care Representative
**Active Pipeline:** Google Gemini Live (native audio)
**Stack:** Gemini 2.5 Flash Native Audio, Vobiz SIP, Google Sheets, Gmail SMTP, Google Cloud Run

---

## 1. System Overview

Mydoot Customer Care is an AI voice agent that handles inbound phone calls on the Vobiz SIP platform. It uses Google Gemini Live for end-to-end audio understanding and synthesis (no separate STT/TTS steps), collects structured complaint data conversationally in Hinglish, and persists results to Google Sheets.

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
│    Sends setup: model, speechConfig, systemInstruction, tools       │
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
│ → ratecv(8k→16k)     │    │ → lin2ulaw              │
│ → PCM 16kHz          │    │ → mu-law 8kHz           │
│ → Gemini realtimeIn  │    │ → Vobiz playAudio       │
│                      │    │                         │
│ ECHO GUARD:          │    │ Tracks turnComplete:    │
│ Block while          │    │ gemini_speaking=True    │
│ gemini_speaking=True │    │ until turnComplete msg  │
│ + 1.5s after end     │    │ then gemini_speaking=F  │
└──────────────────────┘    └────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. Tool Call: save_customer_feedback                                │
│    Triggered when Gemini has all 7 fields                           │
│    Handler: mydoot_functions.py                                     │
│    - Appends row to Google Sheets                                   │
│    - Returns success message to Gemini                              │
│    - Gemini speaks confirmation to customer                         │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 6. Call ends (customer hangs up or agent says goodbye)              │
│    finally block → send_call_summary_email()                        │
│    Gmail SMTP SSL → transcript email to admin                       │
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
        ▼ audioop.ratecv(pcm8, 2, 1, 8000, 16000, state)
  Linear PCM, 16kHz, mono, 16-bit
        │
        ▼ base64.b64encode()
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

### Echo Guard

Prevents Gemini's own audio from being echoed back as customer input (which would confuse VAD and cause stuck calls or infinite loops):

```python
# In g_receiver (asyncio task):
if part has audio:
    gemini_speaking = True

if serverContent.turnComplete:
    gemini_speaking = False
    gemini_turn_end_ts = time.time()

# In Vobiz audio forwarding loop:
if gemini_speaking:
    continue  # Drop customer audio while agent is speaking
if time.time() - gemini_turn_end_ts < 1.5:
    continue  # 1.5s buffer after turn ends
```

---

## 4. Gemini Live Configuration

```json
{
  "setup": {
    "model": "models/gemini-2.5-flash-native-audio-latest",
    "generationConfig": {
      "responseModalities": ["AUDIO"],
      "speechConfig": {
        "voiceConfig": {
          "prebuiltVoiceConfig": { "voiceName": "Aoede" }
        }
      }
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
| Voice | Aoede | Warm, clear female voice |
| Modalities | AUDIO only | No text output needed |
| Transcription | Both directions | For post-call transcript email |
| ping_interval | 20s | Prevents mid-call WebSocket timeout |
| ping_timeout | 20s | Drops dead connections quickly |

---

## 5. Data Collection State Machine

`core/state_engine.py` tracks which of the 7 fields have been collected and injects a status summary into every Gemini system prompt turn.

### States

```
COLLECTING_COMPLAINT → COLLECTING_NAME → COLLECTING_BRAND →
COLLECTING_ITEM → COLLECTING_PRODUCT_SINCE → COLLECTING_USAGE →
COLLECTING_WARRANTY → COMPLETED
```

### Prompt Injection (appended to every system prompt)

```
CURRENT STATE: COLLECTING_WARRANTY
COLLECTED: complaint, customer_name, brand, item, product_used_since, usage_duration
STILL NEED: warranty_status
```

When all 7 collected:
```
ALL 7 FIELDS COLLECTED — call save_customer_feedback NOW.
```

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
| _(auto)_ | caller_id | I: Caller ID |
| _(auto)_ | _(timestamp)_ | H: Timestamp |

---

## 6. Tool Call: save_customer_feedback

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

### Google Sheets Write

```python
service.spreadsheets().values().append(
    spreadsheetId=SPREADSHEET_ID,
    range="Sheet1!A2",
    valueInputOption="RAW",
    insertDataOption="INSERT_ROWS",   # always appends new row
    body={"values": [[name, brand, item, since, duration, warranty,
                      complaint, timestamp, caller_id]]}
)
```

---

## 7. Credentials Architecture

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

---

## 8. Post-Call Email

```python
# pipelines/gemini.py — finally block (always runs, even on error)
await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
```

`transcript_log` is built during the call from Gemini's transcription events:
- `inputAudioTranscription.text` → `"Customer: ..."` lines
- `outputAudioTranscription.text` → `"Agent: ..."` lines

Sends via Gmail SMTP SSL (port 465). Spaces stripped from App Password automatically.

---

## 9. Infrastructure

### Docker Image (Multi-stage)

```dockerfile
FROM python:3.11-slim AS builder
# pip install --prefix=/install -r requirements.txt

FROM python:3.11-slim
# Copies only: app.py, mydoot_functions.py, app_config.json,
#              config/, core/, pipelines/, routes/, metrics/
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

## 10. Key Files Reference

| File | Purpose |
|------|---------|
| `app.py` | Entry point — registers routes, reconstructs google-credentials.json at startup |
| `app_config.json` | System prompt, greeting scripts, tool schema, model/voice config |
| `mydoot_functions.py` | `save_customer_feedback()`, `send_call_summary_email()`, Sheets client |
| `pipelines/gemini.py` | Gemini Live WebSocket pipeline — audio I/O, echo guard, tool dispatch |
| `routes/webhook.py` | Vobiz inbound call handler — returns Stream XML with wss:// URL |
| `core/state_engine.py` | 7-field state machine — tracks collected fields, generates prompt injection |
| `config/settings.py` | API keys and WebSocket URLs from environment variables |
| `Dockerfile` | Multi-stage build — builder + minimal runtime image |
| `.github/workflows/deploy.yml` | GitHub Actions → Cloud Run CI/CD pipeline |
