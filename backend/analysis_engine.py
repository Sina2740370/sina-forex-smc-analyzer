"""
موتور تحلیل تکنیکال SMC/ICT
ترکیب Order Block + FVG + ساختار بازار + Premium/Discount
"""
import logging
import pandas as pd
import numpy as np

from .config import (
    MIN_CANDLES, MIN_SIGNAL_SCORE, RISK_REWARD, SL_ATR_MULTIPLIER
)

logger = logging.getLogger(__name__)


# ============================================================
#  ابزارهای پایه
# ============================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """محاسبه ATR (میانگین دامنه واقعی)"""
    if len(df) < period + 1:
        return 0.0
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    v = tr.rolling(period).mean().iloc[-1]
    return float(v) if not pd.isna(v) else 0.0


def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> dict:
    """شناسایی Swing High و Swing Low"""
    highs, lows = [], []
    for i in range(left, len(df) - right):
        w_high = df["high"].iloc[i - left:i + right + 1]
        w_low = df["low"].iloc[i - left:i + right + 1]
        if df["high"].iloc[i] == w_high.max():
            highs.append((i, float(df["high"].iloc[i])))
        if df["low"].iloc[i] == w_low.min():
            lows.append((i, float(df["low"].iloc[i])))
    return {"highs": highs, "lows": lows}


# ============================================================
#  ساختار بازار
# ============================================================

def detect_structure(df: pd.DataFrame) -> dict:
    """تشخیص روند بر اساس ساختار HH/HL یا LL/LH"""
    swings = find_swings(df)
    highs, lows = swings["highs"], swings["lows"]

    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "UNCLEAR"}

    lh, ph = highs[-1][1], highs[-2][1]
    ll, pl = lows[-1][1], lows[-2][1]

    higher_high = lh > ph
    higher_low = ll > pl
    lower_high = lh < ph
    lower_low = ll < pl

    if higher_high and higher_low:
        trend = "BULLISH"
    elif lower_high and lower_low:
        trend = "BEARISH"
    elif higher_high or higher_low:
        trend = "BULLISH"
    elif lower_high or lower_low:
        trend = "BEARISH"
    else:
        trend = "RANGING"

    return {
        "trend": trend,
        "last_high": lh,
        "last_low": ll,
    }


# ============================================================
#  Order Block
# ============================================================

def find_order_blocks(df: pd.DataFrame, lookback: int = 80) -> dict:
    """شناسایی Order Block صعودی و نزولی"""
    if len(df) < lookback:
        lookback = len(df) - 1

    window = df.iloc[-lookback:]
    avg_body = (window["close"] - window["open"]).abs().mean()

    if avg_body == 0:
        return {"bullish": None, "bearish": None}

    bull = bear = None
    for i in range(len(window) - 3, 1, -1):
        cur = window.iloc[i]
        nxt = window.iloc[i + 1]
        body = abs(nxt["close"] - nxt["open"])

        # Bullish OB
        if (cur["close"] < cur["open"]
                and nxt["close"] > cur["high"]
                and body > avg_body * 1.5
                and bull is None):
            bull = {
                "high": float(cur["high"]),
                "low": float(cur["low"]),
                "mid": float((cur["high"] + cur["low"]) / 2),
            }

        # Bearish OB
        if (cur["close"] > cur["open"]
                and nxt["close"] < cur["low"]
                and body > avg_body * 1.5
                and bear is None):
            bear = {
                "high": float(cur["high"]),
                "low": float(cur["low"]),
                "mid": float((cur["high"] + cur["low"]) / 2),
            }

        if bull and bear:
            break

    return {"bullish": bull, "bearish": bear}


# ============================================================
#  Fair Value Gap
# ============================================================

def detect_fvg(df: pd.DataFrame, lookback: int = 15) -> dict | None:
    """شناسایی FVG پرنشده"""
    if len(df) < 5:
        return None

    price = float(df["close"].iloc[-1])
    start = max(2, len(df) - lookback)

    for i in range(len(df) - 1, start, -1):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        # Bullish FVG
        if c3["low"] > c1["high"]:
            top, bottom = float(c3["low"]), float(c1["high"])
            if price > bottom:
                return {"type": "BULLISH", "top": top, "bottom": bottom}

        # Bearish FVG
        if c3["high"] < c1["low"]:
            top, bottom = float(c1["low"]), float(c3["high"])
            if price < top:
                return {"type": "BEARISH", "top": top, "bottom": bottom}

    return None


# ============================================================
#  Premium / Discount Zone
# ============================================================

def get_premium_discount(df: pd.DataFrame, lookback: int = 60) -> dict:
    """ناحیه Premium (گرون) یا Discount (ارزون)"""
    window = df.iloc[-lookback:]
    swing_high = float(window["high"].max())
    swing_low = float(window["low"].min())
    eq = (swing_high + swing_low) / 2
    price = float(df["close"].iloc[-1])

    zone = "PREMIUM" if price > eq else "DISCOUNT"
    return {
        "zone": zone,
        "high": swing_high,
        "low": swing_low,
        "eq": eq,
    }


# ============================================================
#  تأییدیه کندلی
# ============================================================

def get_candle_confirmation(df: pd.DataFrame, direction: str) -> bool:
    """کندل rejection یا engulfing"""
    if len(df) < 3:
        return False

    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]

    if rng == 0:
        return False

    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    if direction == "BUY":
        pin = lower_wick > body * 1.5
        engulf = (last["close"] > last["open"]
                  and last["close"] > prev["high"]
                  and last["open"] < prev["close"])
        return pin or engulf

    if direction == "SELL":
        pin = upper_wick > body * 1.5
        engulf = (last["close"] < last["open"]
                  and last["close"] < prev["low"]
                  and last["open"] > prev["close"])
        return pin or engulf

    return False


# ============================================================
#  تحلیلگر اصلی
# ============================================================

def analyze_multi_timeframe(data_htf: list, data_ltf: list,
                             symbol: str = "") -> dict:
    """
    تحلیل کامل یک جفت‌ارز با ترکیب HTF + LTF

    Returns:
        dict: score, direction, sl, tp, trend, zone, reasons
    """
    try:
        df_htf = pd.DataFrame(data_htf)
        df_ltf = pd.DataFrame(data_ltf)

        required = {"open", "high", "low", "close"}
        if not required.issubset(df_htf.columns) or \
           not required.issubset(df_ltf.columns):
            return {"score": None, "error": "ستون‌های OHLC ناقص"}

        if len(df_htf) < MIN_CANDLES or len(df_ltf) < MIN_CANDLES:
            return {"score": None, "error": "داده کافی نیست"}

        price = float(df_ltf["close"].iloc[-1])
        atr = calculate_atr(df_ltf)

        if atr == 0:
            return {"score": None, "error": "ATR نامعتبر"}

        # ---- HTF ----
        htf_structure = detect_structure(df_htf)
        htf_trend = htf_structure["trend"]
        htf_obs = find_order_blocks(df_htf, lookback=80)
        htf_pd = get_premium_discount(df_htf, lookback=60)

        # ---- LTF ----
        ltf_fvg = detect_fvg(df_ltf, lookback=15)

        score = 0.0
        reasons = []
        direction = None

        # ---- BUY Setup ----
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

        # ---- SELL Setup ----
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

        # ---- SL/TP ----
        sl = tp = None
        if direction == "BUY":
            ob = htf_obs["bullish"]
            sl = round(ob["low"] - atr * SL_ATR_MULTIPLIER, 6)
            risk = price - sl
            tp = round(price + risk * RISK_REWARD, 6)
        elif direction == "SELL":
            ob = htf_obs["bearish"]
            sl = round(ob["high"] + atr * SL_ATR_MULTIPLIER, 6)
            risk = sl - price
            tp = round(price - risk * RISK_REWARD, 6)

        # ---- خلاصه ----
        if direction:
            summary = f"سیگنال {direction} | امتیاز {score:+.2f}"
        elif htf_trend == "BULLISH":
            summary = "روند صعودی HTF - انتظار ورود در Discount"
        elif htf_trend == "BEARISH":
            summary = "روند نزولی HTF - انتظار ورود در Premium"
        else:
            summary = "ساختار HTF نامشخص - بدون معامله"

        return {
            "score": round(score, 2),
            "direction": direction,
            "current_price": price,
            "suggested_sl": sl,
            "suggested_tp": tp,
            "htf_trend": htf_trend,
            "zone": htf_pd["zone"],
            "atr": round(atr, 6),
            "reasons": reasons,
            "summary": summary,
        }

    except Exception as e:
        logger.exception(f"خطا در تحلیل {symbol}")
        return {"score": None, "error": str(e)}
