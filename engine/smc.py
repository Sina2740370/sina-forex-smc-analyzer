"""
primitiveهای SMC/ICT: همه توابع خالص‌اند (ورودی DataFrame → خروجی ساده)، بدون I/O و بدون ساعت سیستم.
DataFrame ستون‌های time, open, high, low, close دارد و فقط شامل کندل‌های «بسته‌شده» است.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


# ---------------------------------------------------------------- اندیکاتور
def atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    h = df["high"].to_numpy()[-(period + 1):]
    l = df["low"].to_numpy()[-(period + 1):]
    c = df["close"].to_numpy()[-(period + 1):]
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    v = float(tr.mean())
    return v if np.isfinite(v) else 0.0


def ema_slope(df: pd.DataFrame, period: int = 10) -> str:
    if len(df) < period + 3:
        return "UNCLEAR"
    e = df["close"].ewm(span=period, adjust=False).mean()
    if e.iloc[-1] > e.iloc[-3]:
        return "BULLISH"
    if e.iloc[-1] < e.iloc[-3]:
        return "BEARISH"
    return "RANGING"


# ---------------------------------------------------------------- ساختار
def swings(df: pd.DataFrame, left: int = 3, right: int = 3):
    """سوئینگ‌ها: لیست (index, price). یک سوئینگ تا `right` کندل بعدش تأیید نمی‌شود."""
    w = left + right + 1
    if len(df) < w:
        return [], []
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    mx = sliding_window_view(hi, w).max(axis=1)
    mn = sliding_window_view(lo, w).min(axis=1)
    ih = np.flatnonzero(hi[left:left + len(mx)] == mx) + left
    il = np.flatnonzero(lo[left:left + len(mn)] == mn) + left
    return ([(int(i), float(hi[i])) for i in ih],
            [(int(i), float(lo[i])) for i in il])


def structure(df: pd.DataFrame) -> str:
    """BULLISH فقط با HH+HL، BEARISH فقط با LH+LL، وگرنه RANGING."""
    highs, lows = swings(df, 3, 3)
    if len(highs) < 2 or len(lows) < 2:
        return "UNCLEAR"
    hh, ll_ = highs[-1][1] > highs[-2][1], lows[-1][1] < lows[-2][1]
    hl, lh = lows[-1][1] > lows[-2][1], highs[-1][1] < highs[-2][1]
    if hh and hl:
        return "BULLISH"
    if lh and ll_:
        return "BEARISH"
    return "RANGING"


def structure_break(df: pd.DataFrame, lookback: int = 40, fresh_bars: int = 3):
    """
    شکست تازه‌ی سوئینگ + تفکیک واقعی CHOCH از BOS:
      CHOCH = شکست خلاف ساختار قبلی (مثلاً ساختار نزولی بود و سقف آخر شکسته شد)
      BOS   = شکست هم‌جهت با ساختار قبلی
    «تازه» یعنی یکی از `fresh_bars` کلوز قبلی هنوز آن‌طرف سطح نبوده.
    """
    w = df.tail(lookback).reset_index(drop=True)
    highs, lows = swings(w, 2, 2)
    if len(highs) < 2 or len(lows) < 2 or len(w) < 6:
        return None
    h1, h0 = highs[-1][1], highs[-2][1]
    l1, l0 = lows[-1][1], lows[-2][1]
    closes = w["close"].to_numpy()
    last, prev = closes[-1], closes[-1 - fresh_bars:-1]
    prior_bear = h1 < h0 and l1 < l0
    prior_bull = h1 > h0 and l1 > l0
    if last > h1 and (prev <= h1).any():
        return {"kind": "CHOCH" if prior_bear else "BOS", "dir": "BULL", "level": h1}
    if last < l1 and (prev >= l1).any():
        return {"kind": "CHOCH" if prior_bull else "BOS", "dir": "BEAR", "level": l1}
    return None


# ---------------------------------------------------------------- نواحی
def order_blocks(df: pd.DataFrame, lookback: int = 60):
    """آخرین OB معتبر (تخفیف‌نخورده با کلوز) صعودی/نزولی."""
    w = df.tail(lookback + 2).reset_index(drop=True)
    o, h, l, c = (w[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(w)
    out = {"bullish": None, "bearish": None}
    if n < 5:
        return out
    avg_body = float(np.mean(np.abs(c - o)))
    if avg_body == 0:
        return out
    for i in range(n - 3, -1, -1):
        if abs(c[i + 1] - o[i + 1]) <= avg_body * 1.5:
            continue
        later = c[i + 2:]
        if out["bullish"] is None and c[i] < o[i] and c[i + 1] > h[i] \
                and not (later < l[i]).any():
            out["bullish"] = {"high": float(h[i]), "low": float(l[i])}
        if out["bearish"] is None and c[i] > o[i] and c[i + 1] < l[i] \
                and not (later > h[i]).any():
            out["bearish"] = {"high": float(h[i]), "low": float(l[i])}
        if out["bullish"] and out["bearish"]:
            break
    return out


def ob_distance(price: float, ob: dict, atr_ref: float) -> float:
    """فاصله تا OB بر حسب ATR مرجع (ATR هم‌تایم‌فریم OB، نه 15m)."""
    if ob["low"] <= price <= ob["high"]:
        return 0.0
    gap = ob["low"] - price if price < ob["low"] else price - ob["high"]
    return gap / atr_ref if atr_ref > 0 else 999.0


def fvgs(df: pd.DataFrame, lookback: int = 15, min_gap_ratio: float = 0.15):
    """آخرین FVG تخفیف‌نخورده‌ی هر نوع؛ دیگر یکی نوع دیگری را نمی‌پوشاند."""
    out = {"BULLISH": None, "BEARISH": None}
    n = len(df)
    if n < 5:
        return out
    h, l, c = (df[k].to_numpy() for k in ("high", "low", "close"))
    min_gap = float(np.mean(h[-(lookback + 3):] - l[-(lookback + 3):])) * min_gap_ratio
    for i in range(n - 1, max(2, n - lookback) - 1, -1):
        if out["BULLISH"] is None and l[i] > h[i - 2] and (l[i] - h[i - 2]) >= min_gap \
                and not (c[i + 1:] < h[i - 2]).any():
            out["BULLISH"] = {"top": float(l[i]), "bottom": float(h[i - 2])}
        if out["BEARISH"] is None and h[i] < l[i - 2] and (l[i - 2] - h[i]) >= min_gap \
                and not (c[i + 1:] > l[i - 2]).any():
            out["BEARISH"] = {"top": float(l[i - 2]), "bottom": float(h[i])}
    return out


def premium_discount(df: pd.DataFrame, lookback: int = 60) -> str:
    w = df.tail(lookback)
    eq = (float(w["high"].max()) + float(w["low"].min())) / 2
    return "PREMIUM" if float(df["close"].iloc[-1]) > eq else "DISCOUNT"


# ---------------------------------------------------------------- نقدینگی و کندل
def liquidity_sweep(df: pd.DataFrame, lookback: int = 96, recent: int = 3):
    """جاروی سقف/کف قبلی توسط فتیله در `recent` کندل آخر؛ extreme = انتهای فتیله."""
    for name, value in (("lookback", lookback), ("recent", recent)):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < 1:
            raise ValueError(f"{name} باید عدد صحیح مثبت باشد")
    if recent >= lookback:
        raise ValueError("recent باید از lookback کوچک‌تر باشد")
    if len(df) < lookback + recent:
        return None
    ref = df.iloc[-lookback:-recent]
    prev_high, prev_low = float(ref["high"].max()), float(ref["low"].min())
    for i in range(-recent, 0):
        c = df.iloc[i]
        if c["high"] > prev_high and c["close"] < prev_high:
            return {"type": "BEARISH_SWEEP", "level": prev_high,
                    "size": float(c["high"] - prev_high), "extreme": float(c["high"])}
        if c["low"] < prev_low and c["close"] > prev_low:
            return {"type": "BULLISH_SWEEP", "level": prev_low,
                    "size": float(prev_low - c["low"]), "extreme": float(c["low"])}
    return None


def sweep_multiplier(sweep, atr_ref: float) -> float:
    if not sweep or not atr_ref:
        return 1.0
    return max(0.5, min(1.6, 0.5 + 0.7 * sweep["size"] / atr_ref))


def candle_confirmation(df: pd.DataFrame, side: int) -> bool:
    if len(df) < 3:
        return False
    a, p = df.iloc[-1], df.iloc[-2]
    body = abs(a["close"] - a["open"])
    if a["high"] - a["low"] == 0:
        return False
    uw = a["high"] - max(a["close"], a["open"])
    lw = min(a["close"], a["open"]) - a["low"]
    if side > 0:
        return bool(lw > body * 1.5 or (a["close"] > a["open"] and a["close"] > p["high"]
                                         and a["open"] < p["close"]))
    return bool(uw > body * 1.5 or (a["close"] < a["open"] and a["close"] < p["low"]
                                     and a["open"] > p["close"]))
