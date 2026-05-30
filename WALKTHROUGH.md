# Code Walkthrough: Mydoot Customer Care Voice Agent

This document explains the technical flow of the system, from the moment a customer calls +917971542939 to the moment their complaint is saved in Google Sheets.

---

## 1. The Entry Point (`app.py`)

Everything starts here. `app.py` starts an `aiohttp` async web server on port `5050` and registers all routes.

**What it does at startup:**
- Reads `GOOGLE_CREDENTIALS` from the environment and writes it to `google-credentials.json` on disk. This enables cloud deployments where secrets live in env vars, not files.
- Registers the `/answer` webhook that Vobiz calls when a customer dials +917971542939.
- Registers `/sarvam-stream` and `/gemini-stream` WebSocket endpoints — one per pipeline.
- Serves the dashboard at `/` and the Voice Lab at `/voice-lab`.

**To change the active pipeline**, update `active_provider` in `app_config.json` to `"sarvam"` or `"google"`, or use the toggle on the dashboard.

---

## 2. The Inbound Call (`routes/webhook.py`)

When Vobiz receives a call on +917971542939, it makes a `POST /answer` request to this server.

The webhook:
1. Reads the `From` header to extract the caller's phone number (used as `caller_id`).
2. Checks `active_provider` to decide whether to direct the call to `/sarvam-stream` or `/gemini-stream`.
3. Returns an XML `<Stream>` response that tells Vobiz to open a **bidirectional WebSocket** — audio flows both ways on this single connection.

From this point, the call is a live WebSocket session.

---

## 3. The Customer Care Logic (`core/state_engine.py`)

The state engine is injected into the LLM system prompt on every single turn. Its job is to tell the LLM exactly where it is in the 5-step collection flow and what it still needs.

**How it works:**

```python
# Every LLM turn gets this appended to its system prompt:
CURRENT STATE: COLLECTING_WARRANTY
COLLECTED: customer_name, product_name, usage_duration
STILL NEED: warranty_status, complaint
```

This prevents the model from:
- Calling `save_customer_feedback` before all fields are collected.
- Asking for the same information twice.
- Jumping ahead to the complaint before asking for the warranty.

When the LLM calls the `save_customer_feedback` tool, the pipeline records each argument into the state engine via `set_data()`, which advances the state to `COMPLETED`.

---

## 4. The Voice Pipeline (`pipelines/sarvam.py`)

This is the Listen → Think → Speak loop that runs for the entire duration of the call.

### Listen (STT)
Raw Mu-law audio from Vobiz is forwarded to **Deepgram Nova-2** over a WebSocket. Deepgram returns:
- Interim transcripts (partial — used for barge-in detection).
- Final transcripts (confirmed — triggers the LLM).
- `UtteranceEnd` events (customer has stopped speaking — triggers LLM if final wasn't fired).

**Barge-in**: If the customer starts speaking while the agent is playing audio (2+ words detected), the current `speak` task is cancelled and the audio buffer on Vobiz is cleared. The agent listens immediately.

### Think (LLM)
The final transcript is appended to the conversation history and sent to **Sarvam 30B** as a streaming HTTP request. The system prompt includes:
- The full Mydoot Customer Care persona and 5-step collection rules (from `app_config.json`).
- The current state injection from `state_engine.get_prompt_injection()`.

The LLM streams back either:
- **Text** — the agent's spoken response.
- **Tool call** — `save_customer_feedback` with all 5 collected fields.

### Speak (TTS)
Text is split into sentence chunks using a regex boundary. Each sentence is immediately sent to TTS as it arrives — the first sentence starts playing while the rest of the response is still being generated. This cuts perceived latency significantly.

TTS is attempted in order:
1. **ElevenLabs** (if `ELEVEN_LABS_API_KEY` is set) — most natural Hindi voice.
2. **Sarvam Bulbul v2** (fallback) — fast, native Hindi.

The resulting 16kHz PCM is encoded as Mu-law and sent back to Vobiz.

### Tool Execution
When the LLM emits a `save_customer_feedback` tool call:
1. The pipeline extracts the 5 arguments and the `caller_id`.
2. Calls `save_customer_feedback()` in a thread pool (non-blocking).
3. Returns the tool result to the LLM for the follow-up response (the thank-you confirmation message).

---

## 5. The Feedback Save (`mydoot_functions.py`)

This module contains all Google Sheets logic.

**`save_customer_feedback(customer_name, product_name, usage_duration, warranty_status, complaint, caller_id)`:**

1. Loads Google credentials from `google-credentials.json` or `GOOGLE_CREDENTIALS` env var.
2. Normalizes the RSA private key (fixes newline encoding issues common in env var deployments).
3. Builds a Google Sheets API client (cached per process).
4. Checks if the sheet has a header row — if not, writes the 7-column header first.
5. Appends a new row with all fields plus a timestamp.
6. Returns a confirmation message that the LLM reads back to the customer.

**Expected Sheet1 columns:** Customer Name | Product Used | Usage Duration | Warranty Status | Complaint | Timestamp | Caller ID

---

## 6. The Voice Lab (`routes/voice_lab.py`)

The Voice Lab at `http://localhost:5050/voice-lab` is a browser-based call simulator.

It uses your microphone and speakers to simulate exactly what a Vobiz call looks like:
- Captures browser audio and converts it to **Mu-law 8kHz** — matching the phone line format.
- Connects directly to `/sarvam-stream` or `/gemini-stream` over WebSocket.
- Displays the live transcript and current collection state in real time.
- Allows provider switching without making a real phone call.

This is the primary tool for testing and tuning the agent's Hinglish conversation flow before going live.

---

## 7. Metrics & Monitoring (`metrics/`)

Every call is automatically tracked:

| Metric | What It Measures |
|---|---|
| **TTFT** | Time from customer's last word to agent's first spoken word |
| **TTS Characters** | Volume of text sent to TTS (drives cost estimate) |
| **Cost** | Estimated API cost per call (Deepgram + Sarvam + ElevenLabs) |
| **Call Recording** | Stereo WAV saved to `recordings/` — caller on left, agent on right |

View the metrics dashboard at `http://localhost:5050/metrics`.

---

## 8. How to Modify the Agent

No code changes needed for most customizations — edit `app_config.json`:

| What to Change | Where in app_config.json |
|---|---|
| The greeting message | `scripts.greeting` |
| The system prompt / collection rules | `agent.system_prompt` |
| Switch pipeline (Sarvam ↔ Gemini) | `active_provider` |
| LLM temperature | `parameters.sarvam.temperature` |
| Add a new tool | `tools.sarvam` array + `FUNCTION_MAP` in `mydoot_functions.py` |

---

## 9. Call Flow: End to End

```
Customer dials +917971542939
        │
        ▼
Vobiz POST /answer
        │
        ▼ XML <Stream> response
Vobiz opens WebSocket to /sarvam-stream
        │
        ▼ "start" event
Agent speaks greeting: "Namaste! Aap Mydoot Customer Care mein bol rahe hain..."
        │
        ▼ customer speaks
Deepgram → transcript → Sarvam LLM → "Aapka naam kya hai?"
        │
        ▼ customer answers name
State: COLLECTING_NAME → COLLECTING_PRODUCT
LLM asks: "Aap kaun sa Mydoot product use karte hain?"
        │
        ... (5 steps total) ...
        │
        ▼ all 5 fields confirmed
LLM calls: save_customer_feedback(name, product, duration, warranty, complaint)
        │
        ▼
Google Sheets: new row appended
        │
        ▼
Agent: "Aapka feedback save ho gaya hai. Shukriya Mydoot se contact karne ke liye!"
        │
        ▼
Call ends
```
