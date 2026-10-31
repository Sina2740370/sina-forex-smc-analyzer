"""
لایه‌ی دیتا: CSV خروجی MT5، yfinance، ریسمپل، و TopDownView (بدون lookahead).

نکات مهم:
- همه‌ی زمان‌ها UTC و tz-aware هستند. خروجی MT5 بر پایه‌ی ساعت «سرور بروکر» است.
  برای LiteFinance: server_tz="Europe/Athens" (UTC+2 زمستان / UTC+3 تابستان، DST اروپا).
  در غیر این صورت tz_offset_hours ثابت کافی است.
- روزانه/هفتگی با مرز 17:00 نیویورک (با DST؛ 21:00 یا 22:00 UTC) ساخته می‌شود تا کندل یکشنبه جدا نیفتد.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1wk": 10080,
              "5m": 5, "1m": 1}
TAIL = {"1wk": 120, "1d": 200, "4h": 200, "1h": 300, "15m": 400,
        "5m": 500, "1m": 600}
COLS = ["time", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------- CSV (MT5)
def load_mt5_csv(path: str, tz_offset_hours: float = 0.0,
                 server_tz: Optional[str] = None) -> pd.DataFrame:
    """
    خروجی «Export Bars» متاتریدر (<DATE> <TIME> <OPEN> ...) یا CSV عمومی
    با ستون‌های time/open/high/low/close را می‌خواند.

    server_tz:
      None — رفتار قبلی: ts - tz_offset_hours سپس tz_localize(UTC)
      نام IANA (مثل "Europe/Athens") — ts ساده (naive) را با
        tz_localize(server_tz, ambiguous="NaT", nonexistent="shift_forward")
        سپس tz_convert("UTC")؛ ردیف‌های NaT حذف می‌شوند.
        LiteFinance: "Europe/Athens" (UTC+2 زمستان / UTC+3 تابستان).
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

    if server_tz:
        naive = pd.to_datetime(ts)
        if getattr(naive.dt, "tz", None) is not None:
            naive = naive.dt.tz_localize(None)
        aware = naive.dt.tz_localize(
            server_tz, ambiguous="NaT", nonexistent="shift_forward"
        )
        time_utc = aware.dt.tz_convert("UTC")
    else:
        shifted = pd.to_datetime(ts) - pd.Timedelta(hours=tz_offset_hours)
        if getattr(shifted.dt, "tz", None) is None:
            time_utc = shifted.dt.tz_localize("UTC")
        else:
            time_utc = shifted.dt.tz_convert("UTC")

    df = pd.DataFrame({
        "time": time_utc,
        "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
        "volume": vol,
    })
    df = df[df["time"].notna()]
    return _tidy(df)


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLS].dropna().drop_duplicates("time").sort_values("time")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------- ریسمپل
NY_TZ = "America/New_York"
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _resample_ny(df15: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    روز/هفته‌ی فارکس: شروع 17:00 به وقت نیویورک (با DST).
    کار روی ساعت دیواری محلی انجام می‌شود تا مرز در تابستان 21:00 UTC و در زمستان 22:00 UTC شود.
    """
    d = df15.set_index("time")
    shift = pd.Timedelta(hours=17)
    d.index = d.index.tz_convert(NY_TZ).tz_localize(None) - shift
    out = d.resample(rule, closed="left", label="left").agg(_AGG).dropna()
    out.index = (out.index + shift).tz_localize(
        NY_TZ, ambiguous="NaT", nonexistent="shift_forward").tz_convert("UTC")
    out = out[out.index.notna()]
    out.index.name = "time"
    return out.reset_index()[COLS]


def resample_ohlc(df15: pd.DataFrame, tf: str) -> pd.DataFrame:
    """ساخت تایم‌فریم بالاتر از 15m. 1d/1wk روی 17:00 نیویورک (با DST) شروع می‌شوند."""
    if tf == "15m":
        return df15
    if tf == "1d":
        return _resample_ny(df15, "1D")
    if tf == "1wk":
        return _resample_ny(df15, "W-SUN")
    rule = {"1h": "1h", "4h": "4h"}[tf]
    d = df15.set_index("time")
    out = d.resample(rule, closed="left", label="left").agg(_AGG).dropna()
    return out.reset_index().rename(columns={"index": "time"})[COLS]


# تایم‌فریم‌هایی که از 15m قابل ساخت هستند (5m/1m فقط از دیتای مستقل می‌آیند)
BASE_TFS = ("15m", "1h", "4h", "1d", "1wk")


def build_topdown(df15: pd.DataFrame) -> dict:
    return {tf: resample_ohlc(df15, tf) for tf in BASE_TFS}


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
