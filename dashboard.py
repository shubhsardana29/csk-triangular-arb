"""
Real-time Arbitrage Dashboard Server
Serves a beautiful OKLCH-themed web UI and streams live market data via SSE.
"""
import asyncio
import json
import os
import time
import logging
import aiohttp
from aiohttp import web
from aiohttp_sse import sse_response
from dotenv import load_dotenv
from arbitrage_engine import ArbitrageEngine, ShadowExecutor
import config

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

class AppState:
    def __init__(self):
        self.latest_data = {}
        self.trade_log = []
        self.data_condition = None

async def index(request):
    return web.FileResponse('./static/index.html')

async def sse_handler(request):
    state = request.app['state']
    try:
        async with sse_response(request) as resp:
            # Push latest data immediately on connect
            if state.latest_data:
                await resp.send(json.dumps(state.latest_data), event='tick')
                
            while True:
                async with state.data_condition:
                    await state.data_condition.wait()
                
                if state.latest_data:
                    await resp.send(json.dumps(state.latest_data), event='tick')
    except Exception:
        pass
    return resp

async def trades_handler(request):
    return web.json_response(request.app['state'].trade_log[-100:])

async def market_loop(app):
    state = app['state']
    api_key = os.getenv("COINSWITCH_API_KEY")
    secret_key = os.getenv("COINSWITCH_SECRET_KEY")
    
    if not api_key or not secret_key or api_key == "your_api_key_here":
        logging.error("Dashboard: API Keys missing. Cannot start live dashboard.")
        return
        
    balances = {"BTC": 0.25, "INR": 1500000.0, "USDT": 8000.0}
    engine = ArbitrageEngine(taker_fee=config.TAKER_FEE, tds_rate=config.TDS_RATE)
    executor = ShadowExecutor(balances, fee=config.TAKER_FEE, tds=config.TDS_RATE)
    
    from api_client import CoinSwitchClient
    logging.info("Dashboard: Live API Mode (Shadow Balances)")
    
    async with aiohttp.ClientSession() as session:
        market = CoinSwitchClient(api_key, secret_key, session=session)
        cycle = 0
        while True:
            cycle += 1
            try:
                start_time = time.time()
                books = await market.fetch_triangular_books()
                fetch_duration = (time.time() - start_time) * 1000
                
                opp_data = engine.calculate_triangular_arbitrage(books, balances)
                opportunity = opp_data["net"]
                gross_opp = opp_data["gross"]
                
                def safe_price(pair, side):
                    try: return float(books[pair][side][0][0])
                    except: return 0
                
                def get_depth_levels(pair, side, limit=5):
                    levels = books.get(pair, {}).get(side, [])
                    return [[float(p), float(q)] for p, q in levels[:limit]]
                
                tick = {
                    "cycle": cycle,
                    "timestamp": int(time.time() * 1000),
                    "fetch_latency_ms": round(fetch_duration, 2),
                    "btc_inr_bid": safe_price("BTC/INR", "bids"),
                    "btc_inr_ask": safe_price("BTC/INR", "asks"),
                    "btc_usdt_bid": safe_price("BTC/USDT", "bids"),
                    "btc_usdt_ask": safe_price("BTC/USDT", "asks"),
                    "usdt_inr_bid": safe_price("USDT/INR", "bids"),
                    "usdt_inr_ask": safe_price("USDT/INR", "asks"),
                    
                    # Depth Data
                    "depth": {
                        "btc_inr": {
                            "bids": get_depth_levels("BTC/INR", "bids"),
                            "asks": get_depth_levels("BTC/INR", "asks")
                        },
                        "btc_usdt": {
                            "bids": get_depth_levels("BTC/USDT", "bids"),
                            "asks": get_depth_levels("BTC/USDT", "asks")
                        },
                        "usdt_inr": {
                            "bids": get_depth_levels("USDT/INR", "bids"),
                            "asks": get_depth_levels("USDT/INR", "asks")
                        }
                    },
                    
                    "profit_pct": round(opportunity.get("profit_pct", 0) * 100, 4),
                    "gross_profit_pct": round(gross_opp.get("profit_pct", 0) * 100, 4),
                    "opportunity": opportunity["opportunity"],
                    "direction": opportunity.get("direction", ""),
                    "profit_inr": round(opportunity.get("expected_profit_inr", 0), 2),
                    "balances": balances,
                    "taker_fee": config.TAKER_FEE,
                    "tds_rate": config.TDS_RATE
                }
                
                if opportunity["opportunity"]:
                    result = executor.execute(opportunity, books)
                    balances = result["result_balances"]
                    state.trade_log.append(tick)
                    logging.info(f"🚀 Opportunity! {opportunity['direction']} Profit: ₹{opportunity['expected_profit_inr']:.2f}")
                
                state.latest_data = tick
                async with state.data_condition:
                    state.data_condition.notify_all()
                
                if cycle % 20 == 0:
                    logging.info(f"Dashboard: Active - Cycle {cycle} (Avg Latency: {fetch_duration:.1f}ms)")
                    
                await asyncio.sleep(0.05)
            except Exception as e:
                logging.exception(f"Dashboard Market Loop Error: {e}")
                await asyncio.sleep(1)

async def start_background_tasks(app):
    state = AppState()
    state.data_condition = asyncio.Condition()
    app['state'] = state
    app['market_task'] = asyncio.create_task(market_loop(app))

async def cleanup_background_tasks(app):
    app['market_task'].cancel()
    try:
        await app['market_task']
    except asyncio.CancelledError:
        pass

def create_app():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/sse', sse_handler)
    app.router.add_get('/api/trades', trades_handler)
    app.router.add_static('/static/', path='./static', name='static')
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app

if __name__ == '__main__':
    app = create_app()
    logging.info("Dashboard available at http://localhost:8080")
    web.run_app(app, host='0.0.0.0', port=8080)
