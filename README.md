# Mydoot Customer Care — AI Voice Agent

> **Every customer complaint deserves a real response, instantly.**
> Mydoot Customer Care is a production-grade AI voice agent that answers inbound calls 24/7, collects structured appliance complaint data in Hinglish (Hindi-English mix), and logs every interaction directly to Google Sheets — no human agent required.

---

## What It Does

A customer calls **+917971542939**. The AI agent:

1. Answers immediately with a warm Hinglish greeting and asks about the problem directly
2. Auto-detects language silently — stays Hinglish unless the customer's first reply is clearly English-only
3. Collects 7 structured fields conversationally in a fixed order
4. Asks short clarifying questions whenever something is unclear — never goes silent mid-call
5. Saves the complete record to Google Sheets
6. Emails the full call transcript to the admin after every call

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
        ├── Audio In:  mu-law 8kHz → noise gate → PCM 16kHz → Gemini Live
        ├── Audio Out: Gemini Live PCM 24kHz → mu-law 8kHz → Vobiz
        │
        ▼
 Gemini Live (gemini-2.5-flash-native-audio-latest)
 • Native STT + LLM + TTS in one model
 • Voice: Aoede (warm, clear female)
 • Language: Hinglish by default; switches to English if customer responds in English
        │
        │  When all 7 fields collected:
        ▼
 save_customer_feedback() tool call
        │
        ├── Google Sheets API ──► Append row to mydoot_Customer_Care sheet
        └── Gmail SMTP ──────────► Send transcript email to admin (after call)
```

---

## Call Flow

```
[CALL_STARTED] → Agent speaks Hinglish greeting (1 of 3, random)
      ↓
Agent asks about the appliance and problem (first question, immediate)
      ↓
Language auto-detection (silent):
  ├── Customer first reply in clear English only → rest of call in English
  └── Hindi / Hinglish / mixed / unclear / anything else → stay Hinglish
      ↓
Agent collects remaining fields in order:
  1. complaint + device (combined first question)
  2. brand (if not already mentioned)
  3. item / device type (if not already mentioned)
  4. usage duration → fills both product_used_since + usage_duration
  5. warranty status
  6. customer name (LAST)
      ↓
If any response is unclear → agent asks a short clarifying question (never silent)
      ↓
All 7 fields collected → save_customer_feedback() tool call
      ↓
Agent speaks confirmation ONCE, then goes completely silent
      ↓
Call auto-closes 8 seconds after successful save
      ↓
Transcript emailed to admin (always, even on dropped calls)
```

---

## Data Collected

The agent collects **7 fields** for every complaint:

| # | Field | Example |
|---|-------|---------|
| 1 | Complaint | "laptop chal nahi raha hai" |
| 2 | Brand | HP, Samsung, Apple, LG |
| 3 | Item (device) | Laptop, TV, Refrigerator, MacBook |
| 4 | Product Used Since | 2022, 3 saal pehle |
| 5 | Usage Duration | 3 saal, 6 mahine |
| 6 | Warranty Status | Yes - Under Warranty / No - Out of Warranty / Customer Does Not Know |
| 7 | Customer Name | Kumud Ranjan |

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
- Lighting: Tube light, LED bulb, ceiling fan
- And anything else a customer might have

---

## Audio Pipeline

The pipeline includes several layers to handle PSTN phone call quality (8kHz mu-law):

| Layer | Purpose |
|-------|---------|
| **Noise gate** | Filters background noise (fan, line hiss) below RMS threshold. Fallback to lower threshold if customer goes silent too long. |
| **Silence injection** | After the noise gate blocks a packet, injects zero-amplitude PCM so Gemini's VAD detects end-of-speech in ~1–2s instead of 20–30s. |
| **Startup guard** | Blocks customer audio for 2 seconds after call connect to prevent connection burst noise interrupting the greeting. |
| **Echo guard** | 0.3s buffer after each Gemini `turnComplete` to prevent agent audio echoing back as customer speech. |
| **Post-save guard** | Blocks all customer audio for 15s after `save_customer_feedback` succeeds, so the confirmation message plays uninterrupted. |
| **Inactivity timeout** | Closes the call if no customer speech is detected for 20s after the agent's last turn. |
| **Audio batching** | Accumulates 4 × 20ms frames (80ms) before each Gemini send, reducing send rate from 50/s to ~12/s. |

---

## Project Structure

```
mydoot-voice-agent/
├── app.py                    # Entry point — registers routes, starts server
├── app_config.json           # Agent persona, system prompt, tool schemas, greetings
├── mydoot_functions.py       # save_customer_feedback() + Gmail transcript email
├── requirements.txt
├── Dockerfile
├── test_asr_compare.py       # Offline ASR benchmark: Azure / Sarvam / Deepgram
│
├── config/
│   └── settings.py           # API keys, URLs loaded from env
│
├── core/
│   └── state_engine.py       # 7-field conversation state tracker (used at tool call time)
│
├── pipelines/
│   └── gemini.py             # Gemini Live WebSocket pipeline (audio I/O, noise gate, tools)
│
├── routes/
│   └── webhook.py            # POST /answer — Vobiz inbound call handler
│
├── recordings/               # WAV files saved when RECORD_CALLS=1 (gitignored)
│
└── .github/workflows/
    └── deploy.yml            # GitHub Actions → Cloud Run CI/CD
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

#### Required

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI Studio API key |
| `GOOGLE_CREDENTIALS` | GCP service account JSON (full key) |
| `GOOGLE_SPREADSHEET_ID` | Target Google Sheet ID |
| `GMAIL_USER` | Gmail address for transcript emails |
| `GMAIL_APP_PASSWORD` | Gmail App Password (16-char, spaces OK) |
| `PUBLIC_URL` | Public HTTPS URL (for Vobiz webhook) |

#### Audio Tuning (optional — defaults shown)

| Variable | Default | Description |
|----------|---------|-------------|
| `NOISE_GATE_RMS` | `100` | RMS energy threshold — packets below this are treated as background noise |
| `NOISE_GATE_FALLBACK_RMS` | `20` | Lower threshold used when no speech detected after `NOISE_GATE_FALLBACK_AFTER_S` |
| `NOISE_GATE_FALLBACK_AFTER_S` | `2.0` | Seconds after agent turn before fallback threshold activates |
| `NOISE_GATE_FALLBACK_FULL_DISABLE_S` | `7.0` | Seconds after which noise gate is fully disabled if still no speech |
| `SPEECH_TAIL_SECS` | `0.4` | Seconds to keep forwarding audio after last speech packet (captures utterance tail) |
| `SILENCE_SEND_SECS` | `5.0` | Seconds of injected silence to send after speech tail expires |
| `NOISE_GATE_DEBUG` | `0` | Set to `1` for per-packet RMS logging (use for threshold tuning) |

#### Call Recording & ASR Testing (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `RECORD_CALLS` | `0` | Set to `1` to save each call's inbound audio as a WAV file in `recordings/` |
| `RECORDINGS_DIR` | `recordings` | Directory for WAV recordings |
| `AZURE_KEY1` | — | Azure OpenAI key (for `test_asr_compare.py`) |
| `ENDPINTS` | — | Azure OpenAI endpoint URL (for `test_asr_compare.py`) |
| `AZURE_DEPLOYMENT` | `whisper` | Azure OpenAI Whisper deployment name |
| `SARVAM_API_KEY` | — | Sarvam AI key (for `test_asr_compare.py` and Sarvam pipeline) |
| `DEEPGRAM_API_KEY` | — | Deepgram key (for `test_asr_compare.py`) |

---

## ASR Benchmarking

To compare speech recognition quality on real PSTN call audio:

**Step 1 — Record a call:**
```
RECORD_CALLS=1  # add to .env, restart server, make a test call
```

**Step 2 — Run the comparison:**
```bash
python test_asr_compare.py
# or: python test_asr_compare.py recordings/caller_20260601_143022.wav
```

This sends the same WAV to **Azure OpenAI Whisper**, **Sarvam Saaras V3**, and **Deepgram Nova-2 Phonecall** simultaneously and prints transcripts + latency side by side.

### ASR Comparison (Hindi/Hinglish PSTN calls)

| Service | Hindi WER | Hinglish WER | Streaming | Cost/min | India Latency |
|---------|-----------|--------------|-----------|----------|---------------|
| **Sarvam Saaras V3** | **~8%** | **~11.5%** | Yes | $0.006 | 180ms |
| Deepgram Nova-2 | ~21% | ~32% | Yes | $0.005 | 1200ms* |
| Azure Whisper | ~20% | ~29% | Batch only | $0.017 | ~800ms |
| Whisper (OpenAI) | ~22% | ~35% | Batch only | $0.006 | 1200ms* |
| Google Chirp 2 | — | — | **No Hindi** | — | — |

*Includes India→US network round-trip. Sarvam servers are India-hosted.

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

Powered by Google Gemini Live · Vobiz SIP · Google Cloud Run · GitHub Actions
