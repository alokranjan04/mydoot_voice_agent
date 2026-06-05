# -*- coding: utf-8 -*-
"""
GET /latency?hours=N  — Return P50/P95/P99 latency percentiles as JSON.
"""
from aiohttp import web
from mydoot_functions import get_latency_stats


async def latency_data(request: web.Request) -> web.Response:
    hours = int(request.rel_url.query.get("hours", 24))
    stats = await request.loop.run_in_executor(None, get_latency_stats, hours)
    return web.json_response(stats)
