import os
from typing import Dict, List
from dotenv import load_dotenv

load_dotenv()

# API Credentials
COINSWITCH_API_KEY = os.getenv("COINSWITCH_API_KEY")
COINSWITCH_SECRET_KEY = os.getenv("COINSWITCH_SECRET_KEY") or os.getenv("COINSWITCH_API_SECRET")

# Trading Costs
# 0.001 = 0.1% taker fee
TAKER_FEE = float(os.getenv("TAKER_FEE", 0.001))
# 0.01 = 1% TDS
TDS_RATE = float(os.getenv("TDS_RATE", 0.01))

# Slack Alerts
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
SLACK_ALERTS_ENABLED = os.getenv("SLACK_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SLACK_OPPORTUNITY_ALERTS_ENABLED = os.getenv("SLACK_OPPORTUNITY_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SLACK_EXECUTION_ALERTS_ENABLED = os.getenv("SLACK_EXECUTION_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SLACK_ALERT_COOLDOWN_SECONDS = int(os.getenv("SLACK_ALERT_COOLDOWN_SECONDS", "60"))
SLACK_ALERT_USERNAME = os.getenv("SLACK_ALERT_USERNAME", "OmniArb")

# Multi-Symbol Support
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "TRX", "UNI", "DOT"]

# Performance
POLLING_INTERVAL = 0.5  # Seconds between cycles

# Per-cycle Exposure Limits
MAX_EXPOSURES = {
    "BTC": 0.012,
    "ETH": 0.15,
    "SOL": 5.0,
    "XRP": 1200.0,
    "DOGE": 12000.0,
    "ADA": 1800.0,
    "BNB": 1.2,
    "TRX": 6000.0,
    "UNI": 120.0,
    "DOT": 250.0,
    "INR": 100000.0,
    "USDT": 1000.0
}
DEFAULT_SYMBOL_EXPOSURE = 0.01
DEFAULT_INR_EXPOSURE = 100000.0
ARBITRAGE_BASE_RETURN = 1.0
ARBITRAGE_BEST_SENTINEL = -1.0
ARBITRAGE_MIN_PROFIT_THRESHOLD = 0.0001

# Shadow Portfolio
SHADOW_PORTFOLIO_TOTAL_INR = 1_000_000.0
SHADOW_INR_RESERVE_PCT = 0.20
SHADOW_USDT_RESERVE_PCT = 0.10
SHADOW_TOKEN_WEIGHTS = {
    "BTC": 0.28,
    "ETH": 0.18,
    "SOL": 0.12,
    "XRP": 0.10,
    "DOGE": 0.06,
    "ADA": 0.07,
    "BNB": 0.08,
    "TRX": 0.04,
    "UNI": 0.04,
    "DOT": 0.03,
}


# Helpers
def _best_shadow_price(book: dict) -> float:
    bids = book.get("bids", []) if book else []
    asks = book.get("asks", []) if book else []

    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0

    if best_bid and best_ask:
        return (best_bid + best_ask) / 2.0
    return best_bid or best_ask or 0.0


def build_initial_shadow_balances(symbols: List[str], tri_books: Dict[str, dict]) -> Dict[str, float]:
    """
    Builds a shadow portfolio using config-defined allocation.
    Default allocation:
    - SHADOW_INR_RESERVE_PCT kept as INR
    - SHADOW_USDT_RESERVE_PCT converted to USDT
    - remainder allocated across configured symbols using SHADOW_TOKEN_WEIGHTS
    """
    balances = {symbol: 0.0 for symbol in symbols}

    inr_reserve = SHADOW_PORTFOLIO_TOTAL_INR * SHADOW_INR_RESERVE_PCT
    usdt_budget_inr = SHADOW_PORTFOLIO_TOTAL_INR * SHADOW_USDT_RESERVE_PCT
    token_budget_inr = max(SHADOW_PORTFOLIO_TOTAL_INR - inr_reserve - usdt_budget_inr, 0.0)
    configured_weight_sum = sum(SHADOW_TOKEN_WEIGHTS.get(symbol, 0.0) for symbol in symbols)
    fallback_weight = 1.0 / len(symbols) if symbols else 0.0

    usdt_price_inr = _best_shadow_price(tri_books.get(symbols[0], {}).get("USDT/INR", {})) if symbols else 0.0
    balances["USDT"] = (usdt_budget_inr / usdt_price_inr) if usdt_price_inr > 0 else 0.0

    unallocated_inr = 0.0
    for symbol in symbols:
        if configured_weight_sum > 0:
            symbol_weight = SHADOW_TOKEN_WEIGHTS.get(symbol, 0.0) / configured_weight_sum
        else:
            symbol_weight = fallback_weight
        symbol_budget_inr = token_budget_inr * symbol_weight

        symbol_price_inr = _best_shadow_price(tri_books.get(symbol, {}).get(f"{symbol}/INR", {}))
        if symbol_price_inr > 0:
            balances[symbol] = symbol_budget_inr / symbol_price_inr
        else:
            balances[symbol] = 0.0
            unallocated_inr += symbol_budget_inr

    balances["INR"] = inr_reserve + unallocated_inr
    return balances


# Startup Summary
print(f"--- Config Loaded ---")
print(f"Taker Fee: {TAKER_FEE*100:.2f}%")
print(f"TDS Rate:  {TDS_RATE*100:.2f}%")
print(f"Symbols:   {', '.join(SYMBOLS)}")
print(f"Shadow Portfolio: ₹{SHADOW_PORTFOLIO_TOTAL_INR:,.0f}")
print(f"INR Reserve: {SHADOW_INR_RESERVE_PCT*100:.0f}% | USDT Reserve: {SHADOW_USDT_RESERVE_PCT*100:.0f}%")
print(f"Slack Alerts: {'On' if SLACK_WEBHOOK_URL and SLACK_ALERTS_ENABLED else 'Off'}")
