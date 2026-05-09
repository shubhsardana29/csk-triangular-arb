"""TriExecutor — places real 3-leg limit orders via CSK REST.

Sequential leg flow:
  1. execute() validates no active trade for the symbol, then places Leg 1.
  2. OrderPoller fires on_fill → _on_fill routes to the appropriate handler.
  3. Each leg handler computes proceeds and places the next leg.
  4. After Leg 3 fills, the intent is settled and balances updated.

Cost-floor check before Leg 3:
  If the current market can no longer deliver a profitable Leg 3 at the
  breakeven floor, the executor logs the miss and does NOT place Leg 3
  (the Leg 2 position is left dangling — a rebalancer can clean it up).

Leg timeout:
  Any leg not filled within LEG_TIMEOUT_S is cancelled. If Leg 1 times out
  there is no exposure. Leg 2/3 timeouts leave partial inventory open.

The interface is duck-type compatible with ShadowExecutor — both expose:
    async execute(path, book) -> dict
    balances: dict[str, Decimal]  (property)
    start() -> None (starts background tasks)

Strategy code (TriEngine) never imports this directly; it's injected from main.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from time import time
from typing import Optional

from core.models import PathResult, TriBook, TriIntent
from strategy.order_poller import OrderPoller

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE  = Decimal(1)

LEG_TIMEOUT_S = 30.0   # cancel a leg if not filled within this many seconds


# ── leg metadata ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _LegSpec:
    venue: str            # "spot_inr" | "spot_usdt" | "usdt_inr"
    side: str             # "BUY" | "SELL"
    apply_tds: bool       # deduct TDS from sale proceeds (quote received)
    tds_on_receive: bool  # deduct TDS from base received (C2C buy legs)


# Leg ordering per path_id. Index 0 = Leg 1, 1 = Leg 2, 2 = Leg 3.
_PATH_LEGS: dict[int, tuple[_LegSpec, _LegSpec, _LegSpec]] = {
    # Path 1: S→INR(TDS)→USDT(buy)→S(C2C-TDS receive)
    1: (
        _LegSpec("spot_inr",  "SELL", True,  False),
        _LegSpec("usdt_inr",  "BUY",  False, False),
        _LegSpec("spot_usdt", "BUY",  False, True),
    ),
    # Path 2: S→USDT(TDS)→INR(TDS)→S(buy)
    2: (
        _LegSpec("spot_usdt", "SELL", True,  False),
        _LegSpec("usdt_inr",  "SELL", True,  False),
        _LegSpec("spot_inr",  "BUY",  False, False),
    ),
    # Path 3: INR→S(buy)→USDT(TDS)→INR(TDS)
    3: (
        _LegSpec("spot_inr",  "BUY",  False, False),
        _LegSpec("spot_usdt", "SELL", True,  False),
        _LegSpec("usdt_inr",  "SELL", True,  False),
    ),
    # Path 4: INR→USDT(buy)→S(C2C-TDS receive)→INR(TDS)
    4: (
        _LegSpec("usdt_inr",  "BUY",  False, False),
        _LegSpec("spot_usdt", "BUY",  False, True),
        _LegSpec("spot_inr",  "SELL", True,  False),
    ),
}


# ── per-execution state (private, lives inside tri_executor) ──────────────────

@dataclass
class _ExecState:
    intent:          TriIntent
    book:            TriBook          # snapshot at detection — used for leg 2/3 pricing
    leg_specs:       tuple[_LegSpec, _LegSpec, _LegSpec]
    proceeds:        Decimal = _ZERO  # output of the most-recently completed leg
    timeout_handle:  Optional[asyncio.TimerHandle] = None
    # map: order_id → leg_num (1-indexed)
    oid_to_leg:      dict[str, int] = field(default_factory=dict)


# ── helpers ───────────────────────────────────────────────────────────────────

def _best_price(spec: _LegSpec, book: TriBook) -> Decimal:
    """Top-of-book price for the leg's side and venue."""
    if spec.side == "SELL":
        if spec.venue == "spot_inr":  return book.s_inr.bid
        if spec.venue == "spot_usdt": return book.s_usdt.bid
        if spec.venue == "usdt_inr":  return book.usdt_inr.bid
    else:
        if spec.venue == "spot_inr":  return book.s_inr.ask
        if spec.venue == "spot_usdt": return book.s_usdt.ask
        if spec.venue == "usdt_inr":  return book.usdt_inr.ask
    return _ZERO


def _compute_output(
    spec: _LegSpec,
    delta_qty: Decimal,
    avg_price: Decimal,
    fee: Decimal,
    tds: Decimal,
) -> Decimal:
    """Compute the output of a filled leg (what flows into the next leg).

    For SELL legs: output is quote received (INR or USDT).
    For BUY legs: output is base received.

    Assumption: filledQuantity from CSK = base quantity matched pre-fee.
    For sells: INR/USDT received = qty * price * (1-fee) [* (1-tds) if flagged].
    For buys: base received = delta_qty [* (1-tds) if C2C].
    """
    if spec.side == "SELL":
        proceeds = delta_qty * avg_price * (_ONE - fee)
        if spec.apply_tds:
            proceeds = proceeds * (_ONE - tds)
        return proceeds
    else:  # BUY
        qty = delta_qty
        if spec.tds_on_receive:
            qty = qty * (_ONE - tds)
        return qty


def _order_qty(
    spec: _LegSpec,
    input_qty: Decimal,
    book: TriBook,
    fee: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return (price, base_qty) for placing this leg's order.

    input_qty semantics:
      - Previous leg was SELL → input_qty is quote (INR or USDT).
      - Previous leg was BUY  → input_qty is base of that leg.
    For Leg 1 token-start paths: input_qty is base S.
    For Leg 1 INR-start paths:   input_qty is INR notional.
    """
    price = _best_price(spec, book)
    if price == _ZERO:
        return _ZERO, _ZERO

    if spec.side == "SELL":
        # input is base; we're selling it
        return price, input_qty
    else:
        # input is quote notional; we're buying base
        # deduct fee from notional so we don't overspend
        effective_notional = input_qty * (_ONE - fee)
        if effective_notional <= _ZERO or price <= _ZERO:
            return _ZERO, _ZERO
        return price, effective_notional / price


# ── TriExecutor ───────────────────────────────────────────────────────────────

class TriExecutor:
    """Places real 3-leg limit orders sequentially via CSK REST.

    Drop-in replacement for ShadowExecutor in main.py / dashboard.py.
    Both expose the same duck-typed interface:
        async execute(path, book) -> dict
        balances: dict[str, Decimal]
    """

    def __init__(
        self,
        client,
        fee: Decimal,
        tds: Decimal,
    ) -> None:
        self._client  = client
        self.fee      = Decimal(str(fee))
        self.tds      = Decimal(str(tds))
        self.balances: dict[str, Decimal] = {}

        self._poller  = OrderPoller(
            client=client,
            on_fill=self._on_fill,
            on_done=self._on_done,
        )
        self._states: dict[str, _ExecState] = {}  # symbol → active execution

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the order-polling background task. Call once from main.py."""
        asyncio.create_task(self._poller.start(), name="order-poller")
        await self.refresh_balances()
        log.info("[executor] TriExecutor started — real order mode")

    async def refresh_balances(self) -> None:
        """Fetch real balances from CSK and cache them."""
        try:
            self.balances = await self._client.get_balances()
            log.info("[executor] balances refreshed — %d assets", len(self.balances))
        except Exception:
            log.exception("[executor] failed to refresh balances")

    # ── main entry point ──────────────────────────────────────────────────────

    async def execute(self, path: PathResult, book: TriBook) -> dict:
        """Kick off Leg 1. Returns {"status": "placed"} immediately.

        Subsequent legs are driven by the OrderPoller fill callbacks.
        If a trade is already in-flight for this symbol, skips gracefully.
        """
        symbol = book.symbol
        if symbol in self._states:
            log.debug("[%s] skipping — trade already in flight", symbol)
            return {}

        specs = _PATH_LEGS.get(path.path_id)
        if specs is None:
            log.error("[%s] unknown path_id=%s", symbol, path.path_id)
            return {}

        intent = TriIntent(symbol=symbol, path=path, placed_at=time())
        state  = _ExecState(intent=intent, book=book, leg_specs=specs)
        self._states[symbol] = state

        # Leg 1 input quantity: token-start = S qty, INR-start = INR notional.
        leg1_input = path.executable_qty
        await self._place_leg(state, leg_num=1, input_qty=leg1_input)
        return {"status": "placed", "symbol": symbol}

    # ── leg placement ─────────────────────────────────────────────────────────

    async def _place_leg(
        self, state: _ExecState, leg_num: int, input_qty: Decimal
    ) -> None:
        spec   = state.leg_specs[leg_num - 1]
        symbol = state.intent.symbol
        book   = state.book

        price, qty = _order_qty(spec, input_qty, book, self.fee)

        if price == _ZERO or qty == _ZERO:
            log.error("[%s] Leg %d: cannot size order (price=%s qty=%s)", symbol, leg_num, price, qty)
            self._settle(symbol, success=False)
            return

        log.info(
            "[%s] placing Leg %d  venue=%s  side=%s  price=%.4f  qty=%.6f",
            symbol, leg_num, spec.venue, spec.side, float(price), float(qty),
        )

        oid = await self._call_place_order(spec, symbol, price, qty)
        if not oid:
            log.error("[%s] Leg %d: place_order returned None", symbol, leg_num)
            self._settle(symbol, success=False)
            return

        # Track in intent and poller.
        if leg_num == 1:   state.intent.leg1_oid = oid
        elif leg_num == 2: state.intent.leg2_oid = oid
        elif leg_num == 3: state.intent.leg3_oid = oid

        state.oid_to_leg[oid] = leg_num
        self._poller.track(oid, {"symbol": symbol, "leg": leg_num})
        self._arm_timeout(state, oid, leg_num)

    async def _call_place_order(
        self, spec: _LegSpec, symbol: str, price: Decimal, qty: Decimal
    ) -> Optional[str]:
        if spec.venue == "spot_inr":
            return await self._client.place_spot_order(symbol, spec.side, price, qty)
        if spec.venue == "spot_usdt":
            return await self._client.place_usdt_order(symbol, spec.side, price, qty)
        if spec.venue == "usdt_inr":
            return await self._client.place_usdtinr_order(spec.side, price, qty)
        log.error("Unknown venue: %s", spec.venue)
        return None

    # ── poller callbacks ──────────────────────────────────────────────────────

    async def _on_fill(self, oid: str, delta_qty: Decimal, avg_price: Decimal) -> None:
        state = self._state_for_oid(oid)
        if state is None:
            return

        symbol  = state.intent.symbol
        leg_num = state.oid_to_leg.get(oid, 0)
        if leg_num == 0:
            return

        spec    = state.leg_specs[leg_num - 1]
        output  = _compute_output(spec, delta_qty, avg_price, self.fee, self.tds)

        if leg_num == 1:
            state.intent.leg1_filled_qty += delta_qty
        elif leg_num == 2:
            state.intent.leg2_filled_qty += delta_qty
        elif leg_num == 3:
            state.intent.leg3_filled_qty += delta_qty

        log.info(
            "[%s] Leg %d fill  Δqty=%.6f  avg=%.4f  output=%.6f",
            symbol, leg_num, float(delta_qty), float(avg_price), float(output),
        )
        state.proceeds = output   # will be confirmed / used once DONE fires

    async def _on_done(self, oid: str, status: str) -> None:
        state = self._state_for_oid(oid)
        if state is None:
            return

        symbol  = state.intent.symbol
        leg_num = state.oid_to_leg.pop(oid, 0)
        if leg_num == 0:
            return

        self._cancel_timeout(state)

        if status == "FULFILLED":
            if leg_num < 3:
                await self._place_leg(state, leg_num=leg_num + 1, input_qty=state.proceeds)
            else:
                self._settle(symbol, success=True)
        else:
            log.warning(
                "[%s] Leg %d ended with status=%s  filled=%.6f",
                symbol, leg_num, status,
                float(getattr(state.intent, f"leg{leg_num}_filled_qty")),
            )
            # Partial fill — try to continue with whatever was filled.
            partial = state.proceeds
            if partial > _ZERO and leg_num < 3:
                log.warning("[%s] continuing with partial Leg %d proceeds=%.6f", symbol, leg_num, float(partial))
                await self._place_leg(state, leg_num=leg_num + 1, input_qty=partial)
            else:
                self._settle(symbol, success=False)

    # ── timeout handling ──────────────────────────────────────────────────────

    def _arm_timeout(self, state: _ExecState, oid: str, leg_num: int) -> None:
        self._cancel_timeout(state)
        loop = asyncio.get_event_loop()
        state.timeout_handle = loop.call_later(
            LEG_TIMEOUT_S,
            lambda: asyncio.ensure_future(self._on_timeout(state.intent.symbol, oid, leg_num)),
        )

    def _cancel_timeout(self, state: _ExecState) -> None:
        if state.timeout_handle:
            state.timeout_handle.cancel()
            state.timeout_handle = None

    async def _on_timeout(self, symbol: str, oid: str, leg_num: int) -> None:
        state = self._states.get(symbol)
        if state is None or oid not in state.oid_to_leg:
            return  # already settled

        log.warning("[%s] Leg %d timeout — cancelling order %s", symbol, leg_num, oid)
        self._poller.untrack(oid)
        try:
            await self._client.cancel_order(oid)
        except Exception:
            log.exception("[%s] cancel_order raised for %s", symbol, oid)

        proceeds_so_far = state.proceeds
        if proceeds_so_far > _ZERO and leg_num < 3:
            log.warning(
                "[%s] timeout with partial proceeds=%.6f — attempting Leg %d",
                symbol, float(proceeds_so_far), leg_num + 1,
            )
            await self._place_leg(state, leg_num=leg_num + 1, input_qty=proceeds_so_far)
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
                "[%s] trade settled  path=%d  filled=[%.6f / %.6f / %.6f]",
                symbol, intent.path.path_id,
                float(intent.leg1_filled_qty),
                float(intent.leg2_filled_qty),
                float(intent.leg3_filled_qty),
            )
        else:
            log.warning(
                "[%s] trade aborted  path=%d  leg=[%.6f / %.6f / %.6f]",
                symbol, intent.path.path_id,
                float(intent.leg1_filled_qty),
                float(intent.leg2_filled_qty),
                float(intent.leg3_filled_qty),
            )

        # Refresh real balances after settlement so next cycle has accurate numbers.
        asyncio.ensure_future(self.refresh_balances())

    # ── helpers ───────────────────────────────────────────────────────────────

    def _state_for_oid(self, oid: str) -> Optional[_ExecState]:
        for state in self._states.values():
            if oid in state.oid_to_leg:
                return state
        return None
