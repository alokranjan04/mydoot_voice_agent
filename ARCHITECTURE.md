# Technical Architecture: Mydoot Customer Care Voice Agent

**Agent Name:** Mydoot Customer Care Representative
**Active Pipeline:** Google Gemini Live (native audio)
**Stack:** Gemini 2.5 Flash Native Audio, Vobiz SIP, Google Sheets, Gmail SMTP, Google Cloud Run

---

## 1. System Overview

Mydoot Customer Care is an AI voice agent that handles inbound phone calls on the Vobiz SIP platform. It uses Google Gemini Live for end-to-end audio understanding and synthesis (no separate STT/TTS steps), collects structured complaint data conversationally in English or Hinglish based on the customer's language preference, and persists results to Google Sheets.

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
│ → ratecv(8k→16k)     │    │ → lin2ulaw              │
│ → PCM 16kHz          │    │ → mu-law 8kHz           │
│ → Gemini realtimeIn  │    │ → Vobiz playAudio       │
│                      │    │                         │
│ BLOCKED until        │    │ Tracks turnComplete:    │
│ greeting_done=True   │    │ flushes transcript buf  │
│ (first turnComplete) │    │ sets gemini_turn_end_ts │
│                      │    │                         │
│ Echo guard:          │    │                         │
│ 1.0s after each      │    │                         │
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

---

## 4. Echo Guard and Audio Blocking Logic

Three distinct audio blocking layers prevent feedback loops and protect the confirmation message:

### Layer 1 — Greeting Guard (startup)
All customer audio is blocked until the first `turnComplete` event fires (`greeting_done` flag). This ensures background noise on the line cannot interrupt the greeting before it finishes. Safety release: if `turnComplete` never arrives within 20 seconds, the guard is force-released.

### Layer 2 — Echo Guard (per turn)
After every `turnComplete`, a 1.0-second buffer blocks customer audio. This prevents the tail end of Gemini's own audio being echoed back as customer speech. A safety timeout releases the guard if `turnComplete` is missing for more than 8 seconds after audio was last sent.

### Layer 3 — Post-Save Guard (end of call)
After `save_customer_feedback` succeeds, ALL customer audio is blocked for 15 seconds. This ensures the confirmation message plays completely without interruption before the call closes.

```python
# Precedence order (highest to lowest):
# 1. Post-save guard: save_done_ts set → block for 15s
# 2. Greeting guard: greeting_done=False → block
# 3. Echo guard: now - gemini_turn_end_ts < 1.0 → block
# 4. Forward packet to Gemini
```

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

### Language Selection (Step 1)
On `[CALL_STARTED]`, the agent delivers a bilingual greeting (one of 3 random options, each spoken in both English and Hindi) and asks the customer their language preference:
- Customer says "English" or responds in clear English → entire call in English
- Customer says "Hindi", responds in Hindi, or response is unclear/mixed → entire call in Hinglish
- Default is Hinglish when in doubt

### Field Collection Order (Step 2)
After language selection, fields are collected in this fixed order:

| Step | Field(s) | Notes |
|------|----------|-------|
| 1 | complaint + device | First question after language: "Which appliance has a problem and what's wrong?" |
| 2 | brand | Skipped if mentioned anywhere in prior speech |
| 3 | item | Skipped if mentioned anywhere in prior speech |
| 4 | product_used_since + usage_duration | Single question: "How long have you been using it?" — fills both fields |
| 5 | warranty_status | Enum value selected from three options |
| 6 | customer_name | Always collected LAST |

### Extract, Don't Re-Ask
The agent extracts information from any point in the conversation. If a customer mentions brand, device, or name at any point, those fields are never asked again. Examples:
- "my LG TV is not working" → brand=LG, item=TV, no further questions on these
- "MacBook" → brand=Apple, item=MacBook Laptop
- "I'm Rohit" → customer_name=Rohit

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

## 8. Transcript Logging

Transcripts are buffered per turn and flushed as single lines on turn boundaries:

- Agent speech chunks accumulate in `agent_buf` during a turn
- Customer speech chunks accumulate in `customer_buf`
- On `turnComplete`: agent buffer is flushed as one `"Agent: ..."` line
- When agent starts speaking: any pending customer buffer is flushed first as `"Customer: ..."` line
- Partial buffers are flushed in the `finally` block of `g_receiver`

All log lines are prefixed with `HH:MM:SS.mmm` timestamps and the caller ID. The full transcript is printed at call end before the email is sent.

---

## 9. State Engine

`core/state_engine.py` tracks which of the 7 fields have been collected. It is used only at tool call time — `set_data()` is called for each argument received in the `save_customer_feedback` tool call. The state engine does **not** inject prompts into the conversation during the call (prompt injection was removed as it caused the agent to re-ask already-answered fields).

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

## 10. Post-Call Email

```python
# pipelines/gemini.py — finally block (always runs, even on error)
await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
```

`transcript_log` contains `"Agent: ..."` and `"Customer: ..."` lines accumulated during the call via Gemini's transcription events:
- `inputAudioTranscription.text` → customer speech
- `outputAudioTranscription.text` → agent speech

Sent via Gmail SMTP SSL (port 465). Spaces are stripped from the App Password automatically. Sent to `GMAIL_USER` (admin email).

---

## 11. Credentials Architecture

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

## 12. Infrastructure

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

## 13. Key Files Reference

| File | Purpose |
|------|---------|
| `app.py` | Entry point — registers routes, reconstructs google-credentials.json at startup |
| `app_config.json` | System prompt, greeting scripts, tool schema, model config |
| `mydoot_functions.py` | `save_customer_feedback()`, `send_call_summary_email()`, Sheets client |
| `pipelines/gemini.py` | Gemini Live WebSocket pipeline — audio I/O, echo guard, tool dispatch, transcript |
| `routes/webhook.py` | Vobiz inbound call handler — returns Stream XML with wss:// URL |
| `core/state_engine.py` | 7-field state tracker — used only at tool call time |
| `config/settings.py` | API keys and WebSocket URLs from environment variables |
| `Dockerfile` | Multi-stage build — builder + minimal runtime image |
| `.github/workflows/deploy.yml` | GitHub Actions → Cloud Run CI/CD pipeline |
