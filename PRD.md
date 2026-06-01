# PRD: Mydoot Customer Care — AI Voice Agent

**Version:** 3.1
**Owner:** Alok Ranjan
**Phone Number:** +917971542939
**Last Updated:** June 2026

---

## 1. Problem Statement

Home appliance customers have complaints — their TV won't turn on, the AC is not cooling, the washing machine leaks. When they call for help:

- Lines are often busy or unmanned outside business hours
- Agents take details inconsistently (some miss the brand, some skip warranty status)
- Data rarely makes it into a structured format for follow-up
- Customers wait on hold, get frustrated, hang up

**Result:** Complaints are lost, service teams have incomplete data, and customers feel unheard.

---

## 2. Solution

An always-available AI voice agent that:
1. Answers every call instantly, 24/7
2. Greets in Hinglish and auto-detects language from the customer's first response
3. Collects all 7 required fields in a single conversation
4. Asks short clarifying questions when unclear — never goes silent mid-call
5. Saves a complete structured record to Google Sheets
6. Emails the full transcript to the admin after every call

No hold time. No missed fields. No data entry lag.

---

## 3. Users

| User | Role |
|------|------|
| End Customer | Calls to register a complaint about any home or office appliance |
| Support Manager | Reviews Google Sheet, assigns service tickets |
| Admin (Alok Ranjan) | Receives transcript emails, monitors system |

---

## 4. Functional Requirements

### FR-1: Inbound Call Handling
- Agent answers all calls on +917971542939 within 2 rings
- No call should go unanswered due to concurrency limits (up to 10 simultaneous)
- Call must work 24/7/365

### FR-2: Hinglish-First Greeting with Auto Language Detection
- On call connect, agent speaks a Hinglish greeting (one of 3 random options) and immediately asks about the problem
- No language preference question is asked
- Language auto-detection (silent, one-time):
  - If the customer's first complete response is exclusively in English with no Hindi words → entire call in English
  - In all other cases (Hindi, Hinglish, mixed, unclear, garbled, noisy, silent) → entire call in Hinglish
- Default is Hinglish — no detection required for the most common case
- Language is fixed for the rest of the call once detected

### FR-3: 7-Field Data Collection
The agent must collect all 7 fields before saving:

| # | Field | Validation |
|---|-------|-----------|
| 1 | Complaint | Free text description of the problem |
| 2 | Brand | Any brand (Samsung, Apple, LG, HP, Bajaj, etc.) |
| 3 | Item | Any appliance or device — TV, laptop, AC, mixer, MacBook, tube light, etc. |
| 4 | Product Used Since | Year or relative period (e.g., "2022", "3 saal pehle") |
| 5 | Usage Duration | Duration (e.g., "3 saal", "6 mahine") |
| 6 | Warranty Status | Enum: "Yes - Under Warranty" / "No - Out of Warranty" / "Customer Does Not Know" |
| 7 | Customer Name | Free text, as spoken |

Fields 4 and 5 are collected with a single question ("How long have you been using it?"); the agent derives both from the customer's answer.

### FR-4: Conversational Flow
- First question after greeting: asks about the appliance and the problem simultaneously
- Collects brand and item next (skipped if already mentioned)
- Collects usage duration (fills both product_used_since and usage_duration from one answer)
- Collects warranty status
- Collects customer name LAST
- One question at a time
- Never asks for information the customer has already provided anywhere in the conversation
- Accepts any device type — no restricted list
- Smart brand detection: "MacBook" → brand=Apple, item=MacBook Laptop
- "tube light" or "bulb" → item=Light/Tube Light (valid appliance type)

### FR-5: Clarifying Questions — Never Go Silent
- If the customer's response is unclear, garbled, or not understood: agent asks a short (one-sentence) clarifying question
- If the device type is ambiguous: asks to clarify (e.g., "tube light or LED bulb?")
- If audio was noisy/inaudible: asks customer to repeat
- Agent never goes blank or silent mid-conversation waiting for input

### FR-6: Data Persistence
- On completing all 7 fields: call `save_customer_feedback` tool immediately
- Do NOT say "complaint registered" before the tool call succeeds
- Write one row per call to Google Sheets (Sheet1, appended, never overwrite)
- Sheet columns: Customer Name | Brand | Item | Product Used Since | Usage Duration | Warranty Status | Complaint | Timestamp | Caller ID
- Auto-close the call 8 seconds after successful save

### FR-7: Post-Save Confirmation
- After save succeeds, speak the confirmation message exactly once in the customer's detected language
- Hinglish: "[name] ji, aapki complaint register ho gayi hai. Hamari team 24 ghante ke andar aapse sampark karegi. My Doot ko call karne ke liye shukriya!"
- English: "[name], your complaint has been registered. Our team will get in touch within 24 hours. Thank you for calling My Doot!"
- After the confirmation, go completely silent — do not repeat, do not add anything

### FR-8: Post-Call Transcript Email
- After every call (completed or dropped), send email to admin
- Email contains: Caller ID, timestamp, full Agent+Customer transcript
- Send even if transcript is empty (confirms call happened)

### FR-9: Call Recording for QA
- When `RECORD_CALLS=1` env var is set, save each call's inbound PSTN audio as an 8kHz PCM16 WAV file in `./recordings/`
- File naming: `{caller_id}_{YYYYMMDD_HHMMSS}.wav`
- Used with `test_asr_compare.py` for offline ASR benchmarking

### FR-10: Voice Quality
- Soft, warm, clear female voice (Aoede via Gemini Live)
- Slow, deliberate pace with natural pauses
- Empathetic tone throughout
- Female verb forms for self-reference in Hindi: "kar sakti hoon", "karungi", "bataungi"
- Gender-neutral forms when addressing the customer: "kar rahe hain" (not "kar rahi hain")

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
| Duplicate save protection | save_executed flag prevents re-execution per session |
| Inactivity timeout | 20 seconds after agent's last turn with no customer speech |

---

## 6. Data Model

### Google Sheet: mydoot_Customer_Care

| Column | Field | Type | Example |
|--------|-------|------|---------|
| A | Customer Name | String | Kumud Ranjan |
| B | Brand | String | HP |
| C | Item | String | laptop |
| D | Product Used Since | String | 3 saal pehle |
| E | Usage Duration | String | 3 saal |
| F | Warranty Status | Enum | No - Out of Warranty |
| G | Complaint | String | laptop chal nahi raha hai |
| H | Timestamp | DateTime | 2026-05-31 00:15:22 |
| I | Caller ID | String | 917042915552 |

Warranty Status enum values:
- `Yes - Under Warranty`
- `No - Out of Warranty`
- `Customer Does Not Know`

Sheet ID: `1uW39kklQKc4rhf5REATgKqgwbvSNAhlDVKXyAzOMKCk`

---

## 7. Technical Constraints

- **Language model**: Google Gemini 2.5 Flash Native Audio (BidiGenerateContent API, gemini-2.5-flash-native-audio-latest)
- **Telephony**: Vobiz SIP (+917971542939)
- **Audio codec**: mu-law 8kHz (Vobiz ↔ server), PCM 16kHz (server → Gemini), PCM 24kHz (Gemini → server)
- **Infrastructure**: Google Cloud Run (us-central1, project testcnx-169610)
- **Data storage**: Google Sheets only (no database)
- **Notification**: Gmail SMTP SSL port 465 (App Password auth)
- **No VAD config**: removed — caused 1008 policy violations on native audio model
- **No speechConfig**: removed — caused deferred 1008 errors on native audio model
- **PSTN codec limitation**: 8kHz mu-law makes phonetically similar words (e.g., "Hindi"/"Hello") indistinguishable to ASR — handled by defaulting to Hinglish without requiring language confirmation

---

## 8. Success Metrics

| Metric | Target |
|--------|--------|
| Complaint registration completion rate | > 85% of calls where customer speaks |
| Data completeness | 100% of saved rows have all 7 fields |
| Average handle time | 2–4 minutes |
| Call drop rate (before completion) | < 15% |
| Sheet write latency | < 5 seconds after all fields collected |
| Transcript email delivery | 100% of completed calls |

---

## 9. Out of Scope (v3.1)

- CRM or ticketing system integration (Freshdesk, Zoho, etc.)
- SMS/WhatsApp confirmation to customer after registration
- Outbound call-back scheduling
- Sentiment analysis or complaint severity scoring
- Regional languages beyond English and Hinglish
- Real-time dashboard for complaint volume/trends
- IVR menu (press 1 for AC, press 2 for TV)
- Separate STT pipeline (Sarvam/Deepgram) replacing Gemini native audio

---

## 10. Future Roadmap

| Priority | Feature |
|----------|---------|
| High | WhatsApp confirmation message to customer after registration |
| High | Complaint category auto-classification (hardware failure, installation, noise, etc.) |
| High | Switch to Sarvam Saaras V3 as separate STT for Hindi accuracy (~8% WER vs ~22%) |
| Medium | CRM integration (Freshdesk / Zoho) — auto-create ticket from Sheet row |
| Medium | Regional language support (Tamil, Telugu, Marathi, Bengali) |
| Medium | Weekly summary email to manager (total complaints, brands, devices) |
| Low | Real-time call monitoring dashboard |
| Low | Repeat caller detection (same phone number within 7 days) |
| Low | Estimated resolution time based on brand + complaint type |

---

## 11. Configuration

All agent behavior is controlled via `app_config.json` — no code changes needed for:
- System prompt updates (including greeting scripts, language rules, field order)
- Model selection (`parameters.google.model`)
- Tool schema modifications
- Temperature / generation parameters

Active provider is set via `active_provider` in `app_config.json` (currently `"google"` for Gemini Live).

Audio pipeline tuning is controlled via environment variables (NOISE_GATE_RMS, SPEECH_TAIL_SECS, SILENCE_SEND_SECS, etc.) — no code changes needed for threshold adjustments.
