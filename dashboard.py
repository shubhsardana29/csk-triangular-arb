import asyncio
import json
import logging
from aiohttp import web
from aiohttp_sse import sse_response
from api_client import CoinSwitchClient
from arbitrage_engine import ArbitrageEngine
import config
from dotenv import load_dotenv
import os
import time

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class AppState:
    def __init__(self):
        self.data = {
            "symbols": {}, # symbol -> latest data
            "cycle_count": 0,
            "status": "Initializing...",
            "last_update": 0
        }
        self.data_condition = None

    async def update(self, symbol_data: dict, cycle: int, latency: float):
        async with self.data_condition:
            self.data["symbols"] = symbol_data
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
                await app_state.data_condition.wait()
                payload = json.dumps(app_state.data)
                await resp.send(payload)

async def index(request):
    return web.FileResponse('./static/index.html')

async def market_loop(app):
    client = CoinSwitchClient(
        os.getenv("COINSWITCH_API_KEY"),
        os.getenv("COINSWITCH_SECRET_KEY")
    )
    engine = ArbitrageEngine()
    
    # Initialize condition in the loop context
    app_state.data_condition = asyncio.Condition()
    
    # Shadow balances for visualization
    shadow_balances = {s: 1.0 for s in config.SYMBOLS}
    shadow_balances["INR"] = 100000.0
    shadow_balances["USDT"] = 1000.0
    
    cycle = 0
    async with client:
        while True:
            try:
                start_time = time.time()
                
                # 1. Fetch all books in parallel
                tri_books = await client.fetch_triangular_books(config.SYMBOLS)
                fetch_latency = (time.time() - start_time) * 1000
                
                # 2. Calculate opportunities for all symbols
                all_opps = engine.calculate_multi_symbol_arbitrage(tri_books, shadow_balances)
                
                # 3. Update state
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
