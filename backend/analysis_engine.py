"""
موتور تحلیل Top-Down SMC/ICT - نسخه 5.1 — کیفیت‌محور
هم‌راستا با engine/strategy/smc_topdown.py v5.1 (Displacement quality + Quality Density)
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

from .config import (
    MIN_CANDLES, MIN_SIGNAL_SCORE, RISK_REWARD,
    SL_ATR_MULTIPLIER, SL_OB_BUFFER,
    WEIGHT_TECHNICAL, WEIGHT_FUNDAMENTAL, WEIGHT_NEWS,
    get_symbol_profile, SPREAD, PIP_SIZE,
)

logger = logging.getLogger(__name__)

MIN_RISK_ATR_RATIO = 0.25
MIN_RISK_SPREAD_MULT = 3.0


def _min_spread_risk(symbol) -> float:
    try:
        spread_pips = SPREAD.get(symbol)
        pip = PIP_SIZE.get(symbol)
        if not spread_pips or not pip:
            return 0.0
        return MIN_RISK_SPREAD_MULT * spread_pips * pip
    except Exception:
        return 0.0


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


def _drop_forming(df, minutes):
    if df is None or len(df) == 0:
        return df
    if "time" not in df.columns:
        return df
    try:
        last_time = pd.Timestamp(df["time"].iloc[-1])
        if last_time.tzinfo is None:
            last_time = last_time.tz_localize("UTC")
        now = datetime.now(timezone.utc)
        if (now - last_time).total_seconds() < minutes * 60:
            return df.iloc[:-1].copy()
    except Exception:
        pass
    return df


def calculate_super_quant_score(tech_score: float, fund_score: float, news_score: float,
                                mem_score: float, regime_label: str = "TRENDING_LOW_VOL") -> dict:
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
    abs_score = abs(final_score)
    num_reasons = len(confluence_reasons)
    if abs_score >= 0.70 and num_reasons >= 4:
        return "A+"
    if abs_score >= 0.50 and num_reasons >= 3:
        return "A"
    return "B"


def _directional_composite(
    tech_score: float,
    tech_direction: str,
    fundamental_score: float,
    news_score: float,
    min_score: float,
    technical_weight: float = WEIGHT_TECHNICAL,
    fundamental_weight: float = WEIGHT_FUNDAMENTAL,
    news_weight: float = WEIGHT_NEWS,
) -> tuple[float, float, str | None]:
    """Combine directional evidence without confusing quality with direction.

    The SMC engine's score measures setup strength; its explicit direction
    carries the BUY/SELL side. Fundamental and news scores are signed for the
    instrument. Market-memory performance is intentionally excluded because
    it is a strategy-quality signal, not bullish/bearish market evidence.
    """
    weights = (technical_weight, fundamental_weight, news_weight)
    values = (tech_score, fundamental_score, news_score, min_score)
    if (
        tech_direction not in ("BUY", "SELL")
        or any(not np.isfinite(float(value)) for value in values + weights)
        or any(float(weight) < 0 for weight in weights)
        or technical_weight <= 0
        or min_score < 0
    ):
        return 0.0, 0.0, None

    active_weight = float(sum(weights))
    if active_weight <= 0:
        return 0.0, 0.0, None

    side = 1.0 if tech_direction == "BUY" else -1.0
    signed_technical = side * abs(float(tech_score))
    weighted_score = (
        signed_technical * float(technical_weight)
        + float(fundamental_score) * float(fundamental_weight)
        + float(news_score) * float(news_weight)
    )
    final_score = weighted_score / active_weight

    # Preserve the SMC engine's calibrated minimum in technical-score units:
    # with neutral external evidence, the composite threshold is met exactly
    # when |technical_score| reaches min_score.
    threshold = float(min_score) * float(technical_weight) / active_weight
    direction = tech_direction if side * final_score >= threshold else None
    return round(final_score, 4), round(threshold, 4), direction


def analyze_technical_topdown(data_dict, symbol: str = None, now: datetime = None):
    from engine.strategy.smc_topdown import analyze as smc_analyze
    from engine.core.config import EngineConfig

    profile = get_symbol_profile(symbol) if symbol else {"min_score": MIN_SIGNAL_SCORE, "sl_atr_mult": SL_ATR_MULTIPLIER}

    m_map = {"1wk": 10080, "1d": 1440, "4h": 240, "1h": 60, "15m": 15, "5m": 5, "1m": 1}
    cleaned_data = {}
    for tf, df in data_dict.items():
        if df is not None:
            df_m = pd.DataFrame(df)
            cleaned_data[tf] = _drop_forming(df_m, m_map.get(tf, 15))

    df_15m = cleaned_data.get("15m")
    price = float(df_15m["close"].iloc[-1]) if (df_15m is not None and len(df_15m) > 0) else None
    atr = calculate_atr(df_15m) if (df_15m is not None and len(df_15m) > 0) else 0.0

    df_4h = cleaned_data.get("4h")
    pd_zone = get_premium_discount(df_4h, lookback=60) if (df_4h is not None and len(df_4h) >= 20) else {"zone": "-"}

    cfg = EngineConfig(min_score=profile["min_score"], sl_atr1h_mult=profile["sl_atr_mult"])
    now_dt = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_dt.tzinfo is None:
        now_dt = now_dt.tz_localize("UTC")

    dec = smc_analyze(cleaned_data, cfg, now_dt)

    blocked = dec.blocked is not None or dec.direction is None

    return {
        "score": round(dec.score, 2),
        "grade": getattr(dec, "grade", "-"),
        "power": getattr(dec, "power", 0.0),
        "quality": getattr(dec, "quality", 0.0),
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
        "features": getattr(dec, "features", {}),
    }


async def full_analysis(data_dict, symbol, news_safe=True,
                         news_reason="", news_sentiment=0.0):
    from .fundamental_analyzer import calculate_fundamental_score
    from .market_memory import calculate_memory_score

    tech = analyze_technical_topdown(data_dict, symbol=symbol)

    if tech.get("blocked"):
        return {
            "score": 0.0,
            "grade": "-",
            "power": 0.0,
            "quality": 0.0,
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

    if not news_safe:
        news_score = 0.0
        logger.info("%s: خبر ایمن نیست، news_score صفر شد", symbol)

    profile = get_symbol_profile(symbol)
    final_score, final_min, direction = _directional_composite(
        tech_score=tech["score"],
        tech_direction=tech["direction"],
        fundamental_score=fund["score"],
        news_score=news_score,
        min_score=profile["min_score"],
    )

    if direction and (tech["sl"] is None or tech["tp"] is None):
        direction = None

    logger.info(
        "%s: final_score=%.2f tech=%.2f fund=%.2f news=%.2f mem=%.2f direction=%s grade=%s power=%.1f quality=%.2f",
        symbol, final_score, tech["score"], fund["score"], news_score, mem["score"], direction,
        tech.get("grade", "-"), tech.get("power", 0), tech.get("quality", 0),
    )

    all_reasons = [f"📈 تکنیکال: {tech['score']:+.2f} (گرید {tech.get('grade', '-')})"]
    all_reasons.extend(tech["reasons"])

    if fund["reasons"]:
        all_reasons.append(f"💵 فاندامنتال: {fund['score']:+.2f}")
        all_reasons.extend(fund["reasons"])

    if news_reason:
        all_reasons.append(f"📰 {news_reason}")

    if mem["reasons"]:
        all_reasons.append(f"🧠 حافظه: {mem['score']:+.2f}")
        all_reasons.extend(mem["reasons"])

    grade = tech.get("grade") or classify_setup_grade(tech["score"], final_score, all_reasons) if direction else "-"

    return {
        "score": round(final_score, 2),
        "grade": grade,
        "power": tech.get("power", 0.0),
        "quality": tech.get("quality", 0.0),
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
        "summary": f"امتیاز کل: {final_score:+.2f} (ستاپ {grade} | قدرت {tech.get('power', 0):.1f} | کیفیت {tech.get('quality', 0):.2f})" if direction else "در انتظار",
        "features": tech.get("features", {}),
    }
