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
│ VAD: RMS ≥ 100       │    │ → mu-law 8kHz           │
│ accumulate speech    │    │ → Vobiz playAudio        │
│ end on 0.3s silence  │    │ (blocked if barge_in_   │
│ min 0.3s utterance   │    │  active=True)           │
│                      │    │                         │
│ Barge-in: RMS ≥ 350  │    │ Tracks turnComplete:    │
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
│    - Appends row to Google Sheets (11 columns, A–K)                 │
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
┌─────────────────────────────────────────────────────────────────────┐
│ 7. Observability log written (non-blocking, asyncio.to_thread)      │
│    save_call_log() → appends one row to Call_Logs sheet tab         │
│    18 columns: timestamp, caller, duration, stage, saved,           │
│    category, subcategory, issue_type, customer_name, address,       │
│    preferred_time, stt_count, stt_avg_ms, stt_drops,               │
│    barge_ins, reconnects, audio_gcs, transcript                     │
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
  Speech frames accumulated; silence > 0.3s triggers flush (VAD_END_SECS, tunable via env var)
  Utterances < 0.3s discarded (noise blips)
        │
        ▼ Sarvam Saaras v3 REST  (POST /speech-to-text)
  Persistent aiohttp.ClientSession() per call — avoids TCP+TLS handshake per utterance (~200-300ms saved)
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
category → subcategory → diagnosis → brand* → address → preferred_time → customer_name → done
*brand stage is skipped for categories where needs_brand=False
```

The **diagnosis** stage replaces the old free-text `problem` stage. It injects category-specific questions from `DIAGNOSTIC_FLOWS` to identify the structured `issue_type` and auto-derive `severity`.

#### DIAGNOSTIC_FLOWS

`DIAGNOSTIC_FLOWS` is a dict in `core/service_graph.py` with entries for 20 subcategories:

```python
DIAGNOSTIC_FLOWS = {
    "TV / Television": {
        "issue_types": ["Power Failure", "Display Failure", "No Sound", "Remote Not Working", ...],
        "questions": ["Is the power indicator light glowing?", "Is there any picture on screen?"],
        "hints": "If no power light → Power Failure (High). If picture but no sound → No Sound (Low).",
        "severity_map": {"Power Failure": "High", "Display Failure": "High", "No Sound": "Low", ...}
    },
    "Refrigerator": { ... },
    "Washing Machine": { ... },
    "AC / Air Conditioner": { ... },
    "Water Purifier": { ... },
    # ... 15 more subcategories
}
```

Each entry drives the `[STAGE CONTEXT]` block for the diagnosis stage, telling Gemini which questions to ask and how to map answers to a specific `issue_type` and `severity`.

#### [STAGE CONTEXT] Injection

On each customer utterance, `service_graph.get_context()` prepends a block:

```
[STAGE CONTEXT — follow these instructions for this turn]
Stage       : diagnosis
Collected   : {"category": "Appliance Repair", "subcategory": "Refrigerator"}
Issue types : Cooling Failure, Water Leakage, Noisy Operation, Power Failure, Ice Build-up
Diagnosis Q : Is the compressor running (humming sound)? Is there any cooling at all?
Hints       : No cooling + compressor not running → Cooling Failure (High). Water below → Water Leakage (Medium).
Instruction : Ask ONE diagnosis question. Once issue is clear, set issue_type and move to brand stage.
[END STAGE CONTEXT]

Customer: fridge mein bilkul thanda nahi ho raha
```

This block tells Gemini exactly which stage it's in, what's collected, and what single question to ask next.

#### Confirmation Loop (Late-Stage Fields)

Each of the three late-stage fields — address, preferred_time, customer_name — goes through a two-step confirmation cycle before the stage advances:

```
1. Customer provides value  → set_pending(field, value)   [stage unchanged]
2. Gemini echoes the value  → "Sector 15, Noida, sahi hai?"
3. Customer confirms        → confirm_pending()            [stage advances]
   Customer corrects        → clear_pending() + set_pending(field, newValue) [re-confirm]
```

`ServiceGraph` stores the pending field/value in `self._pending`. `get_context()` returns `get_confirmation_context()` when pending is set — injecting a confirmation-specific `[STAGE CONTEXT]` block that tells Gemini exactly what to echo and when to advance. Only after customer confirms does `confirm_pending()` call `on_field_collected()` and advance the stage.

```python
# _stt_and_send() — late-stage with confirmation
if service_graph.pending is not None:
    if _is_confirmation(_clean_t):          # "haan", "sahi hai", "yes correct"
        service_graph.confirm_pending()     # commits field, advances stage
    else:
        service_graph.clear_pending()       # discard; re-capture if correction
        if pf == "address" and len(_words) >= 2:
            service_graph.set_pending("address", transcript.strip())
        elif pf == "preferred_time":
            service_graph.set_pending("preferred_time", transcript.strip())
elif _cur == "address" and len(_words) >= 2:
    service_graph.set_pending("address", transcript.strip())
# ... preferred_time, customer_name same pattern
```

On the customer_name confirmation turn, `confirm_pending()` calls `on_field_collected("customer_name", ...)` which advances stage to `"done"`. `get_context()` then returns the done instruction → Gemini calls `save_service_request` immediately on that same turn — no extra round-trip.

#### Agent Speech–Based Stage Advancement (Tool Hallucination Fix)

Keyword extraction in `_update_stage_from_customer()` only covers category and Vehicle Service subcategories. For all other categories (Electrical, Plumbing, Carpentry, Cleaning), the ServiceGraph stage stays stuck at `"subcategory"` the entire call — Gemini never receives `[STAGE CONTEXT: stage=done]` and sometimes generates the booking confirmation *without* calling `save_service_request`.

Fix: in `g_receiver()`, on every `turnComplete`, the agent's buffered speech is scanned with `_AGENT_STAGE_TRIGGERS` patterns. When Gemini asks for a specific field, the ServiceGraph stage is advanced to that field — independently of keyword extraction:

```python
_AGENT_STAGE_TRIGGERS = [
    ("address",        [r"\baddress\b", r"\bpata\b", r"\bsociety\b", ...]),
    ("preferred_time", [r"\bkab\b.{0,50}\b(aaye|visit|chahte|time)\b", ...]),
    ("customer_name",  [r"\bapna\s+naam\b", r"\byour\s+name\b", ...]),
]
# On turnComplete — after flushing agent_buf:
for _tgt, _pats in _AGENT_STAGE_TRIGGERS:
    if _tgt_idx > _cur_idx and any(re.search(p, agent_turn_text) for p in _pats):
        service_graph.state = ServiceState(**{**service_graph.state, "stage": _tgt})
        break
```

Combined flow (e.g. Electrical / Short Circuit — no keyword subcat patterns):
1. Agent asks "apna address batayein" → `turnComplete` → stage = `address`
2. Customer gives address → late-stage advancement → stage = `preferred_time`
3. Agent asks "kab aaye" → `turnComplete` → stage = `preferred_time` (confirmed)
4. Customer gives time → late-stage advancement → stage = `customer_name`
5. Agent asks "apna naam batayein" → `turnComplete` → stage = `customer_name` (confirmed)
6. Customer gives name → late-stage advancement → stage = `done`
7. Gemini receives `[STAGE CONTEXT: stage=done, ALL FIELDS]` → calls `save_service_request` reliably

#### State Updates

- `on_tool_call(args)`: called when `save_service_request` fires — merges all args into state, sets `stage = "done"`
- `on_field_collected(field, value)`: called mid-conversation to advance stage when a field is confirmed

---

## 5. Echo Guard, Barge-in, and Audio Blocking Logic

Five distinct audio control layers protect conversation integrity:

### Layer 1 — Greeting Guard (startup)
All customer audio is blocked until the first `turnComplete` event fires (`greeting_done` flag). Prevents background noise from interrupting the greeting. Safety release: force-released at 20 seconds.

### Layer 2 — Echo Guard (per turn)
After every `turnComplete`, a 0.3-second buffer blocks customer audio. Prevents Gemini's own audio being echoed back as customer speech. Safety timeout releases guard if `turnComplete` is missing for > 8 seconds after last audio.

### Layer 3 — Barge-in Guard (mid-agent-speech)
While the agent is actively speaking (`waiting_for_gemini=True` and recent AI audio), the VAD loop monitors for a sustained high-RMS signal:

```python
BARGE_IN_RMS_THRESHOLD = 350   # 3.5× higher than VAD threshold — filters fan/background noise
BARGE_IN_SUSTAIN_SECS  = 0.3   # sustained human speech, not a door slam or cough
```

When confirmed: `barge_in_active=True` is set → `{"event": "clear"}` is sent to Vobiz (stops audio playback) → `g_receiver` drops all subsequent Gemini audio chunks → accumulated frames are seeded into `speech_buf` (utterance start preserved) → `waiting_for_gemini=False` (utterance allowed through). Clears when next STT turn is sent to Gemini.

### Layer 4 — Post-Save Guard (end of call)
After `save_service_request` succeeds, ALL customer audio (from Vobiz) is blocked — `save_done_ts` is set and the VAD loop skips all packets.

### Layer 5 — Confirmation Audio Guard (close timing)
`confirmation_audio_secs` (float) accumulates PCM bytes of Gemini audio played after save. A `turnComplete` can only trigger call close when `confirmation_audio_secs >= 2.5` seconds. Prevents any premature close before the ~6s confirmation message completes.

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
      "issue_type":     { "type": "STRING", "description": "Structured fault label from DIAGNOSTIC_FLOWS, e.g. Cooling Failure, MCB Tripping" },
      "brand":          { "type": "STRING" },
      "model":          { "type": "STRING" },
      "severity":       { "type": "STRING", "description": "High / Medium / Low — auto-derived from issue_type" },
      "error_code":     { "type": "STRING", "description": "Appliance display error code if any, e.g. E3, F1" },
      "address":        { "type": "STRING" },
      "preferred_time": { "type": "STRING" }
    },
    "required": ["customer_name","category","subcategory","issue_type","address","preferred_time"]
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

After tool success, Gemini speaks the confirmation (Hinglish):

> *"[name] ji, aapki request register ho gayi hai. Hamari team jald se jald aapse sampark karegi. My Doot ko call karne ke liye shukriya!"*

Response time is left intentionally open ("jald se jald" — as soon as possible) in the voice message; the actual SLA is communicated separately by the dispatcher.

### Google Sheets Write

Columns A–K (11 total):

```python
service.spreadsheets().values().append(
    spreadsheetId=SPREADSHEET_ID,
    range="Sheet1!A2",
    valueInputOption="RAW",
    insertDataOption="INSERT_ROWS",
    body={"values": [[
        customer_name, category, subcategory, issue_type,
        brand, model, severity,
        address, preferred_time,
        timestamp, caller_id
    ]]}
)
```

#### Sheets Service Caching

`_get_sheets_service()` caches the service object in `_SHEETS_CACHE` with a 3000 s (50-min) TTL. The discovery-doc fetch + TCP/TLS handshake costs ~500 ms; caching ensures only the first save per warm Cloud Run instance pays this cost. On expiry or on a stale-connection error (`reset`, `eof`, `broken pipe`), the cache is invalidated and the service is rebuilt — the append is retried once automatically.

`_SHEETS_CACHE["headers_written"]` flag eliminates the ~300 ms `GET Sheet1!A1:K1` header check on every save after headers are confirmed present.

---

## 8. VAD — Voice Activity Detection

Local VAD runs before Sarvam STT. It eliminates PSTN line noise from ever reaching the ASR engine.

```python
VAD_SPEECH_THRESHOLD   = 100    # RMS amplitude gate for speech detection
VAD_END_SECS           = 0.3    # silence after speech to end utterance (tunable via env var)
VAD_MIN_SPEECH_SECS    = 0.3    # minimum utterance duration — catches short responses like "LG", "haan"
VAD_MAX_SPEECH_SECS    = 30.0   # hard ceiling — force-flush long utterances

BARGE_IN_RMS_THRESHOLD = 350    # high-RMS threshold: fan/background noise stays below this
BARGE_IN_SUSTAIN_SECS  = 0.3    # sustained duration to confirm human interruption vs. single loud noise
```

State machine per call:
```
in_speech=False + rms >= 100  → in_speech=True, start accumulating
in_speech=True  + silence > 0.3s → flush to Sarvam STT (if duration >= 0.3s)
in_speech=True  + duration >= 30s → force-flush to Sarvam STT

barge-in detection (agent speaking):
  rms >= 350 sustained >= 0.3s → clear Vobiz audio, barge_in_active=True
```

**Latency reductions (cumulative per turn):**

| Optimization | Savings |
|---|---|
| `VAD_END_SECS` 0.7 → 0.3 s | ~400 ms |
| `VAD_MIN_SPEECH_SECS` 0.5 → 0.3 s | prevents dropped short responses (saves re-ask round-trip) |
| Persistent `aiohttp.ClientSession()` per call (Sarvam STT) | ~200–300 ms |
| Cached Sheets service (TTL 3000 s) | ~500 ms per save |
| `headers_written` flag (skip GET on each save) | ~300 ms per save |
| Confirmation loop: confirm_pending() → stage=done on name turn | ~5–10 s saved vs extra round-trip |

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

The Sheets service is cached with a 3000 s TTL in `_SHEETS_CACHE` and reused for all calls on the same instance. On stale-connection errors the cache is invalidated and the service is force-rebuilt.

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

## 13. Observability Dashboard

Every call writes one row to the **Call_Logs** tab of the same Google Sheet via `save_call_log()` in `mydoot_functions.py`. The write is non-blocking (`asyncio.to_thread`) and fires in the `finally` block of `gemini_handler`.

### _call_track dict (per call)

`pipelines/gemini.py` maintains a mutable `_call_track` dict at `gemini_handler` scope:

```python
_call_track: dict = {
    "stt_latencies_ms": [],   # ms per successful Sarvam STT call
    "stt_dropped":      0,    # utterances rejected (concurrent or VAD drop)
    "barge_ins":        0,    # confirmed customer interruptions
    "reconnects":       0,    # Gemini WS reconnect events
    "gcs_uri":          "",   # gs:// URI after recording upload
}
```

Instrumentation hooks are placed at:
- After each successful STT call → `stt_latencies_ms.append(stt_ms)`
- After each STT concurrent-drop or VAD utterance drop → `stt_dropped += 1`
- After each confirmed barge-in → `barge_ins += 1`
- After each Gemini WS reconnect → `reconnects += 1`
- After GCS upload → `gcs_uri = gcs_uri`

### Dashboard Routes

| Route | Handler | Description |
|-------|---------|-------------|
| `GET /calls` | `routes/calls.py:calls_page` | HTML dashboard — stats, call table, expandable detail panels |
| `GET /calls/data` | `routes/calls.py:calls_data` | JSON — last 200 rows from Call_Logs sheet |
| `GET /calls/audio` | `routes/calls.py:audio_proxy` | Streams WAV from GCS to browser (query: `?uri=gs://...`) |

The dashboard renders each call's transcript as timestamped chat bubbles (Agent = purple/left, Customer = green/right). The audio player streams directly from GCS via the `/calls/audio` proxy.

### Call_Logs Sheet Schema

18 columns (A–R):

```
Timestamp (IST) | Caller ID | Duration (s) | Stage Reached | Saved |
Category | Subcategory | Issue Type | Customer Name | Address | Preferred Time |
STT Count | STT Avg (ms) | STT Drops | Barge-Ins | Reconnects | Audio GCS | Transcript
```

---

## 14. Key Files Reference

| File | Purpose |
|------|---------|
| `app.py` | Entry point — registers routes, reconstructs google-credentials.json at startup |
| `app_config.json` | System prompt, greeting scripts, tool schema (save_service_request), model config |
| `mydoot_functions.py` | `save_service_request()`, `save_call_log()`, `get_call_logs()`, `send_call_summary_email()`, `upload_recording_to_gcs()`, Sheets client |
| `pipelines/gemini.py` | Hybrid pipeline — VAD, Sarvam STT, ServiceGraph context injection, Gemini Live, tool dispatch |
| `routes/webhook.py` | Vobiz inbound call handler — returns Stream XML with wss:// URL |
| `routes/calls.py` | `GET /calls` HTML dashboard, `/calls/data` JSON API, `/calls/audio` GCS audio proxy |
| `core/service_graph.py` | LangGraph ServiceGraph — category taxonomy, stage state, [STAGE CONTEXT] injection |
| `core/state_engine.py` | Legacy 7-field state tracker (used by save_customer_feedback path) |
| `config/settings.py` | API keys (Gemini, Sarvam) and WebSocket URLs from environment variables |
| `Dockerfile` | Multi-stage build — builder + minimal runtime image |
| `.github/workflows/deploy.yml` | GitHub Actions → Cloud Run CI/CD pipeline |
