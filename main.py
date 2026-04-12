import asyncio
import logging
from api_client import CoinSwitchClient
from arbitrage_engine import ArbitrageEngine, ShadowExecutor
from dotenv import load_dotenv
import config
from slack_notifier import SlackNotifier

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _format_opportunity_alert(symbol: str, net_opp: dict, gross_opp: dict) -> str:
    start_amount = net_opp.get("executable_qty", 0.0)
    expected_inr = net_opp.get("expected_profit_inr", 0.0)
    return (
        f":rotating_light: Opportunity detected for {symbol}\n"
        f"Path: {net_opp.get('direction', 'Unknown')}\n"
        f"Net spread: {net_opp.get('profit_pct', 0.0):+.4f}% | Gross: {gross_opp.get('profit_pct', 0.0):+.4f}%\n"
        f"Start amount: {start_amount:.6f} {net_opp.get('base_currency', 'N/A')}\n"
        f"Projected INR: {expected_inr:+.2f}"
    )


def _format_execution_alert(symbol: str, result: dict) -> str:
    balances = result.get("result_balances", {})
    return (
        f":white_check_mark: Shadow trade executed for {symbol}\n"
        f"INR change: {result.get('inr_variance', 0.0):+.2f}\n"
        f"{symbol} change: {result.get('symbol_variance', 0.0):+.6f}\n"
        f"USDT change: {result.get('usdt_variance', 0.0):+.6f}\n"
        f"Balances -> INR: {balances.get('INR', 0.0):,.2f}, USDT: {balances.get('USDT', 0.0):,.4f}, {symbol}: {balances.get(symbol, 0.0):,.6f}"
    )


async def main():
    client = CoinSwitchClient(
        config.COINSWITCH_API_KEY,
        config.COINSWITCH_SECRET_KEY
    )
    engine = ArbitrageEngine()
    executor = None
    notifier = SlackNotifier(
        webhook_url=config.SLACK_WEBHOOK_URL,
        enabled=config.SLACK_ALERTS_ENABLED,
        cooldown_seconds=config.SLACK_ALERT_COOLDOWN_SECONDS,
        username=config.SLACK_ALERT_USERNAME,
    )

    logger.info(f"Starting Multi-Symbol Arbitrage Engine: {', '.join(config.SYMBOLS)}")
    if notifier.enabled:
        logger.info("Slack webhook alerts enabled.")
    else:
        logger.info("Slack webhook alerts disabled. Engine will continue normally without notifications.")

    cycle = 0
    async with client, notifier:
        while True:
            try:
                # 1. Fetch live books for all symbols
                tri_books = await client.fetch_triangular_books()

                if executor is None:
                    balances = config.build_initial_shadow_balances(config.SYMBOLS, tri_books)
                    executor = ShadowExecutor(balances, config.TAKER_FEE, config.TDS_RATE)
                    logger.info(
                        "Initialized shadow portfolio at ~₹%s across %s with INR and USDT reserves",
                        f"{config.SHADOW_PORTFOLIO_TOTAL_INR:,.0f}",
                        ", ".join(config.SYMBOLS)
                    )
                
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

                        if config.SLACK_OPPORTUNITY_ALERTS_ENABLED:
                            await notifier.send(
                                _format_opportunity_alert(symbol, net_opp, gross_opp),
                                key=f"opp:{symbol}:{net_opp.get('direction')}:{round(net_opp.get('profit_pct', 0.0), 4)}",
                            )
                        
                        # Execute Shadow Trade
                        result = executor.execute(net_opp, tri_books[symbol])
                        logger.info(f"Shadow Result: {symbol} Var: {result['symbol_variance']:.4f}, INR Var: {result['inr_variance']:.2f}")

                        if config.SLACK_EXECUTION_ALERTS_ENABLED:
                            await notifier.send(
                                _format_execution_alert(symbol, result),
                                key=f"exec:{symbol}:{net_opp.get('direction')}",
                            )
                    
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
