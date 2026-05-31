# Mydoot Customer Care — AI Voice Agent

> **Every customer complaint deserves a real response, instantly.**
> Mydoot Customer Care is a production-grade AI voice agent that answers inbound calls 24/7, collects structured appliance complaint data in natural Hinglish, and logs every interaction directly to Google Sheets — no human agent required.

---

## What It Does

A customer calls **+917971542939**. The AI agent:

1. Answers immediately with a warm, empathetic Hinglish greeting
2. Understands the complaint in the customer's own words
3. Collects 7 structured fields conversationally
4. Saves the complete record to Google Sheets
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
        ├── Audio In:  mu-law 8kHz → PCM 16kHz → Gemini Live
        ├── Audio Out: Gemini Live PCM 24kHz → mu-law 8kHz → Vobiz
        │
        ▼
 Gemini Live (gemini-2.5-flash-native-audio-latest)
 • Native STT + LLM + TTS in one model
 • Voice: Aoede (warm, clear female)
 • Language: Hinglish
        │
        │  When all 7 fields collected:
        ▼
 save_customer_feedback() tool call
        │
        ├── Google Sheets API ──► Append row to mydoot_Customer_Care sheet
        └── Gmail SMTP ──────────► Send transcript email to admin
```

---

## Data Collected

The agent collects **7 fields** for every complaint:

| # | Field | Example |
|---|-------|---------|
| 1 | Customer Name | Kumud Ranjan |
| 2 | Brand | HP, Samsung, Apple, LG |
| 3 | Item (device) | Laptop, TV, Refrigerator, MacBook |
| 4 | Product Used Since | 2022, 3 saal pehle |
| 5 | Usage Duration | 3 saal, 6 mahine |
| 6 | Warranty Status | Yes - Under Warranty / No - Out of Warranty / Customer Does Not Know |
| 7 | Complaint | Free-text description |

### Google Sheet Columns (A–I)

```
Customer Name | Brand | Item | Product Used Since | Usage Duration | Warranty Status | Complaint | Timestamp | Caller ID
```

Sheet: [mydoot_Customer_Care](https://docs.google.com/spreadsheets/d/1uW39kklQKc4rhf5REATgKqgwbvSNAhlDVKXyAzOMKCk)

---

## Supported Devices

The agent accepts **any appliance or device** — there is no restricted list:
- Home appliances: TV, Refrigerator, Washing Machine, AC, Geyser, Microwave
- Electronics: Laptop, MacBook, Desktop, Tablet, iPhone
- Kitchen: Mixer, Grinder, Water Purifier
- Power: Inverter, UPS
- And anything else a customer might have

---

## Project Structure

```
mydoot-voice-agent/
├── app.py                  # Entry point — registers routes, starts server
├── app_config.json         # Agent persona, system prompt, tool schemas, greetings
├── mydoot_functions.py     # save_customer_feedback() + Gmail transcript email
├── requirements.txt
├── Dockerfile
│
├── config/
│   └── settings.py         # API keys, URLs loaded from env
│
├── core/
│   └── state_engine.py     # 7-field conversation state machine
│
├── pipelines/
│   └── gemini.py           # Gemini Live WebSocket pipeline (active)
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
| `GOOGLE_CREDENTIALS` | Yes | GCP service account JSON (full key) |
| `GOOGLE_SPREADSHEET_ID` | Yes | Target Google Sheet ID |
| `GMAIL_USER` | Yes | Gmail address for transcript emails |
| `GMAIL_APP_PASSWORD` | Yes | Gmail App Password (16-char, spaces OK) |
| `PUBLIC_URL` | Yes | Public HTTPS URL (for Vobiz webhook) |
| `SARVAM_API_KEY` | No | Reserved for Sarvam pipeline |
| `DEEPGRAM_API_KEY` | No | Reserved for Deepgram pipeline |
| `PORT` | No | Server port (default: 5050) |

---

## Deployment (CI/CD)

Every push to `main` auto-deploys to Google Cloud Run via GitHub Actions.

### GitHub Secrets Required (in `mydoot_env` environment)

| Secret | Value |
|--------|-------|
| `GCP_SA_KEY` | Full GCP service account JSON |
| `GEMINI_API_KEY` | Gemini API key |
| `GOOGLE_SPREADSHEET_ID` | Sheet ID |
| `GMAIL_USER` | Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password |
| `PUBLIC_URL` | Cloud Run service URL |

### Cloud Run Configuration

| Setting | Value |
|---------|-------|
| Region | us-central1 |
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

## Conversation Flow

```
[CALL_STARTED] → Agent speaks greeting (1 of 3, random)
      ↓
Customer speaks (complaint, name, or anything)
      ↓
Agent collects (in natural conversation order):
  1. Complaint description
  2. Customer name
  3. Brand name
  4. Device/appliance type
  5. How long they've been using it (fills both product_used_since + usage_duration)
  6. Warranty status
      ↓
All 7 fields collected → save_customer_feedback() tool call
      ↓
"[Name] ji, aapki complaint humne register kar li hai. Hamari service team
agle 24 ghanton mein aapse sampark karegi. Shukriya!"
      ↓
Call ends → Transcript emailed to admin
```

---

## Post-Call Email

After every call (including dropped/incomplete calls), an email is sent to `GMAIL_USER` with:
- Caller ID (phone number)
- Call timestamp
- Full conversation transcript (Agent + Customer turns)

---

## Built By

**Alok Ranjan** — [alokranjan04@gmail.com](mailto:alokranjan04@gmail.com)

Powered by Google Gemini Live · Vobiz SIP · Google Cloud Run · GitHub Actions
