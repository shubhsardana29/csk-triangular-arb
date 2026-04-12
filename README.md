# ⚡ CoinSwitch PRO: Triangular Arbitrage Engine

A high-frequency, low-latency triangular arbitrage trading system for **CoinSwitch PRO**. This engine monitors spread inefficiencies across three base markets in four distinct paths, featuring a premium OKLCH-themed web dashboard and a secured ed25519 authenticated API client.

---

## ⚡ Performance Benchmarks
- **Update Frequency:** ~8-14 Hz (8-14 cycles per second).
- **Core Latency:** ~70ms per API fetch cycle.
- **Connection Logic:** Persistent HTTP sessions with keep-alive to eliminate TCP/TLS handshake overhead.
- **Real-time Streaming:** Event-driven SSE (Server-Sent Events) for instantaneous UI updates.

---

## 🚀 How it Works (Triangular Arbitrage)

The engine identifies moments where prices between three assets (**BTC**, **USDT**, and **INR**) disconnect. Instead of simple cross-exchange arbitrage, this system cycles capital through a triangle to arrive back at the base asset with a profit.

### The 4 Dynamic Paths
The engine simultaneously tracks **Buy-First** and **Sell-First** scenarios:

- **Path 1**: SELL BTC/INR $\rightarrow$ BUY USDT/INR $\rightarrow$ BUY BTC/USDT (via Binance/Kucoin)
- **Path 2**: SELL BTC/USDT (via Binance/Kucoin) $\rightarrow$ SELL USDT/INR $\rightarrow$ BUY BTC/INR
- **Path 3**: BUY BTC/INR $\rightarrow$ SELL BTC/USDT (via Binance/Kucoin) $\rightarrow$ SELL USDT/INR
- **Path 4**: BUY USDT/INR $\rightarrow$ BUY BTC/USDT (via Binance/Kucoin) $\rightarrow$ SELL BTC/INR

---

## 💰 Fees & TDS (The Math)

Arbitrage is only profitable if the spread exceeds the "Toll" taken by the exchange and the government.

### 1. Exchange Trading Fees
The system supports configurable fees (defaulting to **0.1% taker fee** for VIP tiers). Since a triangular cycle involves **3 trades**, the system calculates net profitability after compounding these fees.
- **Formula**: `Profit = (1 - fee)^3` must be `> 1.0` for gross profit.

### 2. TDS (1% Tax Deducted at Source)
Under Indian Tax Law, a **1% TDS** is applicable on the **Sell Side** of every VDA (Virtual Digital Asset) transaction.
- In our cycles, every "Sell" leg triggers a 1% withhold.
- The engine computes **Net P&L** by accounting for both taker fees and TDS logs.

---

## 🛠️ System Architecture

- **Low-Latency Aggregator**: Uses `aiohttp` persistent sessions to maintain open connections to CoinSwitch, reducing fetch times by ~72%.
- **`api_client.py`**: Handles ed25519 signature generation and authenticated REST requests with session pooling.
- **`arbitrage_engine.py`**: High-performance math engine for spread calculation and "Shadow Execution".
- **`dashboard.py`**: Optimized SSE server using an app-bound state machine for 100% stable real-time streaming.
- **Visual Dashboard**: Premium OKLCH-themed UI with:
  - **Live Orderbook Depth**: Top 5 levels of bids/asks for all trading pairs.
  - **Real-time Metrics**: Compounded spread charts and high-resolution cycle counters.
  - **Performance Badge**: Continuous monitoring of API fetch latency.

---

## 🚦 Quick Start

1. **Install Dependencies**:
   ```bash
   pip install aiohttp cryptography python-dotenv aiohttp_sse
   ```

2. **Configure Environment**:
   Create a `.env` file from the example:
   ```env
   COINSWITCH_API_KEY=your_key
   COINSWITCH_SECRET_KEY=your_hex_secret
   ```

3. **Launch the System**:
   - **Main Engine**: `python main.py`
   - **Web Dashboard**: `python dashboard.py` (Visit [localhost:8080](http://localhost:8080))

---

## 🔒 Security
- **No Private Keys**: Uses API-level ed25519 credential keys only.
- **Shadow Mode**: By default, the `ShadowExecutor` is active. It logs theoretical trades to prove profitability without executing real orders.

> [!IMPORTANT]
> To enable live trading, the `POST /trade/api/v2/order` logic in `api_client.py` must be configured for your account and risk limits.
