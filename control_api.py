"""ControlAPI — tiny HTTP server for n8n → bot control plane.

Runs inside the bot's asyncio event loop (same process). Binds to localhost
only so it's not reachable from the network even if the firewall is open.

Endpoints:
  GET  /status          — returns current config values as JSON
  POST /control         — applies one or more parameter changes
  GET  /health          — simple liveness probe for n8n

Authentication: every request must include the header
  X-Control-Secret: <CONTROL_API_SECRET env var>
Requests without a valid secret receive 401.

Supported /control fields:
  min_spread_pct    float   — new TWO_LEG_MIN_SPREAD_PCT (clamped 0.005–0.10)
  three_leg_enabled bool    — enable/disable 3-leg strategy
  two_leg_enabled   bool    — enable/disable 2-leg strategy

Example curl:
  curl -s -H "X-Control-Secret: mysecret" http://127.0.0.1:8765/status
  curl -s -X POST -H "Content-Type: application/json" \\
       -H "X-Control-Secret: mysecret" \\
       -d '{"min_spread_pct": 0.012}' \\
       http://127.0.0.1:8765/control
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiohttp import web

import config

log = logging.getLogger(__name__)

_MIN_SPREAD_FLOOR = Decimal("0.005")   # 0.5% — never go below this
_MIN_SPREAD_CAP   = Decimal("0.10")    # 10%  — sanity upper bound


class ControlAPI:
    """Async HTTP control server. Call start() once inside the bot's event loop."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        secret: str = "",
        two_leg_ranker=None,   # TwoLegRanker | None — notified on min_spread changes
    ):
        self._host           = host
        self._port           = port
        self._secret         = secret.strip()
        self._two_leg_ranker = two_leg_ranker

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/health",  self._health)
        app.router.add_get("/status",  self._status)
        app.router.add_post("/control", self._control)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("[control-api] listening on http://%s:%d", self._host, self._port)

    # ── middleware ────────────────────────────────────────────────────────────

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if self._secret:
            incoming = request.headers.get("X-Control-Secret", "")
            if incoming != self._secret:
                return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    # ── handlers ──────────────────────────────────────────────────────────────

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _status(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "min_spread_pct":     float(config.TWO_LEG_MIN_SPREAD_PCT),
            "three_leg_enabled":  config.THREE_LEG_ENABLED,
            "two_leg_enabled":    config.TWO_LEG_ENABLED,
            "taker_fee":          float(config.TAKER_FEE),
            "tds_rate":           float(config.TDS_RATE),
        })

    async def _control(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        applied: list[str] = []
        errors:  list[str] = []

        if "min_spread_pct" in body:
            try:
                new_val = Decimal(str(body["min_spread_pct"]))
                clamped = max(_MIN_SPREAD_FLOOR, min(_MIN_SPREAD_CAP, new_val))
                config.TWO_LEG_MIN_SPREAD_PCT = clamped
                applied.append(f"min_spread_pct={float(clamped):.4f}")
                log.warning(
                    "[control-api] min_spread_pct set to %.4f (requested %.4f)",
                    float(clamped), float(new_val),
                )
            except Exception as exc:
                errors.append(f"min_spread_pct: {exc}")

        if "three_leg_enabled" in body:
            config.THREE_LEG_ENABLED = bool(body["three_leg_enabled"])
            applied.append(f"three_leg_enabled={config.THREE_LEG_ENABLED}")
            log.warning("[control-api] three_leg_enabled=%s", config.THREE_LEG_ENABLED)

        if "two_leg_enabled" in body:
            config.TWO_LEG_ENABLED = bool(body["two_leg_enabled"])
            applied.append(f"two_leg_enabled={config.TWO_LEG_ENABLED}")
            log.warning("[control-api] two_leg_enabled=%s", config.TWO_LEG_ENABLED)

        if not applied and not errors:
            return web.json_response({"error": "No recognised fields"}, status=400)

        return web.json_response({
            "applied": applied,
            "errors":  errors,
            "status": {
                "min_spread_pct":    float(config.TWO_LEG_MIN_SPREAD_PCT),
                "three_leg_enabled": config.THREE_LEG_ENABLED,
                "two_leg_enabled":   config.TWO_LEG_ENABLED,
            },
        })
