"""Shared data types for csk-triangular-arb.

All financial quantities use Decimal to avoid the ~6-significant-figure
loss of float arithmetic. Never use float for prices, quantities, or fees.
Convert at the API boundary (Depth.from_raw) and keep Decimal throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Optional


# ── Depth ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Depth:
    """Immutable order book snapshot. Levels are (price, qty) Decimal tuples."""

    bids: tuple[tuple[Decimal, Decimal], ...]   # best bid first (highest price)
    asks: tuple[tuple[Decimal, Decimal], ...]   # best ask first (lowest price)

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_raw(cls, book: dict) -> Depth:
        """Parse raw API response {"bids": [["price", "qty"], ...], "asks": [...]}."""
        return cls(
            bids=_parse_levels(book.get("bids", [])),
            asks=_parse_levels(book.get("asks", [])),
        )

    @classmethod
    def empty(cls) -> Depth:
        return cls(bids=(), asks=())

    # ── top-of-book ───────────────────────────────────────────────────────────

    @property
    def bid(self) -> Decimal:
        return self.bids[0][0] if self.bids else Decimal(0)

    @property
    def ask(self) -> Decimal:
        return self.asks[0][0] if self.asks else Decimal(0)

    @property
    def mid(self) -> Decimal:
        b, a = self.bid, self.ask
        if b > 0 and a > 0:
            return (b + a) / 2
        return b or a

    # ── depth walking ─────────────────────────────────────────────────────────

    def walk_bids_to_qty(self, target_qty: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Walk bids (SELL into book). Returns (filled_qty, vwap, worst_price)."""
        return _walk_levels(self.bids, target_qty)

    def walk_asks_to_qty(self, target_qty: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Walk asks (BUY from book by qty). Returns (filled_qty, vwap, worst_price)."""
        return _walk_levels(self.asks, target_qty)

    def walk_asks_to_notional(self, notional: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Walk asks spending up to `notional` quote currency.
        Returns (qty_bought, vwap, worst_price).
        """
        return _walk_asks_by_notional(self.asks, notional)


def _parse_levels(
    raw: list,
) -> tuple[tuple[Decimal, Decimal], ...]:
    out: list[tuple[Decimal, Decimal]] = []
    for level in raw:
        try:
            p = Decimal(str(level[0]))
            q = Decimal(str(level[1]))
            if p > 0 and q > 0:
                out.append((p, q))
        except Exception:
            pass
    return tuple(out)


def _walk_levels(
    levels: tuple[tuple[Decimal, Decimal], ...],
    target_qty: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    if not levels or target_qty <= 0:
        return Decimal(0), Decimal(0), Decimal(0)

    remaining = target_qty
    total_cost = Decimal(0)
    worst = Decimal(0)

    for price, qty in levels:
        take = min(remaining, qty)
        total_cost += take * price
        worst = price
        remaining -= take
        if remaining <= 0:
            break

    filled = target_qty - remaining
    if filled <= 0:
        return Decimal(0), Decimal(0), Decimal(0)

    return filled, total_cost / filled, worst


def _walk_asks_by_notional(
    asks: tuple[tuple[Decimal, Decimal], ...],
    notional: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Walk asks spending up to `notional` quote currency. Returns (qty_bought, vwap, worst_price)."""
    if not asks or notional <= 0:
        return Decimal(0), Decimal(0), Decimal(0)

    remaining = notional
    total_qty = Decimal(0)
    worst = Decimal(0)

    for price, qty in asks:
        level_cost = qty * price
        if remaining <= level_cost:
            take_qty = remaining / price
            total_qty += take_qty
            worst = price
            remaining = Decimal(0)
            break
        total_qty += qty
        remaining -= level_cost
        worst = price

    if total_qty <= 0:
        return Decimal(0), Decimal(0), Decimal(0)

    spent = notional - remaining
    return total_qty, spent / total_qty, worst


# ── TriBook ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TriBook:
    """Three Depth snapshots needed to evaluate one symbol's triangle."""

    symbol: str
    s_inr: Depth
    s_usdt: Depth
    usdt_inr: Depth
    ts: float = 0.0

    @classmethod
    def from_raw(cls, symbol: str, raw: dict, ts: float = 0.0) -> TriBook:
        """Build from the raw dict format: {"{SYMBOL}/INR": {...}, ...}."""
        s = symbol.upper()
        return cls(
            symbol=s,
            s_inr=Depth.from_raw(raw.get(f"{s}/INR", {})),
            s_usdt=Depth.from_raw(raw.get(f"{s}/USDT", {})),
            usdt_inr=Depth.from_raw(raw.get("USDT/INR", {})),
            ts=ts,
        )


# ── PathResult ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PathResult:
    """One evaluated triangular path — all financials in Decimal."""

    path_id: int              # 1-4 matching path_definitions order
    direction: str
    logical_case: str
    logical_case_label: str
    inventory_mode: str       # "token-start" or "inr-start"
    thesis: str
    yield_ratio: Decimal      # output_qty / input_qty  (>1 means raw gain)
    profit_pct: Decimal       # (yield_ratio - 1) expressed as a fraction, e.g. 0.008
    executable_qty: Decimal
    base_currency: str        # "INR" or the symbol name
    expected_profit_inr: Decimal
    opportunity: bool
    reason: str = ""          # populated when opportunity=False


# ── TwoLegResult ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TwoLegResult:
    """One evaluated 2-leg spread opportunity — buy cheap venue, sell expensive."""

    symbol: str
    direction: str           # "INR_CHEAP" | "INR_EXPENSIVE"
    buy_venue: str           # "spot_inr" | "spot_usdt"
    sell_venue: str          # "spot_usdt" | "spot_inr"
    buy_price: Decimal       # worst-of-walk limit price (BUY leg)
    sell_price: Decimal      # reference bid (SELL leg initial price)
    qty: Decimal             # base qty to trade
    base_currency: str       # always the symbol
    spread_pct: Decimal      # (fair - vwap) / fair  or  (vwap - fair) / fair
    profit_pct: Decimal      # net after fee + TDS
    expected_profit_inr: Decimal
    executable_qty: Decimal  # clamped to balance
    opportunity: bool
    reason: str = ""


# ── TriIntent ─────────────────────────────────────────────────────────────────

@dataclass
class TriIntent:
    """In-flight execution state for one 3-leg trade. Mutable by design."""

    symbol: str
    path: PathResult
    placed_at: float = 0.0
    leg1_oid: Optional[str] = None
    leg2_oid: Optional[str] = None
    leg3_oid: Optional[str] = None
    leg1_filled_qty: Decimal = field(default_factory=lambda: Decimal(0))
    leg2_filled_qty: Decimal = field(default_factory=lambda: Decimal(0))
    leg3_filled_qty: Decimal = field(default_factory=lambda: Decimal(0))


# ── TwoLegIntent ──────────────────────────────────────────────────────────────

@dataclass
class TwoLegIntent:
    """In-flight execution state for one 2-leg spread trade. Mutable by design."""

    symbol: str
    result: TwoLegResult
    placed_at: float = 0.0
    leg1_oid: Optional[str] = None
    leg2_oid: Optional[str] = None
    leg1_filled_qty: Decimal = field(default_factory=lambda: Decimal(0))
    leg2_filled_qty: Decimal = field(default_factory=lambda: Decimal(0))
    buy_avg_price: Decimal = field(default_factory=lambda: Decimal(0))
    cost_floor: Decimal = field(default_factory=lambda: Decimal(0))    # floor for Leg 2 SELL
