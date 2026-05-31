# PRD: Mydoot Customer Care — AI Voice Agent

**Version:** 2.0
**Owner:** Alok Ranjan
**Phone Number:** +917971542939
**Last Updated:** May 2026

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
2. Speaks natural, empathetic Hinglish
3. Collects all 7 required fields in a single conversation
4. Saves a complete structured record to Google Sheets
5. Emails the full transcript to the admin after every call

No hold time. No missed fields. No data entry lag.

---

## 3. Users

| User | Role |
|------|------|
| End Customer | Calls to register a complaint about any home appliance |
| Support Manager | Reviews Google Sheet, assigns service tickets |
| Admin (Alok Ranjan) | Receives transcript emails, monitors system |

---

## 4. Functional Requirements

### FR-1: Inbound Call Handling
- Agent answers all calls on +917971542939 within 2 rings
- No call should go unanswered due to concurrency limits (up to 10 simultaneous)
- Call must work 24/7/365

### FR-2: Greeting
- Agent speaks a warm, empathetic Hinglish greeting on call connect
- 3 greeting scripts rotate randomly across calls (avoids robotic repetition)
- Greeting explains what Mydoot does and invites the customer to share their problem

### FR-3: 7-Field Data Collection
Agent must collect all 7 fields before saving:

| # | Field | Validation |
|---|-------|-----------|
| 1 | Customer Name | Free text, as spoken |
| 2 | Brand | Any brand (Samsung, Apple, LG, HP, Bajaj, etc.) |
| 3 | Item | Any appliance or device — TV, laptop, AC, mixer, MacBook, etc. |
| 4 | Product Used Since | Year or relative period (e.g., "2022", "3 saal pehle") |
| 5 | Usage Duration | Duration (e.g., "3 saal", "6 mahine") |
| 6 | Warranty Status | Enum: "Yes - Under Warranty" / "No - Out of Warranty" / "Customer Does Not Know" |
| 7 | Complaint | Free text description of the problem |

Fields 4 and 5 are collected with a single question; the agent derives both from the customer's answer.

### FR-4: Conversational Flow
- Collects complaint first (customer's primary concern)
- Collects name second (personalization)
- Remaining fields in natural order
- One question at a time
- Never asks a question the customer already answered
- Accepts any device type — no restricted list
- Smart brand detection: "MacBook" → brand=Apple, item=MacBook Laptop

### FR-5: Data Persistence
- On completing all 7 fields: call `save_customer_feedback` tool immediately
- Do NOT say "complaint registered" before the tool call succeeds
- Write one row per call to Google Sheets (Sheet1, appended, never overwrite)
- Sheet columns: Customer Name | Brand | Item | Product Used Since | Usage Duration | Warranty Status | Complaint | Timestamp | Caller ID

### FR-6: Post-Call Transcript Email
- After every call (completed or dropped), send email to admin
- Email contains: Caller ID, timestamp, full Agent+Customer transcript
- Send even if transcript is empty (confirms call happened)

### FR-7: Voice Quality
- Soft, warm, clear female voice (Aoede via Gemini Live)
- Slow, deliberate pace with natural pauses
- Empathetic tone throughout
- Language: Hinglish (Hindi + English mix)

---

## 5. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Call answer latency | < 3 seconds from ring to greeting |
| Agent response latency (TTFT) | < 2 seconds per turn |
| Google Sheets write success | > 99% |
| Concurrent calls supported | 10 |
| Uptime | 99.5% (Cloud Run managed) |
| Call max duration | 60 minutes |
| Audio quality | Clear mu-law 8kHz, no distortion |

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

---

## 7. Technical Constraints

- **Language model**: Google Gemini 2.5 Flash Native Audio (BidiGenerateContent API)
- **Telephony**: Vobiz SIP (+917971542939)
- **Audio codec**: mu-law 8kHz (Vobiz ↔ server), PCM 16kHz (server → Gemini), PCM 24kHz (Gemini → server)
- **Infrastructure**: Google Cloud Run (us-central1, project testcnx-169610)
- **Data storage**: Google Sheets only (no database)
- **Notification**: Gmail SMTP (App Password auth)

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

## 9. Out of Scope (v2.0)

- CRM or ticketing system integration (Freshdesk, Zoho, etc.)
- SMS/WhatsApp confirmation to customer after registration
- Outbound call-back scheduling
- Sentiment analysis or complaint severity scoring
- Multi-language support beyond Hinglish
- Real-time dashboard for complaint volume/trends
- IVR menu (press 1 for AC, press 2 for TV)

---

## 10. Future Roadmap

| Priority | Feature |
|----------|---------|
| High | WhatsApp confirmation message to customer after registration |
| High | Complaint category auto-classification (hardware failure, installation, noise, etc.) |
| Medium | CRM integration (Freshdesk / Zoho) — auto-create ticket from Sheet row |
| Medium | Regional language support (Tamil, Telugu, Marathi, Bengali) |
| Medium | Weekly summary email to manager (total complaints, brands, devices) |
| Low | Real-time call monitoring dashboard |
| Low | Repeat caller detection (same phone number within 7 days) |
| Low | Estimated resolution time based on brand + complaint type |

---

## 11. Configuration

All agent behavior is controlled via `app_config.json` — no code changes needed for:
- System prompt updates
- Greeting script changes
- Model or voice selection
- Tool schema modifications
- Temperature / generation parameters

To change the active voice pipeline: update `active_provider` in `app_config.json` (`"google"` for Gemini Live).
