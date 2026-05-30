# Technical Architecture: Mydoot Customer Care Voice Agent

**Agent Name:** Mydoot Customer Care Representative
**Core Logic:** 5-Step Customer Feedback Collection Flow
**Stack:** Sarvam AI, Google Gemini Live, Deepgram, ElevenLabs, Google Sheets

---

## 1. System Overview

The Mydoot Customer Care Voice Agent is an inbound telephony AI. When a customer calls +917971542939, Vobiz routes the call as a bidirectional WebSocket stream to this server. The agent conducts the call in natural Hinglish, collects five structured data points, and writes them to Google Sheets — all within a single phone call.

```
┌─────────────────────────────────────────────────────────────────┐
│  Customer dials +917971542939 (Vobiz SIP Trunk)                 │
└─────────────────┬───────────────────────────────────────────────┘
                  │ POST /answer
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│  Webhook Handler (routes/webhook.py)                            │
│  Reads active_provider from app_config.json                     │
│  Returns XML → opens WS stream to /sarvam-stream or            │
│  /gemini-stream                                                  │
└─────────────────┬───────────────────────────────────────────────┘
                  │
        ┌─────────┴──────────┐
        ▼                    ▼
  Pipeline A            Pipeline B
  (Sarvam Hybrid)       (Gemini Live)
  sarvam.py             gemini.py
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  5-Step Collection State Engine (core/state_engine.py)          │
│  Injects current state + collected/missing fields into prompt   │
└─────────────────┬───────────────────────────────────────────────┘
                  │
                  ▼ (when all 5 fields collected)
┌─────────────────────────────────────────────────────────────────┐
│  save_customer_feedback() tool call (mydoot_functions.py)       │
│  Appends one row to Google Sheets via Service Account API       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Conversation State Engine (`core/state_engine.py`)

The state engine ensures the agent collects every required field without skipping ahead or repeating itself. It is a lightweight class that tracks collected data and injects a status summary into every LLM prompt.

### States

| State | Description |
|---|---|
| `COLLECTING_NAME` | Waiting for customer's full name |
| `COLLECTING_PRODUCT` | Waiting for Mydoot product name |
| `COLLECTING_USAGE` | Waiting for how long the product has been used |
| `COLLECTING_WARRANTY` | Waiting for warranty status |
| `COLLECTING_COMPLAINT` | Waiting for the complaint or feedback description |
| `COMPLETED` | All fields collected — `save_customer_feedback` can be called |

### Prompt Injection

On every LLM turn, `get_prompt_injection()` appends a block like:

```
CURRENT STATE: COLLECTING_WARRANTY
COLLECTED: customer_name, product_name, usage_duration
STILL NEED: warranty_status, complaint
```

This prevents the LLM from jumping ahead or calling the save tool prematurely, regardless of how the conversation drifts.

### State Transitions

States advance by calling `set_data(key, value)`. This is triggered when the LLM calls `save_customer_feedback` — the pipeline extracts each argument from the tool call parameters and records them.

---

## 3. Media Pipelines

### 3.1 Pipeline A: Sarvam Hybrid (Primary)

```
Customer Audio (Mu-law 8kHz)
       │
       ▼
Deepgram Nova-2 (STT — WebSocket)
  Language: hi (Hindi/Hinglish)
  interim_results: true
  UtteranceEnd detection: enabled
       │
       ▼ transcript
Sarvam 30B (LLM — HTTP streaming)
  model: sarvam-30b
  temperature: 0.4
  tools: [save_customer_feedback]
  System prompt: Mydoot Customer Care persona + 5-step flow + state injection
       │
       ▼ text
ElevenLabs (if key present) / Sarvam Bulbul v2 (TTS)
  output: 16kHz PCM → Mu-law → WebSocket to Vobiz
```

**Barge-in handling**: Deepgram's `interim_results` stream detects when the customer starts speaking mid-response. If 2+ words are detected while the agent is speaking, the current TTS task is cancelled and audio is cleared.

**Sentence streaming**: LLM output is streamed token by token. A sentence boundary regex splits text into chunks for TTS, so the first sentence starts playing before the full response is complete, cutting perceived latency.

### 3.2 Pipeline B: Gemini Live (Alternative)

```
Customer Audio (Mu-law 8kHz) → PCM decode
       │
       ▼
Gemini 1.5 Flash (BidiGenerateContent WebSocket)
  Multimodal: audio in, audio out
  Native barge-in + emotion detection
       │
       ▼
24kHz PCM audio → sent directly to Voice Lab
```

Pipeline B is selected by setting `active_provider: "google"` in `app_config.json` or via the dashboard toggle. It requires no separate STT/TTS services — Gemini handles both ends natively.

---

## 4. Tool Integration (`mydoot_functions.py`)

The agent has a single tool: `save_customer_feedback`.

### Tool Schema

```json
{
  "name": "save_customer_feedback",
  "parameters": {
    "customer_name":   "string — Full name of the customer",
    "product_name":    "string — Mydoot product being used",
    "usage_duration":  "string — e.g. '6 months', '2 years'",
    "warranty_status": "enum   — 'Yes - Under Warranty' | 'No - Out of Warranty' | 'Customer Does Not Know'",
    "complaint":       "string — Detailed complaint or feedback"
  }
}
```

The tool is called **only** after all 5 fields are confirmed. The system prompt enforces this via an explicit rule: "Jab tak saari 5 cheezein nahi mil jaati, save_customer_feedback BILKUL MAT CALL KARO."

### Google Sheets Write

`save_customer_feedback()` appends one row to Sheet1 of the configured spreadsheet:

| Column | Field |
|---|---|
| A | Customer Name |
| B | Product Used |
| C | Usage Duration |
| D | Warranty Status |
| E | Complaint |
| F | Timestamp (YYYY-MM-DD HH:MM:SS) |
| G | Caller ID (phone number from Vobiz) |

If the sheet has no header row yet, the function auto-writes the header before appending data.

### Credentials

Google credentials are loaded in order:
1. `google-credentials.json` (local file)
2. `GOOGLE_CREDENTIALS` environment variable (JSON string — used in cloud deployments)

The private key is normalized (newlines re-wrapped to 64-char lines) before use to survive env var encoding issues.

---

## 5. Audio Pipeline Details

| Stage | Format | Sample Rate | Encoding |
|---|---|---|---|
| Vobiz → Server | Mu-law | 8 kHz | Base64 |
| Server → Deepgram | Mu-law | 16 kHz | Binary |
| Deepgram → LLM | Text transcript | — | UTF-8 |
| LLM → TTS | Text | — | UTF-8 |
| TTS → Server | PCM | 16 kHz | Base64 |
| Server → Vobiz | Mu-law | 16 kHz | Base64 |

Linear PCM ↔ Mu-law conversion is handled by the `audioop` stdlib module (Python 3.10–3.12 required).

---

## 6. Observability & Security

### Call Recording
Every call session is recorded as a **stereo 16kHz WAV file** in `recordings/`:
- Left channel: customer audio
- Right channel: agent (TTS) audio

Files are named by the first 8 characters of the Vobiz stream session ID.

### Metrics Dashboard (`/metrics`)
Tracked per call:
- **TTFT** (Time to First Token) — LLM response latency
- **TTS duration** — characters sent to TTS and estimated cost
- **CPU / memory** — resource polling during the call
- **Cost** — per-service API cost estimation

### Configuration Security
- Google credentials are never hardcoded. They are injected via `.env` at startup.
- `app.py` reconstructs `google-credentials.json` from the `GOOGLE_CREDENTIALS` env var on boot, enabling secure cloud deployments.

---

## 7. Configuration (`app_config.json`)

All agent behavior is controlled without code changes:

| Key | Purpose |
|---|---|
| `agent.system_prompt` | Full Hinglish system prompt with 5-step collection rules |
| `scripts.greeting` | First sentence spoken when a call connects |
| `tools.sarvam` | JSON schema for `save_customer_feedback` (passed to LLM) |
| `active_provider` | `"sarvam"` or `"google"` — selects active pipeline |
| `parameters.sarvam.model` | LLM model name |
| `parameters.sarvam.temperature` | LLM sampling temperature |

---

## 8. Environment Requirements

| Requirement | Detail |
|---|---|
| Python version | 3.10–3.12 (audioop compatibility) |
| Telephony | Vobiz SIP trunk, number +917971542939, Answer URL = `POST /answer` |
| Google Cloud | Sheets API enabled, service account with Editor access to target sheet |
| Env vars | `SARVAM_API_KEY`, `DEEPGRAM_API_KEY`, `GOOGLE_SPREADSHEET_ID`, `GOOGLE_CREDENTIALS` |
| Optional | `GEMINI_API_KEY` (Pipeline B), `ELEVEN_LABS_API_KEY` (premium TTS) |
