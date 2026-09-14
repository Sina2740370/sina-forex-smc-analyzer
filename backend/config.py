"""
تنظیمات پروژه تحلیل تکنیکال فارکس
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
#  نمادهای فارکس (۱۰ جفت‌ارز + طلا)
# ============================================================
SYMBOLS = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "NZDUSD=X": "NZD/USD",
    "USDCHF=X": "USD/CHF",
    "EURGBP=X": "EUR/GBP",
    "EURJPY=X": "EUR/JPY",
    "GBPJPY=X": "GBP/JPY",
    "GC=F":    "XAU/USD",
}

# ============================================================
#  تایم‌فریم‌ها (HTF, LTF)
# ============================================================
SYMBOL_TIMEFRAMES = {
    "EURUSD=X": ("1h", "15m"),
    "GBPUSD=X": ("1h", "15m"),
    "USDJPY=X": ("1h", "15m"),
    "AUDUSD=X": ("1h", "15m"),
    "USDCAD=X": ("1h", "15m"),
    "NZDUSD=X": ("1h", "15m"),
    "USDCHF=X": ("1h", "15m"),
    "EURGBP=X": ("1h", "15m"),
    "EURJPY=X": ("1h", "15m"),
    "GBPJPY=X": ("1h", "15m"),
    "GC=F":    ("1h", "15m"),
}

# حداقل کندل مورد نیاز
MIN_CANDLES = 60
CANDLE_LIMIT = 200

# ============================================================
#  تنظیمات تست
# ============================================================
TEST_DURATION_DAYS = int(os.getenv("TEST_DURATION_DAYS", 20))
MIN_WIN_RATE = float(os.getenv("MIN_WIN_RATE", 0.65))
MIN_TRADES = int(os.getenv("MIN_TRADES", 20))
MIN_PROFIT_FACTOR = float(os.getenv("MIN_PROFIT_FACTOR", 1.5))
MAX_DRAWDOWN = float(os.getenv("MAX_DRAWDOWN", 0.15))

# ============================================================
#  آستانه سیگنال
# ============================================================
MIN_SIGNAL_SCORE = 0.65
RISK_REWARD = 3.0
SL_ATR_MULTIPLIER = 0.8

# ============================================================
#  اسپرد فرضی (پیپ)
# ============================================================
PIP_SIZE = {
    "EURUSD=X": 0.0001,
    "GBPUSD=X": 0.0001,
    "USDJPY=X": 0.01,
    "AUDUSD=X": 0.0001,
    "USDCAD=X": 0.0001,
    "NZDUSD=X": 0.0001,
    "USDCHF=X": 0.0001,
    "EURGBP=X": 0.0001,
    "EURJPY=X": 0.01,
    "GBPJPY=X": 0.01,
    "GC=F":    0.1,
}

SPREAD = {
    "EURUSD=X": 0.8,
    "GBPUSD=X": 1.2,
    "USDJPY=X": 1.0,
    "AUDUSD=X": 1.2,
    "USDCAD=X": 1.5,
    "NZDUSD=X": 1.8,
    "USDCHF=X": 1.5,
    "EURGBP=X": 1.5,
    "EURJPY=X": 1.8,
    "GBPJPY=X": 2.5,
    "GC=F":     25.0,
}

# ============================================================
#  دیتابیس
# ============================================================
DB_PATH = os.getenv("DB_PATH", "signals.db")

# ============================================================
#  زمان‌بند
# ============================================================
SCAN_INTERVAL_MINUTES = 15
