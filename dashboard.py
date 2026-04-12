import asyncio
import json
import logging
from aiohttp import web
from aiohttp_sse import sse_response
from api_client import CoinSwitchClient
from arbitrage_engine import ArbitrageEngine, ShadowExecutor
import config
from dotenv import load_dotenv
import time

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class AppState:
    def __init__(self):
        self.data = {
            "symbols": {}, # symbol -> latest data
            "recent_events": {},
            "cycle_count": 0,
            "status": "Initializing...",
            "last_update": 0
        }
        self.data_condition = None
        self._previous_symbols = {}
        self._recent_events = {}
        self._last_execution = {}
        self._cumulative_pnl = {}
        self._pnl_history = {}

    def _push_event(self, symbol: str, level: str, title: str, detail: str):
        # Keep only a short rolling event feed per symbol for the modal UI.
        events = self._recent_events.setdefault(symbol, [])
        events.insert(0, {
            "level": level,
            "title": title,
            "detail": detail,
            "timestamp": time.time()
        })
        self._recent_events[symbol] = events[:8]

    def record_execution(self, symbol: str, opportunity: dict, result: dict):
        # Shadow execution feeds both the recent activity timeline and the P&L chart.
        self._cumulative_pnl[symbol] = self._cumulative_pnl.get(symbol, 0.0) + result.get("inr_variance", 0.0)
        self._last_execution[symbol] = {
            "timestamp": time.time(),
            "direction": opportunity.get("direction"),
            "profit_pct": opportunity.get("profit_pct", 0.0),
            "expected_profit_inr": opportunity.get("expected_profit_inr", 0.0),
            "symbol_variance": result.get("symbol_variance", 0.0),
            "inr_variance": result.get("inr_variance", 0.0),
            "cumulative_inr_pnl": self._cumulative_pnl[symbol],
            "balances": result.get("result_balances", {}).copy()
        }
        self._push_event(
            symbol,
            "good",
            "Shadow trade executed",
            f"INR Δ {result.get('inr_variance', 0.0):+.2f} | {symbol} Δ {result.get('symbol_variance', 0.0):+.6f}"
        )

    async def update(self, symbol_data: dict, cycle: int, latency: float):
        for symbol, payload in symbol_data.items():
            current_net = payload.get("net", {})
            previous_net = self._previous_symbols.get(symbol, {}).get("net", {})

            current_live = bool(current_net.get("opportunity"))
            previous_live = bool(previous_net.get("opportunity"))

            if current_live and not previous_live:
                self._push_event(
                    symbol,
                    "good",
                    "Opportunity detected",
                    f"{current_net.get('profit_pct', 0):+.4f}% via {current_net.get('direction', 'Unknown path')}"
                )
            elif current_live and previous_live and current_net.get("direction") != previous_net.get("direction"):
                self._push_event(
                    symbol,
                    "info",
                    "Route changed",
                    f"Now tracking {current_net.get('direction', 'Unknown path')}"
                )
            elif not current_live and previous_live:
                self._push_event(
                    symbol,
                    "warn",
                    "Opportunity cleared",
                    previous_net.get("reason", "Spread moved below threshold")
                )

            payload["last_execution"] = self._last_execution.get(symbol)
            payload["cumulative_shadow_pnl"] = self._cumulative_pnl.get(symbol, 0.0)

            # Persist a rolling per-symbol P&L series for the dashboard chart.
            pnl_series = self._pnl_history.setdefault(symbol, [])
            pnl_series.append(self._cumulative_pnl.get(symbol, 0.0))
            self._pnl_history[symbol] = pnl_series[-60:]
            payload["shadow_pnl_history"] = self._pnl_history[symbol]

        self._previous_symbols = symbol_data

        async with self.data_condition:
            self.data["symbols"] = symbol_data
            self.data["recent_events"] = self._recent_events
            self.data["cycle_count"] = cycle
            self.data["last_latency"] = f"{latency:.1f}ms"
            self.data["status"] = "Active"
            self.data["last_update"] = time.time()
            self.data_condition.notify_all()

app_state = AppState()

async def sse_handler(request):
    async with sse_response(request) as resp:
        logger.info("New SSE client connected")
        while True:
            async with app_state.data_condition:
                # Every update wakes connected clients with the latest full dashboard snapshot.
                await app_state.data_condition.wait()
                payload = json.dumps(app_state.data)
                await resp.send(payload)

async def index(request):
    return web.FileResponse('./static/index.html')

async def market_loop(app):
    client = CoinSwitchClient(
        config.COINSWITCH_API_KEY,
        config.COINSWITCH_SECRET_KEY
    )
    engine = ArbitrageEngine()
    
    # Initialize condition in the loop context
    app_state.data_condition = asyncio.Condition()
    
    executor = None
    
    cycle = 0
    async with client:
        while True:
            try:
                start_time = time.time()
                
                # 1. Pull the latest depth snapshot for every configured symbol triangle.
                tri_books = await client.fetch_triangular_books(config.SYMBOLS)
                fetch_latency = (time.time() - start_time) * 1000

                if executor is None:
                    # Seed the shadow portfolio from live prices so the UI starts from a realistic book value.
                    balances = config.build_initial_shadow_balances(config.SYMBOLS, tri_books)
                    executor = ShadowExecutor(balances, config.TAKER_FEE, config.TDS_RATE)
                    logger.info("Dashboard shadow portfolio initialized at ~₹%s", f"{config.SHADOW_PORTFOLIO_TOTAL_INR:,.0f}")
                
                # 2. Evaluate best gross/net path per symbol using the latest balances.
                all_opps = engine.calculate_multi_symbol_arbitrage(tri_books, executor.balances)

                # 3. Simulate fills for actionable paths so the dashboard can show execution-side effects.
                for symbol, opps in all_opps.items():
                    net_opp = opps.get("net", {})
                    if net_opp.get("opportunity") and net_opp.get("executable_qty", 0) > 0:
                        result = executor.execute(net_opp, tri_books[symbol])
                        app_state.record_execution(symbol, net_opp, result)

                    opps["shadow_balances"] = {
                        "symbol": executor.balances.get(symbol, 0.0),
                        "INR": executor.balances.get("INR", 0.0),
                        "USDT": executor.balances.get("USDT", 0.0),
                    }
                
                # 4. Update state
                await app_state.update(all_opps, cycle, fetch_latency)
                
                cycle += 1
                if cycle % 20 == 0:
                    logger.info(f"Dashboard: Monitoring {len(config.SYMBOLS)} tokens - Cycle {cycle} (Lat: {fetch_latency:.1f}ms)")
                
                # Throttle to avoid rate limits
                await asyncio.sleep(config.POLLING_INTERVAL) 
                
            except Exception as e:
                logger.error(f"Error in market loop: {e}")
                await asyncio.sleep(5)

async def start_background_tasks(app):
    app['market_task'] = asyncio.create_task(market_loop(app))

async def cleanup_background_tasks(app):
    app['market_task'].cancel()
    await app['market_task']

def main():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/events', sse_handler)
    app.router.add_static('/static/', path='./static/', name='static')
    
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    web.run_app(app, host='0.0.0.0', port=8080)

if __name__ == '__main__':
    main()
