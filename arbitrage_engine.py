import asyncio
from typing import Dict, List, Tuple
import logging
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class ArbitrageEngine:
    def __init__(self, taker_fee: float = None, tds_rate: float = None):
        self.fee = taker_fee if taker_fee is not None else config.TAKER_FEE
        self.tds = tds_rate if tds_rate is not None else config.TDS_RATE
        
    def _calculate_vwap(self, levels: list, target_qty: float) -> float:
        """
        Calculates the Volume Weighted Average Price for a target quantity.
        Returns 0.0 if liquidity is insufficient.
        """
        if not levels or target_qty <= 0:
            return 0.0
            
        remaining = target_qty
        total_cost = 0.0
        
        for price_str, qty_str in levels:
            price, qty = float(price_str), float(qty_str)
            if remaining <= qty:
                total_cost += remaining * price
                remaining = 0
                break
            else:
                total_cost += qty * price
                remaining -= qty
        
        if remaining > 0:
            return 0.0 # Insufficient liquidity
            
        return total_cost / target_qty

    def calculate_triangular_arbitrage(self, books: dict, balances: dict) -> dict:
        """
        Calculates both Gross (raw spread) and Net (post fees/TDS) opportunities.
        Uses VWAP based on configured exposure limits.
        """
        def _get_path_yield(fee, tds, target_btc=config.MAX_BTC_EXPOSURE, target_inr=config.MAX_INR_EXPOSURE):
            # Helper to apply sequential fees and TDS
            # Calculation: Result = (Amount * (1-fee)) * (1-tds)
            
            # --- Path 1: SELL BTC/INR (TDS) -> BUY USDT/INR (No TDS) -> BUY BTC/USDT (Selling USDT -> TDS) ---
            # Leg 1: BTC -> INR
            vwap1 = self._calculate_vwap(books.get("BTC/INR", {}).get("bids", []), target_btc)
            if vwap1 == 0: p1 = 0
            else:
                inr_after_leg1 = (target_btc * vwap1) * (1-fee) * (1-tds)
                # Leg 2: INR -> USDT (Buying USDT with INR does not trigger TDS for buyer)
                vwap2 = self._calculate_vwap(books.get("USDT/INR", {}).get("asks", []), inr_after_leg1)
                if vwap2 == 0: p1 = 0
                else:
                    usdt_after_leg2 = (inr_after_leg1 / vwap2) * (1-fee)
                    # Leg 3: USDT -> BTC
                    leg3_book = books.get("BTC/USDT", {}).get("asks", [])
                    ask3 = float(leg3_book[0][0]) if leg3_book else 0
                    if ask3 == 0: p1 = 0
                    else:
                        # Estimate BTC quantity for VWAP
                        btc_qty_est = usdt_after_leg2 / ask3
                        vwap3 = self._calculate_vwap(leg3_book, btc_qty_est)
                        if vwap3 == 0: p1 = 0
                        else:
                            btc_final = (usdt_after_leg2 * (1-fee) * (1-tds)) / vwap3
                            p1 = btc_final / target_btc

            # --- Path 2: SELL BTC/USDT (TDS) -> SELL USDT/INR (TDS) -> BUY BTC/INR (No TDS) ---
            vwap1 = self._calculate_vwap(books.get("BTC/USDT", {}).get("bids", []), target_btc)
            if vwap1 == 0: p2 = 0
            else:
                usdt_after_leg1 = (target_btc * vwap1) * (1-fee) * (1-tds)
                vwap2 = self._calculate_vwap(books.get("USDT/INR", {}).get("bids", []), usdt_after_leg1)
                if vwap2 == 0: p2 = 0
                else:
                    inr_after_leg2 = (usdt_after_leg1 * vwap2) * (1-fee) * (1-tds)
                    leg3_book = books.get("BTC/INR", {}).get("asks", [])
                    ask3 = float(leg3_book[0][0]) if leg3_book else 0
                    if ask3 == 0: p2 = 0
                    else:
                        btc_qty_est = inr_after_leg2 / ask3
                        vwap3 = self._calculate_vwap(leg3_book, btc_qty_est)
                        if vwap3 == 0: p2 = 0
                        else:
                            btc_final = (inr_after_leg2 * (1-fee)) / vwap3
                            p2 = btc_final / target_btc

            # --- Path 3: BUY BTC/INR (No TDS) -> SELL BTC/USDT (TDS) -> SELL USDT/INR (TDS) ---
            leg1_book = books.get("BTC/INR", {}).get("asks", [])
            ask1 = float(leg1_book[0][0]) if leg1_book else 0
            if ask1 == 0: p3 = 0
            else:
                # Estimate BTC quantity for VWAP spending target_inr
                btc_qty_est = target_inr / ask1
                vwap1 = self._calculate_vwap(leg1_book, btc_qty_est)
                if vwap1 == 0: p3 = 0
                else:
                    btc_after_leg1 = (target_inr * (1-fee)) / vwap1
                    vwap2 = self._calculate_vwap(books.get("BTC/USDT", {}).get("bids", []), btc_after_leg1)
                    if vwap2 == 0: p3 = 0
                    else:
                        usdt_after_leg2 = (btc_after_leg1 * vwap2) * (1-fee) * (1-tds)
                        vwap3 = self._calculate_vwap(books.get("USDT/INR", {}).get("bids", []), usdt_after_leg2)
                        if vwap3 == 0: p3 = 0
                        else:
                            inr_final = (usdt_after_leg2 * vwap3) * (1-fee) * (1-tds)
                            p3 = inr_final / target_inr

            # --- Path 4: BUY USDT/INR (No TDS) -> BUY BTC/USDT (Selling USDT -> TDS) -> SELL BTC/INR (TDS) ---
            vwap1 = self._calculate_vwap(books.get("USDT/INR", {}).get("asks", []), target_inr)
            if vwap1 == 0: p4 = 0
            else:
                usdt_after_leg1 = (target_inr / vwap1) * (1-fee)
                leg2_book = books.get("BTC/USDT", {}).get("asks", [])
                ask2 = float(leg2_book[0][0]) if leg2_book else 0
                if ask2 == 0: p4 = 0
                else:
                    btc_qty_est = usdt_after_leg1 / ask2
                    vwap2 = self._calculate_vwap(leg2_book, btc_qty_est)
                    if vwap2 == 0: p4 = 0
                    else:
                        btc_after_leg2 = (usdt_after_leg1 * (1-fee) * (1-tds)) / vwap2
                        vwap3 = self._calculate_vwap(books.get("BTC/INR", {}).get("bids", []), btc_after_leg2)
                    if vwap3 == 0: p4 = 0
                    else:
                        inr_final = (btc_after_leg2 * vwap3) * (1-fee) * (1-tds)
                        p4 = inr_final / target_inr
            
            return [p1, p2, p3, p4]

        # Calculate NET (with fees/TDS)
        net_paths = [p - 1.0 for p in _get_path_yield(self.fee, self.tds)]
        # Calculate GROSS (zero fees/TDS)
        gross_paths = [p - 1.0 for p in _get_path_yield(0.0, 0.0)]

        directions = [
            "SELL BTC/INR -> BUY USDT/INR -> BUY BTC/USDT",
            "SELL BTC/USDT -> SELL USDT/INR -> BUY BTC/INR",
            "BUY BTC/INR -> SELL BTC/USDT -> SELL USDT/INR",
            "BUY USDT/INR -> BUY BTC/USDT -> SELL BTC/INR"
        ]

        def find_best(paths):
            best_idx = 0
            max_val = -1.0 
            for i, p in enumerate(paths):
                if i == 0 or p > max_val:
                    max_val = p
                    best_idx = i
            
            base = "BTC" if best_idx < 2 else "INR"
            # Use real limits from config
            target_exposure = config.MAX_BTC_EXPOSURE if base == "BTC" else config.MAX_INR_EXPOSURE
            
            opp = self._build_opportunity(directions[best_idx], max_val, (1.0 + max_val) * target_exposure, books, balances, base)
            
            if max_val <= 0.0001:
                opp["opportunity"] = False
                opp["reason"] = "Spread < Costs" if max_val < 0 else "Spread < threshold"
            
            return opp

        return {
            "net": find_best(net_paths),
            "gross": find_best(gross_paths)
        }

    def _build_opportunity(self, direction, profit_pct, final_amt, books, balances, base_currency):
        """
        Calculates max executable quantity based on limits and balances
        """
        if base_currency == "BTC":
            limit = config.MAX_BTC_EXPOSURE
            executable_amt = min(limit, balances["BTC"])
            btc_price = float(books["BTC/INR"]["bids"][0][0]) if books["BTC/INR"]["bids"] else 0
            profit_inr = profit_pct * executable_amt * btc_price
        else:
            limit = config.MAX_INR_EXPOSURE
            executable_amt = min(limit, balances["INR"])
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
    def __init__(self, balances: dict, fee: float, tds: float):
        self.balances = balances.copy()
        self.fee = fee
        self.tds = tds
        
    def execute(self, opportunity: dict, price_data: dict) -> dict:
        direction = opportunity["direction"]
        qty = opportunity["executable_qty"]
        
        tot_val_start = self._total_inr_value(price_data)
        btc_start = self.balances.get("BTC", 0)
        inr_start = self.balances.get("INR", 0)
        
        # Helper for sequential fee+tds on SELL legs
        def sell_vda(amount, price):
            return amount * price * (1 - self.fee) * (1 - self.tds)
            
        # Helper for sequential fee on BUY legs (INR/USDT buy)
        def buy_vda(amount_base, price, tds_on_base=False):
            # amount_base is what we are spending (INR or USDT)
            if tds_on_base:
                net_spend = amount_base * (1 - self.fee) * (1 - self.tds)
            else:
                net_spend = amount_base * (1 - self.fee)
            return net_spend / price

        if "SELL BTC/INR" in direction:
            # Path 1: BTC -> INR (TDS) -> USDT (Buy) -> BTC (Sell USDT -> TDS)
            inr_gained = sell_vda(qty, float(price_data["BTC/INR"]["bids"][0][0]))
            usdt_gained = buy_vda(inr_gained, float(price_data["USDT/INR"]["asks"][0][0]), tds_on_base=False)
            btc_final = buy_vda(usdt_gained, float(price_data["BTC/USDT"]["asks"][0][0]), tds_on_base=True) # Selling USDT to buy BTC
            
            self.balances["BTC"] -= qty
            self.balances["BTC"] += btc_final
            
        elif "SELL BTC/USDT" in direction and "BUY BTC/INR" in direction:
            # Path 2: BTC -> USDT (TDS) -> INR (TDS) -> BTC (Buy)
            usdt_gained = sell_vda(qty, float(price_data["BTC/USDT"]["bids"][0][0]))
            inr_gained = sell_vda(usdt_gained, float(price_data["USDT/INR"]["bids"][0][0]))
            btc_final = buy_vda(inr_gained, float(price_data["BTC/INR"]["asks"][0][0]), tds_on_base=False)
            
            self.balances["BTC"] -= qty
            self.balances["BTC"] += btc_final
 
        elif "BUY BTC/INR" in direction and "SELL BTC/USDT" in direction:
            # Path 3: INR -> BTC (Buy) -> USDT (Sell BTC -> TDS) -> INR (Sell USDT -> TDS)
            btc_gained = buy_vda(qty, float(price_data["BTC/INR"]["asks"][0][0]), tds_on_base=False)
            usdt_gained = sell_vda(btc_gained, float(price_data["BTC/USDT"]["bids"][0][0]))
            inr_final = sell_vda(usdt_gained, float(price_data["USDT/INR"]["bids"][0][0]))
            
            self.balances["INR"] -= qty
            self.balances["INR"] += inr_final
 
        elif "BUY USDT/INR" in direction and "BUY BTC/USDT" in direction:
            # Path 4: INR -> USDT (Buy) -> BTC (Buy USDT -> TDS) -> INR (Sell BTC -> TDS)
            usdt_gained = buy_vda(qty, float(price_data["USDT/INR"]["asks"][0][0]), tds_on_base=False)
            btc_gained = buy_vda(usdt_gained, float(price_data["BTC/USDT"]["asks"][0][0]), tds_on_base=True) # Selling USDT
            inr_final = sell_vda(btc_gained, float(price_data["BTC/INR"]["bids"][0][0]))
            
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
        btc_in_inr = self.balances["BTC"] * float(price_data["BTC/INR"]["bids"][0][0]) if price_data["BTC/INR"]["bids"] else 0
        usdt_in_inr = self.balances["USDT"] * float(price_data["USDT/INR"]["bids"][0][0]) if price_data["USDT/INR"]["bids"] else 0
        return inr + btc_in_inr + usdt_in_inr
