# Mydoot Customer Care — AI Voice Agent

> **Every service request deserves a real response, instantly.**
> Mydoot Customer Care is a production-grade AI voice agent that answers inbound calls 24/7, collects structured home service request data through a guided bilingual conversation, and logs every interaction directly to Google Sheets — no human agent required.

---

## What It Does

A customer calls **+917971542939**. The AI agent:

1. Answers immediately with a Hinglish greeting, then asks the customer to choose Hindi or English
2. Conducts the entire call in the language the customer selected
3. Identifies the service category (Appliance Repair, Plumbing, Electrical, Carpentry, Cleaning, Vehicle Service, or Other) and guides through subcategory, structured diagnosis (issue type + severity), address, and preferred visit time
4. **Confirms each collected field** by echoing it back ("Sector 15, Noida, sahi hai?") — only advances after the customer confirms; corrects if the customer gives a different value. High-confidence values (≥0.85) are auto-confirmed to save time.
5. Supports **barge-in** — if the customer speaks while the agent is talking, agent audio stops immediately (RMS threshold filters out background noise/fan sounds)
6. **Records every call** as stereo WAV and uploads to GCS bucket `mydootrecordings` (RECORD_CALLS=1)
7. Saves a structured 11-field service request to **PostgreSQL** (primary) and **Google Sheets** (secondary)
8. Emails the full call transcript to the admin after every call
9. **Structured Cloud Logging** via `config/cloud_logging.py` — JSON logs, log-based metrics, Cloud Monitoring integration
10. **Observability dashboard** at `/calls` — per-call quality metrics, turn latency drill-down, transcript viewer

No hold music. No missed calls. No incomplete forms.

---

## System Architecture

```
Customer Phone Call
        │
        ▼
   Vobiz SIP Trunk
   (+917971542939)
        │  POST /answer
        ▼
 Cloud Run Service
 (mydoot-voice-agent)
        │
        ├── routes/webhook.py ──► Returns WebSocket URL (wss://...)
        │
        │  Bidirectional WebSocket (mu-law 8kHz audio)
        ▼
 pipelines/gemini.py
        │
        ├── Audio In:  mu-law 8kHz → PCM 16kHz → realtimeInput.audio
        │               (Gemini native audio — no separate STT)
        │
        ├── Audio Out: Gemini Live PCM 24kHz → mu-law 8kHz → Vobiz
        │
        ├── core/service_graph.py — LangGraph ServiceGraph
        │               ↓  Compressed system prompt (~620 tokens)
        │               ↓  Tracks: category → subcategory → diagnosis
        │                          → brand → address → preferred_time
        │                          → customer_name → done
        │               ↓  DIAGNOSTIC_FLOWS: 20-subcategory fault tree
        │                  (issue_type, severity, error_code)
        │
        ├── config/cloud_logging.py — Structured JSON logging
        │               ↓  Cloud Logging + Cloud Monitoring (log-based metrics)
        │
        ├── Call Recording — stereo WAV → GCS bucket mydootrecordings
        │
        ├── Reconnect on code=1000 — handles Gemini clean-close mid-call
        ▼
 Gemini Live (gemini-3.1-flash-live-preview)
 • STT + LLM + TTS all-in-one (native audio mode, NATIVE_AUDIO_INPUT=1)
 • Voice: Aoede (slow, sweet voice style)
 • Language: Hindi or English (customer chooses at start of call)
        │
        │  When all fields collected:
        ▼
 save_service_request() tool call
        │
        ├── PostgreSQL (primary) ──► service_requests + call_logs tables
        ├── Google Sheets (secondary) ──► Append row to Sheet1 (11 columns)
        └── Gmail SMTP ──────────► Send transcript email to admin (after call)
```

---

## Call Flow

```
[CALL_STARTED] → Agent speaks Hinglish greeting (1 of 3, random)
      ↓
Agent asks: "Hindi mein baat karein ya English mein?"
      ↓
Customer chooses language → Gemini continues in that language
      ↓
Gemini guided through stages (native audio — no separate STT):
  1. category      — detect service type from customer description
  2. subcategory   — specific type (e.g. Refrigerator, Pipe Leak, Wiring)
  3. diagnosis     — structured fault diagnosis: DIAGNOSTIC_FLOWS injects
                     per-subcategory questions to identify issue_type,
                     severity (High/Medium/Low), and optional error_code
                     *(skipped for Car Wash, Tyre Change, Battery Replacement, all Cleaning)*
  4. brand         — only for Appliance Repair and Car/Bike Service
                     *(skipped for Plumbing, Electrical, Carpentry, Cleaning, Car Wash, Tyre Change, Battery)*
  5. address       — customer provides → agent echoes "X, sahi hai?" → customer confirms
  6. preferred_time — customer provides → agent echoes → customer confirms
  7. customer_name — collected LAST; same confirm loop
      ↓
Each of steps 5–7 uses a two-step confirmation cycle:
  Customer provides value → agent echoes it → customer confirms → stage advances
  (If customer corrects: new value is echoed and confirmed before advancing)
      ↓
Customer confirms name → save_service_request() tool call IMMEDIATELY
      ↓
Agent: "[name] ji, aapki request register ho gayi hai. Hamari team jald se jald aapse sampark karegi."
      ↓
Agent goes silent (end-marker detection + 9s byte cap + 5s drain delay)
      ↓
Call auto-closes after confirmation audio completes
      ↓
Stereo WAV recording uploaded to GCS (mydootrecordings bucket)
      ↓
Transcript emailed to admin (always, even on dropped calls)
```

---

## Services Handled

The agent handles **any home or office service request** across these categories:

| Category | Examples |
|----------|---------|
| **Appliance Repair** | Refrigerator, AC, Washing Machine, TV, Geyser, Laptop, Microwave, Water Purifier |
| **Plumbing** | Pipe Leak, Tap/Faucet, Water Tank, Toilet, Seelan/Dampness, Waterproofing |
| **Electrical** | Wiring, MCB/Fuse, Fan Fitting, Switch/Socket, Short Circuit |
| **Carpentry** | Door/Window Repair, Furniture, Wardrobe, Lock/Hinge |
| **Cleaning** | Home/Deep Cleaning, AC Deep Clean, Sofa/Carpet, Pest Control |
| **Vehicle Service** | Car/Bike Repair, Tyre Change, Battery, Car Wash |
| **Other** | Any other service |

---

## Data Collected

### Service Request Fields

| # | Field | Example |
|---|-------|---------|
| 1 | Customer Name | Kumud Ranjan |
| 2 | Category | Plumbing |
| 3 | Subcategory | Pipe Leak |
| 4 | Issue Type | Water Leakage Indoor |
| 5 | Brand | Samsung *(Appliance/Vehicle only)* |
| 6 | Model | *(optional)* |
| 7 | Severity | High / Medium / Low *(auto-derived)* |
| 8 | Address | Sector 15, Noida |
| 9 | Preferred Time | kal subah 10 baje |
| 10 | Timestamp | *(auto)* |
| 11 | Caller ID | *(auto)* |

### Google Sheet Columns (A–K)

```
Customer Name | Category | Subcategory | Issue Type | Brand | Model | Severity | Address | Preferred Time | Timestamp | Caller ID
```

Sheet: [mydoot_Customer_Care](https://docs.google.com/spreadsheets/d/1uW39kklQKc4rhf5REATgKqgwbvSNAhlDVKXyAzOMKCk)

---

## Project Structure

```
mydoot-voice-agent/
├── app.py                  # Entry point — registers routes, starts server
├── app_config.json         # Agent persona, system prompt, tool schemas, greetings
├── mydoot_functions.py     # save_service_request(), save_call_log(), get_call_logs(), send_call_summary_email(), upload_recording_to_gcs(), Sheets client
├── requirements.txt
├── Dockerfile
│
├── config/
│   ├── settings.py         # API keys, URLs, POSTGRES_URL, INSTANCE_ID loaded from env
│   ├── database.py         # PostgreSQL ThreadedConnectionPool — init_db(), get_conn(), put_conn()
│   └── cloud_logging.py    # Structured JSON logging — Cloud Logging, Cloud Monitoring (log-based metrics)
│
├── core/
│   ├── state_engine.py     # Legacy 7-field state tracker
│   └── service_graph.py    # LangGraph ServiceGraph — category taxonomy + stage context injection
│
├── pipelines/
│   ├── gemini.py           # Native audio pipeline: Gemini 3.1 STT+LLM+TTS (NATIVE_AUDIO_INPUT=1)
│   └── sarvam.py           # Sarvam pipeline (legacy fallback)
│
├── routes/
│   ├── webhook.py          # POST /answer — Vobiz inbound call handler
│   ├── dashboard.py        # GET / — pipeline selector + model config dashboard
│   ├── calls.py            # GET /calls — observability dashboard; /calls/data JSON; /calls/audio GCS proxy
│   ├── metrics.py          # GET /metrics — legacy metrics dashboard
│   ├── voice_lab.py        # GET /voice-lab — test interface
│   └── uploads.py          # POST /api/upload, GET /api/files, POST /api/delete-file
│
└── .github/workflows/
    └── deploy.yml          # GitHub Actions → Cloud Run CI/CD
```

---

## Setup

### Prerequisites
- Python 3.11+
- GCP project with Cloud Run enabled
- Vobiz SIP account
- Google service account with Sheets Editor access
- Gmail account with App Password enabled
- Sarvam AI API key (optional — only needed if NATIVE_AUDIO_INPUT=0 fallback)

### Local Development

```bash
git clone https://github.com/alokranjan04/mydoot_voice_agent.git
cd mydoot_voice_agent
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env         # fill in your keys
python app.py
```

Server starts on `http://localhost:5050`

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Google AI Studio API key |
| `SARVAM_API_KEY` | No | Sarvam AI API key (only for legacy STT fallback, NATIVE_AUDIO_INPUT=0) |
| `GOOGLE_CREDENTIALS` | Yes | GCP service account JSON (full key) |
| `GOOGLE_SPREADSHEET_ID` | Yes | Target Google Sheet ID |
| `GMAIL_USER` | Yes | Gmail address for transcript emails |
| `GMAIL_APP_PASSWORD` | Yes | Gmail App Password (16-char, spaces OK) |
| `PUBLIC_URL` | Yes | Public HTTPS URL (for Vobiz webhook) |
| `POSTGRES_URL` | No | PostgreSQL DSN — enables dual-write to PG + Sheets (`postgresql://user:pass@host/db`) |
| `INSTANCE_ID` | No | Tenant identifier — tags all PG rows (default: `"default"`) |
| `PORT` | No | Server port (default: 5050) |
| `NATIVE_AUDIO_INPUT` | No | Set to `1` (default) for Gemini native audio; `0` for legacy Sarvam STT path |
| `RECORD_CALLS` | No | Set to `1` to save stereo WAV recordings and upload to GCS |
| `GCS_RECORDINGS_BUCKET` | No | GCS bucket for WAV upload (default: `mydootrecordings`) |
| `GCS_DELETE_LOCAL` | No | Set to 1 to delete local WAV after GCS upload |

---

## Deployment (CI/CD)

Every push to `main` auto-deploys to Google Cloud Run via GitHub Actions.

### GitHub Secrets Required (in `mydoot_env` environment)

| Secret | Value |
|--------|-------|
| `GCP_SA_KEY` | Full GCP service account JSON |
| `GEMINI_API_KEY` | Gemini API key (paid tier required for Live API) |
| `SARVAM_API_KEY` | Sarvam AI API key (optional — legacy STT fallback only) |
| `GOOGLE_SPREADSHEET_ID` | Sheet ID |
| `GMAIL_USER` | Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password |
| `PUBLIC_URL` | Cloud Run service URL |
| `POSTGRES_URL` | PostgreSQL DSN (optional — enables dual-write) |
| `INSTANCE_ID` | Tenant identifier for multi-tenancy (optional) |

### Cloud Run Configuration

| Setting | Value |
|---------|-------|
| Region | us-central1 |
| Project | testcnx-169610 |
| Memory | 512Mi |
| CPU | 1 |
| Min instances | 1 (always warm) |
| Max instances | 10 |
| Timeout | 3600s |
| Port | 8080 |

---

## Vobiz Configuration

1. Log in to your Vobiz account
2. Set the **Answer URL** for +917971542939 to:
   ```
   POST https://<your-cloud-run-url>/answer
   ```
3. The agent handles all inbound calls automatically

---

## Post-Call Email

After every call (including dropped/incomplete calls), an email is sent to `GMAIL_USER` with:
- Caller ID (phone number)
- Call timestamp
- Full conversation transcript (Agent + Customer turns)

---

## Observability Dashboard

Every call is logged to **both PostgreSQL** (`call_logs` table) and the **Call_Logs tab** in Google Sheets, with 18 columns each. PostgreSQL is the primary store; Sheets is secondary (soft-fail). If `POSTGRES_URL` is unset, Sheets-only mode is used.

| Column | Field |
|--------|-------|
| Timestamp (IST) | Call start time |
| Caller ID | Phone number |
| Duration (s) | Call length in seconds |
| Stage Reached | Last conversation stage completed |
| Saved | Whether save_service_request succeeded |
| Category / Subcategory / Issue Type | Structured service request data |
| Customer Name / Address / Preferred Time | Collected fields |
| STT Count / STT Avg (ms) | STT call count and average latency (legacy path only) |
| STT Drops / Barge-Ins / Reconnects | Audio quality signals |
| Audio GCS | `gs://` URI of the call recording (if RECORD_CALLS=1) |
| Transcript | Full timestamped Agent + Customer conversation |

The dashboard is available at `<your-url>/calls` — shows summary stats, a per-call table with expandable rows, timestamped chat-bubble transcript, and an in-browser audio player that streams the recording directly from GCS.

---

## Cost Per Minute

| Component | Rate | Per Minute |
|-----------|------|------------|
| **Gemini 3.1 Flash Live API** (STT + LLM + TTS all-in-one) | $0.0023/min | **₹0.19** |
| **Vobiz Telephony** (incoming PSTN calls) | ₹0.45/min | **₹0.45** |
| **Total** | | **₹0.64/min** (~$0.0077) |

**Per call (avg 1.5 min):** ₹0.96 (~$0.012)

Notes:
- Gemini Live API pricing: input $0.00025/1K tokens + output $0.001/1K tokens; audio ≈ 40 tokens/sec → $0.0023/min blended
- Sarvam STT cost eliminated by switching to Gemini native audio input (was $0.0077/min with Deepgram)
- Google Sheets API and PostgreSQL writes are negligible (free tier / fixed cost)
- Cloud Run: ~$0.00002/min (512Mi, 1 vCPU, pay-per-use after min instances)

---

## Built By

**Alok Ranjan** — [alokranjan04@gmail.com](mailto:alokranjan04@gmail.com)

Powered by Google Gemini 3.1 Flash Live (native audio) · LangGraph · Vobiz SIP · PostgreSQL · Cloud Logging · Google Cloud Run · GitHub Actions
