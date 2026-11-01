"""
موتور تحلیل Top-Down SMC/ICT - نسخه 4.3
بر اساس جمع‌بندی استراتژی بهترین تریدرهای حرفه‌ای دنیا (ICT/SMC + اصول Paul Tudor Jones)

اصول اعمال‌شده:
1. Higher Timeframe Bias اولویت مطلق دارد
2. بهترین ستاپ = Liquidity Sweep → Structure Shift (CHOCH) → Entry در FVG/OB
3. Premium/Discount به صورت فیلتر نرم
4. کیفیت و confluence مهم‌تر از تعداد سیگنال
5. ریسک نامتقارن و محافظه‌کارانه حفظ شده
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

from .config import (
    MIN_CANDLES, MIN_SIGNAL_SCORE, RISK_REWARD,
    SL_ATR_MULTIPLIER, SL_OB_BUFFER,
    WEIGHT_TECHNICAL, WEIGHT_FUNDAMENTAL, WEIGHT_NEWS, WEIGHT_MEMORY,
    get_symbol_profile, SPREAD, PIP_SIZE,
)

logger = logging.getLogger(__name__)

# حداقل فاصلهٔ ریسک (SL تا قیمت) بر حسب ضریب ATR — قبلاً هیچ حداقلی چک نمی‌شد
# و در صورت کوچک بودن OB، SL می‌توانست غیرواقعی نزدیک به قیمت باشد.
# در صورت نیاز به تنظیم‌پذیری، می‌توان این را به config.py منتقل کرد.
MIN_RISK_ATR_RATIO = 0.25
MIN_RISK_SPREAD_MULT = 3.0   # حداقل فاصلهٔ SL = ۳ برابر اسپرد معمول نماد


def _min_spread_risk(symbol) -> float:
    """حداقل فاصلهٔ SL بر حسب اسپرد معمول نماد.
    استاپی که داخل نویز اسپرد باشد = باخت قطعی بدون تحرک بازار؛ مسدود می‌شود.
    (از audit v1 — اینجا فقط برای سازگاری تست/ابزارهای جانبی حفظ شده.)"""
    try:
        spread_pips = SPREAD.get(symbol)
        pip = PIP_SIZE.get(symbol)
        if not spread_pips or not pip:
            return 0.0
        return MIN_RISK_SPREAD_MULT * spread_pips * pip
    except Exception:
        return 0.0


# ============================================================
#  ابزارهای پایه
# ============================================================

def calculate_atr(df, period=14):
    if len(df) < period + 1:
        return 0.0
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    v = tr.rolling(period).mean().iloc[-1]
    return float(v) if not pd.isna(v) else 0.0


def calculate_ema(df, period=50):
    if len(df) < period:
        return 0.0
    ema = df["close"].ewm(span=period, adjust=False).mean()
    return float(ema.iloc[-1])


def ema_trend(df, period=50):
    if len(df) < period + 3:
        return "UNCLEAR"
    ema = df["close"].ewm(span=period, adjust=False).mean()
    if ema.iloc[-1] > ema.iloc[-3]:
        return "BULLISH"
    elif ema.iloc[-1] < ema.iloc[-3]:
        return "BEARISH"
    return "RANGING"


def find_swings(df, left=3, right=3):
    """نسخه بهینه: به‌جای برش pandas در هر پله حلقه (کند روی دیتافریم بزرگ)،
    ماکزیمم/مینیمم پنجره متحرک را یک‌بار به‌صورت برداری محاسبه می‌کند."""
    n = len(df)
    if n < left + right + 1:
        return {"highs": [], "lows": []}
    high, low = df["high"], df["low"]
    # max/min سمت چپ (شامل i) + سمت راست (شامل i) -> اجتماع = کل پنجره [i-left, i+right]
    back_max = high.rolling(left + 1).max()
    fwd_max = high[::-1].rolling(right + 1).max()[::-1]
    win_max = pd.concat([back_max, fwd_max], axis=1).max(axis=1)
    back_min = low.rolling(left + 1).min()
    fwd_min = low[::-1].rolling(right + 1).min()[::-1]
    win_min = pd.concat([back_min, fwd_min], axis=1).min(axis=1)

    is_high = high == win_max
    is_low = low == win_min
    window = left + right + 1

    def _dedupe(pairs):
        # پلتو غیرمجاور در یک پنجره (دو سوئینگ هم‌قیمت نزدیک هم) یک‌بار شمرده می‌شود
        # تا ساختار به‌اشتباه RANGING نشود (هم‌راستا با engine/core/smc.py).
        out = []
        for i, p in pairs:
            if out and p == out[-1][1] and i - out[-1][0] < window:
                continue
            out.append((i, p))
        return out

    highs = _dedupe([(i, float(high.iloc[i])) for i in range(left, n - right) if is_high.iloc[i]])
    lows = _dedupe([(i, float(low.iloc[i])) for i in range(left, n - right) if is_low.iloc[i]])
    return {"highs": highs, "lows": lows}


def detect_structure(df):
    """
    نکته: قبلاً حالت مختلط (مثلاً HH ولی LL، یا LH ولی HL) به‌اشتباه با
    شرط ضعیف `hh or hl` صعودی/نزولی قلمداد می‌شد. این حالت در واقع یعنی
    ساختار در حال تغییر/تناقض دارد (نه یک روند تمیز)، پس باید RANGING باشد.
    """
    sw = find_swings(df)
    highs, lows = sw["highs"], sw["lows"]
    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "UNCLEAR"}

    lh, ph = highs[-1][1], highs[-2][1]
    ll, pl = lows[-1][1], lows[-2][1]

    hh, hl = lh > ph, ll > pl
    lh_, ll_ = lh < ph, ll < pl

    if hh and hl:
        trend = "BULLISH"
    elif lh_ and ll_:
        trend = "BEARISH"
    else:
        trend = "RANGING"

    return {"trend": trend, "last_high": lh, "last_low": ll}


def find_order_blocks(df, lookback=80):
    if len(df) < lookback:
        lookback = len(df) - 1
    w = df.iloc[-lookback:]
    avg_body = (w["close"] - w["open"]).abs().mean()
    if avg_body == 0:
        return {"bullish": None, "bearish": None}

    bull = bear = None
    for i in range(len(w) - 3, -1, -1):
        cur, nxt = w.iloc[i], w.iloc[i + 1]
        body = abs(nxt["close"] - nxt["open"])
        later = w.iloc[i + 2:]

        if (cur["close"] < cur["open"]
                and nxt["close"] > cur["high"]
                and body > avg_body * 1.5
                and (later.empty or not (later["close"] < cur["low"]).any())
                and bull is None):
            bull = {"high": float(cur["high"]), "low": float(cur["low"]),
                    "mid": float((cur["high"] + cur["low"]) / 2)}

        if (cur["close"] > cur["open"]
                and nxt["close"] < cur["low"]
                and body > avg_body * 1.5
                and (later.empty or not (later["close"] > cur["high"]).any())
                and bear is None):
            bear = {"high": float(cur["high"]), "low": float(cur["low"]),
                    "mid": float((cur["high"] + cur["low"]) / 2)}

        if bull and bear:
            break
    return {"bullish": bull, "bearish": bear}


def detect_fvg(df, lookback=15, min_gap_ratio=0.15):
    """
    min_gap_ratio: حداقل اندازهٔ gap نسبت به میانگین رنج کندل‌های بازه.
    قبلاً هیچ حداقلی نبود، پس شکاف‌های خیلی ریز/نویزی هم به‌عنوان FVG معتبر برمی‌گشتند.
    """
    if len(df) < 5:
        return None
    price = float(df["close"].iloc[-1])
    start = max(2, len(df) - lookback)
    window = df.iloc[max(0, start - 2):]
    avg_range = float((window["high"] - window["low"]).mean())
    min_gap = avg_range * min_gap_ratio if avg_range > 0 else 0.0

    for i in range(len(df) - 1, start - 1, -1):
        c1, c3 = df.iloc[i - 2], df.iloc[i]
        if c3["low"] > c1["high"]:
            t, b = float(c3["low"]), float(c1["high"])
            if (t - b) >= min_gap and price > b:
                return {"type": "BULLISH", "top": t, "bottom": b}
        if c3["high"] < c1["low"]:
            t, b = float(c1["low"]), float(c3["high"])
            if (t - b) >= min_gap and price < t:
                return {"type": "BEARISH", "top": t, "bottom": b}
    return None


def get_premium_discount(df, lookback=60):
    if len(df) == 0:
        return {"zone": "-", "high": 0.0, "low": 0.0, "eq": 0.0}
    if len(df) < lookback:
        lookback = len(df)
    w = df.iloc[-lookback:]
    sh = float(w["high"].max())
    sl = float(w["low"].min())
    eq = (sh + sl) / 2
    p = float(df["close"].iloc[-1])
    return {"zone": "PREMIUM" if p > eq else "DISCOUNT",
            "high": sh, "low": sl, "eq": eq}


def get_candle_confirmation(df, direction):
    if len(df) < 3:
        return False
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    if rng == 0:
        return False
    uw = last["high"] - max(last["close"], last["open"])
    lw = min(last["close"], last["open"]) - last["low"]

    if direction == "BUY":
        return (lw > body * 1.5) or (
            last["close"] > last["open"]
            and last["close"] > prev["high"]
            and last["open"] < prev["close"])
    if direction == "SELL":
        return (uw > body * 1.5) or (
            last["close"] < last["open"]
            and last["close"] < prev["low"]
            and last["open"] > prev["close"])
    return False


def detect_liquidity_sweep(df, lookback=30):
    """اندازهٔ sweep (size) هم برگردانده می‌شود تا امتیازدهی بتواند بین یک
    sweep پنج‌پیپی و پنجاه‌پیپی تفاوت بگذارد (به‌جای امتیاز یکسان)."""
    if len(df) < lookback + 3:
        return None
    recent = df.iloc[-lookback:-3]
    prev_high = float(recent["high"].max())
    prev_low = float(recent["low"].min())

    for i in range(-3, 0):
        c = df.iloc[i]
        if c["high"] > prev_high and c["close"] < prev_high:
            return {"type": "BEARISH_SWEEP", "level": prev_high,
                    "size": float(c["high"] - prev_high)}
        if c["low"] < prev_low and c["close"] > prev_low:
            return {"type": "BULLISH_SWEEP", "level": prev_low,
                    "size": float(prev_low - c["low"])}
    return None


def sweep_strength_multiplier(sweep, atr):
    """
    ضریب کیفیت Sweep بر اساس sweep_size / ATR.
    قبلاً همهٔ Sweepها (صرف‌نظر از اندازه) امتیاز ثابت داشتند.
    خروجی بین ۰.۵ (Sweep ضعیف/کم‌عمق) تا ۱.۶ (Sweep قوی/عمیق) کلیپ می‌شود
    تا نه بی‌اثر شود نه بیش‌ازحد امتیاز را منفجر کند.
    """
    if not sweep or not atr:
        return 1.0
    ratio = sweep.get("size", 0.0) / atr
    return max(0.5, min(1.6, 0.5 + ratio * 0.7))


_LONDON = ZoneInfo("Europe/London")
_NEW_YORK = ZoneInfo("America/New_York")


def _local_hour(dt, tz):
    """ساعت محلی (با DST). dt بدون tzinfo به‌صورت UTC در نظر گرفته می‌شود."""
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).hour


def is_london_session(dt=None):
    """سشن لندن: ۰۷:۰۰ تا ۱۶:۰۰ به وقت محلی لندن (با DST)."""
    return 7 <= _local_hour(dt, _LONDON) < 16


def is_ny_session(dt=None):
    """سشن نیویورک: ۰۷:۰۰ تا ۱۶:۰۰ به وقت محلی نیویورک (با DST)."""
    return 7 <= _local_hour(dt, _NEW_YORK) < 16


def is_london_killzone(dt=None):
    """کیل‌زون لندن: ۰۷:۰۰ تا ۱۰:۰۰ به وقت محلی لندن (با DST)."""
    return 7 <= _local_hour(dt, _LONDON) < 10


def is_ny_killzone(dt=None):
    """کیل‌زون نیویورک: ۰۷:۰۰ تا ۱۰:۰۰ به وقت محلی نیویورک (با DST)."""
    return 7 <= _local_hour(dt, _NEW_YORK) < 10


def current_session_label(dt=None):
    dt = dt or datetime.now(timezone.utc)
    if is_london_killzone(dt) or is_ny_killzone(dt):
        return "KILLZONE"
    if is_london_session(dt) or is_ny_session(dt):
        return "ACTIVE_SESSION"
    return "OFF_HOURS"


def detect_choch(df, lookback=20):
    if len(df) < lookback + 5:
        return None
    w = df.iloc[-lookback:]
    sw = find_swings(w, 2, 2)
    highs, lows = sw["highs"], sw["lows"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    last_high = highs[-1][1]
    last_low = lows[-1][1]
    price = float(df["close"].iloc[-1])
    if price > last_high:
        return {"type": "BULLISH_CHOCH", "level": last_high}
    if price < last_low:
        return {"type": "BEARISH_CHOCH", "level": last_low}
    return None


def find_breaker_block(df, lookback=40):
    obs = find_order_blocks(df, lookback)
    price = float(df["close"].iloc[-1])
    if obs["bullish"] and price < obs["bullish"]["low"]:
        return {"type": "BEARISH_BREAKER",
                "high": obs["bullish"]["high"],
                "low": obs["bullish"]["low"]}
    if obs["bearish"] and price > obs["bearish"]["high"]:
        return {"type": "BULLISH_BREAKER",
                "high": obs["bearish"]["high"],
                "low": obs["bearish"]["low"]}
    return None


def detect_unicorn_setup(df_4h, df_1h, df_15m):
    """ستاپ کلاسیک ICT: Sweep + CHOCH + (Breaker یا FVG) — بدون هیچ‌کدام از دو، None.

    نکته: find_order_blocks فقط OB تخریب‌نشده برمی‌گرداند، پس breaker به‌ندرت پیدا می‌شود.
    """
    sweep = detect_liquidity_sweep(df_15m, lookback=30)
    if not sweep:
        return None
    choch = detect_choch(df_1h, lookback=20)
    if not choch:
        return None
    if sweep["type"] == "BULLISH_SWEEP" and choch["type"] != "BULLISH_CHOCH":
        return None
    if sweep["type"] == "BEARISH_SWEEP" and choch["type"] != "BEARISH_CHOCH":
        return None
    breaker = find_breaker_block(df_4h, lookback=40)
    fvg = detect_fvg(df_15m, lookback=15)
    if breaker is None and fvg is None:
        return None
    return {
        "sweep": sweep, "choch": choch,
        "breaker": breaker, "fvg": fvg,
        "direction": "BUY" if sweep["type"] == "BULLISH_SWEEP" else "SELL",
    }


def calculate_super_quant_score(tech_score: float, fund_score: float, news_score: float,
                                mem_score: float, regime_label: str = "TRENDING_LOW_VOL") -> dict:
    """
    موتور هم‌پوشانی لایه‌ای ابر پروژه (Super Quant Scorer):
    ترکیب ۵ لایه اصلی و تبدیل به امتیاز -100 تا +100 با رده‌بندی هوشمند A++ تا B.
    """
    raw_composite = (
        tech_score * 0.40 +
        fund_score * 0.20 +
        news_score * 0.15 +
        mem_score * 0.15 +
        (0.10 if "TRENDING" in regime_label else -0.05)
    )
    sq_score = round(max(-100.0, min(100.0, raw_composite * 100.0)), 1)
    abs_sq = abs(sq_score)

    if abs_sq >= 75.0:
        super_grade = "A++"
    elif abs_sq >= 55.0:
        super_grade = "A+"
    elif abs_sq >= 35.0:
        super_grade = "A"
    else:
        super_grade = "B"

    return {
        "super_quant_score": sq_score,
        "super_grade": super_grade,
        "components": {
            "tech": round(tech_score * 100, 1),
            "fund": round(fund_score * 100, 1),
            "news": round(news_score * 100, 1),
            "memory": round(mem_score * 100, 1),
            "regime": regime_label,
        }
    }


def classify_setup_grade(tech_score: float, final_score: float, confluence_reasons: list) -> str:
    """
    رده‌بندی ستاپ‌های صادره:
    - "A+": ستاپ‌های طلایی با بالاترین confluence (امتیاز مطلق >= 0.70 و حداقل 4 علت هم‌پوشانی)
    - "A": ستاپ‌های قوی استاندارد
    - "B": ستاپ‌های متوسط
    """
    abs_score = abs(final_score)
    num_reasons = len(confluence_reasons)
    if abs_score >= 0.70 and num_reasons >= 4:
        return "A+"
    if abs_score >= 0.50 and num_reasons >= 3:
        return "A"
    return "B"


def _distance_to_ob(price: float, ob: dict, atr: float) -> float:
    """فاصله نرمال‌شده تا Order Block (بر حسب ATR)"""
    if ob is None:
        return 999.0
    if ob["low"] <= price <= ob["high"]:
        return 0.0
    if price < ob["low"]:
        return (ob["low"] - price) / atr if atr > 0 else 999.0
    return (price - ob["high"]) / atr if atr > 0 else 999.0


# ============================================================
#  فیلتر Top-Down (نسخه حرفه‌ای)
# ============================================================

def top_down_filter(data_dict):
    """
    Returns: (direction, reason, wk_trend, d_trend, soft)
    soft=True یعنی اجازه ادامه با جریمه
    """
    try:
        df_wk = pd.DataFrame(data_dict["1wk"])
        df_d = pd.DataFrame(data_dict["1d"])
        # بدون کندل در حال تشکیل (هم‌راستا با بک‌تست)
        df_wk = _drop_forming(df_wk, 10080)
        df_d = _drop_forming(df_d, 1440)
    except Exception:
        logger.warning("top_down_filter: خطا در آماده‌سازی داده HTF", exc_info=True)
        return "NEUTRAL", "داده کافی برای فیلتر", "-", "-", False

    wk_struct = detect_structure(df_wk)
    d_struct = detect_structure(df_d)
    wk_trend = wk_struct["trend"]
    d_trend = d_struct["trend"]

    # فیلتر EMA روی هفتگی
    wk_ema = ema_trend(df_wk, 10)
    if wk_trend == "BULLISH" and wk_ema == "BEARISH":
        wk_trend = "RANGING"
    elif wk_trend == "BEARISH" and wk_ema == "BULLISH":
        wk_trend = "RANGING"

    # هم‌راستایی کامل → بهترین حالت
    if wk_trend == "BULLISH" and d_trend == "BULLISH":
        return "BULLISH", "هفتگی و روزانه صعودی ✅", wk_trend, d_trend, False
    if wk_trend == "BEARISH" and d_trend == "BEARISH":
        return "BEARISH", "هفتگی و روزانه نزولی ✅", wk_trend, d_trend, False

    # هفتگی رنج + روزانه واضح → اجازه با جریمه (رایج در بازار واقعی)
    if wk_trend == "RANGING" and d_trend == "BULLISH":
        return "BULLISH", "هفتگی رنج / روزانه صعودی (نیمه‌قوی)", wk_trend, d_trend, True
    if wk_trend == "RANGING" and d_trend == "BEARISH":
        return "BEARISH", "هفتگی رنج / روزانه نزولی (نیمه‌قوی)", wk_trend, d_trend, True

    # روزانه رنج + هفتگی واضح
    if d_trend == "RANGING" and wk_trend == "BULLISH":
        return "BULLISH", "هفتگی صعودی / روزانه رنج (ضعیف)", wk_trend, d_trend, True
    if d_trend == "RANGING" and wk_trend == "BEARISH":
        return "BEARISH", "هفتگی نزولی / روزانه رنج (ضعیف)", wk_trend, d_trend, True

    # تضاد مستقیم → بلاک سخت (اصل ICT: با HTF نجنگ)
    if (wk_trend == "BULLISH" and d_trend == "BEARISH") or \
       (wk_trend == "BEARISH" and d_trend == "BULLISH"):
        return "CONFLICT", f"تضاد مستقیم: هفتگی {wk_trend} / روزانه {d_trend}", wk_trend, d_trend, False

    return "NEUTRAL", f"هفتگی {wk_trend} / روزانه {d_trend}", wk_trend, d_trend, False


# ============================================================
#  لایه تکنیکال (نسخه ۴.۳ - مبتنی بر بهترین روش‌های دنیا)
# ============================================================

def _drop_forming(df, minutes):
    """حذف کندل در حال تشکیل برای جلوگیری از repaint"""
    try:
        if df is None or len(df) < 2:
            return df
        t = pd.to_datetime(df["time"].iloc[-1])
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        now_utc = pd.Timestamp.now(tz="UTC")
        if t + pd.Timedelta(minutes=minutes) > now_utc:
            return df.iloc[:-1].reset_index(drop=True)
    except Exception:
        logger.warning("_drop_forming: خطا در بررسی کندل در حال تشکیل", exc_info=True)
    return df


def analyze_technical_topdown(data_dict, symbol: str = None, now: datetime = None):
    """
    تحلیل تکنیکال - استفاده مستقیم از استراتژی Top-Down SMC موتور بک‌تست
    جهت تضمین ۱۰۰٪ انطباق عددی تحلیل زنده با بک‌تست.
    """
    from engine.strategy.smc_topdown import analyze as smc_analyze
    from engine.core.config import EngineConfig

    profile = get_symbol_profile(symbol) if symbol else {
        "min_score": MIN_SIGNAL_SCORE,
        "sl_atr_mult": SL_ATR_MULTIPLIER,
    }

    # آماده‌سازی دیتافریم‌های پاک‌شده (حذف کندل‌های در حال تشکیل)
    cleaned_data = {}
    m_map = {"1wk": 10080, "1d": 1440, "4h": 240, "1h": 60, "15m": 15, "5m": 5, "1m": 1}
    for tf, df in data_dict.items():
        if df is not None:
            df_m = pd.DataFrame(df)
            cleaned_data[tf] = _drop_forming(df_m, m_map.get(tf, 15))

    df_15m = cleaned_data.get("15m")
    price = float(df_15m["close"].iloc[-1]) if (df_15m is not None and len(df_15m) > 0) else None
    atr = calculate_atr(df_15m) if (df_15m is not None and len(df_15m) > 0) else 0.0

    df_4h = cleaned_data.get("4h")
    pd_zone = get_premium_discount(df_4h, lookback=60) if (df_4h is not None and len(df_4h) >= 20) else {"zone": "-"}

    # اجرای استراتژی SMC موتور اصلی
    cfg = EngineConfig(min_score=profile["min_score"], sl_atr1h_mult=profile["sl_atr_mult"])
    now_dt = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_dt.tzinfo is None:
        now_dt = now_dt.tz_localize("UTC")

    dec = smc_analyze(cleaned_data, cfg, now_dt)

    blocked = dec.blocked is not None or dec.direction is None

    return {
        "score": round(dec.score, 2),
        "direction": dec.direction,
        "price": price if price is not None else dec.entry,
        "suggested_sl": dec.sl,
        "suggested_tp": dec.tp,
        "sl": dec.sl,
        "tp": dec.tp,
        "trend": dec.bias,
        "zone": pd_zone["zone"],
        "atr": round(atr, 6),
        "reasons": dec.reasons or ([f"🚫 {dec.blocked}"] if dec.blocked else ["در انتظار"]),
        "blocked": blocked,
    }


# ============================================================
#  تحلیل کامل ۴ لایه
# ============================================================

async def full_analysis(data_dict, symbol, news_safe=True,
                         news_reason="", news_sentiment=0.0):
    from .fundamental_analyzer import calculate_fundamental_score
    from .market_memory import calculate_memory_score

    tech = analyze_technical_topdown(data_dict, symbol=symbol)

    if tech.get("blocked"):
        return {
            "score": 0.0,
            "direction": None,
            "current_price": tech.get("price"),
            "suggested_sl": None,
            "suggested_tp": None,
            "htf_trend": tech.get("trend", "RANGING"),
            "zone": tech.get("zone", "-"),
            "atr": tech.get("atr", 0),
            "reasons": tech.get("reasons", []),
            "summary": tech.get("reasons", ["در انتظار"])[0] if tech.get("reasons") else "در انتظار",
        }

    fund = await calculate_fundamental_score(symbol)
    news_score = news_sentiment
    mem = calculate_memory_score(symbol)

    final_score = (
        tech["score"] * WEIGHT_TECHNICAL +
        fund["score"] * WEIGHT_FUNDAMENTAL +
        news_score * WEIGHT_NEWS +
        mem["score"] * WEIGHT_MEMORY
    )

    if not news_safe:
        final_score = 0.0
        logger.info("%s: خبر ایمن نیست، final_score صفر شد", symbol)

    # آستانه نهایی: فقط اضافه‌ی پروفایل (نسبت به سراسری) با وزن تکنیکال اعمال می‌شود
    # تا سخت‌گیری جفت‌های پرریسک، final_score ترکیبی را تقریباً غیرفعال نکند.
    profile = get_symbol_profile(symbol)
    final_min = MIN_SIGNAL_SCORE + (profile["min_score"] - MIN_SIGNAL_SCORE) * WEIGHT_TECHNICAL

    direction = None
    if final_score >= final_min:
        direction = "BUY"
    elif final_score <= -final_min:
        direction = "SELL"

    # هم‌جهتی اجباری
    if tech["score"] > 0 and direction == "SELL":
        direction = None
    if tech["score"] < 0 and direction == "BUY":
        direction = None
    if direction and (tech["sl"] is None or tech["tp"] is None):
        direction = None

    logger.info(
        "%s: final_score=%.2f tech=%.2f fund=%.2f news=%.2f mem=%.2f direction=%s",
        symbol, final_score, tech["score"], fund["score"], news_score, mem["score"], direction,
    )

    all_reasons = [f"📈 تکنیکال: {tech['score']:+.2f}"]
    all_reasons.extend(tech["reasons"])

    if fund["reasons"]:
        all_reasons.append(f"💵 فاندامنتال: {fund['score']:+.2f}")
        all_reasons.extend(fund["reasons"])

    if news_reason:
        all_reasons.append(f"📰 {news_reason}")

    if mem["reasons"]:
        all_reasons.append(f"🧠 حافظه: {mem['score']:+.2f}")
        all_reasons.extend(mem["reasons"])

    grade = classify_setup_grade(tech["score"], final_score, all_reasons) if direction else "-"

    return {
        "score": round(final_score, 2),
        "grade": grade,
        "technical_score": tech["score"],
        "fundamental_score": fund["score"],
        "news_score": news_score,
        "memory_score": mem["score"],
        "direction": direction,
        "current_price": tech["price"],
        "suggested_sl": tech["sl"],
        "suggested_tp": tech["tp"],
        "htf_trend": tech["trend"],
        "zone": tech["zone"],
        "atr": tech["atr"],
        "reasons": all_reasons,
        "summary": f"امتیاز کل: {final_score:+.2f} (ستاپ {grade})" if direction else "در انتظار",
    }
