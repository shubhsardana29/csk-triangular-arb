from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from time import time

import socketio

from core.models import Depth

log = logging.getLogger(__name__)

CSK_WS_URL = "wss://ws.coinswitch.co/"
CSK_NAMESPACE = "/coinswitchx"
CSK_SOCKETIO_PATH = "pro/realtime-rates-socket/spot/coinswitchx"
_USDT_INR = "USDT,INR"  # Note: pair format is "BTC,INR" not "BTC/INR"


def _parse_levels(raw: list[list[str]]) -> tuple[tuple[Decimal, Decimal], ...]:
    """Convert CSK order book levels [[price, quantity], ...] to Depth tuples."""
    out: list[tuple[Decimal, Decimal]] = []
    for lvl in raw:
        try:
            p = Decimal(str(lvl[0]))
            q = Decimal(str(lvl[1]))
            if p > 0 and q > 0:
                out.append((p, q))
        except (IndexError, ValueError, Exception):
            pass
    return tuple(out)


class CSKPublicWS:
    """Live INR depth + USDT/INR rate via CSK socket.io WebSocket."""

    def __init__(self, ws_url: str = CSK_WS_URL):
        self.ws_url = ws_url
        self.books: dict[str, Depth] = {}
        self.book_ts: dict[str, float] = {}   # per-instrument last-update timestamp
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
        # Ensure all instruments are formatted as 'SYMBOL,INR'
        formatted_instruments = set()
        for instr in instruments:
            if "," in instr:
                formatted_instruments.add(instr.upper())
            elif "/" in instr:
                base, quote = instr.split("/")
                formatted_instruments.add(
                    f"{base.strip().upper()},{quote.strip().upper()}"
                )
            else:
                # fallback, just add as is
                formatted_instruments.add(instr.upper())
        self._instruments = formatted_instruments
        log.info(
            "CSK WS subscribing to instrument pairs: %s", sorted(self._instruments)
        )
        if self._sio.connected:
            await self._subscribe_all()

    async def connect(self) -> None:
        """Connect and keep alive. Run as a background task."""
        headers = {"Origin": "https://coinswitch.co"}
        try:
            await self._sio.connect(
                self.ws_url,
                namespaces=[CSK_NAMESPACE],
                transports=["websocket"],
                socketio_path=CSK_SOCKETIO_PATH,
                headers=headers,
            )
            await self._sio.wait()  # blocks until disconnect
        except asyncio.CancelledError:
            await self._sio.disconnect()
        except Exception:
            log.exception("CSK public WS failed to connect to %s", self.ws_url)

    async def disconnect(self) -> None:
        await self._sio.disconnect()

    async def _subscribe_all(self) -> None:
        # USDT,INR is always subscribed regardless of the instrument list.
        for instrument in {_USDT_INR} | self._instruments:
            log.debug("Subscribing to CSK pair: %s", instrument)
            await self._sio.emit(
                "FETCH_ORDER_BOOK_CS_PRO",
                {"event": "subscribe", "pair": instrument},
                namespace=CSK_NAMESPACE,
            )
        log.info(
            "CSK WS subscribed FETCH_ORDER_BOOK_CS_PRO for %d instruments",
            len(self._instruments) + 1,
        )

    def _setup_handlers(self) -> None:
        sio = self._sio

        @sio.event(namespace=CSK_NAMESPACE)
        async def connect():
            log.info("CSK public WS connected → subscribing")
            await self._subscribe_all()

        @sio.event(namespace=CSK_NAMESPACE)
        async def disconnect():
            log.warning("CSK public WS disconnected (will reconnect)")

        @sio.event(namespace=CSK_NAMESPACE)
        async def connect_error(data):
            log.error("CSK public WS connect error: %s", data)

        @sio.on("FETCH_ORDER_BOOK_CS_PRO", namespace=CSK_NAMESPACE)
        async def on_order_book(data: dict):
            if not isinstance(data, dict):
                return
            instrument = data.get("s", "")
            if not instrument:
                return
            if instrument != _USDT_INR and instrument not in self._instruments:
                return

            try:
                bids = _parse_levels(data.get("bids") or [])
                asks = _parse_levels(data.get("asks") or [])
                # Normalize "BTC,INR" → "BTC/INR" so the rest of the system
                # can use a single consistent key format.
                key = instrument.replace(",", "/")
                self.books[key] = Depth(bids=bids, asks=asks)
                self.book_ts[key] = time()
                self._last_msg_ts = time()
            except Exception:
                log.exception(
                    "CSK WS failed to parse FETCH_ORDER_BOOK_CS_PRO for %s", instrument
                )
