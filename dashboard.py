import asyncio
import collections
import json
import logging
from decimal import Decimal
from typing import Optional, Union
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from aiohttp_sse import sse_response
import os
from api_client import CoinSwitchClient
import config
from core.models import Depth, PathResult, TriBook, TwoLegResult
from feeds.binance_depth_ws import BinanceDepthFeed
from feeds.csk_public_ws import CSKPublicWS
from strategy.tri_ranker import TriRanker
from strategy.two_leg_ranker import TwoLegRanker
from strategy.shadow_executor import ShadowExecutor, ShadowTwoLegExecutor
from strategy.tri_engine import TriEngine
from strategy.tri_rebalancer import TriRebalancer
from dotenv import load_dotenv
import time

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
config.log_config()


class DecimalEncoder(json.JSONEncoder):
    """Serialize Decimal as float at the JSON boundary. TriBook/Depth are not JSON-serializable."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (TriBook, Depth)):
            return None   # strip depth objects — dashboard doesn't render raw books
        return super().default(obj)


def _format_pct(value) -> str:
    return f"{float(value) * 100:+.4f}%"


def _best_mark_price(depth: Optional[Depth]) -> float:
    """Mid-price from a Depth snapshot. Returns float for dashboard rendering."""
    if depth is None:
        return 0.0
    bid = float(depth.bid)
    ask = float(depth.ask)
    if bid and ask:
        return (bid + ask) / 2.0
    return bid or ask or 0.0


def _two_leg_to_dict(r: TwoLegResult) -> dict:
    return {
        "opportunity":         r.opportunity,
        "reason":              r.reason,
        "direction":           r.direction,
        "buy_venue":           r.buy_venue,
        "sell_venue":          r.sell_venue,
        "spread_pct":          r.spread_pct,
        "profit_pct":          r.profit_pct,
        "executable_qty":      r.executable_qty,
        "base_currency":       r.base_currency,
        "expected_profit_inr": r.expected_profit_inr,
    }


def _path_to_dict(path: PathResult) -> dict:
    """Convert a PathResult to a JSON-compatible dict for the dashboard payload."""
    return {
        "opportunity":         path.opportunity,
        "reason":              path.reason,
        "direction":           path.direction,
        "logical_case":        path.logical_case,
        "logical_case_label":  path.logical_case_label,
        "inventory_mode":      path.inventory_mode,
        "thesis":              path.thesis,
        "path_id":             path.path_id,
        "executable_qty":      path.executable_qty,
        "base_currency":       path.base_currency,
        "expected_profit_inr": path.expected_profit_inr,
        "profit_pct":          path.profit_pct,
        "yield_ratio":         path.yield_ratio,
    }

SSE_PUSH_INTERVAL_S = 0.5   # push to browser at 2 Hz regardless of engine tick rate

class AppState:
    _RECENT_TRADES_MAX = 10   # rolling window of trades per symbol

    def __init__(self):
        self.data = {
            "symbols": {},
            "shadow_inventory": {},
            "recent_events": {},
            "cycle_count": 0,
            "status": "Initializing...",
            "last_update": 0
        }
        self.data_condition = None
        self._previous_symbols = {}
        self._recent_events: dict[str, list] = {}
        self._recent_trades: dict[str, collections.deque] = {}
        self._cumulative_pnl: dict[str, float] = {}
        self._pnl_history: dict[str, list] = {}
        self._last_push_ts: float = 0.0

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

    def record_execution(self, symbol: str, net: Union[PathResult, TwoLegResult], result: dict):
        inr_var   = float(result.get("inr_variance", 0))
        sym_var   = float(result.get("symbol_variance", 0))
        leg_label = getattr(net, "logical_case_label", None) or net.direction
        inv_mode  = getattr(net, "inventory_mode", "2-leg")
        is_2leg   = isinstance(net, TwoLegResult)

        self._cumulative_pnl[symbol] = self._cumulative_pnl.get(symbol, 0.0) + inr_var

        trade = {
            "timestamp":           time.time(),
            "leg":                 "2-leg" if is_2leg else "3-leg",
            "direction":           net.direction,
            "logical_case_label":  leg_label,
            "inventory_mode":      inv_mode,
            "profit_pct":          float(net.profit_pct),
            "expected_profit_inr": float(net.expected_profit_inr),
            "symbol_variance":     sym_var,
            "inr_variance":        inr_var,
            "cumulative_inr_pnl":  self._cumulative_pnl[symbol],
            "balances":            {
                k: float(v) for k, v in result.get("result_balances", {}).items()
                if k in (symbol, "INR", "USDT")
            },
        }

        deque = self._recent_trades.setdefault(
            symbol, collections.deque(maxlen=self._RECENT_TRADES_MAX)
        )
        deque.appendleft(trade)

        self._push_event(
            symbol,
            "good",
            f"Shadow {'2-leg' if is_2leg else '3-leg'} trade",
            f"INR Δ {inr_var:+.2f}  {symbol} Δ {sym_var:+.6f}  cumulative {self._cumulative_pnl[symbol]:+.2f}",
        )

    async def update(self, symbol_data: dict, cycle: int, latency: float, shadow_inventory: dict):
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
                    f"{_format_pct(current_net.get('profit_pct', 0))} via {current_net.get('logical_case_label', 'Unknown case')}"
                )
            elif current_live and previous_live and current_net.get("direction") != previous_net.get("direction"):
                self._push_event(
                    symbol,
                    "info",
                    "Route changed",
                    f"Now tracking {current_net.get('logical_case_label', 'Unknown case')} ({current_net.get('inventory_mode', 'n/a')})"
                )
            elif not current_live and previous_live:
                self._push_event(
                    symbol,
                    "warn",
                    "Opportunity cleared",
                    previous_net.get("reason", "Spread moved below threshold")
                )

            trades = list(self._recent_trades.get(symbol, []))
            payload["recent_trades"]       = trades
            payload["last_execution"]      = trades[0] if trades else None  # backward compat
            payload["trade_count"]         = len(trades)
            payload["cumulative_shadow_pnl"] = self._cumulative_pnl.get(symbol, 0.0)

            pnl_series = self._pnl_history.setdefault(symbol, [])
            pnl_series.append(self._cumulative_pnl.get(symbol, 0.0))
            self._pnl_history[symbol] = pnl_series[-60:]
            payload["shadow_pnl_history"] = self._pnl_history[symbol]

        self._previous_symbols = symbol_data

        now = time.time()
        if now - self._last_push_ts < SSE_PUSH_INTERVAL_S:
            # Engine ticks at 10Hz but SSE only needs 2Hz — skip this push.
            return
        self._last_push_ts = now

        async with self.data_condition:
            self.data["symbols"] = symbol_data
            self.data["shadow_inventory"] = shadow_inventory
            self.data["recent_events"] = self._recent_events
            self.data["cycle_count"] = cycle
            self.data["last_latency"] = f"{latency:.1f}ms"
            self.data["status"] = "Active"
            self.data["last_update"] = now
            self.data_condition.notify_all()

app_state = AppState()

async def sse_handler(request):
    async with sse_response(request) as resp:
        logger.info("New SSE client connected")
        while True:
            async with app_state.data_condition:
                # Every update wakes connected clients with the latest full dashboard snapshot.
                await app_state.data_condition.wait()
                payload = json.dumps(app_state.data, cls=DecimalEncoder)
                try:
                    await resp.send(payload)
                except (ClientConnectionResetError, ConnectionResetError, asyncio.CancelledError):
                    logger.info("SSE client disconnected")
                    break
    return resp

async def index(request):
    return web.FileResponse('./static/index.html')

def _build_shadow_inventory(
    balances: dict,
    tri_books: dict[str, TriBook],
) -> dict:
    """Assemble the shadow inventory dict for the dashboard state push."""
    from decimal import Decimal as _D
    zero = _D(0)

    positions = [
        {
            "symbol": symbol,
            "qty": balances.get(symbol, zero),
            "mark_price_inr": _best_mark_price(
                tri_books[symbol].s_inr if symbol in tri_books else None
            ),
            "market_value_inr": float(balances.get(symbol, zero)) * _best_mark_price(
                tri_books[symbol].s_inr if symbol in tri_books else None
            ),
        }
        for symbol in config.SYMBOLS
        if balances.get(symbol, zero) > zero
    ]

    first_book = tri_books.get(config.SYMBOLS[0]) if config.SYMBOLS else None
    usdt_mark = _best_mark_price(first_book.usdt_inr if first_book else None)
    usdt_bal  = balances.get("USDT", zero)

    return {
        "INR":              balances.get("INR", zero),
        "USDT":             usdt_bal,
        "asset_count":      len(positions),
        "positions":        positions,
        "usdt_mark_price_inr": usdt_mark,
        "usdt_value_inr":   float(usdt_bal) * usdt_mark,
        "asset_value_inr":  sum(p["market_value_inr"] for p in positions),
        "total_value_inr":  (
            float(balances.get("INR", zero))
            + float(usdt_bal) * usdt_mark
            + sum(p["market_value_inr"] for p in positions)
        ),
    }


async def market_loop(app):
    # Initialize condition inside the running event loop.
    app_state.data_condition = asyncio.Condition()

    use_rest       = os.getenv("USE_REST_FALLBACK", "").lower() in {"1", "true", "yes"}
    execution_mode = os.getenv("EXECUTION_MODE", "shadow").strip().lower()

    client         = CoinSwitchClient(config.COINSWITCH_API_KEY, config.COINSWITCH_SECRET_KEY)
    ranker         = TriRanker()
    two_leg_ranker = TwoLegRanker() if config.TWO_LEG_ENABLED else None
    rebalancer     = TriRebalancer(client) if config.REBALANCER_ENABLED and execution_mode == "real" else None

    _settle_ref: list = []

    def on_settle(symbol: str) -> None:
        if _settle_ref:
            _settle_ref[0](symbol)

    if execution_mode == "real":
        from strategy.tri_executor import TriExecutor
        from strategy.two_leg_executor import TwoLegExecutor
        logger.warning("EXECUTION_MODE=real — LIVE ORDERS will be placed on CSK")
        executor         = TriExecutor(client=client, fee=config.TAKER_FEE, tds=config.TDS_RATE,
                                       on_settle=on_settle)
        two_leg_executor = TwoLegExecutor(client=client, fee=config.TAKER_FEE, tds=config.TDS_RATE,
                                          on_settle=on_settle) if config.TWO_LEG_ENABLED else None
    else:
        executor         = ShadowExecutor({}, fee=config.TAKER_FEE, tds=config.TDS_RATE,
                                          on_settle=on_settle)
        two_leg_executor = ShadowTwoLegExecutor(
            balances=executor.balances,
            fee=config.TAKER_FEE,
            tds=config.TDS_RATE,
            on_settle=on_settle,
        ) if config.TWO_LEG_ENABLED else None

    async def on_opportunity(symbol: str, net: Union[PathResult, TwoLegResult], gross, result: dict) -> None:
        app_state.record_execution(symbol, net, result)

    async def on_tick(
        tri_books: dict[str, TriBook],
        ranked: dict,
        exec_results: dict,
        ranked_2l: dict,
        cycle: int,
        latency_ms: float,
    ) -> None:
        symbol_data = {}
        for symbol, (net, gross) in ranked.items():
            balances = executor.balances
            two_leg = ranked_2l.get(symbol)
            symbol_data[symbol] = {
                "net":   _path_to_dict(net),
                "gross": _path_to_dict(gross),
                "two_leg": _two_leg_to_dict(two_leg) if two_leg else None,
                "shadow_balances": {
                    "symbol": balances.get(symbol, 0),
                    "INR":    balances.get("INR",   0),
                    "USDT":   balances.get("USDT",  0),
                },
            }
        shadow_inventory = _build_shadow_inventory(executor.balances, tri_books)
        await app_state.update(symbol_data, cycle, latency_ms, shadow_inventory)

    async with client:
        if use_rest:
            symbols = config.SYMBOLS
            logger.info("REST mode — using fallback symbol list (%d symbols)", len(symbols))
        else:
            symbols = await client.discover_symbols(
                whitelist=config.SYMBOLS_WHITELIST,
                blacklist=config.SYMBOLS_BLACKLIST,
            )
            if not symbols:
                logger.error("Symbol discovery returned 0 symbols — aborting")
                return

        if hasattr(executor, "_symbols"):
            executor._symbols = [s.upper() for s in symbols]

        binance_feed = None if use_rest else BinanceDepthFeed(symbols)
        csk_ws       = None if use_rest else CSKPublicWS()

        engine = TriEngine(
            client=client,
            ranker=ranker,
            executor=executor,
            symbols=symbols,
            binance_feed=binance_feed,
            csk_ws=csk_ws,
            two_leg_ranker=two_leg_ranker,
            two_leg_executor=two_leg_executor,
            rebalancer=rebalancer,
            on_opportunity=on_opportunity,
            on_tick=on_tick,
        )
        _settle_ref.append(engine._on_settle)
        await engine.run()

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
