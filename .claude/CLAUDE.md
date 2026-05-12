# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in credentials

# Run modes
USE_REST_FALLBACK=1 python3 main.py          # REST polling (no WS, no credentials needed for smoke test)
python3 main.py                              # WS 10Hz, paper trading (default)
python3 dashboard.py                         # WS + live SSE dashboard on :8080
THREE_LEG_ENABLED=true python3 main.py       # Both strategies
EXECUTION_MODE=real python3 main.py          # Live trading (real orders)

# Symbol discovery debug
python3 scripts/list_pairs.py

# n8n local integration test (requires n8n running on :5678)
bash n8n/test_integration.sh
```

## Architecture

### Data Flow

```
WebSocket Feeds (100ms)          REST Fallback (1.5s)
├── BinanceDepthFeed             │
│   S/USDT @depth20@100ms        │  client.fetch_triangular_books()
└── CSKPublicWS                  │
    S/INR + USDT/INR             │
         │                       │
         └──────────┬────────────┘
                    ▼
              TriEngine (10Hz tick loop)
              ├── assembles TriBook snapshots
              ├── TriRanker → scores 4 triangular paths
              ├── TwoLegRanker → scores 2-leg spread
              ├── position lock (one trade per symbol)
              └── dispatches to executors
                    ├── ShadowExecutor / ShadowTwoLegExecutor (paper)
                    └── TriExecutor / TwoLegExecutor (real orders)
```

### Key Architectural Constraints

**All financial arithmetic uses `Decimal` — never `float`.** Conversion from raw API strings happens only in `Depth.from_raw()` and `TriBook.from_raw()`. Strategy code receives and returns Decimal throughout.

**Strategy code never imports feeds or `api_client` directly.** Everything is injected as constructor args at the wiring layer (`main.py` / `dashboard.py`). This keeps `strategy/` testable in isolation.

**Position lock** (`TriEngine._locks`) is a per-symbol asyncio lock — prevents simultaneous 2-leg and 3-leg trades on the same symbol. Executors call `on_settle(symbol)` when a trade completes, which calls `engine._on_settle()` to release the lock.

**`config.py` is the single source of truth for all tunables.** Strategy code reads from `config` directly (e.g., `config.TWO_LEG_MIN_SPREAD_PCT`) so live changes via the Control API take effect on the next tick without restarting.

### Core Types (`core/models.py`)

| Type | Role |
|---|---|
| `Depth` | Immutable order book snapshot; exposes `walk_bids_to_qty`, `walk_asks_to_qty`, `walk_asks_to_notional` for VWAP walking |
| `TriBook` | Three `Depth` objects for one symbol: `s_inr`, `s_usdt`, `usdt_inr` |
| `PathResult` | One evaluated 3-leg path (stateless, all Decimal) |
| `TwoLegResult` | One evaluated 2-leg spread (stateless, all Decimal) |
| `TriIntent` | Mutable in-flight 3-leg execution state (order IDs, fill quantities) |
| `TwoLegIntent` | Mutable in-flight 2-leg execution state (tracks `cost_floor` for Leg 2 repricing) |

### Strategy Layer (`strategy/`)

- **Rankers** (`tri_ranker.py`, `two_leg_ranker.py`) — pure stateless scorers. Take a `TriBook`, return a `PathResult` / `TwoLegResult`. No side effects.
- **Executors** (`tri_executor.py`, `two_leg_executor.py`) — stateful, manage in-flight `TriIntent` / `TwoLegIntent`. Real executors place limit orders via `api_client`. Shadow executors simulate fills against in-memory balances.
- **`order_poller.py`** — polls CSK REST for fill status at 1Hz idle / 10Hz active. Called by executors; not by the engine directly.
- **`tri_rebalancer.py`** — passive maker BUY on USDT/INR when USDT drops below 20% of portfolio. Only active in real mode; has a 30-second cooldown after failure.

### Cost Model

The dominant cost is **1% TDS per sell leg**. Breakeven floor is ~1.5% (`MIN_PROFIT_PCT`). The Control API clamps `min_spread_pct` to a floor of 0.5% as a guardrail — do not lower it manually below the actual cost floor (~1.08%).

Per-symbol taker fees are fetched from CSK `/trade/api/v2/tradingFee` at boot; `config.TAKER_FEE` is the fallback only.

### External Integrations

- **Slack** (`slack_notifier.py`) — fires directly on every trade via `SLACK_WEBHOOK_URL`, with configurable cooldown. Independent of n8n.
- **n8n** (`feeds/webhook_emitter.py`) — fire-and-forget HTTP emitter to n8n webhook. n8n can call back via the Control API to adjust `min_spread_pct` or toggle strategies live. Workflows are in `n8n/`.
- **Control API** (`control_api.py`) — `aiohttp` server on `127.0.0.1:8765`, requires `X-Control-Secret` header. Mutates `config.TWO_LEG_MIN_SPREAD_PCT`, `config.THREE_LEG_ENABLED`, `config.TWO_LEG_ENABLED` directly — changes apply on the next tick.
- **Dashboard** (`dashboard.py`) — alternative entry point to `main.py`; adds SSE streaming to `static/index.html` on `:8080`. Uses the same engine but wires a `tick_callback` for live UI updates.

### Symbol Discovery

At boot, `client.discover_symbols()` intersects CSK's live INR pairs with Binance's live USDT pairs, applies `SYMBOLS_BLACKLIST` / `SYMBOLS_WHITELIST`, and returns the eligible set. `config.SYMBOLS` is a fallback used only when `USE_REST_FALLBACK=1`.

### Production Safety Features

- Staleness watchdog: cancels all open orders if either WS feed is silent >15s
- Boot cancel: cancels all open orders from a previous run before starting
- Boot recovery: detects unexpected token balances after a crash and places a liquidating SELL
- 3-leg cost floor check: before Leg 3, verifies current book depth still delivers profit
- 2-leg cost floor: Leg 2 SELL never placed below `buy_avg × (1 + fee + TDS + safety)`; repriced every tick
- Leg timeout: any leg not filled within 30s is cancelled