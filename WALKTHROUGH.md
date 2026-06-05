# Code Walkthrough: Mydoot Customer Care Voice Agent

This document explains the technical flow of the system, from the moment a customer calls +917971542939 to the moment their service request is saved in Google Sheets.

> **Active pipeline:** Gemini Live hybrid — Sarvam Saaras v3 STT + Google Gemini 2.5 Flash Native Audio (text-in / audio-out)

---

## 1. The Entry Point (`app.py`)

Everything starts here. `app.py` starts an `aiohttp` async web server on port `5050` and registers all routes.

**What it does at startup:**
- Reads `GOOGLE_CREDENTIALS` from the environment and writes it to `google-credentials.json` on disk (cloud deployments pass secrets as env vars, not files).
- Calls `init_db()` — opens a `ThreadedConnectionPool` to PostgreSQL (if `POSTGRES_URL` is set), runs the DDL to create `instances`, `service_requests`, and `call_logs` tables, and upserts the current `INSTANCE_ID` into `instances`. If `POSTGRES_URL` is unset, logs a warning and continues in Sheets-only mode.
- Registers the `/answer` webhook that Vobiz calls when a customer dials +917971542939.
- Registers the `/gemini-stream` WebSocket endpoint for the active hybrid pipeline.
- Serves the dashboard at `/` and the Voice Lab at `/voice-lab`.
- Registers /calls, /calls/data, and /calls/audio for the per-call observability dashboard.

**To switch the active pipeline**, change `active_provider` in `app_config.json` to `"sarvam"` or `"google"`.

---

## 2. The Inbound Call (`routes/webhook.py`)

When Vobiz receives a call on +917971542939, it makes a `POST /answer` request to this server.

The webhook:
1. Reads the `From` header to extract the caller's phone number (used as `caller_id`).
2. Checks `active_provider` in `app_config.json` to choose the pipeline.
3. Returns an XML `<Stream>` response that tells Vobiz to open a **bidirectional WebSocket** — audio flows both ways on this single connection to `/gemini-stream`.

From this point, the call is a live WebSocket session.

---

## 3. Conversation Orchestration (`core/service_graph.py`)

`ServiceGraph` wraps a LangGraph `StateGraph` and holds per-call `ServiceState`. It controls what the agent asks at every stage and injects a `[STAGE CONTEXT]` block into every Gemini turn.

### Stage Order

```
category → subcategory → diagnosis → brand* → address → preferred_time → customer_name → done
* brand stage is skipped for Plumbing, Electrical, Carpentry, Cleaning, and simple Vehicle services
```

### What Each Stage Does

| Stage | What Gemini is told to ask / do |
|-------|----------------------------------|
| `category` | Detect service type from customer description (Appliance Repair, Plumbing, Electrical, Carpentry, Cleaning, Vehicle Service, Other) |
| `subcategory` | Identify specific type within category (e.g. Refrigerator, Pipe Leak, Wiring) |
| `diagnosis` | Ask 2–3 targeted fault questions from `DIAGNOSTIC_FLOWS`; identify `issue_type` and `severity` |
| `brand` | Ask brand name — only for Appliance Repair and Vehicle Service (Car/Bike repair) |
| `address` | Collect society name + area/locality for the technician |
| `preferred_time` | When does the customer want the technician to visit |
| `customer_name` | Collect name — always last |
| `done` | All fields collected: say wait message, call `save_service_request` tool |

### Simple Services Fast Path

Certain subcategories have self-evident issue types requiring no diagnosis and no brand:

```python
AUTO_ISSUE_TYPES = {
    "Car Wash / Detailing": ("Car Wash / Detailing", "Low"),
    "Tyre Change":          ("Tyre Change",          "Medium"),
    "Battery Replacement":  ("Battery Replacement",  "High"),
    "Home / Deep Cleaning": ("Full Home Cleaning",   "Low"),
    # ... all Cleaning subcategories
}
```

When one of these subcategories is detected, `advance_stage()` skips diagnosis and brand entirely — stage jumps directly to `address`.

### [STAGE CONTEXT] Block

On every customer utterance, `service_graph.get_context()` prepends:

```
[STAGE CONTEXT — follow these instructions for this turn]
Stage       : diagnosis
Collected   : {"category": "Appliance Repair", "subcategory": "Refrigerator"}
Instruction : Subcategory: 'Refrigerator'. Now run DIAGNOSTIC to identify the issue type.
              Possible issue types: Cooling Failure / Water Leakage / Compressor Noise / ...
              Ask these diagnostic questions (one at a time, skip if already answered):
              Are both fridge and freezer warm, or only one? | Do you hear compressor running?
              Routing hints: Cooling Failure: not cooling at all... Water Leakage: dripping inside...
              Once issue type is clear, record issue_type (and severity if obvious) then proceed.
[END STAGE CONTEXT]

Customer: fridge mein bilkul thanda nahi ho raha
```

This block tells Gemini exactly which stage it's in, what's collected, and what single question to ask next. Gemini handles all language generation, Hinglish/English switching, and voice output.

### Late-Stage Field Advancement (Latency Optimization)

After each STT transcript, `pipelines/gemini.py` calls `on_field_collected()` for the current late-stage field (address / preferred_time / customer_name) **before** calling `get_context()`. This means:

- When the customer gives their name, Gemini sees `stage=done` with ALL FIELDS in its `[STAGE CONTEXT]` on the **same turn** → calls `save_service_request` immediately without needing an extra reasoning round-trip.
- Without this, stage would stay at `address` for all remaining turns and Gemini would spend 5–10s reasoning "are all fields collected?" before calling the tool.

### Agent Speech–Based Stage Advancement (Tool Hallucination Fix)

`_SUBCAT_PATTERNS` only covers Vehicle Service subcategories. For Electrical, Plumbing, Carpentry, and Cleaning calls, the ServiceGraph stage stays stuck at `"subcategory"` the entire call. Gemini then never receives `[STAGE CONTEXT: stage=done]`, and occasionally generates the booking confirmation *without* calling the `save_service_request` tool (hallucination — the row never reaches Google Sheets).

Fix: in `g_receiver()`, on every `turnComplete`, the agent's speech for that turn is scanned with `_AGENT_STAGE_TRIGGERS` patterns. When Gemini's words indicate it just asked for a specific field, the ServiceGraph stage is advanced immediately:

| Agent says | Stage set to |
|---|---|
| "apna address batayein" / "pata" / "society" / "locality" | `address` |
| "kab aaye" / "kab visit" / "samay batayein" / "preferred time" | `preferred_time` |
| "apna naam batayein" / "your name" | `customer_name` |

This feeds correctly into the late-stage field advancement above: address question → stage=`address` → customer answers → stage=`preferred_time` → ... → customer gives name → stage=`done` → Gemini receives the explicit "call tool now" instruction reliably.

---

## 4. The Hybrid Voice Pipeline (`pipelines/gemini.py`)

This is the Listen → Think → Speak loop for the entire call duration.

### Listen: VAD + Sarvam Saaras v3 STT

```
Vobiz WebSocket (mu-law 8kHz)
        │
        ▼ audioop.ulaw2lin → PCM 8kHz, 16-bit
        │
        ▼ Local VAD (RMS threshold = 100)
          in_speech=False + rms >= 100 → start accumulating
          in_speech=True  + silence > 0.3s → flush to Sarvam STT
          utterance < 0.3s → discard (reject noise blips)
        │
        ▼ Sarvam Saaras v3 REST API (POST, hi-IN, 8kHz WAV)
          Persistent aiohttp.ClientSession() per call — reuses TCP connection
        │
        ▼ Text transcript → _stt_and_send()
```

Customer audio is **never** sent raw to Gemini. Only clean text transcripts go to the LLM, eliminating hallucinations caused by PSTN line noise.

**`waiting_for_gemini` flag:** Set `True` after each `clientContent` send. Dropped utterances while the flag is set prevent the 1008 "policy violation" WebSocket error caused by sending two turns before Gemini responds. Barge-in resets this to `False` so the interrupting utterance is not dropped.

### Think + Speak: Gemini Live

```
_stt_and_send():
  1. ServiceGraph keyword extraction → advance category/subcategory
  2. Late-stage confirmation loop (address / preferred_time / customer_name):
       If service_graph.pending is set:
         → _is_confirmation(text)? → confirm_pending() → stage advances
         → else (correction)      → clear_pending(); re-capture new value as pending
       Else (no pending yet):
         → set_pending(field, value) — stage stays; Gemini will echo for confirmation
  3. service_graph.get_context() → [STAGE CONTEXT] block
       (returns confirmation context if pending, otherwise regular stage context)
  4. Gemini clientContent turn:
     { "clientContent": { "turns": [{ "role": "user", "parts": [{ "text": "[STAGE CONTEXT]\n\nCustomer: ..." }] }], "turnComplete": true } }
  5. waiting_for_gemini = True

g_receiver() — runs in parallel:
  Gemini serverContent.modelTurn.parts[].inlineData
  └── PCM 24kHz base64
        ▼ audioop.ratecv(24000 → 8000)
        ▼ audioop.lin2ulaw
        ▼ Vobiz playAudio (mu-law 8kHz)

  On turnComplete:
  └── flush agent transcript buffer
  └── waiting_for_gemini = False (next customer utterance can proceed)
```

Gemini handles all LLM reasoning, language generation, Hinglish/English detection, and TTS (Aoede voice — warm, clear female).

### Tool Call: save_service_request

When Gemini has all required fields and stage context shows `done`, it fires the tool:

```python
# Tool handler in gemini.py
if fn == "save_service_request":
    service_graph.on_tool_call(args)          # merge all args into ServiceState
    args.setdefault("caller_id", caller_id)   # pipeline injects caller phone number

res = await asyncio.to_thread(FUNCTION_MAP[fn], **args)   # non-blocking Sheets write

if res.get("success"):
    save_executed = True                   # one-save-per-session guard
    save_done_ts  = time.time()            # activates post-save audio block
    asyncio.create_task(_close_after(ws, g_ws, 15.0))  # fallback close timer
```

### Barge-in Detection

When the customer starts speaking while the agent is talking, barge-in stops the agent's audio immediately:

```
VAD loop (while agent is speaking):
  rms >= BARGE_IN_RMS_THRESHOLD (350) for >= BARGE_IN_SUSTAIN_SECS (0.3s)
        │
        ▼  Confirmed human interruption (not fan/background noise)
  barge_in_active = True
  {"event": "clear"} → Vobiz (stops audio playback on caller's phone)
  waiting_for_gemini = False (allow new utterance through)
  barge-in frames → speech_buf (utterance start not lost)
        │
  g_receiver: all new Gemini audio chunks are dropped while barge_in_active=True
        │
  Customer utterance → STT → Gemini → barge_in_active = False (on send)
```

**Two-threshold design**: VAD threshold (100 RMS) catches all speech. Barge-in threshold (350 RMS) is 3.5× higher — fan hum, TV, and ambient noise stay below 350; a person speaking into a phone handset exceeds it. The 0.3s sustain requirement further rejects transient loud sounds (door slams, coughs).

### Audio Blocking Guards

Five layers prevent audio from disrupting the conversation:

| Guard | Trigger | Effect |
|-------|---------|--------|
| **Greeting guard** | startup | Block all customer audio until first `turnComplete` fires (greeting done) |
| **Echo guard** | per turn | 0.3s silence buffer after `turnComplete` — prevents agent audio echoing back |
| **Barge-in guard** | `barge_in_active=True` | Drop Gemini audio chunks after customer interrupts — prevents overlap |
| **Concurrent-send guard** | `waiting_for_gemini=True` | Drop new VAD utterances while Gemini is processing — prevents 1008 errors |
| **Post-save guard** | `save_done_ts > 0` | Block ALL customer audio after save — call is ending |

---

## 5. The Service Request Save (`mydoot_functions.py`)

### `save_service_request()`

Called from the Gemini pipeline tool handler. Dual-writes to PostgreSQL (primary) and Google Sheets (secondary):

**PostgreSQL (primary):**
```python
conn = get_conn()  # borrow from ThreadedConnectionPool
cur.execute(INSERT INTO service_requests (...) VALUES (...))
conn.commit()
put_conn(conn)
```
Rows are tagged with `INSTANCE_ID` for multi-tenancy. If the pool is not initialized (`POSTGRES_URL` unset), skips silently.

**Google Sheets (secondary, soft-fail):**
```
Sheet1 columns (A–K):
Customer Name | Category | Subcategory | Issue Type | Brand | Model | Severity | Address | Preferred Time | Timestamp | Caller ID
```
If the PostgreSQL write already succeeded, a Sheets failure is non-fatal — returns `{"success": True}` based on `pg_ok`.

**Caching:** `_get_sheets_service()` caches the service object with a 3000s (50-min) TTL. The discovery-doc fetch + TCP handshake costs ~500ms; caching ensures only the first call per warm instance pays this cost.

**Header check:** `_SHEETS_CACHE["headers_written"]` flag — once headers are confirmed written, the ~300ms `GET Sheet1!A1:K1` check is skipped on subsequent saves.

**Stale-connection retry:** On any connection error (`reset`, `eof`, `broken pipe`), the cache is invalidated and the append is retried once with a fresh service.

### `send_call_summary_email()`

Called in the `finally` block of `gemini_handler` — runs **after every call** whether it completed, dropped, or errored. Sends the full `Agent: ...` / `Customer: ...` transcript to `GMAIL_USER` via Gmail SMTP SSL (port 465).

Sent to GMAIL_USER (admin email).

### `save_call_log()`

Called in the `finally` block of `gemini_handler` — runs **after every call** (completed or dropped). Dual-writes to PostgreSQL `call_logs` table (primary) and **Call_Logs** sheet tab (secondary) with 18 columns:

```python
asyncio.create_task(asyncio.to_thread(
    save_call_log,
    caller_id, duration_secs, stage_reached, saved,
    category, subcategory, issue_type, customer_name,
    address, preferred_time,
    stt_count, stt_avg_ms, stt_drops, barge_ins, reconnects,
    audio_gcs, transcript,
))
```

The call is **non-blocking** (`asyncio.to_thread` + `create_task`) — the `finally` block does not wait for the Sheet write to complete before returning.

`_ensure_call_logs_sheet()` runs once per process to create the Call_Logs tab and write headers if missing.

### `get_call_logs(n)`

Returns the last `n` rows from Call_Logs as a list of dicts (newest first). Called by `routes/calls.py:calls_data()` for the JSON API.

---

## 6. Call Flow: End to End

```
Customer dials +917971542939
        │
        ▼ Vobiz POST /answer
routes/webhook.py → returns XML <Stream wss://.../gemini-stream>
        │
        ▼ Vobiz opens bidirectional WebSocket
pipelines/gemini.py → gemini_handler()
  - Opens Gemini Live WebSocket (BidiGenerateContent)
  - Sends setup: model, systemInstruction, save_service_request tool schema
  - Instantiates ServiceGraph()
        │
        ▼ Gemini speaks Hinglish greeting (one of 3 scripts, random)
Agent: "Namaskar! Main माई डूट Customer Care se bol rahi hoon..."
        │
        ▼ customer speaks
VAD accumulates → Sarvam STT → "mere fridge mein paani aa raha hai"
        │
        ▼ keyword extraction
ServiceGraph: category=Appliance Repair, subcategory=Refrigerator
[STAGE CONTEXT: stage=diagnosis, Collected: {category, subcategory}]
Gemini asks ONE diagnosis question: "Kya cooling bhi band ho gayi hai?"
        │
        ▼ customer answers diagnosis questions
[STAGE CONTEXT: stage=brand] → Gemini: "Aapka fridge kaunsi company ka hai?"
        │
        ▼ customer says "Samsung"
[STAGE CONTEXT: stage=address] → Gemini: "Aapka address kya hai?"
        │
        ▼ customer gives address → set_pending("address", "Sector 15, Noida")
[STAGE CONTEXT: confirming address] → Gemini: "Sector 15, Noida, sahi hai?"
        │
        ▼ customer says "haan" → confirm_pending() → stage=preferred_time
[STAGE CONTEXT: stage=preferred_time] → Gemini: "Aap technician ko kab bulana chahte hain?"
        │
        ▼ customer gives time → set_pending("preferred_time", "kal subah 10 baje")
[STAGE CONTEXT: confirming preferred_time] → Gemini: "Kal subah 10 baje, sahi hai?"
        │
        ▼ customer says "haan" → confirm_pending() → stage=customer_name
[STAGE CONTEXT: stage=customer_name] → Gemini: "Aapka naam kya hai?"
        │
        ▼ customer says name → set_pending("customer_name", "Alok Ranjan")
[STAGE CONTEXT: confirming name] → Gemini: "Alok Ranjan, sahi hai?"
        │
        ▼ customer says "haan" → confirm_pending() → stage=done
[STAGE CONTEXT: stage=done, Collected: {ALL FIELDS}]
Gemini calls save_service_request() IMMEDIATELY — no wait message before tool call
        │
        ▼ Google Sheets: new row appended (11 columns)
Gemini: "[name] ji, aapki request register ho gayi hai. Hamari team jald se jald aapse sampark karegi. MyDoot ko call karne ke liye shukriya!"
        │
        ▼ Call auto-closes after confirmation audio completes
        │
        ▼ finally block → send_call_summary_email() → Gmail SMTP
Transcript emailed to admin
```

---

## 7. How to Modify the Agent

No code changes needed for most customizations — edit `app_config.json`:

| What to Change | Where in `app_config.json` |
|---|---|
| Greeting messages (3 scripts) | `scripts.greetings` array |
| System prompt (language rules, stage instructions, persona) | `agent.system_prompt` |
| Switch pipeline (Sarvam ↔ Gemini) | `active_provider` |
| Gemini model / temperature | `parameters.google.model` / `temperature` |
| Tool schema for save_service_request | `tools.gemini[0].function_declarations[0]` |

To add a new service category or subcategory, update `CATEGORIES` in `core/service_graph.py`.
To add a new diagnostic flow, add an entry to `DIAGNOSTIC_FLOWS` in the same file.

---

## 8. Observability Dashboard

The `/calls` dashboard shows per-call quality data from the Call_Logs Google Sheet.

### How data flows in

`pipelines/gemini.py` maintains a `_call_track` dict per call:

```python
_call_track = {
    "stt_latencies_ms": [],  # ms per Sarvam STT call
    "stt_dropped":      0,   # utterances rejected
    "barge_ins":        0,   # customer interruptions
    "reconnects":       0,   # Gemini WS drops+reconnects
    "gcs_uri":          "",  # gs:// URI of recording
}
```

Five instrumentation hooks write to this dict during the call. At call end (`finally` block), `save_call_log()` is called with the accumulated data.

### Routes

| URL | What it shows |
|-----|---------------|
| `/calls` | HTML dashboard — 6 summary stat cards + sortable call table |
| `/calls/data` | JSON — last 200 rows from Call_Logs sheet |
| `/calls/audio?uri=gs://...` | Streams WAV from GCS to the browser audio player |

### Expanded row layout

Clicking any row expands a two-column detail panel:
- **Left**: audio player (loads the WAV from GCS via `/calls/audio` proxy)
- **Right**: timestamped transcript as chat bubbles
  - Agent turns: purple, left-aligned
  - Customer turns: green, right-aligned
  - Each bubble shows `[HH:MM:SS.mmm] · Role` above the text

The audio player is always visible when a recording exists — no extra click required. Audio pauses automatically when the row is collapsed.

### Navigation

- From the main dashboard (`/`): click **Call Logs** in the Monitoring section
- From the Call Logs page: click **← Dashboard** button (top-right) to return
