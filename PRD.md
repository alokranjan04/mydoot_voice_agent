# PRD: Mydoot Customer Care — AI Voice Agent

**Version:** 4.0
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
5. Saves a complete 10-field structured record to Google Sheets
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
category → subcategory → problem → brand* → address → preferred_time → customer_name → done
*brand only asked for Appliance Repair and Vehicle Service categories
```

Fields collected:

| # | Field | Required | Notes |
|---|-------|----------|-------|
| 1 | category | Yes | Detected from description: Appliance Repair / Plumbing / Electrical / Carpentry / Cleaning / Vehicle Service / Other |
| 2 | subcategory | Yes | Specific type within category (e.g. Refrigerator, Pipe Leak, Wiring) |
| 3 | problem | Yes | Detailed description of the issue |
| 4 | brand | Conditional | Required for Appliance Repair and Vehicle Service; empty for others |
| 5 | model | No | Optional — captured if mentioned |
| 6 | address | Yes | Society name + area/locality for technician |
| 7 | preferred_time | Yes | When customer wants the technician to visit |
| 8 | customer_name | Yes | Collected LAST |

### FR-4: Conversational Rules
- One question at a time — always the next missing field per stage
- Never ask for information the customer has already provided anywhere in the conversation
- Accept category/subcategory hints from natural description: "mere fridge mein paani aa raha hai" → category=Appliance Repair, subcategory=Refrigerator
- Never go silent — if unclear, ask one short clarifying question
- If customer gives garbled or inaudible response, ask once to repeat

### FR-5: Data Persistence
- On completing all required fields, call `save_service_request` tool immediately
- Do NOT say "request registered" before the tool call succeeds
- Write one row per call to Google Sheets (Sheet1, appended, never overwrite)
- Sheet columns (A–J): Customer Name | Category | Subcategory | Problem | Brand | Model | Address | Preferred Time | Timestamp | Caller ID
- `save_executed` flag prevents duplicate Sheet rows per session

### FR-6: Post-Save Confirmation
- Before calling the tool: say exactly "Ek second, register ho raha hai." (Hinglish) or "One moment, registering now." (English)
- After save succeeds, speak the confirmation message exactly once — first word must be the customer's name:
  - Hinglish: "[name] ji, aapki request register ho gayi hai. Hamari team 24 ghante ke andar aapse sampark karegi. My Doot ko call karne ke liye shukriya!"
  - English: "[name], your request has been registered. Our team will get in touch within 24 hours. Thank you for calling My Doot!"
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

### FR-9: Noise Rejection (VAD)
- Customer audio is processed through a local Voice Activity Detector (VAD) before STT
- Only audio above RMS threshold (default: 100) is sent to Sarvam Saaras v3
- Utterances shorter than 0.3s are discarded (avoids noise blips)
- This prevents PSTN line noise from triggering hallucinated transcriptions

---

## 5. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Call answer latency | < 3 seconds from ring to greeting |
| Agent response latency (TTFT) | < 2 seconds per turn |
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
| D | Problem | String | Water leaking from bathroom pipe |
| E | Brand | String | Samsung *(Appliance/Vehicle only)* |
| F | Model | String | *(optional)* |
| G | Address | String | Sector 15, Noida |
| H | Preferred Time | String | kal subah 10 baje |
| I | Timestamp | DateTime | 2026-06-01 10:15:22 |
| J | Caller ID | String | 917042915552 |

Sheet ID: `1uW39kklQKc4rhf5REATgKqgwbvSNAhlDVKXyAzOMKCk`

---

## 7. Technical Constraints

- **STT**: Sarvam Saaras v3 REST API (`saaras:v3`, hi-IN, 8kHz WAV) — replaces Gemini native audio input to eliminate PSTN noise hallucinations
- **Language model**: Google Gemini 2.5 Flash Native Audio (BidiGenerateContent, text-in / audio-out)
- **Conversation orchestration**: LangGraph `StateGraph` via `core/service_graph.py`
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
- Sentiment analysis or complaint severity scoring
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
