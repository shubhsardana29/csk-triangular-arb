"""CoinSwitch public socket.io WebSocket — INR depth and USDT/INR rate.

Subscribes to DEPTH_UPDATE for every `{symbol}/INR` instrument and for
USDT/INR (FX rate + USDT/INR book).

Architecture note: strategy/ never imports this. TriEngine receives it as
an injected dependency so the strategy layer stays exchange-agnostic.

Adapted from simple-arb's csk/ws_client.py for the Depth type used here
(tuple-based levels instead of PriceLevel dataclasses).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from time import time

import socketio

from core.models import Depth

log = logging.getLogger(__name__)

CSK_WS_URL = "wss://exchange-websocket.coinswitch.co/"
_USDT_INR   = "USDT/INR"


def _parse_levels(raw: list[dict]) -> tuple[tuple[Decimal, Decimal], ...]:
    """Convert CSK DEPTH_UPDATE levels [{"price": "...", "quantity": "..."}] to Depth tuples."""
    out: list[tuple[Decimal, Decimal]] = []
    for lvl in raw:
        try:
            p = Decimal(str(lvl["price"]))
            q = Decimal(str(lvl["quantity"]))
            if p > 0 and q > 0:
                out.append((p, q))
        except (KeyError, ValueError, Exception):
            pass
    return tuple(out)


class CSKPublicWS:
    """Live INR depth + USDT/INR rate via CSK socket.io WebSocket.

    books[instrument]  → Depth snapshot keyed by instrument string
                         e.g. "BTC/INR", "USDT/INR"
    age_s()            → seconds since last DEPTH_UPDATE (staleness)
    """

    def __init__(self, ws_url: str = CSK_WS_URL):
        self.ws_url = ws_url
        self.books: dict[str, Depth] = {}
        self._last_msg_ts: float = 0.0
        self._instruments: set[str] = set()
        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_delay_max=10,
            logger=False,
        )
        self._setup_handlers()

    def age_s(self) -> float:
        return (time() - self._last_msg_ts) if self._last_msg_ts else float("inf")

    async def subscribe(self, instruments: list[str]) -> None:
        """Set the instruments to subscribe to. Subscribes immediately if connected."""
        self._instruments = set(instruments)
        if self._sio.connected:
            await self._subscribe_all()

    async def connect(self) -> None:
        """Connect and keep alive. Run as a background task."""
        # Origin header required — CSK WS returns 403 without it.
        headers = {"Origin": "https://coinswitch.co"}
        try:
            await self._sio.connect(
                self.ws_url,
                transports=["websocket"],
                headers=headers,
            )
            await self._sio.wait()    # blocks until disconnect
        except asyncio.CancelledError:
            await self._sio.disconnect()
        except Exception:
            log.exception("CSK public WS failed to connect to %s", self.ws_url)

    async def disconnect(self) -> None:
        await self._sio.disconnect()

    # ── internals ─────────────────────────────────────────────────────────────

    async def _subscribe_all(self) -> None:
        # USDT/INR is always subscribed regardless of the instrument list.
        for instrument in {_USDT_INR} | self._instruments:
            await self._sio.emit("DEPTH_UPDATE", {"event": "subscribe", "pair": instrument})
        log.info(
            "CSK WS subscribed DEPTH_UPDATE for %d instruments",
            len(self._instruments) + 1,
        )

    def _setup_handlers(self) -> None:
        sio = self._sio

        @sio.on("connect")
        async def _on_connect():
            log.info("CSK public WS connected → subscribing")
            await self._subscribe_all()

        @sio.on("disconnect")
        async def _on_disconnect():
            log.warning("CSK public WS disconnected (will reconnect)")

        @sio.on("connect_error")
        async def _on_error(data):
            log.error("CSK public WS connect error: %s", data)

        @sio.on("DEPTH_UPDATE")
        async def _on_depth(data: dict):
            if not isinstance(data, dict):
                return
            instrument = data.get("Instrument", "")
            if not instrument:
                return
            if instrument != _USDT_INR and instrument not in self._instruments:
                return

            try:
                bids = _parse_levels(data.get("Buy")  or [])
                asks = _parse_levels(data.get("Sell") or [])
                self.books[instrument] = Depth(bids=bids, asks=asks)
                self._last_msg_ts = time()
            except Exception:
                log.exception("CSK WS failed to parse DEPTH_UPDATE for %s", instrument)
