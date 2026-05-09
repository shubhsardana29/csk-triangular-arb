"""ExchangeAdapter protocol for csk-triangular-arb.

Strategy code talks only to this interface. Concrete implementations
(CoinSwitchAdapter, etc.) live outside strategy/ and are never imported
from within strategy code.
"""

from __future__ import annotations

from decimal import Decimal
from typing import AsyncIterator, Protocol, runtime_checkable

from core.models import TriBook


# ── lightweight event types ───────────────────────────────────────────────────

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderRequest:
    symbol: str       # e.g. "BTC"
    side: str         # "BUY" or "SELL"
    market: str       # e.g. "BTC/INR", "BTC/USDT", "USDT/INR"
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class OrderEvent:
    order_id: str
    symbol: str
    market: str
    side: str
    filled_qty: Decimal
    avg_price: Decimal
    status: str       # "PARTIAL", "FILLED", "CANCELLED"


@dataclass(frozen=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


# ── protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class ExchangeAdapter(Protocol):
    """Minimum surface an exchange must expose for the triangular strategy."""

    async def fetch_books(self, symbols: list[str]) -> dict[str, TriBook]:
        """Fetch live 3-book snapshots for each symbol."""
        ...

    def book_stream(self) -> AsyncIterator[tuple[str, TriBook]]:
        """Yield (symbol, TriBook) as books update. Used by the WS event loop."""
        ...

    async def place_order(self, req: OrderRequest) -> str:
        """Place a limit order. Returns venue-assigned order ID."""
        ...

    async def cancel_order(self, order_id: str) -> None:
        ...

    async def get_balances(self) -> dict[str, Balance]:
        """Return current balances for all held assets."""
        ...
