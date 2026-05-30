# -*- coding: utf-8 -*-
"""
Pure cost calculation for voice agent calls — no I/O, no external deps.
All pricing as of April 2026. Update constants when vendor prices change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# ─── Pricing constants (USD) ──────────────────────────────────────────────────

# Deepgram Nova-2: $0.0077 / minute (pay-as-you-go)
DEEPGRAM_PER_MIN: float = 0.0077

# Sarvam 30B chat: ~$0.001 per 1 K tokens (input + output blended estimate)
# Update when Sarvam publishes exact pricing.
SARVAM_LLM_PER_1K_TOKENS: float = 0.001

# Sarvam TTS Bulbul v2: ~$0.000015 per character
SARVAM_TTS_PER_CHAR: float = 0.000015

# Google Gemini Live API: blended bidirectional audio estimate $0.0023 / minute
# (Gemini 2.0 Flash Live: input $0.00025/1K tokens, output $0.001/1K tokens;
#  audio ≈ 1 token / 25 ms → ~40 tokens/sec → ~$0.0023/min blended)
GEMINI_LIVE_PER_MIN: float = 0.0023


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class CallCost:
    stt_usd:   float = 0.0   # Deepgram STT (Sarvam pipeline only)
    llm_usd:   float = 0.0   # Sarvam LLM or Gemini audio (provider-dependent)
    tts_usd:   float = 0.0   # Sarvam TTS (Sarvam pipeline only)
    total_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "stt_usd":   round(self.stt_usd,   6),
            "llm_usd":   round(self.llm_usd,   6),
            "tts_usd":   round(self.tts_usd,   6),
            "total_usd": round(self.total_usd, 6),
        }


# ─── Main calculator ──────────────────────────────────────────────────────────

def calculate_cost(
    provider:         str,
    duration_sec:     float,
    token_count:      Optional[int] = None,
    tts_chars:        int           = 0,
    transcript_chars: int           = 0,
) -> CallCost:
    """
    Calculate the estimated cost for one completed call.

    Parameters
    ----------
    provider         : "sarvam" or "google"
    duration_sec     : total call duration in seconds
    token_count      : LLM token count; estimated from transcript_chars/4 if None
    tts_chars        : total characters sent to Sarvam TTS during the call
    transcript_chars : fallback for token estimation when token_count is None
    """
    cost  = CallCost()
    mins  = duration_sec / 60.0
    tokens = token_count if token_count is not None else max(transcript_chars // 4, 1)

    if provider == "sarvam":
        cost.stt_usd   = mins * DEEPGRAM_PER_MIN
        cost.llm_usd   = (tokens / 1000.0) * SARVAM_LLM_PER_1K_TOKENS
        cost.tts_usd   = tts_chars * SARVAM_TTS_PER_CHAR
        cost.total_usd = cost.stt_usd + cost.llm_usd + cost.tts_usd

    elif provider == "google":
        # Gemini Live covers STT + LLM + TTS in one blended audio rate
        cost.llm_usd   = mins * GEMINI_LIVE_PER_MIN
        cost.total_usd = cost.llm_usd

    return cost


# ─── Aggregate helpers (used by /metrics/data endpoint) ──────────────────────

def cost_per_booking(calls: List[dict]) -> float:
    """Average cost per successful booking across a list of call records."""
    booking_costs = [
        c.get("cost_usd") or 0.0
        for c in calls
        if c.get("booking_success")
    ]
    if not booking_costs:
        return 0.0
    return sum(booking_costs) / len(booking_costs)


def aggregate_costs(calls: List[dict]) -> dict:
    """
    Return per-provider average cost breakdown for the dashboard.
    Expects records from call_log.jsonl.
    """
    totals:  dict = {}
    counts:  dict = {}
    for rec in calls:
        p = rec.get("provider", "unknown")
        c = rec.get("cost_usd") or 0.0
        totals[p] = totals.get(p, 0.0) + c
        counts[p] = counts.get(p, 0)   + 1
    return {
        p: round(totals[p] / counts[p], 6)
        for p in totals
    }
