import asyncio
import logging
import random
import os
from arbitrage_engine import ArbitrageEngine, ShadowExecutor

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

from api_client import CoinSwitchClient
from dotenv import load_dotenv
import config

async def main():
    load_dotenv()
    logging.info(f"Starting Arbitrage Shadow System (Live Market Data) - Fee: {config.TAKER_FEE*100}%")
    
    api_key = os.getenv("COINSWITCH_API_KEY")
    secret_key = os.getenv("COINSWITCH_SECRET_KEY")
    
    if not api_key or not secret_key or api_key == "your_api_key_here":
        logging.error("CRITICAL: API Keys missing in .env. Cannot start live system.")
        return
        
    # Init balances with 40 Lakhs INR total portfolio value
    balances = {
        "BTC": 0.25,        # ~17.2L INR
        "INR": 1500000.0,   # 15.0L INR
        "USDT": 8000.0      # ~7.8L INR (Total ~40L)
    }
    
    engine = ArbitrageEngine(taker_fee=config.TAKER_FEE, tds_rate=config.TDS_RATE) 
    executor = ShadowExecutor(balances, fee=config.TAKER_FEE, tds=config.TDS_RATE)
    
    logging.info("Connecting to CoinSwitch API...")
    
    import aiohttp
    async with aiohttp.ClientSession() as session:
        market = CoinSwitchClient(api_key, secret_key, session=session)
        
        cycles = 0
        while True:
            cycles += 1
            try:
                books = await market.fetch_triangular_books()
                await asyncio.sleep(0.05) # 50ms polling 
            
                opportunity_data = engine.calculate_triangular_arbitrage(books, balances)
                opportunity = opportunity_data["net"]
                gross_opp = opportunity_data["gross"]
                
                # Extract prices for logging summary
                btc_inr_bid = float(books["BTC/INR"]["bids"][0][0]) if books["BTC/INR"]["bids"] else 0
                btc_inr_ask = float(books["BTC/INR"]["asks"][0][0]) if books["BTC/INR"]["asks"] else 0

                if opportunity["opportunity"]:
                    logging.info(f"🚀 --- OPPORTUNITY DETECTED (Cycle {cycles}) ---")
                    logging.info(f"Direction: {opportunity['direction']}")
                    logging.info(f"Expected Net Profit: ₹{opportunity['expected_profit_inr']:.2f} ({opportunity['profit_pct']*100:.4f}%)")
                    
                    # Execute
                    result = executor.execute(opportunity, books)
                    logging.info("Execution Output:")
                    logging.info(f"  BTC Variance: {result['btc_variance']:.8f}")
                    logging.info(f"  Total Portfolio INR Growth: ₹{result['total_value_increase_inr']:.2f}")
                    
                    # Sync balances for next loop
                    balances = result["result_balances"]
                
                if cycles % 20 == 0:
                    logging.info(
                        f"[Cycle {cycles}] "
                        f"BTC/INR: ₹{btc_inr_bid:,.0f}/{btc_inr_ask:,.0f} | "
                        f"Spreads: Gross:{gross_opp['profit_pct']*100:+.2f}% / Net:{opportunity['profit_pct']*100:+.2f}%"
                    )
            except Exception as e:
                logging.exception(f"Main Loop Error: {e}")
                await asyncio.sleep(1)
        
    logging.info("Simulation completed.")

if __name__ == "__main__":
    asyncio.run(main())
