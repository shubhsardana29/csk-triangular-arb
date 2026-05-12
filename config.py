import logging
import os
from decimal import Decimal
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ── API Credentials ───────────────────────────────────────────────────────────

COINSWITCH_API_KEY    = os.getenv("COINSWITCH_API_KEY")
COINSWITCH_SECRET_KEY = os.getenv("COINSWITCH_SECRET_KEY") or os.getenv("COINSWITCH_API_SECRET")

# ── Shared Trading Costs ──────────────────────────────────────────────────────
# All Decimal — never float in financial math.

TAKER_FEE = Decimal(os.getenv("TAKER_FEE", "0.001"))   # 0.1% per leg
TDS_RATE  = Decimal(os.getenv("TDS_RATE",  "0.01"))    # 1% per sell leg (Indian withholding)

# ── Symbol Universe ───────────────────────────────────────────────────────────
#
# Engine discovers eligible symbols at boot: CSK live INR pairs ∩ Binance live
# USDT pairs, filtered by WHITELIST / BLACKLIST.
#
# SYMBOLS_WHITELIST — if non-empty, only these symbols are traded.
# SYMBOLS_BLACKLIST — always excluded.
# SYMBOLS           — fallback when USE_REST_FALLBACK=1 or for offline tests.

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

SYMBOLS: list[str] = [
    "DOGE", "PARTI", "POL", "FET", "DOT", "TRX", "ICP",
    "LINK", "ACT", "BNB", "PENGU", "ONDO", "UNI",
    "HBAR", "GALA", "ENJ", "NEAR", "SUI", "LINK",
    "RENDER", "HYPE", "ARB", "OP", "INJ",
]

# ── Engine Performance ────────────────────────────────────────────────────────

POLLING_INTERVAL           = 1.5   # seconds between REST cycles (float is fine — non-financial)
FULL_DEPTH_SYMBOL_LIMIT    = int(os.getenv("FULL_DEPTH_SYMBOL_LIMIT",    "10"))
PREFILTER_MIN_EDGE_PCT     = float(os.getenv("PREFILTER_MIN_EDGE_PCT",   "0.02"))
DEPTH_REQUEST_TIMEOUT_SECONDS = float(os.getenv("DEPTH_REQUEST_TIMEOUT_SECONDS", "4"))
DEPTH_REQUEST_RETRIES      = int(os.getenv("DEPTH_REQUEST_RETRIES",      "1"))
DEPTH_REQUEST_CONCURRENCY  = int(os.getenv("DEPTH_REQUEST_CONCURRENCY",  "8"))
# Skip the entire tick if USDT/INR rate hasn't updated within this window.
# Stale rate → wrong fair price → false 2-leg signals.
USDT_INR_MAX_AGE_S         = float(os.getenv("USDT_INR_MAX_AGE_S",       "5.0"))

# ── Exposure Limits ───────────────────────────────────────────────────────────
# Decimal — used directly in financial calculations.

MAX_EXPOSURES: Dict[str, Decimal] = {
    "INR":  Decimal(os.getenv("MAX_EXPOSURE_INR",  "25000")),
    "USDT": Decimal(os.getenv("MAX_EXPOSURE_USDT", "250")),
}
DEFAULT_SYMBOL_EXPOSURE     = Decimal(os.getenv("DEFAULT_SYMBOL_EXPOSURE",    "0.01"))
DEFAULT_SYMBOL_NOTIONAL_INR = Decimal(os.getenv("DEFAULT_SYMBOL_NOTIONAL_INR","20000"))
DEFAULT_INR_EXPOSURE        = Decimal(os.getenv("DEFAULT_INR_EXPOSURE",       "25000"))

# ══════════════════════════════════════════════════════════════════════════════
# 2-LEG SPREAD ARB
#
# Strategy: compare CSK S/INR price against Binance S/USDT × CSK USDT/INR
# (fair price). Trade the dislocation in whichever direction is profitable.
#
# Cost model: 2× taker fee (0.1%) + 1× TDS (1%) = 1.2% floor.
# MIN_SPREAD threshold is set above floor at 1.5% to include a safety buffer.
# ══════════════════════════════════════════════════════════════════════════════

TWO_LEG_ENABLED = os.getenv("TWO_LEG_ENABLED", "true").lower() in {"1", "true", "yes"}

# Opportunity gate — must exceed cost floor (1.2%) plus safety buffer.
TWO_LEG_MIN_SPREAD_PCT = Decimal(os.getenv("TWO_LEG_MIN_SPREAD_PCT", "0.015"))  # 1.5%

# Leg 2 cost floor safety — Leg 2 SELL is never placed below:
#   buy_avg × (1 + fee + TDS + MIN_PROFIT_PCT)
# Keeps repricing from locking in a loss.
MIN_PROFIT_PCT = Decimal(os.getenv("MIN_PROFIT_PCT", "0.015"))  # 1.5%

# Reprice Leg 2 only when market has moved more than this fraction.
REPRICE_THRESHOLD_PCT = Decimal(os.getenv("REPRICE_THRESHOLD_PCT", "0.0005"))  # 0.05%

# Log a "stuck" warning if Leg 2 hasn't filled after this many seconds.
STUCK_ALERT_AFTER_S = float(os.getenv("STUCK_ALERT_AFTER_S", "60"))

# Cut Leg 2 if market drops this fraction below cost floor — accept a known loss
# rather than risk an unbounded one waiting for market to recover.
TWO_LEG_STOP_LOSS_PCT = Decimal(os.getenv("TWO_LEG_STOP_LOSS_PCT", "0.02"))  # 2% below floor

# Liquidity guards — reject symbols with stale or empty books.
# Spread > 15%: almost certainly a dead book, not a real opportunity.
TWO_LEG_MAX_SPREAD_SANITY = Decimal(os.getenv("TWO_LEG_MAX_SPREAD_SANITY", "0.15"))  # 15%
# Book must offer at least this much notional depth on the winning side.
TWO_LEG_MIN_NOTIONAL_INR  = Decimal(os.getenv("TWO_LEG_MIN_NOTIONAL_INR",  "2000"))  # ₹2,000

# ══════════════════════════════════════════════════════════════════════════════
# 3-LEG TRIANGULAR ARB
#
# Strategy: exploit pricing inconsistency across CSK's three internal markets:
#   S/INR ↔ S/USDT ↔ USDT/INR
# Four paths evaluated per symbol per tick; best path is taken.
#
# Cost model: 3× taker fee (0.1%) + 2× TDS (1%) = ~2.28% floor.
# Threshold is set at 2.3% (floor + small buffer).
# ══════════════════════════════════════════════════════════════════════════════

THREE_LEG_ENABLED = os.getenv("THREE_LEG_ENABLED", "false").lower() in {"1", "true", "yes"}

# Opportunity gate and Leg 3 cost-floor check.
# Any path below this is a guaranteed loss after fees + TDS.
ARBITRAGE_MIN_PROFIT_THRESHOLD = Decimal(os.getenv("ARBITRAGE_MIN_PROFIT_THRESHOLD", "0.023"))  # 2.3%

# Internal sentinel — yield ratios compare against this base.
ARBITRAGE_BASE_RETURN = Decimal("1")

# ── Integrations ──────────────────────────────────────────────────────────────

# Slack — fires on every trade and opportunity alert.
SLACK_WEBHOOK_URL                  = os.getenv("SLACK_WEBHOOK_URL", "").strip()
SLACK_ALERTS_ENABLED               = os.getenv("SLACK_ALERTS_ENABLED",             "true").lower() in {"1", "true", "yes", "on"}
SLACK_OPPORTUNITY_ALERTS_ENABLED   = os.getenv("SLACK_OPPORTUNITY_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SLACK_EXECUTION_ALERTS_ENABLED     = os.getenv("SLACK_EXECUTION_ALERTS_ENABLED",   "true").lower() in {"1", "true", "yes", "on"}
SLACK_ALERT_COOLDOWN_SECONDS       = int(os.getenv("SLACK_ALERT_COOLDOWN_SECONDS", "60"))
SLACK_ALERT_USERNAME               = os.getenv("SLACK_ALERT_USERNAME", "OmniArb")

# n8n — fire-and-forget webhook; n8n calls back via Control API.
N8N_WEBHOOK_URL     = os.getenv("N8N_WEBHOOK_URL", "").strip()
N8N_WEBHOOK_ENABLED = os.getenv("N8N_WEBHOOK_ENABLED", "false").lower() in {"1", "true", "yes"}

# Control API — runtime param mutation via HTTP. Localhost only.
CONTROL_API_ENABLED = os.getenv("CONTROL_API_ENABLED", "false").lower() in {"1", "true", "yes"}
CONTROL_API_HOST    = os.getenv("CONTROL_API_HOST", "127.0.0.1")
CONTROL_API_PORT    = int(os.getenv("CONTROL_API_PORT", "8765"))
CONTROL_API_SECRET  = os.getenv("CONTROL_API_SECRET", "")

# ── Rebalancer ────────────────────────────────────────────────────────────────
# Passive maker BUY on USDT/INR when USDT drops below floor. Real mode only.

REBALANCER_ENABLED         = os.getenv("REBALANCER_ENABLED", "true").lower() in {"1", "true", "yes"}
REBALANCER_USDT_FLOOR_PCT  = Decimal(os.getenv("REBALANCER_USDT_FLOOR_PCT",  "0.20"))
REBALANCER_USDT_TARGET_PCT = Decimal(os.getenv("REBALANCER_USDT_TARGET_PCT", "0.35"))

# ── Shadow Portfolio ──────────────────────────────────────────────────────────
# In-memory simulated portfolio for paper trading. Not persisted across restarts.

SHADOW_PORTFOLIO_TOTAL_INR = Decimal(os.getenv("SHADOW_PORTFOLIO_TOTAL_INR", "1000000"))
SHADOW_INR_RESERVE_PCT     = Decimal(os.getenv("SHADOW_INR_RESERVE_PCT",     "0.20"))
SHADOW_USDT_RESERVE_PCT    = Decimal(os.getenv("SHADOW_USDT_RESERVE_PCT",    "0.10"))
SHADOW_TOKEN_WEIGHTS: Dict[str, Decimal] = {}


# ── helpers ───────────────────────────────────────────────────────────────────

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

    inr_reserve      = SHADOW_PORTFOLIO_TOTAL_INR * SHADOW_INR_RESERVE_PCT
    usdt_budget_inr  = SHADOW_PORTFOLIO_TOTAL_INR * SHADOW_USDT_RESERVE_PCT
    token_budget_inr = max(SHADOW_PORTFOLIO_TOTAL_INR - inr_reserve - usdt_budget_inr, Decimal(0))

    configured_weight_sum = sum(SHADOW_TOKEN_WEIGHTS.get(s, Decimal(0)) for s in symbols)
    unweighted_symbols    = [s for s in symbols if s not in SHADOW_TOKEN_WEIGHTS]
    fallback_weight       = Decimal(1) / len(symbols) if symbols else Decimal(0)

    first_book      = tri_books.get(symbols[0]) if symbols else None
    usdt_price_inr  = first_book.usdt_inr.mid if first_book is not None else Decimal(0)
    balances["USDT"] = usdt_budget_inr / usdt_price_inr if usdt_price_inr > 0 else Decimal(0)

    unallocated_inr = Decimal(0)
    for symbol in symbols:
        if Decimal(0) < configured_weight_sum < Decimal(1):
            if symbol in SHADOW_TOKEN_WEIGHTS:
                symbol_weight = SHADOW_TOKEN_WEIGHTS[symbol]
            else:
                residual      = Decimal(1) - configured_weight_sum
                symbol_weight = residual / len(unweighted_symbols) if unweighted_symbols else Decimal(0)
        elif configured_weight_sum >= Decimal(1):
            symbol_weight = SHADOW_TOKEN_WEIGHTS.get(symbol, Decimal(0)) / configured_weight_sum
        else:
            symbol_weight = fallback_weight

        symbol_budget_inr = token_budget_inr * symbol_weight
        book              = tri_books.get(symbol)
        symbol_price_inr  = book.s_inr.mid if book is not None else Decimal(0)
        if symbol_price_inr > 0:
            balances[symbol] = symbol_budget_inr / symbol_price_inr
        else:
            balances[symbol]  = Decimal(0)
            unallocated_inr  += symbol_budget_inr

    balances["INR"] = inr_reserve + unallocated_inr
    return balances


def log_config() -> None:
    """Log the active config. Call once at startup from main.py, not on import."""
    log.info("--- Config Loaded ---")
    log.info("Taker Fee: %s%% (default — actual fetched from API at boot)", TAKER_FEE * 100)
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
    log.info(
        "2-leg: %s  min_spread=%.1f%%  |  3-leg: %s  min_profit=%.1f%%",
        "ON" if TWO_LEG_ENABLED else "OFF", float(TWO_LEG_MIN_SPREAD_PCT) * 100,
        "ON" if THREE_LEG_ENABLED else "OFF", float(ARBITRAGE_MIN_PROFIT_THRESHOLD) * 100,
    )
