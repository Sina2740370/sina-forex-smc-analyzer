"""
تنظیمات پروژه - Sina Quant
"""
import os
from dotenv import load_dotenv

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

# نمادهای غیرفعال پیش‌فرض + ورودی محیط (کاما جدا).
# GC=F (طلا) به‌صورت پیش‌فرض غیرفعال است: سوییپ ۴ ساله (Dukascopy M15)
# نشان داد استراتژی روی XAUUSD به‌طور پایدار ضررده است (E[r]=−0.13، PF=0.79)
# و به هیچ پارامتری حساس نیست — تا بازطراحی سیگنال طلا، معامله نشود.
# برای فعال‌سازی مجدد: DISABLED_SYMBOLS را خالی نکن؛ از SYMBOLS حذف/اضافه کن در دیپلوی.
DEFAULT_DISABLED_SYMBOLS = {"GC=F"}
_DISABLED_RAW = os.getenv("DISABLED_SYMBOLS", "").strip()
DISABLED_SYMBOLS = DEFAULT_DISABLED_SYMBOLS | {
    s.strip() for s in _DISABLED_RAW.split(",") if s.strip()
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
#  آستانه سیگنال (تعدیل‌شده با سوییپ ۴ ساله — ۲۰۲۶-۱۰)
#  سوییپ OAT روی ۴ سال M15 واقعی نشان داد معامله‌های کم‌امتیاز ضررده‌اند:
#  min_score=0.72 + sl_ob_buffer=0.24 → E[r]=+0.25، PF=1.45، DD=7.1R (EURUSD)
#  در مقابل 0.40/0.30 قدیمی → لبه عملاً صفر.
#  توجه: وزن‌دهی نهایی لایو (final_min) پروفایل را با WEIGHT_TECHNICAL می‌میزاند.
# ============================================================
MIN_SIGNAL_SCORE = 0.72
RISK_REWARD = 2.0
SL_ATR_MULTIPLIER = 1.5
SL_OB_BUFFER = 0.24

# پروفایل هر نماد: min_score سخت‌گیرانه‌تر و SL پهن‌تر برای جفت‌های پرریسک
# کلید = نماد yfinance (همان کلید SYMBOLS)
SYMBOL_PROFILE = {
    "EURUSD=X": {"min_score": 0.72, "sl_atr_mult": 1.5},   # مقدار تأییدشده با سوییپ ۴ ساله
    "GBPUSD=X": {"min_score": 0.72, "sl_atr_mult": 1.8},
    "USDJPY=X": {"min_score": 0.72, "sl_atr_mult": 1.8},
    "EURJPY=X": {"min_score": 0.72, "sl_atr_mult": 2.0},
    "GBPJPY=X": {"min_score": 0.72, "sl_atr_mult": 2.0},
}


def get_symbol_profile(symbol: str) -> dict:
    """min_score و sl_atr_mult برای نماد؛ در صورت نبود پروفایل → مقادیر سراسری."""
    p = SYMBOL_PROFILE.get(symbol) or {}
    return {
        "min_score": float(p.get("min_score", MIN_SIGNAL_SCORE)),
        "sl_atr_mult": float(p.get("sl_atr_mult", SL_ATR_MULTIPLIER)),
    }


# ============================================================
#  دروازه سیگنال (آستانه‌ها؛ همان envها در engine/tools/signal_gate هم خوانده می‌شوند)
# ============================================================
FAILED_ZONE_H = float(os.getenv("FAILED_ZONE_H", "48"))
ZONE_TOL = float(os.getenv("ZONE_TOL", "1.0"))
STREAK_BLOCK_H = float(os.getenv("STREAK_BLOCK_H", "24"))
STREAK_LOSSES = int(os.getenv("STREAK_LOSSES", "2"))

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
    "GBPJPY=X": 2.5, "GC=F":     3.0,
}

DB_PATH = os.getenv("DB_PATH", "signals.db")
SCAN_INTERVAL_MINUTES = 15

# ============================================================
#  وزن لایه‌ها
#  لایه‌ی خبر به‌عنوان وزن امتیاز حذف شد (قبلاً همیشه صفر بود چون
#  scheduler هرگز news_sentiment نمی‌فرستاد)؛ خبر فقط به‌عنوان
#  «فیلتر سخت ایمنی» (news_safe) عمل می‌کند. اخبار مرتبط در reasons گزارش می‌شوند.
#  اگر در آینده منبع sentiment وصل شد، WEIGHT_NEWS را env کنید.
# ============================================================
WEIGHT_TECHNICAL = float(os.getenv("WEIGHT_TECHNICAL", 0.70))
WEIGHT_FUNDAMENTAL = float(os.getenv("WEIGHT_FUNDAMENTAL", 0.20))
WEIGHT_NEWS = float(os.getenv("WEIGHT_NEWS", 0.00))
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
MAX_ENTRY_SLIP_R = float(os.getenv("MAX_ENTRY_SLIP_R", 0.3))   # حداکثر فاصله ورود، نسبت به فاصله SL
