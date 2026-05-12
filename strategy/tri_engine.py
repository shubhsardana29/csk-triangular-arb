"""TriEngine — event loop, WebSocket orchestration, staleness watchdog.

Two operating modes depending on what feeds are injected:

  WS mode (production):
    BinanceDepthFeed + CSKPublicWS injected.
    run() starts feeds as background tasks, then drives a 10Hz tick loop
    that assembles TriBook snapshots from live WS state.
    A staleness watchdog cancels all open orders and logs a warning if either
    feed goes silent for more than STALENESS_THRESHOLD_S seconds.

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
from typing import Optional

import config
from core.models import Depth, PathResult, TriBook, TwoLegResult
from strategy.tri_ranker import TriRanker
from strategy.shadow_executor import ShadowExecutor

# feeds/ types are injected at runtime from main.py/dashboard.py.
# String annotations keep strategy/ importable without feeds/ installed.

log = logging.getLogger(__name__)

TICK_INTERVAL_S       = 0.10   # 10 Hz
STALENESS_THRESHOLD_S = 15.0
WATCHDOG_INTERVAL_S   = 5.0
WS_STARTUP_GRACE_S    = 2.0

OpportunityCallback = Callable[
    [str, PathResult, PathResult, dict], Awaitable[None]
]  # symbol, net, gross, exec_result

TickCallback = Callable[
    [dict[str, TriBook], dict[str, tuple[PathResult, PathResult]], dict[str, dict],
     dict[str, "TwoLegResult"], int, float],
    Awaitable[None],
]  # tri_books, ranked_3leg, exec_results, ranked_2leg, cycle, latency_ms


class TriEngine:
    """Drives the rank → execute loop at 10Hz (WS) or 1.5s (REST fallback)."""

    def __init__(
        self,
        *,
        client,                                         # CoinSwitchClient (duck-typed)
        ranker: TriRanker,
        executor,                                       # ShadowExecutor | TriExecutor
        symbols: list[str],
        binance_feed=None,   # BinanceDepthFeed | None  (injected from main.py)
        csk_ws=None,         # CSKPublicWS | None       (injected from main.py)
        two_leg_ranker=None,  # TwoLegRanker | None
        two_leg_executor=None,  # TwoLegExecutor | None
        rebalancer=None,     # TriRebalancer | None
        polling_interval: Optional[float] = None,
        on_opportunity: Optional[OpportunityCallback] = None,
        on_tick: Optional[TickCallback] = None,
    ):
        self._client             = client
        self._ranker             = ranker
        self._executor           = executor
        self._symbols            = [s.upper() for s in symbols]
        self._binance_feed       = binance_feed
        self._csk_ws             = csk_ws
        self._two_leg_ranker     = two_leg_ranker
        self._two_leg_executor   = two_leg_executor
        self._rebalancer         = rebalancer
        self._polling_interval   = polling_interval if polling_interval is not None else config.POLLING_INTERVAL
        self._on_opportunity     = on_opportunity
        self._on_tick            = on_tick
        self._cycle              = 0
        self._booted             = False
        # Position lock: symbols currently in-flight (3-leg or 2-leg).
        # Both executors call _on_settle(symbol) when done.
        self._active_symbols: set[str] = set()

    # ── settle callback (shared by both executors) ────────────────────────────

    def _on_settle(self, symbol: str) -> None:
        self._active_symbols.discard(symbol)
        log.info("[engine] position released: %s  active=%d", symbol, len(self._active_symbols))

    # ── boot ─────────────────────────────────────────────────────────────────

    async def boot(self) -> None:
        """Use REST to fetch initial prices and warm-start the executor.

        Called once regardless of WS/REST mode. The tick loop starts
        immediately after; WS data replaces REST data as it arrives.
        """
        if self._booted:
            return
        log.info("[engine] booting — fetching initial books for %d symbols", len(self._symbols))

        # Cancel any open orders left from a previous run (defensive).
        await self._cancel_all_exchange_orders()

        tri_books = await self._client.fetch_triangular_books(self._symbols, prefilter=True)

        # Fetch actual per-symbol trading fees and propagate to all components.
        try:
            fee_map = await self._client.get_trading_fees(exchange="coinswitchx")
            if fee_map:
                self._ranker.update_fees(fee_map)
                if self._two_leg_ranker is not None:
                    self._two_leg_ranker.update_fees(fee_map)
                if hasattr(self._executor, "update_fees"):
                    self._executor.update_fees(fee_map)
                if self._two_leg_executor is not None and hasattr(self._two_leg_executor, "update_fees"):
                    self._two_leg_executor.update_fees(fee_map)
                sample = next(iter(fee_map.values()))
                log.info("[engine] actual taker fee loaded: %s (sample)", sample)
        except Exception:
            log.warning("[engine] could not fetch trading fees — using config default %s", config.TAKER_FEE)

        # ShadowExecutor: build a simulated portfolio from config.
        # TriExecutor: start() fetches real balances from the exchange.
        if not self._executor.balances:
            self._executor.balances = config.build_initial_shadow_balances(self._symbols, tri_books)

        await self._executor.start()
        if self._two_leg_executor is not None:
            await self._two_leg_executor.start()

        self._booted = True
        log.warning(
            "[engine] boot done — shadow portfolio ₹%s  symbols=%d",
            f"{float(config.SHADOW_PORTFOLIO_TOTAL_INR):,.0f}",
            len(self._symbols),
        )

    async def _cancel_all_exchange_orders(self) -> None:
        """Cancel all open orders on the exchange at boot (defensive cleanup)."""
        try:
            open_orders = await self._client.list_open_orders()
            if not open_orders:
                return
            log.warning("[engine] boot: cancelling %d open orders from previous run", len(open_orders))
            for order in open_orders:
                oid = order.get("order_id") or order.get("orderId") or order.get("id")
                if oid:
                    try:
                        await self._client.cancel_order(str(oid))
                    except Exception:
                        log.exception("[engine] boot cancel failed for order %s", oid)
        except Exception:
            log.exception("[engine] boot: failed to fetch/cancel open orders")

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

        binance_task  = asyncio.create_task(self._binance_feed.start(),  name="binance-depth-ws")
        csk_task      = asyncio.create_task(self._csk_ws.connect(),      name="csk-public-ws")
        tick_task     = asyncio.create_task(self._tick_loop(),            name="tri-tick-loop")
        watchdog_task = asyncio.create_task(self._staleness_watchdog(),   name="tri-watchdog")

        await asyncio.sleep(WS_STARTUP_GRACE_S)
        if csk_task.done():
            log.warning("[engine] CSK WS ended during startup; switching to REST fallback mode")
            for t in (binance_task, tick_task, watchdog_task):
                t.cancel()
            await asyncio.gather(binance_task, tick_task, watchdog_task, return_exceptions=True)
            await self._run_rest_mode()
            return

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
                ranked    = self._ranker.rank_all(tri_books, self._executor.balances)
                ranked_2l = (
                    self._two_leg_ranker.rank_all(tri_books, self._executor.balances)
                    if self._two_leg_ranker else {}
                )
                latency_ms = (time() - t0) * 1000

                # Reprice any open 2-leg Leg 2 orders.
                if self._two_leg_executor is not None:
                    await self._two_leg_executor.reprice_tick(tri_books)

                # Rebalancer tick.
                if self._rebalancer is not None:
                    await self._rebalancer.on_tick(tri_books, self._executor.balances)

                await self._process_tick(tri_books, ranked, ranked_2l, latency_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[engine] tick error (cycle=%d)", self._cycle)

    async def _staleness_watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            b_age = self._binance_feed.age_s() if self._binance_feed else 0.0
            c_age = self._csk_ws.age_s()       if self._csk_ws       else 0.0
            stale = b_age > STALENESS_THRESHOLD_S or c_age > STALENESS_THRESHOLD_S

            if b_age > STALENESS_THRESHOLD_S:
                log.warning(
                    "[watchdog] Binance depth feed silent for %.0fs — cancelling open orders",
                    b_age,
                )
            if c_age > STALENESS_THRESHOLD_S:
                log.warning(
                    "[watchdog] CSK WS silent for %.0fs — cancelling open orders",
                    c_age,
                )

            if stale:
                await self._cancel_all_active_orders()

    async def _cancel_all_active_orders(self) -> None:
        """Cancel every in-flight order across both executors and reset position locks."""
        all_ids: list[str] = list(self._executor.active_order_ids)
        if self._two_leg_executor is not None:
            all_ids.extend(self._two_leg_executor.active_order_ids)

        if not all_ids:
            return

        log.warning("[engine] staleness: cancelling %d active orders", len(all_ids))
        for oid in all_ids:
            try:
                await self._client.cancel_order(oid)
            except Exception:
                log.exception("[engine] cancel_order failed for %s", oid)

        # Clear position locks so the engine can re-enter on next fresh tick.
        self._active_symbols.clear()

    # ── REST fallback mode ────────────────────────────────────────────────────

    async def _run_rest_mode(self) -> None:
        while True:
            try:
                t0 = time()
                tri_books  = await self._client.fetch_triangular_books(self._symbols, prefilter=True)
                latency_ms = (time() - t0) * 1000
                ranked     = self._ranker.rank_all(tri_books, self._executor.balances)
                ranked_2l  = (
                    self._two_leg_ranker.rank_all(tri_books, self._executor.balances)
                    if self._two_leg_ranker else {}
                )
                await self._process_tick(tri_books, ranked, ranked_2l, latency_ms)
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
        ranked_2l: dict[str, "TwoLegResult"],
        latency_ms: float,
    ) -> None:
        exec_results: dict[str, dict] = {}

        # ── 3-leg opportunities ───────────────────────────────────────────────
        for symbol, (net, gross) in ranked.items():
            if not config.THREE_LEG_ENABLED:
                break
            if not net.opportunity or net.executable_qty <= 0:
                continue
            if symbol in self._active_symbols:
                continue   # position already open
            book = tri_books.get(symbol)
            if book is None:
                continue

            self._active_symbols.add(symbol)
            result = await self._executor.execute(net, book)
            exec_results[symbol] = result

            log.warning(
                "[%s] 3-leg  path=%d  %s  profit=%+.4f%%  INR_Δ=%+.2f",
                symbol, net.path_id, net.logical_case_label,
                float(net.profit_pct) * 100,
                float(result.get("inr_variance", 0)),
            )
            if self._on_opportunity is not None:
                try:
                    await self._on_opportunity(symbol, net, gross, result)
                except Exception:
                    log.exception("[engine] on_opportunity raised for %s", symbol)

        # ── 2-leg opportunities ───────────────────────────────────────────────
        if self._two_leg_executor is not None and config.TWO_LEG_ENABLED:
            for symbol, two_result in ranked_2l.items():
                if not two_result.opportunity or two_result.executable_qty <= 0:
                    continue
                if symbol in self._active_symbols:
                    continue   # 3-leg already entered this symbol this tick
                book = tri_books.get(symbol)
                if book is None:
                    continue

                self._active_symbols.add(symbol)
                result = await self._two_leg_executor.execute(two_result, book)
                exec_results[symbol] = exec_results.get(symbol) or result

                log.warning(
                    "[%s] 2-leg  %s  spread=%+.4f%%  profit=%+.4f%%",
                    symbol, two_result.direction,
                    float(two_result.spread_pct) * 100,
                    float(two_result.profit_pct) * 100,
                )
                if self._on_opportunity is not None:
                    try:
                        await self._on_opportunity(symbol, two_result, two_result, result)
                    except Exception:
                        log.exception("[engine] on_opportunity raised for 2-leg %s", symbol)

        # ── on_tick callback ─────────────────────────────────────────────────
        if self._on_tick is not None:
            try:
                await self._on_tick(tri_books, ranked, exec_results, ranked_2l, self._cycle, latency_ms)
            except Exception:
                log.exception("[engine] on_tick raised")

        if self._cycle % 100 == 0:
            placeable_3 = sum(1 for net, _ in ranked.values() if net.opportunity)
            placeable_2 = sum(1 for r in ranked_2l.values() if r.opportunity)
            log.info(
                "[engine] cycle=%d  %.0fms  symbols=%d  3leg=%d  2leg=%d  active=%d",
                self._cycle, latency_ms, len(ranked),
                placeable_3, placeable_2, len(self._active_symbols),
            )
            # Show top-5 2-leg spreads so the user can see how close the market is.
            if ranked_2l:
                top5 = sorted(
                    (r for r in ranked_2l.values() if r.direction),
                    key=lambda r: r.profit_pct, reverse=True,
                )[:5]
                for r in top5:
                    book = tri_books.get(r.symbol)
                    csk_price = float(book.s_inr.mid) if book else 0.0
                    bnb_price = float(book.s_usdt.mid * book.usdt_inr.mid) if book else 0.0
                    log.info(
                        "[2leg-best] %-10s  %-13s  spread=%+.3f%%  profit=%+.3f%%  "
                        "threshold=%.3f%%  csk=₹%.4f  bnb=₹%.4f",
                        r.symbol, r.direction,
                        float(r.spread_pct) * 100,
                        float(r.profit_pct) * 100,
                        float(config.TWO_LEG_MIN_SPREAD_PCT) * 100,
                        csk_price, bnb_price,
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

            if not (s_inr.bids and s_inr.asks
                    and s_usdt.bids and s_usdt.asks
                    and usdt_inr.bids and usdt_inr.asks):
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
