import asyncio
import logging
import os
from api_client import CoinSwitchClient
from arbitrage_engine import ArbitrageEngine, ShadowExecutor
from dotenv import load_dotenv
import config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def main():
    client = CoinSwitchClient(
        os.getenv("COINSWITCH_API_KEY"),
        os.getenv("COINSWITCH_SECRET_KEY")
    )
    engine = ArbitrageEngine()
    
    # Initial Shadow Balances
    balances = {s: 1.0 for s in config.SYMBOLS}
    balances["INR"] = 1000000.0
    balances["USDT"] = 10000.0
    
    executor = ShadowExecutor(balances, config.TAKER_FEE, config.TDS_RATE)
    
    logger.info(f"Starting Multi-Symbol Arbitrage Engine: {', '.join(config.SYMBOLS)}")
    
    cycle = 0
    async with client:
        while True:
            try:
                # 1. Fetch live books for all symbols
                tri_books = await client.fetch_triangular_books()
                
                # 2. Calculate opportunities
                all_opps = engine.calculate_multi_symbol_arbitrage(tri_books, executor.balances)
                
                # 3. Process each symbol
                for symbol, opps in all_opps.items():
                    net_opp = opps["net"]
                    gross_opp = opps["gross"]
                    
                    if net_opp["opportunity"]:
                        logger.info(f"🚀 --- OPPORTUNITY: {symbol} ---")
                        logger.info(f"Direction: {net_opp['direction']}")
                        logger.info(f"Yield: {net_opp['profit_pct']:.4f}% (Gross: {gross_opp['profit_pct']:.4f}%)")
                        
                        # Execute Shadow Trade
                        result = executor.execute(net_opp, tri_books[symbol])
                        logger.info(f"Shadow Result: {symbol} Var: {result['symbol_variance']:.4f}, INR Var: {result['inr_variance']:.2f}")
                    
                    # Periodic summary
                    if cycle % 20 == 0:
                        logger.info(f"[{symbol} Cycle {cycle}] Net: {net_opp['profit_pct']:.4f}%")
                
                cycle += 1
                await asyncio.sleep(config.POLLING_INTERVAL)
                
            except Exception as e:
                logger.error(f"Main Loop Error: {e}")
                await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Engine stopped by user.")
