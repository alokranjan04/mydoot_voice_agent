# -*- coding: utf-8 -*-
"""
Shared persistent aiohttp ClientSession with auto-recovery.
Import get_http() for outbound HTTP. Call reset_http() after connection errors
so the next request starts with a clean session instead of a stale one.
"""
import aiohttp

_SESSION: aiohttp.ClientSession | None = None


def get_http() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=20,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
        )
    return _SESSION


def reset_http() -> None:
    """Discard the current session so the next get_http() call starts fresh.
    Call this after a STREAM ERROR to avoid reusing a broken connection."""
    global _SESSION
    if _SESSION and not _SESSION.closed:
        try:
            import asyncio
            asyncio.get_event_loop().create_task(_SESSION.close())
        except Exception:
            pass
    _SESSION = None
