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

from engine.core.config import EngineConfig
from engine.data.providers import TopDownView
from engine.strategy.smc_topdown import analyze as analyze_smc_engine

from .config import (
    MAX_ENTRY_SLIP_R, WEIGHT_TECHNICAL, WEIGHT_FUNDAMENTAL,
    WEIGHT_NEWS, WEIGHT_MEMORY,
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
    Adapter لایو به موتور SMC مشترک با Backtest/Paper.
    ورودی همچنان قرارداد قدیمی backend است، اما منطق تصمیم‌گیری فقط از
    engine.strategy.smc_topdown.analyze می‌آید؛ داده‌ی در حال تشکیل حذف می‌شود.
    """
    base = {
        "score": 0.0, "direction": None, "price": None, "sl": None, "tp": None,
        "trend": "-", "wk_trend": "-", "d_trend": "-", "zone": "-",
        "atr": 0.0, "reasons": [], "blocked": True, "config_hash": None,
    }
    try:
        frames = {}
        for tf in ("1wk", "1d", "4h", "1h", "15m"):
            df = pd.DataFrame(data_dict.get(tf, []))
            if df.empty or "time" not in df.columns:
                base["reasons"] = [f"داده کافی نیست: {tf}"]
                return base
            df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
            for col in ("open", "high", "low", "close"):
                if col not in df.columns:
                    base["reasons"] = [f"ستون {col} در {tf} وجود ندارد"]
                    return base
                df[col] = pd.to_numeric(df[col], errors="coerce")
            if "volume" not in df.columns:
                df["volume"] = 0.0
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
            frames[tf] = (
                df.dropna(subset=["time", "open", "high", "low", "close"])
                .drop_duplicates("time").sort_values("time").reset_index(drop=True)
            )

        now = pd.Timestamp.now(tz="UTC")
        closed = TopDownView(frames).at(now)
        decision = analyze_smc_engine(
            closed, EngineConfig(max_entry_slip_r=MAX_ENTRY_SLIP_R), now
        )
        base["config_hash"] = getattr(decision, "config_hash", None)
        df15 = closed.get("15m", pd.DataFrame())
        price = float(df15["close"].iloc[-1]) if not df15.empty else None
        atr = calculate_atr(df15) if len(df15) >= 15 else 0.0
        zone = next(
            (r for r in decision.reasons if r in ("PREMIUM", "DISCOUNT")), "-"
        )
        base.update({
            "price": decision.entry if decision.entry is not None else price,
            "sl": decision.sl,
            "tp": decision.tp,
            "trend": decision.bias,
            "wk_trend": decision.bias,
            "d_trend": decision.bias,
            "zone": zone,
            "atr": round(atr, 6) if atr else 0.0,
            "reasons": list(decision.reasons),
        })
        if decision.direction not in ("BUY", "SELL"):
            base["reasons"].append(f"مسدود توسط موتور مشترک: {decision.blocked or 'بدون جهت معتبر'}")
            return base

        # جهت و سطوح ورود/خروج همگی از یک Decision مشترک می‌آیند.
        signed_score = abs(float(decision.score))
        base.update({
            "score": signed_score if decision.direction == "BUY" else -signed_score,
            "direction": decision.direction,
            "price": decision.entry if decision.entry is not None else price,
            "blocked": False,
        })
        return base
    except Exception:
        logger.exception("خطا در adapter موتور SMC مشترک")
        base["reasons"] = ["خطا در آماده‌سازی داده برای موتور SMC مشترک"]
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
            "config_hash": tech.get("config_hash"),
            "decision_source": "shared_smc_engine",
            "news_safe": bool(news_safe),
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

    # تصمیم، Entry، SL و TP از موتور مشترک SMC می‌آیند.
    # امتیازهای فاندامنتال/خبر/حافظه گزارش تشخیصی‌اند و اجازه ندارند
    # جهت موتور را عوض کنند؛ خبر ناایمن همچنان یک veto مستقل است.
    direction = tech.get("direction")
    if not news_safe:
        direction = None
        logger.info("%s: خبر ایمن نیست؛ سیگنال مشترک مسدود شد", symbol)
    if direction and (tech.get("sl") is None or tech.get("tp") is None):
        direction = None

    logger.info(
        "%s: core_score=%.2f context_score=%.2f fund=%.2f news=%.2f mem=%.2f direction=%s",
        symbol, tech["score"], final_score, fund["score"], news_score, mem["score"], direction,
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
        "score": round(float(tech["score"]), 4),
        "technical_score": tech["score"],
        "context_score": round(final_score, 4),
        "fundamental_score": fund["score"],
        "news_score": news_score,
        "memory_score": mem["score"],
        "decision_source": "shared_smc_engine",
        "config_hash": tech.get("config_hash"),
        "news_safe": bool(news_safe),
        "direction": direction,
        "current_price": tech["price"],
        "suggested_sl": tech["sl"],
        "suggested_tp": tech["tp"],
        "htf_trend": tech["trend"],
        "zone": tech["zone"],
        "atr": tech["atr"],
        "reasons": all_reasons,
        "summary": f"سیگنال هسته: {direction} | امتیاز زمینه: {final_score:+.2f}" if direction else "در انتظار",
    }
