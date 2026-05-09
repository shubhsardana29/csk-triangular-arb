"""Entry point — wiring only.

Creates all components, wires callbacks, and calls engine.run().
No strategy logic lives here.

Env vars:
  USE_REST_FALLBACK=1       — use 1.5s REST polling instead of 10Hz WS feeds
  EXECUTION_MODE=shadow     — paper-trade only (default)
  EXECUTION_MODE=real       — place real limit orders via CSK REST
"""

import asyncio
import logging
import os
from decimal import Decimal

from dotenv import load_dotenv

import config
from api_client import CoinSwitchClient
from core.models import PathResult
from feeds.binance_depth_ws import BinanceDepthFeed
from feeds.csk_public_ws import CSKPublicWS
from slack_notifier import SlackNotifier
from strategy.tri_ranker import TriRanker
from strategy.shadow_executor import ShadowExecutor
from strategy.tri_engine import TriEngine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)
config.log_config()


def _fmt_pct(value: Decimal) -> str:
    return f"{float(value) * 100:+.4f}%"


def _format_opportunity_alert(symbol: str, net: PathResult, gross: PathResult) -> str:
    return (
        f":rotating_light: Opportunity: {symbol}\n"
        f"Case: {net.logical_case_label} ({net.inventory_mode})\n"
        f"Route: {net.direction}\n"
        f"Net: {_fmt_pct(net.profit_pct)}  Gross: {_fmt_pct(gross.profit_pct)}\n"
        f"Start: {float(net.executable_qty):.6f} {net.base_currency}\n"
        f"Projected INR: {float(net.expected_profit_inr):+.2f}"
    )


def _format_execution_alert(symbol: str, net: PathResult, result: dict) -> str:
    bal = result.get("result_balances", {})
    return (
        f":white_check_mark: Shadow trade: {symbol}\n"
        f"INR Δ {float(result.get('inr_variance', 0)):+.2f}  "
        f"{symbol} Δ {float(result.get('symbol_variance', 0)):+.6f}  "
        f"USDT Δ {float(result.get('usdt_variance', 0)):+.6f}\n"
        f"Balances → INR: {float(bal.get('INR', 0)):,.2f}  "
        f"USDT: {float(bal.get('USDT', 0)):,.4f}  "
        f"{symbol}: {float(bal.get(symbol, 0)):,.6f}"
    )


def _build_executor(execution_mode: str, client: CoinSwitchClient):
    """Build the appropriate executor based on EXECUTION_MODE."""
    if execution_mode == "real":
        from strategy.tri_executor import TriExecutor
        log.warning(
            "[main] EXECUTION_MODE=real — LIVE ORDERS will be placed on CSK"
        )
        return TriExecutor(client=client, fee=config.TAKER_FEE, tds=config.TDS_RATE)

    log.info("[main] EXECUTION_MODE=shadow — paper trading only")
    return ShadowExecutor({}, fee=config.TAKER_FEE, tds=config.TDS_RATE)


async def main() -> None:
    use_rest       = os.getenv("USE_REST_FALLBACK", "").lower() in {"1", "true", "yes"}
    execution_mode = os.getenv("EXECUTION_MODE", "shadow").strip().lower()

    notifier = SlackNotifier(
        webhook_url=config.SLACK_WEBHOOK_URL,
        enabled=config.SLACK_ALERTS_ENABLED,
        cooldown_seconds=config.SLACK_ALERT_COOLDOWN_SECONDS,
        username=config.SLACK_ALERT_USERNAME,
    )

    client   = CoinSwitchClient(config.COINSWITCH_API_KEY, config.COINSWITCH_SECRET_KEY)
    ranker   = TriRanker()
    executor = _build_executor(execution_mode, client)

    async def on_opportunity(symbol: str, net: PathResult, gross: PathResult, result: dict) -> None:
        log.info(
            "[%s] path=%d  %s  yield=%s  gross=%s  qty=%.6f %s  profit_inr=%+.2f",
            symbol, net.path_id, net.logical_case_label,
            _fmt_pct(net.profit_pct), _fmt_pct(gross.profit_pct),
            float(net.executable_qty), net.base_currency,
            float(net.expected_profit_inr),
        )
        if config.SLACK_OPPORTUNITY_ALERTS_ENABLED:
            await notifier.send(
                _format_opportunity_alert(symbol, net, gross),
                key=f"opp:{symbol}:{net.direction}:{round(float(net.profit_pct), 4)}",
            )
        if config.SLACK_EXECUTION_ALERTS_ENABLED and result:
            await notifier.send(
                _format_execution_alert(symbol, net, result),
                key=f"exec:{symbol}:{net.direction}",
            )

    async with client:
        if use_rest:
            symbols = config.SYMBOLS
            log.info("REST mode — using fallback symbol list (%d symbols)", len(symbols))
        else:
            symbols = await client.discover_symbols(
                whitelist=config.SYMBOLS_WHITELIST,
                blacklist=config.SYMBOLS_BLACKLIST,
                max_symbols=config.MAX_SYMBOLS,
            )
            if not symbols:
                log.error("Symbol discovery returned 0 eligible symbols — exiting")
                return

        binance_feed = None
        csk_ws       = None
        if not use_rest:
            binance_feed = BinanceDepthFeed(symbols)
            csk_ws       = CSKPublicWS()

        engine = TriEngine(
            client=client,
            ranker=ranker,
            executor=executor,
            symbols=symbols,
            binance_feed=binance_feed,
            csk_ws=csk_ws,
            on_opportunity=on_opportunity,
        )

        log.info(
            "Starting TriEngine — %s mode — %s execution — %d symbols",
            "REST fallback" if use_rest else "WebSocket",
            execution_mode,
            len(symbols),
        )
        async with notifier:
            await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Engine stopped.")
