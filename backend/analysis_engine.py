"""
موتور تحلیل تکنیکال SMC/ICT - نسخه 3.0
4 لایه: تکنیکال + فاندامنتال + اخبار + حافظه
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
#  لایه ۱: تکنیکال
# ============================================================

def analyze_technical(data_htf, data_ltf):
    df_htf = pd.DataFrame(data_htf)
    df_ltf = pd.DataFrame(data_ltf)

    required = {"open", "high", "low", "close"}
    if not required.issubset(df_htf.columns) or not required.issubset(df_ltf.columns):
        return None

    if len(df_htf) < MIN_CANDLES or len(df_ltf) < MIN_CANDLES:
        return None

    price = float(df_ltf["close"].iloc[-1])
    atr = calculate_atr(df_ltf)
    if atr == 0:
        return None

    htf_struct = detect_structure(df_htf)
    htf_trend = htf_struct["trend"]

    ema_dir = ema_trend(df_htf, 50)
    if htf_trend == "BULLISH" and ema_dir == "BEARISH":
        htf_trend = "RANGING"
    elif htf_trend == "BEARISH" and ema_dir == "BULLISH":
        htf_trend = "RANGING"

    htf_obs = find_order_blocks(df_htf, 80)
    htf_pd = get_premium_discount(df_htf, 60)
    ltf_fvg = detect_fvg(df_ltf, 15)

    score = 0.0
    reasons = []
    direction = None

    if htf_trend == "BULLISH" and htf_obs["bullish"]:
        ob = htf_obs["bullish"]
        in_ob = ob["low"] * 0.998 <= price <= ob["high"] * 1.002
        if in_ob and htf_pd["zone"] == "DISCOUNT":
            score += 0.5
            reasons.append("OB صعودی HTF + Discount")
            if ltf_fvg and ltf_fvg["type"] == "BULLISH":
                score += 0.2
                reasons.append("FVG صعودی")
            if get_candle_confirmation(df_ltf, "BUY"):
                score += 0.2
                reasons.append("تأییدیه کندلی صعودی")
            if score >= MIN_SIGNAL_SCORE:
                direction = "BUY"

    elif htf_trend == "BEARISH" and htf_obs["bearish"]:
        ob = htf_obs["bearish"]
        in_ob = ob["low"] * 0.998 <= price <= ob["high"] * 1.002
        if in_ob and htf_pd["zone"] == "PREMIUM":
            score -= 0.5
            reasons.append("OB نزولی HTF + Premium")
            if ltf_fvg and ltf_fvg["type"] == "BEARISH":
                score -= 0.2
                reasons.append("FVG نزولی")
            if get_candle_confirmation(df_ltf, "SELL"):
                score -= 0.2
                reasons.append("تأییدیه کندلی نزولی")
            if score <= -MIN_SIGNAL_SCORE:
                direction = "SELL"

    sl = tp = None
    if direction == "BUY":
        ob = htf_obs["bullish"]
        ob_size = ob["high"] - ob["low"]
        buf = max(atr * SL_ATR_MULTIPLIER, ob_size * SL_OB_BUFFER)
        sl = round(ob["low"] - buf, 6)
        risk = price - sl
        tp = round(price + risk * RISK_REWARD, 6)
    elif direction == "SELL":
        ob = htf_obs["bearish"]
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
        "trend": htf_trend,
        "zone": htf_pd["zone"],
        "reasons": reasons,
        "atr": round(atr, 6),
    }


# ============================================================
#  تحلیل کامل ۴ لایه
# ============================================================

async def full_analysis(data_htf, data_ltf, symbol, news_safe=True,
                         news_reason="", news_sentiment=0.0):
    from .fundamental_analyzer import calculate_fundamental_score
    from .market_memory import calculate_memory_score

    tech = analyze_technical(data_htf, data_ltf)
    if not tech:
        return {"score": None, "error": "داده کافی نیست"}

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

    direction = None
    if final_score >= MIN_SIGNAL_SCORE:
        direction = "BUY"
    elif final_score <= -MIN_SIGNAL_SCORE:
        direction = "SELL"

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
