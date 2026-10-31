"""
استراتژی‌های پایهٔ از پیش ثبت‌شده (S1 Donchian H4، S2 Asia-range London breakout).
امضا و خروجی همان Decision مورد انتظار run_backtest.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..core import smc
from ..core.config import EngineConfig
from .smc_topdown import Decision


def _h4_just_closed(df4h: pd.DataFrame, now: pd.Timestamp) -> bool:
    """آیا now دقیقاً لحظه‌ی بسته‌شدن آخرین کندل H4 است؟"""
    if df4h is None or len(df4h) < 1:
        return False
    last_open = pd.Timestamp(df4h["time"].iloc[-1])
    if last_open.tzinfo is None:
        last_open = last_open.tz_localize("UTC")
    else:
        last_open = last_open.tz_convert("UTC")
    now_u = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    return abs((last_open + pd.Timedelta(hours=4)) - now_u) <= pd.Timedelta(seconds=1)


def analyze_s1(data: dict, cfg: EngineConfig, now: pd.Timestamp) -> Decision:
    """
    Donchian(20) روی H4 بسته‌شده.
    BUY: close > max(high 20 کندل قبلی)؛ SELL: close < min(low 20 کندل قبلی).
    SL = 2*ATR(14) H4؛ TP = 2*risk.
    """
    dec = Decision(config_hash=cfg.hash())
    df4h = data.get("4h")
    if df4h is None or len(df4h) < 22:
        dec.blocked = "insufficient data: 4h"
        return dec
    if not _h4_just_closed(df4h, now):
        return dec  # فقط روی بسته‌شدن H4

    prev = df4h.iloc[-21:-1]  # ۲۰ کندل قبل از کندل فعلی
    cur = df4h.iloc[-1]
    price = float(cur["close"])
    don_hi = float(prev["high"].max())
    don_lo = float(prev["low"].min())

    side = 0
    if price > don_hi:
        side = 1
    elif price < don_lo:
        side = -1
    else:
        return dec

    atr4 = smc.atr(df4h, 14)
    if atr4 <= 0:
        dec.blocked = "invalid ATR"
        return dec
    risk = 2.0 * atr4
    sl = price - side * risk
    tp = price + side * risk * 2.0  # RR=2

    dec.direction = "BUY" if side > 0 else "SELL"
    dec.entry = price
    dec.sl = round(sl, 6)
    dec.tp = round(tp, 6)
    dec.score = 1.0
    dec.features = {"donchian": 1.0, "atr4h": atr4}
    dec.reasons = [f"S1 Donchian break {'up' if side > 0 else 'down'}"]
    return dec


def analyze_s2(data: dict, cfg: EngineConfig, now: pd.Timestamp) -> Decision:
    """
    شکست رنج آسیا (00:00–07:00 UTC) در پنجره‌ی لندن 07:00–10:00 UTC روی M15.
    فقط اولین شکست روز؛ SL سمت مقابل رنج؛ فیلتر 0.3*ATR1h ≤ risk ≤ 1.5*ATR4h؛ RR=2.
    """
    dec = Decision(config_hash=cfg.hash())
    df15 = data.get("15m")
    df1h = data.get("1h")
    df4h = data.get("4h")
    if df15 is None or len(df15) < 50:
        dec.blocked = "insufficient data: 15m"
        return dec
    if df1h is None or len(df1h) < 20:
        dec.blocked = "insufficient data: 1h"
        return dec
    if df4h is None or len(df4h) < 20:
        dec.blocked = "insufficient data: 4h"
        return dec

    now_u = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    if not (7 <= now_u.hour < 10):
        return dec

    day0 = now_u.normalize()
    asia_start = day0
    asia_end = day0 + pd.Timedelta(hours=7)

    # M15 times may be tz-aware or naive UTC
    tcol = pd.to_datetime(df15["time"], utc=True)
    # کندل‌هایی که در بازه‌ی آسیا بسته‌شده‌اند: open در [00:00, 07:00)
    mask = (tcol >= asia_start) & (tcol < asia_end)
    asia = df15.loc[mask]
    if len(asia) < 4:
        dec.blocked = "no asia range"
        return dec
    asia_hi = float(asia["high"].max())
    asia_lo = float(asia["low"].min())
    if asia_hi <= asia_lo:
        dec.blocked = "flat asia range"
        return dec

    cur = df15.iloc[-1]
    price = float(cur["close"])
    prev_close = float(df15.iloc[-2]["close"]) if len(df15) >= 2 else price

    side = 0
    # اولین شکست: کندل قبلی هنوز داخل/زیر رنج، این کندل بیرون
    if price > asia_hi and prev_close <= asia_hi:
        side = 1
    elif price < asia_lo and prev_close >= asia_lo:
        side = -1
    else:
        return dec

    if side > 0:
        sl = asia_lo
    else:
        sl = asia_hi
    risk = abs(price - sl)
    atr1h = smc.atr(df1h, 14)
    atr4h = smc.atr(df4h, 14)
    if atr1h <= 0 or atr4h <= 0:
        dec.blocked = "invalid ATR"
        return dec
    if risk < 0.3 * atr1h:
        dec.blocked = "risk too small"
        return dec
    if risk > 1.5 * atr4h:
        dec.blocked = "risk too large"
        return dec

    tp = price + (1 if side > 0 else -1) * risk * 2.0
    dec.direction = "BUY" if side > 0 else "SELL"
    dec.entry = price
    dec.sl = round(sl, 6)
    dec.tp = round(tp, 6)
    dec.score = 1.0
    dec.features = {"asia_break": 1.0, "asia_hi": asia_hi, "asia_lo": asia_lo}
    dec.reasons = [f"S2 asia break {'up' if side > 0 else 'down'}"]
    return dec
