"""
تنظیمات پروژه - Sina Quant
"""
import os
from dotenv import load_dotenv
from engine.core.config import EngineConfig

load_dotenv()


def _require_env(name: str) -> str:
    """کلیدهای حساس را فقط از Environment Variables می‌خواند؛
    اگر ست نشده باشد برنامه بالا نمی‌آید (به‌جای استفاده از مقدار پیش‌فرض هاردکد)."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"متغیر محیطی {name} تنظیم نشده. آن را در Render → Environment اضافه کن."
        )
    return value


# ============================================================
#  API Keys
# ============================================================
FRED_API_KEY = _require_env("FRED_API_KEY")

# ============================================================
#  Telegram Notifications
# ============================================================
TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

# ============================================================
#  نمادها
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
#  تایم‌فریم‌ها (Top-Down)
# ============================================================
TIMEFRAMES = ["1wk", "1d", "4h", "1h", "15m"]
SYMBOL_TIMEFRAMES = {sym: TIMEFRAMES for sym in SYMBOLS.keys()}

MIN_CANDLES = 60
CANDLE_LIMIT = 200

# ============================================================
#  تست
# ============================================================
TEST_DURATION_DAYS = int(os.getenv("TEST_DURATION_DAYS", 20))
MIN_WIN_RATE = float(os.getenv("MIN_WIN_RATE", 0.65))
MIN_TRADES = int(os.getenv("MIN_TRADES", 20))
MIN_PROFIT_FACTOR = float(os.getenv("MIN_PROFIT_FACTOR", 1.5))
MAX_DRAWDOWN = float(os.getenv("MAX_DRAWDOWN", 0.15))

# ============================================================
#  آستانه سیگنال (تعدیل‌شده)
# ============================================================
# مقادیر استراتژی از تنظیمات canonical موتور می‌آیند تا Backend تنظیمات موازی نداشته باشد.
_ENGINE_DEFAULTS = EngineConfig()
MIN_SIGNAL_SCORE = _ENGINE_DEFAULTS.min_score
RISK_REWARD = _ENGINE_DEFAULTS.risk_reward
SL_ATR_MULTIPLIER = _ENGINE_DEFAULTS.sl_atr1h_mult
SL_OB_BUFFER = _ENGINE_DEFAULTS.sl_ob_buffer

# ============================================================
#  کش هوشمند
# ============================================================
CACHE_TTL = {
    "1wk": 240,
    "1d":  60,
    "4h":  30,
    "1h":  15,
    "15m": 5,
}

# ============================================================
#  پیپ و اسپرد
# ============================================================
PIP_SIZE = {
    "EURUSD=X": 0.0001, "GBPUSD=X": 0.0001, "USDJPY=X": 0.01,
    "AUDUSD=X": 0.0001, "USDCAD=X": 0.0001, "NZDUSD=X": 0.0001,
    "USDCHF=X": 0.0001, "EURGBP=X": 0.0001, "EURJPY=X": 0.01,
    "GBPJPY=X": 0.01,    "GC=F":    0.1,
}

SPREAD = {
    "EURUSD=X": 0.8, "GBPUSD=X": 1.2, "USDJPY=X": 1.0,
    "AUDUSD=X": 1.2, "USDCAD=X": 1.5, "NZDUSD=X": 1.8,
    "USDCHF=X": 1.5, "EURGBP=X": 1.5, "EURJPY=X": 1.8,
    "GBPJPY=X": 2.5,
    # فقط برای برابری مدل با Engine: 3 pip × $0.10 = $0.30؛
    # این فرض آزمایشی است، نه اسپرد زنده‌ی GC=F یا XAUUSD.
    "GC=F":     3.0,
}

DB_PATH = os.getenv("DB_PATH", "signals.db")
SCAN_INTERVAL_MINUTES = 15

# ============================================================
#  وزن لایه‌ها
# ============================================================
WEIGHT_TECHNICAL = float(os.getenv("WEIGHT_TECHNICAL", 0.60))
WEIGHT_FUNDAMENTAL = float(os.getenv("WEIGHT_FUNDAMENTAL", 0.15))
WEIGHT_NEWS = float(os.getenv("WEIGHT_NEWS", 0.15))
WEIGHT_MEMORY = float(os.getenv("WEIGHT_MEMORY", 0.10))

# ============================================================
#  ریسک و حافظه (اضافه‌شده)
# ============================================================
# RISK_PER_TRADE حذف شد: قبلاً جدا از LOT_RISK_PCT برای محاسبه‌ی drawdown استفاده می‌شد
# و چون مقدارشون فرق داشت (۱٪ در برابر ۰.۵٪ واقعی)، آمار drawdown نادرست بود.
# الان database.py مستقیم از LOT_RISK_PCT (پایین همین فایل) استفاده می‌کنه.
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "0") == "1"    # لایه حافظه؛ تو دوره تست خاموش

# ============================================================
#  حساب و بروکر (برای پیام تلگرام)
# ============================================================
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", 1000))    # موجودی حساب (دلار)
LOT_RISK_PCT = float(os.getenv("LOT_RISK_PCT", 0.5))           # درصد ریسک هر معامله
BROKER_SUFFIX = os.getenv("BROKER_SUFFIX", "")                 # پسوند نماد تو MT5 (مثلاً ".m")
GOLD_SYMBOL = os.getenv("GOLD_SYMBOL", "XAUUSD")               # اسم طلا تو MT5 (مثلاً "GOLD")
ALLOW_GOLD_FUTURES_PROXY_SIGNALS = os.getenv("ALLOW_GOLD_FUTURES_PROXY_SIGNALS", "0") == "1"  # opt-in صریح؛ GC=F اسپات نیست
MAX_ENTRY_SLIP_R = float(os.getenv("MAX_ENTRY_SLIP_R", 0.3))   # حداکثر فاصله ورود، نسبت به فاصله SL
