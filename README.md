# Mydoot Customer Care — AI Voice Agent

> **Every service request deserves a real response, instantly.**
> Mydoot Customer Care is a production-grade AI voice agent that answers inbound calls 24/7, collects structured home service request data through a guided bilingual conversation, and logs every interaction directly to Google Sheets — no human agent required.

---

## What It Does

A customer calls **+917971542939**. The AI agent:

1. Answers immediately with a Hinglish greeting, then auto-detects language (English or Hinglish)
2. Conducts the entire call in English if the customer responds in English, otherwise Hinglish
3. Identifies the service category (Appliance Repair, Plumbing, Electrical, Carpentry, Cleaning, Vehicle Service, or Other) and guides through subcategory, problem, address, and preferred visit time
4. Saves a structured 10-field service request to Google Sheets
5. Emails the full call transcript to the admin after every call

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
        ├── Audio In:  mu-law 8kHz → PCM 8kHz
        │               ↓  Local VAD (RMS threshold)
        │               ↓  Sarvam Saaras v3 REST (hi-IN)
        │               ↓  Text transcript → clientContent turn
        │
        ├── Audio Out: Gemini Live PCM 24kHz → mu-law 8kHz → Vobiz
        │
        ├── core/service_graph.py — LangGraph ServiceGraph
        │               ↓  Injects [STAGE CONTEXT] with each turn
        │               ↓  Tracks: category → subcategory → problem
        │                          → brand → address → preferred_time
        │                          → customer_name → done
        ▼
 Gemini Live (gemini-2.5-flash-native-audio-latest)
 • LLM + TTS in one model (text-in / audio-out)
 • Voice: Aoede (warm, clear female)
 • Language: English or Hinglish (auto-detected)
        │
        │  When all fields collected:
        ▼
 save_service_request() tool call
        │
        ├── Google Sheets API ──► Append row to Sheet1 (10 columns)
        └── Gmail SMTP ──────────► Send transcript email to admin (after call)
```

---

## Call Flow

```
[CALL_STARTED] → Agent speaks Hinglish greeting (1 of 3, random)
      ↓
Customer responds → Sarvam Saaras v3 STT transcribes
      ↓
[STAGE CONTEXT] injected → Gemini guided through stages:
  1. category      — detect service type from customer description
  2. subcategory   — specific type (e.g. Refrigerator, Pipe Leak, Wiring)
  3. problem       — what exactly is wrong / what work is needed
  4. brand         — only for Appliance Repair and Vehicle Service
  5. address       — society name + area/locality for technician visit
  6. preferred_time — when to send the technician
  7. customer_name — collected LAST
      ↓
All fields collected → save_service_request() tool call
      ↓
Agent speaks confirmation ONCE, then goes silent
      ↓
Call auto-closes after confirmation audio completes
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
| 4 | Problem | Water leaking from bathroom pipe since 2 days |
| 5 | Brand | Samsung *(Appliance/Vehicle only)* |
| 6 | Model | *(optional)* |
| 7 | Address | Sector 15, Noida |
| 8 | Preferred Time | kal subah 10 baje |
| 9 | Timestamp | *(auto)* |
| 10 | Caller ID | *(auto)* |

### Google Sheet Columns (A–J)

```
Customer Name | Category | Subcategory | Problem | Brand | Model | Address | Preferred Time | Timestamp | Caller ID
```

Sheet: [mydoot_Customer_Care](https://docs.google.com/spreadsheets/d/1uW39kklQKc4rhf5REATgKqgwbvSNAhlDVKXyAzOMKCk)

---

## Project Structure

```
mydoot-voice-agent/
├── app.py                  # Entry point — registers routes, starts server
├── app_config.json         # Agent persona, system prompt, tool schemas, greetings
├── mydoot_functions.py     # save_service_request() + save_customer_feedback() + Gmail email
├── requirements.txt
├── Dockerfile
│
├── config/
│   └── settings.py         # API keys, URLs loaded from env
│
├── core/
│   ├── state_engine.py     # Legacy 7-field state tracker
│   └── service_graph.py    # LangGraph ServiceGraph — category taxonomy + stage context injection
│
├── pipelines/
│   ├── gemini.py           # Hybrid pipeline: Sarvam STT + Gemini Live LLM+TTS
│   └── sarvam.py           # Sarvam pipeline (backup)
│
├── routes/
│   └── webhook.py          # POST /answer — Vobiz inbound call handler
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
- Sarvam AI API key (for STT)

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
| `SARVAM_API_KEY` | Yes | Sarvam AI API key (for Saaras v3 STT) |
| `GOOGLE_CREDENTIALS` | Yes | GCP service account JSON (full key) |
| `GOOGLE_SPREADSHEET_ID` | Yes | Target Google Sheet ID |
| `GMAIL_USER` | Yes | Gmail address for transcript emails |
| `GMAIL_APP_PASSWORD` | Yes | Gmail App Password (16-char, spaces OK) |
| `PUBLIC_URL` | Yes | Public HTTPS URL (for Vobiz webhook) |
| `PORT` | No | Server port (default: 5050) |
| `RECORD_CALLS` | No | Set to `1` to save inbound audio as WAV files |
| `GCS_RECORDINGS_BUCKET` | No | GCS bucket for WAV upload |

---

## Deployment (CI/CD)

Every push to `main` auto-deploys to Google Cloud Run via GitHub Actions.

### GitHub Secrets Required (in `mydoot_env` environment)

| Secret | Value |
|--------|-------|
| `GCP_SA_KEY` | Full GCP service account JSON |
| `GEMINI_API_KEY` | Gemini API key |
| `SARVAM_API_KEY` | Sarvam AI API key |
| `GOOGLE_SPREADSHEET_ID` | Sheet ID |
| `GMAIL_USER` | Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password |
| `PUBLIC_URL` | Cloud Run service URL |

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

## Built By

**Alok Ranjan** — [alokranjan04@gmail.com](mailto:alokranjan04@gmail.com)

Powered by Google Gemini Live · Sarvam Saaras v3 STT · LangGraph · Vobiz SIP · Google Cloud Run · GitHub Actions
