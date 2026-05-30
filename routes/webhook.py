# -*- coding: utf-8 -*-
"""
Vobiz webhook handler.

Vobiz POSTs to /answer on each incoming call.
We parse the caller ID, decide which pipeline is active, and return the
XML instruction that tells Vobiz to open a bidirectional WebSocket stream.
"""
from aiohttp import web
from config.settings import APP_CONFIG


async def handle_answer(request: web.Request) -> web.Response:
    host     = request.headers.get("X-Forwarded-Host") or request.host
    provider = APP_CONFIG.get("active_provider", "sarvam")
    path     = "/gemini-stream" if provider == "google" else "/sarvam-stream"

    try:
        body = await request.post()
        raw  = body.get("From") or body.get("CallerName") or "Unknown"
        cid  = str(raw).replace("+", "").strip()
        if "sip:" in cid:
            cid = cid.split("sip:")[1].split("@")[0]
    except Exception:
        cid = "Unknown"

    ws_scheme = "wss" if request.secure else "ws"
    ws_url = f"{ws_scheme}://{host}{path}?caller_id={cid}"
    xml    = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Stream bidirectional="true" keepCallAlive="true" '
        f'contentType="audio/x-mulaw;rate=8000">{ws_url}</Stream>'
        "</Response>"
    )
    print(f"[INCOMING] Provider={provider}  Caller={cid}")
    return web.Response(text=xml, content_type="text/xml")
