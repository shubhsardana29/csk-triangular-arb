import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from api_client import CoinSwitchClient


def _flatten_records(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        flattened: list[dict] = []
        for item in payload:
            flattened.extend(_flatten_records(item))
        return flattened

    if isinstance(payload, dict):
        dict_values = list(payload.values())
        if any(isinstance(value, (dict, list)) for value in dict_values):
            flattened: list[dict] = []
            for key in ("data", "result", "tickers", "coins", "rows", "markets"):
                if key in payload:
                    flattened.extend(_flatten_records(payload[key]))
            if flattened:
                return flattened
        return [payload]

    return []


def _collect_symbols(payload: Any) -> set[str]:
    symbols: set[str] = set()

    if isinstance(payload, list):
        for item in payload:
            symbols.update(_collect_symbols(item))
        return symbols

    if isinstance(payload, dict):
        # CoinSwitch often returns a map like {"BTC/INR": {...}, "ETH/INR": {...}}
        for key, value in payload.items():
            if isinstance(key, str) and "/" in key:
                symbols.add(key.upper())
            symbols.update(_collect_symbols(value))
        return symbols

    if isinstance(payload, str) and "/" in payload:
        symbols.add(payload.upper())

    return symbols


def _collect_pair_metrics(payload: Any) -> dict[str, dict[str, float]]:
    metrics: dict[str, dict[str, float]] = {}

    if isinstance(payload, list):
        for item in payload:
            metrics.update(_collect_pair_metrics(item))
        return metrics

    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and "/" in key and isinstance(value, dict):
                metrics[key.upper()] = {
                    "quote_volume": _extract_quote_volume(value),
                    "last_price": _extract_last_price(value),
                }
            if isinstance(value, (dict, list)):
                metrics.update(_collect_pair_metrics(value))
        return metrics

    return metrics


def _extract_symbol(entry: dict) -> str | None:
    for key in ("symbol", "market", "pair", "s"):
        value = entry.get(key)
        if isinstance(value, str) and "/" in value:
            return value.upper()

    base = entry.get("base") or entry.get("baseAsset") or entry.get("base_currency")
    quote = entry.get("quote") or entry.get("quoteAsset") or entry.get("quote_currency")
    if isinstance(base, str) and isinstance(quote, str) and base and quote:
        return f"{base.upper()}/{quote.upper()}"

    return None


def _extract_coin(entry: dict) -> str | None:
    for key in ("symbol", "coin", "currency", "base", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.upper()
    return None


def _extract_quote_volume(entry: dict) -> float:
    value = (
        entry.get("quoteVolume")
        or entry.get("quote_volume")
        or entry.get("volume_quote")
        or 0
    )
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _extract_last_price(entry: dict) -> float:
    value = (
        entry.get("lastPrice")
        or entry.get("last_price")
        or entry.get("close")
        or 0
    )
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _book_has_depth(client: CoinSwitchClient, symbol: str, exchange: str) -> bool:
    book = await client.get_depth(symbol.lower(), exchange, quiet_missing=True)
    return bool(book.get("bids") or book.get("asks"))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch CoinSwitch market pairs and print symbols that fit this triangular-arbitrage project."
    )
    parser.add_argument(
        "--spot-exchange",
        default="coinswitchx",
        help="Exchange used for INR spot pairs and the all-pairs ticker. Default: coinswitchx",
    )
    parser.add_argument(
        "--cross-exchange",
        default="binance",
        help="Exchange used to verify SYMBOL/USDT depth. Default: binance",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a human summary.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print a small sample of the raw ticker and coin payloads for troubleshooting.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Show only the top N triangular-ready symbols ranked by INR quote volume. Default: all",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=0.0,
        help="Keep only symbols with at least this INR quoteVolume from the all-pairs ticker.",
    )
    args = parser.parse_args()

    async with CoinSwitchClient(config.COINSWITCH_API_KEY, config.COINSWITCH_SECRET_KEY) as client:
        tickers, active_coins = await asyncio.gather(
            client.get_all_pairs_ticker(args.spot_exchange),
            client.get_active_coins(args.spot_exchange),
        )

        ticker_records = _flatten_records(tickers)
        coin_records = _flatten_records(active_coins)

        raw_pair_set = _collect_symbols(tickers)
        raw_pair_set.update(
            symbol for entry in ticker_records for symbol in [_extract_symbol(entry)] if symbol
        )
        raw_pairs = sorted(raw_pair_set)

        pair_metrics = _collect_pair_metrics(tickers)
        for entry in ticker_records:
            symbol = _extract_symbol(entry)
            if not symbol:
                continue
            pair_metrics[symbol] = {
                "quote_volume": _extract_quote_volume(entry),
                "last_price": _extract_last_price(entry),
            }

        active_pair_set = _collect_symbols(active_coins)
        active_pair_set.update(
            symbol for entry in coin_records for symbol in [_extract_symbol(entry)] if symbol
        )
        active_coin_set = {
            pair.split("/")[0]
            for pair in active_pair_set
            if "/" in pair
        }
        active_coin_set.update(
            coin for entry in coin_records for coin in [_extract_coin(entry)] if coin
        )

        inr_symbols = []
        for pair in raw_pairs:
            if not pair.endswith("/INR"):
                continue
            base_symbol = pair.split("/")[0]
            if base_symbol in {"INR", "USDT"}:
                continue
            if pair_metrics.get(pair, {}).get("quote_volume", 0.0) < args.min_volume:
                continue
            inr_symbols.append(base_symbol)
        inr_symbols = sorted(inr_symbols)

        usdt_inr_available = await _book_has_depth(client, "usdt/inr", args.spot_exchange)

        verify_tasks = [
            _book_has_depth(client, f"{symbol}/usdt", args.cross_exchange)
            for symbol in inr_symbols
        ]
        usdt_results = await asyncio.gather(*verify_tasks)

        triangular_ready = []
        for symbol, has_usdt in zip(inr_symbols, usdt_results):
            if not has_usdt:
                continue
            if active_coin_set and symbol not in active_coin_set:
                continue
            pair = f"{symbol}/INR"
            triangular_ready.append({
                "symbol": symbol,
                "quote_volume_inr": pair_metrics.get(pair, {}).get("quote_volume", 0.0),
                "last_price_inr": pair_metrics.get(pair, {}).get("last_price", 0.0),
            })

        triangular_ready.sort(key=lambda item: item["quote_volume_inr"], reverse=True)
        if args.top > 0:
            triangular_ready = triangular_ready[:args.top]

        payload = {
            "spot_exchange": args.spot_exchange,
            "cross_exchange": args.cross_exchange,
            "usdt_inr_available": usdt_inr_available,
            "ticker_record_count": len(ticker_records),
            "coin_record_count": len(coin_records),
            "raw_pairs": raw_pairs,
            "inr_symbols": inr_symbols,
            "triangular_ready_symbols": [item["symbol"] for item in triangular_ready],
            "triangular_ready_details": triangular_ready,
            "active_coins": sorted(active_coin_set),
        }

        if args.debug:
            payload["ticker_sample"] = ticker_records[:3]
            payload["coin_sample"] = coin_records[:3]

        if args.json:
            print(json.dumps(payload, indent=2))
            return

        print(f"Spot exchange: {args.spot_exchange}")
        print(f"Cross exchange: {args.cross_exchange}")
        print(f"USDT/INR depth available: {'yes' if usdt_inr_available else 'no'}")
        print(f"Ticker records parsed: {len(ticker_records)}")
        print(f"Coin records parsed: {len(coin_records)}")
        print()
        print(f"All discovered spot pairs: {len(raw_pairs)}")
        for pair in raw_pairs:
            print(f"  {pair}")

        print()
        print(f"INR-quoted base symbols: {len(inr_symbols)}")
        print(", ".join(inr_symbols) if inr_symbols else "None")

        print()
        print("Triangular-ready symbols for this project")
        print("These have SYMBOL/INR on the spot exchange and SYMBOL/USDT depth on the cross exchange.")
        if triangular_ready:
            for item in triangular_ready:
                print(
                    f"  {item['symbol']}: INR volume {item['quote_volume_inr']:,.2f}, "
                    f"last price {item['last_price_inr']:,.6f}"
                )
        else:
            print("None")
        print()
        if args.debug:
            print("Ticker sample:")
            print(json.dumps(ticker_records[:3], indent=2))
            print()
            print("Coin sample:")
            print(json.dumps(coin_records[:3], indent=2))
            print()
        print("Copy the symbols you want into config.SYMBOLS.")


if __name__ == "__main__":
    asyncio.run(main())
