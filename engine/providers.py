"""
لایه‌ی دیتا: CSV خروجی MT5، yfinance، ریسمپل، و TopDownView (بدون lookahead).

نکات مهم:
- همه‌ی زمان‌ها UTC و tz-aware هستند. خروجی MT5 بر پایه‌ی ساعت «سرور بروکر» است
  (معمولاً UTC+2/+3)، پس tz_offset_hours را درست بده.
- روزانه/هفتگی با مرز بسته‌شدن فارکس (~22:00 UTC) ساخته می‌شود تا کندل یکشنبه جدا نیفتد.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1wk": 10080}
TAIL = {"1wk": 120, "1d": 200, "4h": 200, "1h": 300, "15m": 400}
COLS = ["time", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------- CSV (MT5)
def load_mt5_csv(path: str, tz_offset_hours: float = 0.0) -> pd.DataFrame:
    """
    خروجی «Export Bars» متاتریدر (<DATE> <TIME> <OPEN> ...) یا CSV عمومی
    با ستون‌های time/open/high/low/close را می‌خواند.
    """
    raw = pd.read_csv(path, sep=None, engine="python")
    raw.columns = [str(c).strip().strip("<>").lower() for c in raw.columns]
    if "date" in raw.columns and "time" in raw.columns:
        ts = pd.to_datetime(raw["date"].astype(str) + " " + raw["time"].astype(str))
    elif "datetime" in raw.columns:
        ts = pd.to_datetime(raw["datetime"])
    elif "time" in raw.columns:
        ts = pd.to_datetime(raw["time"])
    else:
        raise ValueError("ستون زمان پیدا نشد (date+time / datetime / time)")
    vol = raw["tickvol"] if "tickvol" in raw.columns else raw.get("volume", 0)
    df = pd.DataFrame({
        "time": ts - pd.Timedelta(hours=tz_offset_hours),
        "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
        "volume": vol,
    })
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize("UTC") \
        if df["time"].dt.tz is None else df["time"].dt.tz_convert("UTC")
    return _tidy(df)


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLS].dropna().drop_duplicates("time").sort_values("time")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------- ریسمپل
def resample_ohlc(df15: pd.DataFrame, tf: str) -> pd.DataFrame:
    """ساخت تایم‌فریم بالاتر از 15m. 1d/1wk با شیفت ۲ ساعت (۲۲:۰۰ UTC = شروع روز)."""
    if tf == "15m":
        return df15
    shift = pd.Timedelta(hours=2) if tf in ("1d", "1wk") else pd.Timedelta(0)
    rule = {"1h": "1h", "4h": "4h", "1d": "1D", "1wk": "W-MON"}[tf]
    d = df15.set_index("time")
    d.index = d.index + shift
    kw = {"label": "left", "closed": "left"}
    out = d.resample(rule, **kw).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    out.index = out.index - shift
    return out.reset_index().rename(columns={"index": "time"})[COLS]


def build_topdown(df15: pd.DataFrame) -> dict:
    return {tf: resample_ohlc(df15, tf) for tf in TF_MINUTES}


# ---------------------------------------------------------------- yfinance
_YF = {"15m": ("15m", "60d"), "1h": ("60m", "730d"), "1d": ("1d", "10y"), "1wk": ("1wk", "10y")}


def load_yfinance(yf_symbol: str, tf: str) -> pd.DataFrame:
    import yfinance as yf  # import تنبل: فقط وقتی لازم است

    if tf == "4h":
        return resample_ohlc(load_yfinance(yf_symbol, "1h"), "4h")
    interval, period = _YF[tf]
    df = yf.download(yf_symbol, interval=interval, period=period,
                     progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        return pd.DataFrame(columns=COLS)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    tcol = next(c for c in ("Datetime", "Date", "index") if c in df.columns)
    df = df.rename(columns={tcol: "time", "Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Volume": "volume"})
    t = pd.to_datetime(df["time"])
    df["time"] = t.dt.tz_localize("UTC") if t.dt.tz is None else t.dt.tz_convert("UTC")
    return _tidy(df)


def load_yfinance_topdown(yf_symbol: str) -> dict:
    """هر تایم‌فریم را بومی از yfinance می‌گیرد (1h و 4h: تا ۷۳۰ روز، 15m: تا ۶۰ روز)."""
    data = {tf: load_yfinance(yf_symbol, tf) for tf in ("15m", "1h", "1d", "1wk")}
    data["4h"] = resample_ohlc(data["1h"], "4h")
    return data


# ---------------------------------------------------------------- تازگی
def staleness_minutes(df: pd.DataFrame, tf: str, now: pd.Timestamp) -> float:
    """چند دقیقه از بسته‌شدن آخرین کندل گذشته؛ برای رد داده‌ی کهنه در live."""
    if df.empty:
        return float("inf")
    last_close = df["time"].iloc[-1] + pd.Timedelta(minutes=TF_MINUTES[tf])
    return (now - last_close).total_seconds() / 60.0


# ---------------------------------------------------------------- نمای بدون lookahead
class TopDownView:
    """
    در هر لحظه‌ی `now` فقط کندل‌هایی را نشان می‌دهد که close_time ≤ now دارند؛
    کندل در حال تشکیل (در همه‌ی تایم‌فریم‌ها، از جمله هفتگی/روزانه) هرگز دیده نمی‌شود.
    """

    def __init__(self, data: dict):
        self.data = data
        self._close = {
            tf: (df["time"] + pd.Timedelta(minutes=TF_MINUTES[tf]))
            .dt.tz_convert("UTC").dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
            for tf, df in data.items()
        }

    def at(self, now: pd.Timestamp) -> dict:
        now64 = np.datetime64(now.tz_convert("UTC").tz_localize(None), "ns")
        out = {}
        for tf, df in self.data.items():
            end = int(np.searchsorted(self._close[tf], now64, side="right"))
            out[tf] = df.iloc[max(0, end - TAIL[tf]):end].reset_index(drop=True)
        return out
