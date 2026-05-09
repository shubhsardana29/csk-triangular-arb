"""TriRebalancer — maker-only USDT/INR balance maintainer.

Monitors the USDT share of the portfolio. When USDT drops below
REBALANCER_USDT_FLOOR_PCT, places a passive BUY on USDT/INR (buying
USDT with INR at the book bid) to restore USDT to REBALANCER_USDT_TARGET_PCT.

Design:
  - Never takes the spread (posts at or below current bid, waits for fill).
  - Reprices every tick if the book bid has moved more than 1 tick.
  - Cancels and does not replace if USDT share recovers above target.
  - Balances are read from the executor's live balances dict.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from time import time
from typing import Optional

import config
from core.models import TriBook

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE  = Decimal(1)


class TriRebalancer:
    """Maintains USDT balance via passive maker orders on USDT/INR."""

    RETRY_COOLDOWN_S = 30.0   # wait this long after a failed placement before retrying

    def __init__(self, client) -> None:
        self._client    = client
        self._oid: Optional[str] = None       # active rebalance order id
        self._last_price: Decimal = _ZERO     # price of the resting order
        self._last_qty: Decimal   = _ZERO
        self._placed_at: float    = 0.0
        self._last_fail_ts: float = 0.0       # timestamp of last failed placement

    async def on_tick(
        self,
        tri_books: dict[str, TriBook],
        balances: dict[str, Decimal],
    ) -> None:
        if not config.REBALANCER_ENABLED:
            return

        # Compute portfolio total value in INR.
        first_book  = next(iter(tri_books.values()), None)
        usdt_inr    = first_book.usdt_inr.mid if first_book else _ZERO
        if usdt_inr <= _ZERO:
            return

        inr_bal   = balances.get("INR",  _ZERO)
        usdt_bal  = balances.get("USDT", _ZERO)
        usdt_inr_val = usdt_bal * usdt_inr
        total_inr = inr_bal + usdt_inr_val
        if total_inr <= _ZERO:
            return

        usdt_share = usdt_inr_val / total_inr

        # If USDT share is already at or above target, cancel any resting order.
        if usdt_share >= config.REBALANCER_USDT_FLOOR_PCT:
            if self._oid:
                await self._cancel_resting()
            return

        # Compute how much USDT to buy to reach target.
        target_usdt_inr_val = total_inr * config.REBALANCER_USDT_TARGET_PCT
        needed_inr = max(_ZERO, target_usdt_inr_val - usdt_inr_val)
        if needed_inr <= _ZERO or inr_bal < needed_inr:
            return

        # Buy USDT at the book bid (passive — wait for a seller to hit us).
        usdt_book = first_book.usdt_inr if first_book else None
        if usdt_book is None or not usdt_book.bids:
            return
        bid_price = usdt_book.bid
        if bid_price <= _ZERO:
            return

        buy_qty = needed_inr / bid_price  # USDT amount to buy

        # Reprice if we have a resting order at a stale price.
        if self._oid:
            price_moved = abs(bid_price - self._last_price) > bid_price * Decimal("0.001")
            if not price_moved:
                return  # order is still competitive, leave it
            await self._cancel_resting()

        # Don't hammer the API after a failed placement — wait for cooldown.
        if time() - self._last_fail_ts < self.RETRY_COOLDOWN_S:
            return

        await self._place(bid_price, buy_qty)

    async def _place(self, price: Decimal, qty: Decimal) -> None:
        log.info("[rebalancer] placing BUY USDT/INR  price=%.4f  qty=%.4f USDT", float(price), float(qty))
        try:
            oid = await self._client.place_usdtinr_order("BUY", price, qty)
            if oid:
                self._oid        = oid
                self._last_price = price
                self._last_qty   = qty
                self._placed_at  = time()
                log.info("[rebalancer] resting order %s", oid)
            else:
                log.warning("[rebalancer] place_usdtinr_order returned None — cooling down %ds", int(self.RETRY_COOLDOWN_S))
                self._last_fail_ts = time()
        except Exception:
            log.exception("[rebalancer] failed to place order")

    async def _cancel_resting(self) -> None:
        if not self._oid:
            return
        try:
            await self._client.cancel_order(self._oid)
            log.info("[rebalancer] cancelled resting order %s", self._oid)
        except Exception:
            log.exception("[rebalancer] cancel failed for %s", self._oid)
        finally:
            self._oid        = None
            self._last_price = _ZERO
            self._last_qty   = _ZERO
