# -*- coding: utf-8 -*-
import os, json
from dotenv import load_dotenv

load_dotenv()

SARVAM_API_KEY   = os.getenv("SARVAM_API_KEY",   "").strip()
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY",  "").strip()
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY",    "").strip()
PORT             = int(os.getenv("PORT", "5050"))

# ── PostgreSQL + multi-instance ───────────────────────────────────────────────
# POSTGRES_URL format: postgresql://user:password@34.122.77.178:5432/mydoot
# Leave unset to run without PostgreSQL (Sheets-only mode).
POSTGRES_URL = os.getenv("POSTGRES_URL", "").strip()
# INSTANCE_ID identifies which client/tenant this Cloud Run service belongs to.
# Set a unique value per service deployment (e.g. "mydoot", "client_abc").
INSTANCE_ID  = os.getenv("INSTANCE_ID", "default").strip()

SARVAM_CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"
SARVAM_TTS_URL  = "https://api.sarvam.ai/text-to-speech"

GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage"
    f".v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"
)

# Deepgram streaming STT
# encoding=mulaw matches Vobiz audio/x-mulaw;rate=8000
# sample_rate=8000 must match the actual Vobiz stream rate
# endpointing=400 — detect end-of-speech after 400ms silence
# utterance_end_ms=1000 — flush partial hypothesis after 1s gap
DG_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-2&language=hi&encoding=mulaw&sample_rate=8000"
    "&interim_results=true&endpointing=400&utterance_end_ms=1000"
)

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app_config.json")

def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

APP_CONFIG: dict = _load_config()

def save_config() -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(APP_CONFIG, f, ensure_ascii=False, indent=4)
