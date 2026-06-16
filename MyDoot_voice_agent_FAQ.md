# MyDoot Voice Agent — Comprehensive FAQ

> **Project:** Mydoot AI Voice Agent for Home Services
> **Stack:** Python, Gemini 3.1 Flash Live, Vobiz SIP, Cloud Run, PostgreSQL, Google Sheets
> **Author:** Alok Ranjan

---

## 0. End-to-End Flow & Architecture

### Explain the complete end-to-end flow of a customer call

#### Architecture Diagram

```
┌──────────┐     mu-law 8kHz      ┌──────────────┐    PCM 16kHz     ┌──────────┐
│ Customer │◄───────────────────►│  Cloud Run    │◄──────────────►│  Gemini  │
│  Phone   │     WebSocket #1    │  (Python)     │   WebSocket #2  │  3.1     │
└──────────┘                     │               │                 │  Flash   │
                                 │  audioop      │                 │  Live    │
    Vobiz SIP                    │  resample     │                 │          │
    +917971542939                │               │                 │ Voice:   │
                                 │  ┌─────────┐  │                 │ Aoede    │
                                 │  │ asyncio │  │                 └────┬─────┘
                                 │  │ 2 tasks │  │                      │
                                 │  └─────────┘  │               toolCall
                                 │               │                      │
                                 │  ┌─────────┐  │    ┌──────────────┐  │
                                 │  │  save    │◄─────│ PostgreSQL   │◄─┘
                                 │  │  tool    │──────│ Google Sheets│
                                 │  └─────────┘  │    └──────────────┘
                                 └──────────────┘

Cost: ₹0.64/min  |  Latency: ~2s/turn  |  Concurrency: 80 calls/instance
```

#### Phase 1: Call Initiation

```
Customer dials +917971542939
       │
       ▼
Vobiz Telephony Platform (SIP provider)
       │
       ▼
POST /answer → our Cloud Run server
       │
       ▼
Server returns XML with WebSocket URL:
  wss://mydoot-voice-agent.../gemini-stream?caller_id=9582211961
       │
       ▼
Vobiz opens bidirectional WebSocket
  (carries G.711 mu-law audio at 8kHz)
```

**Interview answer:** *"When a customer calls our Vobiz number, Vobiz sends a POST webhook to our Cloud Run server. We respond with XML containing a WebSocket URL. Vobiz then opens a bidirectional WebSocket that carries real-time audio in G.711 mu-law format at 8kHz — this is the standard phone codec."*

#### Phase 2: Gemini Connection

```
Server simultaneously opens WebSocket to:
  wss://generativelanguage.googleapis.com/.../BidiGenerateContent

Sends setup message:
  {
    model: "gemini-3.1-flash-live-preview",
    responseModalities: ["AUDIO"],
    speechConfig: { voiceName: "Aoede" },
    systemInstruction: "You are Mydoot Customer Care...",
    tools: [save_service_request],
    inputAudioTranscription: {},
    outputAudioTranscription: {}
  }

Sends greeting trigger:
  { realtimeInput: { text: "[CALL_STARTED]" } }

Gemini generates greeting audio
```

**Interview answer:** *"We maintain two WebSocket connections simultaneously — one to Vobiz (customer audio) and one to Gemini Live API. The setup message configures the AI model, voice, system prompt, and available tools. We trigger the greeting by sending a text message through realtimeInput."*

#### Phase 3: Audio Pipeline (the core)

```
INBOUND (Customer → AI):
  Vobiz sends mu-law 8kHz audio frames
    → base64 decode
    → audioop.ulaw2lin()     (mu-law → PCM 16-bit)
    → audioop.ratecv()       (8kHz → 16kHz)
    → base64 encode
    → Send to Gemini as realtimeInput.audio

OUTBOUND (AI → Customer):
  Gemini sends PCM 24kHz audio
    → base64 decode
    → audioop.ratecv()       (24kHz → 8kHz)
    → audioop.lin2ulaw()     (PCM → mu-law)
    → base64 encode
    → Send to Vobiz as playAudio event
```

**Interview answer:** *"This is the most critical part. We're bridging two different audio worlds. Phone networks use G.711 mu-law at 8kHz. Gemini expects PCM at 16kHz for input and produces PCM at 24kHz for output. I use Python's audioop library for real-time resampling. The entire pipeline runs in asyncio — two concurrent coroutines, one reading from Vobiz and forwarding to Gemini, the other reading from Gemini and forwarding to Vobiz. All in-memory, no disk I/O."*

#### Phase 4: Conversation Flow

```
Gemini handles EVERYTHING natively:
  - Turn detection (VAD)
  - Speech recognition
  - Language understanding
  - Response generation
  - Speech synthesis

Conversation stages (guided by system prompt):
  1. Greeting → full service list
  2. Category → "Achcha, electrical problem hai"
  3. Subcategory → "Light chali gayi hai"
  4. Diagnosis → "Kya specific fault hai?"
  5. Brand → (only for appliances/vehicles)
  6. Address → echo back: "15 Janpath, sahi hai?"
  7. Preferred time → "Kal subah 10 baje"
  8. Customer name → proceed to save

KEY RULE: Echo back every input
  Customer: "Crompton"
  Agent: "Achcha, Crompton. Aapka address kya hai?"
  (catches mishearing immediately)
```

**Interview answer:** *"With native audio mode, Gemini handles turn detection, speech recognition, and response generation in one hop — no separate STT service. The system prompt guides it through 8 stages. The critical design decision is echo-back: the agent repeats every key input so the customer can catch errors. For example, if Gemini mishears 'Sethi Max' as 'Princesley state', the customer immediately corrects it."*

#### Phase 5: Tool Call & Save

```
All fields collected
       │
       ▼
Gemini calls save_service_request tool:
  {
    customer_name: "Sonu Kumar",
    category: "Electrical",
    subcategory: "Wiring",
    issue_type: "Power Failure",
    address: "15 Janpath, New Delhi",
    preferred_time: "aaj shaam 5 baje"
  }
       │
       ├──► PostgreSQL (primary) — INSERT into service_requests
       ├──► Google Sheets (secondary, soft-fail) — append row
       │
       ▼
Tool returns { success: true } to Gemini
       │
       ▼
Gemini speaks confirmation:
  "Sonu ji, aapki request register ho gayi hai..."
       │
       ▼
End-marker "shukriya" detected → stop forwarding audio
       │
       ▼
3-second drain delay → close both WebSockets
       │
       ▼
Transcript emailed to admin via Gmail SMTP
```

**Interview answer:** *"When Gemini has all fields, it calls our save_service_request tool. We save to PostgreSQL as the primary store and Google Sheets as secondary with soft-fail. The tool returns success, Gemini speaks the confirmation. I detect the word 'shukriya' in the output transcription to know the confirmation is complete, then close the call after a 3-second drain delay so the customer hears the full message."*

#### Phase 6: Edge Cases I Solved

**Q: What if Gemini disconnects mid-call?**
*"Gemini sometimes closes the WebSocket with code=1000 when it thinks the conversation is done — like after saying 'main samajh gayi'. I catch both ConnectionClosedError and ConnectionClosedOK, reconnect to Gemini, send the current stage context, and the conversation resumes. The customer doesn't notice."*

**Q: What about duplicate confirmations?**
*"Gemini sometimes repeats the closing message twice. I detect 'shukriya' in the output transcription and immediately stop forwarding new audio. A 9-second byte cap catches anything the text detection misses."*

**Q: What about the save tool call getting interrupted?**
*"When the save tool call starts, I set a save_tool_pending flag that blocks customer audio to Gemini. Without this, residual audio triggers an interrupt and Gemini never speaks the confirmation."*

**Q: What about barge-in?**
*"In native audio mode, Gemini handles barge-in natively — when the customer speaks while the agent is talking, Gemini stops generating and processes the interruption. I send a clear event to Vobiz to flush any buffered audio."*

---

## 1. Architecture & Core Infrastructure

### Why did you use Cloud Run?

I needed a serverless platform that supports long-running WebSocket connections, scales to zero when idle, and deploys via Docker — Cloud Run checks all three.

**WebSocket support with long timeouts:** Voice calls can last up to 60 minutes. Cloud Run allows timeout up to 3600 seconds (1 hour). AWS Lambda caps at 15 minutes. Cloud Functions cap at 9 minutes. Neither can hold a WebSocket open for a full phone call.

**Scale to zero, pay per use:** At night there are zero calls. I don't want to pay for idle servers. Cloud Run scales to zero — I only pay when a call is active. But I keep `min-instances=1` so the first call doesn't hit a cold start.

**Scales up automatically:** Each instance handles 80 concurrent WebSocket connections. If traffic spikes, Cloud Run auto-scales up to 10 instances = 800 concurrent calls. No load balancer config needed.

**Docker-based = full control:** I need Python 3.11 with audioop (removed in 3.13), specific pip packages, and a non-root user. Cloud Run runs any Docker container — I have full control over the runtime.

**Simple CI/CD:** Push to main → GitHub Actions builds Docker image → pushes to Artifact Registry → deploys to Cloud Run. The whole pipeline is 40 lines of YAML. New features go live in 3-4 minutes.

**Why NOT other options?**

| Option | Why I rejected it |
|---|---|
| AWS Lambda | 15-min timeout, no persistent WebSocket |
| Cloud Functions | 9-min timeout, no WebSocket support |
| EC2 / GCE VM | Always-on cost, manual scaling, manual deployment |
| Kubernetes (GKE) | Overkill for a single service, complex operations |
| App Engine | Less Docker control, WebSocket support is limited |

---

### Why is PostgreSQL used and where is it deployed?

I need a reliable, persistent database that survives container restarts and is accessible across all Cloud Run instances.

**What PostgreSQL stores:**

| Table | Purpose |
|---|---|
| `service_requests` | Customer bookings — the actual business data |
| `call_logs` | Per-call observability (duration, STT count, barge-ins, transcript) |
| `turn_latency_metrics` | Per-turn breakdown (VAD ms, STT ms, LLM first token ms) |
| `instances` | Multi-tenant tracking |

**Why not just Google Sheets?** I actually use both. Sheets is secondary — the client sees bookings there in a familiar UI. But Sheets has rate limits (60 writes/min), random API failures, no SQL querying, and race conditions on concurrent writes. I write to PostgreSQL first (primary), then Sheets (secondary, soft-fail). If Sheets API fails, the booking is still safe in PostgreSQL.

**Deployment:** PostgreSQL is hosted on Supabase (managed PostgreSQL-as-a-service, free tier — 500MB storage). Cloud Run connects over TLS via a connection string in environment variables. For production at scale, I'd migrate to Cloud SQL for VPC-level security and ~2ms latency (vs ~20-50ms over public internet).

---

### Is PostgreSQL on a GCP Compute Engine instance?

No. PostgreSQL is on Supabase (external managed service), not inside GCP. Cloud Run connects to it over the public internet with TLS encryption.

```
Cloud Run (GCP, us-central1)
       │
       │  Public internet (TLS encrypted)
       ▼
Supabase PostgreSQL (external)
```

For production at scale, I'd move to Cloud SQL (same GCP VPC, ~2ms latency, 99.95% SLA). The migration is simple — just change the `POSTGRES_URL` environment variable. The code uses standard psycopg2, no Supabase-specific SDK.

---

### Why asyncio and not threading or multiprocessing?

Because the workload is I/O-bound, not CPU-bound. Each call has two WebSockets (Vobiz + Gemini) that spend 95% of their time waiting for network data.

- **asyncio** handles thousands of idle connections in a single thread with near-zero overhead
- **Threading** would waste memory — 8MB stack per thread × 80 connections = 640MB
- **Multiprocessing** can't share WebSocket state across processes

The only CPU-bound work is audioop resampling, which is a C extension (doesn't hold the GIL). Database writes use `asyncio.to_thread()` to offload blocking psycopg2 calls to a thread pool without blocking the event loop.

---

### Why not use Twilio instead of Vobiz?

Cost. Twilio charges ~₹4-5/min for Indian PSTN numbers. Vobiz charges ₹0.45/min. For an MVP handling hundreds of calls, that's the difference between ₹500/day and ₹5000/day.

The WebSocket API is similar — both provide bidirectional audio streams with G.711 mu-law codec. If we expand internationally, I'd switch to Twilio for global coverage and reliability. But for India-only, Vobiz is 10x cheaper with comparable quality.

---

### Why not use a framework like LiveKit, Pipecat, or Vocode?

Control. These frameworks abstract the audio pipeline — great for demos but painful when you need:

- Custom barge-in thresholds for noisy Indian PSTN lines
- Echo-guard timing specific to Vobiz's audio buffering
- Post-save audio blocking (tool call timing)
- Reconnect with stage context on Gemini disconnect
- Dual-write to PostgreSQL + Sheets with soft-fail

These are edge cases that break in production. With a framework, I'd be fighting the abstraction. With raw WebSockets, I control every audio frame and every timing decision. The total code is ~1800 lines — not much more than configuring a framework, but fully debuggable.

---

## 2. Audio Processing, VAD & Edge Cases

### Where did you handle VAD and barge-in — Gemini native or custom logic?

The answer depends on which mode is active. I built both approaches and the system can switch between them.

**Mode 1 — Native Audio (Current Production, `NATIVE_AUDIO_INPUT=1`):**

```
Customer audio → PCM 16kHz → realtimeInput.audio → Gemini
```

Gemini handles everything internally: VAD, turn detection, barge-in, speech recognition, response generation, and speech synthesis. My Python server is just a dumb audio bridge — resample and forward. No custom VAD logic runs.

**Mode 2 — Legacy STT Path (Fallback, `NATIVE_AUDIO_INPUT=0`):**

```
Customer audio → Custom VAD → Sarvam STT → text → Gemini
```

I built custom VAD using RMS (root mean square) energy detection:
- **Speech threshold:** RMS ≥ 100 (filters fan noise)
- **Silence gap:** 200ms triggers end-of-utterance
- **Min duration:** 300ms (discards coughs, door slams)
- **Barge-in threshold:** RMS ≥ 350 (3.5x VAD threshold) sustained for 300ms

The high barge-in threshold (350 vs 100) is critical — during barge-in, the agent's own audio leaks back through the customer's phone speaker. At 100, echo and fan noise trigger false barge-ins. At 350, only deliberate loud speech triggers it.

---

### What about barge-in?

In native audio mode, Gemini handles barge-in natively. When the customer speaks while the agent is talking, Gemini sends an `interrupted` signal, stops generating, and processes the customer's speech.

One important exception: after `save_service_request` completes, I IGNORE interrupt signals. Residual customer audio in Gemini's buffer triggers false interrupts that would kill the confirmation message.

```python
if server_content.get("interrupted"):
    if save_done_ts > 0:
        log("⚡ interrupted — IGNORED (post-save, residual audio)")
    else:
        log("⚡ interrupted — real customer barge-in")
```

In the legacy path, I detect barge-in manually: sustained RMS ≥ 350 for ≥ 0.3 seconds → send `clear` event to Vobiz (flush agent audio) → process the interrupting speech as a new utterance.

---

### What if Gemini disconnects mid-call?

Gemini sometimes closes the WebSocket with `code=1000` mid-conversation — for example, after the agent says "main samajh gayi" (I understand), Gemini interprets it as conversation done.

I handle both abnormal and clean disconnects:

```
ConnectionClosedError (abnormal) → reconnect
ConnectionClosedOK (code=1000, clean) → reconnect if save not done
```

On reconnect: open new Gemini WebSocket → send setup → send current stage context ("address collected, need preferred_time...") → Gemini resumes naturally. The customer doesn't notice — they just hear the agent continue.

The reconnect limit is 1 per call to prevent infinite loops. If reconnect fails, the call ends gracefully with a transcript email.

---

### What about duplicate confirmations?

Gemini sometimes repeats the closing message twice ("Sonu ji, aapki request register ho gayi hai... shukriya! Sonu ji, aapki request register..."). Three layers prevent this:

**Layer 1 — End-marker detection:** I monitor the output transcription for "shukriya" or "thank you for calling." The moment it appears, I stop forwarding new audio to Vobiz. A 3-second drain delay lets Vobiz play the already-buffered audio.

**Layer 2 — Byte cap (9 seconds):** One confirmation is ~6-7 seconds. A 9-second cap allows the full message but catches any duplicate (which would be ~12-14 seconds).

**Layer 3 — turnComplete guard:** After the first turnComplete post-save with ≥ 4 seconds of audio played, the call closes. If less than 4 seconds played (incomplete confirmation), it nudges Gemini to retry.

---

### What about the save tool call getting interrupted?

When `save_service_request` starts, I set a `save_tool_pending` flag that blocks customer audio to Gemini immediately. Without this, residual audio flows during the 1-2 second tool execution, triggering Gemini's interrupt signal — Gemini never speaks the confirmation, and the call drops silently.

```
Tool call starts → save_tool_pending = True → audio blocked
Tool completes → save_done_ts set → audio stays blocked
Gemini speaks confirmation uninterrupted
```

---

## 3. System Reliability & Model Behavior

### What if Gemini hallucinates — saves wrong data?

This happened. Gemini 2.5 hallucinated "Aditya Urban Casa" when the customer said "Sethi Max" and "Sector 78" became "Sector 70". Three mitigations:

**1. Echo-back confirmation:** The agent repeats every input before moving on. "Achcha, Sethi Max, Sector 78, Noida. Technician kab aaye?" — the customer corrects immediately if wrong. This turns a silent error into an audible one.

**2. Model upgrade:** Switched from Gemini 2.5 to 3.1 Flash Live, which has significantly better Hindi speech recognition. Brand names and addresses are now mostly accurate.

**3. Fallback path:** `NATIVE_AUDIO_INPUT=0` reverts to Sarvam STT → text → Gemini. Separate STT gives an explicit transcript — no audio hallucination possible.

---

### How do you ensure the tool call always fires?

This was a real bug. Gemini sometimes generated the confirmation message WITHOUT calling `save_service_request` — the customer heard "aapki request register ho gayi hai" but nothing was saved.

**Layer 1 — System prompt:** "Call save_service_request tool IMMEDIATELY. Do NOT say anything before the tool call. MANDATORY — NO EXCEPTIONS."

**Layer 2 — Agent speech monitoring:** When Gemini's transcript contains confirmation keywords but `save_executed` is still False, it's flagged as a hallucination in metrics.

**Layer 3 — Stage advancement from agent speech:** When Gemini asks for address/time/name, the ServiceGraph advances the stage so the next turn's context says `stage=done`, which forces the tool call.

With Gemini 3.1 and native audio, tool call reliability is significantly higher than 2.5.

---

### What's the hardest bug you fixed?

The silent call drop after save. The customer heard nothing — call just died. Took 3 iterations:

**Iteration 1:** Discovered Gemini's `interrupted` signal was firing from residual customer audio still in the buffer. Fix: ignore interrupts after save.

**Iteration 2:** `turnComplete` fired with 0 seconds of audio. The code assumed first turnComplete = confirmation done. Fix: check `audio_secs > 4.0`, else nudge Gemini to retry the confirmation.

**Iteration 3:** Audio was forwarded but Vobiz buffer was flushed. Gemini generates 6 seconds of audio in 1.6 seconds of wall time. The byte cap fired and cleared the buffer before the customer heard it. Fix: removed the `clear` event on byte cap, added 5-second drain delay.

Each fix exposed the next layer. The root cause was the mismatch between Gemini's faster-than-real-time audio generation and Vobiz's real-time playback. **In voice systems, timing bugs are harder than logic bugs.**

---

## 4. Scale, Operations & Governance

### How do you test it at scale — say 100 concurrent calls?

I haven't done 100 concurrent call testing yet — the product is in MVP stage. But I've designed the architecture for it.

**Approach:** Simulate WebSocket connections directly — no real phone calls needed. A Python script opens 100 WebSockets to `/gemini-stream`, sends pre-recorded mu-law audio at real-time pace (20ms chunks), and measures responses.

**Key bottlenecks at 100 calls:**

| Bottleneck | Issue | Fix |
|---|---|---|
| Gemini API quotas | AI Studio caps at ~3 concurrent sessions | Switch to Vertex AI (1000+ sessions) |
| Cold starts | 5-8s delay if Cloud Run spins up new instance | `min-instances=2`, CPU always allocated |
| Python GIL | 80 concurrent audio resampling streams | Lower concurrency to 30-40, or rewrite bridge in Go |
| PostgreSQL pool | Current max=5 connections | Increase to max=20, or switch to asyncpg |
| Google Sheets | 60 writes/min limit | Sheets is secondary — PostgreSQL handles the load |
| audioop CPU | Resampling 100 streams on 1 vCPU | Scale to 2 vCPU per instance |
| Live API session limits | Sessions may expire after 10-15 minutes | Graceful reconnect with context (already built) |

**Cloud Run scaling math:** 100 calls ÷ 80 concurrency = 2 instances. Cloud Run auto-creates the second instance in ~5-8 seconds (mitigated by `min-instances=2`).

---

### What is the current governance and how do you improve it for production?

**Current state (MVP):**

| Area | State | Grade |
|---|---|---|
| Secrets management | GitHub Secrets → Cloud Run env vars | B |
| Authentication | `--allow-unauthenticated` (public) | D |
| Monitoring | Custom metrics dashboard, latency tracker | B |
| Alerting | None — manual log checking | F |
| Data privacy | Audio in-memory only, not persisted | B |
| Access control | None — anyone with URL can hit endpoints | D |
| CI/CD | Auto-deploy on push to main, no staging | B- |
| Testing | Manual call testing only | D |

**Production improvements:**

- **Access control:** Google IAP for dashboard/admin endpoints, IP whitelist for Vobiz webhook
- **Secrets:** Google Secret Manager (audit logs, rotation without redeploy)
- **CI/CD:** Staging environment → automated smoke tests → canary deployment (10% traffic) → auto-rollback
- **Alerting:** Cloud Monitoring → PagerDuty/Slack for call failure rate >5%, latency spike P95 >6s, Gemini reconnects >10 in 10 minutes
- **Data privacy:** PII masking in logs, encrypted transcripts, 90-day auto-purge, DPDPA 2023 consent
- **Testing:** Unit tests (audio resampling, validators), integration tests (simulated calls), load tests (100 concurrent)

---

### What's your observability story? How do you debug a bad call?

Every call generates an `EVT_SUMMARY` — a timeline of events with millisecond timestamps:

```json
[
  {"t_ms": 14817, "evt": "turn_complete", "tc_seq": 1},
  {"t_ms": 77756, "evt": "tool_call_start", "fn": "save_service_request"},
  {"t_ms": 78959, "evt": "tool_call_complete", "took_ms": 1202},
  {"t_ms": 81187, "evt": "confirmation_done", "reason": "end_marker", "audio_secs": 6.02}
]
```

If a customer complains, I search Cloud Logging by caller ID, pull the EVT_SUMMARY, and see exactly what happened — which turn was slow, whether the tool call fired, how much confirmation audio played.

The `/calls` dashboard shows this visually with chat bubbles, latency charts, and an in-browser audio player. Per-turn latency warnings fire automatically when STT > 800ms or end-to-end > 4000ms.

---

### How do you handle sensitive data like addresses and phone numbers in logs?

Currently, I don't mask PII — this is a known MVP gap. Phone numbers appear in every log line, transcripts are emailed in plain text.

**Production plan:**

| Data | Current | Production |
|---|---|---|
| Phone numbers | Stored as-is | Masked: 95822***61 in logs |
| Transcripts | Emailed plain text | Encrypted at rest, retention policy |
| Call recordings | Optional, local disk | GCS with 90-day auto-delete lifecycle |
| PII in logs | Full caller ID | Pseudonymized hash |
| Consent | None | IVR consent message before recording |
| DPDPA compliance | Not addressed | Right to deletion API |

---

## 5. Product Experience & Flexibility

### What happens if the customer speaks a language you don't support?

The system prompt has a hard rule: only Hindi/Hinglish and English. If Gemini detects Tamil, Bengali, Marathi, or any other language, the agent says: "Maafi chahti hoon, yeh service sirf English aur Hindi mein available hai." Then continues in Hinglish.

Gemini's native audio handles language detection automatically — no separate classifier needed.

**Adding Tamil support** would require two changes: update the system prompt to include Tamil, and test if Gemini 3.1's native audio handles Tamil speech well. If not, fall back to the STT path with Google Speech-to-Text using Tamil language hints. It's a prompt change, not a code rewrite.

---

### How would you add a new service category — say 'Pest Control'?

Zero code changes. It's entirely prompt-driven. I'd add "Pest Control" to the service list in the system prompt, and Gemini would start recognizing it. The tool schema already accepts any category as a STRING — no enum restriction.

This is a deliberate design decision — the conversation flow is in the prompt, not in code. Adding a service is a 1-line config change, not a PR.

---

### If you had to rebuild this from scratch, what would you change?

**1. Start with native audio from day one.** I built the entire Sarvam STT pipeline first, then migrated to native audio. If I knew Gemini 3.1 would handle Hindi well, I'd skip the STT path and save 2 weeks.

**2. Use asyncpg instead of psycopg2.** psycopg2 is synchronous — I wrap every DB call in `asyncio.to_thread()`. asyncpg is natively async and eliminates thread pool overhead.

**3. Build reconnect logic from the start.** Gemini disconnects were the #1 production issue. I'd design the reconnect-with-context pattern into the architecture from day one, not bolt it on after customer complaints.

---

## 6. Business Value & Problem Solving

### How does this architecture solve core operational bottlenecks?

The client (Mydoot) was handling home service requests manually — a team of 3-4 agents answering calls during business hours. Key problems:

- **Missed calls after hours:** 40% of calls came between 7 PM and 9 AM when no one was available
- **Incomplete bookings:** Human agents forgot to collect address or preferred time in ~15% of calls
- **Inconsistent data entry:** Same service logged differently by different agents ("AC repair" vs "Air Conditioner" vs "Split AC not cooling")
- **Cost:** ₹15,000-20,000/month for 3 agents handling ~50 calls/day

**What the AI agent solves:**

| Problem | Before | After |
|---|---|---|
| Availability | Business hours only | 24/7, zero missed calls |
| Data completeness | ~85% fields filled | 100% — structured 8-stage flow enforces it |
| Data consistency | Free-text, inconsistent | Standardized categories, subcategories, issue types |
| Cost per call | ~₹10-15 (human agent time) | ₹0.96 (Gemini + Vobiz) |
| Scale | 3 agents, ~50 calls/day max | Auto-scales to 800 concurrent calls |

---

### What are the key business metrics this system improves?

| Metric | Impact |
|---|---|
| **Cost per booking** | ₹10-15 → ₹0.96 (93% reduction) |
| **Booking conversion rate** | Higher — no missed calls, no incomplete forms |
| **First response time** | Minutes (human picks up) → instant (AI answers) |
| **After-hours coverage** | 0% → 100% |
| **Data quality** | Inconsistent free-text → structured, validated fields |
| **Agent workload** | 50 calls/day manual → 0 (fully automated) |

---

### Why an AI voice agent and not a web form or text chatbot?

**The customer demographic.** Mydoot serves home service customers across India — many are not tech-savvy, don't use apps, or are in an urgent situation (AC broken in 45°C, water leak flooding the kitchen). They pick up the phone and call.

**Voice is the natural interface for urgency.** When your kitchen is flooding, you don't fill out a web form. You call someone. The AI voice agent answers in 2 seconds, speaks your language (Hindi/Hinglish), and books the plumber in 90 seconds.

**Web forms have abandonment rates of 60-70%** for service bookings. Phone calls have near-zero abandonment once someone picks up. The AI ensures someone always picks up.

---

## 7. Advanced Engineering & Optimization

### How do you monitor and optimize end-to-end turn latency?

Every turn is instrumented with a `LatencyTracker` that captures 10+ timestamps:

```
Customer speaks → vad_start → vad_end → stt_start → stt_end →
  langgraph_start → langgraph_end → gemini_send →
  gemini_first_audio → gemini_turn_end
```

Automatic warnings fire when thresholds are breached:
- STT > 800ms
- LLM first token > 2500ms
- End-to-end turn > 4000ms

These are stored in PostgreSQL (`turn_latency_metrics` table) and visible on the `/calls` dashboard.

**Optimizations I've made:**
- Switched to native audio (eliminated 1.5s STT hop)
- Persistent HTTP sessions for Sarvam STT (saved 200-300ms TCP+TLS per utterance)
- Audio amplitude normalization for quiet PSTN calls
- Context compression (65% fewer tokens in stage context)

---

### If call volume spikes 10x unexpectedly, what breaks first?

**In order of failure:**

1. **Gemini API quota** — hits concurrent session limit first. Fix: pre-request quota increase from Google, use Vertex AI instead of AI Studio.

2. **Cloud Run cold starts** — new instances take 5-8 seconds. Fix: increase `min-instances` to match expected baseline. Customers hear dead silence during cold start.

3. **PostgreSQL connection pool** — max=5 connections exhausted with 50+ concurrent saves. Fix: increase to max=20 or switch to asyncpg.

4. **Google Sheets API** — 60 writes/min limit. Fix: already handled — Sheets is soft-fail secondary.

5. **Cloud Run instance limit** — max-instances=10 caps at 800 concurrent. Fix: increase limit in GCP console.

The architecture is designed so each layer fails independently and gracefully. Sheets failing doesn't affect PostgreSQL. PostgreSQL failing doesn't affect the call. The call continues even if persistence fails — the customer always hears the confirmation.

---

### How do you evaluate qualitative performance — tone, empathy, accuracy?

**Quantitative signals I track today:**

| Signal | How | What it tells me |
|---|---|---|
| Hallucination count | Gemini says confirmation without tool call | Accuracy |
| Interruption count | Barge-in events per call | Pace issues (agent talking too long) |
| English fallback count | >10% ASCII in agent response | Language detection failure |
| STT corrections | Customer corrects after echo-back | Speech recognition quality |
| Call duration | Saved per call | Efficiency (shorter = better) |
| Stage reached | Last stage before disconnect | Drop-off points |

**What I'd add for production:**

- **Post-call CSAT:** Short IVR survey after confirmation ("Press 1 if satisfied")
- **Transcript review:** Weekly sample of 20 calls reviewed manually for tone, empathy, missed context
- **A/B testing voices:** Compare Aoede vs Kore vs Puck on customer satisfaction
- **Prompt regression testing:** Replay recorded calls through new prompt versions, compare transcripts
- **Sentiment analysis:** Run Gemini on transcripts to score customer sentiment per turn — detect frustration early

---

### How did you implement Cloud Logging, Cloud Trace, and Cloud Monitoring?

I built a structured JSON logging module (`config/cloud_logging.py`) that integrates with Google Cloud's operations suite.

**Cloud Logging (structured JSON):**
Every log line is a JSON object with fields like `severity`, `caller_id`, `stage`, `event`, and `latency_ms`. On Cloud Run, these JSON payloads are automatically ingested by Cloud Logging with full field indexing — so I can filter by `jsonPayload.caller_id="9582211961"` or `jsonPayload.event="tool_call_complete"` to debug a specific call.

**Cloud Monitoring (log-based metrics):**
I defined log-based metrics that extract numeric values from structured logs — for example, a metric on `jsonPayload.latency_ms` filtered to `jsonPayload.event="turn_complete"` gives me per-turn latency as a Cloud Monitoring time series. I can set alerts when P95 latency exceeds 6 seconds or when error rate spikes above 5%.

**How it works in the code:**
`config/cloud_logging.py` configures Python's `logging` module to emit JSON to stdout. On Cloud Run, stdout is captured by the Cloud Logging agent automatically. Locally, it falls back to human-readable console output. Every pipeline event (call start, turn complete, tool call, reconnect, call end) emits a structured log with consistent field names, making it easy to build dashboards and alerts without any external logging SDK.

**Why not a third-party logging service?**
Cloud Logging is free for the first 50 GB/month, integrates natively with Cloud Run (zero config), and feeds directly into Cloud Monitoring for alerts. External services like Datadog or Splunk would add cost and another dependency for no real benefit at this scale.

---

### How does the language selection feature work?

Instead of auto-detecting the customer's language (which was unreliable on short utterances), I switched to explicit language selection at the start of every call.

**Flow:**
1. Agent greets in Hinglish: "Namaste! Mydoot Customer Care mein aapka swagat hai..."
2. Agent immediately asks: "Hindi mein baat karein ya English mein?"
3. Customer responds with their preference
4. Gemini continues the entire conversation in the chosen language

**Why explicit selection over auto-detection?**
Auto-detection required 2-3 sentences of speech to reliably identify the language. During that ramp-up period, the agent sometimes switched languages mid-sentence or defaulted to the wrong language. With explicit selection, the customer is in control from the first turn, and Gemini's system prompt locks the language for the rest of the call.

**Implementation:**
This is entirely prompt-driven — the system prompt instructs Gemini to ask for language preference after the greeting and then stick to the chosen language. No code changes were needed. The compressed system prompt (~620 tokens, 75% reduction from the original ~2500 tokens) includes the language selection instruction.

---

## Key Numbers to Remember

| Metric | Value |
|---|---|
| Response latency | ~2 seconds per turn |
| Cost per minute | ₹0.64 (Gemini ₹0.19 + Vobiz ₹0.45) |
| Cost per call (avg) | ₹0.96 |
| Avg call duration | 1.5 minutes |
| Concurrent calls | 80 per instance, auto-scales to 10 instances |
| Audio formats | mu-law 8kHz (phone) ↔ PCM 16kHz/24kHz (Gemini) |
| Model | gemini-3.1-flash-live-preview |
| Voice | Aoede |
| Deployment | Cloud Run, us-central1, 512Mi, 1 vCPU |
| CI/CD | GitHub Actions, ~3-4 min deploy |
| Codebase | ~4300 lines Python |
