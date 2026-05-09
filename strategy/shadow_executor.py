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
from decimal import Decimal

from core.models import Depth, PathResult, TriBook

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

    def __init__(
        self,
        balances: dict[str, Decimal],
        fee: Decimal,
        tds: Decimal,
    ):
        self.balances: dict[str, Decimal] = {k: Decimal(str(v)) for k, v in balances.items()}
        self.fee = Decimal(str(fee))
        self.tds  = Decimal(str(tds))

    # ── lifecycle (matches TriExecutor interface) ─────────────────────────────

    async def start(self) -> None:
        """No-op — ShadowExecutor needs no background tasks."""

    # ── public ────────────────────────────────────────────────────────────────

    async def execute(self, path: PathResult, tri_book: TriBook) -> dict:
        """Simulate fills for `path` against `tri_book`. Returns variance dict."""
        symbol  = tri_book.symbol
        qty     = path.executable_qty

        pre_s   = self.balances.get(symbol, _ZERO)
        pre_inr = self.balances.get("INR",  _ZERO)
        pre_usd = self.balances.get("USDT", _ZERO)

        _empty = {
            "result_balances": self.balances.copy(),
            "symbol_variance": _ZERO,
            "inr_variance":    _ZERO,
            "usdt_variance":   _ZERO,
        }

        if path.path_id == 1:
            # S → INR (TDS) → USDT (buy) → S (USDT-sell TDS)
            inr   = self._sell(tri_book.s_inr,   qty,  apply_tds=True)
            usdt  = self._buy_by_notional(tri_book.usdt_inr, inr)
            s_out = self._buy_by_notional(tri_book.s_usdt, usdt, tds_on_receive=True)
            if not all([inr, usdt, s_out]):
                return _empty
            self.balances[symbol] = pre_s - qty + s_out

        elif path.path_id == 2:
            # S → USDT (TDS) → INR (TDS) → S (buy)
            usdt  = self._sell(tri_book.s_usdt,   qty,  apply_tds=True)
            inr   = self._sell(tri_book.usdt_inr, usdt, apply_tds=True)
            s_out = self._buy_by_notional(tri_book.s_inr, inr)
            if not all([usdt, inr, s_out]):
                return _empty
            self.balances[symbol] = pre_s - qty + s_out

        elif path.path_id == 3:
            # INR → S (buy) → USDT (TDS) → INR (TDS)
            s_got = self._buy_by_notional(tri_book.s_inr, qty)
            usdt  = self._sell(tri_book.s_usdt,   s_got, apply_tds=True)
            inr   = self._sell(tri_book.usdt_inr, usdt,  apply_tds=True)
            if not all([s_got, usdt, inr]):
                return _empty
            self.balances["INR"] = pre_inr - qty + inr

        elif path.path_id == 4:
            # INR → USDT (buy) → S (USDT-sell TDS) → INR (TDS)
            usdt  = self._buy_by_notional(tri_book.usdt_inr, qty)
            s_got = self._buy_by_notional(tri_book.s_usdt, usdt, tds_on_receive=True)
            inr   = self._sell(tri_book.s_inr, s_got, apply_tds=True)
            if not all([usdt, s_got, inr]):
                return _empty
            self.balances["INR"] = pre_inr - qty + inr

        else:
            log.warning("ShadowExecutor: unknown path_id=%s for %s", path.path_id, symbol)
            return _empty

        return {
            "result_balances": self.balances.copy(),
            "symbol_variance": self.balances.get(symbol, _ZERO) - pre_s,
            "inr_variance":    self.balances.get("INR",  _ZERO) - pre_inr,
            "usdt_variance":   self.balances.get("USDT", _ZERO) - pre_usd,
        }

    # ── private: leg helpers ──────────────────────────────────────────────────

    def _sell(self, depth: Depth, amount: Decimal, apply_tds: bool = True) -> Decimal:
        """Sell `amount` base into book. Returns quote received net of fee (+ TDS if flagged)."""
        vwap = _vwap_bids(depth, amount)
        if vwap == _ZERO:
            return _ZERO
        proceeds = amount * vwap * (_ONE - self.fee)
        return proceeds * (_ONE - self.tds) if apply_tds else proceeds

    def _buy_by_notional(
        self, depth: Depth, notional: Decimal, tds_on_receive: bool = False,
    ) -> Decimal:
        """Spend `notional` quote buying base. Returns qty received net of fee (+ TDS if flagged)."""
        net_notional = notional * (_ONE - self.fee)
        if tds_on_receive:
            net_notional = net_notional * (_ONE - self.tds)
        ask = depth.ask
        if ask == _ZERO:
            return _ZERO
        vwap = _vwap_asks(depth, net_notional / ask)
        if vwap == _ZERO:
            return _ZERO
        return net_notional / vwap
