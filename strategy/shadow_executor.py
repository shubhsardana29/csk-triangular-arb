"""ShadowExecutor — paper portfolio simulator.

Simulates 3-leg fills against an in-memory balance dict without placing
real orders. All arithmetic uses Decimal. Dispatches on PathResult.path_id
(an integer 1-4) — not fragile string matching on direction text.

This is the stand-in for the real TriExecutor that will place live orders
in a later migration step. The interface is intentionally identical so
swapping it requires only changing the injected executor in main.py.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Optional

from core.models import Depth, PathResult, TriBook, TwoLegResult

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE  = Decimal(1)


def _vwap_bids(depth: Depth, qty: Decimal) -> Decimal:
    _, vwap, _ = depth.walk_bids_to_qty(qty)
    return vwap


def _vwap_asks(depth: Depth, qty: Decimal) -> Decimal:
    _, vwap, _ = depth.walk_asks_to_qty(qty)
    return vwap


class ShadowExecutor:
    """Simulates 3-leg fills against a paper portfolio.

    balances: mutable dict[asset → Decimal]. Written on every execute().
    fee, tds: Decimal rates applied per leg as in the real cost model.
    """

    TRADE_COOLDOWN_S = 30.0

    def __init__(
        self,
        balances: dict[str, Decimal],
        fee: Decimal,
        tds: Decimal,
        on_settle: Optional[Callable[[str], None]] = None,
    ):
        self.balances: dict[str, Decimal] = {k: Decimal(str(v)) for k, v in balances.items()}
        self.fee = Decimal(str(fee))
        self.tds  = Decimal(str(tds))
        self._on_settle = on_settle
        self._fee_map: dict[str, Decimal] = {}
        self._last_traded: dict[str, float] = {}

    def update_fees(self, fee_map: dict[str, Decimal]) -> None:
        self._fee_map = fee_map

    @property
    def active_order_ids(self) -> list[str]:
        return []   # shadow mode has no real orders

    # ── lifecycle (matches TriExecutor interface) ─────────────────────────────

    async def start(self) -> None:
        """No-op — ShadowExecutor needs no background tasks."""

    # ── public ────────────────────────────────────────────────────────────────

    async def execute(self, path: PathResult, tri_book: TriBook) -> dict:
        """Simulate fills for `path` against `tri_book`. Returns variance dict."""
        symbol  = tri_book.symbol
        qty     = path.executable_qty
        fee     = self._fee_map.get(symbol, self.fee)
        usdt_fee = self._fee_map.get("USDT", self.fee)

        pre_s   = self.balances.get(symbol, _ZERO)
        pre_inr = self.balances.get("INR",  _ZERO)
        pre_usd = self.balances.get("USDT", _ZERO)

        _empty = {
            "result_balances": self.balances.copy(),
            "symbol_variance": _ZERO,
            "inr_variance":    _ZERO,
            "usdt_variance":   _ZERO,
        }

        now = time.monotonic()
        if now - self._last_traded.get(symbol, 0.0) < self.TRADE_COOLDOWN_S:
            return _empty

        if path.path_id == 1:
            # S → INR (TDS) → USDT (buy) → S (USDT-sell TDS)
            inr   = self._sell(tri_book.s_inr,   qty,  fee=fee,      apply_tds=True)
            usdt  = self._buy_by_notional(tri_book.usdt_inr, inr,  fee=usdt_fee)
            s_out = self._buy_by_notional(tri_book.s_usdt, usdt, fee=fee, tds_on_receive=True)
            if not all([inr, usdt, s_out]):
                return _empty
            self.balances[symbol] = pre_s - qty + s_out

        elif path.path_id == 2:
            # S → USDT (TDS) → INR (TDS) → S (buy)
            usdt  = self._sell(tri_book.s_usdt,   qty,  fee=fee,      apply_tds=True)
            inr   = self._sell(tri_book.usdt_inr, usdt, fee=usdt_fee, apply_tds=True)
            s_out = self._buy_by_notional(tri_book.s_inr, inr, fee=fee)
            if not all([usdt, inr, s_out]):
                return _empty
            self.balances[symbol] = pre_s - qty + s_out

        elif path.path_id == 3:
            # INR → S (buy) → USDT (TDS) → INR (TDS)
            s_got = self._buy_by_notional(tri_book.s_inr, qty,   fee=fee)
            usdt  = self._sell(tri_book.s_usdt,   s_got, fee=fee,      apply_tds=True)
            inr   = self._sell(tri_book.usdt_inr, usdt,  fee=usdt_fee, apply_tds=True)
            if not all([s_got, usdt, inr]):
                return _empty
            self.balances["INR"] = pre_inr - qty + inr

        elif path.path_id == 4:
            # INR → USDT (buy) → S (USDT-sell TDS) → INR (TDS)
            usdt  = self._buy_by_notional(tri_book.usdt_inr, qty,  fee=usdt_fee)
            s_got = self._buy_by_notional(tri_book.s_usdt, usdt, fee=fee, tds_on_receive=True)
            inr   = self._sell(tri_book.s_inr, s_got, fee=fee, apply_tds=True)
            if not all([usdt, s_got, inr]):
                return _empty
            self.balances["INR"] = pre_inr - qty + inr

        else:
            log.warning("ShadowExecutor: unknown path_id=%s for %s", path.path_id, symbol)
            return _empty

        self._last_traded[symbol] = now

        result = {
            "result_balances": self.balances.copy(),
            "symbol_variance": self.balances.get(symbol, _ZERO) - pre_s,
            "inr_variance":    self.balances.get("INR",  _ZERO) - pre_inr,
            "usdt_variance":   self.balances.get("USDT", _ZERO) - pre_usd,
        }

        # Release the position lock immediately — shadow trades settle synchronously.
        if self._on_settle is not None:
            try:
                self._on_settle(symbol)
            except Exception:
                log.exception("[shadow] on_settle raised for %s", symbol)

        return result

    # ── private: leg helpers ──────────────────────────────────────────────────

    def _sell(self, depth: Depth, amount: Decimal, apply_tds: bool = True,
              fee: Optional[Decimal] = None) -> Decimal:
        """Sell `amount` base into book. Returns quote received net of fee (+ TDS if flagged)."""
        vwap = _vwap_bids(depth, amount)
        if vwap == _ZERO:
            return _ZERO
        f = fee if fee is not None else self.fee
        proceeds = amount * vwap * (_ONE - f)
        return proceeds * (_ONE - self.tds) if apply_tds else proceeds

    def _buy_by_notional(
        self, depth: Depth, notional: Decimal, tds_on_receive: bool = False,
        fee: Optional[Decimal] = None,
    ) -> Decimal:
        """Spend `notional` quote buying base. Returns qty received net of fee (+ TDS if flagged)."""
        f = fee if fee is not None else self.fee
        net_notional = notional * (_ONE - f)
        if tds_on_receive:
            net_notional = net_notional * (_ONE - self.tds)
        ask = depth.ask
        if ask == _ZERO:
            return _ZERO
        vwap = _vwap_asks(depth, net_notional / ask)
        if vwap == _ZERO:
            return _ZERO
        return net_notional / vwap


class ShadowTwoLegExecutor:
    """Paper-trades 2-leg spread opportunities against the shared balance dict.

    Shares the same `balances` reference as ShadowExecutor so both strategies
    deplete the same paper portfolio. Simulates fills synchronously against
    the live book snapshot and releases the position lock immediately.

    INR_CHEAP:     BUY S/INR → SELL S/USDT (C2C converts USDT → INR implicitly)
    INR_EXPENSIVE: BUY S/USDT (C2C) → SELL S/INR
    """

    # Minimum seconds between shadow trades on the same symbol. Prevents
    # re-entry every tick while an opportunity persists — mirrors real
    # settlement latency and keeps the paper portfolio realistic.
    TRADE_COOLDOWN_S = 30.0

    def __init__(
        self,
        balances: dict[str, Decimal],
        fee: Decimal,
        tds: Decimal,
        on_settle=None,
    ):
        self.balances = balances   # shared reference — same dict as ShadowExecutor
        self.fee = Decimal(str(fee))
        self.tds = Decimal(str(tds))
        self._on_settle = on_settle
        self._fee_map: dict[str, Decimal] = {}
        self._last_traded: dict[str, float] = {}

    def update_fees(self, fee_map: dict[str, Decimal]) -> None:
        self._fee_map = fee_map

    @property
    def active_order_ids(self) -> list[str]:
        return []

    async def start(self) -> None:
        pass

    async def reprice_tick(self, tri_books: dict) -> None:
        pass   # no repricing needed in shadow mode

    async def execute(self, result: TwoLegResult, book: TriBook) -> dict:
        symbol = result.symbol
        qty    = result.executable_qty

        pre_s   = self.balances.get(symbol, _ZERO)
        pre_inr = self.balances.get("INR",  _ZERO)
        pre_usd = self.balances.get("USDT", _ZERO)

        _empty = {
            "result_balances": self.balances.copy(),
            "symbol_variance": _ZERO,
            "inr_variance":    _ZERO,
            "usdt_variance":   _ZERO,
        }

        if qty <= _ZERO:
            return _empty

        # Bug fix: per-symbol cooldown — prevents re-entry every 100ms tick
        # while a spread persists. 30s mirrors realistic settlement latency.
        now = time.monotonic()
        if now - self._last_traded.get(symbol, 0.0) < self.TRADE_COOLDOWN_S:
            return _empty

        fee = self._fee_map.get(symbol, self.fee)

        if result.direction == "INR_CHEAP":
            # Leg 1: BUY S on INR book — spend INR, receive tokens net of fee.
            _, ask_vwap_inr, _ = book.s_inr.walk_asks_to_qty(qty)
            if ask_vwap_inr == _ZERO:
                return _empty
            inr_cost        = qty * ask_vwap_inr
            tokens_received = qty * (_ONE - fee)

            # Bug fix: reject if insufficient INR balance.
            if pre_inr < inr_cost:
                log.warning("[shadow_2leg] insufficient INR for %s: have %.2f need %.2f",
                            symbol, float(pre_inr), float(inr_cost))
                return _empty

            # Leg 2: SELL S on USDT C2C — receive USDT net of fee + TDS.
            _, bid_vwap_usdt, _ = book.s_usdt.walk_bids_to_qty(tokens_received)
            if bid_vwap_usdt == _ZERO:
                return _empty
            usdt_received = tokens_received * bid_vwap_usdt * (_ONE - fee) * (_ONE - self.tds)

            self.balances["INR"]  = pre_inr - inr_cost
            self.balances["USDT"] = pre_usd + usdt_received

        elif result.direction == "INR_EXPENSIVE":
            # Leg 1: BUY S on USDT C2C — spend USDT, receive tokens net of fee.
            _, ask_vwap_usdt, _ = book.s_usdt.walk_asks_to_qty(qty)
            if ask_vwap_usdt == _ZERO:
                return _empty
            usdt_cost       = qty * ask_vwap_usdt
            tokens_received = qty * (_ONE - fee)

            # Bug fix: reject if insufficient USDT balance.
            if pre_usd < usdt_cost:
                log.warning("[shadow_2leg] insufficient USDT for %s: have %.4f need %.4f",
                            symbol, float(pre_usd), float(usdt_cost))
                return _empty

            # Leg 2: SELL S on INR book — receive INR net of fee + TDS.
            _, bid_vwap_inr, _ = book.s_inr.walk_bids_to_qty(tokens_received)
            if bid_vwap_inr == _ZERO:
                return _empty
            inr_received = tokens_received * bid_vwap_inr * (_ONE - fee) * (_ONE - self.tds)

            self.balances["USDT"] = pre_usd - usdt_cost
            self.balances["INR"]  = pre_inr + inr_received

        else:
            log.warning("[shadow_2leg] unknown direction=%s for %s", result.direction, symbol)
            return _empty

        self._last_traded[symbol] = now

        if self._on_settle is not None:
            try:
                self._on_settle(symbol)
            except Exception:
                log.exception("[shadow_2leg] on_settle raised for %s", symbol)

        return {
            "result_balances": self.balances.copy(),
            "symbol_variance": self.balances.get(symbol, _ZERO) - pre_s,
            "inr_variance":    self.balances.get("INR",  _ZERO) - pre_inr,
            "usdt_variance":   self.balances.get("USDT", _ZERO) - pre_usd,
        }
