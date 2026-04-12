# CoinSwitch PRO Triangular Arbitrage Engine

Multi-symbol triangular arbitrage scanner for CoinSwitch PRO. The project fetches live order book depth, evaluates four triangular paths per asset, applies fee and TDS logic, and streams the best net and gross opportunity for each configured token to a live dashboard.

The codebase no longer assumes a BTC-only strategy. It now works across the token list defined in [`config.py`](/home/shubh/codes/csk-triangular-arb/config.py), currently:

- `BTC`
- `ETH`
- `SOL`
- `XRP`
- `DOGE`
- `ADA`
- `BNB`
- `TRX`
- `UNI`
- `DOT`

## What The Project Does

- Fetches depth for `SYMBOL/INR`, `SYMBOL/USDT`, and shared `USDT/INR`
- Reuses `USDT/INR` across all symbols to reduce duplicate API calls
- Computes four triangular arbitrage paths for every configured symbol
- Produces both:
  - `net` opportunity: after taker fee and TDS
  - `gross` opportunity: before fee and tax deductions
- Sizes trades using per-asset exposure caps and available shadow balances
- Streams live symbol data to a browser dashboard over SSE
- Supports shadow execution so you can validate logic without placing real orders

## Project Structure

- [`main.py`](/home/shubh/codes/csk-triangular-arb/main.py): runs the continuous multi-symbol arbitrage loop
- [`dashboard.py`](/home/shubh/codes/csk-triangular-arb/dashboard.py): starts the web UI and SSE stream on port `8080`
- [`api_client.py`](/home/shubh/codes/csk-triangular-arb/api_client.py): CoinSwitch REST client with ed25519 request signing and pooled `aiohttp` sessions
- [`arbitrage_engine.py`](/home/shubh/codes/csk-triangular-arb/arbitrage_engine.py): shared VWAP pricing, path math, opportunity selection, and VWAP-aware shadow execution
- [`config.py`](/home/shubh/codes/csk-triangular-arb/config.py): API keys, fees, symbols, exposure caps, and shadow portfolio allocation
- [`static/index.html`](/home/shubh/codes/csk-triangular-arb/static/index.html): live multi-asset dashboard

## Strategy Model

For each symbol `S`, the engine evaluates these four routes:

1. `SELL S/INR -> BUY USDT/INR -> BUY S/USDT`
2. `SELL S/USDT -> SELL USDT/INR -> BUY S/INR`
3. `BUY S/INR -> SELL S/USDT -> SELL USDT/INR`
4. `BUY USDT/INR -> BUY S/USDT -> SELL S/INR`

The engine chooses the best path separately for net and gross yield, then builds an executable opportunity payload with:

- `symbol`
- `direction`
- `base_currency`
- `executable_qty`
- `profit_pct`
- `expected_profit_inr`
- `depth`

If the best spread is not positive enough, the engine marks the result as not actionable.

## Pricing, Fees, And TDS

### VWAP-aware fills

The engine does not rely only on top-of-book prices. It calculates VWAP against the requested trade size and returns `0.0` when book depth is insufficient.

This is now used in both layers:

- opportunity detection
- shadow execution

### Sequential deductions

Net yields are computed with sequential deductions:

`gross_amount * (1 - taker_fee) * (1 - tds)`

This is applied only where appropriate:

- buy legs: fee only
- VDA sell legs: fee and TDS

Default values come from environment-backed config:

- `TAKER_FEE=0.001`
- `TDS_RATE=0.01`

## Multi-Symbol Configuration

Edit [`config.py`](/home/shubh/codes/csk-triangular-arb/config.py) to control the universe and position sizing.

### Symbols scanned

```python
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "TRX", "UNI", "DOT"]
```

### Per-cycle exposure caps

```python
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
```

The engine uses these limits together with current balances to determine the path start amount for each cycle.

### Shadow portfolio controls

`config.py` also owns the starting shadow portfolio:

```python
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
```

At startup, the app builds a roughly 10 lakh INR shadow portfolio from live prices:

- 20% reserved as INR
- 10% reserved as USDT
- 70% distributed across tracked tokens using the configured weights

## Dashboard

The dashboard in [`dashboard.py`](/home/shubh/codes/csk-triangular-arb/dashboard.py) serves [`static/index.html`](/home/shubh/codes/csk-triangular-arb/static/index.html) and pushes updates over `EventSource` from `/events`.

For each configured symbol, the UI shows:

- latest best net spread
- selected arbitrage path
- compact `SYMBOL/INR` and `SYMBOL/USDT` depth snapshots on the main cards
- recent activity on the main cards
- spread history sparkline on the cards
- modal detail view with:
  - path start currency
  - start amount
  - projected INR
  - recent execution stats
  - shadow balances
  - spread history chart
  - shadow P&L chart
- modal depth view for all three books:
  - `SYMBOL/INR`
  - `SYMBOL/USDT`
  - `USDT/INR`
- recent activity feed with opportunity and shadow execution events
- fetch latency and cycle count

## Shadow Execution

`main.py` uses `ShadowExecutor` by default. No live orders are submitted.

The executor:

- starts from a config-driven shadow portfolio built from live market prices
- updates balances after each detected net opportunity
- uses VWAP-based fills instead of only top-of-book prices
- reports symbol, INR, and USDT balance deltas after each simulated cycle

This makes it safer to validate strategy behavior before wiring in real execution.

## CoinSwitch API Notes

- Authentication uses `COINSWITCH_API_KEY` and `COINSWITCH_SECRET_KEY`
- `COINSWITCH_API_SECRET` is also accepted as a fallback env name for compatibility
- Requests are signed with ed25519 in [`api_client.py`](/home/shubh/codes/csk-triangular-arb/api_client.py)
- `SYMBOL/USDT` depth is fetched from `binance` by default
- `USDT/INR` and `SYMBOL/INR` depth are fetched from `coinswitchx`
- The client handles `429` responses by returning empty books for that request

## Setup

### 1. Install Python dependencies

Install the packages used by the current code:

```bash
pip install aiohttp aiohttp_sse cryptography python-dotenv
```

### 2. Configure environment variables

Create a `.env` file in the project root:

```env
COINSWITCH_API_KEY=your_api_key
COINSWITCH_SECRET_KEY=your_hex_secret_key
TAKER_FEE=0.001
TDS_RATE=0.01
```

Notes:

- `COINSWITCH_SECRET_KEY` must be a valid hex string
- `COINSWITCH_API_SECRET` is also accepted by the current code
- `TAKER_FEE` and `TDS_RATE` are optional because defaults exist in `config.py`
- Slack alerts are optional. Add `SLACK_WEBHOOK_URL` if you want webhook notifications

Optional Slack env vars:

```env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_ALERTS_ENABLED=true
SLACK_OPPORTUNITY_ALERTS_ENABLED=true
SLACK_EXECUTION_ALERTS_ENABLED=true
SLACK_ALERT_COOLDOWN_SECONDS=60
SLACK_ALERT_USERNAME=OmniArb
```

### 3. Tune symbols and limits

Update [`config.py`](/home/shubh/codes/csk-triangular-arb/config.py) if you want to:

- add or remove supported tokens
- adjust `POLLING_INTERVAL`
- change per-asset exposure limits
- change shadow portfolio size, reserves, or token weights

If you want to explore more symbols before editing `SYMBOLS`, use the pair-discovery script:

```bash
python scripts/list_pairs.py
```

Useful variants:

```bash
python scripts/list_pairs.py --json
python scripts/list_pairs.py --spot-exchange coinswitchx --cross-exchange binance
python scripts/list_pairs.py --top 25 --min-volume 100000
```

The script:

- fetches the current spot pair list from CoinSwitch
- fetches the current active coin list
- checks whether `USDT/INR` depth exists
- verifies which `SYMBOL/USDT` books exist on the configured cross exchange
- ranks the usable symbols by INR quote volume
- supports `--top` and `--min-volume` to reduce noisy long-tail assets
- prints a `triangular-ready symbols` shortlist you can copy into `config.SYMBOLS`

### 4. Run the engine

```bash
python main.py
```

This starts the multi-symbol arbitrage loop in shadow mode and logs opportunities to the terminal.

If Slack alerts are configured, `main.py` will also send webhook notifications for:

- detected opportunities
- shadow trade executions

### 5. Run the dashboard

In another terminal:

```bash
python dashboard.py
```

Then open `http://localhost:8080`.

## Example Runtime Flow

1. Fetch all required books for the configured symbol list
2. Group market depth into per-symbol triangles
3. Calculate all four paths for each symbol
4. Compare gross and net yields
5. Pick the best path
6. Stream the result to the dashboard
7. In `main.py`, shadow-execute positive net opportunities

## Important Limitations

- This repo currently simulates execution only
- There is no live order placement path enabled in the current runtime
- The dashboard reads market state only and does not trade
- Empty or shallow books will zero out the affected path
- Tight polling can still hit exchange rate limits depending on symbol count and market conditions

## Safety Reminder

Before enabling any real trading flow, review:

- fee assumptions
- TDS treatment
- exposure caps
- actual available liquidity
- CoinSwitch account permissions
- failure handling and order state reconciliation
