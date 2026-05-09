"""OrderPoller — polls CSK REST for fill and terminal events.

Runs at 1 Hz when no orders are in flight, 10 Hz while tracking. Emits:
  on_fill(order_id, delta_qty, avg_price)  — for each new fill delta
  on_done(order_id, status)               — when order reaches terminal status

Run as a background asyncio task via start(). strategy/ never imports api_client
directly; the client is injected at the wiring layer (main.py).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

log = logging.getLogger(__name__)

IDLE_INTERVAL_S   = 1.0    # poll rate when no orders tracked
ACTIVE_INTERVAL_S = 0.10   # 10 Hz while orders are in flight
TERMINAL_STATUSES = frozenset({"FULFILLED", "CANCELLED", "PARTIALLY_CANCELLED"})

FillCallback = Callable[[str, Decimal, Decimal], Awaitable[None]]
DoneCallback = Callable[[str, str], Awaitable[None]]


class OrderPoller:
    """Polls CSK REST for fill events at 1 Hz (idle) / 10 Hz (active).

    Usage:
        poller = OrderPoller(client, on_fill=..., on_done=...)
        asyncio.create_task(poller.start())   # background task
        poller.track("oid-123", meta={...})   # register an order to watch
    """

    def __init__(
        self,
        client,
        on_fill: FillCallback,
        on_done: DoneCallback,
    ) -> None:
        self._client  = client
        self._on_fill = on_fill
        self._on_done = on_done
        # order_id → {"meta": dict, "last_filled": Decimal}
        self._tracked: dict[str, dict] = {}

    # ── public ────────────────────────────────────────────────────────────────

    def track(self, order_id: str, meta: dict) -> None:
        """Register an order for polling."""
        self._tracked[order_id] = {"meta": meta, "last_filled": Decimal(0)}
        log.debug("[poller] tracking %s  meta=%s", order_id, meta)

    def untrack(self, order_id: str) -> None:
        self._tracked.pop(order_id, None)

    @property
    def active_count(self) -> int:
        return len(self._tracked)

    async def start(self) -> None:
        """Polling loop — run as a background asyncio task."""
        while True:
            interval = ACTIVE_INTERVAL_S if self._tracked else IDLE_INTERVAL_S
            await asyncio.sleep(interval)
            if not self._tracked:
                continue
            await self._poll_once()

    # ── internals ─────────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        for oid in list(self._tracked):
            try:
                data = await self._client.get_order_status(oid)
                if data:
                    await self._handle_response(oid, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[poller] error polling order %s", oid)

    async def _handle_response(self, oid: str, data: dict) -> None:
        state = self._tracked.get(oid)
        if state is None:
            return

        status = str(data.get("status", "")).upper()
        try:
            filled_qty = Decimal(str(data.get("filledQuantity") or "0"))
        except Exception:
            filled_qty = Decimal(0)
        try:
            avg_price = Decimal(
                str(data.get("avgPrice") or data.get("limitPrice") or "0")
            )
        except Exception:
            avg_price = Decimal(0)

        delta = filled_qty - state["last_filled"]
        if delta > 0 and avg_price > 0:
            state["last_filled"] = filled_qty
            log.info(
                "[poller] fill  oid=%s  Δqty=%.6f  avg=%.4f",
                oid, float(delta), float(avg_price),
            )
            try:
                await self._on_fill(oid, delta, avg_price)
            except Exception:
                log.exception("[poller] on_fill raised for %s", oid)

        if status in TERMINAL_STATUSES:
            log.info("[poller] done  oid=%s  status=%s", oid, status)
            self.untrack(oid)
            try:
                await self._on_done(oid, status)
            except Exception:
                log.exception("[poller] on_done raised for %s", oid)
