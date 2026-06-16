# Technical Architecture: Mydoot Customer Care Voice Agent

**Agent Name:** Mydoot Customer Care Representative
**Active Pipeline:** Gemini 3.1 Flash Live — native audio (STT + LLM + TTS all-in-one)
**Orchestration:** LangGraph ServiceGraph (compressed system prompt, ~620 tokens)
**Stack:** Gemini 3.1 Flash Live, LangGraph, Vobiz SIP, PostgreSQL, Google Sheets, Gmail SMTP, Cloud Logging, Google Cloud Run

---

## 1. System Overview

Mydoot Customer Care is an AI voice agent that handles inbound phone calls on the Vobiz SIP platform. It uses Gemini 3.1 Flash Live in **native audio mode** — a single model handles STT, LLM reasoning, and TTS voice synthesis in one hop.

- **Gemini 3.1 Flash Live** (`gemini-3.1-flash-live-preview`) — receives raw audio via `realtimeInput.audio`, handles turn detection/VAD, speech recognition, response generation, and speech synthesis natively
- **LangGraph ServiceGraph** — structured conversation orchestration with a compressed system prompt (~620 tokens, 75% reduction from original ~2500)
- **Cloud Logging** (`config/cloud_logging.py`) — structured JSON logging, Cloud Monitoring integration via log-based metrics
- **Call recordings** — stereo WAV saved to GCS bucket `mydootrecordings` (RECORD_CALLS=1)

Customer audio goes through: Vobiz mu-law 8kHz → PCM 16kHz → realtimeInput.audio → Gemini native → audio response.

**Feature flags:**
- `NATIVE_AUDIO_INPUT=1` (default) — Native audio mode. Gemini handles STT+LLM+TTS in one hop. Production default since 2026-06-07.
- `NATIVE_AUDIO_INPUT=0` — Legacy STT fallback: streams to Sarvam Saaras v3 for STT, sends text to Gemini. Available for rollback.
- `RECORD_CALLS=1` — Stereo WAV recording uploaded to GCS bucket `mydootrecordings`.
- `LOCAL_PROMPT_STAGES=""` (default) — All stages handled by Gemini. Set to `address,preferred_time,customer_name` to re-enable local TTS for those stages.

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
│    Sends setup: model, systemInstruction, tools,                    │
│    inputAudioTranscription, outputAudioTranscription                │
│    Mode: text clientContent in, audio out                           │
│    Instantiates: ServiceGraph() per call                            │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
           ┌──────────────┴──────────────┐
           │                             │
           ▼                             ▼
┌──────────────────────┐    ┌────────────────────────┐
│ Audio IN loop        │    │ g_receiver task         │
│ (Vobiz → Gemini)     │    │ (Gemini → Vobiz)        │
│                      │    │                         │
│ mu-law 8kHz          │    │ PCM 24kHz               │
│ → ulaw2lin → PCM 8k  │    │ → ratecv(24000→8000)    │
│ → ratecv(8k→16k)     │    │ → lin2ulaw              │
│ → realtimeInput.audio│    │ → mu-law 8kHz           │
│                      │    │ → Vobiz playAudio        │
│ Gemini handles:      │    │                         │
│  VAD, turn detection │    │ Tracks turnComplete:    │
│  speech recognition  │    │ flushes transcript buf  │
│  (native audio mode) │    │ sets gemini_turn_end_ts │
│                      │    │                         │
│ Language selection:   │    │ End-marker detection:   │
│  Hindi or English    │    │ "shukriya" / "thank you"│
│  (customer chooses)  │    │ → stop forwarding audio │
│                      │    │                         │
│ caller_id sanitized  │    │ Byte cap: 9s max        │
│ (path traversal safe)│    │ Drain delay: 5s         │
│                      │    │ Nudge on empty TC       │
└──────────────────────┘    └────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. Tool Call: save_service_request                                  │
│    Triggered when Gemini has all required fields                    │
│    service_graph.on_tool_call(args) → state = done                 │
│    Handler: mydoot_functions.py                                     │
│    - Writes to PostgreSQL service_requests table (primary)         │
│    - Appends row to Google Sheets Sheet1 (secondary, soft-fail)    │
│    - Returns success to Gemini                                      │
│    - Gemini speaks confirmation ONCE                                │
│    - End-marker detection + 9s byte cap + 5s drain delay           │
│    - save_executed flag prevents duplicate execution per session    │
│    - Duplicate text truncated at first "shukriya" before flush     │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5b. Reconnect on clean close (code=1000)                            │
│    Gemini sometimes closes WS mid-call (thinks conversation done)  │
│    Catches ConnectionClosedOK + ConnectionClosedError               │
│    → Reconnect to Gemini → resend setup + stage context             │
│    → Customer doesn't notice; conversation resumes                  │
│    Limit: 1 reconnect per call (prevents infinite loops)           │
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
│    save_call_log() → writes to PostgreSQL call_logs (primary)       │
│                    → appends to Call_Logs sheet tab (secondary)     │
│    Structured JSON log emitted via config/cloud_logging.py          │
│    18 columns: timestamp, caller, duration, stage, saved,           │
│    category, subcategory, issue_type, customer_name, address,       │
│    preferred_time, stt_count, stt_avg_ms, stt_drops,               │
│    barge_ins, reconnects, audio_gcs, transcript                     │
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│ 8. Call recording (RECORD_CALLS=1)                                   │
│    Stereo WAV (customer L + agent R) uploaded to GCS                │
│    Bucket: mydootrecordings | URI stored in call_logs.audio_gcs     │
│    Playable in-browser from /calls dashboard                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Audio Pipeline

### Inbound — Native Audio Mode (Customer Voice → Gemini directly, NATIVE_AUDIO_INPUT=1)

```
Vobiz WebSocket frame
  └── event: "media"
  └── payload: base64(mu-law PCM, 8kHz, mono)
        │
        ▼ audioop.ulaw2lin(data, 2)
  Linear PCM, 8kHz, mono, 16-bit
        │
        ▼ audioop.ratecv(pcm8, 2, 1, 8000, 16000, state)
  Linear PCM, 16kHz, mono, 16-bit
        │
        ▼ base64 encode
        │
        ▼ Gemini realtimeInput { media: { audio: base64, mimeType: "audio/pcm;rate=16000" } }
  Gemini handles VAD, turn detection, speech recognition natively
  inputAudioTranscription captures customer speech for logging
```

### Inbound — Legacy STT Fallback (NATIVE_AUDIO_INPUT=0)

```
Vobiz WebSocket frame → mu-law 8kHz → PCM 8kHz
        │
        ▼ Local VAD (RMS threshold = 100)
  Speech frames accumulated; silence > 0.2s triggers flush
  Utterances < 0.3s discarded (noise blips)
        │
        ▼ Sarvam Saaras v3 REST (POST /speech-to-text)
        │
        ▼ Transcript → ServiceGraph.get_context() → clientContent text → Gemini
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

`ServiceGraph` wraps a LangGraph `StateGraph` and holds per-call `ServiceState`. It tracks the current stage and injects compressed context into every Gemini turn.

#### Stage Routing

```
category → subcategory → diagnosis → brand* → address → preferred_time → customer_name → done
*brand stage is skipped for categories where needs_brand=False
```

#### Compressed Stage Context (latency-optimized)

Stage context is injected per turn with minimal tokens (~65% reduction from original verbose format):

```
[STAGE stage=address]
Collected: {"category": "Appliance Repair", "subcategory": "TV / Television", "brand": "Samsung"}
Do: ASK: Society/building name aur area?
[/STAGE]

Customer: Mahagun Mazaria, Sector 78, Noida
```

#### System Prompt Speed Rules

The system prompt includes speed constraints to minimize response latency:
1. MAX 15 WORDS per response
2. NEVER repeat what the customer just said
3. Start every response with a brief acknowledgment ("Theek hai.", "Achcha.")
4. Say confirmation ONCE only — never repeat the closing message

#### Agent Speech–Based Stage Advancement

`_AGENT_STAGE_TRIGGERS` patterns scan Gemini's speech at each `turnComplete` to advance the ServiceGraph stage. Guard: won't skip past uncollected local-prompt stages.

```python
_AGENT_STAGE_TRIGGERS = [
    ("address",        [r"\baddress\b", r"\bpata\b", r"\bsociety\b", ...]),
    ("preferred_time", [r"\bkab\b.{0,50}\b(aaye|visit|chahte|time)\b", ...]),
    ("customer_name",  [r"\bapna\s+(?:poora\s+)?naam\b", ...]),
]
```

---

## 5. Confirmation & Call Close Logic

### Gemini-Driven Confirmation (LOCAL_PROMPT_STAGES empty)

When Gemini calls `save_service_request` and local TTS is disabled:
1. Save executes → `save_done_ts` set → customer audio blocked (`save_tool_pending`)
2. Gemini speaks its own confirmation message
3. **End-marker detection:** output transcription monitored for "shukriya" / "thank you for calling" → stop forwarding new audio
4. **Byte cap (9 seconds):** catches duplicate confirmations the text detection misses (one confirmation is ~6-7s)
5. **Drain delay (5 seconds):** lets Vobiz play already-buffered audio before closing
6. **turnComplete guard:** first TC post-save with >= 4s audio played → close call; if < 4s → nudge Gemini to retry
7. Duplicate text truncated at first "shukriya" before flushing to transcript

### Guard Flags (race condition protection)

When local save is triggered (`stage=done`):
- `save_done_ts` and `confirmation_done` set **synchronously** at call site (before `asyncio.create_task`)
- `save_executed` set inside `_trigger_local_save` (prevents Gemini duplicates)
- g_receiver's finally block checks `confirmation_done` and `save_done_ts` — won't close Vobiz WS if save is in progress

### 10 WS Close Paths (all verified safe)

| # | Trigger | Guard |
|---|---------|-------|
| 1 | End-marker "shukriya" | Only when `_LOCAL_PROMPT_STAGES` set |
| 2 | Byte cap (7s audio) | `not confirmation_done` |
| 3 | turnComplete after save | First TC closes; extras ignored |
| 4 | g_receiver finally | `not confirmation_done AND save_done_ts==0` |
| 5 | _local_final_confirmation end | Intentional close |
| 6 | _trigger_local_save failure | Graceful error close |
| 7 | Call timeout (600s) | Always |
| 8 | Inactivity (25s silence) | `save_done_ts==0` |
| 9 | Vobiz WS error | Transport error |
| 10 | Outer finally cleanup | Always |

---

## 6. Gemini Live Configuration

```json
{
  "setup": {
    "model": "models/gemini-3.1-flash-live-preview",
    "generationConfig": { "responseModalities": ["AUDIO"] },
    "speechConfig": { "voiceName": "Aoede" },
    "inputAudioTranscription": {},
    "outputAudioTranscription": {},
    "systemInstruction": { "parts": [{ "text": "<compressed_system_prompt ~620 tokens>" }] },
    "tools": [{ "function_declarations": [{ "name": "save_service_request", ... }] }]
  }
}
```

| Setting | Value | Reason |
|---------|-------|--------|
| Model | gemini-3.1-flash-live-preview | Native audio: STT + LLM + TTS all-in-one |
| Modalities | AUDIO only | Text output not needed |
| Input mode | realtimeInput.audio (native) | Raw PCM 16kHz streamed directly to Gemini |
| Voice | Aoede (slow, sweet style) | Warm, clear female voice for customer care |
| inputAudioTranscription | Set | Captures customer speech for logging |
| outputAudioTranscription | Set | Captures agent speech for transcript |
| System prompt | ~620 tokens (compressed) | 75% reduction from original ~2500 tokens |
| Language | Hindi or English | Customer chooses at start of call |

---

## 7. Security & Guardrails

- **caller_id sanitized**: `re.sub(r'[^a-zA-Z0-9_+\-]', '', caller_id)` — prevents path traversal in filenames
- **ReDoS prevention**: Time patterns use `[\w\s,।]{0,20}` instead of `.{0,20}`
- **Devanagari word boundaries**: `\b` not used with Devanagari (vowel signs are non-`\w`)
- **Local audio streaming**: `web.FileResponse` (no memory spikes for large WAVs)
- **Confirmation words**: Include Devanagari variants (जी, अच्छा, ठीक है, सही है, etc.)
- **Name rejection**: Multi-word affirmatives rejected as names (अच्छा जी, ठीक जी, etc.)

---

## 8. VAD — Voice Activity Detection (Legacy STT Path Only)

In native audio mode (`NATIVE_AUDIO_INPUT=1`), Gemini handles VAD and turn detection internally. The following thresholds apply only to the legacy Sarvam STT fallback path (`NATIVE_AUDIO_INPUT=0`):

```python
VAD_SPEECH_THRESHOLD   = 100    # RMS amplitude gate for speech detection
VAD_END_SECS           = 0.2    # silence after speech to end utterance
VAD_MIN_SPEECH_SECS    = 0.3    # minimum utterance duration
VAD_MAX_SPEECH_SECS    = 30.0   # hard ceiling — force-flush long utterances

BARGE_IN_RMS_THRESHOLD = 350    # high-RMS threshold for interruption detection
BARGE_IN_SUSTAIN_SECS  = 0.3    # sustained duration to confirm human interruption
```

---

## 9. Observability

### Cloud Logging & Monitoring (`config/cloud_logging.py`)
- **Structured JSON logging** — every event emitted as JSON to stdout; Cloud Run auto-ingests into Cloud Logging
- **Log-based metrics** — extract numeric values (latency, error counts) for Cloud Monitoring dashboards and alerts
- **Filterable fields:** `caller_id`, `stage`, `event`, `latency_ms`, `severity`
- Falls back to human-readable console output for local development

### Dashboard: `/calls`
- Per-call quality metrics (category, stage, saved, barge-ins, reconnects)
- Per-call turn latency view (LLM first token, end-to-end per turn)
- Transcript with chat bubbles
- Audio playback (stereo WAV from GCS bucket `mydootrecordings`)

### Call Recordings
- Stereo WAV saved per call when `RECORD_CALLS=1`
- Uploaded to GCS bucket `mydootrecordings`
- Playable in-browser from `/calls` dashboard

### Latency Metrics: `/latency`
- P50/P95/P99 for LLM first token, end-to-end
- Stored in PostgreSQL `turn_latency_metrics` table
- Per-call drill-down via `/calls/latency?caller_id=XXX`

### Current Performance (P50, native audio mode)
- End-to-end turn latency: ~2 seconds (STT hop eliminated by native audio)
- LLM first token: ~1500ms (Gemini 3.1 processing)

---

## 10. Sarvam TTS (Legacy, Disabled)

**Status:** Disabled by default (`LOCAL_PROMPT_STAGES=""`). Superseded by Gemini 3.1 native audio which handles TTS natively with the Aoede voice.

When enabled, Sarvam Bulbul v2 handles address/time/name stages locally:
- Model: `bulbul:v2` (v3 doesn't support pitch/loudness; v1 deprecated)
- Speaker: `anushka`, pace=1.05, pitch=0, loudness=1.5
- Disabled because voice quality was not acceptable (different from Gemini voice)
- Pre-cached engagement phrases: "Theek hai." and "Ek second..." (not played when disabled)

---

## 11. Post-Call Email

```python
# pipelines/gemini.py — finally block (always runs)
await asyncio.to_thread(send_call_summary_email, caller_id, transcript_log)
```

Sent via Gmail SMTP SSL (port 465) to admin email.
