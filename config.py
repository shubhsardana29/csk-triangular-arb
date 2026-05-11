import logging
import os
from decimal import Decimal
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# API Credentials
COINSWITCH_API_KEY = os.getenv("COINSWITCH_API_KEY")
COINSWITCH_SECRET_KEY = os.getenv("COINSWITCH_SECRET_KEY") or os.getenv("COINSWITCH_API_SECRET")

# Trading Costs — Decimal to avoid float precision drift in financial math.
# 0.001 = 0.1% taker fee
TAKER_FEE = Decimal(os.getenv("TAKER_FEE", "0.001"))
# 0.01 = 1% TDS
TDS_RATE = Decimal(os.getenv("TDS_RATE", "0.01"))
# Breakeven: 2× taker + TDS + safety buffer
MIN_PROFIT_PCT = Decimal(os.getenv("MIN_PROFIT_PCT", "0.015"))

# Slack Alerts
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
SLACK_ALERTS_ENABLED = os.getenv("SLACK_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SLACK_OPPORTUNITY_ALERTS_ENABLED = os.getenv("SLACK_OPPORTUNITY_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SLACK_EXECUTION_ALERTS_ENABLED = os.getenv("SLACK_EXECUTION_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SLACK_ALERT_COOLDOWN_SECONDS = int(os.getenv("SLACK_ALERT_COOLDOWN_SECONDS", "60"))
SLACK_ALERT_USERNAME = os.getenv("SLACK_ALERT_USERNAME", "OmniArb")

# Multi-Symbol Support
#
# The engine discovers eligible symbols at boot by intersecting CSK's live INR
# pairs with Binance's live USDT pairs. No manual list needed.
#
# SYMBOLS_WHITELIST — if non-empty, only these symbols are traded.
#                     Empty = trade everything not blacklisted.
# SYMBOLS_BLACKLIST — always excluded. Add high-spread / low-liquidity / known-
#                     inefficient pairs here. Mirrors simple-arb's approach.
#
# SYMBOLS — fallback used when USE_REST_FALLBACK=1 (no live discovery at boot)
#           and as a seed for offline testing. Keep it reasonably broad.

_wl = os.getenv("SYMBOLS_WHITELIST", "")
_bl = os.getenv("SYMBOLS_BLACKLIST", "")

SYMBOLS_WHITELIST: list[str] = [s.strip().upper() for s in _wl.split(",") if s.strip()]
SYMBOLS_BLACKLIST: list[str] = [s.strip().upper() for s in _bl.split(",") if s.strip()] or [
    # Large-caps: highly efficient, thin arb margin after TDS
    "BTC", "ETH", "SOL", "XRP", "ADA", "AVAX",
    # Meme / ultra-thin books
    "SHIB", "PEPE", "BONK", "FARTCOIN",
    # Stablecoins / not arb-eligible
    "USDC", "USDT", "BUSD", "DAI",
]


# Fallback symbol list — used when dynamic discovery is skipped (REST mode / offline).
SYMBOLS: list[str] = [
    "DOGE", "PARTI", "POL", "FET", "DOT", "TRX", "ICP",
    "LINK", "ACT", "BNB", "PENGU", "ONDO", "UNI",
    "HBAR", "GALA", "ENJ", "NEAR", "SUI", "LINK",
    "RENDER", "HYPE", "ARB", "OP", "INJ",
]

# Performance
POLLING_INTERVAL = 1.5  # seconds between cycles (non-financial, float is fine)
FULL_DEPTH_SYMBOL_LIMIT = int(os.getenv("FULL_DEPTH_SYMBOL_LIMIT", "10"))
PREFILTER_MIN_EDGE_PCT = float(os.getenv("PREFILTER_MIN_EDGE_PCT", "0.02"))
DEPTH_REQUEST_TIMEOUT_SECONDS = float(os.getenv("DEPTH_REQUEST_TIMEOUT_SECONDS", "4"))
DEPTH_REQUEST_RETRIES = int(os.getenv("DEPTH_REQUEST_RETRIES", "1"))
DEPTH_REQUEST_CONCURRENCY = int(os.getenv("DEPTH_REQUEST_CONCURRENCY", "8"))

# Per-cycle Exposure Limits — Decimal for use in financial calculations.
MAX_EXPOSURES: Dict[str, Decimal] = {
    "INR":  Decimal(os.getenv("MAX_EXPOSURE_INR", "25000")),
    "USDT": Decimal(os.getenv("MAX_EXPOSURE_USDT", "250")),
}
DEFAULT_SYMBOL_EXPOSURE = Decimal(os.getenv("DEFAULT_SYMBOL_EXPOSURE", "0.01"))
DEFAULT_SYMBOL_NOTIONAL_INR = Decimal(os.getenv("DEFAULT_SYMBOL_NOTIONAL_INR", "20000"))
DEFAULT_INR_EXPOSURE = Decimal(os.getenv("DEFAULT_INR_EXPOSURE", "25000"))

# Internal sentinel values — Decimal so they compare cleanly with Decimal path yields.
ARBITRAGE_BASE_RETURN = Decimal("1")
ARBITRAGE_MIN_PROFIT_THRESHOLD = Decimal(os.getenv("ARBITRAGE_MIN_PROFIT_THRESHOLD", "0.0001"))

# Strategy switches
THREE_LEG_ENABLED = os.getenv("THREE_LEG_ENABLED", "false").lower() in {"1", "true", "yes"}
# 2-Leg Arb
TWO_LEG_ENABLED = os.getenv("TWO_LEG_ENABLED", "true").lower() in {"1", "true", "yes"}
# Reprice Leg 2 SELL if market moved more than this fraction
REPRICE_THRESHOLD_PCT = Decimal(os.getenv("REPRICE_THRESHOLD_PCT", "0.0005"))  # 0.05%
# Minimum spread to consider a 2-leg opportunity (must clear cost floor)
TWO_LEG_MIN_SPREAD_PCT = Decimal(os.getenv("TWO_LEG_MIN_SPREAD_PCT", "0.015"))  # 1.5%
STUCK_ALERT_AFTER_S = float(os.getenv("STUCK_ALERT_AFTER_S", "60"))

# Liquidity guards — reject symbols with stale/empty books
# Spread above this is almost certainly a dead book, not a real opportunity (e.g. XVS 103%)
TWO_LEG_MAX_SPREAD_SANITY = Decimal(os.getenv("TWO_LEG_MAX_SPREAD_SANITY", "0.15"))   # 15%
# Book must offer at least this much notional depth for the winning direction
TWO_LEG_MIN_NOTIONAL_INR  = Decimal(os.getenv("TWO_LEG_MIN_NOTIONAL_INR",  "2000"))   # ₹2,000

# n8n / Webhook integration
# Set N8N_WEBHOOK_URL to the webhook trigger URL from your n8n workflow.
# All events are posted as JSON: {"event": "<type>", ...fields}
N8N_WEBHOOK_URL     = os.getenv("N8N_WEBHOOK_URL", "").strip()
N8N_WEBHOOK_ENABLED = os.getenv("N8N_WEBHOOK_ENABLED", "false").lower() in {"1", "true", "yes"}

# Control API — allows n8n (or curl) to change strategy params at runtime.
# Binds to localhost only; never exposed to the network.
CONTROL_API_ENABLED = os.getenv("CONTROL_API_ENABLED", "false").lower() in {"1", "true", "yes"}
CONTROL_API_HOST    = os.getenv("CONTROL_API_HOST", "127.0.0.1")
CONTROL_API_PORT    = int(os.getenv("CONTROL_API_PORT", "8765"))
CONTROL_API_SECRET  = os.getenv("CONTROL_API_SECRET", "")

# Rebalancer
REBALANCER_ENABLED = os.getenv("REBALANCER_ENABLED", "true").lower() in {"1", "true", "yes"}
REBALANCER_USDT_FLOOR_PCT = Decimal(os.getenv("REBALANCER_USDT_FLOOR_PCT", "0.20"))
REBALANCER_USDT_TARGET_PCT = Decimal(os.getenv("REBALANCER_USDT_TARGET_PCT", "0.35"))

# Shadow Portfolio
SHADOW_PORTFOLIO_TOTAL_INR = Decimal(os.getenv("SHADOW_PORTFOLIO_TOTAL_INR", "1000000"))
SHADOW_INR_RESERVE_PCT = Decimal(os.getenv("SHADOW_INR_RESERVE_PCT", "0.20"))
SHADOW_USDT_RESERVE_PCT = Decimal(os.getenv("SHADOW_USDT_RESERVE_PCT", "0.10"))
SHADOW_TOKEN_WEIGHTS: Dict[str, Decimal] = {}


# ── helpers ───────────────────────────────────────────────────────────────────

def _best_shadow_price_raw(book: dict) -> Decimal:
    """Mid-price from a raw {"bids": [...], "asks": [...]} dict. Returns Decimal."""
    bids = book.get("bids", []) if book else []
    asks = book.get("asks", []) if book else []
    try:
        best_bid = Decimal(str(bids[0][0])) if bids else Decimal(0)
    except Exception:
        best_bid = Decimal(0)
    try:
        best_ask = Decimal(str(asks[0][0])) if asks else Decimal(0)
    except Exception:
        best_ask = Decimal(0)

    if best_bid > 0 and best_ask > 0:
        return (best_bid + best_ask) / 2
    return best_bid or best_ask


def build_initial_shadow_balances(
    symbols: List[str], tri_books: dict,
) -> Dict[str, Decimal]:
    """Build a shadow portfolio from config-defined allocation.

    `tri_books` is dict[str, TriBook]. Accepts the TriBook type from core.models
    without importing it here (avoids circular imports).
      - SHADOW_INR_RESERVE_PCT kept as INR
      - SHADOW_USDT_RESERVE_PCT converted to USDT at current rate
      - Remainder allocated across symbols using SHADOW_TOKEN_WEIGHTS
    """
    balances: Dict[str, Decimal] = {symbol: Decimal(0) for symbol in symbols}

    inr_reserve = SHADOW_PORTFOLIO_TOTAL_INR * SHADOW_INR_RESERVE_PCT
    usdt_budget_inr = SHADOW_PORTFOLIO_TOTAL_INR * SHADOW_USDT_RESERVE_PCT
    token_budget_inr = max(SHADOW_PORTFOLIO_TOTAL_INR - inr_reserve - usdt_budget_inr, Decimal(0))

    configured_weight_sum = sum(SHADOW_TOKEN_WEIGHTS.get(s, Decimal(0)) for s in symbols)
    unweighted_symbols = [s for s in symbols if s not in SHADOW_TOKEN_WEIGHTS]
    fallback_weight = Decimal(1) / len(symbols) if symbols else Decimal(0)

    first_book = tri_books.get(symbols[0]) if symbols else None
    usdt_price_inr = first_book.usdt_inr.mid if first_book is not None else Decimal(0)
    balances["USDT"] = usdt_budget_inr / usdt_price_inr if usdt_price_inr > 0 else Decimal(0)

    unallocated_inr = Decimal(0)
    for symbol in symbols:
        if Decimal(0) < configured_weight_sum < Decimal(1):
            if symbol in SHADOW_TOKEN_WEIGHTS:
                symbol_weight = SHADOW_TOKEN_WEIGHTS[symbol]
            else:
                residual = Decimal(1) - configured_weight_sum
                symbol_weight = residual / len(unweighted_symbols) if unweighted_symbols else Decimal(0)
        elif configured_weight_sum >= Decimal(1):
            symbol_weight = SHADOW_TOKEN_WEIGHTS.get(symbol, Decimal(0)) / configured_weight_sum
        else:
            symbol_weight = fallback_weight

        symbol_budget_inr = token_budget_inr * symbol_weight
        book = tri_books.get(symbol)
        symbol_price_inr = book.s_inr.mid if book is not None else Decimal(0)
        if symbol_price_inr > 0:
            balances[symbol] = symbol_budget_inr / symbol_price_inr
        else:
            balances[symbol] = Decimal(0)
            unallocated_inr += symbol_budget_inr

    balances["INR"] = inr_reserve + unallocated_inr
    return balances


def log_config() -> None:
    """Log the active config. Call once at startup from main.py, not on import."""
    log.info("--- Config Loaded ---")
    log.info("Taker Fee: %s%% (config default — actual fetched from API at boot)", TAKER_FEE * 100)
    log.info("TDS Rate:  %s%%", TDS_RATE * 100)
    log.info("Symbol discovery: whitelist=%s  blacklist=%d symbols",
             SYMBOLS_WHITELIST or "all", len(SYMBOLS_BLACKLIST))
    log.info("Shadow Portfolio: ₹%s", f"{float(SHADOW_PORTFOLIO_TOTAL_INR):,.0f}")
    log.info(
        "INR Reserve: %s%% | USDT Reserve: %s%%",
        SHADOW_INR_RESERVE_PCT * 100,
        SHADOW_USDT_RESERVE_PCT * 100,
    )
    log.info("Slack Alerts: %s", "On" if SLACK_WEBHOOK_URL and SLACK_ALERTS_ENABLED else "Off")
    log.info(
        "n8n Webhook: %s  Control API: %s",
        f"On ({N8N_WEBHOOK_URL})" if N8N_WEBHOOK_ENABLED and N8N_WEBHOOK_URL else "Off",
        f"On (:{CONTROL_API_PORT})" if CONTROL_API_ENABLED else "Off",
    )
