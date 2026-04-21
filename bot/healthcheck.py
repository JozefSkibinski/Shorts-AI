"""Lightweight aiohttp healthcheck server.

Runs in the same event loop as the Discord bot (no threads). Exposes:

- ``GET /health``  – always 200, JSON ``{"status": "ok", ...}``
- ``GET /ready``   – 200 once the bot has finished its ready handshake,
                     503 before that. Kubernetes-style readiness signal.
- ``GET /metrics`` – cache stats. Not Prometheus-formatted (keep deps light)
                     but enough for quick debugging.

Designed to run alongside the bot by calling :func:`start_healthcheck_server`
from :meth:`ShortsAIBot.setup_hook` and :func:`stop_healthcheck_server` in
teardown.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

log = logging.getLogger(__name__)


class HealthcheckServer:
    def __init__(
        self,
        bot,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self._bot = bot
        self._host = host
        self._port = port
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.BaseSite] = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/ready", self._ready)
        app.router.add_get("/metrics", self._metrics)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()
        log.info("Healthcheck listening on http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- handlers ----------------------------------------------------------

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "ready": self._bot.is_ready()}
        )

    async def _ready(self, _request: web.Request) -> web.Response:
        if self._bot.is_ready():
            return web.json_response({"status": "ready"})
        return web.json_response({"status": "starting"}, status=503)

    async def _metrics(self, _request: web.Request) -> web.Response:
        result: dict = {"ready": self._bot.is_ready()}
        # These attributes are attached by ShortsAIBot when caches are wired.
        for name in ("result_cache", "suggestion_cache"):
            cache = getattr(self._bot, name, None)
            if cache is not None:
                result[name] = cache.stats().as_dict()
        return web.json_response(result)
