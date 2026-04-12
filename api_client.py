import time
import json
import urllib.parse
from urllib.parse import urlparse, urlencode
from cryptography.hazmat.primitives.asymmetric import ed25519
import aiohttp
import asyncio

class CoinSwitchClient:
    def __init__(self, api_key: str, secret_key: str, session: aiohttp.ClientSession = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = "https://coinswitch.co"
        self.session = session
        
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
        
        if self.session:
            async with self.session.get(url, headers=headers, params=params, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", data)
                else:
                    text = await response.text()
                    raise Exception(f"Failed to fetch depth for {symbol}: {response.status} - {text}")
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("data", data)
                    else:
                        text = await response.text()
                        raise Exception(f"Failed to fetch depth for {symbol}: {response.status} - {text}")

    async def fetch_triangular_books(self) -> dict:
        """
        Fetches live books for BTC/INR, USDT/INR from CoinSwitchX, 
        and aggregates BTC/USDT from both Binance and Kucoin to find the best price.
        """
        tasks = [
            self.get_depth("btc/inr", "coinswitchx"),
            self.get_depth("usdt/inr", "coinswitchx"),
            self.get_depth("btc/usdt", "binance"),
            self.get_depth("btc/usdt", "kucoin")
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        books = {}
        
        def safe_get(res, key):
            if isinstance(res, Exception): return []
            return res.get(key, [])
            
        books["BTC/INR"] = {"bids": safe_get(results[0], "bids"), "asks": safe_get(results[0], "asks")}
        books["USDT/INR"] = {"bids": safe_get(results[1], "bids"), "asks": safe_get(results[1], "asks")}
        
        # Aggregate Binance and Kucoin C2C
        binance_bids = safe_get(results[2], "bids")
        binance_asks = safe_get(results[2], "asks")
        kucoin_bids = safe_get(results[3], "bids")
        kucoin_asks = safe_get(results[3], "asks")
        
        # Combine and sort to natively put the best prices at index 0
        merged_bids = sorted(binance_bids + kucoin_bids, key=lambda x: float(x[0]), reverse=True)
        merged_asks = sorted(binance_asks + kucoin_asks, key=lambda x: float(x[0]), reverse=False)
        
        books["BTC/USDT"] = {"bids": merged_bids, "asks": merged_asks}
        
        return books
