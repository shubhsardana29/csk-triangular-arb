import logging
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def calculate_vwap(levels: list, target_qty: float) -> float:
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
        total_cost += qty * price
        remaining -= qty

    if remaining > 0:
        return 0.0

    return total_cost / target_qty

class ArbitrageEngine:
    def __init__(self, taker_fee: float = None, tds_rate: float = None):
        self.fee = taker_fee if taker_fee is not None else config.TAKER_FEE
        self.tds = tds_rate if tds_rate is not None else config.TDS_RATE

    def calculate_multi_symbol_arbitrage(self, tri_books: dict, balances: dict) -> dict:
        """
        Calculates arbitrage for all symbols in tri_books.
        Returns a dict mapping Symbol -> {"net": opp, "gross": opp}
        """
        results = {}
        for symbol, books in tri_books.items():
            results[symbol] = self._calculate_symbol_arbitrage(symbol, books, balances)
        return results

    def _calculate_symbol_arbitrage(self, symbol: str, books: dict, balances: dict) -> dict:
        """
        Inner logic for a single symbol triangle.
        """
        symbol = symbol.upper()
        # Token-start paths (1/2) use token exposure; INR-start paths (3/4) use INR exposure.
        target_s = config.MAX_EXPOSURES.get(symbol, config.DEFAULT_SYMBOL_EXPOSURE)
        target_inr = config.MAX_EXPOSURES.get("INR", config.DEFAULT_INR_EXPOSURE)

        def _get_path_yield(fee, tds):
            # Paths use symbol-specific keys from 'books'
            s_inr = books.get(f"{symbol}/INR", {})
            s_usdt = books.get(f"{symbol}/USDT", {})
            usdt_inr = books.get("USDT/INR", {})

            # --- Path 1: SELL S/INR (TDS) -> BUY USDT/INR (No TDS) -> BUY S/USDT (Selling USDT -> TDS) ---
            vwap1 = calculate_vwap(s_inr.get("bids", []), target_s)
            if vwap1 == 0: p1 = 0
            else:
                inr_after_leg1 = (target_s * vwap1) * (1-fee) * (1-tds)
                vwap2 = calculate_vwap(usdt_inr.get("asks", []), inr_after_leg1)
                if vwap2 == 0: p1 = 0
                else:
                    usdt_after_leg2 = (inr_after_leg1 / vwap2) * (1-fee)
                    leg3_book = s_usdt.get("asks", [])
                    ask3 = float(leg3_book[0][0]) if leg3_book else 0
                    if ask3 == 0: p1 = 0
                    else:
                        s_qty_est = usdt_after_leg2 / ask3
                        vwap3 = calculate_vwap(leg3_book, s_qty_est)
                        if vwap3 == 0: p1 = 0
                        else:
                            s_final = (usdt_after_leg2 * (1-fee) * (1-tds)) / vwap3
                            p1 = s_final / target_s

            # --- Path 2: SELL S/USDT (TDS) -> SELL USDT/INR (TDS) -> BUY S/INR (No TDS) ---
            vwap1 = calculate_vwap(s_usdt.get("bids", []), target_s)
            if vwap1 == 0: p2 = 0
            else:
                usdt_after_leg1 = (target_s * vwap1) * (1-fee) * (1-tds)
                vwap2 = calculate_vwap(usdt_inr.get("bids", []), usdt_after_leg1)
                if vwap2 == 0: p2 = 0
                else:
                    inr_after_leg2 = (usdt_after_leg1 * vwap2) * (1-fee) * (1-tds)
                    leg3_book = s_inr.get("asks", [])
                    ask3 = float(leg3_book[0][0]) if leg3_book else 0
                    if ask3 == 0: p2 = 0
                    else:
                        s_qty_est = inr_after_leg2 / ask3
                        vwap3 = calculate_vwap(leg3_book, s_qty_est)
                        if vwap3 == 0: p2 = 0
                        else:
                            s_final = (inr_after_leg2 * (1-fee)) / vwap3
                            p2 = s_final / target_s

            # --- Path 3: BUY S/INR (No TDS) -> SELL S/USDT (TDS) -> SELL USDT/INR (TDS) ---
            leg1_book = s_inr.get("asks", [])
            ask1 = float(leg1_book[0][0]) if leg1_book else 0
            if ask1 == 0: p3 = 0
            else:
                s_qty_est = target_inr / ask1
                vwap1 = calculate_vwap(leg1_book, s_qty_est)
                if vwap1 == 0: p3 = 0
                else:
                    s_after_leg1 = (target_inr * (1-fee)) / vwap1
                    vwap2 = calculate_vwap(s_usdt.get("bids", []), s_after_leg1)
                    if vwap2 == 0: p3 = 0
                    else:
                        usdt_after_leg2 = (s_after_leg1 * vwap2) * (1-fee) * (1-tds)
                        vwap3 = calculate_vwap(usdt_inr.get("bids", []), usdt_after_leg2)
                        if vwap3 == 0: p3 = 0
                        else:
                            inr_final = (usdt_after_leg2 * vwap3) * (1-fee) * (1-tds)
                            p3 = inr_final / target_inr

            # --- Path 4: BUY USDT/INR (No TDS) -> BUY S/USDT (Selling USDT -> TDS) -> SELL S/INR (TDS) ---
            vwap1 = calculate_vwap(usdt_inr.get("asks", []), target_inr)
            if vwap1 == 0: p4 = 0
            else:
                usdt_after_leg1 = (target_inr / vwap1) * (1-fee)
                leg2_book = s_usdt.get("asks", [])
                ask2 = float(leg2_book[0][0]) if leg2_book else 0
                if ask2 == 0: p4 = 0
                else:
                    s_qty_est = usdt_after_leg1 / ask2
                    vwap2 = calculate_vwap(leg2_book, s_qty_est)
                    if vwap2 == 0: p4 = 0
                    else:
                        s_after_leg2 = (usdt_after_leg1 * (1-fee) * (1-tds)) / vwap2
                        vwap3 = calculate_vwap(s_inr.get("bids", []), s_after_leg2)
                        if vwap3 == 0: p4 = 0
                        else:
                            inr_final = (s_after_leg2 * vwap3) * (1-fee) * (1-tds)
                            p4 = inr_final / target_inr
            
            return [p1, p2, p3, p4]

        # Compare the same path set twice: once with real fee/TDS drag, once as raw gross spread.
        net_paths = [p - config.ARBITRAGE_BASE_RETURN for p in _get_path_yield(self.fee, self.tds)]
        gross_paths = [p - config.ARBITRAGE_BASE_RETURN for p in _get_path_yield(0.0, 0.0)]

        directions = [
            f"SELL {symbol}/INR -> BUY USDT/INR -> BUY {symbol}/USDT",
            f"SELL {symbol}/USDT -> SELL USDT/INR -> BUY {symbol}/INR",
            f"BUY {symbol}/INR -> SELL {symbol}/USDT -> SELL USDT/INR",
            f"BUY USDT/INR -> BUY {symbol}/USDT -> SELL {symbol}/INR"
        ]

        def find_best(paths):
            best_idx = 0
            max_val = config.ARBITRAGE_BEST_SENTINEL
            for i, p in enumerate(paths):
                if i == 0 or p > max_val:
                    max_val = p
                    best_idx = i
            
            # Paths 1/2 consume the token balance first; paths 3/4 consume INR first.
            base = symbol if best_idx < 2 else "INR"
            target_exposure = config.MAX_EXPOSURES.get(base, config.DEFAULT_INR_EXPOSURE)
            
            opp = self._build_opportunity(symbol, directions[best_idx], max_val, books, balances, base, target_exposure)
            
            if max_val <= config.ARBITRAGE_MIN_PROFIT_THRESHOLD:
                opp["opportunity"] = False
                opp["reason"] = "Spread < Costs" if max_val < 0 else "Spread < threshold"
            
            return opp

        return {
            "net": find_best(net_paths),
            "gross": find_best(gross_paths)
        }

    def _build_opportunity(self, symbol, direction, profit_pct, books, balances, base_currency, limit_amt):
        """
        Calculates max executable quantity based on limits and balances
        """
        # An opportunity is only as executable as both the configured risk cap and current balance allow.
        executable_amt = min(limit_amt, balances.get(base_currency, 0))
        
        # Normalize expected profit into INR so symbols can be compared on one scale in the UI.
        if base_currency == symbol:
            s_inr_book = books.get(f"{symbol}/INR", {}).get("bids", [])
            s_price = float(s_inr_book[0][0]) if s_inr_book else 0
            profit_inr = profit_pct * executable_amt * s_price
        else:
            profit_inr = profit_pct * executable_amt
        
        return {
            "symbol": symbol,
            "opportunity": True,
            "direction": direction,
            "executable_qty": executable_amt,
            "base_currency": base_currency,
            "expected_profit_inr": profit_inr,
            "profit_pct": profit_pct,
            "depth": books  # Include depth for UI rendering
        }

class ShadowExecutor:
    def __init__(self, balances: dict, fee: float, tds: float):
        self.balances = balances.copy()
        self.fee = fee
        self.tds = tds
        
    def execute(self, opportunity: dict, price_data: dict) -> dict:
        symbol = opportunity["symbol"]
        direction = opportunity["direction"]
        qty = opportunity["executable_qty"]
        base_currency = opportunity["base_currency"]
        
        # Initial stats
        pre_balance_s = self.balances.get(symbol, 0)
        pre_balance_inr = self.balances.get("INR", 0)
        pre_balance_usdt = self.balances.get("USDT", 0)
        
        # Sell legs realize proceeds and take both fee and TDS in the current tax model.
        def sell_vda(amount, levels):
            vwap = calculate_vwap(levels, amount)
            if vwap == 0:
                return 0.0
            return amount * vwap * (1 - self.fee) * (1 - self.tds)
            
        # Buy legs first reduce spend by charges, then infer a realistic fill size from the book.
        def buy_vda(amount_base, levels, tds_on_base=False):
            if tds_on_base:
                net_spend = amount_base * (1 - self.fee) * (1 - self.tds)
            else:
                net_spend = amount_base * (1 - self.fee)

            top_price = float(levels[0][0]) if levels else 0.0
            if top_price == 0:
                return 0.0

            qty_est = net_spend / top_price
            vwap = calculate_vwap(levels, qty_est)
            if vwap == 0:
                return 0.0
            return net_spend / vwap

        # price_data contains the three books for this symbol's triangle.
        s_inr = price_data.get(f"{symbol}/INR", {})
        s_usdt = price_data.get(f"{symbol}/USDT", {})
        usdt_inr = price_data.get("USDT/INR", {})

        if f"SELL {symbol}/INR" in direction:
            # Path 1: S -> INR (TDS) -> USDT (Buy) -> S (Sell USDT -> TDS)
            inr_gained = sell_vda(qty, s_inr.get("bids", []))
            usdt_gained = buy_vda(inr_gained, usdt_inr.get("asks", []))
            s_final = buy_vda(usdt_gained, s_usdt.get("asks", []), tds_on_base=True)
            if not all([inr_gained, usdt_gained, s_final]):
                return {
                    "result_balances": self.balances.copy(),
                    "symbol_variance": 0.0,
                    "inr_variance": 0.0,
                    "usdt_variance": 0.0
                }
            self.balances[symbol] -= qty
            self.balances[symbol] = self.balances.get(symbol, 0) + s_final
            
        elif f"SELL {symbol}/USDT" in direction and f"BUY {symbol}/INR" in direction:
            # Path 2: S -> USDT (TDS) -> INR (TDS) -> S (Buy)
            usdt_gained = sell_vda(qty, s_usdt.get("bids", []))
            inr_gained = sell_vda(usdt_gained, usdt_inr.get("bids", []))
            s_final = buy_vda(inr_gained, s_inr.get("asks", []))
            if not all([usdt_gained, inr_gained, s_final]):
                return {
                    "result_balances": self.balances.copy(),
                    "symbol_variance": 0.0,
                    "inr_variance": 0.0,
                    "usdt_variance": 0.0
                }
            self.balances[symbol] -= qty
            self.balances[symbol] = self.balances.get(symbol, 0) + s_final
 
        elif f"BUY {symbol}/INR" in direction and f"SELL {symbol}/USDT" in direction:
            # Path 3: INR -> S (Buy) -> USDT (Sell S -> TDS) -> INR (Sell USDT -> TDS)
            s_gained = buy_vda(qty, s_inr.get("asks", []))
            usdt_gained = sell_vda(s_gained, s_usdt.get("bids", []))
            inr_final = sell_vda(usdt_gained, usdt_inr.get("bids", []))
            if not all([s_gained, usdt_gained, inr_final]):
                return {
                    "result_balances": self.balances.copy(),
                    "symbol_variance": 0.0,
                    "inr_variance": 0.0,
                    "usdt_variance": 0.0
                }
            self.balances["INR"] -= qty
            self.balances["INR"] += inr_final
 
        elif f"BUY USDT/INR" in direction and f"BUY {symbol}/USDT" in direction:
            # Path 4: INR -> USDT (Buy) -> S (Buy USDT -> TDS) -> INR (Sell S -> TDS)
            usdt_gained = buy_vda(qty, usdt_inr.get("asks", []))
            s_gained = buy_vda(usdt_gained, s_usdt.get("asks", []), tds_on_base=True)
            inr_final = sell_vda(s_gained, s_inr.get("bids", []))
            if not all([usdt_gained, s_gained, inr_final]):
                return {
                    "result_balances": self.balances.copy(),
                    "symbol_variance": 0.0,
                    "inr_variance": 0.0,
                    "usdt_variance": 0.0
                }
            self.balances["INR"] -= qty
            self.balances["INR"] += inr_final
            
        return {
            "result_balances": self.balances.copy(),
            "symbol_variance": self.balances.get(symbol, 0) - pre_balance_s,
            "inr_variance": self.balances.get("INR", 0) - pre_balance_inr,
            "usdt_variance": self.balances.get("USDT", 0) - pre_balance_usdt
        }
