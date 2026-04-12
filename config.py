import os
from dotenv import load_dotenv

load_dotenv()

# Global Trading Configuration
# Taker Fee: 0.005 for 0.5%, 0.001 for 0.1% (VIP)
TAKER_FEE = float(os.getenv("TAKER_FEE", 0.005))

# TDS Rate: 0.01 for 1%
TDS_RATE = float(os.getenv("TDS_RATE", 0.01))

# Performance Tuning
POLLING_INTERVAL = 0.5  # Seconds between cycles (Increase if hitting 429 Rate Limits)

# Trade Limits (Per-trade exposure)
MAX_BTC_EXPOSURE = 0.1
MAX_INR_EXPOSURE = 100000

# Multi-Symbol Support
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA"]

# Per-asset Exposure Limits (Max trade size per cycle)
MAX_EXPOSURES = {
    "BTC": 0.1,
    "ETH": 2.0,
    "SOL": 50.0,
    "XRP": 5000.0,
    "ADA": 5000.0,
    "INR": 100000.0,
    "USDT": 1500.0
}

print(f"--- Config Loaded ---")
print(f"Taker Fee: {TAKER_FEE*100:.2f}%")
print(f"TDS Rate:  {TDS_RATE*100:.2f}%")
print(f"Symbols:   {', '.join(SYMBOLS)}")
