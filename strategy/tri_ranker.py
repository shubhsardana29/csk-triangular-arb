"""TriRanker — stateless per-tick opportunity scorer.

Pure function over TriBook data. No I/O, no state, no side effects.
TriEngine calls rank_all() every tick and routes placeable results
to the executor. Sub-threshold results come back with opportunity=False
so observability/logging stays informative without acting on them.

Direction is decided here once and flows through to the executor unchanged —
the executor never re-evaluates venue or path direction.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import config
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


def _path_definitions(symbol: str) -> list[dict]:
    """Static path metadata for a given symbol — no math, just labels."""
    return [
        {
            "direction":          f"SELL {symbol}/INR -> BUY USDT/INR -> BUY {symbol}/USDT",
            "logical_case":       "case_a",
            "logical_case_label": "Case A: INR-rich spot leg",
            "inventory_mode":     "token-start",
            "thesis":             f"{symbol}/INR bid is richer than rebuilding through USDT asks",
        },
        {
            "direction":          f"SELL {symbol}/USDT -> SELL USDT/INR -> BUY {symbol}/INR",
            "logical_case":       "case_b",
            "logical_case_label": "Case B: USDT-rich cross leg",
            "inventory_mode":     "token-start",
            "thesis":             f"{symbol}/USDT→USDT/INR bids richer than buying {symbol}/INR asks",
        },
        {
            "direction":          f"BUY {symbol}/INR -> SELL {symbol}/USDT -> SELL USDT/INR",
            "logical_case":       "case_b",
            "logical_case_label": "Case B: USDT-rich cross leg",
            "inventory_mode":     "inr-start",
            "thesis":             f"{symbol}/USDT→USDT/INR bids richer than buying {symbol}/INR asks",
        },
        {
            "direction":          f"BUY USDT/INR -> BUY {symbol}/USDT -> SELL {symbol}/INR",
            "logical_case":       "case_a",
            "logical_case_label": "Case A: INR-rich spot leg",
            "inventory_mode":     "inr-start",
            "thesis":             f"{symbol}/INR bid is richer than rebuilding through USDT asks",
        },
    ]


class TriRanker:
    """Scores all symbols on each tick. Stateless beyond config.

    Mirrors simple-arb's Ranker pattern: pure function, no exchange
    calls, no state mutation. TriEngine calls rank_all() per tick
    and gets a typed result it can route to the executor.
    """

    def __init__(
        self,
        taker_fee: Optional[Decimal] = None,
        tds_rate: Optional[Decimal] = None,
        min_profit_pct: Optional[Decimal] = None,
    ):
        self.fee            = taker_fee      if taker_fee      is not None else config.TAKER_FEE
        self.tds            = tds_rate       if tds_rate       is not None else config.TDS_RATE
        self.min_profit_pct = min_profit_pct if min_profit_pct is not None else config.ARBITRAGE_MIN_PROFIT_THRESHOLD

    # ── public API ────────────────────────────────────────────────────────────

    def rank_all(
        self,
        tri_books: dict[str, TriBook],
        balances: dict[str, Decimal],
    ) -> dict[str, tuple[PathResult, PathResult]]:
        """Evaluate every symbol. Returns {symbol: (net_result, gross_result)}.

        net   = real fees + TDS applied → what the bot would actually capture.
        gross = zero fees + zero TDS    → theoretical ceiling, useful for UI.
        """
        return {
            symbol: self.rank(symbol, book, balances)
            for symbol, book in tri_books.items()
        }

    def rank(
        self,
        symbol: str,
        tri_book: TriBook,
        balances: dict[str, Decimal],
    ) -> tuple[PathResult, PathResult]:
        """Evaluate all 4 paths for one symbol. Returns (net, gross)."""
        symbol = symbol.upper()

        ref_price = tri_book.s_inr.mid
        target_s = config.MAX_EXPOSURES.get(symbol)
        if target_s is None:
            target_s = (
                config.DEFAULT_SYMBOL_NOTIONAL_INR / ref_price
                if ref_price > _ZERO
                else config.DEFAULT_SYMBOL_EXPOSURE
            )
        target_inr = config.MAX_EXPOSURES.get("INR", config.DEFAULT_INR_EXPOSURE)

        net_ratios   = self._path_yields(tri_book, target_s, target_inr, self.fee, self.tds)
        gross_ratios = self._path_yields(tri_book, target_s, target_inr, _ZERO,    _ZERO)
        meta         = _path_definitions(symbol)

        net   = self._best_path(net_ratios,   meta, symbol, tri_book, balances, target_s, target_inr, with_costs=True)
        gross = self._best_path(gross_ratios, meta, symbol, tri_book, balances, target_s, target_inr, with_costs=False)
        return net, gross

    # ── private: path yield computation ──────────────────────────────────────

    @staticmethod
    def _path_yields(
        tri: TriBook,
        target_s: Decimal,
        target_inr: Decimal,
        fee: Decimal,
        tds: Decimal,
    ) -> list[Decimal]:
        """Compute output/input yield ratio for all 4 paths.

        Returns a list of 4 Decimal values; 0 means the path is not
        executable (insufficient depth or zero price on some leg).
        """
        s_inr    = tri.s_inr
        s_usdt   = tri.s_usdt
        usdt_inr = tri.usdt_inr
        net = _ONE - fee

        # Path 1: SELL S/INR (TDS) → BUY USDT/INR → BUY S/USDT (USDT-sell TDS)
        v1 = _vwap_bids(s_inr, target_s)
        if v1 == _ZERO:
            p1 = _ZERO
        else:
            inr1 = target_s * v1 * net * (_ONE - tds)
            v2   = _vwap_asks(usdt_inr, inr1)
            if v2 == _ZERO:
                p1 = _ZERO
            else:
                usdt2 = (inr1 / v2) * net
                ask3  = s_usdt.ask
                if ask3 == _ZERO:
                    p1 = _ZERO
                else:
                    v3 = _vwap_asks(s_usdt, usdt2 / ask3)
                    p1 = (usdt2 * net * (_ONE - tds) / v3 / target_s) if v3 else _ZERO

        # Path 2: SELL S/USDT (TDS) → SELL USDT/INR (TDS) → BUY S/INR
        v1 = _vwap_bids(s_usdt, target_s)
        if v1 == _ZERO:
            p2 = _ZERO
        else:
            usdt1 = target_s * v1 * net * (_ONE - tds)
            v2    = _vwap_bids(usdt_inr, usdt1)
            if v2 == _ZERO:
                p2 = _ZERO
            else:
                inr2 = usdt1 * v2 * net * (_ONE - tds)
                ask3 = s_inr.ask
                if ask3 == _ZERO:
                    p2 = _ZERO
                else:
                    v3 = _vwap_asks(s_inr, inr2 / ask3)
                    p2 = (inr2 * net / v3 / target_s) if v3 else _ZERO

        # Path 3: BUY S/INR → SELL S/USDT (TDS) → SELL USDT/INR (TDS)
        ask1 = s_inr.ask
        if ask1 == _ZERO:
            p3 = _ZERO
        else:
            v1 = _vwap_asks(s_inr, target_inr / ask1)
            if v1 == _ZERO:
                p3 = _ZERO
            else:
                s1 = target_inr * net / v1
                v2 = _vwap_bids(s_usdt, s1)
                if v2 == _ZERO:
                    p3 = _ZERO
                else:
                    usdt2 = s1 * v2 * net * (_ONE - tds)
                    v3    = _vwap_bids(usdt_inr, usdt2)
                    p3 = (usdt2 * v3 * net * (_ONE - tds) / target_inr) if v3 else _ZERO

        # Path 4: BUY USDT/INR → BUY S/USDT (USDT-sell TDS) → SELL S/INR (TDS)
        v1 = _vwap_asks(usdt_inr, target_inr)
        if v1 == _ZERO:
            p4 = _ZERO
        else:
            usdt1 = target_inr / v1 * net
            ask2  = s_usdt.ask
            if ask2 == _ZERO:
                p4 = _ZERO
            else:
                v2 = _vwap_asks(s_usdt, usdt1 / ask2)
                if v2 == _ZERO:
                    p4 = _ZERO
                else:
                    s2 = usdt1 * net * (_ONE - tds) / v2
                    v3 = _vwap_bids(s_inr, s2)
                    p4 = (s2 * v3 * net * (_ONE - tds) / target_inr) if v3 else _ZERO

        return [p1, p2, p3, p4]

    # ── private: select best path and build PathResult ────────────────────────

    def _best_path(
        self,
        ratios: list[Decimal],
        meta: list[dict],
        symbol: str,
        tri_book: TriBook,
        balances: dict[str, Decimal],
        target_s: Decimal,
        target_inr: Decimal,
        with_costs: bool,
    ) -> PathResult:
        best_idx    = max(range(len(ratios)), key=lambda i: ratios[i])
        yield_ratio = ratios[best_idx]
        profit_pct  = yield_ratio - _ONE

        # Paths 1/2 are token-start (use symbol balance);
        # paths 3/4 are INR-start (use INR balance).
        base          = symbol if best_idx < 2 else "INR"
        target_exp    = target_s if base == symbol else config.MAX_EXPOSURES.get("INR", target_inr)
        executable    = min(target_exp, balances.get(base, _ZERO))

        mark = tri_book.s_inr.bid
        profit_inr = (
            profit_pct * executable * mark if base == symbol
            else profit_pct * executable
        )

        opportunity = with_costs and profit_pct >= self.min_profit_pct
        reason = ""
        if with_costs and not opportunity:
            reason = "Spread < costs" if profit_pct < _ZERO else "Spread < threshold"

        return PathResult(
            path_id=best_idx + 1,
            direction=meta[best_idx]["direction"],
            logical_case=meta[best_idx]["logical_case"],
            logical_case_label=meta[best_idx]["logical_case_label"],
            inventory_mode=meta[best_idx]["inventory_mode"],
            thesis=meta[best_idx]["thesis"],
            yield_ratio=yield_ratio,
            profit_pct=profit_pct,
            executable_qty=executable,
            base_currency=base,
            expected_profit_inr=profit_inr,
            opportunity=opportunity,
            reason=reason,
        )
