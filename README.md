# Mydoot Customer Care — AI Voice Agent

> **Every customer complaint deserves a real response, instantly.**
> Mydoot Customer Care is a production-grade AI voice agent that answers inbound calls 24/7, collects structured customer feedback in natural Hinglish, and logs every interaction directly to Google Sheets — no human agent required.

Callers speak naturally. The agent asks the right questions, understands the answers, and saves a complete, structured record of their name, product, usage, warranty status, and complaint — automatically.

---

## The Problem It Solves

Most customer care lines fail at three things:

- **Missed calls**: Complaints outside business hours go unrecorded and unresolved.
- **Incomplete data**: Human agents forget to ask for product name, warranty status, or clear complaint details.
- **Zero traceability**: No structured log means issues fall through the cracks.

Mydoot Customer Care solves all three. It is always available, always asks every required question, and always writes a complete row to Google Sheets the moment the call ends.

---

## System Architecture

```
Inbound Call (+917971542939 via Vobiz)
         │
         ▼
  POST /answer  ──►  WebSocket Stream  ──►  active_provider = "sarvam" | "google"
         │
   ┌─────┴──────┐
   │            │
   ▼            ▼
Pipeline A   Pipeline B
(Sarvam/EL) (Gemini Live)
   │
   ▼
5-Step Feedback Collection Flow
   │
   ▼
save_customer_feedback()
   │
   ▼
Google Sheets  ──►  Sheet1 (Customer Name | Product | Usage | Warranty | Complaint | Timestamp | Caller ID)
```

---

## Pipeline A — Hybrid (Primary)

| Component | Technology |
|---|---|
| **Telephony** | Vobiz — SIP trunk, number +917971542939 |
| **STT** | Deepgram **Nova-2** — tuned for Hindi accents and mixed Hinglish speech |
| **LLM** | Sarvam **sarvam-30b** — specialized for Indian languages and customer context |
| **TTS (Premium)** | **ElevenLabs** Multilingual V2 — natural, human-sounding Hindi voice |
| **TTS (Standard)** | Sarvam **Bulbul v2** — fast, native Hindi fallback |
| **State Engine** | Custom **5-Step Collection Flow** (Name → Product → Usage → Warranty → Complaint) |

---

## Pipeline B — Google Gemini Live (Low Latency)

| Component | Technology |
|---|---|
| **Model** | Gemini **1.5 Flash Live** (BidiGenerateContent multimodal) |
| **Voice** | Aoede (Native Gemini Voice) |
| **Advantage** | Zero-latency barge-in; handles interruptions and long pauses gracefully |

---

## The 5-Step Feedback Collection Flow

| Step | What the Agent Asks | Field Saved |
|---|---|---|
| 1 | "Aapka naam kya hai?" | `customer_name` |
| 2 | "Aap kaun sa Mydoot product use karte hain?" | `product_name` |
| 3 | "Yeh product kitne time se use kar rahe hain?" | `usage_duration` |
| 4 | "Kya aapka product warranty mein hai?" | `warranty_status` |
| 5 | "Aapka complaint ya feedback kya hai?" | `complaint` |

Once all five fields are confirmed, the agent calls `save_customer_feedback()` and writes the record to Google Sheets, then thanks the customer and closes the call.

**Warranty status** is stored as one of three values:
- `Yes - Under Warranty`
- `No - Out of Warranty`
- `Customer Does Not Know`

---

## Google Sheets Output

Every completed call adds one row to **Sheet1**:

| A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|
| Customer Name | Product Used | Usage Duration | Warranty Status | Complaint | Timestamp | Caller ID |

---

## Dashboard & Voice Lab

The built-in interface at `http://localhost:5050/` lets you:

- **Toggle Providers**: Switch between Sarvam and Gemini Live without restarting.
- **Voice Lab** (`/voice-lab`): Browser-based call simulator — test the full flow without making a real call.
- **Real-time Transcript**: Watch what the customer and agent are saying, live.
- **State Tracker**: See which collection step the agent is on (Name / Product / Usage / Warranty / Complaint).
- **Metrics** (`/metrics`): Latency, cost per call, and turn-by-turn analytics.

---

## Project Structure

```
app.py                    — aiohttp server, route registration
app_config.json           — Agent persona, system prompt, tool schema
mydoot_functions.py       — save_customer_feedback() + Google Sheets integration
core/
  state_engine.py         — 5-step collection state machine
  recorder.py             — Stereo call recording (Caller + Agent)
  hindi_utils.py          — Hinglish text utilities
pipelines/
  sarvam.py               — Sarvam + Deepgram + ElevenLabs pipeline
  gemini.py               — Gemini Live Multimodal pipeline
routes/
  webhook.py              — POST /answer  (Vobiz inbound call handler)
  dashboard.py            — Provider toggle + parameter control
  voice_lab.py            — Browser test interface
  metrics.py              — Cost and latency dashboard
metrics/
  collector.py            — Per-call metrics store
  cost_calculator.py      — API cost estimation
recordings/               — Stereo WAV recordings per call
```

---

## Setup

### Prerequisites

- Python 3.10–3.12 (required for `audioop`)
- A Google Cloud service account with **Sheets API** enabled
- A Google Spreadsheet shared with the service account as Editor
- Vobiz account with number +917971542939 pointing to this server

### Installation

```bash
pip install -r requirements.txt
```

### Environment Variables (`.env`)

```env
SARVAM_API_KEY=your_sarvam_key
DEEPGRAM_API_KEY=your_deepgram_key
GEMINI_API_KEY=your_gemini_key          # optional — Pipeline B only
ELEVEN_LABS_API_KEY=your_el_key         # optional — premium TTS
GOOGLE_SPREADSHEET_ID=your_sheet_id
GOOGLE_CREDENTIALS={"type":"service_account",...}  # or use google-credentials.json
PORT=5050
```

### Run

```bash
python app.py
```

Dashboard: `http://localhost:5050/`
Voice Lab: `http://localhost:5050/voice-lab`

---

## Built by

**Alok Ranjan**

Mydoot Customer Care is part of the next generation of AI-powered voice support for Indian product companies.
