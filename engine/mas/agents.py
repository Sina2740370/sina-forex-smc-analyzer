"""
ایجنت‌های تحلیلی MAS — هر ایجنت فقط از دیتاست A1 تغذیه می‌شود و گزارش استاندارد می‌دهد.

A1 Data        → دیتاست تمیز + quality_score (زمینه، نه گزارش امتیازی)
A2 Regime      → رژیم بازار + قدرت + اطمینان
A3 Technical   → جهت، امتیاز، سطوح، ستاپ، شرایط تأیید/ابطال (+ کاندید entry/SL/TP)
A6 Liquidity   → سشن، نقدینگی، ریسک شکست کاذب، سوئیپ
"""
from __future__ import annotations

import pandas as pd

from ..data.providers import build_topdown
from .contracts import BUY, SELL, NEUTRAL, AgentReport, Reason
from .indicators import adx, atr, bollinger_width, ema, macd, rsi, swing_points

PARKINSON_ALERT = 0.5   # آستانه نسبی برای نوسان بالا (ATR/price)


# ---------------------------------------------------------------- A1: Data
def run_data_agent(df15: pd.DataFrame) -> dict:
    """پاک‌سازی و سنجش کیفیت داده. خروجی: {"df": df تمیز, "quality": 0..1, "warnings": []}"""
    warnings, quality = [], 1.0
    n0 = len(df15)
    df = df15.dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
    dup = n0 - len(df)
    if dup:
        warnings.append(f"{dup} ردیف تکراری حذف شد")
        quality -= min(0.05, 0.005 * dup)
    # گپ غیرمعمول: بیش از ۴.۵ روز (آخرهفته عادی فارکس ~۵۱ ساعت و تعطیلات بلند ~۷۵ ساعت
    # طبیعی‌اند و نباید کیفیت داده را جریمه کنند)
    gaps = df["time"].diff().dt.total_seconds()
    big = gaps[gaps > 4.5 * 86400]
    if len(big):
        warnings.append(f"{len(big)} گپ زمانی غیرمعمول (بیش از ۴.۵ روز) پیدا شد")
        quality -= min(0.20, 0.05 * len(big))
    if df["high"].lt(df["low"]).any() or df["close"].isna().any():
        warnings.append("کندل خراب (high<low یا close تهی)")
        quality -= 0.1
    # حجم صفر و غیبت اسپرد واقعی → پرچم (M15 داکا حجم تخمینی دارد)
    if (df["volume"] <= 0).mean() > 0.5:
        warnings.append("حجم غیرقابل‌اتکا (بیش از نصف کندل‌ها بدون حجم)")
    return {"df": df, "quality": max(0.0, min(1.0, quality)), "warnings": warnings}


# ---------------------------------------------------------------- A2: Regime
def run_regime_agent(top: dict, now: pd.Timestamp) -> AgentReport:
    """تشخیص رژیم: strong_trend | range | high_vol | low_vol (کم‌نوسان) + سشن."""
    d4, d1 = top["4h"].iloc[-120:], top["1d"].iloc[-60:]
    if len(d4) < 60 or len(d1) < 20:
        return AgentReport("regime", NEUTRAL, 50, 0,
                           missing_data=["تاریخچه کافی برای تشخیص رژیم"])

    px = float(d4["close"].iloc[-1])
    a = adx(d4)
    a14 = atr(d4) / px
    bw = bollinger_width(d4["close"])
    e20, e50, e200 = ema(d4["close"], 20).iloc[-1], ema(d4["close"], 50).iloc[-1], ema(d4["close"], 200).iloc[-1]
    hh, _ = swing_points(d4)
    _, ll = swing_points(d4)
    # ساختار سقف/کف: آخرین دو سقف و کف
    up_struct = len(hh) >= 2 and d4["high"].iloc[hh[-1]] > d4["high"].iloc[hh[-2]] \
        and len(ll) >= 2 and d4["low"].iloc[ll[-1]] > d4["low"].iloc[ll[-2]]
    dn_struct = len(hh) >= 2 and d4["high"].iloc[hh[-1]] < d4["high"].iloc[hh[-2]] \
        and len(ll) >= 2 and d4["low"].iloc[ll[-1]] < d4["low"].iloc[ll[-2]]

    t = now.tz_convert("Europe/London") if now.tzinfo else now.tz_localize("UTC").tz_convert("Europe/London")
    session = "London" if 7 <= t.hour < 16 else ("NewYork" if 12 <= t.hour < 21 else "Asia")

    reasons, missing = [], []
    if a14 > PARKINSON_ALERT or bw > 0.06:
        regime, strength, conf = "high_vol", 60 + min(30, 100 * a14), min(80, 50 + 5 * a)
        reasons.append(Reason("R-HV", "نوسان بالاتر از حد عادی (ATR/price و عرض بولینگر)",
                              f"ATR4h/px={a14:.4f}, BBwidth={bw:.3f}"))
    elif a >= 25 and (up_struct or dn_struct):
        regime = "strong_trend"
        strength = min(100, a * 2)
        conf = min(85, 45 + a / 2)
        reasons.append(Reason("R-TR", "ADX بالای ۲۵ + ساختار سقف/کف هم‌جهت",
                              f"ADX={a:.1f}, EMA20>{'' if e20 > e50 else '<'}EMA50, struct={'up' if up_struct else 'down'}"))
    elif a < 18:
        regime, strength, conf = "range", max(0, 60 - a * 2), 55
        reasons.append(Reason("R-RG", "ADX کم؛ بازار بی‌روند/رنج", f"ADX={a:.1f}"))
    else:
        regime, strength, conf = "transition", 50, 35
        reasons.append(Reason("R-TS", "رژیم میانی؛ نه روند قطعی نه رنج واضح", f"ADX={a:.1f}"))

    inv = ["تغییر رژیم حین معامله (شکست ساختار مخالف با حجم) ابطال می‌کند"]
    # در روند واضح رژیم هم‌جهت رأی می‌دهد؛ در بقیه رژیم‌ها خنثی می‌ماند
    direction, rscore = NEUTRAL, 50.0
    if regime == "strong_trend" and up_struct and e20 > e50:
        direction, rscore = BUY, 50 + min(30.0, strength / 3)
    elif regime == "strong_trend" and dn_struct and e20 < e50:
        direction, rscore = SELL, 50 - min(30.0, strength / 3)
    return AgentReport("regime", direction, rscore, conf, reasons, inv, missing,
                       context={"regime": regime, "strength": round(strength, 1),
                                "session": session, "adx": round(a, 1), "atr_ratio": round(a14, 5),
                                "session_ok": session in ("London", "NewYork")})


# ---------------------------------------------------------------- A3: Technical
def _trend_dir(df: pd.DataFrame) -> int:
    """+1 روند صعودی (EMA20>EMA50>EMA200)، -1 نزولی، 0 نامشخص."""
    c = df["close"]
    if len(df) < 200:
        e20, e50 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1]
        return 1 if e20 > e50 else (-1 if e20 < e50 else 0)
    e20, e50, e200 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
    if e20 > e50 > e200:
        return 1
    if e20 < e50 < e200:
        return -1
    return 0


def _fib_zone(d1: pd.DataFrame) -> dict:
    """فیبوناچی فقط روی آخرین سوئینگ واضح (پولبک معتبر). سطوح 0.382/0.5/0.618/0.786."""
    hh, ll = swing_points(d1, 3, 3)
    if len(hh) < 2 or len(ll) < 2:
        return {}
    i2, i1 = hh[-2], hh[-1]
    if d1["high"].iloc[i1] > d1["high"].iloc[i2] and d1["low"].iloc[ll[-1]] > d1["low"].iloc[ll[-2]]:
        lo, hi = float(d1["low"].iloc[ll[-1]]), float(d1["high"].iloc[i1])
        ext = "down"   # روند صعودی؛ فیبو از کف تا سقف
    elif d1["low"].iloc[ll[-1]] < d1["low"].iloc[ll[-2]] and d1["high"].iloc[hh[-1]] < d1["high"].iloc[hh[-2]]:
        lo, hi = float(d1["high"].iloc[i1]), float(d1["low"].iloc[ll[-1]])
        ext = "up"     # روند نزولی
    else:
        return {}
    rng = hi - lo
    levels = {f"{p}": lo + rng * p if ext == "down" else hi - rng * p
              for p in (0.382, 0.5, 0.618, 0.786)}
    return {"swing_low": lo, "swing_high": hi, "trend": "up" if ext == "down" else "down",
            "levels": levels, "golden": levels["0.618"]}


def run_technical_agent(top: dict, now: pd.Timestamp, cfg_atr15: float = 0.0) -> AgentReport:
    """تحلیل تکنیکال چندتایم‌فریم + کاندید معامله (entry/SL/TP2 با RR≥2)."""
    m15, h1, d1 = top["15m"], top["1h"], top["1d"]
    px = float(m15["close"].iloc[-1])
    reasons, missing = [], []
    score = 50.0
    conf = 0.0

    t_h4, t_h1, t_d1 = _trend_dir(d4 := top["4h"]), _trend_dir(h1), _trend_dir(d1)
    if t_d1 != 0:
        score += 12 * t_d1
        conf += 15
        reasons.append(Reason("T-D1", f"روند D1 {'صعودی' if t_d1 > 0 else 'نزولی'} (EMA 20/50/200)",
                              f"D1 close={px:.5f}, EMA-align={'bull' if t_d1 > 0 else 'bear'}"))
    else:
        reasons.append(Reason("T-D1x", "روند D1 نامشخص (EMA هم‌تراز)", "-"))
    if t_h4 == t_d1 != 0:
        score += 8 * t_h4
        conf += 15
        reasons.append(Reason("T-H4", "تأیید روند در H4", f"H4 EMA-align={'bull' if t_h4 > 0 else 'bear'}"))
    if t_h1 == t_d1 != 0:
        conf += 10

    # مومنتوم: RSI و MACD روی H1
    r = rsi(h1["close"])
    _, _, hist = macd(h1["close"])
    bull_momo = (r > 50 and hist > 0) or (40 < r < 65)
    bear_momo = (r < 50 and hist < 0) or (35 < r < 60)
    if bull_momo and t_d1 >= 0:
        score += 6
        reasons.append(Reason("T-MM", "مومنتوم H1 هم‌جهت صعودی", f"RSI={r:.1f}, MACDhist={hist:.6f}"))
    if bear_momo and t_d1 <= 0:
        score -= 6
        reasons.append(Reason("T-MM", "مومنتوم H1 هم‌جهت نزولی", f"RSI={r:.1f}, MACDhist={hist:.6f}"))

    # فیبوناچی: فقط در پولبکِ روند واضح؛ بدون تأیید مومنتوم/ساختار → نه امتیاز، فقط WAIT
    fib = _fib_zone(d1)
    at_fib = None
    if fib and fib["trend"] == ("up" if t_d1 >= 0 else "down"):
        levels = fib["levels"]
        band = max(0.0005, 1.5 * atr(h1))
        for k, v in levels.items():
            if abs(px - v) <= band:
                at_fib = (k, v)
                break
        if at_fib:
            momo_ok = (t_d1 >= 0 and r > 45 and hist > -1e-9) or (t_d1 <= 0 and r < 55)
            reasons.append(Reason("T-FB", f"قیمت در ناحیه فیبو {at_fib[0]} از سوئینگ {fib['trend']}",
                                  f"level={at_fib[1]:.5f}, px={px:.5f}"))
            if momo_ok:
                score += 8 if t_d1 >= 0 else -8
                reasons.append(Reason("T-FBv", "پولبک فیبو با تأیید مومنتوم", f"RSI={r:.1f}"))
            else:
                reasons.append(Reason("T-FBi", "ناحیه فیبو بدون تأیید مومنتوم → مجوز ورود نیست؛ WAIT",
                                      f"RSI={r:.1f}, MACDhist={hist:.6f}"))
                score = 50 + (score - 50) * 0.3   # امتیاز جهت‌دار خنثی‌سازی می‌شود

    a15 = atr(m15) if cfg_atr15 == 0 else cfg_atr15
    a1h = atr(h1)
    direction = BUY if score >= 60 else (SELL if score <= 40 else NEUTRAL)
    conf = min(85.0, conf + min(20, abs(score - 50)))

    # کاندید معامله (فقط وقتی جهت داریم) — SL پشت ساختار یا ≥1.5×ATR(H1)
    cand = {}
    if direction != NEUTRAL:
        sign = 1 if direction == BUY else -1
        entry = px
        sl_struct = (m15["low"].tail(12).min() - 0.1 * a1h) if sign > 0 else (m15["high"].tail(12).max() + 0.1 * a1h)
        sl = sl_struct if sign * (entry - sl_struct) > 1.5 * a1h else entry - sign * 1.6 * a1h
        risk = abs(entry - sl)
        tp1, tp2 = entry + sign * 1.5 * risk, entry + sign * 2.2 * risk
        if risk > 0:
            cand = {"direction": direction, "entry": float(entry), "sl": float(sl),
                    "tp1": float(tp1), "tp2": float(tp2),
                    "rr": float(abs(tp2 - entry) / risk), "risk_atr1h": float(risk / a1h)}
    return AgentReport("technical", direction, min(100, max(0, score)), conf, reasons,
                       [f"بسته‌شدن H1 زیر/بالای {'کف' if direction == BUY else 'سقف'} ساختار {'12 کندل' if direction != NEUTRAL else '-'} ابطال می‌کند",
                        "واگرایی منفی/مثبت RSI در H1 هم‌زمان با توقف روند ابطال می‌کند"],
                       missing, context={"candidate": cand, "fib": fib,
                                         "levels": {"px": px, "atr1h": a1h},
                                         "rsi_h1": round(r, 1)})


# ---------------------------------------------------------------- A6: Liquidity & Session
def run_liquidity_agent(top: dict, now: pd.Timestamp, spread_price: float = 0.0) -> AgentReport:
    """سشن، نواحی نقدینگی (equal highs/lows)، سوئیپ اخیر، اسپرد."""
    m15, h1 = top["15m"], top["1h"]
    px = float(m15["close"].iloc[-1])
    reasons, missing = [], []
    score, conf = 50.0, 0.0
    a1h = atr(h1)

    t = now.tz_convert("Europe/London") if now.tzinfo else now.tz_localize("UTC").tz_convert("Europe/London")
    hour = t.hour
    in_ldn, in_ny = 7 <= hour < 16, 12 <= hour < 21
    if in_ldn or in_ny:
        score += 5
        conf += 25
        reasons.append(Reason("L-SS", "سشن پرنقدینگی (لندن/نیویورک)",
                              f"London={in_ldn}, NewYork={in_ny} (UTC+London wall clock)"))
    else:
        score -= 5
        reasons.append(Reason("L-SSx", "سشن کم‌نقدینگی (آسیا) — ریسک شکست کاذب بالاتر",
                              f"hour_london={hour}"))

    # سوئیپ اخیر: نفوذ به سوئینگ 96 کندل اخیر (۲۴ ساعت) و بازگشت
    look = m15.tail(96)
    hh, ll = swing_points(look, 3, 3)
    sweep_dir, swept_level = None, None
    if hh and look["high"].iloc[-3:].max() > look["high"].iloc[hh[-1]] and px < look["high"].iloc[hh[-1]]:
        sweep_dir, swept_level = "bear_sweep", float(look["high"].iloc[hh[-1]])   # استاپ‌هانت بالای سقف → فشار فروش
    if ll and look["low"].iloc[-3:].min() < look["low"].iloc[ll[-1]] and px > look["low"].iloc[ll[-1]]:
        sweep_dir, swept_level = "bull_sweep", float(look["low"].iloc[ll[-1]])    # استاپ‌هانت زیر کف → فشار خرید
    if sweep_dir:
        score += 6 if sweep_dir == "bull_sweep" else -6
        conf += 15
        reasons.append(Reason("L-SW", f"سوئیپ نقدینگی {'پایین' if sweep_dir == 'bull_sweep' else 'بالا'} در ۲۴ ساعت اخیر",
                              f"level={swept_level:.5f}, px={px:.5f}"))

    # اسپرد: اگر داده واقعی نبود، تخمین از ATR (و پرچم missing)
    spread = spread_price if spread_price > 0 else max(0.0001, a1h * 0.02)
    if spread_price <= 0:
        missing.append("اسپرد زنده — تخمین از ATR H1 (×0.02)")
    spread_ratio = spread / px
    if spread_ratio > 0.0004:
        score = 50 + (score - 50) * 0.5
        conf = max(0, conf - 15)
        reasons.append(Reason("L-SP", "اسپرد پهن نسبت به قیمت — اجرا پرهزینه", f"spread_ratio={spread_ratio:.5f}"))

    direction = BUY if score >= 58 else (SELL if score <= 42 else NEUTRAL)
    return AgentReport("liquidity", direction, min(100, max(0, score)), min(60, conf), reasons,
                       ["شکست مجدد سطح سوئیپ‌شده در جهت مخالف ابطال می‌کند"], missing,
                       context={"sweep": sweep_dir or "", "swept_level": swept_level,
                                "session_ok": in_ldn or in_ny, "spread_price": spread})
