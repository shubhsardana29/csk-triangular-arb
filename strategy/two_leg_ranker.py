"""TwoLegRanker — stateless 2-leg spread opportunity scorer.

Evaluates CSK INR book vs Binance USDT × USDT/INR rate (fair price).

Two directions:
  INR_CHEAP    — CSK INR asks are cheap vs fair → BUY INR, SELL USDT C2C
  INR_EXPENSIVE — CSK INR bids are expensive vs fair → BUY USDT C2C, SELL INR

Cost model: one TDS (on the sell leg) + two taker fees (one per leg).
Min spread must exceed fees + TDS + safety buffer (same floor as 3-leg).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import config
from core.models import Depth, TriBook, TwoLegResult

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE  = Decimal(1)


class TwoLegRanker:
    """Scores all symbols for 2-leg spread arb on each tick. Stateless."""

    def __init__(
        self,
        taker_fee: Optional[Decimal] = None,
        tds_rate: Optional[Decimal] = None,
        min_spread_pct: Optional[Decimal] = None,
    ):
        self.fee        = taker_fee      if taker_fee      is not None else config.TAKER_FEE
        self.tds        = tds_rate       if tds_rate       is not None else config.TDS_RATE
        self.min_spread = min_spread_pct if min_spread_pct is not None else config.TWO_LEG_MIN_SPREAD_PCT
        self._fee_map: dict[str, Decimal] = {}

    def update_fees(self, fee_map: dict[str, Decimal]) -> None:
        self._fee_map = fee_map

    def rank_all(
        self,
        tri_books: dict[str, TriBook],
        balances: dict[str, Decimal],
    ) -> dict[str, TwoLegResult]:
        """Evaluate every symbol. Returns {symbol: TwoLegResult}."""
        return {
            symbol: self.rank(symbol, book, balances)
            for symbol, book in tri_books.items()
        }

    def rank(
        self,
        symbol: str,
        book: TriBook,
        balances: dict[str, Decimal],
    ) -> TwoLegResult:
        symbol = symbol.upper()

        # Fair INR price derived from Binance USDT mid × CSK USDT/INR mid.
        usdt_inr_mid = book.usdt_inr.mid
        usdt_mid     = book.s_usdt.mid
        if usdt_inr_mid <= _ZERO or usdt_mid <= _ZERO:
            return self._no_opportunity(symbol, "No USDT/INR or USDT price")

        fair_inr = usdt_mid * usdt_inr_mid

        # Determine trade size.
        ref_price = book.s_inr.mid
        if ref_price <= _ZERO:
            return self._no_opportunity(symbol, "No INR price")

        target_qty = (
            config.MAX_EXPOSURES.get(symbol)
            or (config.DEFAULT_SYMBOL_NOTIONAL_INR / ref_price)
        )

        # Walk INR asks (cost to BUY on INR).
        ask_qty, ask_vwap, ask_worst = book.s_inr.walk_asks_to_qty(target_qty)
        # Walk INR bids (proceeds from SELL on INR).
        bid_qty, bid_vwap, bid_worst = book.s_inr.walk_bids_to_qty(target_qty)

        sym_fee = self._fee_map.get(symbol, self.fee)

        # ── INR_CHEAP: buy on INR (cheap), sell on USDT C2C (expensive) ──────
        # yield ≈ fair / ask_vwap * (1-fee_buy) * (1-fee_sell) * (1-tds)
        if ask_qty > _ZERO and ask_vwap > _ZERO:
            net_yield_cheap = (fair_inr / ask_vwap) * (_ONE - sym_fee) ** 2 * (_ONE - self.tds)
            spread_cheap    = (fair_inr - ask_vwap) / fair_inr
            profit_cheap    = net_yield_cheap - _ONE
        else:
            net_yield_cheap = _ZERO
            spread_cheap    = _ZERO
            profit_cheap    = _ZERO

        # ── INR_EXPENSIVE: buy on USDT C2C (cheap), sell on INR (expensive) ──
        # yield ≈ bid_vwap / fair * (1-fee_buy) * (1-fee_sell) * (1-tds)
        if bid_qty > _ZERO and bid_vwap > _ZERO:
            net_yield_exp = (bid_vwap / fair_inr) * (_ONE - sym_fee) ** 2 * (_ONE - self.tds)
            spread_exp    = (bid_vwap - fair_inr) / fair_inr
            profit_exp    = net_yield_exp - _ONE
        else:
            net_yield_exp = _ZERO
            spread_exp    = _ZERO
            profit_exp    = _ZERO

        # Pick the better direction.
        if profit_cheap >= profit_exp:
            direction   = "INR_CHEAP"
            buy_venue   = "spot_inr"
            sell_venue  = "spot_usdt"
            buy_price   = ask_worst
            sell_price  = book.s_usdt.bid
            spread_pct  = spread_cheap
            profit_pct  = profit_cheap
            qty         = ask_qty
        else:
            direction   = "INR_EXPENSIVE"
            buy_venue   = "spot_usdt"
            sell_venue  = "spot_inr"
            buy_price   = book.s_usdt.ask
            sell_price  = bid_worst
            spread_pct  = spread_exp
            profit_pct  = profit_exp
            qty         = bid_qty

        if buy_price <= _ZERO or sell_price <= _ZERO or qty <= _ZERO:
            return self._no_opportunity(symbol, "Zero price or qty")

        # ── Liquidity guards ──────────────────────────────────────────────────
        # 1. Spread sanity cap — a spread above 15% is a stale/empty book, not a real edge.
        abs_spread = abs(spread_pct)
        if abs_spread > config.TWO_LEG_MAX_SPREAD_SANITY:
            return self._no_opportunity(
                symbol,
                f"Spread {float(abs_spread)*100:.1f}% exceeds sanity cap — illiquid book",
            )

        # 2. Minimum available notional — book must offer at least ₹2,000 of depth.
        #    (fill ratio is not used — target_qty is the desired size, not the minimum viable size)
        raw_notional = qty * (ask_vwap if direction == "INR_CHEAP" else bid_vwap)
        if raw_notional < config.TWO_LEG_MIN_NOTIONAL_INR:
            return self._no_opportunity(
                symbol,
                f"Notional ₹{float(raw_notional):.0f} below minimum ₹{float(config.TWO_LEG_MIN_NOTIONAL_INR):.0f}",
            )

        # Balance clamp.
        if direction == "INR_CHEAP":
            balance = balances.get("INR", _ZERO)
            max_qty = balance / buy_price if buy_price > _ZERO else _ZERO
            executable = min(qty, max_qty)
        else:
            balance = balances.get("USDT", _ZERO)
            max_qty = balance / buy_price if buy_price > _ZERO else _ZERO
            executable = min(qty, max_qty)

        # Opportunity gate — read from config live so ControlAPI changes take effect immediately.
        min_spread = config.TWO_LEG_MIN_SPREAD_PCT
        if profit_pct < min_spread:
            return TwoLegResult(
                symbol=symbol, direction=direction,
                buy_venue=buy_venue, sell_venue=sell_venue,
                buy_price=buy_price, sell_price=sell_price,
                qty=qty, base_currency=symbol,
                spread_pct=spread_pct, profit_pct=profit_pct,
                expected_profit_inr=profit_pct * (executable or qty) * ref_price,
                executable_qty=executable,
                opportunity=False,
                reason="Spread < threshold",
            )

        expected_profit_inr = profit_pct * executable * ref_price

        return TwoLegResult(
            symbol=symbol, direction=direction,
            buy_venue=buy_venue, sell_venue=sell_venue,
            buy_price=buy_price, sell_price=sell_price,
            qty=qty, base_currency=symbol,
            spread_pct=spread_pct, profit_pct=profit_pct,
            expected_profit_inr=expected_profit_inr,
            executable_qty=executable,
            opportunity=executable > _ZERO,
            reason="" if executable > _ZERO else "No balance",
        )

    @staticmethod
    def _no_opportunity(symbol: str, reason: str) -> TwoLegResult:
        z = _ZERO
        return TwoLegResult(
            symbol=symbol, direction="", buy_venue="", sell_venue="",
            buy_price=z, sell_price=z, qty=z, base_currency=symbol,
            spread_pct=z, profit_pct=z, expected_profit_inr=z,
            executable_qty=z, opportunity=False, reason=reason,
        )
