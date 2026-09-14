"""
موتور تحلیل Top-Down SMC/ICT - نسخه 4.0
5 تایم‌فریم + ICT Unicorn + 4 لایه امتیازدهی
"""
import logging
import pandas as pd
import numpy as np

from .config import (
    MIN_CANDLES, MIN_SIGNAL_SCORE, RISK_REWARD,
    SL_ATR_MULTIPLIER, SL_OB_BUFFER,
    WEIGHT_TECHNICAL, WEIGHT_FUNDAMENTAL, WEIGHT_NEWS, WEIGHT_MEMORY,
)

logger = logging.getLogger(__name__)


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
    highs, lows = [], []
    for i in range(left, len(df) - right):
        wh = df["high"].iloc[i - left:i + right + 1]
        wl = df["low"].iloc[i - left:i + right + 1]
        if df["high"].iloc[i] == wh.max():
            highs.append((i, float(df["high"].iloc[i])))
        if df["low"].iloc[i] == wl.min():
            lows.append((i, float(df["low"].iloc[i])))
    return {"highs": highs, "lows": lows}


def detect_structure(df):
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
    elif hh or hl:
        trend = "BULLISH"
    elif lh_ or ll_:
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
    for i in range(len(w) - 3, 1, -1):
        cur, nxt = w.iloc[i], w.iloc[i + 1]
        body = abs(nxt["close"] - nxt["open"])

        if (cur["close"] < cur["open"]
                and nxt["close"] > cur["high"]
                and body > avg_body * 1.5
                and bull is None):
            bull = {"high": float(cur["high"]), "low": float(cur["low"]),
                    "mid": float((cur["high"] + cur["low"]) / 2)}

        if (cur["close"] > cur["open"]
                and nxt["close"] < cur["low"]
                and body > avg_body * 1.5
                and bear is None):
            bear = {"high": float(cur["high"]), "low": float(cur["low"]),
                    "mid": float((cur["high"] + cur["low"]) / 2)}

        if bull and bear:
            break
    return {"bullish": bull, "bearish": bear}


def detect_fvg(df, lookback=15):
    if len(df) < 5:
        return None
    price = float(df["close"].iloc[-1])
    start = max(2, len(df) - lookback)
    for i in range(len(df) - 1, start, -1):
        c1, c3 = df.iloc[i - 2], df.iloc[i]
        if c3["low"] > c1["high"]:
            t, b = float(c3["low"]), float(c1["high"])
            if price > b:
                return {"type": "BULLISH", "top": t, "bottom": b}
        if c3["high"] < c1["low"]:
            t, b = float(c1["low"]), float(c3["high"])
            if price < t:
                return {"type": "BEARISH", "top": t, "bottom": b}
    return None


def get_premium_discount(df, lookback=60):
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


# ============================================================
#  تشخیص Liquidity Sweep (شکار نقدینگی)
# ============================================================

def detect_liquidity_sweep(df, lookback=30):
    if len(df) < lookback + 3:
        return None

    recent = df.iloc[-lookback:-3]
    prev_high = float(recent["high"].max())
    prev_low = float(recent["low"].min())

    for i in range(-3, 0):
        c = df.iloc[i]
        if c["high"] > prev_high and c["close"] < prev_high:
            return {"type": "BEARISH_SWEEP",
                    "level": prev_high,
                    "wick": float(c["high"])}
        if c["low"] < prev_low and c["close"] > prev_low:
            return {"type": "BULLISH_SWEEP",
                    "level": prev_low,
                    "wick": float(c["low"])}
    return None


# ============================================================
#  تشخیص CHOCH (تغییر ساختار)
# ============================================================

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

    # CHOCH صعودی: شکست سقف قبلی
    if price > last_high:
        return {"type": "BULLISH_CHOCH", "level": last_high}
    if price < last_low:
        return {"type": "BEARISH_CHOCH", "level": last_low}
    return None


# ============================================================
#  تشخیص Breaker Block
# ============================================================

def find_breaker_block(df, lookback=40):
    """Breaker Block: OB شکسته‌شده که حالا به عنوان مقاومت/حمایت عمل می‌کنه"""
    obs = find_order_blocks(df, lookback)
    price = float(df["close"].iloc[-1])

    # اگه قیمت OB صعودی رو شکسته باشه
    if obs["bullish"] and price < obs["bullish"]["low"]:
        return {"type": "BEARISH_BREAKER",
                "high": obs["bullish"]["high"],
                "low": obs["bullish"]["low"]}

    if obs["bearish"] and price > obs["bearish"]["high"]:
        return {"type": "BULLISH_BREAKER",
                "high": obs["bearish"]["high"],
                "low": obs["bearish"]["low"]}
    return None


# ============================================================
#  مدل ICT Unicorn (ورود پیشرفته)
# ============================================================

def detect_unicorn_setup(df_4h, df_1h, df_15m):
    """
    مدل Unicorn:
    1. Liquidity Sweep در 15m
    2. Displacement (حرکت شارپ)
    3. CHoCH در 1h
    4. بازگشت به Breaker + FVG
    """
    sweep = detect_liquidity_sweep(df_15m, lookback=30)
    if not sweep:
        return None

    choch = detect_choch(df_1h, lookback=20)
    if not choch:
        return None

    # هم‌جهتی: Sweep و CHOCH باید هم‌جهت باشن
    if sweep["type"] == "BULLISH_SWEEP" and choch["type"] != "BULLISH_CHOCH":
        return None
    if sweep["type"] == "BEARISH_SWEEP" and choch["type"] != "BEARISH_CHOCH":
        return None

    breaker = find_breaker_block(df_4h, lookback=40)
    fvg = detect_fvg(df_15m, lookback=15)

    return {
        "sweep": sweep,
        "choch": choch,
        "breaker": breaker,
        "fvg": fvg,
        "direction": "BUY" if sweep["type"] == "BULLISH_SWEEP" else "SELL",
    }


# ============================================================
#  فیلتر Top-Down (هفتگی + روزانه)
# ============================================================

def top_down_filter(data_dict):
    """
    فیلتر محیطی: هفتگی و روزانه باید هم‌جهت باشن
    Returns: (direction, reason)
    """
    try:
        df_wk = pd.DataFrame(data_dict["1wk"])
        df_d = pd.DataFrame(data_dict["1d"])
    except (KeyError, Exception):
        return "NEUTRAL", "داده کافی برای فیلتر"

    # تشخیص روند
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

    # هر دو باید هم‌جهت باشن
    if wk_trend == "BULLISH" and d_trend == "BULLISH":
        return "BULLISH", "هفتگی و روزانه صعودی ✅"
    if wk_trend == "BEARISH" and d_trend == "BEARISH":
        return "BEARISH", "هفتگی و روزانه نزولی ✅"

    if wk_trend != d_trend:
        return "CONFLICT", f"تضاد: هفتگی {wk_trend} / روزانه {d_trend}"

    return "NEUTRAL", f"هفتگی {wk_trend} / روزانه {d_trend}"


# ============================================================
#  لایه تکنیکال جدید (Top-Down + Unicorn)
# ============================================================

def analyze_technical_topdown(data_dict):
    """
    تحلیل تکنیکال با 5 تایم‌فریم
    Returns: dict با score, direction, sl, tp, reasons
    """
    # --- فیلتر Top-Down ---
    td_dir, td_reason = top_down_filter(data_dict)

    if td_dir == "CONFLICT":
        return {
            "score": 0.0,
            "direction": None,
            "reasons": [f"🚫 {td_reason}"],
            "blocked": True,
        }

    if td_dir == "NEUTRAL":
        return {
            "score": 0.0,
            "direction": None,
            "reasons": [f"⏸ {td_reason}"],
            "blocked": True,
        }

    # --- تحلیل در 4h (OB اصلی) ---
    try:
        df_4h = pd.DataFrame(data_dict["4h"])
        df_1h = pd.DataFrame(data_dict["1h"])
        df_15m = pd.DataFrame(data_dict["15m"])
    except (KeyError, Exception) as e:
        return {"score": 0.0, "direction": None,
                "reasons": [f"خطا: {e}"], "blocked": True}

    if len(df_4h) < 30 or len(df_1h) < MIN_CANDLES or len(df_15m) < MIN_CANDLES:
        return {"score": 0.0, "direction": None,
                "reasons": ["داده کافی نیست"], "blocked": True}

    price = float(df_15m["close"].iloc[-1])
    atr = calculate_atr(df_15m)
    if atr == 0:
        return {"score": 0.0, "direction": None,
                "reasons": ["ATR صفر"], "blocked": True}

    htf_obs = find_order_blocks(df_4h, lookback=60)
    htf_pd = get_premium_discount(df_4h, lookback=60)
    ltf_fvg = detect_fvg(df_15m, lookback=15)
    unicorn = detect_unicorn_setup(df_4h, df_1h, df_15m)

    score = 0.0
    reasons = [f"🎯 فیلتر محیطی: {td_reason}"]

    direction = None
    sl = tp = None

    # ============================================
    #  BUY Setup
    # ============================================
    if td_dir == "BULLISH" and htf_obs["bullish"]:
        ob = htf_obs["bullish"]
        in_ob = ob["low"] * 0.998 <= price <= ob["high"] * 1.002

        if in_ob and htf_pd["zone"] == "DISCOUNT":
            score += 0.5
            reasons.append("OB صعودی 4H + Discount")

            if ltf_fvg and ltf_fvg["type"] == "BULLISH":
                score += 0.15
                reasons.append("FVG صعودی 15m")

            if get_candle_confirmation(df_15m, "BUY"):
                score += 0.15
                reasons.append("تأییدیه کندلی صعودی")

            if unicorn and unicorn["direction"] == "BUY":
                score += 0.2
                reasons.append("🦄 ستاپ Unicorn صعودی")

            if score >= MIN_SIGNAL_SCORE:
                direction = "BUY"
                ob_size = ob["high"] - ob["low"]
                buf = max(atr * SL_ATR_MULTIPLIER, ob_size * SL_OB_BUFFER)
                sl = round(ob["low"] - buf, 6)
                risk = price - sl
                tp = round(price + risk * RISK_REWARD, 6)

    # ============================================
    #  SELL Setup
    # ============================================
    elif td_dir == "BEARISH" and htf_obs["bearish"]:
        ob = htf_obs["bearish"]
        in_ob = ob["low"] * 0.998 <= price <= ob["high"] * 1.002

        if in_ob and htf_pd["zone"] == "PREMIUM":
            score -= 0.5
            reasons.append("OB نزولی 4H + Premium")

            if ltf_fvg and ltf_fvg["type"] == "BEARISH":
                score -= 0.15
                reasons.append("FVG نزولی 15m")

            if get_candle_confirmation(df_15m, "SELL"):
                score -= 0.15
                reasons.append("تأییدیه کندلی نزولی")

            if unicorn and unicorn["direction"] == "SELL":
                score -= 0.2
                reasons.append("🦄 ستاپ Unicorn نزولی")

            if score <= -MIN_SIGNAL_SCORE:
                direction = "SELL"
                ob_size = ob["high"] - ob["low"]
                buf = max(atr * SL_ATR_MULTIPLIER, ob_size * SL_OB_BUFFER)
                sl = round(ob["high"] + buf, 6)
                risk = sl - price
                tp = round(price - risk * RISK_REWARD, 6)

    return {
        "score": round(score, 2),
        "direction": direction,
        "price": price,
        "sl": sl,
        "tp": tp,
        "trend": td_dir,
        "zone": htf_pd["zone"],
        "atr": round(atr, 6),
        "reasons": reasons,
        "blocked": False,
    }


# ============================================================
#  تحلیل کامل ۴ لایه
# ============================================================

async def full_analysis(data_dict, symbol, news_safe=True,
                         news_reason="", news_sentiment=0.0):
    """
    تحلیل کامل با 4 لایه (Top-Down)
    data_dict: {"1wk": [...], "1d": [...], "4h": [...], "1h": [...], "15m": [...]}
    """
    from .fundamental_analyzer import calculate_fundamental_score
    from .market_memory import calculate_memory_score

    # لایه 1: تکنیکال Top-Down
    tech = analyze_technical_topdown(data_dict)
    if tech.get("blocked"):
        return {
            "score": 0.0,
            "direction": None,
            "current_price": tech.get("price"),
            "suggested_sl": None,
            "suggested_tp": None,
            "htf_trend": tech.get("trend", "UNCLEAR"),
            "zone": tech.get("zone", "-"),
            "atr": tech.get("atr", 0),
            "reasons": tech.get("reasons", []),
            "summary": tech.get("reasons", ["در انتظار"])[0],
        }

    # لایه 2: فاندامنتال
    fund = await calculate_fundamental_score(symbol)

    # لایه 3: اخبار
    news_score = news_sentiment

    # لایه 4: حافظه
    mem = calculate_memory_score(symbol)

    final_score = (
        tech["score"] * WEIGHT_TECHNICAL +
        fund["score"] * WEIGHT_FUNDAMENTAL +
        news_score * WEIGHT_NEWS +
        mem["score"] * WEIGHT_MEMORY
    )

    if not news_safe:
        final_score = 0.0

    direction = None
    if final_score >= MIN_SIGNAL_SCORE:
        direction = "BUY"
    elif final_score <= -MIN_SIGNAL_SCORE:
        direction = "SELL"

    # هم‌جهتی با تکنیکال
    if tech["score"] > 0 and direction == "SELL":
        direction = None
    if tech["score"] < 0 and direction == "BUY":
        direction = None

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
