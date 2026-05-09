"""Binance public WebSocket — top-20 depth at 100ms intervals.

Subscribes to `{symbol}usdt@depth20@100ms` for every symbol and keeps
`books[symbol]` as a live Depth snapshot. Reconnects automatically on
any error.

Uses aiohttp (already a project dependency) rather than the `websockets`
package to avoid an extra dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import time

import aiohttp

from core.models import Depth

log = logging.getLogger(__name__)

_BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
_SUBSCRIBE_BATCH = 200   # Binance hard limit per subscribe payload


class BinanceDepthFeed:
    """Maintains live Depth snapshots for S/USDT pairs via Binance public WS.

    books[symbol]  → latest Depth for that symbol (e.g. books["BTC"])
    age_s()        → seconds since last message (used for staleness checks)
    """

    def __init__(self, symbols: list[str]):
        self.symbols: list[str] = [s.upper() for s in symbols]
        self.books: dict[str, Depth] = {}
        self._last_msg_ts: float = 0.0
        self._running: bool = False

    def age_s(self) -> float:
        return (time() - self._last_msg_ts) if self._last_msg_ts else float("inf")

    async def start(self) -> None:
        """Connect and listen. Reconnects automatically. Run as a background task."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except aiohttp.ClientError as e:
                log.warning("Binance depth WS disconnected: %s — reconnecting in 3s", e)
                await asyncio.sleep(3)
            except Exception:
                log.exception("Binance depth WS unexpected error — reconnecting in 5s")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False

    # ── internals ─────────────────────────────────────────────────────────────

    def _stream_names(self) -> list[str]:
        return [f"{s.lower()}usdt@depth20@100ms" for s in self.symbols]

    async def _connect_and_listen(self) -> None:
        streams = self._stream_names()
        if not streams:
            log.warning("BinanceDepthFeed: no symbols to subscribe")
            return

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                _BINANCE_WS_URL,
                heartbeat=20,
                receive_timeout=30,
            ) as ws:
                log.info(
                    "Binance depth WS connected — subscribing to %d streams", len(streams)
                )
                # Subscribe in batches (Binance allows max 200 per message).
                for i in range(0, len(streams), _SUBSCRIBE_BATCH):
                    batch = streams[i : i + _SUBSCRIBE_BATCH]
                    await ws.send_json({
                        "method": "SUBSCRIBE",
                        "params": batch,
                        "id": i // _SUBSCRIBE_BATCH + 1,
                    })
                    # Consume the ack so it doesn't interfere with data messages.
                    ack = await ws.receive_json()
                    log.debug("Binance subscribe ack: %s", ack)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle(json.loads(msg.data))
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        log.warning("Binance depth WS closed/error: %s", msg)
                        break

    def _handle(self, msg: dict) -> None:
        data = msg.get("data", msg)
        bids = data.get("bids")
        asks = data.get("asks")
        stream = msg.get("stream", "")
        if not bids and not asks:
            return

        # stream = "btcusdt@depth20@100ms" → symbol = "BTC"
        symbol = stream.split("usdt@")[0].upper() if "usdt@" in stream else ""
        if not symbol:
            return

        self.books[symbol] = Depth.from_raw({"bids": bids or [], "asks": asks or []})
        self._last_msg_ts = time()
