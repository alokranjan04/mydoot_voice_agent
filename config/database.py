# -*- coding: utf-8 -*-
"""
config/database.py — PostgreSQL connection pool and schema bootstrap.

Uses psycopg2 ThreadedConnectionPool because all DB writes happen in
threads (via asyncio.to_thread), not in coroutines directly.

Usage:
    from config.database import init_db, get_conn, put_conn

    # Once at startup:
    init_db()

    # Per-call (use try/finally to return connection):
    conn = get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(...)
            conn.commit()
            put_conn(conn)
        except Exception:
            put_conn(conn, discard=True)
"""
import json
import logging

import psycopg2
from psycopg2 import pool as pg_pool

from config.settings import INSTANCE_ID, POSTGRES_URL

logger = logging.getLogger(__name__)

_pool: pg_pool.ThreadedConnectionPool | None = None

# ── Schema DDL ────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS instances (
    id            SERIAL PRIMARY KEY,
    instance_id   TEXT NOT NULL UNIQUE,
    display_name  TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS service_requests (
    id             BIGSERIAL PRIMARY KEY,
    instance_id    TEXT        NOT NULL REFERENCES instances(instance_id),
    caller_id      TEXT,
    customer_name  TEXT,
    category       TEXT,
    subcategory    TEXT,
    issue_type     TEXT,
    brand          TEXT,
    model          TEXT,
    severity       TEXT,
    address        TEXT,
    preferred_time TEXT,
    raw_json       JSONB,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS call_logs (
    id             BIGSERIAL PRIMARY KEY,
    instance_id    TEXT        NOT NULL REFERENCES instances(instance_id),
    caller_id      TEXT,
    duration_secs  NUMERIC(8,1),
    stage_reached  TEXT,
    saved          BOOLEAN,
    category       TEXT,
    subcategory    TEXT,
    issue_type     TEXT,
    customer_name  TEXT,
    address        TEXT,
    preferred_time TEXT,
    stt_count      INTEGER,
    stt_avg_ms     NUMERIC(8,1),
    stt_drops      INTEGER,
    barge_ins      INTEGER,
    reconnects     INTEGER,
    audio_gcs      TEXT,
    local_wav      TEXT        DEFAULT '',
    transcript     TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sr_instance  ON service_requests(instance_id);
CREATE INDEX IF NOT EXISTS idx_sr_created   ON service_requests(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cl_instance  ON call_logs(instance_id);
CREATE INDEX IF NOT EXISTS idx_cl_created   ON call_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS turn_latency_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    instance_id         TEXT    NOT NULL REFERENCES instances(instance_id),
    caller_id           TEXT,
    turn_id             INTEGER,
    customer_text       TEXT,
    vad_ms              INTEGER,
    stt_ms              INTEGER,
    langgraph_ms        INTEGER,
    llm_first_token_ms  INTEGER,
    llm_total_ms        INTEGER,
    tts_first_audio_ms  INTEGER,
    tts_total_ms        INTEGER,
    end_to_end_turn_ms  INTEGER,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tlm_instance ON turn_latency_metrics(instance_id);
CREATE INDEX IF NOT EXISTS idx_tlm_created  ON turn_latency_metrics(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tlm_e2e      ON turn_latency_metrics(end_to_end_turn_ms);

CREATE TABLE IF NOT EXISTS field_quality_log (
    id               BIGSERIAL PRIMARY KEY,
    instance_id      TEXT        NOT NULL REFERENCES instances(instance_id),
    caller_id        TEXT        NOT NULL,
    field            TEXT        NOT NULL,
    first_value      TEXT        DEFAULT '',
    final_value      TEXT        DEFAULT '',
    num_attempts     INTEGER     NOT NULL DEFAULT 1,
    num_corrections  INTEGER     NOT NULL DEFAULT 0,
    confidence       NUMERIC(6,4),
    source           TEXT        NOT NULL DEFAULT 'gemini',
    call_saved       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fql_instance ON field_quality_log(instance_id);
CREATE INDEX IF NOT EXISTS idx_fql_field    ON field_quality_log(field);
CREATE INDEX IF NOT EXISTS idx_fql_created  ON field_quality_log(created_at DESC);
"""


def init_db() -> bool:
    """
    Open the connection pool, run DDL, upsert the current instance row.
    Returns True on success, False if POSTGRES_URL is unset or connection fails.
    Called synchronously from app.py main() before routes are registered.
    Safe to call multiple times — subsequent calls are no-ops if pool is open.
    """
    global _pool

    if _pool is not None:
        return True

    if not POSTGRES_URL:
        logger.warning("[DB] POSTGRES_URL not set — PostgreSQL disabled (Sheets-only mode).")
        return False

    try:
        _pool = pg_pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=POSTGRES_URL,
            connect_timeout=5,
        )
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(_DDL)
                # Migrations for columns added after initial deployment
                cur.execute(
                    "ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS local_wav TEXT DEFAULT ''"
                )
                cur.execute(
                    """
                    INSERT INTO instances (instance_id, display_name)
                    VALUES (%s, %s)
                    ON CONFLICT (instance_id) DO NOTHING
                    """,
                    (INSTANCE_ID, INSTANCE_ID),
                )
            conn.commit()
        finally:
            _pool.putconn(conn)

        logger.info(f"[DB] PostgreSQL ready — instance_id={INSTANCE_ID!r}")
        print(f"[DB] PostgreSQL ready — instance_id={INSTANCE_ID!r}")
        return True

    except Exception as exc:
        logger.error(f"[DB INIT ERROR] {exc}")
        print(f"[DB INIT ERROR] {exc}")
        _pool = None
        return False


def get_conn():
    """Borrow a connection from the pool. Caller MUST call put_conn() afterwards."""
    if _pool is None:
        return None
    try:
        return _pool.getconn()
    except Exception as e:
        logger.error(f"[DB] get_conn failed: {e}")
        return None


def put_conn(conn, discard: bool = False) -> None:
    """Return a connection to the pool. Pass discard=True after an exception."""
    if _pool and conn:
        _pool.putconn(conn, close=discard)
