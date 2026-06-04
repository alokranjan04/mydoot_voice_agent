# -*- coding: utf-8 -*-
"""
app.py — entry point only.

Registers all routes and starts the aiohttp server.
Business logic lives in:
  config/     — settings, env vars
  core/       — recorder, Hindi utilities
  pipelines/  — sarvam handler, gemini handler
  routes/     — webhook, dashboard, metrics
Prompts and tool schemas are in app_config.json.
"""
import asyncio, sys, os, json
from aiohttp import web

from config.settings import APP_CONFIG, PORT
from pipelines.sarvam import sarvam_handler
from pipelines.gemini import gemini_handler
from routes.webhook   import handle_answer
from routes.dashboard import home_page, set_provider, set_parameters
from routes.voice_lab import voice_lab_page
from routes.metrics   import metrics_page, metrics_data
from routes.uploads   import upload_file, list_files, delete_file
from routes.calls     import calls_page, calls_data, audio_proxy
from mydoot_functions import get_google_creds_health, get_gmail_health, SPREADSHEET_ID
from config.database import init_db

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


async def health_check(request):
    google_health = get_google_creds_health()
    gmail_health = get_gmail_health()
    return web.json_response({
        "status": "ok",
        "provider": APP_CONFIG.get("active_provider", "sarvam"),
        "google_sheets": google_health,
        "spreadsheet_id": SPREADSHEET_ID,
        "gmail": gmail_health,
        "public_url": os.getenv("PUBLIC_URL", ""),
    })


async def main():
    # ── Reconstruct Google Credentials from Environment ───────────────────
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if creds_json and creds_json.startswith("{"):
        try:
            json_data = json.loads(creds_json)
            with open("google-credentials.json", "w", encoding="utf-8") as f:
                json.dump(json_data, f, indent=2)
        except Exception:
            pass

    # ── Bootstrap PostgreSQL (no-op if POSTGRES_URL unset) ────────────────────
    init_db()

    app = web.Application()

    app.router.add_get( "/",                  home_page)
    app.router.add_get( "/voice-lab",         voice_lab_page)
    app.router.add_post("/answer",            handle_answer)
    app.router.add_post("/api/set-provider",  set_provider)
    app.router.add_post("/api/set-parameters", set_parameters)
    app.router.add_get( "/sarvam-stream",     sarvam_handler)
    app.router.add_get( "/gemini-stream",     gemini_handler)
    app.router.add_get( "/metrics",           metrics_page)
    app.router.add_get( "/metrics/data",      metrics_data)
    app.router.add_get( "/health",           health_check)
    app.router.add_post("/api/upload",        upload_file)
    app.router.add_get( "/api/files",         list_files)
    app.router.add_post("/api/delete-file",   delete_file)
    app.router.add_static("/recordings",      "recordings", show_index=True)
    app.router.add_get( "/calls",             calls_page)
    app.router.add_get( "/calls/data",        calls_data)
    app.router.add_get( "/calls/audio",       audio_proxy)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    provider = APP_CONFIG.get("active_provider", "sarvam")
    print(f"🚀 MYDOOT CUSTOMER CARE ONLINE — PORT {PORT}")
    print(f"   Active pipeline : {provider.upper()}")
    print(f"   Dashboard       : http://localhost:{PORT}/")
    print(f"   Metrics         : http://localhost:{PORT}/metrics")
    await asyncio.Future()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
