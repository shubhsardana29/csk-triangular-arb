# CoinSwitch Triangular Arbitrage Engine

A production-grade multi-symbol arbitrage engine for CoinSwitch (CSK). Runs two strategies simultaneously with live 10Hz WebSocket market data, exact fee + TDS accounting, paper trading, and real limit-order execution.

---

## Strategies

### 2-Leg Spread Arbitrage (default, always on)

Compares the CSK INR price of a token against the fair value derived from Binance USDT × USDT/INR rate. When they diverge beyond costs, profit exists with just 2 orders.

```
INR_CHEAP     — BUY S/INR on CSK  →  SELL S/USDT on CSK C2C  (INR → USDT implicit)
INR_EXPENSIVE — BUY S/USDT on CSK C2C  →  SELL S/INR on CSK
```

Leg 2 is placed after Leg 1 fills at `max(cost_floor, current_market_bid)`. Leg 2 is **repriced every tick** — if the market rises, the sell price follows it. If it drops below the cost floor, the order rests at the floor and a stuck-alert fires after 60 seconds.

### 3-Leg Triangular Arbitrage (opt-in)

For any token S, three markets form a triangle: `S/INR` on CSK, `S/USDT` on Binance via CSK C2C, and `USDT/INR` on CSK. The engine evaluates 4 paths per symbol every 100ms:

```
Path 1 — token-start, INR leg cheap:    SELL S/INR  → BUY USDT/INR  → BUY S/USDT
Path 2 — token-start, USDT leg cheap:   SELL S/USDT → SELL USDT/INR → BUY S/INR
Path 3 — INR-start, INR leg cheap:      BUY S/INR   → SELL S/USDT   → SELL USDT/INR
Path 4 — INR-start, USDT leg cheap:     BUY USDT/INR → BUY S/USDT  → SELL S/INR
```

Before Leg 3 is placed, a **cost floor check** verifies the current book still delivers profit — the trade is aborted rather than locked into a guaranteed loss.

### Position Locking

Both strategies share a single position lock. **One trade per symbol** runs at a time — 3-leg or 2-leg, not both. A symbol is locked when a trade starts and released the moment it settles.

---

## Cost Model

| Cost | Rate | Applied to |
|---|---|---|
| Taker fee (from API) | ~0.04% per leg | Every order (fetched at boot, per symbol) |
| TDS | 1% per VDA sale | 1–2 sell legs per path |
| Safety buffer | ~0.2% | Slippage + price movement between legs |
| **Breakeven floor** | **~1.5%** | Strategy will not enter below this |

**TDS is the dominant cost.** At 1% per sell, even with a 0.04% taker fee, you need ~1.08% raw spread to break even. Calm markets rarely offer this on liquid pairs. Opportunities arise during volatility spikes, listings, and market dislocations.

Per-symbol taker fees are fetched from the CSK `/trade/api/v2/tradingFee` endpoint at boot. If the API call fails, the config default (`TAKER_FEE=0.001`) is used as fallback.

---

## Architecture

```
Market Data (100ms)
├── feeds/binance_depth_ws.py    — S/USDT depth via Binance WS (@depth20@100ms)
├── feeds/csk_public_ws.py       — S/INR + USDT/INR via CSK socket.io
└── feeds/webhook_emitter.py     — fire-and-forget HTTP event emitter → n8n

Strategy (10Hz tick loop)
├── strategy/tri_ranker.py       — stateless 4-path triangular VWAP scorer
├── strategy/two_leg_ranker.py   — stateless 2-leg spread scorer (reads config live for ControlAPI)
├── strategy/shadow_executor.py  — paper fills on in-memory balances (ShadowExecutor + ShadowTwoLegExecutor)
├── strategy/tri_executor.py     — real 3-leg sequential limit orders + boot recovery
├── strategy/two_leg_executor.py — real 2-leg orders with cost-floor repricing
├── strategy/tri_rebalancer.py   — passive maker USDT/INR restorer (real mode only)
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
├── api_client.py   — CSK REST client (Ed25519 auth, discover_symbols, orders, balances, fees)
├── control_api.py  — aiohttp HTTP control server on localhost:8765 (n8n → bot param changes)
├── main.py         — CLI entry point
└── dashboard.py    — aiohttp web server + SSE dashboard on :8080
```

All financial arithmetic uses `Decimal`. Strategy code never imports feeds or `api_client` — they are injected at the wiring layer.

---

## Production Safety

| Feature | What it does |
|---|---|
| **Position lock** | One open trade per symbol at a time — prevents double-entry across both strategies |
| **Staleness cancel-all** | If either WS feed goes silent >15s, all open orders are cancelled immediately |
| **Boot cancel** | All open orders from a previous run are cancelled before the engine starts |
| **Boot recovery** | Detects unexpected token balances after a crash and places a liquidating SELL |
| **Cost floor (3-leg)** | Before Leg 3, verifies current book depth still delivers profit — aborts if not |
| **Cost floor (2-leg)** | Leg 2 SELL never placed below `buy_avg × (1 + fee + TDS + safety)` |
| **Leg timeout** | Any leg not filled within 30s is cancelled; partial proceeds continue if available |
| **Stuck alert** | If 2-leg Leg 2 rests at cost floor >60s, a warning is logged |
| **Rebalancer** | Passive maker BUY on USDT/INR when USDT share drops below 20%; 30s cooldown after failure |
| **Per-symbol fees** | Actual taker fees fetched from CSK API at boot — no stale hardcoded defaults |

---

## Setup

### 1. Install

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` and fill in:

```env
# ── Required ───────────────────────────────────────────────────────────────
COINSWITCH_API_KEY=your_api_key
COINSWITCH_SECRET_KEY=your_hex_ed25519_secret

# ── Trading costs (defaults are correct for most CSK accounts) ─────────────
TAKER_FEE=0.001           # fallback only — actual fees fetched from API at boot
TDS_RATE=0.01             # 1% TDS — set by law, do not change
MIN_PROFIT_PCT=0.015      # 1.5% net minimum to enter

# ── Exposure caps per cycle ────────────────────────────────────────────────
MAX_EXPOSURE_INR=25000
MAX_EXPOSURE_USDT=250

# ── Shadow portfolio (paper trading) ──────────────────────────────────────
SHADOW_PORTFOLIO_TOTAL_INR=1000000

# ── Symbol filtering ───────────────────────────────────────────────────────
# Default blacklist (in config.py): BTC, ETH, SOL, XRP, ADA, AVAX, SHIB, PEPE, BONK, USDC, BUSD, DAI
# SYMBOLS_WHITELIST=DOGE,NEAR,SUI,LINK,FET   # narrow to specific symbols

# ── Execution mode ─────────────────────────────────────────────────────────
# EXECUTION_MODE=shadow   ← paper trades only (default)
# EXECUTION_MODE=real     ← places real limit orders on CSK

# ── Strategy switches ──────────────────────────────────────────────────────
THREE_LEG_ENABLED=false        # 3-leg triangular arb (off by default)
TWO_LEG_ENABLED=true           # 2-leg spread arb (default strategy)
REBALANCER_ENABLED=true        # passive USDT/INR restorer (only active in real mode)
TWO_LEG_MIN_SPREAD_PCT=0.015   # minimum 2-leg spread to enter (1.5%)
REPRICE_THRESHOLD_PCT=0.0005   # reprice 2-leg Leg 2 if market moves >0.05%
STUCK_ALERT_AFTER_S=60         # alert if 2-leg Leg 2 stuck at floor >60s

# ── REST fallback (no WebSocket) ───────────────────────────────────────────
# USE_REST_FALLBACK=1

# ── Direct Slack alerts (fires on every trade, no n8n needed) ─────────────
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_ALERTS_ENABLED=true
SLACK_ALERT_COOLDOWN_SECONDS=60

# ── n8n integration (optional agentic layer on top of direct Slack) ────────
# N8N_WEBHOOK_ENABLED=true
# N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity
# CONTROL_API_ENABLED=true
# CONTROL_API_SECRET=change-me-to-a-random-string
# CONTROL_API_PORT=8765
```

### 3. Run

| Option | Command | Market data | Executes | Use when |
|---|---|---|---|---|
| 1 | `USE_REST_FALLBACK=1 python3 main.py` | REST 1.5s | Paper | First test — no WS needed |
| 2 | `python3 main.py` | WS 100ms | Paper | Default: 2-leg monitoring |
| 3 | `python3 dashboard.py` | WS 100ms | Paper | 2-leg with live UI on :8080 |
| 4 | `THREE_LEG_ENABLED=true python3 main.py` | WS 100ms | Paper | Both strategies |
| 5 | `EXECUTION_MODE=real python3 main.py` | WS 100ms | Real orders | Live trading |

**Recommended whitelist** to keep symbol count manageable:
```env
SYMBOLS_WHITELIST=DOGE,NEAR,LINK,FET,POL,DOT,HBAR,SUI,INJ,RENDER,ARB,GALA,ENJ,UNI,ONDO,TRX,ICP
```

---

## Slack Alerts

There are **two independent Slack paths** — you can use either or both:

### Path 1 — Direct (always available)

Set `SLACK_WEBHOOK_URL` in `.env`. The bot sends alerts directly on every trade (both 2-leg and 3-leg, shadow and real) with a configurable cooldown. No n8n needed.

### Path 2 — Via n8n (agentic layer)

The bot posts trade events to n8n, which runs a Claude analysis and optionally adjusts the spread threshold before notifying Slack. Adds intelligence on top of raw alerts. See the n8n Integration section below.

Both paths fire on shadow trades too — useful for verifying the pipeline before going live.

---

## n8n Integration

The bot has a built-in integration layer for [n8n](https://n8n.io) and Claude. It works in two directions:

```
Bot  ──trade events──►  n8n  ──Claude──►  lower / raise / hold threshold
Bot  ◄──POST /control── n8n              (applied immediately, no restart)
                          └──────────────►  Slack notification
```

Three pre-built workflows are in `n8n/`:

| Workflow | File | Needs Claude key |
|---|---|---|
| Opportunity Analyzer | `opportunity_analyzer.json` | Yes |
| Opportunity Analyzer (no-key) | `opportunity_analyzer_local_test.json` | **No** — uses rule-based mock |
| Daily Digest | `daily_digest.json` | Yes |

**No Claude API key yet?** Use `opportunity_analyzer_local_test.json` — it makes the same lower/raise/hold decisions using deterministic JS logic and sends real Slack alerts. Swap in `opportunity_analyzer.json` later when you have a key.

For full setup, architecture details, and VPS deployment steps see **[n8n/SETUP.md](n8n/SETUP.md)**.

### Quick start (local)

```bash
# 1. Start n8n
docker run -d --name n8n --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n

# 2. Import and activate the no-key workflow
docker cp n8n/opportunity_analyzer_local_test.json n8n:/tmp/wf.json
docker exec n8n n8n import:workflow --input=/tmp/wf.json
docker exec n8n n8n publish:workflow --id=arb-opportunity-analyzer-test
docker restart n8n

# 3. Run the full integration test (8 scenarios)
chmod +x n8n/test_integration.sh
N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity \
CONTROL_API_SECRET=test-secret \
bash n8n/test_integration.sh

# 4. Start the bot with n8n + Control API enabled
N8N_WEBHOOK_ENABLED=true \
N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity \
CONTROL_API_ENABLED=true \
CONTROL_API_HOST=0.0.0.0 \
CONTROL_API_PORT=8765 \
CONTROL_API_SECRET=test-secret \
python main.py
```

> **Note:** `CONTROL_API_HOST=0.0.0.0` is needed locally so the Docker container can reach the host. In production keep it `127.0.0.1`.

### Control API reference

Runs on `127.0.0.1:8765` (localhost only). All requests require `X-Control-Secret` header. Changes apply on the next tick (<100ms). No restart needed.

```bash
curl -s -H "X-Control-Secret: your-secret" http://127.0.0.1:8765/health
curl -s -H "X-Control-Secret: your-secret" http://127.0.0.1:8765/status

curl -s -X POST http://127.0.0.1:8765/control \
  -H "Content-Type: application/json" \
  -H "X-Control-Secret: your-secret" \
  -d '{"min_spread_pct": 0.013, "three_leg_enabled": true}'
```

**Supported `/control` fields:**

| Field | Type | Clamped range | Effect |
|---|---|---|---|
| `min_spread_pct` | float | 0.005 – 0.10 | 2-leg minimum spread threshold |
| `three_leg_enabled` | bool | — | Enable/disable 3-leg strategy live |
| `two_leg_enabled` | bool | — | Enable/disable 2-leg strategy live |

---

## Dashboard

```bash
python3 dashboard.py   # opens on http://localhost:8080
```

The SSE dashboard updates at 2Hz. Per-symbol modal shows:

- Live 2-leg and 3-leg opportunity state
- Shadow portfolio balances (INR, USDT, token)
- Cumulative shadow P&L with chart
- **Recent Trades** — last 10 trades per symbol (2-leg and 3-leg) with direction, profit %, INR Δ, and running cumulative P&L
- Recent activity feed (opportunity detected / cleared / route changed)
- Live order book depth for all 3 pairs

---

## Diagnostics

Every 100 cycles (~10 seconds in WS mode), the engine logs the top-5 2-leg opportunities:

```
[2leg-best] ICP        INR_CHEAP  spread=+0.541%  profit=-0.476%  threshold=1.500%
[2leg-best] NEAR       INR_CHEAP  spread=+0.410%  profit=-0.608%  threshold=1.500%
```

`spread` is the raw price gap. `profit` is net after fees + TDS. When `profit` is still deeply negative, it means TDS is dominating — real opportunities require market dislocations or the threshold to be lowered for experimentation.

---

## Symbol Discovery

At boot the engine intersects CSK's live INR pairs with Binance's live USDT pairs, applies the blacklist, and trades every eligible symbol automatically.

```env
# Always excluded (default blacklist in config.py)
SYMBOLS_BLACKLIST=BTC,ETH,SOL,XRP,ADA,AVAX,SHIB,PEPE,BONK,USDC,BUSD,DAI

# Restrict to specific symbols only
SYMBOLS_WHITELIST=DOGE,NEAR,SUI,LINK
```

`config.SYMBOLS` is a fallback used only when `USE_REST_FALLBACK=1`.

---

## Project Structure

```
.
├── main.py                        # CLI entry point (wiring only)
├── dashboard.py                   # aiohttp web server + SSE dashboard on :8080
├── api_client.py                  # CSK REST client (auth, orders, balances, fees, recovery)
├── config.py                      # all tunables (fees, symbols, exposure, rebalancer, 2-leg, n8n)
├── control_api.py                 # HTTP control server on localhost:8765 (n8n → bot)
├── slack_notifier.py              # direct Slack webhook alerts with cooldown
├── requirements.txt
├── core/
│   ├── models.py                  # Depth, TriBook, PathResult, TwoLegResult, intents (Decimal)
│   └── protocol.py                # ExchangeAdapter Protocol
├── feeds/
│   ├── binance_depth_ws.py        # Binance @depth20@100ms WebSocket feed
│   ├── csk_public_ws.py           # CSK socket.io depth feed (S/INR + USDT/INR)
│   └── webhook_emitter.py         # fire-and-forget HTTP emitter (bot → n8n)
├── strategy/
│   ├── tri_ranker.py              # stateless 4-path triangular scorer
│   ├── two_leg_ranker.py          # stateless 2-leg spread scorer (reads config live)
│   ├── shadow_executor.py         # paper portfolio simulator (ShadowExecutor + ShadowTwoLegExecutor)
│   ├── tri_executor.py            # real 3-leg sequential orders + cost floor + boot recovery
│   ├── two_leg_executor.py        # real 2-leg orders with repricing + cost floor + stuck alert
│   ├── tri_rebalancer.py          # passive USDT/INR maker (real mode only, 30s cooldown)
│   ├── order_poller.py            # 1Hz idle / 10Hz active REST fill detection
│   └── tri_engine.py              # orchestration: position lock, staleness cancel-all, watchdog
├── n8n/
│   ├── SETUP.md                   # full n8n setup guide (local + VPS, with and without Claude key)
│   ├── opportunity_analyzer.json  # production workflow: trade → Claude → threshold → Slack
│   ├── opportunity_analyzer_local_test.json  # same flow with mock Claude (no API key needed)
│   ├── daily_digest.json          # daily cron: bot status → Claude → Slack ops digest
│   └── test_integration.sh        # 8-scenario end-to-end integration test
├── scripts/
│   └── list_pairs.py              # symbol discovery debug tool
└── static/
    └── index.html                 # SSE dashboard UI
```

---

## VPS Deployment

### Transfer code

```bash
rsync -av --exclude=venv --exclude=__pycache__ --exclude=.git \
  /local/path/csk-triangular-arb/ user@your-vps:~/csk-triangular-arb/
```

### Install on server

```bash
cd ~/csk-triangular-arb
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
```

### Run with screen (no sudo required)

```bash
# Start the bot in a detached screen session
screen -dmS arb bash -c '
  cd ~/csk-triangular-arb
  source venv/bin/activate
  python main.py >> logs/arb.log 2>&1
'

# Attach to check on it
screen -r arb

# Detach without stopping: Ctrl+A then D
```

To survive reboots without sudo, add to crontab (`crontab -e`):

```cron
@reboot sleep 30 && screen -dmS arb bash -c 'cd ~/csk-triangular-arb && source venv/bin/activate && python main.py >> logs/arb.log 2>&1'
```

### Run n8n alongside the bot (no sudo)

```bash
# Option A: npm (no Docker required)
npm install -g n8n
screen -dmS n8n bash -c 'N8N_HOST=127.0.0.1 n8n start >> ~/n8n.log 2>&1'

# Option B: Docker (if available on your VPS)
docker run -d --name n8n --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p 127.0.0.1:5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n
```

Add n8n to crontab before the bot (it must be ready when the bot starts):

```cron
@reboot sleep 20 && screen -dmS n8n bash -c 'N8N_HOST=127.0.0.1 n8n start >> ~/n8n.log 2>&1'
@reboot sleep 45 && screen -dmS arb bash -c 'cd ~/csk-triangular-arb && source venv/bin/activate && python main.py >> logs/arb.log 2>&1'
```

Access n8n UI on your VPS via SSH tunnel (n8n is bound to localhost):

```bash
# Run on your local machine
ssh -L 15678:localhost:5678 user@your-vps
# Then open: http://localhost:15678
```

See **[n8n/SETUP.md](n8n/SETUP.md)** for importing workflows, filling credentials, and verifying the full pipeline on VPS.

---

## Safety Notes

- Always run shadow mode first. Only enable `EXECUTION_MODE=real` after weeks of positive shadow P&L.
- Never commit `COINSWITCH_SECRET_KEY` or `CONTROL_API_SECRET` to version control.
- In real mode: if the bot crashes between Leg 1 and Leg 2, boot recovery will attempt to liquidate the stranded position on next restart. Check logs for `[executor] recovery:` messages.
- The rebalancer places passive maker orders — it will not fill instantly. Monitor USDT balance if running many INR-start trades.
- The Control API clamps `min_spread_pct` to a floor of 0.5% — do not lower it manually below the cost floor (~1.08%). The clamp is a guardrail, not a recommendation.
- n8n's Claude agent (or mock equivalent) applies threshold changes automatically. Review the n8n Executions tab periodically to verify decisions are sensible.
