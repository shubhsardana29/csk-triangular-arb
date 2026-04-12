import asyncio
from typing import Dict, List, Tuple
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class ArbitrageEngine:
    def __init__(self, taker_fee: float = 0.005, tds_rate: float = 0.01):
        self.fee = taker_fee
        self.tds = tds_rate
        
    def _parse_depth(self, bids: list, asks: list):
        # expects [[price, qty], ...]
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else float('inf')
        return best_bid, best_ask

    def calculate_triangular_arbitrage(self, books: dict, balances: dict) -> dict:
        """
        Calculates both Gross (raw spread) and Net (post fees/TDS) opportunities.
        """
        # Parse tops of books
        btc_inr_bid, btc_inr_ask = self._parse_depth(books.get("BTC/INR", {}).get("bids", []), books.get("BTC/INR", {}).get("asks", []))
        btc_usdt_bid, btc_usdt_ask = self._parse_depth(books.get("BTC/USDT", {}).get("bids", []), books.get("BTC/USDT", {}).get("asks", []))
        usdt_inr_bid, usdt_inr_ask = self._parse_depth(books.get("USDT/INR", {}).get("bids", []), books.get("USDT/INR", {}).get("asks", []))

        if not all((btc_inr_bid, btc_usdt_bid, usdt_inr_bid, btc_inr_ask, btc_usdt_ask, usdt_inr_ask)):
            res = {"opportunity": False, "reason": "Missing depth data", "profit_pct": 0.0}
            return {"net": res, "gross": res}

        def _get_paths(fee, tds):
            # Path 1: SELL BTC/INR (TDS) -> BUY USDT/INR (No TDS) -> BUY BTC/USDT (Selling USDT -> TDS)
            p1 = (btc_inr_bid * (1-fee-tds) / usdt_inr_ask * (1-fee) / btc_usdt_ask * (1-fee-tds))
            # Path 2: SELL BTC/USDT (TDS) -> SELL USDT/INR (TDS) -> BUY BTC/INR (No TDS)
            p2 = (btc_usdt_bid * (1-fee-tds) * usdt_inr_bid * (1-fee-tds) / btc_inr_ask * (1-fee))
            # Path 3: BUY BTC/INR (No TDS) -> SELL BTC/USDT (TDS) -> SELL USDT/INR (TDS)
            p3 = (1.0 / btc_inr_ask * (1-fee) * btc_usdt_bid * (1-fee-tds) * usdt_inr_bid * (1-fee-tds))
            # Path 4: BUY USDT/INR (No TDS) -> BUY BTC/USDT (Selling USDT -> TDS) -> SELL BTC/INR (TDS)
            p4 = (1.0 / usdt_inr_ask * (1-fee) / btc_usdt_ask * (1-fee-tds) * btc_inr_bid * (1-fee-tds))
            return [p1, p2, p3, p4]

        # Calculate NET (with fees/TDS)
        net_paths = [p - 1.0 for p in _get_paths(self.fee, self.tds)]
        # Calculate GROSS (zero fees/TDS)
        gross_paths = [p - 1.0 for p in _get_paths(0.0, 0.0)]

        directions = [
            "SELL BTC/INR -> BUY USDT/INR -> BUY BTC/USDT",
            "SELL BTC/USDT -> SELL USDT/INR -> BUY BTC/INR",
            "BUY BTC/INR -> SELL BTC/USDT -> SELL USDT/INR",
            "BUY USDT/INR -> BUY BTC/USDT -> SELL BTC/INR"
        ]

        def find_best(paths):
            best_idx = 0
            max_val = -1.0 # Start low to find best
            for i, p in enumerate(paths):
                if i == 0 or p > max_val:
                    max_val = p
                    best_idx = i
            
            base = "BTC" if best_idx < 2 else "INR"
            amt = 1.0 if best_idx < 2 else 10000.0
            
            opp = self._build_opportunity(directions[best_idx], max_val, (1.0 + max_val) * amt, books, balances, base, amt)
            
            # Only mark as "Opportunity" if it's actually profitable (>0.01%)
            if max_val <= 0.0001:
                opp["opportunity"] = False
                opp["reason"] = "Spread < Costs" if max_val < 0 else "Spread < threshold"
            
            return opp

        return {
            "net": find_best(net_paths),
            "gross": find_best(gross_paths)
        }

    def _build_opportunity(self, direction, profit_pct, final_amt, books, balances, base_currency, scale_amt):
        """
        Calculates max executable quantity based on depth and balances
        """
        if base_currency == "BTC":
            executable_amt = min(0.1, balances["BTC"]) # Limit exposure per trade
            profit_inr = profit_pct * executable_amt * float(books["BTC/INR"]["bids"][0][0])
        else:
            executable_amt = min(100000, balances["INR"]) # Limit to 1L per trade
            profit_inr = profit_pct * executable_amt
        
        return {
            "opportunity": True,
            "direction": direction,
            "executable_qty": executable_amt,
            "base_currency": base_currency,
            "expected_profit_inr": profit_inr,
            "profit_pct": profit_pct
        }

class ShadowExecutor:
    def __init__(self, balances: dict, fee: float, tds: float = 0.01):
        self.balances = balances.copy()
        self.fee = fee
        self.tds = tds
        
    def execute(self, opportunity: dict, price_data: dict) -> dict:
        direction = opportunity["direction"]
        qty = opportunity["executable_qty"]
        base_currency = opportunity["base_currency"]
        
        # Initial Total Value in INR
        tot_val_start = self._total_inr_value(price_data)
        btc_start = self.balances.get("BTC", 0)
        inr_start = self.balances.get("INR", 0)
        
        if "SELL BTC/INR" in direction:
            # BTC -> INR (TDS applies) -> USDT (Buy) -> BTC (Buy USDT -> TDS applies)
            price_btc_inr = float(price_data["BTC/INR"]["bids"][0][0])
            price_usdt_inr = float(price_data["USDT/INR"]["asks"][0][0])
            price_btc_usdt = float(price_data["BTC/USDT"]["asks"][0][0])
            
            inr_gained = qty * price_btc_inr * (1 - self.fee - self.tds)
            usdt_gained = (inr_gained / price_usdt_inr) * (1 - self.fee)
            btc_final = (usdt_gained / price_btc_usdt) * (1 - self.fee - self.tds)
            
            self.balances["BTC"] -= qty
            self.balances["BTC"] += btc_final
            
        elif "SELL BTC/USDT" in direction and "BUY BTC/INR" in direction:
            # BTC -> USDT (TDS) -> INR (TDS) -> BTC (Buy)
            price_btc_usdt = float(price_data["BTC/USDT"]["bids"][0][0])
            price_usdt_inr = float(price_data["USDT/INR"]["bids"][0][0])
            price_btc_inr = float(price_data["BTC/INR"]["asks"][0][0])
            
            usdt_gained = qty * price_btc_usdt * (1 - self.fee - self.tds)
            inr_gained = usdt_gained * price_usdt_inr * (1 - self.fee - self.tds)
            btc_final = (inr_gained / price_btc_inr) * (1 - self.fee)
            
            self.balances["BTC"] -= qty
            self.balances["BTC"] += btc_final
 
        elif "BUY BTC/INR" in direction:
            # INR -> BTC (Buy) -> USDT (Sell BTC -> TDS) -> INR (Sell USDT -> TDS)
            price_btc_inr = float(price_data["BTC/INR"]["asks"][0][0])
            price_btc_usdt = float(price_data["BTC/USDT"]["bids"][0][0])
            price_usdt_inr = float(price_data["USDT/INR"]["bids"][0][0])
            
            btc_gained = (qty / price_btc_inr) * (1 - self.fee)
            usdt_gained = (btc_gained * price_btc_usdt) * (1 - self.fee - self.tds)
            inr_final = (usdt_gained * price_usdt_inr) * (1 - self.fee - self.tds)
            
            self.balances["INR"] -= qty
            self.balances["INR"] += inr_final
 
        elif "BUY USDT/INR" in direction:
            # INR -> USDT (Buy) -> BTC (Buy USDT -> TDS) -> INR (Sell BTC -> TDS)
            price_usdt_inr = float(price_data["USDT/INR"]["asks"][0][0])
            price_btc_usdt = float(price_data["BTC/USDT"]["asks"][0][0])
            price_btc_inr = float(price_data["BTC/INR"]["bids"][0][0])
            
            usdt_gained = (qty / price_usdt_inr) * (1 - self.fee)
            btc_gained = (usdt_gained / price_btc_usdt) * (1 - self.fee - self.tds)
            inr_final = (btc_gained * price_btc_inr) * (1 - self.fee - self.tds)
            
            self.balances["INR"] -= qty
            self.balances["INR"] += inr_final
            
        tot_val_end = self._total_inr_value(price_data)
        
        return {
            "result_balances": self.balances.copy(),
            "btc_variance": self.balances["BTC"] - btc_start,
            "total_value_increase_inr": tot_val_end - tot_val_start
        }
        
    def _total_inr_value(self, price_data):
        inr = self.balances["INR"]
        btc_in_inr = self.balances["BTC"] * float(price_data["BTC/INR"]["bids"][0][0])
        usdt_in_inr = self.balances["USDT"] * float(price_data["USDT/INR"]["bids"][0][0])
        return inr + btc_in_inr + usdt_in_inr
