"""کتابخانهٔ ویژگی فاز ۲ — ۲۴ ویژگی ساختاری و کمّی، همهٔ اکیداً عقب‌نگر.

قرارداد ضدنشتی همان فاز ۰: feature(df).iloc[:k] فقط تابع df.iloc[:k].
ویژگی‌های ساختاری (SMC-گونه) به رویداد تبدیل می‌شوند در aee/scoring.py،
نه فیلتر — قاعدهٔ «امتیازدهی به‌جای ردکردن».
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from aee.features import _log_return


def _rv(df, window):
    return _log_return(df).rolling(window, min_periods=window).std()


def _ema(s, span):
    return s.ewm(span=span, min_periods=span, adjust=False).mean()


def vol_ratio(df):
    """نسبت نوسان کوتاه به بلند: انفجار/سردشدن نوسان."""
    return _rv(df, 5) / _rv(df, 20)


def z_ret_16(df):
    """بازده ۴ ساعته استانداردشده با نوسان محقق‌شده."""
    ret = _log_return(df, 16)
    rv = _rv(df, 20)
    return ret / (rv * np.sqrt(16))


def atr_pct(df, window=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    return atr / df["close"]


def streak(df):
    """طول دنبالهٔ کلوزهای هم‌جهت (مثبت = صعودی، منفی = نزولی)."""
    sign = np.sign(df["close"].diff())
    out = sign.copy()
    for i in range(1, len(sign)):
        s = sign.iloc[i]
        if s == 0:
            out.iloc[i] = 0
        elif s == sign.iloc[i - 1] and out.iloc[i - 1] == out.iloc[i - 1]:
            out.iloc[i] = out.iloc[i - 1] + s
        else:
            out.iloc[i] = s
    return out


def gap_pct(df):
    """شکاف بازشدن نسبت به کلوز قبلی."""
    return df["open"] / df["close"].shift(1) - 1


def efficiency_16(df):
    """بازده مسیر: حرکت خالص ÷ مسافت طی‌شده ∈ [0,1] (کشش روند)."""
    net = (df["close"] - df["close"].shift(16)).abs()
    path = df["close"].diff().abs().rolling(16, min_periods=16).sum()
    return net / path


def dist_from_session_open(df):
    """فاصلهٔ کلوز از اولین بازشدن روز UTC، بر حسب ATR."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    day = pd.DatetimeIndex(df["time"]).date
    day_open = pd.Series(df["open"].to_numpy(), index=df.index).groupby(day).transform("first")
    return (df["close"] - day_open) / atr


def ema_slope_20(df):
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    ema = _ema(df["close"], 20)
    return (ema - ema.shift(4)) / atr


def ema_align(df):
    return ((_ema(df["close"], 20) > _ema(df["close"], 50)) * 2 - 1).astype(float)


def macd_norm(df):
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    return (_ema(df["close"], 8) - _ema(df["close"], 21)) / atr


def vol_z(df, window=20):
    v = df["volume"].astype(float)
    m = v.rolling(window, min_periods=window).mean()
    s = v.rolling(window, min_periods=window).std()
    return (v - m) / s.replace(0, np.nan)


def pivot_break(df, window=20):
    """شکست سوئینگ: کلوز بالاتر از اوج ۲۰ کندل گذشته (+1) یا پایین‌تر از کف (−1)."""
    hi = df["high"].shift(1).rolling(window, min_periods=window).max()
    lo = df["low"].shift(1).rolling(window, min_periods=window).min()
    up = (df["close"] > hi).astype(float)
    dn = -(df["close"] < lo).astype(float)
    return up + dn


def fvg_flag(df):
    """شکاف ارزش منصفانه (Fair Value Gap): گپ ساختاری سه‌کندلی."""
    up = (df["low"] > df["high"].shift(2)).astype(float)
    dn = -(df["high"] < df["low"].shift(2)).astype(float)
    return up + dn


def displacement(df):
    """کندل جابه‌جایی: بدنه بر حسب ATR (قدرت جهت‌دار).\n"""
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    return (df["close"] - df["open"]) / atr


def wick_upper(df):
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return (df["high"] - np.maximum(df["open"], df["close"])) / rng


def wick_lower(df):
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return (np.minimum(df["open"], df["close"]) - df["low"]) / rng


def rsi_14(df):
    d = df["close"].diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def boll_pos(df, window=20):
    m = df["close"].rolling(window, min_periods=window).mean()
    s = df["close"].rolling(window, min_periods=window).std()
    return (df["close"] - m) / (2 * s.replace(0, np.nan))


def ret_64(df):
    return _log_return(df, 64)


def donchian_pos_50(df):
    hi = df["high"].shift(1).rolling(50, min_periods=50).max()
    lo = df["low"].shift(1).rolling(50, min_periods=50).min()
    return (df["close"] - lo) / (hi - lo)


def inside_bar_ratio(df, window=20):
    """نسبت کندل‌های درونی (range < range قبلی) در پنجرهٔ گذشته."""
    inside = ((df["high"] < df["high"].shift(1)) &
              (df["low"] > df["low"].shift(1))).astype(float)
    return inside.shift(1).rolling(window, min_periods=window).mean()


def fresh_high_age(df, window=96):
    """سن آخرین اوج ۹۶ کندلی بر حسب کندل (عقب‌نگر: پنجرهٔ گذشته)."""
    hi = df["high"].shift(1).rolling(window, min_periods=window).max()
    is_new = (df["high"] >= hi).astype(float)
    age = pd.Series(np.nan, index=df.index)
    last = np.nan
    for i, v in enumerate(is_new.to_numpy()):
        if v == 1:
            last = 0
        elif last == last:
            last += 1
        age.iloc[i] = last
    return age / window


LIBRARY_FEATURES = {
    "vol_ratio_5_20": vol_ratio,
    "z_ret_16": z_ret_16,
    "atr_pct": atr_pct,
    "streak": streak,
    "gap_pct": gap_pct,
    "efficiency_16": efficiency_16,
    "dist_from_session_open": dist_from_session_open,
    "ema_slope_20": ema_slope_20,
    "ema_align": ema_align,
    "macd_norm": macd_norm,
    "vol_z_20": vol_z,
    "pivot_break_20": pivot_break,
    "fvg_flag": fvg_flag,
    "displacement_atr": displacement,
    "wick_upper": wick_upper,
    "wick_lower": wick_lower,
    "rsi_14": rsi_14,
    "boll_pos_20": boll_pos,
    "ret_64": ret_64,
    "donchian_pos_50": donchian_pos_50,
    "inside_bar_ratio_20": inside_bar_ratio,
    "fresh_high_age_96": fresh_high_age,
}


def build_library(df: pd.DataFrame, names=None) -> pd.DataFrame:
    """ماتریس ویژگی فاز ۲ (فقط کتابخانهٔ جدید؛ بدون session one-hot)."""
    names = list(names) if names else list(LIBRARY_FEATURES)
    out = pd.DataFrame(index=df.index)
    for name in names:
        if name not in LIBRARY_FEATURES:
            raise KeyError(f"ویژگی ناشناخته: {name}")
        out[name] = LIBRARY_FEATURES[name](df)
    return out
