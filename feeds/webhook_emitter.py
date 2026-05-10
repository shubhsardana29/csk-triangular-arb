"""WebhookEmitter — fire-and-forget HTTP event emitter for n8n integration.

Puts events into an in-memory queue and drains it in a background task.
emit() is always non-blocking: if the queue is full, the event is dropped
and a warning is logged. The tick loop is never delayed.

Usage:
    emitter = WebhookEmitter(url=config.N8N_WEBHOOK_URL, enabled=config.N8N_WEBHOOK_ENABLED)
    await emitter.start()
    await emitter.emit("opportunity", {"symbol": "DOGE", "profit_pct": 0.02})
    await emitter.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_QUEUE_MAX = 100
_WARN_THRESHOLD = 50  # log warning when queue exceeds this


def _default_serializer(obj: Any) -> Any:
    """Convert Decimal to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class WebhookEmitter:
    """Non-blocking HTTP event emitter. All network I/O happens in a background task."""

    def __init__(self, url: str, enabled: bool = True):
        self._url     = url.strip()
        self._enabled = enabled and bool(self._url)
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self._enabled:
            return
        self._session = aiohttp.ClientSession()
        self._task    = asyncio.create_task(self._drain(), name="webhook-emitter")
        log.info("[webhook] emitter started — url=%s", self._url)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            await self._session.close()

    async def emit(self, event_type: str, payload: dict) -> None:
        """Queue an event for delivery. Never blocks; drops silently if queue is full."""
        if not self._enabled:
            return
        body = {"event": event_type, **payload}
        try:
            self._queue.put_nowait(body)
        except asyncio.QueueFull:
            log.warning("[webhook] queue full (%d) — dropping event %s", _QUEUE_MAX, event_type)

    async def _drain(self) -> None:
        """Background task: consume queue and POST each event to n8n."""
        while True:
            body = await self._queue.get()
            qsize = self._queue.qsize()
            if qsize > _WARN_THRESHOLD:
                log.warning(
                    "[webhook] queue at %d/%d — n8n may be slow or unreachable", qsize, _QUEUE_MAX
                )
            try:
                data = json.dumps(body, default=_default_serializer)
                async with self._session.post(
                    self._url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status >= 400:
                        log.warning("[webhook] n8n returned HTTP %d for event %s", resp.status, body.get("event"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("[webhook] emit failed for event %s: %s", body.get("event"), exc)
