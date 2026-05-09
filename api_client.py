import time
import urllib.parse
from typing import Optional
from urllib.parse import urlparse, urlencode
from cryptography.hazmat.primitives.asymmetric import ed25519
import aiohttp
import asyncio
import logging

import config
from core.models import Depth, TriBook

logger = logging.getLogger(__name__)


def _flatten_records(payload) -> list[dict]:
    if isinstance(payload, list):
        flattened = []
        for item in payload:
            flattened.extend(_flatten_records(item))
        return flattened

    if isinstance(payload, dict):
        dict_values = list(payload.values())
        if any(isinstance(value, (dict, list)) for value in dict_values):
            flattened = []
            for key in ("data", "result", "tickers", "coins", "rows", "markets"):
                if key in payload:
                    flattened.extend(_flatten_records(payload[key]))
            if flattened:
                return flattened
        return [payload]

    return []


def _extract_symbol(entry: dict) -> Optional[str]:
    for key in ("symbol", "market", "pair", "s"):
        value = entry.get(key)
        if isinstance(value, str) and "/" in value:
            return value.upper()

    base = entry.get("base") or entry.get("baseAsset") or entry.get("base_currency")
    quote = entry.get("quote") or entry.get("quoteAsset") or entry.get("quote_currency")
    if isinstance(base, str) and isinstance(quote, str) and base and quote:
        return f"{base.upper()}/{quote.upper()}"

    return None


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


def _collect_last_price_map(payload) -> dict[str, float]:
    prices: dict[str, float] = {}
    records = _flatten_records(payload)
    for entry in records:
        if not isinstance(entry, dict):
            continue
        symbol = _extract_symbol(entry)
        if symbol:
            prices[symbol] = _extract_last_price(entry)

    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and "/" in key:
                if isinstance(value, dict):
                    prices[key.upper()] = _extract_last_price(value)
                else:
                    try:
                        prices[key.upper()] = float(value)
                    except (TypeError, ValueError):
                        continue

    return prices

class CoinSwitchClient:
    def __init__(self, api_key: str, secret_key: str, session: aiohttp.ClientSession = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = "https://coinswitch.co"
        self.session = session
        self._owned_session = False
        self._depth_semaphore = asyncio.Semaphore(max(1, config.DEPTH_REQUEST_CONCURRENCY))

    async def __aenter__(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owned_session = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._owned_session and self.session:
            await self.session.close()
        
    def _generate_signature(self, method: str, endpoint: str, params: dict, epoch_time: str) -> str:
        if not self.api_key:
            raise ValueError("Missing CoinSwitch API key. Set COINSWITCH_API_KEY in your .env file.")
        if not self.secret_key:
            raise ValueError("Missing CoinSwitch secret key. Set COINSWITCH_SECRET_KEY or COINSWITCH_API_SECRET in your .env file.")

        unquote_endpoint = endpoint
        if method == "GET" and params:
            # CoinSwitch signs the full GET path including its query string.
            query = urlparse(endpoint).query
            prefix = '?' if not query else '&'
            endpoint += prefix + urlencode(params)
        
        unquote_endpoint = urllib.parse.unquote_plus(endpoint)
        signature_msg = method + unquote_endpoint + epoch_time
        request_string = bytes(signature_msg, 'utf-8')
        
        # Sign with ed25519
        try:
            secret_key_bytes = bytes.fromhex(self.secret_key.strip())
        except ValueError:
            raise ValueError("COINSWITCH_SECRET_KEY is not a valid hexadecimal string. Please provide your actual hex secret key in .env without asterisks or spaces.")
            
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(secret_key_bytes)
        signature_bytes = private_key.sign(request_string)
        return signature_bytes.hex()

    def _get_headers(self, method: str, endpoint: str, params: dict) -> dict:
        epoch_time = str(int(time.time() * 1000))
        signature = self._generate_signature(method, endpoint, params, epoch_time)
        return {
            'Content-Type': 'application/json',
            'X-AUTH-SIGNATURE': signature,
            'X-AUTH-APIKEY': self.api_key,
            'X-AUTH-EPOCH': epoch_time
        }

    async def _get(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        timeout: float = 5,
        suppress_statuses: Optional[set[int]] = None,
    ) -> dict:
        params = params or {}
        suppress_statuses = suppress_statuses or set()
        url = self.base_url + endpoint
        headers = self._get_headers("GET", endpoint, params)

        try:
            if self.session:
                async with self.session.get(url, headers=headers, params=params, timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("data", data)
                    if response.status in suppress_statuses:
                        return {}

                    text = await response.text()
                    logger.error("API Error %s for %s: %s", response.status, endpoint, text[:200])
                    return {}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("data", data)
                    if response.status in suppress_statuses:
                        return {}
                    return {}
        except Exception as e:
            logger.error("Connection Error for %s: %s", endpoint, str(e))
            return {}

    async def get_depth(self, symbol: str, exchange: str = "coinswitchx", quiet_missing: bool = False) -> dict:
        endpoint = "/trade/api/v2/depth"
        
        # USDT cross pairs are sourced from the external C2C venue by default.
        if exchange == "coinswitchx" and "usdt" in symbol and "inr" not in symbol:
            exchange = "binance"
            
        params = {"symbol": symbol, "exchange": exchange}
        url = self.base_url + endpoint
        headers = self._get_headers("GET", endpoint, params)
        suppress_statuses = {422} if quiet_missing else set()

        for attempt in range(config.DEPTH_REQUEST_RETRIES + 1):
            try:
                async with self._depth_semaphore:
                    if self.session:
                        async with self.session.get(
                            url,
                            headers=headers,
                            params=params,
                            timeout=config.DEPTH_REQUEST_TIMEOUT_SECONDS,
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                return data.get("data", data)
                            if response.status == 429:
                                logger.warning("RATE LIMIT (429) for %s on %s", symbol, exchange)
                                return {"bids": [], "asks": []}
                            if response.status in suppress_statuses:
                                return {"bids": [], "asks": []}

                            text = await response.text()
                            logger.error("API Error %s for %s on %s: %s", response.status, symbol, exchange, text[:100])
                            return {"bids": [], "asks": []}

                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            url,
                            headers=headers,
                            params=params,
                            timeout=config.DEPTH_REQUEST_TIMEOUT_SECONDS,
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                return data.get("data", data)
                            if response.status in suppress_statuses:
                                return {"bids": [], "asks": []}
                            return {"bids": [], "asks": []}
            except asyncio.TimeoutError:
                if attempt < config.DEPTH_REQUEST_RETRIES:
                    logger.warning(
                        "Timeout fetching %s on %s, retrying (%s/%s)",
                        symbol,
                        exchange,
                        attempt + 1,
                        config.DEPTH_REQUEST_RETRIES,
                    )
                    continue
                logger.error("Timeout fetching %s on %s after %s attempts", symbol, exchange, attempt + 1)
                return {"bids": [], "asks": []}
            except aiohttp.ClientError as e:
                if attempt < config.DEPTH_REQUEST_RETRIES:
                    logger.warning(
                        "Client error fetching %s on %s: %r, retrying (%s/%s)",
                        symbol,
                        exchange,
                        e,
                        attempt + 1,
                        config.DEPTH_REQUEST_RETRIES,
                    )
                    continue
                logger.error("Client error fetching %s on %s: %r", symbol, exchange, e)
                return {"bids": [], "asks": []}
            except Exception as e:
                logger.error("Connection Error for %s on %s: %s: %r", symbol, exchange, type(e).__name__, e)
                return {"bids": [], "asks": []}

    async def get_all_pairs_ticker(self, exchange: str = "coinswitchx"):
        return await self._get("/trade/api/v2/24hr/all-pairs/ticker", {"exchange": exchange})

    async def get_active_coins(self, exchange: str = "coinswitchx"):
        return await self._get("/trade/api/v2/coins", {"exchange": exchange})

    async def discover_symbols(
        self,
        whitelist: Optional[list[str]] = None,
        blacklist: Optional[list[str]] = None,
    ) -> list[str]:
        """Discover all symbols eligible for triangular arbitrage.

        Eligible = has an active S/INR pair on coinswitchx AND an active
        S/USDT pair on binance (C2C). Returns sorted list of base symbols.

        whitelist: if non-empty, only these symbols are considered.
        blacklist: these symbols are always excluded.
        """
        whitelist = [s.upper() for s in (whitelist or [])]
        blacklist = {s.upper() for s in (blacklist or [])}

        spot_tickers, cross_tickers = await asyncio.gather(
            self.get_all_pairs_ticker("coinswitchx"),
            self.get_all_pairs_ticker("binance"),
        )
        spot_prices  = _collect_last_price_map(spot_tickers)
        cross_prices = _collect_last_price_map(cross_tickers)

        inr_bases = {
            sym.split("/")[0]
            for sym in spot_prices
            if sym.endswith("/INR") and "/" in sym
        }
        usdt_bases = {
            sym.split("/")[0]
            for sym in cross_prices
            if sym.endswith("/USDT") and "/" in sym
        }

        eligible = (inr_bases & usdt_bases) - {"USDT"}

        if whitelist:
            eligible &= set(whitelist)
        eligible -= blacklist

        result = sorted(eligible)
        logger.info(
            "discover_symbols: %d symbols (inr=%d usdt=%d whitelist=%s blacklist=%d)",
            len(result), len(inr_bases), len(usdt_bases),
            whitelist or "all", len(blacklist),
        )
        return result

    # ── order management ─────────────────────────────────────────────────────

    async def _post(self, endpoint: str, body: dict, timeout: float = 5.0) -> dict:
        """POST with JSON body using the same Ed25519 auth as GET."""
        import json as _json
        url = self.base_url + endpoint
        headers = self._get_headers("POST", endpoint, {})
        try:
            if self.session:
                async with self.session.post(
                    url, headers=headers, json=body, timeout=timeout
                ) as resp:
                    if resp.status in {200, 201}:
                        data = await resp.json()
                        return data.get("data", data)
                    text = await resp.text()
                    logger.error("POST %s → %s: %s", endpoint, resp.status, text[:200])
                    return {}
            async with aiohttp.ClientSession() as s:
                async with s.post(url, headers=headers, json=body, timeout=timeout) as resp:
                    if resp.status in {200, 201}:
                        data = await resp.json()
                        return data.get("data", data)
                    return {}
        except Exception as e:
            logger.error("POST %s error: %r", endpoint, e)
            return {}

    async def _delete(
        self, endpoint: str, params: Optional[dict] = None, timeout: float = 5.0
    ) -> dict:
        params = params or {}
        url = self.base_url + endpoint
        headers = self._get_headers("DELETE", endpoint, params)
        try:
            if self.session:
                async with self.session.delete(
                    url, headers=headers, params=params, timeout=timeout
                ) as resp:
                    if resp.status in {200, 204}:
                        if resp.status == 204:
                            return {"cancelled": True}
                        data = await resp.json()
                        return data.get("data", data)
                    text = await resp.text()
                    logger.error("DELETE %s → %s: %s", endpoint, resp.status, text[:200])
                    return {}
            async with aiohttp.ClientSession() as s:
                async with s.delete(url, headers=headers, params=params, timeout=timeout) as resp:
                    if resp.status in {200, 204}:
                        return {"cancelled": True}
                    return {}
        except Exception as e:
            logger.error("DELETE %s error: %r", endpoint, e)
            return {}

    async def place_spot_order(
        self, symbol: str, side: str, price: "Decimal", qty: "Decimal"
    ) -> Optional[str]:
        """Place a limit order on the CSK INR spot book.

        symbol: base asset e.g. "BTC" → order placed on "btcinr"
        side:   "BUY" or "SELL"
        Returns CSK order_id string, or None on failure.
        """
        body = {
            "symbol":      f"{symbol.lower()}inr",
            "side":        side.upper(),
            "type":        "LIMIT",
            "limitPrice":  str(price),
            "quantity":    str(qty),
            "exchange":    "coinswitchx",
        }
        resp = await self._post("/trade/api/v2/order", body)
        oid = resp.get("order_id") or resp.get("orderId") or resp.get("id")
        if not oid:
            logger.error("place_spot_order: no order_id in response: %s", resp)
        return str(oid) if oid else None

    async def place_usdt_order(
        self, symbol: str, side: str, price: "Decimal", qty: "Decimal"
    ) -> Optional[str]:
        """Place a C2C USDT limit order (binance venue).

        symbol: base asset e.g. "BTC" → order placed on "btcusdt"
        Returns CSK order_id string, or None on failure.
        """
        body = {
            "symbol":      f"{symbol.lower()}usdt",
            "side":        side.upper(),
            "type":        "LIMIT",
            "limitPrice":  str(price),
            "quantity":    str(qty),
            "exchange":    "binance",
        }
        resp = await self._post("/trade/api/v2/order", body)
        oid = resp.get("order_id") or resp.get("orderId") or resp.get("id")
        if not oid:
            logger.error("place_usdt_order: no order_id in response: %s", resp)
        return str(oid) if oid else None

    async def place_usdtinr_order(
        self, side: str, price: "Decimal", qty: "Decimal"
    ) -> Optional[str]:
        """Place a USDT/INR limit order on the CSK spot book.

        qty: USDT amount (base).
        Returns CSK order_id string, or None on failure.
        """
        body = {
            "symbol":      "usdtinr",
            "side":        side.upper(),
            "type":        "LIMIT",
            "limitPrice":  str(price),
            "quantity":    str(qty),
            "exchange":    "coinswitchx",
        }
        resp = await self._post("/trade/api/v2/order", body)
        oid = resp.get("order_id") or resp.get("orderId") or resp.get("id")
        if not oid:
            logger.error("place_usdtinr_order: no order_id in response: %s", resp)
        return str(oid) if oid else None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if the cancel was accepted."""
        resp = await self._delete("/trade/api/v2/order", {"order_id": order_id})
        return bool(resp)

    async def get_order_status(self, order_id: str) -> dict:
        """Fetch current status of an order. Returns raw response dict."""
        return await self._get("/trade/api/v2/order", {"order_id": order_id})

    async def list_open_orders(self) -> list[dict]:
        """Return all currently open orders across all symbols."""
        raw = await self._get("/trade/api/v2/orders", {"status": "open"})
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("orders", raw.get("data", []))
        return []

    async def get_recent_orders(self, lookback_minutes: int = 120) -> list[dict]:
        """Return filled orders from the last `lookback_minutes` minutes.

        Used at boot for mid-triangle recovery. Returns empty list on any error
        so recovery degrades gracefully if the endpoint is unavailable.
        """
        import time as _time
        since_ms = int((_time.time() - lookback_minutes * 60) * 1000)
        raw = await self._get(
            "/trade/api/v2/orders",
            {"status": "FULFILLED", "from": since_ms},
            timeout=8.0,
        )
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("orders", raw.get("data", []))
        return []

    async def get_balances(self) -> "dict[str, Decimal]":
        """Fetch wallet balances from CSK. Returns {asset: Decimal(available)}."""
        from decimal import Decimal as _D
        raw = await self._get("/trade/api/v2/user/portfolio")
        balances: dict[str, _D] = {}
        if not raw:
            return balances
        # CSK returns a list of {"currencyCode": "BTC", "availableBalance": "0.5", ...}
        assets = raw if isinstance(raw, list) else raw.get("data", [])
        for entry in (assets if isinstance(assets, list) else []):
            code = entry.get("currencyCode", "").upper()
            available = entry.get("availableBalance", "0") or "0"
            if code:
                try:
                    balances[code] = _D(str(available))
                except Exception:
                    pass
        return balances

    async def fetch_triangular_books(
        self, symbols: Optional[list] = None, prefilter: bool = True
    ) -> dict[str, TriBook]:
        """Fetch live 3-book snapshots for each symbol.

        Returns dict[symbol_upper → TriBook] with Depth objects — no raw strings
        in the financial layer. The prefilter ranking uses float (it's a ranking
        operation, not financial math), but all depth levels are parsed to Decimal
        inside Depth.from_raw.
        """
        if symbols is None:
            symbols = config.SYMBOLS

        selected_symbols = [symbol.upper() for symbol in symbols]
        if prefilter and len(selected_symbols) > config.FULL_DEPTH_SYMBOL_LIMIT:
            spot_tickers, cross_tickers = await asyncio.gather(
                self.get_all_pairs_ticker("coinswitchx"),
                self.get_all_pairs_ticker("binance"),
            )
            spot_prices  = _collect_last_price_map(spot_tickers)
            cross_prices = _collect_last_price_map(cross_tickers)
            usdt_inr = spot_prices.get("USDT/INR", 0.0)  # float ok: ranking only

            ranked = []
            for symbol in selected_symbols:
                direct_inr = spot_prices.get(f"{symbol}/INR", 0.0)
                cross_usdt = cross_prices.get(f"{symbol}/USDT", 0.0)
                if direct_inr <= 0 or cross_usdt <= 0 or usdt_inr <= 0:
                    continue
                implied_inr = cross_usdt * usdt_inr
                if implied_inr <= 0:
                    continue
                edge = abs((direct_inr / implied_inr) - 1.0)
                ranked.append((symbol, edge))

            ranked.sort(key=lambda item: item[1], reverse=True)
            filtered = [s for s, edge in ranked if edge >= config.PREFILTER_MIN_EDGE_PCT]
            shortlist = filtered or [s for s, _ in ranked]
            selected_symbols = (
                shortlist[:config.FULL_DEPTH_SYMBOL_LIMIT]
                or selected_symbols[:config.FULL_DEPTH_SYMBOL_LIMIT]
            )

        pairs = [("usdt/inr", "coinswitchx")]
        for s in selected_symbols:
            pairs.append((f"{s.lower()}/inr",  "coinswitchx"))
            pairs.append((f"{s.lower()}/usdt", "binance"))

        raw_results = await asyncio.gather(
            *[self.get_depth(p[0], p[1]) for p in pairs],
            return_exceptions=True,
        )

        # Parse raw API responses into Depth objects at the boundary.
        depth_map: dict[str, Depth] = {}
        for i, res in enumerate(raw_results):
            key = pairs[i][0].upper()
            raw = {} if isinstance(res, Exception) else res
            depth_map[key] = Depth.from_raw(raw)

        common_usdt_inr = depth_map.get("USDT/INR", Depth.empty())
        ts = time.time()

        tri_books: dict[str, TriBook] = {}
        for s in [sym.upper() for sym in symbols]:
            tri_books[s] = TriBook(
                symbol=s,
                s_inr=depth_map.get(f"{s}/INR",    Depth.empty()),
                s_usdt=depth_map.get(f"{s}/USDT",  Depth.empty()),
                usdt_inr=common_usdt_inr,
                ts=ts,
            )

        return tri_books
