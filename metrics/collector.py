# -*- coding: utf-8 -*-
"""
Per-call metrics accumulator and thread-safe store.
No heavy dependencies — only stdlib + psutil.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from time import perf_counter
from typing import Any, Dict, List, Optional

import psutil

# ─── Path setup ───────────────────────────────────────────────────────────────
_METRICS_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH    = os.path.join(_METRICS_DIR, "call_log.jsonl")

BOOKING_FIELDS = [
    "patient_name", "patient_age", "parent_name",
    "contact_number", "preferred_day", "preferred_time", "reason",
]


# ─── Sub-record dataclasses ───────────────────────────────────────────────────

@dataclass
class ToolCallRecord:
    """One tool invocation captured at runtime."""
    function_name:  str    # e.g. "book_appointment"
    args:           dict
    success:        bool   # True if result has no "error" key
    result_summary: str    # str(result)[:200]
    latency_ms:     float


@dataclass
class TurnLatency:
    """
    Latency breakdown for one user-turn.
    All values in milliseconds; None means the stage did not run this turn.
    """
    turn_index: int
    stt_ms:     Optional[float] = None   # Deepgram final arrival (Sarvam only)
    llm_ms:     Optional[float] = None   # _sarvam_chat() round-trip
    tts_ms:     Optional[float] = None   # _sarvam_tts() round-trip (Sarvam only)
    tool_ms:    Optional[float] = None   # asyncio.to_thread(fn) duration
    e2e_ms:     Optional[float] = None   # t_user_done → first_audio sent


@dataclass
class ResourceSnapshot:
    """Single psutil sample (taken every 1 second)."""
    ts:         float   # perf_counter() at sample time
    cpu_pct:    float   # process CPU %
    mem_rss_mb: float   # process RSS in MB
    net_sent_b: int     # cumulative bytes sent
    net_recv_b: int     # cumulative bytes received
    threads:    int     # process thread count


# ─── Main per-call dataclass ──────────────────────────────────────────────────

@dataclass
class CallMetrics:
    # ── Identity ──────────────────────────────────────────────────────────────
    stream_sid:      str
    provider:        str    # "sarvam" | "google"
    caller_id:       str
    call_start_wall: float = field(default_factory=time.time)
    call_start_perf: float = field(default_factory=perf_counter)

    # ── Latency ───────────────────────────────────────────────────────────────
    turn_latencies:            List[TurnLatency] = field(default_factory=list)
    google_e2e_latencies_ms:   List[float]       = field(default_factory=list)
    google_tool_latencies_ms:  List[float]       = field(default_factory=list)

    # ── Internal timing helpers (not serialised) ───────────────────────────
    _current_turn_index: int   = field(default=0,   repr=False)
    _t_user_done:        float = field(default=0.0, repr=False)
    _t_llm_start:        float = field(default=0.0, repr=False)
    _t_tts_start:        float = field(default=0.0, repr=False)
    _t_turn_complete:    float = field(default=0.0, repr=False)

    # ── Accuracy ──────────────────────────────────────────────────────────────
    tool_calls:             List[ToolCallRecord] = field(default_factory=list)
    hallucination_count:    int                  = 0
    interruption_count:     int                  = 0
    english_fallback_count: int                  = 0
    slot_fields_collected:  List[str]            = field(default_factory=list)

    # ── Call quality ──────────────────────────────────────────────────────────
    turn_count:            int   = 0
    call_duration_s:       float = 0.0
    booking_success:       bool  = False
    cancel_success:        bool  = False
    check_run:             bool  = False
    first_call_resolution: bool  = False

    # ── Cost (set in finalise) ────────────────────────────────────────────────
    cost_usd: Optional[float] = None

    # ── System resources (raw samples; summarised in finalise) ───────────────
    resource_samples: List[ResourceSnapshot] = field(default_factory=list)
    avg_cpu_pct:      Optional[float]        = None
    peak_mem_rss_mb:  Optional[float]        = None
    net_sent_kb:      Optional[float]        = None
    net_recv_kb:      Optional[float]        = None

    # ── Response timing ───────────────────────────────────────────────────────
    first_response_ms: Optional[float] = None    # call start → first audio out
    deepgram_confidences: List[float] = field(default_factory=list)

    # ── Recording ─────────────────────────────────────────────────────────────
    recording_path: Optional[str] = None   # filename under recordings/ (set at teardown)

    # ── Transcript (last 30 turns, 300 chars each) ────────────────────────────
    transcript_turns: List[Dict[str, str]] = field(default_factory=list)

    # ─── Public API ───────────────────────────────────────────────────────────

    def record_interruption(self):
        self.interruption_count += 1

    def record_hallucination(self):
        self.hallucination_count += 1

    def record_english_fallback(self, text: str):
        """Flag the turn if >10% of characters are plain ASCII letters."""
        if not text:
            return
        ascii_chars = sum(1 for c in text if ord(c) < 128 and c.isalpha())
        if ascii_chars / max(len(text), 1) > 0.1:
            self.english_fallback_count += 1

    def record_turn(self, role: str, content: str):
        self.turn_count += 1
        if len(self.transcript_turns) < 30:
            self.transcript_turns.append({"role": role, "content": content[:300]})

    def record_tool_call(
        self, fn_name: str, args: dict, result: Any, latency_ms: float
    ):
        success = (
            isinstance(result, dict)
            and "error" not in result
            and result.get("success", True)
        )
        self.tool_calls.append(ToolCallRecord(
            function_name=fn_name,
            args=args,
            success=success,
            result_summary=str(result)[:200],
            latency_ms=round(latency_ms, 2),
        ))
        if fn_name == "book_appointment" and success:
            self.booking_success       = True
            self.first_call_resolution = True
        elif fn_name == "cancel_appointment" and success:
            self.cancel_success = True
        elif fn_name == "check_available_slots":
            self.check_run = True

        # Track which booking fields were provided
        for fname in BOOKING_FIELDS:
            if fname in args and args[fname] and fname not in self.slot_fields_collected:
                self.slot_fields_collected.append(fname)

    @property
    def slot_fill_ratio(self) -> float:
        """Fraction of the 7 required booking fields captured this call."""
        return len(self.slot_fields_collected) / len(BOOKING_FIELDS)

    def finalise(self, cost_usd: float):
        """Call once at bridge teardown. Summarises resource samples."""
        self.call_duration_s = perf_counter() - self.call_start_perf
        self.cost_usd        = round(cost_usd, 6)

        if self.resource_samples:
            cpus = [s.cpu_pct    for s in self.resource_samples]
            mems = [s.mem_rss_mb for s in self.resource_samples]
            self.avg_cpu_pct     = round(sum(cpus) / len(cpus), 2)
            self.peak_mem_rss_mb = round(max(mems), 2)
            first = self.resource_samples[0]
            last  = self.resource_samples[-1]
            self.net_sent_kb = round((last.net_sent_b - first.net_sent_b) / 1024, 2)
            self.net_recv_kb = round((last.net_recv_b - first.net_recv_b) / 1024, 2)

    def to_jsonl_record(self) -> dict:
        """Serialisable dict for call_log.jsonl. Strips internal _ fields."""
        rec = asdict(self)
        for k in list(rec.keys()):
            if k.startswith("_"):
                del rec[k]
        rec.pop("resource_samples", None)          # only summaries are kept
        rec["slot_fill_ratio"] = round(self.slot_fill_ratio, 3)
        return rec


# ─── MetricsStore singleton ───────────────────────────────────────────────────

class MetricsStore:
    """
    Thread-safe store for active calls, keyed by stream_sid.
    Completed calls are appended to call_log.jsonl.
    """
    _instance: Optional["MetricsStore"] = None
    _class_lock: threading.Lock          = threading.Lock()

    def __new__(cls) -> "MetricsStore":
        with cls._class_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._calls: Dict[str, CallMetrics] = {}
                inst._rlock = threading.Lock()
                cls._instance = inst
        return cls._instance

    def start_call(self, stream_sid: str, provider: str, caller_id: str) -> CallMetrics:
        cm = CallMetrics(stream_sid=stream_sid, provider=provider, caller_id=caller_id)
        with self._rlock:
            self._calls[stream_sid] = cm
        return cm

    def get(self, stream_sid: str) -> Optional[CallMetrics]:
        with self._rlock:
            return self._calls.get(stream_sid)

    def end_call(self, stream_sid: str, cost_usd: float) -> Optional[CallMetrics]:
        with self._rlock:
            cm = self._calls.pop(stream_sid, None)
        if cm:
            cm.finalise(cost_usd)
            _append_jsonl(cm)
        return cm

    def recent_calls(self, n: int = 50) -> List[dict]:
        """Return the last n records from call_log.jsonl."""
        records: List[dict] = []
        try:
            with open(_LOG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except FileNotFoundError:
            pass
        return records[-n:]


def _append_jsonl(cm: CallMetrics):
    os.makedirs(_METRICS_DIR, exist_ok=True)
    record = cm.to_jsonl_record()
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# Module-level singleton
store = MetricsStore()


# ─── Background resource poller ───────────────────────────────────────────────

async def resource_poller(cm: CallMetrics, interval: float = 1.0):
    """
    Coroutine: sample psutil every `interval` seconds until cancelled.
    Launch with: task = asyncio.create_task(resource_poller(cm))
    Cancel at teardown: task.cancel()
    """
    proc = psutil.Process()
    # Prime the cpu_percent call (first call always returns 0.0)
    proc.cpu_percent(interval=None)
    try:
        while True:
            await asyncio.sleep(interval)
            cpu  = proc.cpu_percent(interval=None)
            mem  = proc.memory_info().rss / (1024 * 1024)
            net  = psutil.net_io_counters()
            snap = ResourceSnapshot(
                ts=perf_counter(),
                cpu_pct=cpu,
                mem_rss_mb=round(mem, 2),
                net_sent_b=net.bytes_sent,
                net_recv_b=net.bytes_recv,
                threads=proc.num_threads(),
            )
            cm.resource_samples.append(snap)
    except asyncio.CancelledError:
        pass
