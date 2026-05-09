# CoinSwitch Triangular Arbitrage Engine

A production-grade multi-symbol arbitrage engine for CoinSwitch. Runs two strategies simultaneously:

- **3-leg triangular arb** — exploits price gaps across S/INR, S/USDT, and USDT/INR books
- **2-leg spread arb** — exploits INR vs USDT price divergence on the same asset

Consumes live WebSocket market data at 10Hz, applies exact fee + TDS accounting, and supports paper trading and real limit-order execution.

---

## How It Works

### Strategy 1 — 3-Leg Triangular Arbitrage

For any token S (e.g. DOGE), three markets form a triangle: `S/INR` on CSK, `S/USDT` on Binance (via CSK C2C), and `USDT/INR` on CSK. When the implied prices disagree by more than costs, profit exists.

The engine evaluates **4 paths** per symbol every 100ms:

```
Path 1 — token-start, INR leg cheap:
  SELL S/INR  →  BUY USDT/INR  →  BUY S/USDT
  Start and end with S.

Path 2 — token-start, USDT leg cheap:
  SELL S/USDT  →  SELL USDT/INR  →  BUY S/INR
  Start and end with S.

Path 3 — INR-start, INR leg cheap:
  BUY S/INR  →  SELL S/USDT  →  SELL USDT/INR
  Start and end with INR.

Path 4 — INR-start, USDT leg cheap:
  BUY USDT/INR  →  BUY S/USDT  →  SELL S/INR
  Start and end with INR.
```

**Execution:** Leg 1 placed immediately. Leg 2 placed after Leg 1 fills. Leg 3 placed after Leg 2 fills. Before Leg 3 is placed, a **cost floor check** verifies the current book can still deliver profit — if not, the trade is aborted.

### Strategy 2 — 2-Leg Spread Arbitrage

Compares the CSK INR price of a token against the fair value derived from Binance USDT × USDT/INR rate. When they diverge beyond costs, profit exists with just 2 orders.

```
INR_CHEAP:     BUY S/INR on CSK  →  SELL S/USDT on CSK C2C
INR_EXPENSIVE: BUY S/USDT on CSK C2C  →  SELL S/INR on CSK
```

**Execution:** Leg 1 placed immediately. Leg 2 placed after Leg 1 fills at `max(cost_floor, market_bid)`. Leg 2 is **repriced every tick** — if the market moves up, the sell price follows it. If it drops below the cost floor, the order rests at the floor and the operator is alerted after 60 seconds.

### Position Locking

Both strategies share a position lock. Only **one trade per symbol** runs at a time — whether 3-leg or 2-leg. A symbol is locked when a trade starts and released as soon as it settles. This prevents overexposure and double-entry.

### Cost Model

| Cost | Rate | Applied to |
|---|---|---|
| Taker fee | 0.1% per leg | Every order |
| TDS | 1% per VDA sale | 1–2 sell legs per path |
| Safety buffer | ~0.2% | Slippage, price movement |
| **Breakeven floor** | **~1.5%** | Must beat this to profit |

---

## Architecture

```
Market Data (100ms)
├── feeds/binance_depth_ws.py   — S/USDT depth via Binance WS (@depth20@100ms)
└── feeds/csk_public_ws.py      — S/INR + USDT/INR via CSK socket.io

Strategy (10Hz tick loop)
├── strategy/tri_ranker.py       — stateless 4-path triangular VWAP scorer
├── strategy/two_leg_ranker.py   — stateless 2-leg spread scorer (INR vs Binance fair price)
├── strategy/shadow_executor.py  — paper fills on in-memory balances (3-leg + 2-leg via ShadowTwoLegExecutor)
├── strategy/tri_executor.py     — real 3-leg sequential limit orders + boot recovery
├── strategy/two_leg_executor.py — real 2-leg orders with cost-floor repricing
├── strategy/tri_rebalancer.py   — passive maker USDT/INR restorer
├── strategy/order_poller.py     — 1Hz/10Hz REST fill detection
└── strategy/tri_engine.py       — orchestration: feeds, position lock, watchdog, cancel-all

Core Types (core/models.py)
├── Depth         — immutable order book snapshot (Decimal levels, VWAP walkers)
├── TriBook       — 3 Depth objects per symbol (s_inr, s_usdt, usdt_inr)
├── PathResult    — one evaluated 3-leg path (all Decimal)
├── TwoLegResult  — one evaluated 2-leg opportunity (all Decimal)
├── TriIntent     — in-flight 3-leg execution state
└── TwoLegIntent  — in-flight 2-leg execution state (tracks cost floor)

API / Wiring
├── api_client.py  — CSK REST client (Ed25519 auth, discover_symbols, orders, balances)
├── main.py        — CLI entry point
└── dashboard.py   — aiohttp web server + SSE dashboard on :8080
```

All financial arithmetic uses `Decimal` — no float drift. Strategy code never imports feeds or `api_client` — they are injected at the wiring layer.

---

## Production Safety Features

| Feature | What it does |
|---|---|
| **Position lock** | One open trade per symbol at a time — prevents double-entry |
| **Staleness cancel-all** | If either WS feed goes silent >15s, all open orders are cancelled immediately |
| **Boot cancel** | All open orders from a previous run are cancelled before the engine starts |
| **Boot recovery** | If the bot crashed mid-3-leg (leaving unexpected token balances), it detects unmatched BUY orders in the last 2 hours and places a liquidating SELL |
| **Cost floor (3-leg)** | Before placing Leg 3, verifies current book depth still delivers profit — aborts if not |
| **Cost floor (2-leg)** | Leg 2 SELL never placed below `buy_avg × (1 + fee + TDS + safety)` |
| **Leg timeout** | Any leg not filled within 30s is cancelled; partial proceeds continue if available |
| **Stuck alert** | If Leg 2 (2-leg) is resting at the cost floor for >60s, a warning is logged |
| **Rebalancer** | Passive maker BUY on USDT/INR when USDT share drops below 20% of portfolio; 30s cooldown after failed placement to prevent API hammering |

---

## Setup

### 1. Install dependencies

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
# Required
COINSWITCH_API_KEY=your_api_key
COINSWITCH_SECRET_KEY=your_hex_ed25519_secret

# Trading costs — defaults shown
TAKER_FEE=0.001
TDS_RATE=0.01
MIN_PROFIT_PCT=0.015

# Exposure caps per cycle
MAX_EXPOSURE_INR=25000
MAX_EXPOSURE_USDT=250

# Shadow portfolio size (paper trading)
SHADOW_PORTFOLIO_TOTAL_INR=1000000

# Symbol filtering
SYMBOLS_BLACKLIST=BTC,ETH,SOL,XRP,ADA,AVAX,SHIB,PEPE,BONK,USDC,BUSD,DAI
# SYMBOLS_WHITELIST=DOGE,NEAR,SUI   # uncomment to narrow to specific symbols

# Execution mode
# EXECUTION_MODE=shadow   ← paper trades only (default)
# EXECUTION_MODE=real     ← places real limit orders on CSK

# Strategy switches
THREE_LEG_ENABLED=false       # run 3-leg triangular arb (default false — 2-leg only)
TWO_LEG_ENABLED=true          # run 2-leg spread arb alongside 3-leg
REBALANCER_ENABLED=true       # maintain USDT balance passively (only active in real mode)
REBALANCER_USDT_FLOOR_PCT=0.20  # trigger rebalance below 20% USDT share
REBALANCER_USDT_TARGET_PCT=0.35 # restore to 35%
TWO_LEG_MIN_SPREAD_PCT=0.015    # minimum spread for 2-leg opportunity
REPRICE_THRESHOLD_PCT=0.0005    # reprice 2-leg Leg 2 if market moves >0.05%
STUCK_ALERT_AFTER_S=60          # log alert if 2-leg Leg 2 stuck at floor >60s

# REST fallback — disable WebSocket, poll every 1.5s instead
# USE_REST_FALLBACK=1

# Slack alerts (optional)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_ALERTS_ENABLED=true
SLACK_ALERT_COOLDOWN_SECONDS=60
```

### 3. Run

---

#### Option 1 — REST fallback, shadow mode (safest, good for first test)

No WebSocket. Polls CSK REST every 1.5s. Uses the fallback symbol list from `config.py`. Run this first to verify credentials work.

```bash
USE_REST_FALLBACK=1 python3 main.py
```

---

#### Option 2 — WebSocket mode, shadow trading (recommended for monitoring)

Live 10Hz market data. Discovers all eligible symbols at boot (~100–300 depending on CSK's listings). Paper trades only — no real orders.

```bash
python3 main.py
```

**Recommended: set a whitelist** to keep the symbol count manageable:

```env
SYMBOLS_WHITELIST=DOGE,NEAR,LINK,FET,POL,DOT,HBAR,SUI,INJ,RENDER,ARB,GALA,ENJ,UNI,ONDO,TRX,ICP
```

---

#### Option 3 — Dashboard (shadow mode with live UI)

```bash
python3 dashboard.py
# open http://localhost:8080
```

Per-symbol cards show:
- Best 3-leg spread (net after fees + TDS) and which path
- 2-leg spread (INR_CHEAP / INR_EXPENSIVE direction and spread %)
- Shadow P&L sparkline
- Recent opportunity and execution events

---

#### Option 4 — Both strategies, shadow mode

Run 3-leg and 2-leg simultaneously in paper mode. 3-leg is off by default; enable it explicitly.

```bash
THREE_LEG_ENABLED=true python3 main.py
```

Both `ShadowExecutor` (3-leg) and `ShadowTwoLegExecutor` (2-leg) share the same paper portfolio. The position lock ensures only one trade per symbol runs at a time regardless of strategy.

---

#### Option 5 — Real order execution (live trading)

Places real limit orders. Only enable after confirming consistently positive shadow P&L over several weeks.

```bash
EXECUTION_MODE=real python3 main.py
# or with dashboard:
EXECUTION_MODE=real python3 dashboard.py
```

What happens when an opportunity is detected:
- **3-leg**: Leg 1 placed immediately → Leg 2 after fill → cost floor checked → Leg 3 after fill
- **2-leg**: Leg 1 (BUY) placed → Leg 2 (SELL) placed above cost floor after fill → Leg 2 repriced every tick
- On crash/restart: boot recovery detects unmatched BUYs and liquidates stranded positions

---

#### Summary

| Option | Command | Data | Executes | Use when |
|---|---|---|---|---|
| 1 | `USE_REST_FALLBACK=1 python3 main.py` | REST 1.5s | Paper | First test, offline dev |
| 2 | `python3 main.py` | WS 100ms | Paper | 2-leg only (default) |
| 3 | `python3 dashboard.py` | WS 100ms | Paper | 2-leg only with UI |
| 4 | `THREE_LEG_ENABLED=true python3 main.py` | WS 100ms | Paper | Both strategies |
| 5 | `EXECUTION_MODE=real python3 main.py` | WS 100ms | Real orders | Live trading |

---

## Profitability Assessment

### What works in your favour

- **10Hz market data** via WebSocket — you see the opportunity before REST-based scanners
- **Two strategies simultaneously** — 2-leg opportunities are more frequent and faster to execute than 3-leg
- **VWAP-aware sizing** — thin books are marked non-executable, not acted on
- **Dynamic symbol discovery** — evaluates every eligible symbol automatically at boot
- **Cost floor enforcement** — never places a trade that can't break even at current prices

### What works against you

**1. TDS is the primary obstacle.**
India's 1% TDS on every VDA sale applies to 1–2 legs per path. That alone costs 1–2% per cycle. Your breakeven floor is ~1.5%. In calm markets, spreads this wide are uncommon on liquid pairs.

**2. Sequential fills create execution risk.**
Leg 2 fires only after Leg 1 fills — often 1–5 seconds later. The spread can disappear before all legs complete. The 2-leg strategy has less execution risk than 3-leg (one fewer leg), but the same issue applies.

**3. CSK USDT prices track Binance tightly.**
The C2C system links CSK USDT prices to Binance in near real-time. The gap this engine exploits closes automatically most of the time. Opportunities appear during volatility spikes, listings, and market dislocations.

**4. Shadow P&L overstates real P&L.**
Shadow fills assume best-bid/ask at the moment of detection. Real fills depend on queue position, partial fills, and market movement between legs. Expect real results to be 10–30% worse.

### Realistic expectations

| Scenario | Likely outcome |
|---|---|
| Shadow mode, calm markets | Opportunities detected; net P&L near zero |
| Shadow mode, volatile event | Clear positive signal — arb genuinely works during dislocations |
| Real mode, calm markets | Breakeven to slightly negative after real fill quality |
| Real mode, volatile event | Positive — if all legs complete before the spread closes |

**The recommendation:** Run shadow mode for at least 2–3 weeks across different market conditions. If cumulative shadow P&L is consistently positive across multiple symbols, the edge is real. If barely positive, real will be negative.

---

## Symbol Discovery

At boot the engine intersects CSK's live INR pairs with Binance's live USDT pairs, excludes the blacklist, and trades every eligible symbol automatically. Typically 100–300 symbols found.

```env
# Always excluded (default blacklist in config.py)
SYMBOLS_BLACKLIST=BTC,ETH,SOL,XRP,ADA,AVAX,SHIB,PEPE,BONK,USDC,BUSD,DAI

# Optional: restrict to specific symbols only
SYMBOLS_WHITELIST=DOGE,NEAR,SUI,LINK
```

`config.SYMBOLS` is a fallback used only when `USE_REST_FALLBACK=1`.

---

## Project Structure

```
.
├── main.py                        # CLI entry point (wiring only)
├── dashboard.py                   # aiohttp web server + SSE dashboard on :8080
├── api_client.py                  # CSK REST client (auth, depth, orders, balances, recovery)
├── config.py                      # all tunables (fees, symbols, exposure, rebalancer, 2-leg)
├── slack_notifier.py              # Slack webhook alerts with cooldown
├── requirements.txt
├── core/
│   ├── models.py                  # Depth, TriBook, PathResult, TwoLegResult, intents (Decimal)
│   └── protocol.py                # ExchangeAdapter Protocol
├── feeds/
│   ├── binance_depth_ws.py        # Binance @depth20@100ms WebSocket feed
│   └── csk_public_ws.py           # CSK socket.io depth feed (S/INR + USDT/INR)
├── strategy/
│   ├── tri_ranker.py              # stateless 4-path triangular scorer
│   ├── two_leg_ranker.py          # stateless 2-leg spread scorer (vs Binance fair price)
│   ├── shadow_executor.py         # paper portfolio simulator (3-leg)
│   ├── tri_executor.py            # real 3-leg sequential orders + cost floor + boot recovery
│   ├── two_leg_executor.py        # real 2-leg orders with repricing + cost floor + stuck alert
│   ├── tri_rebalancer.py          # passive USDT/INR maker to maintain USDT balance
│   ├── order_poller.py            # 1Hz idle / 10Hz active REST fill detection
│   └── tri_engine.py              # orchestration: position lock, staleness cancel-all, watchdog
├── scripts/
│   └── list_pairs.py              # symbol discovery debug tool
└── static/
    └── index.html                 # SSE dashboard UI
```

---

## Safety Notes

- Always run shadow mode first. Only enable `EXECUTION_MODE=real` after weeks of positive shadow P&L.
- Never commit your `COINSWITCH_SECRET_KEY` to version control.
- In real mode: if the bot crashes between Leg 1 and Leg 2, boot recovery will attempt to liquidate the stranded position on the next restart. Check logs for `[executor] recovery:` messages.
- The rebalancer places passive maker orders — it will not fill instantly. Monitor USDT balance if running many INR-start trades.
- Partial positions from timed-out legs are not guaranteed to be recovered automatically if the CSK orders API is unavailable at boot. Check `[executor] recovery: could not fetch recent orders` in logs.


Trading Fee
import requests
import json

url = "https://coinswitch.co/trade/api/v2/tradingFee"

params = {
  "exchange": "coinswitchx",
}

headers = {
  'Content-Type': 'application/json',
  'X-AUTH-APIKEY': <api-key>
  'X-AUTH-SIGNATURE': <signature>
}

response = requests.request("GET", url, headers=headers, params=params)


The above command returns JSON structured like this:

{
  "data": {
    "coinswitchx": {
      "AVAX": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "AXP": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "AXS": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "ETH": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "FIL": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "PENDLE": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "SHIB": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "USDT": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      },
      "YFI": {
        "maker_fee": 0.0009,
        "taker_fee": 0.0009,
        "maker_discount_percentage": 100,
        "taker_discount_percentage": 100,
        "maker_fee_after_discount": 0,
        "taker_fee_after_discount": 0,
        "timestamp": 1721909805
      }
    }
  }
}
Use the following endpoint to check the trading fee applicable to you for an exchange:

HTTP Request
METHOD GET
ENDPOINT https://coinswitch.co/trade/api/v2/tradingFee
Request Parameters
Parameter	Type	Mandatory	Description
exchange	string	Yes	(case insensitive) Allowed values: "coinswitchx"/ "wazirx" / "c2c1"/ "c2c2"
