# PRD: Mydoot Customer Care — AI Voice Agent

**Version:** 4.1
**Owner:** Alok Ranjan
**Phone Number:** +917971542939
**Last Updated:** June 2026

---

## 1. Problem Statement

Customers needing home services — appliance repair, plumbing, electrical work, carpentry, cleaning, vehicle service — call a number and often face:

- Lines busy or unmanned outside business hours
- Agents taking details inconsistently (missing address, skipping problem description)
- Data never entering a structured format for service dispatch
- No confirmation that the request was logged
- Customers waiting on hold, getting frustrated, hanging up

**Result:** Service requests are lost, technicians lack context, and customers feel unheard.

---

## 2. Solution

An always-available AI voice agent that:
1. Answers every call instantly, 24/7
2. Auto-detects language (English or Hinglish) after the first response
3. Guides the customer through a structured service request form via natural conversation
4. Uses LangGraph to orchestrate the conversation stage by stage
5. Saves a complete 11-field structured record to Google Sheets
6. Emails the full transcript to the admin after every call

No hold time. No missed fields. No data entry lag.

---

## 3. Users

| User | Role |
|------|------|
| End Customer | Calls to book a home or appliance service |
| Technician Dispatcher | Reviews Google Sheet, assigns technicians |
| Admin (Alok Ranjan) | Receives transcript emails, monitors system |

---

## 4. Functional Requirements

### FR-1: Inbound Call Handling
- Agent answers all calls on +917971542939 within 2 rings
- No call should go unanswered due to concurrency limits (up to 10 simultaneous)
- Call must work 24/7/365

### FR-2: Language Auto-Detection (Silent)
- On call connect, the agent speaks a Hinglish greeting (one of 3 scripts, chosen randomly)
- After the customer's first full response, the agent silently detects language:
  - Customer responds exclusively in clear English with no Hindi words → conduct entire call in English
  - All other cases (Hindi, Hinglish, mixed, unclear, garbled, silent) → conduct entire call in Hinglish
- Default when in doubt: Hinglish
- Language is fixed for the rest of the call — no switching
- Agent never asks the customer to choose a language

### FR-3: Structured Service Request Collection (LangGraph-Guided)
The agent uses LangGraph (`core/service_graph.py`) to guide the conversation through stages in order. A `[STAGE CONTEXT]` block is injected with each customer turn telling Gemini exactly what to ask next.

Stage order:
```
category → subcategory → diagnosis → brand* → address → preferred_time → customer_name → done
*brand only asked for Appliance Repair and Vehicle Service categories
```

The **diagnosis** stage uses `DIAGNOSTIC_FLOWS` (20 subcategory entries in `core/service_graph.py`) to inject targeted questions per fault type, identify the structured `issue_type`, and auto-derive `severity`.

Fields collected:

| # | Field | Required | Notes |
|---|-------|----------|-------|
| 1 | category | Yes | Detected from description: Appliance Repair / Plumbing / Electrical / Carpentry / Cleaning / Vehicle Service / Other |
| 2 | subcategory | Yes | Specific type within category (e.g. Refrigerator, Pipe Leak, Wiring) |
| 3 | issue_type | Yes | Structured fault label from DIAGNOSTIC_FLOWS (e.g. Cooling Failure, Water Leakage Indoor, MCB Tripping) |
| 4 | severity | Auto | Derived from issue_type via severity_map: High / Medium / Low |
| 5 | error_code | No | Appliance display error code if shown (e.g. E3, F1); empty otherwise |
| 6 | brand | Conditional | Required for Appliance Repair and Vehicle Service; empty for others |
| 7 | model | No | Optional — captured if mentioned |
| 8 | address | Yes | Society name + area/locality for technician |
| 9 | preferred_time | Yes | When customer wants the technician to visit |
| 10 | customer_name | Yes | Collected LAST |

### FR-4: Conversational Rules
- One question at a time — always the next missing field per stage
- Never ask for information the customer has already provided anywhere in the conversation
- Accept category/subcategory hints from natural description: "mere fridge mein paani aa raha hai" → category=Appliance Repair, subcategory=Refrigerator
- Never go silent — if unclear, ask one short clarifying question
- If customer gives garbled or inaudible response, ask once to repeat
- **Confirm before advancing**: after each field is provided, echo it back and ask "sahi hai?" / "is that correct?" — only move to the next field after the customer confirms. If customer corrects, echo the new value and confirm again before advancing.

### FR-5: Data Persistence
- On completing all required fields, call `save_service_request` tool immediately
- Do NOT say "request registered" before the tool call succeeds
- Write one row per call to Google Sheets (Sheet1, appended, never overwrite)
- Sheet columns (A–K): Customer Name | Category | Subcategory | Issue Type | Brand | Model | Severity | Address | Preferred Time | Timestamp | Caller ID
- `save_executed` flag prevents duplicate Sheet rows per session

### FR-6: Post-Save Confirmation
- Call `save_service_request` tool IMMEDIATELY when all fields are confirmed — do NOT say anything to the customer before the tool call
- After save succeeds, speak the confirmation message exactly once — first word must be the customer's name:
  - Hinglish: "[name] ji, aapki request register ho gayi hai. Hamari team jald se jald, ek ghante ke andar aapse sampark karegi. My Doot ko call karne ke liye shukriya!"
  - English: "[name], your request has been registered. Our team will contact you as soon as possible, within an hour. Thank you for calling My Doot!"
- After confirmation, go completely silent — do not repeat, do not add anything

### FR-7: Post-Call Transcript Email
- After every call (completed or dropped), send email to admin
- Email contains: Caller ID, timestamp, full Agent+Customer transcript
- Send even if transcript is empty (confirms call happened)

### FR-8: Voice Quality
- Soft, warm, clear female voice (Aoede via Gemini Live)
- Slow, deliberate pace with natural pauses
- Empathetic tone throughout
- Female verb forms for self-reference in Hindi: "kar sakti hoon", "karungi", "bataungi"
- Gender-neutral forms when addressing the customer: "kar rahe hain" (not "kar rahi hain")

### FR-9: Noise Rejection (VAD) and Barge-in
- Customer audio is processed through a local Voice Activity Detector (VAD) before STT
- Only audio above RMS threshold (default: 100) is sent to Sarvam Saaras v3
- Utterances shorter than 0.3 s are discarded (avoids noise blips); silence gap of 0.3 s ends utterance
- This prevents PSTN line noise from triggering hallucinated transcriptions
- **Barge-in**: when the customer speaks while the agent is talking, a sustained RMS ≥ 350 for ≥ 0.3 s stops the agent's audio immediately (Vobiz `{"event": "clear"}`) and processes the customer's interruption. The high threshold (3.5× VAD) ensures fan noise and background sounds do NOT trigger barge-in.

---

## 5. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Call answer latency | < 3 seconds from ring to greeting |
| Agent response latency (TTFT) | < 3 seconds per turn (VAD_END_SECS=0.3 + VAD_MIN=0.3 + persistent aiohttp session + TTL-cached Sheets service) |
| Google Sheets write success | > 99% |
| Concurrent calls supported | 10 |
| Uptime | 99.5% (Cloud Run managed) |
| Call max duration | 10 minutes (hard limit) |
| Audio quality | Clear mu-law 8kHz, no distortion |
| Duplicate save protection | save_executed flag per session |

---

## 6. Data Model

### Google Sheet: mydoot_Customer_Care

| Column | Field | Type | Example |
|--------|-------|------|---------|
| A | Customer Name | String | Kumud Ranjan |
| B | Category | String | Plumbing |
| C | Subcategory | String | Pipe Leak |
| D | Issue Type | String | Water Leakage Indoor |
| E | Brand | String | Samsung *(Appliance/Vehicle only)* |
| F | Model | String | *(optional)* |
| G | Severity | String | High / Medium / Low |
| H | Address | String | Sector 15, Noida |
| I | Preferred Time | String | kal subah 10 baje |
| J | Timestamp | DateTime | 2026-06-01 10:15:22 |
| K | Caller ID | String | 917042915552 |

Sheet ID: `1uW39kklQKc4rhf5REATgKqgwbvSNAhlDVKXyAzOMKCk`

---

## 7. Technical Constraints

- **STT**: Sarvam Saaras v3 REST API (`saaras:v3`, hi-IN, 8kHz WAV) — replaces Gemini native audio input to eliminate PSTN noise hallucinations
- **Language model**: Google Gemini 2.5 Flash Native Audio (BidiGenerateContent, text-in / audio-out)
- **Conversation orchestration**: LangGraph `StateGraph` via `core/service_graph.py`; `DIAGNOSTIC_FLOWS` dict with 20 subcategory fault trees; `diagnosis` stage between subcategory and brand
- **STT latency optimization**: Persistent `aiohttp.ClientSession()` per call (avoids TCP+TLS handshake per utterance, saves ~200–300ms); `VAD_END_SECS` default 0.3 s, `VAD_MIN_SPEECH_SECS` default 0.3 s (catches short responses like "LG", "haan", "kal")
- **Sheets latency optimization**: `_get_sheets_service()` caches the service object with a 3000s TTL (saves ~500ms discovery-doc + TCP handshake per save); `headers_written` flag skips ~300ms header GET on subsequent saves; stale-connection auto-retry on connection errors
- **Telephony**: Vobiz SIP (+917971542939)
- **Audio codec**: mu-law 8kHz (Vobiz ↔ server), PCM 24kHz (Gemini → server)
- **Infrastructure**: Google Cloud Run (us-central1, project testcnx-169610)
- **Data storage**: Google Sheets only (no database)
- **Notification**: Gmail SMTP SSL port 465 (App Password auth)

---

## 8. Success Metrics

| Metric | Target |
|--------|--------|
| Service request completion rate | > 85% of calls where customer speaks |
| Data completeness | 100% of saved rows have all required fields |
| Average handle time | 2–4 minutes |
| Call drop rate (before completion) | < 15% |
| Sheet write latency | < 5 seconds after all fields collected |
| Transcript email delivery | 100% of completed calls |
| False STT triggers (noise) | < 5% of total STT calls |

---

## 9. Out of Scope (v4.0)

- CRM or ticketing system integration (Freshdesk, Zoho, etc.)
- SMS/WhatsApp confirmation to customer after registration
- Outbound call-back scheduling
- Sentiment analysis
- Regional languages beyond English and Hinglish
- Real-time dashboard for request volume/trends
- IVR menu (press 1 for plumbing, press 2 for electrical)

---

## 10. Future Roadmap

| Priority | Feature |
|----------|---------|
| High | WhatsApp confirmation message to customer after registration |
| High | Technician assignment + estimated arrival time communicated to customer |
| Medium | CRM integration (Freshdesk / Zoho) — auto-create ticket from Sheet row |
| Medium | Regional language support (Tamil, Telugu, Marathi, Bengali) |
| Medium | Weekly summary email to manager (total requests, categories, areas) |
| Low | Real-time call monitoring dashboard |
| Low | Repeat caller detection (same phone number within 7 days) |
| Low | Estimated technician availability based on area + service type |

---

## 11. Configuration

All agent behavior is controlled via `app_config.json` — no code changes needed for:
- System prompt updates (greeting scripts, language rules, stage instructions)
- Model selection (`parameters.google.model`)
- Tool schema modifications
- Temperature / generation parameters

Active provider is set via `active_provider` in `app_config.json` (currently `"google"` for Gemini Live hybrid pipeline).

LangGraph category taxonomy (services, subcategories, brand requirements) is defined in `core/service_graph.py`.
