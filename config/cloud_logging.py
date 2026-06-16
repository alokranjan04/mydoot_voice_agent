# -*- coding: utf-8 -*-
"""
Structured JSON logging for Google Cloud Run.

Cloud Run automatically sends stdout to Cloud Logging. When the output
is JSON with specific fields, Cloud Logging parses it into structured
log entries with severity, labels, and trace context.

Usage:
    from config.cloud_logging import cloud_log

    cloud_log("Call started", severity="INFO",
              caller_id="917042915552", event="call_start")
    cloud_log("STT latency high", severity="WARNING",
              caller_id="917042915552", stt_ms=1200)
    cloud_log("Gemini error", severity="ERROR",
              caller_id="917042915552", error="connection refused")
"""
import json
import os
import sys
import time
from datetime import datetime, timezone


# Cloud Run sets these automatically
_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("PROJECT_ID", ""))
_SERVICE_NAME = os.getenv("K_SERVICE", "mydoot-voice-agent")
_REVISION = os.getenv("K_REVISION", "local")


def cloud_log(
    message: str,
    *,
    severity: str = "INFO",
    caller_id: str = "",
    call_id: str = "",
    **extra,
) -> None:
    """
    Emit a structured JSON log line to stdout.

    Cloud Run picks this up and sends it to Cloud Logging with
    proper severity, labels, and searchable fields.

    Severity levels: DEBUG, INFO, NOTICE, WARNING, ERROR, CRITICAL
    """
    entry = {
        "severity": severity.upper(),
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "serviceContext": {
            "service": _SERVICE_NAME,
            "version": _REVISION,
        },
    }

    # Add caller context as labels (searchable in Cloud Logging)
    labels = {}
    if caller_id:
        labels["caller_id"] = str(caller_id)
        entry["caller_id"] = str(caller_id)
    if call_id:
        labels["call_id"] = str(call_id)
    if labels:
        entry["logging.googleapis.com/labels"] = labels

    # Add any extra fields (latency, event type, etc.)
    if extra:
        entry.update(extra)

    # Write as single JSON line — Cloud Run parses this automatically
    print(json.dumps(entry, ensure_ascii=False, default=str), flush=True)


def cloud_metric(
    metric_name: str,
    value: float,
    *,
    caller_id: str = "",
    **labels,
) -> None:
    """
    Log a metric as a structured log entry with a metric label.

    Cloud Logging can create log-based metrics from these entries,
    which appear in Cloud Monitoring automatically.

    To create a log-based metric in GCP Console:
    1. Go to Cloud Logging → Log-based Metrics
    2. Create metric with filter: jsonPayload.metric_name="call_duration_s"
    3. Set value to jsonPayload.metric_value
    4. Metric appears in Cloud Monitoring
    """
    entry = {
        "severity": "INFO",
        "message": f"METRIC {metric_name}={value}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metric_name": metric_name,
        "metric_value": value,
    }
    if caller_id:
        entry["caller_id"] = str(caller_id)
    if labels:
        entry["metric_labels"] = labels

    print(json.dumps(entry, ensure_ascii=False, default=str), flush=True)


def cloud_trace_event(
    event: str,
    *,
    caller_id: str = "",
    call_start_ms: float = 0,
    **data,
) -> None:
    """
    Log a trace event with timing info.

    Cloud Logging can group these by caller_id/call_id to reconstruct
    the full call trace. For proper Cloud Trace integration, use
    OpenTelemetry (Phase 2).
    """
    elapsed_ms = round((time.perf_counter() * 1000) - call_start_ms) if call_start_ms else 0
    entry = {
        "severity": "INFO",
        "message": f"TRACE {event}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_event": event,
        "elapsed_ms": elapsed_ms,
    }
    if caller_id:
        entry["caller_id"] = str(caller_id)
    if data:
        entry.update(data)

    print(json.dumps(entry, ensure_ascii=False, default=str), flush=True)
