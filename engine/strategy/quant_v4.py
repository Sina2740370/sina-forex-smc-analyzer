"""
Quant V4 — موتور ensemble: روند روزانه + دو تریگر + امتیاز confluence

طراحی بر پایه‌ی مؤلفه‌هایی که در منابع/بک‌تست‌ها شواهد دارند:
1) فیلتر روند روزانه (Donchian/تصادبی: هر دو سود در بازار رونددار می‌گیرند، نه رنج)
   - EMA20/50 روزانه + سنجش فاصله با ATR → رژیم UP/DOWN/CHOP
   - رژیم CHOP → هیچ معامله‌ای (بیشترین ضررِ استراتژی‌های شکست در رنج است)
2) فیلتر رژیم نوسان: صدک ATR روزانه باید در [20%, 90%] باشد
   (بازار مُرده = هیچ؛ پانیک = اسپرد و گپ)
3) تریگر A — شکست Donchian(20) روی H4 هم‌جهت با رژیم روزانه
   (الگوی Turtle/Donchian؛ فقط شکست‌های تأییدشده با بسته‌شدن)
4) تریگر B — Sweep نقدینگی هم‌جهت با روند (ICT: هانتِ High/Low دیروز و
   بازگشت به داخل → ادامه‌ی روند). «sweep با روند» نه «sweep ضد روند».
5) امتیاز confluence با گیت cfg.min_score (مقدار 0.72 تأییدشده با سوییپ ۴ ساله)
6) ریسک: SL بر پایه‌ی ATR/اکسترمم sweep؛ TP = RR از cfg (2R تأییدشده)

همه‌ی محاسبات روی دم‌های کوتاه تایم‌فریم‌ها (view.at) و سبک — مناسب اجرای هر 15 دقیقه.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ..core import smc
from ..core.config import EngineConfig
from .smc_topdown import Decision

# پارامترهای موتور (ثابت‌های v4؛ سوییپ‌پذیر بعداً)
EMA_FAST_D, EMA_SLOW_D = 20, 50
EMA_SLOPE_BARS = 10
ATR_D_LEN, ATR_D_PCT_WINDOW = 14, 120
VOL_PCT_LO, VOL_PCT_HI = 0.20, 0.90
VOL_PCT_COMFORT = (0.30, 0.75)
CHOP_ATR_FRAC = 0.35          # فاصله‌ی EMAها کمتر از این × ATR → رنج
DONCHIAN_H4 = 20
SESSION_UTC = (7, 17)          # لندن+نیویورک
SESSION_PRIME = [(7, 10), (12, 15)]   # اوپن لندن و اوپن نیویورک
SWEEP_LOOKBACK_H = 26         # کندل‌های 1h برای یافتن High/Low دیروز
SL_ATR1H_MULT = 1.2
SL_ATR4H_MIN = 0.5
BREAKOUT_PAD_ATR = 0.10       # عمق تأیید شکست نسبت به ATR 4h
SWEEP_DEPTH_ATR = 0.10        # حداقل عمق sweep نسبت به ATR 1h

W = {  # وزن ویژگی‌ها (جمع ≈ 1.0)
    "trend_align": 0.30,
    "h4_slope": 0.20,
    "session_prime": 0.15,
    "vol_comfort": 0.15,
    "trigger_quality": 0.20,
}


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int) -> float:
    return smc.atr(df, n)


def _regime_daily(df1d: pd.DataFrame) -> Optional[dict]:
    """رژیم روزانه: جهت روند + صدک نوسان. None = دیتای ناکافی."""
    if df1d is None or len(df1d) < EMA_SLOW_D + EMA_SLOPE_BARS + 2:
        return None
    c = df1d["close"].astype(float)
    ema_f = _ema(c, EMA_FAST_D).iloc[-1]
    ema_s = _ema(c, EMA_SLOW_D).iloc[-1]
    ema_s_prev = _ema(c, EMA_SLOW_D).iloc[-1 - EMA_SLOPE_BARS]
    atr_d = _atr(df1d, ATR_D_LEN)
    if atr_d <= 0:
        return None
    gap = ema_f - ema_s
    if abs(gap) < CHOP_ATR_FRAC * atr_d:
        regime = "CHOP"
    else:
        regime = "UP" if gap > 0 else "DOWN"
    # صدک ATR فعلی در پنجره‌ی بلند
    tr = pd.concat([
        df1d["high"] - df1d["low"],
        (df1d["high"] - c.shift()).abs(),
        (df1d["low"] - c.shift()).abs(),
    ], axis=1).max(axis=1).tail(ATR_D_PCT_WINDOW)
    atr_series = tr.rolling(ATR_D_LEN).mean().dropna()
    if len(atr_series) < 20:
        vol_pct = 0.5
    else:
        vol_pct = float((atr_series < atr_series.iloc[-1]).mean())
    slope_ok = (ema_s > ema_s_prev) if gap > 0 else (ema_s < ema_s_prev)
    return {"regime": regime, "atr_d": atr_d, "vol_pct": vol_pct,
            "slope_ok": bool(slope_ok), "gap": float(gap)}


def _session_quality(now: pd.Timestamp) -> tuple[bool, bool]:
    """(داخل سشن لندن/نیویورک، داخل سشن طلایی) — به وقت UTC."""
    t = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    h = t.hour
    in_session = SESSION_UTC[0] <= h < SESSION_UTC[1]
    prime = any(a <= h < b for a, b in SESSION_PRIME)
    return in_session, prime


def _trigger_donchian(df4h: pd.DataFrame, regime: str) -> Optional[dict]:
    """شکست Donchian(20) H4 با بسته‌شدن و پد تأیید؛ هم‌جهت با رژیم روزانه."""
    if df4h is None or len(df4h) < DONCHIAN_H4 + 2:
        return None
    cur = df4h.iloc[-1]
    prev = df4h.iloc[-1 - DONCHIAN_H4:-1]
    atr4 = _atr(df4h, 14)
    if atr4 <= 0:
        return None
    price = float(cur["close"])
    hi, lo = float(prev["high"].max()), float(prev["low"].min())
    side = 0
    if regime == "UP" and price > hi + BREAKOUT_PAD_ATR * atr4:
        side = 1
    elif regime == "DOWN" and price < lo - BREAKOUT_PAD_ATR * atr4:
        side = -1
    if side == 0:
        return None
    depth = (price - hi) if side > 0 else (lo - price)
    return {"side": side, "entry": price, "atr4": atr4,
            "depth": float(depth), "extreme": hi if side > 0 else lo,
            "kind": "breakout"}


def _rsi(c: pd.Series, n: int = 2) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _trigger_pullback(df15: pd.DataFrame, df1h: pd.DataFrame, regime: str) -> Optional[dict]:
    """
    تریگر B (v4.2) — ادامه‌ی روند بعد از پولبک:
    در روند UP: قیمت در ۳ کندل اخیر زیر EMA20(M15) رفته ولی کندل آخر دوباره
    بالای EMA بسته شده و صعودی است → ورود market با ادامه‌ی روند.
    (جایگزین sweep خام که در بک‌تست ۴ ساله E[r]=−0.18 داد — چاقوگیری می‌کرد.)
    """
    if df15 is None or len(df15) < 210:
        return None
    c = df15["close"].astype(float)
    # v4.3 — Mean-Reversion هم‌جهت با روند روزانه (سبک Connors RSI-2):
    # در روند UP: RSI(2)<15 و قیمت بالای EMA200(M15) → خرید افت کوتاه.
    # (پولبک ساده EMA20 در بک‌تست ۴ ساله E[r]=−0.15 داد — چون M15 mean-revert است،
    #  ورود باید **در جهت معکوس افت کوتاه** باشد نه ادامه‌ی آن.)
    ema200 = _ema(c, 200)
    rsi2 = float(_rsi(c, 2).iloc[-1])
    price, o15 = float(c.iloc[-1]), float(df15["open"].iloc[-1])
    side = 0
    if regime == "UP" and rsi2 < 15 and price > ema200.iloc[-1]:
        side = 1
    elif regime == "DOWN" and rsi2 > 85 and price < ema200.iloc[-1]:
        side = -1
    if side == 0:
        return None
    atr1 = _atr(df1h, 14) if df1h is not None and len(df1h) > 15 else 0.0
    atr4 = _atr(df15, 56) / 4.0 if len(df15) > 60 else 0.0   # تقریب ATR 4h از M15
    ext = float(df15["low"].iloc[-3:].min()) if side > 0 else float(df15["high"].iloc[-3:].max())
    return {"side": side, "entry": price, "atr1": atr1, "atr4": atr4,
            "extreme": ext, "depth": float(rsi2), "kind": "meanrev"}


def analyze_v4(data: dict, cfg: EngineConfig, now: pd.Timestamp) -> Decision:
    dec = Decision(config_hash=cfg.hash())
    df1d, df4h = data.get("1d"), data.get("4h")
    df1h, df15 = data.get("1h"), data.get("15m")

    reg = _regime_daily(df1d)
    if reg is None:
        dec.blocked = "insufficient daily data"
        return dec
    if reg["regime"] == "CHOP":
        dec.blocked = "chop regime"
        dec.features = {"regime": "CHOP", "vol_pct": round(reg["vol_pct"], 2)}
        return dec
    if not (VOL_PCT_LO <= reg["vol_pct"] <= VOL_PCT_HI):
        dec.blocked = f"vol regime out of band ({reg['vol_pct']:.2f})"
        return dec

    in_session, prime = _session_quality(now)
    if not in_session:
        dec.blocked = "outside London/NY session"
        dec.features = {"regime": reg["regime"], "vol_pct": round(reg["vol_pct"], 2)}
        return dec

    trig = _trigger_donchian(df4h, reg["regime"]) or \
        _trigger_pullback(df15, df1h, reg["regime"])
    if trig is None:
        dec.blocked = "no trigger"
        return dec

    # --- امتیاز confluence ---
    score = 0.0
    score += W["trend_align"]                      # رژیم روزانه کامل (شرط ورود)
    if reg["slope_ok"]:
        score += W["h4_slope"]
    if prime:
        score += W["session_prime"]
    if VOL_PCT_COMFORT[0] <= reg["vol_pct"] <= VOL_PCT_COMFORT[1]:
        score += W["vol_comfort"]
    depth_ref = trig["atr4"] if trig["kind"] == "breakout" else trig["atr1"]
    if depth_ref and trig["depth"] >= 0.10 * depth_ref:
        score += W["trigger_quality"]

    # --- ریسک ---
    side = trig["side"]
    entry = trig["entry"]
    if trig["kind"] == "meanrev":
        risk = max(SL_ATR1H_MULT * trig["atr1"], 0.4 * trig["atr4"])
    elif trig["kind"] == "sweep":
        structural = abs(entry - trig["extreme"])
        risk = max(SL_ATR1H_MULT * trig["atr1"], SL_ATR4H_MIN * trig["atr4"],
                   structural * 1.1)
    else:
        risk = max(SL_ATR1H_MULT * trig["atr1"] if trig.get("atr1") else 0.0,
                   1.5 * trig["atr4"])
    if risk <= 0:
        dec.blocked = "invalid risk"
        return dec
    sl = entry - side * risk
    rr = getattr(cfg, "risk_reward", 2.0)
    tp = entry + side * risk * rr

    dec.direction = "BUY" if side > 0 else "SELL"
    dec.entry = entry
    dec.sl = round(sl, 6)
    dec.tp = round(tp, 6)
    dec.score = round(score, 3)
    dec.grade = "A+" if score >= 0.95 else ("A" if score >= 0.80 else "B")
    dec.bias = "LONG" if reg["regime"] == "UP" else "SHORT"
    dec.features = {
        "trigger": trig["kind"], "regime": reg["regime"],
        "vol_pct": round(reg["vol_pct"], 2), "depth_atr": round(
            trig["depth"] / depth_ref, 2) if depth_ref else 0.0,
    }
    dec.reasons = [
        f"regime={reg['regime']}", f"trigger={trig['kind']}",
        f"session_prime={prime}", f"vol_pct={reg['vol_pct']:.2f}",
    ]
    if dec.score < cfg.min_score:
        dec.blocked = f"score {dec.score:.2f} < {cfg.min_score}"
        # نکته: direction پاک می‌شود تا runner وارد نشود
        dec.direction = None
    return dec
