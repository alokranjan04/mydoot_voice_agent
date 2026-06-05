# -*- coding: utf-8 -*-
"""
core/latency_tracker.py — Per-turn latency instrumentation for the Gemini pipeline.

Each customer turn produces one TurnSpan that records timestamps at every
stage of the pipeline.  Computed metrics:
    vad_ms              — VAD onset → speech_end (utterance duration)
    stt_ms              — speech_end → STT response received
    langgraph_ms        — STT done → stage-context built (ServiceGraph.get_context())
    llm_first_token_ms  — clientContent sent → first audio packet from Gemini
    llm_total_ms        — clientContent sent → Gemini turnComplete
    tts_first_audio_ms  — same as llm_first_token_ms (Gemini Live is LLM+TTS in one call)
    tts_total_ms        — same as llm_total_ms
    end_to_end_turn_ms  — VAD onset → Gemini turnComplete
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_PC = time.perf_counter   # monotonic high-resolution timer


@dataclass
class TurnSpan:
    """All timestamps (perf_counter seconds) for one customer turn."""
    turn_id:            int   = 0

    # ── input side (set in _stt_and_send) ──────────────────────────────────────
    vad_start:          Optional[float] = None  # VAD first above-threshold packet
    vad_end:            Optional[float] = None  # speech_end → utterance flushed to STT

    stt_start:          Optional[float] = None  # immediately before Sarvam POST
    stt_end:            Optional[float] = None  # immediately after STT response

    langgraph_start:    Optional[float] = None  # immediately before get_context()
    langgraph_end:      Optional[float] = None  # immediately after get_context()

    gemini_send:        Optional[float] = None  # immediately before g_ws.send(clientContent)

    # ── output side (set in g_receiver) ────────────────────────────────────────
    gemini_first_audio: Optional[float] = None  # first inlineData audio packet
    gemini_turn_end:    Optional[float] = None  # serverContent.turnComplete

    # ── meta ───────────────────────────────────────────────────────────────────
    customer_text:      str   = ""
    discarded:          bool  = False

    # ── computed metrics (ms) — filled by complete_turn() ──────────────────────
    vad_ms:             Optional[int] = None
    stt_ms:             Optional[int] = None
    langgraph_ms:       Optional[int] = None
    llm_first_token_ms: Optional[int] = None
    llm_total_ms:       Optional[int] = None
    tts_first_audio_ms: Optional[int] = None
    tts_total_ms:       Optional[int] = None
    end_to_end_turn_ms: Optional[int] = None


class LatencyTracker:
    """Tracks per-turn latency for one call session."""

    # Warning thresholds (ms) — emit WARNING log if any metric exceeds these
    _WARN: dict = {
        "stt_ms":              800,
        "llm_first_token_ms": 2500,
        "end_to_end_turn_ms": 4000,
    }

    def __init__(self, caller_id: str = ""):
        self._caller_id = caller_id
        self._turn_seq  = 0
        self._active:   Optional[TurnSpan] = None
        self.completed: list[TurnSpan]     = []

    # ── Public API ──────────────────────────────────────────────────────────────

    def new_turn(self) -> TurnSpan:
        """Start a new turn span. Call at VAD onset."""
        self._turn_seq += 1
        span = TurnSpan(turn_id=self._turn_seq)
        self._active = span
        return span

    def mark(self, field: str, ts: Optional[float] = None) -> None:
        """Set a timestamp field on the active span (uses perf_counter if ts omitted)."""
        if self._active is None:
            return
        setattr(self._active, field, ts if ts is not None else _PC())

    def set_text(self, text: str) -> None:
        """Store the STT transcript in the active span."""
        if self._active:
            self._active.customer_text = text

    def discard_turn(self) -> None:
        """Drop the active span (utterance was noise, too short, or concurrent-drop)."""
        if self._active:
            self._active.discarded = True
        self._active = None

    def complete_turn(self) -> Optional[TurnSpan]:
        """
        Compute derived metrics, log a structured summary line, emit WARNINGs
        for threshold breaches, and move the span to self.completed.
        Returns the completed span, or None if nothing was active.
        """
        span = self._active
        if span is None:
            return None
        self._active = None

        def _ms(start, end) -> Optional[int]:
            if start is not None and end is not None:
                return max(0, round((end - start) * 1000))
            return None

        span.vad_ms             = _ms(span.vad_start, span.vad_end)
        span.stt_ms             = _ms(span.stt_start,  span.stt_end)
        span.langgraph_ms       = _ms(span.langgraph_start, span.langgraph_end)
        span.llm_first_token_ms = _ms(span.gemini_send, span.gemini_first_audio)
        span.llm_total_ms       = _ms(span.gemini_send, span.gemini_turn_end)
        # Gemini Live combines LLM + TTS in one model call
        span.tts_first_audio_ms = span.llm_first_token_ms
        span.tts_total_ms       = span.llm_total_ms
        span.end_to_end_turn_ms = _ms(span.vad_start,  span.gemini_turn_end)

        _parts = []
        for label, val in [
            ("vad",       span.vad_ms),
            ("stt",       span.stt_ms),
            ("langgraph", span.langgraph_ms),
            ("llm_first", span.llm_first_token_ms),
            ("llm_total", span.llm_total_ms),
            ("e2e",       span.end_to_end_turn_ms),
        ]:
            _parts.append(f"{label}={val}ms" if val is not None else f"{label}=?")

        logger.info("[LATENCY] caller=%s turn=%d %s",
                    self._caller_id, span.turn_id, "  ".join(_parts))

        for metric, threshold in self._WARN.items():
            val = getattr(span, metric, None)
            if val is not None and val > threshold:
                logger.warning(
                    "[LATENCY WARN] caller=%s turn=%d %s=%dms > %dms threshold",
                    self._caller_id, span.turn_id, metric, val, threshold,
                )

        self.completed.append(span)
        return span
