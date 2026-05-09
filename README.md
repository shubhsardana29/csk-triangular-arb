# CoinSwitch Triangular Arbitrage Engine

A multi-symbol triangular arbitrage scanner and executor for CoinSwitch. Consumes live WebSocket market data at 10Hz, evaluates four triangular paths per token, applies realistic fee and TDS accounting, and supports both paper trading (shadow mode) and real limit-order execution.

---

## How It Works

### The Core Idea

For any token S (e.g. DOGE), there are two ways to arrive at its price:
- **Direct:** Buy DOGE with INR on CSK → `DOGE/INR` market
- **Indirect:** Buy USDT with INR, then buy DOGE with USDT → `USDT/INR` → `DOGE/USDT`

When these two implied prices disagree by more than the cost of trading (fees + TDS), there is a profit to capture by trading through the triangle. The engine finds and executes that gap.

### The 4 Paths (Evaluated Per Symbol, Per Tick)

```
Path 1 — token-start, INR leg cheap:
  SELL S/INR  →  BUY USDT/INR  →  BUY S/USDT
  Start and end with S. Profit = more S than you started with.

Path 2 — token-start, USDT leg cheap:
  SELL S/USDT  →  SELL USDT/INR  →  BUY S/INR
  Start and end with S. Profit = more S than you started with.

Path 3 — INR-start, INR leg cheap:
  BUY S/INR  →  SELL S/USDT  →  SELL USDT/INR
  Start and end with INR. Profit = more INR than you started with.

Path 4 — INR-start, USDT leg cheap:
  BUY USDT/INR  →  BUY S/USDT  →  SELL S/INR
  Start and end with INR. Profit = more INR than you started with.
```

The ranker evaluates all four every 100ms, picks the best net yield, and only fires if it clears the 1.5% breakeven floor.

### Cost Model

Every cycle involves 3 legs. The costs stack:

| Cost | Amount | Applied to |
|---|---|---|
| Taker fee | 0.1% per leg | All 3 legs (= 0.3% total) |
| TDS | 1% per VDA sale | 1–2 sell legs per path |
| Safety buffer | ~0.2% | Slippage, price movement |
| **Breakeven floor** | **~1.5%** | Must beat this to profit |

The ranker applies these deductions in sequence (not additively) so the math reflects how the exchange actually charges them.

### Architecture

```
Market Data Layer (100ms updates)
├── feeds/binance_depth_ws.py  — S/USDT depth via Binance WS (@depth20@100ms)
└── feeds/csk_public_ws.py     — S/INR + USDT/INR depth via CSK socket.io

Strategy Layer (10Hz tick loop)
├── strategy/tri_ranker.py      — stateless: VWAP + 4-path yield math + fee/TDS
├── strategy/shadow_executor.py — paper fills on an in-memory balance dict
├── strategy/tri_executor.py    — real 3-leg sequential limit orders via CSK REST
├── strategy/order_poller.py    — 1Hz/10Hz REST polling for fill detection
└── strategy/tri_engine.py      — orchestrates feeds, ranker, executor, watchdog

Core Types (core/models.py)
├── Depth      — immutable order book snapshot (Decimal levels)
├── TriBook    — 3 Depth objects per symbol (s_inr, s_usdt, usdt_inr)
├── PathResult — one evaluated path (profit_pct, executable_qty, all Decimal)
└── TriIntent  — in-flight 3-leg execution state

API / Wiring
├── api_client.py  — CSK REST client (Ed25519 auth, discover_symbols, depth, orders, balances)
├── main.py        — CLI entry point: discovers symbols at boot, wires components
└── dashboard.py   — aiohttp web server + SSE dashboard on :8080
```

All financial arithmetic uses `Decimal` — no float drift. The strategy layer never imports feeds or `api_client`; they are injected at the wiring layer.

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

# Shadow portfolio size
SHADOW_PORTFOLIO_TOTAL_INR=1000000

# Symbol filtering (see Configuration section)
SYMBOLS_BLACKLIST=BTC,ETH,SOL,XRP,ADA,AVAX,SHIB,PEPE,BONK,USDC,BUSD,DAI
# SYMBOLS_WHITELIST=DOGE,NEAR,SUI   # uncomment to narrow to specific symbols

# Execution mode
# EXECUTION_MODE=shadow   ← paper trades only, safe (default)
# EXECUTION_MODE=real     ← places real limit orders on CSK

# Disable WebSocket, use 1.5s REST polling + fallback symbol list instead
# USE_REST_FALLBACK=1

# Slack alerts (all optional)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_ALERTS_ENABLED=true
SLACK_OPPORTUNITY_ALERTS_ENABLED=true
SLACK_EXECUTION_ALERTS_ENABLED=true
SLACK_ALERT_COOLDOWN_SECONDS=60
```

### 3. Run

There are four ways to run the engine, from safest to most aggressive:

---

#### Option 1 — REST fallback, shadow execution (safest, good for first test)

No WebSocket connections. Polls CSK REST every 1.5s. Uses `config.SYMBOLS` as the symbol list (no live discovery). Safe to run immediately to verify the setup works.

```bash
USE_REST_FALLBACK=1 python main.py
```

What happens:
- Boots with the fallback symbol list from `config.py`
- Fetches depth via REST every 1.5s
- Runs the ranker and logs opportunities
- Simulates fills on a paper portfolio — no real orders

---

#### Option 2 — WebSocket mode, shadow execution (recommended for ongoing monitoring)

Live 10Hz market data. Discovers all eligible symbols at boot (~50–80+ depending on what CSK lists). Paper trades only.

```bash
python main.py
```

**Set a whitelist first** — without one, discovery returns 300+ symbols which will overwhelm the WS feed. Add this to `.env`:

```env
SYMBOLS_WHITELIST=DOGE,NEAR,LINK,FET,POL,DOT,HBAR,SUI,INJ,RENDER,ARB,GALA,ENJ,UNI,ONDO,TRX,ICP,ACT,PARTI,BNB
```

What happens:
- Discovers eligible symbols at boot (intersection of CSK INR + Binance USDT pairs)
- Opens Binance WS (`@depth20@100ms`) and CSK socket.io feeds
- Ranks all paths every 100ms
- Simulates fills on a paper portfolio — no real orders
- Logs opportunities and shadow P&L

---

#### Option 3 — WebSocket mode, shadow execution + dashboard

Same as Option 2 but with a live browser dashboard on `http://localhost:8080`. Run in two terminals:

```bash
# Terminal 1
python dashboard.py

# Terminal 2 (optional — dashboard already runs the engine internally)
# Open http://localhost:8080 in your browser
```

The dashboard shows per-symbol spread cards, P&L sparklines, shadow inventory, and a live event feed. Click any symbol card for the full depth view.

---

#### Option 4 — WebSocket mode, real order execution (live trading)

Places real limit orders on CSK. Only enable after running shadow mode for several weeks and confirming consistently positive net P&L.

```bash
EXECUTION_MODE=real python main.py
```

What happens:
- Everything from Option 2, plus:
- When net profit > 1.5% is detected, places 3 real limit orders sequentially
- Leg 1 fires immediately; Legs 2 and 3 fire after each fill is confirmed by the order poller (10Hz polling)
- Each leg has a 30-second timeout — stalled orders are cancelled
- Real exchange balances are refreshed after each settlement

**Read before using:**
- If Leg 2 or Leg 3 times out, you are left with a partial open position — there is no automatic unwind.
- Limit orders may not fill if the spread closes before execution completes.
- Verify the CSK order endpoint field names in `api_client.py` match your account's API version.

---

#### Summary

| Option | Command | Data | Execution | Use when |
|---|---|---|---|---|
| 1 | `USE_REST_FALLBACK=1 python main.py` | REST 1.5s | Paper | First test, offline dev |
| 2 | `python main.py` | WS 100ms | Paper | Ongoing monitoring |
| 3 | `python dashboard.py` | WS 100ms | Paper | Monitoring with UI |
| 4 | `EXECUTION_MODE=real python main.py` | WS 100ms | Real orders | Live trading |

---

## Profitability Assessment

### What works in your favour

- **10Hz market data** via WebSocket — you see opportunities before REST-based scanners
- **VWAP-aware sizing** — if the book is too thin to absorb your trade size, the ranker marks it not executable rather than giving a false signal
- **Dynamic symbol discovery** — scans every symbol CSK lists at boot; typically 50–80+ symbols vs. a fixed hardcoded list
- **Decimal accounting** — the math is exact; no float rounding errors inflating paper P&L

### What works against you

**1. TDS is the main obstacle.**
India's 1% TDS on VDA sales applies to 1–2 legs of every path. That alone costs 1–2% per cycle before any exchange fee. Your breakeven floor is ~1.5%. On CSK, spreads that wide are uncommon in calm markets.

**2. CSK's USDT prices track Binance very tightly.**
The C2C system links CSK USDT prices to Binance in near real-time. The gap this engine exploits — between INR-implied price and USDT-implied price — closes automatically most of the time.

**3. Sequential limit orders are slow.**
True arbitrage profits most from near-simultaneous execution. Your system places Leg 2 only after Leg 1 fills, which takes seconds. The spread can disappear in that window.

**4. Shadow P&L overstates real P&L.**
Shadow fills assume you receive the VWAP you calculated. Real fills depend on order queue position and partial fills across legs. Expect real results to be 10–30% worse than shadow.

### Realistic expectations

| Scenario | Likely outcome |
|---|---|
| Shadow mode, calm markets | Opportunities detected occasionally; net P&L near zero or slightly positive |
| Shadow mode, volatile event (listing, crash) | Clear positive signal — this is when triangular arb genuinely works |
| Real mode, calm markets | Breakeven to slightly negative once real fill quality is counted |
| Real mode, volatile event | Positive — if all 3 fills complete before the spread closes |

**The recommendation:** Run shadow mode for several weeks. If cumulative net P&L on the dashboard is consistently positive across multiple symbols, the edge is real and worth enabling real execution. If shadow is barely positive, real will be negative.

---

## Configuration

### Symbol discovery

At boot, the engine calls CSK's ticker API for both `coinswitchx` (INR pairs) and `binance` (USDT pairs), takes their intersection, and trades every symbol that has **both** a live `S/INR` and `S/USDT` book. This typically yields 50–80+ symbols automatically — no manual list needed.

Control which symbols are included via env vars or `config.py`:

```env
# Blacklist — always excluded (default list in config.py covers big-caps and stables)
SYMBOLS_BLACKLIST=BTC,ETH,SOL,XRP,ADA,SHIB,PEPE,USDC

# Whitelist — if set, only these symbols are traded (after eligibility check)
# Leave unset to trade everything not blacklisted
SYMBOLS_WHITELIST=DOGE,NEAR,SUI,LINK
```

The default blacklist in `config.py` excludes:
- Large-caps (BTC, ETH, SOL, XRP, ADA, AVAX) — too efficient, spreads too thin after TDS
- Meme coins (SHIB, PEPE, BONK) — ultra-thin books, VWAP degrades rapidly
- Stablecoins (USDC, BUSD, DAI) — not arb-eligible by nature

`config.SYMBOLS` is kept as a fallback only for `USE_REST_FALLBACK=1` (offline / no live discovery).

### Exposure limits

```python
MAX_EXPOSURES = {
    "INR":  Decimal("25000"),   # max INR notional per cycle
    "USDT": Decimal("250"),     # max USDT notional per cycle
}
```

### Shadow portfolio

```python
SHADOW_PORTFOLIO_TOTAL_INR = Decimal("1000000")  # ₹10 lakh starting portfolio
SHADOW_INR_RESERVE_PCT     = Decimal("0.20")     # 20% kept as INR
SHADOW_USDT_RESERVE_PCT    = Decimal("0.10")     # 10% kept as USDT
# Remainder distributed equally across symbols unless SHADOW_TOKEN_WEIGHTS is set
```

---

## Dashboard

Start with `python dashboard.py`, then open `http://localhost:8080`.

Per-symbol cards show:
- Best net spread (after fees + TDS) and the path that produces it
- Direction (INR-start / token-start) and logical case label
- Shadow P&L sparkline
- Recent opportunity and execution events

Click any card for a modal with full depth view (all 3 books), shadow balance breakdown, and P&L history chart.

---

## Project Structure

```
.
├── main.py                      # CLI entry point (wiring only)
├── dashboard.py                 # aiohttp web server + SSE stream
├── api_client.py                # CSK REST client (auth, depth, orders, balances)
├── config.py                    # all tunables (fees, symbols, exposure caps)
├── slack_notifier.py            # Slack webhook alerts with cooldown
├── requirements.txt
├── core/
│   ├── models.py                # Depth, TriBook, PathResult, TriIntent (Decimal)
│   └── protocol.py              # ExchangeAdapter Protocol
├── feeds/
│   ├── binance_depth_ws.py      # Binance @depth20@100ms WebSocket feed
│   └── csk_public_ws.py         # CSK socket.io DEPTH_UPDATE feed
├── strategy/
│   ├── tri_ranker.py            # stateless 4-path VWAP ranker
│   ├── shadow_executor.py       # paper portfolio simulator
│   ├── tri_executor.py          # real 3-leg sequential order placement
│   ├── order_poller.py          # 1Hz/10Hz REST fill detection
│   └── tri_engine.py            # orchestration: feeds + tick loop + watchdog
├── scripts/
│   └── list_pairs.py            # symbol discovery tool
└── static/
    └── index.html               # SSE dashboard UI
```

---

## Safety Notes

- Always run shadow mode first and validate net P&L before enabling real execution.
- Never set `EXECUTION_MODE=real` without reviewing the order endpoint paths in `api_client.py` against your CSK API documentation — field names may need adjustment for your account tier.
- Partial positions from timed-out legs are not automatically unwound. Monitor logs whenever real mode is active.
- Never commit `COINSWITCH_SECRET_KEY` to version control.
