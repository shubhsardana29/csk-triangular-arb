"""TwoLegExecutor — 2-leg spread trade execution with cost-floor repricing.

Sequential flow:
  1. execute() places Leg 1 (BUY on cheap venue).
  2. OrderPoller fires on_fill/_on_done → records buy_avg, computes cost floor.
  3. Leg 2 (SELL on expensive venue) is placed at max(cost_floor, market_bid).
  4. reprice_tick() is called every engine tick to cancel-replace Leg 2 if
     the market has moved enough to warrant a better price.
  5. On Leg 2 fill, settle and refresh balances.

Cost floor (never breached):
  INR_CHEAP  (bought on INR, selling on USDT C2C):
    floor_usdt = buy_avg_inr / usdt_inr_mid * (1 + fee + tds + safety)
  INR_EXPENSIVE (bought on USDT C2C, selling on INR):
    floor_inr  = buy_avg_usdt * usdt_inr_mid * (1 + fee + tds + safety)

Interface is duck-type compatible with ShadowExecutor / TriExecutor:
    async execute(result, book) -> dict
    async start() -> None
    balances: dict[str, Decimal]
    active_order_ids: list[str]
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from time import time
from typing import Optional

import config
from core.models import Depth, TriBook, TwoLegIntent, TwoLegResult
from strategy.order_poller import OrderPoller

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE  = Decimal(1)

LEG_TIMEOUT_S = 30.0


@dataclass
class _State:
    intent:         TwoLegIntent
    book:           TriBook          # snapshot at detection — updated on reprice
    oid_to_leg:     dict[str, int] = field(default_factory=dict)
    timeout_handle: Optional[asyncio.TimerHandle] = None
    stuck_logged_at: float = 0.0     # last time we logged "stuck at floor"


class TwoLegExecutor:
    """Places real 2-leg limit orders with Leg 2 repricing at the cost floor."""

    def __init__(
        self,
        client,
        fee: Decimal,
        tds: Decimal,
        on_settle: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._client    = client
        self.fee        = Decimal(str(fee))
        self.tds        = Decimal(str(tds))
        self.balances: dict[str, Decimal] = {}
        self._on_settle = on_settle

        self._poller = OrderPoller(
            client=client,
            on_fill=self._on_fill,
            on_done=self._on_done,
        )
        self._states: dict[str, _State] = {}  # symbol → execution state

    @property
    def active_order_ids(self) -> list[str]:
        ids = []
        for state in self._states.values():
            ids.extend(state.oid_to_leg.keys())
        return ids

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        asyncio.create_task(self._poller.start(), name="two-leg-order-poller")
        await self.refresh_balances()
        log.info("[2leg-executor] started")

    async def refresh_balances(self) -> None:
        try:
            self.balances = await self._client.get_balances()
        except Exception:
            log.exception("[2leg-executor] failed to refresh balances")

    # ── main entry point ──────────────────────────────────────────────────────

    async def execute(self, result: TwoLegResult, book: TriBook) -> dict:
        symbol = result.symbol
        if symbol in self._states:
            return {}

        intent = TwoLegIntent(symbol=symbol, result=result, placed_at=time())
        state  = _State(intent=intent, book=book)
        self._states[symbol] = state

        await self._place_leg1(state)
        return {"status": "placed", "symbol": symbol}

    # ── leg placement ─────────────────────────────────────────────────────────

    async def _place_leg1(self, state: _State) -> None:
        result = state.intent.result
        symbol = result.symbol
        price  = result.buy_price
        qty    = result.executable_qty

        if price <= _ZERO or qty <= _ZERO:
            log.error("[2leg %s] Leg 1 bad price/qty", symbol)
            self._settle(symbol, success=False)
            return

        log.info("[2leg %s] placing Leg 1  %s  price=%.4f  qty=%.6f",
                 symbol, result.buy_venue, float(price), float(qty))

        oid = await self._place_order(result.buy_venue, symbol, "BUY", price, qty)
        if not oid:
            self._settle(symbol, success=False)
            return

        state.intent.leg1_oid = oid
        state.oid_to_leg[oid] = 1
        self._poller.track(oid, {"symbol": symbol, "leg": 1})
        self._arm_timeout(state, oid, 1)

    async def _place_leg2(self, state: _State) -> None:
        """Place Leg 2 SELL at max(cost_floor, market_bid)."""
        result = state.intent.result
        symbol = result.symbol
        qty    = state.intent.leg1_filled_qty

        if qty <= _ZERO:
            log.warning("[2leg %s] Leg 2: no filled qty from Leg 1", symbol)
            self._settle(symbol, success=False)
            return

        floor = state.intent.cost_floor
        market_price = self._sell_market_price(result.sell_venue, state.book)
        target_price = max(floor, market_price) if market_price > _ZERO else floor

        if target_price <= _ZERO:
            log.error("[2leg %s] Leg 2: cannot determine sell price", symbol)
            self._settle(symbol, success=False)
            return

        log.info("[2leg %s] placing Leg 2  %s  price=%.4f (floor=%.4f)  qty=%.6f",
                 symbol, result.sell_venue, float(target_price), float(floor), float(qty))

        oid = await self._place_order(result.sell_venue, symbol, "SELL", target_price, qty)
        if not oid:
            self._settle(symbol, success=False)
            return

        # Cancel any old Leg 2 oid before registering the new one.
        old_oid = state.intent.leg2_oid
        if old_oid and old_oid in state.oid_to_leg:
            del state.oid_to_leg[old_oid]

        state.intent.leg2_oid = oid
        state.oid_to_leg[oid] = 2
        self._poller.track(oid, {"symbol": symbol, "leg": 2})
        self._arm_timeout(state, oid, 2)

    # ── reprice on tick ───────────────────────────────────────────────────────

    async def reprice_tick(self, tri_books: dict[str, TriBook]) -> None:
        """Called every engine tick. Cancel-replace Leg 2 if market has moved."""
        for symbol, state in list(self._states.items()):
            if state.intent.leg2_oid is None:
                continue  # still on Leg 1

            book = tri_books.get(symbol)
            if book is None:
                continue
            state.book = book  # keep book snapshot fresh for leg 2 pricing

            result       = state.intent.result
            floor        = state.intent.cost_floor
            market_price = self._sell_market_price(result.sell_venue, book)

            if market_price <= _ZERO:
                continue

            target_price = max(floor, market_price)

            # Log if stuck at floor.
            if market_price < floor:
                now = time()
                if now - state.stuck_logged_at > config.STUCK_ALERT_AFTER_S:
                    log.warning(
                        "[2leg %s] SELL resting at cost floor=%.4f  market_bid=%.4f",
                        symbol, float(floor), float(market_price),
                    )
                    state.stuck_logged_at = now
                continue  # don't reprice — wait for market to come back up

            # Check if the price has moved enough to warrant a cancel-replace.
            old_oid = state.intent.leg2_oid
            # Fetch current resting price from poller meta (approximate via last known).
            # We compare against the floor as proxy; if market moved >threshold above
            # the floor, it's worth repricing.
            threshold = target_price * config.REPRICE_THRESHOLD_PCT
            # Simple heuristic: reprice if market_price changed >threshold vs floor.
            if market_price - floor > threshold:
                log.debug("[2leg %s] repricing Leg 2: market=%.4f floor=%.4f", symbol, float(market_price), float(floor))
                self._poller.untrack(old_oid)
                try:
                    await self._client.cancel_order(old_oid)
                except Exception:
                    log.exception("[2leg %s] cancel Leg 2 failed", symbol)
                state.intent.leg2_oid = None
                del state.oid_to_leg[old_oid]
                await self._place_leg2(state)

    # ── poller callbacks ──────────────────────────────────────────────────────

    async def _on_fill(self, oid: str, delta_qty: Decimal, avg_price: Decimal) -> None:
        state = self._state_for_oid(oid)
        if state is None:
            return

        leg_num = state.oid_to_leg.get(oid, 0)
        symbol  = state.intent.symbol

        if leg_num == 1:
            state.intent.leg1_filled_qty += delta_qty
            # Update rolling buy average.
            prev_filled = state.intent.leg1_filled_qty - delta_qty
            prev_avg    = state.intent.buy_avg_price
            total       = state.intent.leg1_filled_qty
            state.intent.buy_avg_price = (prev_filled * prev_avg + delta_qty * avg_price) / total if total > _ZERO else avg_price
        elif leg_num == 2:
            state.intent.leg2_filled_qty += delta_qty

        log.info("[2leg %s] Leg %d fill  Δqty=%.6f  avg=%.4f", symbol, leg_num, float(delta_qty), float(avg_price))

    async def _on_done(self, oid: str, status: str) -> None:
        state = self._state_for_oid(oid)
        if state is None:
            return

        symbol  = state.intent.symbol
        leg_num = state.oid_to_leg.pop(oid, 0)
        if leg_num == 0:
            return

        self._cancel_timeout(state)

        if leg_num == 1:
            if status == "FULFILLED" or state.intent.leg1_filled_qty > _ZERO:
                self._compute_cost_floor(state)
                await self._place_leg2(state)
            else:
                log.warning("[2leg %s] Leg 1 ended with %s and no fill", symbol, status)
                self._settle(symbol, success=False)

        elif leg_num == 2:
            if status == "FULFILLED" or state.intent.leg2_filled_qty > _ZERO:
                self._settle(symbol, success=True)
            else:
                log.warning("[2leg %s] Leg 2 ended with %s and no fill", symbol, status)
                self._settle(symbol, success=False)

    # ── cost floor ────────────────────────────────────────────────────────────

    def _compute_cost_floor(self, state: _State) -> None:
        """Compute and store the minimum acceptable Leg 2 sell price."""
        result   = state.intent.result
        buy_avg  = state.intent.buy_avg_price
        # Total cost multiplier: fee on buy + TDS on sell + fee on sell + safety
        safety   = Decimal(str(config.MIN_PROFIT_PCT))
        floor_multiplier = _ONE + self.fee + self.tds + safety

        if result.direction == "INR_CHEAP":
            # Bought on INR, selling on USDT C2C. Floor is in USDT.
            usdt_inr = state.book.usdt_inr.mid
            if usdt_inr > _ZERO:
                state.intent.cost_floor = (buy_avg / usdt_inr) * floor_multiplier
            else:
                state.intent.cost_floor = buy_avg * floor_multiplier
        else:
            # INR_EXPENSIVE: bought on USDT C2C, selling on INR. Floor is in INR.
            usdt_inr = state.book.usdt_inr.mid
            state.intent.cost_floor = buy_avg * usdt_inr * floor_multiplier

        log.info("[2leg %s] cost floor set: %.4f  buy_avg=%.4f",
                 result.symbol, float(state.intent.cost_floor), float(buy_avg))

    # ── timeout ───────────────────────────────────────────────────────────────

    def _arm_timeout(self, state: _State, oid: str, leg_num: int) -> None:
        self._cancel_timeout(state)
        loop = asyncio.get_event_loop()
        state.timeout_handle = loop.call_later(
            LEG_TIMEOUT_S,
            lambda: asyncio.ensure_future(self._on_timeout(state.intent.symbol, oid, leg_num)),
        )

    def _cancel_timeout(self, state: _State) -> None:
        if state.timeout_handle:
            state.timeout_handle.cancel()
            state.timeout_handle = None

    async def _on_timeout(self, symbol: str, oid: str, leg_num: int) -> None:
        state = self._states.get(symbol)
        if state is None or oid not in state.oid_to_leg:
            return

        log.warning("[2leg %s] Leg %d timeout — cancelling %s", symbol, leg_num, oid)
        self._poller.untrack(oid)
        try:
            await self._client.cancel_order(oid)
        except Exception:
            log.exception("[2leg %s] cancel on timeout failed", symbol)

        if leg_num == 1 and state.intent.leg1_filled_qty > _ZERO:
            # Partial Leg 1 fill — still try to sell what we got.
            self._compute_cost_floor(state)
            await self._place_leg2(state)
        else:
            self._settle(symbol, success=False)

    # ── settlement ────────────────────────────────────────────────────────────

    def _settle(self, symbol: str, *, success: bool) -> None:
        state = self._states.pop(symbol, None)
        if state is None:
            return
        self._cancel_timeout(state)

        intent = state.intent
        if success:
            log.warning(
                "[2leg %s] settled  dir=%s  leg1=%.6f  leg2=%.6f",
                symbol, intent.result.direction,
                float(intent.leg1_filled_qty), float(intent.leg2_filled_qty),
            )
        else:
            log.warning(
                "[2leg %s] aborted  dir=%s  leg1=%.6f  leg2=%.6f",
                symbol, intent.result.direction,
                float(intent.leg1_filled_qty), float(intent.leg2_filled_qty),
            )

        asyncio.ensure_future(self.refresh_balances())
        if self._on_settle is not None:
            try:
                self._on_settle(symbol)
            except Exception:
                log.exception("[2leg-executor] on_settle raised for %s", symbol)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _state_for_oid(self, oid: str) -> Optional[_State]:
        for state in self._states.values():
            if oid in state.oid_to_leg:
                return state
        return None

    def _sell_market_price(self, sell_venue: str, book: TriBook) -> Decimal:
        if sell_venue == "spot_inr":
            return book.s_inr.bid
        if sell_venue == "spot_usdt":
            return book.s_usdt.bid
        return _ZERO

    async def _place_order(
        self, venue: str, symbol: str, side: str, price: Decimal, qty: Decimal
    ) -> Optional[str]:
        if venue == "spot_inr":
            return await self._client.place_spot_order(symbol, side, price, qty)
        if venue == "spot_usdt":
            return await self._client.place_usdt_order(symbol, side, price, qty)
        log.error("[2leg] unknown venue: %s", venue)
        return None
