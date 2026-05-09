"""TriEngine — event loop, WebSocket orchestration, staleness watchdog.

Two operating modes depending on what feeds are injected:

  WS mode (production):
    BinanceDepthFeed + CSKPublicWS injected.
    run() starts feeds as background tasks, then drives a 10Hz tick loop
    that assembles TriBook snapshots from live WS state.
    A staleness watchdog logs a warning and skips ranking if either feed
    goes silent for more than STALENESS_THRESHOLD_S seconds.

  REST fallback mode (testing / no credentials):
    Neither feed injected.
    run() falls back to the original 1.5s REST polling loop via
    client.fetch_triangular_books(). Useful for smoke-testing without WS.

The engine is exchange-agnostic: it receives typed feeds and client as
constructor args. main.py / dashboard.py are the only wiring points.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from time import time

import config
from core.models import Depth, PathResult, TriBook
from strategy.tri_ranker import TriRanker
from strategy.shadow_executor import ShadowExecutor

# feeds/ types are injected at runtime from main.py/dashboard.py.
# String annotations keep strategy/ importable without feeds/ installed.

log = logging.getLogger(__name__)

TICK_INTERVAL_S      = 0.10   # 10 Hz
STALENESS_THRESHOLD_S = 15.0
WATCHDOG_INTERVAL_S  = 5.0

OpportunityCallback = Callable[
    [str, PathResult, PathResult, dict], Awaitable[None]
]  # symbol, net, gross, exec_result

TickCallback = Callable[
    [dict[str, TriBook], dict[str, tuple[PathResult, PathResult]], dict[str, dict], int, float],
    Awaitable[None],
]  # tri_books, ranked, exec_results, cycle, latency_ms


class TriEngine:
    """Drives the rank → execute loop at 10Hz (WS) or 1.5s (REST fallback)."""

    def __init__(
        self,
        *,
        client,                                         # CoinSwitchClient (duck-typed)
        ranker: TriRanker,
        executor: ShadowExecutor,
        symbols: list[str],
        binance_feed=None,   # BinanceDepthFeed | None  (injected from main.py)
        csk_ws=None,         # CSKPublicWS | None       (injected from main.py)
        polling_interval: float | None = None,
        on_opportunity: OpportunityCallback | None = None,
        on_tick: TickCallback | None = None,
    ):
        self._client           = client
        self._ranker           = ranker
        self._executor         = executor
        self._symbols          = [s.upper() for s in symbols]
        self._binance_feed     = binance_feed
        self._csk_ws           = csk_ws
        self._polling_interval = polling_interval if polling_interval is not None else config.POLLING_INTERVAL
        self._on_opportunity   = on_opportunity
        self._on_tick          = on_tick
        self._cycle            = 0
        self._booted           = False

    # ── boot ─────────────────────────────────────────────────────────────────

    async def boot(self) -> None:
        """Use REST to fetch initial prices and warm-start the executor.

        Called once regardless of WS/REST mode. The tick loop starts
        immediately after; WS data replaces REST data as it arrives.
        """
        if self._booted:
            return
        log.info("[engine] booting — fetching initial books for %d symbols", len(self._symbols))
        tri_books = await self._client.fetch_triangular_books(self._symbols, prefilter=True)

        # ShadowExecutor: build a simulated portfolio from config.
        # TriExecutor: start() fetches real balances from the exchange.
        if not self._executor.balances:
            self._executor.balances = config.build_initial_shadow_balances(self._symbols, tri_books)

        await self._executor.start()
        self._booted = True
        log.warning(
            "[engine] boot done — shadow portfolio ₹%s  symbols=%d",
            f"{float(config.SHADOW_PORTFOLIO_TOTAL_INR):,.0f}",
            len(self._symbols),
        )

    # ── main entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start feeds (if injected), boot, then drive the appropriate loop."""
        if not self._booted:
            await self.boot()

        if self._binance_feed is not None and self._csk_ws is not None:
            await self._run_ws_mode()
        else:
            log.warning("[engine] no WS feeds injected — running in REST fallback mode")
            await self._run_rest_mode()

    # ── WS mode ───────────────────────────────────────────────────────────────

    async def _run_ws_mode(self) -> None:
        """Subscribe feeds, then run tick loop + staleness watchdog concurrently."""
        inr_instruments = [f"{s}/INR" for s in self._symbols]
        await self._csk_ws.subscribe(inr_instruments)
        log.info(
            "[engine] WS mode — Binance depth + CSK public WS, tick=%.0fms",
            TICK_INTERVAL_S * 1000,
        )

        # Start both WS feeds as background tasks.
        binance_task  = asyncio.create_task(self._binance_feed.start(),  name="binance-depth-ws")
        csk_task      = asyncio.create_task(self._csk_ws.connect(),      name="csk-public-ws")
        tick_task     = asyncio.create_task(self._tick_loop(),            name="tri-tick-loop")
        watchdog_task = asyncio.create_task(self._staleness_watchdog(),   name="tri-watchdog")

        try:
            await asyncio.gather(binance_task, csk_task, tick_task, watchdog_task)
        except asyncio.CancelledError:
            for t in (binance_task, csk_task, tick_task, watchdog_task):
                t.cancel()
            raise

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL_S)
            try:
                if not self._feeds_fresh():
                    continue
                tri_books = self._assemble_books()
                if not tri_books:
                    continue
                t0 = time()
                ranked = self._ranker.rank_all(tri_books, self._executor.balances)
                latency_ms = (time() - t0) * 1000
                await self._process_tick(tri_books, ranked, latency_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[engine] tick error (cycle=%d)", self._cycle)

    async def _staleness_watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            b_age = self._binance_feed.age_s() if self._binance_feed else 0.0
            c_age = self._csk_ws.age_s()       if self._csk_ws       else 0.0
            if b_age > STALENESS_THRESHOLD_S:
                log.warning(
                    "[watchdog] Binance depth feed silent for %.0fs (threshold=%ss)",
                    b_age, STALENESS_THRESHOLD_S,
                )
            if c_age > STALENESS_THRESHOLD_S:
                log.warning(
                    "[watchdog] CSK WS silent for %.0fs (threshold=%ss)",
                    c_age, STALENESS_THRESHOLD_S,
                )

    # ── REST fallback mode ────────────────────────────────────────────────────

    async def _run_rest_mode(self) -> None:
        while True:
            try:
                t0 = time()
                tri_books  = await self._client.fetch_triangular_books(self._symbols, prefilter=True)
                latency_ms = (time() - t0) * 1000
                ranked     = self._ranker.rank_all(tri_books, self._executor.balances)
                await self._process_tick(tri_books, ranked, latency_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[engine] REST tick error (cycle=%d)", self._cycle)
            await asyncio.sleep(self._polling_interval)

    # ── shared tick processing ────────────────────────────────────────────────

    async def _process_tick(
        self,
        tri_books: dict[str, TriBook],
        ranked: dict[str, tuple[PathResult, PathResult]],
        latency_ms: float,
    ) -> None:
        exec_results: dict[str, dict] = {}

        for symbol, (net, gross) in ranked.items():
            if not net.opportunity or net.executable_qty <= 0:
                continue
            book   = tri_books.get(symbol)
            if book is None:
                continue
            result = await self._executor.execute(net, book)
            exec_results[symbol] = result

            log.warning(
                "[%s] path=%d  %s  profit=%+.4f%%  INR_Δ=%+.2f",
                symbol, net.path_id, net.logical_case_label,
                float(net.profit_pct) * 100,
                float(result["inr_variance"]),
            )
            if self._on_opportunity is not None:
                try:
                    await self._on_opportunity(symbol, net, gross, result)
                except Exception:
                    log.exception("[engine] on_opportunity raised for %s", symbol)

        if self._on_tick is not None:
            try:
                await self._on_tick(tri_books, ranked, exec_results, self._cycle, latency_ms)
            except Exception:
                log.exception("[engine] on_tick raised")

        if self._cycle % 100 == 0:
            placeable = sum(1 for net, _ in ranked.values() if net.opportunity)
            log.info(
                "[engine] cycle=%d  %.0fms  symbols=%d  placeable=%d",
                self._cycle, latency_ms, len(ranked), placeable,
            )

        self._cycle += 1

    # ── WS book assembly ──────────────────────────────────────────────────────

    def _assemble_books(self) -> dict[str, TriBook]:
        """Build TriBook snapshots from the latest WS state."""
        usdt_inr = (
            self._csk_ws.books.get("USDT/INR", Depth.empty())
            if self._csk_ws else Depth.empty()
        )
        ts = time()
        books: dict[str, TriBook] = {}

        for symbol in self._symbols:
            s_inr  = self._csk_ws.books.get(f"{symbol}/INR", Depth.empty()) if self._csk_ws       else Depth.empty()
            s_usdt = self._binance_feed.books.get(symbol, Depth.empty())     if self._binance_feed else Depth.empty()

            # Only include a symbol if at least one side has live data.
            if not s_inr.bids and not s_inr.asks and not s_usdt.bids and not s_usdt.asks:
                continue

            books[symbol] = TriBook(
                symbol=symbol,
                s_inr=s_inr,
                s_usdt=s_usdt,
                usdt_inr=usdt_inr,
                ts=ts,
            )

        return books

    def _feeds_fresh(self) -> bool:
        """True if both WS feeds have received data within STALENESS_THRESHOLD_S."""
        if self._binance_feed and self._binance_feed.age_s() > STALENESS_THRESHOLD_S:
            return False
        if self._csk_ws and self._csk_ws.age_s() > STALENESS_THRESHOLD_S:
            return False
        return True

    # ── observability ─────────────────────────────────────────────────────────

    @property
    def cycle(self) -> int:
        return self._cycle

    @property
    def balances(self) -> dict[str, Decimal]:
        return self._executor.balances
