# Technical Architecture: Mydoot Customer Care Voice Agent

**Agent Name:** Mydoot Customer Care Representative
**Active Pipeline:** Hybrid — Sarvam Saaras v3 STT + Gemini Live LLM+TTS
**Orchestration:** LangGraph ServiceGraph
**Stack:** Gemini 2.5 Flash Native Audio, Sarvam Saaras v3, LangGraph, Vobiz SIP, Google Sheets, Gmail SMTP, Google Cloud Run

---

## 1. System Overview

Mydoot Customer Care is an AI voice agent that handles inbound phone calls on the Vobiz SIP platform. It uses a hybrid pipeline:
- **Sarvam Saaras v3** (REST API) for speech-to-text — transcribes customer audio with local VAD pre-filtering to reject PSTN line noise
- **Google Gemini Live** (text-in, audio-out) for LLM reasoning and TTS voice synthesis
- **LangGraph ServiceGraph** for structured conversation orchestration — injects stage-specific context into each Gemini turn

Customer audio is never sent raw to Gemini. Only clean text transcripts are sent, eliminating hallucinations caused by line noise.

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
│    Mode: text clientContent in, audio out (no realtimeInput)        │
│    Instantiates: ServiceGraph() per call                            │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
           ┌──────────────┴──────────────┐
           │                             │
           ▼                             ▼
┌──────────────────────┐    ┌────────────────────────┐
│ Audio IN loop        │    │ g_receiver task         │
│ (Vobiz → VAD → STT)  │    │ (Gemini → Vobiz)        │
│                      │    │                         │
│ mu-law 8kHz          │    │ PCM 24kHz               │
│ → ulaw2lin → PCM 8k  │    │ → ratecv(24000→8000)    │
│                      │    │ → lin2ulaw              │
│ VAD: RMS threshold   │    │ → mu-law 8kHz           │
│ accumulate speech    │    │ → Vobiz playAudio        │
│ end on 0.7s silence  │    │                         │
│ min 0.3s utterance   │    │ Tracks turnComplete:    │
│                      │    │ flushes transcript buf  │
│ Sarvam Saaras v3     │    │ sets gemini_turn_end_ts │
│ REST → transcript    │    │                         │
│                      │    │ confirmation_audio_secs │
│ ServiceGraph.        │    │ accumulates post-save   │
│ get_context() inject │    │ audio (float, not bool) │
│ → clientContent text │    │                         │
│ → Gemini Live        │    │                         │
└──────────────────────┘    └────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. Tool Call: save_service_request                                  │
│    Triggered when Gemini has all required fields                    │
│    service_graph.on_tool_call(args) → state = done                 │
│    Handler: mydoot_functions.py                                     │
│    - Appends row to Google Sheets (10 columns)                      │
│    - Returns success to Gemini                                      │
│    - Gemini speaks confirmation once, then goes silent              │
│    - Call closes after confirmation audio ≥ 2.5s completes         │
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

### Inbound (Customer Voice → Sarvam STT → Gemini text)

```
Vobiz WebSocket frame
  └── event: "media"
  └── payload: base64(mu-law PCM, 8kHz, mono)
        │
        ▼ audioop.ulaw2lin(data, 2)
  Linear PCM, 8kHz, mono, 16-bit
        │
        ▼ Local VAD (RMS threshold = 100)
  Speech frames accumulated; silence > 0.7s triggers flush
  Utterances < 0.3s discarded (noise blips)
        │
        ▼ Sarvam Saaras v3 REST  (POST /speech-to-text)
  model=saaras:v3 | language_code=hi-IN | file=audio.wav (8kHz WAV)
        │
        ▼ Transcript string (Hinglish / English)
        │
        ▼ ServiceGraph.get_context() → [STAGE CONTEXT] block
        │
        ▼ Gemini clientContent { turns: [{ role: "user", parts: [{ text: ... }] }] }
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

## 4. LangGraph Conversation Orchestration

### ServiceGraph (`core/service_graph.py`)

`ServiceGraph` wraps a LangGraph `StateGraph` and holds per-call `ServiceState`. It tracks the current stage and injects context into every Gemini turn.

#### Service Taxonomy

```python
CATEGORIES = {
    "Appliance Repair": {
        "subcategories": ["Refrigerator", "AC / Air Conditioner", "Washing Machine",
                          "TV / Television", "Geyser", "Microwave Oven", "Laptop / Computer",
                          "Inverter / UPS", "Water Purifier", "Mixer / Grinder", "Other Appliance"],
        "needs_brand": True,
    },
    "Plumbing":      { "subcategories": [...], "needs_brand": False },
    "Electrical":    { "subcategories": [...], "needs_brand": False },
    "Carpentry":     { "subcategories": [...], "needs_brand": False },
    "Cleaning":      { "subcategories": [...], "needs_brand": False },
    "Vehicle Service": { "subcategories": [...], "needs_brand": True  },
    "Other":         { "subcategories": [],     "needs_brand": False },
}
```

#### Stage Routing

```
category → subcategory → problem → brand* → address → preferred_time → customer_name → done
*brand stage is skipped for categories where needs_brand=False
```

#### [STAGE CONTEXT] Injection

On each customer utterance, `service_graph.get_context()` prepends a block:

```
[STAGE CONTEXT — follow these instructions for this turn]
Stage       : address
Collected   : {"category": "Plumbing", "subcategory": "Pipe Leak", "problem": "Water leaking from bathroom pipe"}
Instruction : ASK: What is your address? We need your society name and area/locality to send a technician.
[END STAGE CONTEXT]

Customer: ghar mein pipe se paani aa raha hai
```

This block tells Gemini exactly which stage it's in, what's collected, and what single question to ask next.

#### State Updates

- `on_tool_call(args)`: called when `save_service_request` fires — merges all args into state, sets `stage = "done"`
- `on_field_collected(field, value)`: call mid-conversation to advance stage when a field is confirmed

---

## 5. Echo Guard and Audio Blocking Logic

Four distinct audio control layers protect conversation integrity:

### Layer 1 — Greeting Guard (startup)
All customer audio is blocked until the first `turnComplete` event fires (`greeting_done` flag). Prevents background noise from interrupting the greeting. Safety release: force-released at 20 seconds.

### Layer 2 — Echo Guard (per turn)
After every `turnComplete`, a 0.3-second buffer blocks customer audio. Prevents Gemini's own audio being echoed back as customer speech. Safety timeout releases guard if `turnComplete` is missing for > 8 seconds after last audio.

### Layer 3 — Post-Save Guard (end of call)
After `save_service_request` succeeds, ALL customer audio (from Vobiz) is blocked — `save_done_ts` is set and the VAD loop skips all packets.

### Layer 4 — Confirmation Audio Guard (close timing)
`confirmation_audio_secs` (float) accumulates PCM bytes of Gemini audio played after save. A `turnComplete` can only trigger call close when `confirmation_audio_secs >= 2.5` seconds. This prevents the wait-message ("Ek second...") ~2s `turnComplete` from prematurely closing the call before the ~6s confirmation plays.

```
Fallback close: asyncio.create_task(_close_after(ws, g_ws, 15.0)) is always created on save,
in case the confirmation turnComplete never fires (Gemini silent after save).
```

---

## 6. Gemini Live Configuration

```json
{
  "setup": {
    "model": "models/gemini-2.5-flash-native-audio-latest",
    "generationConfig": {
      "responseModalities": ["AUDIO"]
    },
    "outputAudioTranscription": {},
    "systemInstruction": { "parts": [{ "text": "<system_prompt>" }] },
    "tools": [{ "function_declarations": [{ "name": "save_service_request", ... }] }]
  }
}
```

| Setting | Value | Reason |
|---------|-------|--------|
| Model | gemini-2.5-flash-native-audio-latest | Native TTS voice (Aoede) |
| Modalities | AUDIO only | Text output not needed |
| Input mode | clientContent text turns | Audio never sent to Gemini — Sarvam handles STT |
| inputAudioTranscription | Not set | No audio input to transcribe |
| outputAudioTranscription | Set | Captures agent speech for transcript email |
| speechConfig | Not set | Causes deferred 1008 errors on this model |
| VAD config | Not set | Causes 1008 policy violations on native audio model |
| ping_interval | 20s | Prevents mid-call WebSocket timeout |
| ping_timeout | 20s | Drops dead connections quickly |

---

## 7. Tool Call: save_service_request

Defined in `app_config.json` under `tools.gemini`, executed in `mydoot_functions.py`.

### Tool Schema

```json
{
  "name": "save_service_request",
  "description": "MANDATORY: Call when [STAGE CONTEXT] shows stage 'done'.",
  "parameters": {
    "type": "OBJECT",
    "properties": {
      "customer_name":  { "type": "STRING" },
      "category":       { "type": "STRING" },
      "subcategory":    { "type": "STRING" },
      "problem":        { "type": "STRING" },
      "brand":          { "type": "STRING" },
      "model":          { "type": "STRING" },
      "address":        { "type": "STRING" },
      "preferred_time": { "type": "STRING" }
    },
    "required": ["customer_name","category","subcategory","problem","address","preferred_time"]
  }
}
```

### Execution Flow

```python
# pipelines/gemini.py — tool handler
if fn == "save_service_request":
    service_graph.on_tool_call(args)          # merge args into ServiceState
    args.setdefault("caller_id", caller_id)   # injected by pipeline

res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)

if res.get("success"):
    save_executed = True                       # duplicate-save guard
    save_done_ts  = time.time()               # activates post-save audio guard
    asyncio.create_task(_close_after(ws, g_ws, 15.0, log))  # fallback close
```

### Google Sheets Write

```python
service.spreadsheets().values().append(
    spreadsheetId=SPREADSHEET_ID,
    range="Sheet1!A2",
    valueInputOption="RAW",
    insertDataOption="INSERT_ROWS",
    body={"values": [[
        customer_name, category, subcategory, problem,
        brand, model, address, preferred_time,
        timestamp, caller_id
    ]]}
)
```

---

## 8. VAD — Voice Activity Detection

Local VAD runs before Sarvam STT. It eliminates PSTN line noise from ever reaching the ASR engine.

```python
VAD_SPEECH_THRESHOLD = 100    # RMS amplitude gate
VAD_END_SECS         = 0.7    # silence after speech to end utterance
VAD_MIN_SPEECH_SECS  = 0.3    # minimum utterance duration (reject noise blips)
VAD_MAX_SPEECH_SECS  = 30.0   # hard ceiling — force-flush long utterances
```

State machine per call:
```
in_speech=False + rms >= threshold → in_speech=True, start accumulating
in_speech=True  + silence > 0.7s  → flush to Sarvam STT (if duration >= 0.3s)
in_speech=True  + duration >= 30s → force-flush to Sarvam STT
```

---

## 9. Transcript Logging

Transcripts are buffered per turn and flushed as single lines on turn boundaries:

- Customer speech (from STT): logged immediately in `_stt_and_send()` as `"Customer: <transcript>"`
- Agent speech: buffered in `agent_buf` via `outputAudioTranscription` events; flushed as one `"Agent: ..."` line on `turnComplete`
- All log lines are timestamped `HH:MM:SS.mmm` and prefixed with caller ID
- Full transcript printed at call end; emailed to admin

---

## 10. Post-Call Email

```python
# pipelines/gemini.py — finally block (always runs, even on error)
await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
```

`transcript_log` contains `"Agent: ..."` and `"Customer: ..."` lines accumulated during the call.

Sent via Gmail SMTP SSL (port 465). Sent to `GMAIL_USER` (admin email).

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
| `app_config.json` | System prompt, greeting scripts, tool schema (save_service_request), model config |
| `mydoot_functions.py` | `save_service_request()`, `save_customer_feedback()`, `send_call_summary_email()`, Sheets client |
| `pipelines/gemini.py` | Hybrid pipeline — VAD, Sarvam STT, ServiceGraph context injection, Gemini Live, tool dispatch |
| `routes/webhook.py` | Vobiz inbound call handler — returns Stream XML with wss:// URL |
| `core/service_graph.py` | LangGraph ServiceGraph — category taxonomy, stage state, [STAGE CONTEXT] injection |
| `core/state_engine.py` | Legacy 7-field state tracker (used by save_customer_feedback path) |
| `config/settings.py` | API keys (Gemini, Sarvam) and WebSocket URLs from environment variables |
| `Dockerfile` | Multi-stage build — builder + minimal runtime image |
| `.github/workflows/deploy.yml` | GitHub Actions → Cloud Run CI/CD pipeline |
