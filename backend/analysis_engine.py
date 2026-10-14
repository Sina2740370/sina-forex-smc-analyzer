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
import pandas as pd
import numpy as np

from .config import (
    MIN_CANDLES, MIN_SIGNAL_SCORE, RISK_REWARD,
    SL_ATR_MULTIPLIER, SL_OB_BUFFER,
    WEIGHT_TECHNICAL, WEIGHT_FUNDAMENTAL, WEIGHT_NEWS, WEIGHT_MEMORY,
)

logger = logging.getLogger(__name__)

# حداقل فاصلهٔ ریسک (SL تا قیمت) بر حسب ضریب ATR — قبلاً هیچ حداقلی چک نمی‌شد
# و در صورت کوچک بودن OB، SL می‌توانست غیرواقعی نزدیک به قیمت باشد.
# در صورت نیاز به تنظیم‌پذیری، می‌توان این را به config.py منتقل کرد.
MIN_RISK_ATR_RATIO = 0.25


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
    highs = [(i, float(high.iloc[i])) for i in range(left, n - right) if is_high.iloc[i]]
    lows = [(i, float(low.iloc[i])) for i in range(left, n - right) if is_low.iloc[i]]
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


def is_london_session(dt=None):
    """تقریبی (بدون احتساب دقیق DST): سشن لندن ~ ۰۷:۰۰ تا ۱۶:۰۰ UTC."""
    dt = dt or datetime.now(timezone.utc)
    return 7 <= dt.hour < 16


def is_ny_session(dt=None):
    """تقریبی: سشن نیویورک ~ ۱۲:۰۰ تا ۲۱:۰۰ UTC."""
    dt = dt or datetime.now(timezone.utc)
    return 12 <= dt.hour < 21


def is_london_killzone(dt=None):
    """تقریبی: کیل‌زون بازگشایی لندن ~ ۰۷:۰۰ تا ۱۰:۰۰ UTC."""
    dt = dt or datetime.now(timezone.utc)
    return 7 <= dt.hour < 10


def is_ny_killzone(dt=None):
    """تقریبی: کیل‌زون همپوشانی لندن/نیویورک ~ ۱۲:۰۰ تا ۱۵:۰۰ UTC."""
    dt = dt or datetime.now(timezone.utc)
    return 12 <= dt.hour < 15


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
    """ستاپ کلاسیک ICT: Sweep + CHOCH + (Breaker/FVG)"""
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
    return {
        "sweep": sweep, "choch": choch,
        "breaker": breaker, "fvg": fvg,
        "direction": "BUY" if sweep["type"] == "BULLISH_SWEEP" else "SELL",
    }


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
    except Exception:
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
        t = pd.to_datetime(df["time"].iloc[-1], utc=True)
        if t + pd.Timedelta(minutes=minutes) > pd.Timestamp.now(tz="UTC"):
            return df.iloc[:-1].reset_index(drop=True)
    except Exception:
        pass
    return df


def analyze_technical_topdown(data_dict):
    """
    تحلیل تکنیکال نسخه ۴.۳
    اولویت‌بندی بر اساس روش ICT/SMC حرفه‌ای:
    1. HTF Bias
    2. Liquidity Sweep
    3. Structure Shift (CHOCH)
    4. Entry در FVG یا Order Block
    """
    price = None
    atr = 0.0
    zone = "-"
    df_4h = df_1h = df_15m = None

    try:
        # Drop the still-forming 15m candle before deriving price/ATR. Doing this
        # later leaves entry and risk sizing dependent on an unfinished candle.
        df_15m = _drop_forming(pd.DataFrame(data_dict["15m"]), 15)
        if len(df_15m) > 0:
            price = float(df_15m["close"].iloc[-1])
            atr = calculate_atr(df_15m)
    except Exception:
        pass

    try:
        df_4h = _drop_forming(pd.DataFrame(data_dict["4h"]), 240)
        if len(df_4h) >= 20:
            pd_zone = get_premium_discount(df_4h, lookback=60)
            zone = pd_zone["zone"]
    except Exception:
        pass

    try:
        df_1h = _drop_forming(pd.DataFrame(data_dict["1h"]), 60)
    except Exception:
        pass

    td_dir, td_reason, wk_trend, d_trend, soft = top_down_filter(data_dict)

    base = {
        "score": 0.0,
        "direction": None,
        "price": price,
        "sl": None,
        "tp": None,
        "trend": td_dir if td_dir in ("BULLISH", "BEARISH") else (wk_trend or "RANGING"),
        "wk_trend": wk_trend,
        "d_trend": d_trend,
        "zone": zone,
        "atr": round(atr, 6) if atr else 0,
        "reasons": [],
        "blocked": False,
    }

    # فقط تضاد مستقیم سخت بلاک می‌شود
    if td_dir == "CONFLICT":
        base["reasons"] = [f"🚫 {td_reason}", f"هفتگی: {wk_trend}", f"روزانه: {d_trend}"]
        base["blocked"] = True
        logger.info("بلاک شد (CONFLICT): wk=%s d=%s", wk_trend, d_trend)
        return base

    if td_dir == "NEUTRAL":
        base["reasons"] = [f"⏸ {td_reason}"]
        base["blocked"] = True
        logger.debug("بلاک شد (NEUTRAL): wk=%s d=%s", wk_trend, d_trend)
        return base

    if df_4h is None or df_1h is None or df_15m is None:
        base["reasons"] = ["داده کافی نیست"]
        base["blocked"] = True
        return base

    if len(df_4h) < 30 or len(df_1h) < MIN_CANDLES or len(df_15m) < MIN_CANDLES:
        base["reasons"] = ["داده کافی نیست"]
        base["blocked"] = True
        return base

    if atr == 0 or price is None:
        base["reasons"] = ["ATR یا قیمت نامعتبر"]
        base["blocked"] = True
        return base

    # Keep ATRs on their own timeframe: 15m for sweep strength, 1h for SL/risk,
    # and 4h for distance to 4h order blocks.
    atr_1h = calculate_atr(df_1h)
    atr_4h = calculate_atr(df_4h)
    if atr_1h == 0 or atr_4h == 0:
        base["reasons"] = ["ATR تایم‌فریم بالاتر نامعتبر"]
        base["blocked"] = True
        return base

    # آماده‌سازی داده‌ها؛ هر سه تایم‌فریم پایین/میانی فقط کندل بسته‌شده دارند.
    htf_obs = find_order_blocks(df_4h, lookback=60)
    htf_pd = get_premium_discount(df_4h, lookback=60)
    ltf_fvg = detect_fvg(df_15m, lookback=15)
    sweep = detect_liquidity_sweep(df_15m, lookback=30)
    choch = detect_choch(df_1h, lookback=20)
    unicorn = detect_unicorn_setup(df_4h, df_1h, df_15m)
    sweep_mult = sweep_strength_multiplier(sweep, atr)
    session = current_session_label()

    score = 0.0
    reasons = [f"🎯 فیلتر محیطی: {td_reason}"]
    base["session"] = session

    # جریمه حالت soft
    if soft:
        score -= 0.10
        reasons.append("⚠️ هم‌راستایی ناقص HTF (جریمه -۰.۱۰)")

    # بونوس کوچک و غیرمسدودکننده برای معامله داخل کیل‌زون (فقط تقویت، نه فیلتر سخت)
    if session == "KILLZONE":
        reasons.append("⏰ داخل کیل‌زون لندن/نیویورک")

    direction = None
    sl = tp = None

    # ============================================================
    #  BUY Logic
    # ============================================================
    if td_dir == "BULLISH":
        # ۱. امتیاز پایه هم‌راستایی HTF
        score += 0.15
        reasons.append("هم‌راستایی صعودی HTF (+۰.۱۵)")

        # ۲. Liquidity Sweep (مهم‌ترین تریگر از نظر ICT) — امتیاز متناسب با قدرت Sweep
        if sweep and sweep["type"] == "BULLISH_SWEEP":
            add = round(0.25 * sweep_mult, 4)
            score += add
            reasons.append(f"🔄 Liquidity Sweep صعودی ×{sweep_mult:.2f} (+{add:.2f})")
        elif sweep and sweep["type"] == "BEARISH_SWEEP":
            pen = round(0.08 * sweep_mult, 4)
            score -= pen
            reasons.append(f"Sweep مخالف جهت (جریمه -{pen:.2f})")

        # کیل‌زون: بونوس کوچک غیرمسدودکننده
        if session == "KILLZONE":
            score += 0.05
            reasons.append("⏰ کیل‌زون فعال (+۰.۰۵)")

        # ۳. Structure Shift (CHOCH)
        if choch and choch["type"] == "BULLISH_CHOCH":
            score += 0.18
            reasons.append("CHOCH صعودی (+۰.۱۸)")

        # ۴. Order Block + فاصله
        ob = htf_obs.get("bullish")
        dist = _distance_to_ob(price, ob, atr_4h) if ob else 999.0

        if ob:
            if dist == 0.0:
                score += 0.30
                reasons.append("داخل OB صعودی 4H (+۰.۳۰)")
            elif dist <= 0.50:
                score += 0.20
                reasons.append(f"نزدیک OB صعودی ({dist:.2f} ATR) (+۰.۲۰)")
            elif dist <= 1.0:
                score += 0.10
                reasons.append(f"نسبتاً نزدیک OB ({dist:.2f} ATR) (+۰.۱۰)")
            else:
                reasons.append(f"دور از OB صعودی ({dist:.2f} ATR)")
        else:
            reasons.append("OB صعودی 4H پیدا نشد")

        # ۵. Premium / Discount (نرم)
        if htf_pd["zone"] == "DISCOUNT":
            score += 0.12
            reasons.append("ناحیه Discount (+۰.۱۲)")
        else:
            score -= 0.12
            reasons.append("ناحیه PREMIUM (جریمه -۰.۱۲)")

        # ۶. FVG
        if ltf_fvg and ltf_fvg["type"] == "BULLISH":
            score += 0.12
            reasons.append("FVG صعودی 15m (+۰.۱۲)")

        # ۷. Unicorn کامل (بالاترین confluence)
        if unicorn and unicorn["direction"] == "BUY":
            score += 0.22
            reasons.append("🦄 ستاپ Unicorn کامل صعودی (+۰.۲۲)")

        # ۸. تأیید کندلی
        if get_candle_confirmation(df_15m, "BUY"):
            score += 0.10
            reasons.append("تأییدیه کندلی (+۰.۱۰)")

        # صدور جهت فقط با confluence کافی
        # شرط: امتیاز کافی + (نزدیک OB یا Unicorn یا Sweep+CHOCH)
        strong_confluence = (
            (ob and dist <= 0.75) or
            (unicorn and unicorn["direction"] == "BUY") or
            (sweep and sweep["type"] == "BULLISH_SWEEP" and choch and choch["type"] == "BULLISH_CHOCH")
        )

        if score >= MIN_SIGNAL_SCORE and strong_confluence:
            direction = "BUY"
            # SL منطقی: زیر OB یا زیر سطح Sweep
            if ob:
                ob_size = ob["high"] - ob["low"]
                buf = max(atr_1h * SL_ATR_MULTIPLIER, ob_size * SL_OB_BUFFER)
                sl = round(ob["low"] - buf, 6)
            elif sweep:
                sl = round(sweep["level"] - atr_1h * SL_ATR_MULTIPLIER, 6)
            else:
                sl = round(price - atr_1h * SL_ATR_MULTIPLIER * 1.5, 6)

            risk = price - sl
            min_risk = atr_1h * MIN_RISK_ATR_RATIO
            if risk <= 0:
                direction = None
                sl = None
                reasons.append("ریسک نامعتبر")
                logger.warning("BUY: ریسک نامعتبر (risk=%.6f)", risk)
            elif risk < min_risk:
                direction = None
                sl = None
                reasons.append(f"ریسک خیلی کوچک ({risk:.5f} < حداقل {min_risk:.5f})")
                logger.warning("BUY: ریسک کمتر از حداقل مجاز (risk=%.6f, min=%.6f)", risk, min_risk)
            else:
                tp = round(price + risk * RISK_REWARD, 6)

    # ============================================================
    #  SELL Logic
    # ============================================================
    elif td_dir == "BEARISH":
        score -= 0.15
        reasons.append("هم‌راستایی نزولی HTF (-۰.۱۵)")

        if sweep and sweep["type"] == "BEARISH_SWEEP":
            add = round(0.25 * sweep_mult, 4)
            score -= add
            reasons.append(f"🔄 Liquidity Sweep نزولی ×{sweep_mult:.2f} (-{add:.2f})")
        elif sweep and sweep["type"] == "BULLISH_SWEEP":
            pen = round(0.08 * sweep_mult, 4)
            score += pen
            reasons.append(f"Sweep مخالف جهت (جریمه +{pen:.2f})")

        if session == "KILLZONE":
            score -= 0.05
            reasons.append("⏰ کیل‌زون فعال (-۰.۰۵)")

        if choch and choch["type"] == "BEARISH_CHOCH":
            score -= 0.18
            reasons.append("CHOCH نزولی (-۰.۱۸)")

        ob = htf_obs.get("bearish")
        dist = _distance_to_ob(price, ob, atr_4h) if ob else 999.0

        if ob:
            if dist == 0.0:
                score -= 0.30
                reasons.append("داخل OB نزولی 4H (-۰.۳۰)")
            elif dist <= 0.50:
                score -= 0.20
                reasons.append(f"نزدیک OB نزولی ({dist:.2f} ATR) (-۰.۲۰)")
            elif dist <= 1.0:
                score -= 0.10
                reasons.append(f"نسبتاً نزدیک OB ({dist:.2f} ATR) (-۰.۱۰)")
            else:
                reasons.append(f"دور از OB نزولی ({dist:.2f} ATR)")
        else:
            reasons.append("OB نزولی 4H پیدا نشد")

        if htf_pd["zone"] == "PREMIUM":
            score -= 0.12
            reasons.append("ناحیه Premium (-۰.۱۲)")
        else:
            score += 0.12
            reasons.append("ناحیه DISCOUNT (جریمه معکوس +۰.۱۲)")

        if ltf_fvg and ltf_fvg["type"] == "BEARISH":
            score -= 0.12
            reasons.append("FVG نزولی 15m (-۰.۱۲)")

        if unicorn and unicorn["direction"] == "SELL":
            score -= 0.22
            reasons.append("🦄 ستاپ Unicorn کامل نزولی (-۰.۲۲)")

        if get_candle_confirmation(df_15m, "SELL"):
            score -= 0.10
            reasons.append("تأییدیه کندلی (-۰.۱۰)")

        strong_confluence = (
            (ob and dist <= 0.75) or
            (unicorn and unicorn["direction"] == "SELL") or
            (sweep and sweep["type"] == "BEARISH_SWEEP" and choch and choch["type"] == "BEARISH_CHOCH")
        )

        if score <= -MIN_SIGNAL_SCORE and strong_confluence:
            direction = "SELL"
            if ob:
                ob_size = ob["high"] - ob["low"]
                buf = max(atr_1h * SL_ATR_MULTIPLIER, ob_size * SL_OB_BUFFER)
                sl = round(ob["high"] + buf, 6)
            elif sweep:
                sl = round(sweep["level"] + atr_1h * SL_ATR_MULTIPLIER, 6)
            else:
                sl = round(price + atr_1h * SL_ATR_MULTIPLIER * 1.5, 6)

            risk = sl - price
            min_risk = atr_1h * MIN_RISK_ATR_RATIO
            if risk <= 0:
                direction = None
                sl = None
                reasons.append("ریسک نامعتبر")
                logger.warning("SELL: ریسک نامعتبر (risk=%.6f)", risk)
            elif risk < min_risk:
                direction = None
                sl = None
                reasons.append(f"ریسک خیلی کوچک ({risk:.5f} < حداقل {min_risk:.5f})")
                logger.warning("SELL: ریسک کمتر از حداقل مجاز (risk=%.6f, min=%.6f)", risk, min_risk)
            else:
                tp = round(price - risk * RISK_REWARD, 6)

    base.update({
        "score": round(score, 2),
        "direction": direction,
        "sl": sl,
        "tp": tp,
        "zone": htf_pd["zone"],
        "reasons": reasons,
    })
    return base


# ============================================================
#  تحلیل کامل ۴ لایه
# ============================================================

async def full_analysis(data_dict, symbol, news_safe=True,
                         news_reason="", news_sentiment=0.0):
    from .fundamental_analyzer import calculate_fundamental_score
    from .market_memory import calculate_memory_score

    tech = analyze_technical_topdown(data_dict)

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

    direction = None
    if final_score >= MIN_SIGNAL_SCORE:
        direction = "BUY"
    elif final_score <= -MIN_SIGNAL_SCORE:
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

    return {
        "score": round(final_score, 2),
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
        "summary": f"امتیاز کل: {final_score:+.2f}" if direction else "در انتظار",
    }
