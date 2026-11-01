"""اندیکاتورهای پایه MAS — قطعی و بدون وابستگی خارجی."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    tr = tr.ewm(alpha=1 / n, adjust=False).mean()
    return float(tr.iloc[-1])


def rsi(c: pd.Series, n: int = 14) -> float:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def macd(c: pd.Series, fast=12, slow=26, sig=9):
    line = ema(c, fast) - ema(c, slow)
    s = line.ewm(span=sig, adjust=False).mean()
    return float(line.iloc[-1]), float(s.iloc[-1]), float((line - s).iloc[-1])


def adx(df: pd.DataFrame, n: int = 14) -> float:
    """ADX کلاسیک (Wilder) — سنجش قدرت روند، بدون جهت."""
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return float(dx.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def bollinger_width(c: pd.Series, n: int = 20, k: float = 2.0) -> float:
    m, sd = c.rolling(n).mean(), c.rolling(n).std()
    return float(((k * sd) / m).iloc[-1])


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3):
    """سقف/کف سوئینگ ساده (fractal). خروجی: (highs, lows) ایندکس‌ها."""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    for i in range(left, len(df) - right):
        if h[i] == max(h[i - left:i + right + 1]):
            highs.append(i)
        if l[i] == min(l[i - left:i + right + 1]):
            lows.append(i)
    return highs, lows
