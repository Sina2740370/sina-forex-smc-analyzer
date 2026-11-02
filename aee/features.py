"""پایپ‌لاین ویژگی پایهٔ فاز ۰ — همهٔ ویژگی‌ها اکیداً عقب‌نگر (causal).

قرارداد ضدنشتی: feature(df).iloc[:k] باید فقط از df.iloc[:k] تابع باشد.
ویژگی‌ها با پنجرهٔ گرم‌شدنی warmup محاسبه می‌شوند و قبل از آن NaN می‌مانند.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WARMUP = 20  # بیشینهٔ پنجرهٔ نگاه به عقب در این کتابخانه


def _log_return(df: pd.DataFrame, periods: int = 1) -> pd.Series:
    return np.log(df["close"]).diff(periods)


def realized_vol(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """انحراف معیار لگاریتم بازده ۱۵ دقیقه‌ای در پنجرهٔ past (شامل خودِ کندل t)."""
    return _log_return(df).rolling(window, min_periods=window).std()


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """میانگین دامنهٔ واقعی با فرمول وایلدر (RMA) — فقط دادهٔ گذشته."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()


def range_expansion(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """دامنهٔ کندل فعلی نسبت به میانهٔ دامنهٔ window کندل گذشته (بدون خود فعلی)."""
    rng = df["high"] - df["low"]
    med_prev = rng.shift(1).rolling(window, min_periods=window).median()
    return rng / med_prev


def donchian_position(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """موقعیت کلوز در کانال min/max با window کندل گذشته (بدون خود فعلی) ∈ [0,1]."""
    hi = df["high"].shift(1).rolling(window, min_periods=window).max()
    lo = df["low"].shift(1).rolling(window, min_periods=window).min()
    return (df["close"] - lo) / (hi - lo)


def body_ratio(df: pd.DataFrame) -> pd.Series:
    """نسبت بدنهٔ کندل به کل دامنه ∈ [0,1]."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return (df["close"] - df["open"]).abs() / rng


def session_of(df: pd.DataFrame) -> pd.Series:
    """نشست جهانی بر اساس ساعت UTC: 'asia' | 'london' | 'newyork' | 'off'."""
    h = pd.DatetimeIndex(df["time"]).hour
    def bucket(x):
        if 0 <= x < 7:
            return "asia"
        if 7 <= x < 13:
            return "london"
        if 13 <= x < 21:
            return "newyork"
        return "off"
    return pd.Series([bucket(x) for x in h], index=df.index, dtype="object")


BASE_FEATURES = {
    "ret_1": lambda d: _log_return(d, 1),
    "ret_4": lambda d: _log_return(d, 4),
    "ret_16": lambda d: _log_return(d, 16),
    "realized_vol_20": lambda d: realized_vol(d, 20),
    "atr_14": lambda d: atr(d, 14),
    "range_expansion_20": lambda d: range_expansion(d, 20),
    "donchian_pos_20": lambda d: donchian_position(d, 20),
    "body_ratio": body_ratio,
}


def build_features(df: pd.DataFrame, names=None) -> pd.DataFrame:
    """ماتریس ویژگی هم‌راستا با df؛ ستون‌های session به one-hot اضافه می‌شوند."""
    names = list(names) if names else list(BASE_FEATURES)
    out = pd.DataFrame(index=df.index)
    for name in names:
        if name not in BASE_FEATURES:
            raise KeyError(f"ویژگی ناشناخته: {name}")
        out[name] = BASE_FEATURES[name](df)
    sess = session_of(df)
    for s in ("asia", "london", "newyork", "off"):
        out[f"session_{s}"] = (sess == s).astype(float)
    return out
