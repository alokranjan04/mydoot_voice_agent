# -*- coding: utf-8 -*-
"""
Metrics routes.

GET /metrics       → HTML dashboard (served from metrics/dashboard_html.py)
GET /metrics/data  → Raw JSON payload consumed by the dashboard's JS
"""
import time
from aiohttp import web
from metrics.collector import store
from metrics.dashboard_html import METRICS_DASHBOARD_HTML


async def metrics_page(request: web.Request) -> web.Response:
    return web.Response(text=METRICS_DASHBOARD_HTML, content_type="text/html")


async def metrics_data(request: web.Request) -> web.Response:
    records = store.recent_calls(200)

    def _pct(data: list, p: int):
        vals = sorted(x for x in data if x is not None)
        if not vals:
            return None
        return round(vals[min(int(len(vals) * p / 100), len(vals) - 1)])

    def _avg(lst: list):
        clean = [x for x in lst if x is not None]
        return round(sum(clean) / len(clean), 2) if clean else None

    def _build(recs: list) -> dict:
        if not recs:
            return {"call_count": 0}
        tl_all = [t for r in recs for t in r.get("turn_latencies", [])]
        recent = []
        for r in recs[-10:]:
            e2es = [t.get("e2e_ms") for t in r.get("turn_latencies", []) if t.get("e2e_ms")]
            recent.append({
                "ts":                r.get("call_start_wall"),
                "caller":            r.get("caller_id", "?"),
                "duration_s":        round(r.get("call_duration_s", 0)),
                "turns":             r.get("turn_count", 0),
                "booking":           r.get("booking_success", False),
                "cost_usd":          round(r.get("cost_usd") or 0, 4),
                "first_response_ms": round(r["first_response_ms"]) if r.get("first_response_ms") else None,
                "avg_e2e_ms":        round(_avg(e2es)) if e2es else None,
                "interrupts":        r.get("interruption_count", 0),
                "hallucinations":    r.get("hallucination_count", 0),
                "recording":         r.get("recording_path"),
                "turn_latencies":    r.get("turn_latencies", []),
                "dg_confidences":    r.get("deepgram_confidences", []),
            })
        bookings = [r for r in recs if r.get("booking_success")]
        costs    = [r.get("cost_usd") or 0 for r in recs if r.get("cost_usd")]
        confs    = [c for r in recs for c in r.get("deepgram_confidences", [])]
        n        = len(recs)
        return {
            "call_count":            n,
            "booking_count":         len(bookings),
            "booking_rate_pct":      round(100 * len(bookings) / n) if n else 0,
            "avg_duration_s":        _avg([r.get("call_duration_s", 0) for r in recs]),
            "avg_turns":             _avg([r.get("turn_count", 0) for r in recs]),
            "avg_interrupts":        _avg([r.get("interruption_count", 0) for r in recs]),
            "avg_hallucins":         _avg([r.get("hallucination_count", 0) for r in recs]),
            "avg_cost_usd":          _avg(costs),
            "cost_per_booking":      _avg([r.get("cost_usd") or 0 for r in bookings]),
            "avg_first_response_ms": _avg([r.get("first_response_ms") for r in recs]),
            "avg_dg_confidence":     _avg(confs),
            "avg_cpu_pct":           _avg([r.get("avg_cpu_pct") for r in recs]),
            "peak_mem_mb":           _avg([r.get("peak_mem_rss_mb") for r in recs]),
            "latency": {
                k: {
                    "p50": _pct([t.get(f"{k}_ms") for t in tl_all], 50),
                    "p95": _pct([t.get(f"{k}_ms") for t in tl_all], 95),
                }
                for k in ["stt", "llm", "tts", "e2e", "tool"]
            },
            "recent_calls": recent,
        }

    sarvam_recs = [r for r in records if r.get("provider") == "sarvam"]
    google_recs = [r for r in records if r.get("provider") == "google"]

    return web.json_response({
        "sarvam":       _build(sarvam_recs),
        "google":       _build(google_recs),
        "last_updated": time.time(),
        "total_calls":  len(records),
    })
