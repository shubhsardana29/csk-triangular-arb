import os
from dotenv import load_dotenv

load_dotenv()

# Global Trading Configuration
# Taker Fee: 0.005 for 0.5%, 0.001 for 0.1% (VIP)
TAKER_FEE = float(os.getenv("TAKER_FEE", 0.005))

# TDS Rate: 0.01 for 1%
TDS_RATE = float(os.getenv("TDS_RATE", 0.01))

print(f"--- Config Loaded ---")
print(f"Taker Fee: {TAKER_FEE*100:.2f}%")
print(f"TDS Rate:  {TDS_RATE*100:.2f}%")
