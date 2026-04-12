import time
import json
import urllib.parse
from urllib.parse import urlparse, urlencode
from cryptography.hazmat.primitives.asymmetric import ed25519
import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

class CoinSwitchClient:
    def __init__(self, api_key: str, secret_key: str, session: aiohttp.ClientSession = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = "https://coinswitch.co"
        self.session = session
        self._owned_session = False

    async def __aenter__(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owned_session = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._owned_session and self.session:
            await self.session.close()
        
    def _generate_signature(self, method: str, endpoint: str, params: dict, epoch_time: str) -> str:
        unquote_endpoint = endpoint
        if method == "GET" and params:
            # build query string exactly as CoinSwitch expects
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
        
    async def get_depth(self, symbol: str, exchange: str = "coinswitchx") -> dict:
        endpoint = "/trade/api/v2/depth"
        
        # Route C2C to binance/kucoin if specified or if usdt is present
        if exchange == "coinswitchx" and "usdt" in symbol and "inr" not in symbol:
            exchange = "binance"
            
        params = {"symbol": symbol, "exchange": exchange}
        url = self.base_url + endpoint
        headers = self._get_headers("GET", endpoint, params)
        
        try:
            if self.session:
                async with self.session.get(url, headers=headers, params=params, timeout=2) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("data", data)
                    elif response.status == 429:
                        logger.warning(f"RATE LIMIT (429) for {symbol} on {exchange}")
                        return {"bids": [], "asks": []}
                    else:
                        text = await response.text()
                        logger.error(f"API Error {response.status} for {symbol}: {text[:100]}")
                        return {"bids": [], "asks": []}
            else:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, params=params, timeout=2) as response:
                        if response.status == 200:
                            data = await response.json()
                            return data.get("data", data)
                        else:
                            return {"bids": [], "asks": []}
        except Exception as e:
            logger.error(f"Connection Error for {symbol}: {str(e)}")
            return {"bids": [], "asks": []}

    async def fetch_triangular_books(self, symbols: list = None) -> dict:
        """
        Fetches live books for S/INR, USDT/INR, and S/USDT for all provided symbols.
        Deduplicates common pairs (like USDT/INR) to save API calls.
        """
        if symbols is None:
            import config
            symbols = config.SYMBOLS
            
        # Deduplicate required pairs
        # 1. S/INR for each S
        # 2. S/USDT for each S
        # 3. USDT/INR (Common)
        
        pairs = [("usdt/inr", "coinswitchx")] # Start with common pair
        for s in symbols:
            s_lower = s.lower()
            pairs.append((f"{s_lower}/inr", "coinswitchx"))
            pairs.append((f"{s_lower}/usdt", "binance")) # default to binance for C2C
            
        tasks = [self.get_depth(p[0], p[1]) for p in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Build raw depth map
        depth_map = {}
        for i, res in enumerate(results):
            pair_key = pairs[i][0].upper()
            if isinstance(res, Exception):
                depth_map[pair_key] = {"bids": [], "asks": []}
            else:
                depth_map[pair_key] = {"bids": res.get("bids", []), "asks": res.get("asks", [])}
                
        # Group by Symbol for the engine
        tri_books = {}
        common_usdt_inr = depth_map.get("USDT/INR", {"bids": [], "asks": []})
        
        for s in symbols:
            s_upper = s.upper()
            tri_books[s_upper] = {
                f"{s_upper}/INR": depth_map.get(f"{s_upper.lower()}/inr".upper(), {"bids": [], "asks": []}),
                f"{s_upper}/USDT": depth_map.get(f"{s_upper.lower()}/usdt".upper(), {"bids": [], "asks": []}),
                "USDT/INR": common_usdt_inr
            }
            
        return tri_books
