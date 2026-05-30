# PRD: Mydoot Customer Care — AI Voice Agent

**Version:** 1.0
**Owner:** Alok Ranjan
**Phone Number:** +917971542939

---

## 1. Vision

Build an always-available AI voice agent for Mydoot that answers every inbound customer call, collects a complete and structured complaint or feedback record in natural Hinglish, and writes the data to Google Sheets in real time — eliminating missed complaints and incomplete tickets.

---

## 2. Problem Statement

| Problem | Impact |
|---|---|
| Customer care lines are unavailable after business hours | Complaints go unrecorded; customers feel ignored |
| Human agents skip required fields under call pressure | Incomplete records lead to unresolved issues |
| No structured log of complaints by product, warranty status, or usage | Support teams cannot identify repeat issues or warranty abuse |
| Long hold times erode customer trust | Customers abandon calls and escalate on social media |

---

## 3. Target Users

| User | Description |
|---|---|
| **End Customer** | Mydoot product buyer calling to report an issue or give feedback |
| **Support Manager** | Reviews Google Sheet logs, assigns resolution tasks |
| **Product Team** | Analyses complaint patterns by product and usage duration |
| **Operations** | Monitors agent uptime, call volume, and data completeness |

---

## 4. Core Features

### 4.1 Structured 5-Step Feedback Collection

The agent must collect exactly these five data points in every call, in order:

1. **Customer Name** — Full name as spoken
2. **Product Used** — Which Mydoot product the customer is using
3. **Usage Duration** — How long the customer has been using it (months / years)
4. **Warranty Status** — One of: `Yes - Under Warranty`, `No - Out of Warranty`, `Customer Does Not Know`
5. **Complaint / Feedback** — Verbatim description of the issue or suggestion

The agent must not call the save function until all five fields are confirmed. If a field is ambiguous, the agent must clarify before proceeding.

### 4.2 Google Sheets Logging

Every completed call must append one row to a pre-configured Google Sheet with columns:

```
Customer Name | Product Used | Usage Duration | Warranty Status | Complaint | Timestamp | Caller ID
```

Rows must appear within 3 seconds of the customer confirming their complaint.

### 4.3 Natural Hinglish Conversation

- The agent speaks fluent Hinglish (mixed Hindi + English) — the natural register of Indian customer service.
- One question at a time. No bullet points or lists read aloud.
- Empathetic tone: acknowledges the complaint before moving to the next question.
- Handles interruptions gracefully — if the customer barge-in, the agent stops and listens.

### 4.4 Dual Pipeline Support

| Mode | Use Case |
|---|---|
| **Sarvam Hybrid** (default) | Best-quality Hinglish understanding; ElevenLabs or Sarvam Bulbul TTS |
| **Gemini Live** (alternative) | Ultra-low latency; better for fast-paced or impatient callers |

Switching between pipelines requires only a config change — no code deployment.

### 4.5 24/7 Availability

The agent must handle concurrent inbound calls without degradation. There is no business-hours restriction.

---

## 5. Technical Requirements

| Requirement | Target |
|---|---|
| First response latency (TTFT) | < 1.5 seconds after customer stops speaking |
| Language accuracy | > 90% correct field extraction on first attempt |
| Google Sheets write success rate | > 99% (retry on transient failure) |
| Warranty field accuracy | Must correctly map ambiguous answers to one of the 3 enum values |
| Concurrent calls supported | Minimum 10 simultaneous (aiohttp async architecture) |
| Call recording | 100% of calls recorded as stereo WAV |
| Uptime | 99.5% monthly |

---

## 6. Data Model

### Google Sheets Row

| Field | Type | Populated By |
|---|---|---|
| Customer Name | String | LLM extraction from speech |
| Product Used | String | LLM extraction from speech |
| Usage Duration | String (free text) | LLM extraction (e.g. "6 months") |
| Warranty Status | Enum (3 values) | LLM classification |
| Complaint | String | LLM extraction from speech |
| Timestamp | DateTime (YYYY-MM-DD HH:MM:SS) | System-generated at save time |
| Caller ID | String | Vobiz `From` header (phone number) |

### Warranty Status Enum

| Value | When Used |
|---|---|
| `Yes - Under Warranty` | Customer confirms the product is under warranty |
| `No - Out of Warranty` | Customer confirms warranty has expired or product is old |
| `Customer Does Not Know` | Customer is unsure or cannot answer |

---

## 7. Out of Scope (v1.0)

- Ticket creation in CRM or helpdesk systems (post-MVP)
- Automated escalation or callback scheduling
- SMS / WhatsApp follow-up after the call
- Multi-language support beyond Hindi / Hinglish
- Sentiment analysis on complaints
- Payment or refund handling via voice

---

## 8. Success Metrics

| Metric | Definition | Target |
|---|---|---|
| **Completion Rate** | % of calls where all 5 fields are collected and saved | > 85% |
| **Data Completeness** | % of saved rows with no empty required fields | 100% |
| **Average Handle Time** | Average call duration for a completed feedback session | 2–4 minutes |
| **CSAT Proxy** | Customer does not hang up mid-collection | < 10% drop rate |
| **Sheet Write Success** | Rows saved vs. calls completed | > 99% |

---

## 9. Assumptions & Constraints

- Customers are calling from Indian mobile numbers via Vobiz SIP trunk (+917971542939).
- Google Sheets credentials (service account) are available and the target sheet is shared with the service account as Editor.
- The server is publicly reachable so Vobiz can POST to `/answer` and open WebSocket streams.
- Python 3.10–3.12 is used (required for `audioop` Mu-law encoding).
- `GOOGLE_SPREADSHEET_ID` and at least one valid API key (`SARVAM_API_KEY` + `DEEPGRAM_API_KEY`) are configured.

---

## 10. Future Roadmap

| Priority | Feature |
|---|---|
| High | WhatsApp confirmation message to customer after call |
| High | CRM / helpdesk ticket auto-creation (Zoho, Freshdesk) |
| Medium | Complaint category classification (defect, delivery, warranty, general) |
| Medium | Weekly complaint summary email to support manager |
| Medium | Escalation detection — if customer mentions legal/consumer forum, flag the row |
| Low | Regional language support (Tamil, Telugu, Bengali) |
| Low | Voice sentiment score appended to the Sheet row |
| Low | Dashboard showing complaint trends by product over time |
