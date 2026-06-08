# Technical Architecture: Mydoot Customer Care Voice Agent

**Agent Name:** Mydoot Customer Care Representative
**Active Pipeline:** Sarvam Saaras v3 STT + Gemini Live LLM+TTS (text-in, audio-out)
**Orchestration:** LangGraph ServiceGraph (stage context injection per turn)
**Stack:** Gemini 2.5 Flash Native Audio, Sarvam Saaras v3, Sarvam Bulbul v2, LangGraph, Vobiz SIP, PostgreSQL, Google Sheets, Gmail SMTP, Google Cloud Run

---

## 1. System Overview

Mydoot Customer Care is an AI voice agent that handles inbound phone calls on the Vobiz SIP platform. It uses a hybrid pipeline:
- **Sarvam Saaras v3** (REST API) for speech-to-text — transcribes customer audio with local VAD pre-filtering
- **Google Gemini Live** (text-in, audio-out) for LLM reasoning and TTS voice synthesis
- **LangGraph ServiceGraph** for structured conversation orchestration — injects compressed stage context into each Gemini turn
- **Sarvam Bulbul v2** for optional local TTS (currently disabled; voice quality not acceptable)

Customer audio goes through: Vobiz mu-law 8kHz → local VAD → Sarvam STT → text → Gemini Live → audio response.

**Feature flags:**
- `NATIVE_AUDIO_INPUT=0` (default) — Legacy STT path. Proven reliable.
- `NATIVE_AUDIO_INPUT=1` — Experimental: streams raw audio to Gemini. Lower latency (~1.5s vs ~3s) but causes hallucinations on PSTN audio. Disabled after testing.
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
│ (Vobiz → VAD → STT)  │    │ (Gemini → Vobiz)        │
│                      │    │                         │
│ mu-law 8kHz          │    │ PCM 24kHz               │
│ → ulaw2lin → PCM 8k  │    │ → ratecv(24000→8000)    │
│                      │    │ → lin2ulaw              │
│ VAD: RMS ≥ 100       │    │ → mu-law 8kHz           │
│ accumulate speech    │    │ → Vobiz playAudio        │
│ end on 0.2s silence  │    │                         │
│ min 0.3s utterance   │    │ Tracks turnComplete:    │
│                      │    │ flushes transcript buf  │
│ Sarvam Saaras v3     │    │ sets gemini_turn_end_ts │
│ REST → transcript    │    │                         │
│                      │    │ Truncates duplicate     │
│ ServiceGraph.        │    │ confirmation text at    │
│ get_context() inject │    │ first "shukriya"        │
│ → clientContent text │    │                         │
│ → Gemini Live        │    │ confirmation_audio_secs │
│                      │    │ accumulates post-save   │
│ caller_id sanitized  │    │ audio (float, not bool) │
│ (path traversal safe)│    │                         │
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
│    - First turnComplete after save → close call in 0.5s            │
│    - save_executed flag prevents duplicate execution per session    │
│    - Duplicate text truncated at first "shukriya" before flush     │
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
  Speech frames accumulated; silence > 0.2s triggers flush (VAD_END_SECS)
  Utterances < 0.3s discarded (noise blips)
        │
        ▼ Sarvam Saaras v3 REST  (POST /speech-to-text)
  Persistent aiohttp.ClientSession() per call
  model=saaras:v3 | language_code=hi-IN | file=audio.wav (8kHz WAV)
  STT hint phrases injected per-stage (brand, address, time)
        │
        ▼ Transcript string (Hinglish / English)
        │
        ▼ ServiceGraph.get_context() → compressed [STAGE] block
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
1. Save executes → `save_done_ts` set
2. Gemini speaks its own confirmation message
3. First `turnComplete` after save → `confirmation_done=True` → send `{"event":"clear"}` to Vobiz → close in 0.5s
4. Duplicate text truncated at first "shukriya" before flushing to transcript
5. Text and audio blocked after `confirmation_done` set

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
    "model": "models/gemini-2.5-flash-native-audio-latest",
    "generationConfig": { "responseModalities": ["AUDIO"] },
    "inputAudioTranscription": {},
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
| Input mode | clientContent text turns | STT text sent as user turns |
| inputAudioTranscription | Set | Captures customer speech for logging |
| outputAudioTranscription | Set | Captures agent speech for transcript |
| speechConfig | Not set | Causes 1008 errors on this model |

---

## 7. Security & Guardrails

- **caller_id sanitized**: `re.sub(r'[^a-zA-Z0-9_+\-]', '', caller_id)` — prevents path traversal in filenames
- **ReDoS prevention**: Time patterns use `[\w\s,।]{0,20}` instead of `.{0,20}`
- **Devanagari word boundaries**: `\b` not used with Devanagari (vowel signs are non-`\w`)
- **Local audio streaming**: `web.FileResponse` (no memory spikes for large WAVs)
- **Confirmation words**: Include Devanagari variants (जी, अच्छा, ठीक है, सही है, etc.)
- **Name rejection**: Multi-word affirmatives rejected as names (अच्छा जी, ठीक जी, etc.)

---

## 8. VAD — Voice Activity Detection

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

### Dashboard: `/calls`
- Per-call quality metrics (category, stage, saved, STT avg, drops, barge-ins)
- Per-call turn latency view (STT, LLM first token, end-to-end per turn)
- Transcript with chat bubbles
- Audio playback (GCS or local)

### Latency Metrics: `/latency`
- P50/P95/P99 for STT, LLM first token, end-to-end
- Stored in PostgreSQL `turn_latency_metrics` table
- Per-call drill-down via `/calls/latency?caller_id=XXX`

### Current Performance (P50)
- STT: ~900ms (Sarvam API bottleneck)
- LLM first token: ~2000ms (Gemini processing)
- End-to-end: ~8000ms (includes agent speaking time)

---

## 10. Sarvam TTS (Local, Optional)

**Status:** Disabled by default (`LOCAL_PROMPT_STAGES=""`)

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
