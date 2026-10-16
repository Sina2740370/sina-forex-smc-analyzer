"""
لایه‌ی دیتا: CSV خروجی MT5، yfinance، ریسمپل، و TopDownView (بدون lookahead).

نکات مهم:
- همه‌ی زمان‌ها UTC و tz-aware هستند. خروجی MT5 بر پایه‌ی ساعت «سرور بروکر» است
  (معمولاً UTC+2/+3)، پس tz_offset_hours را درست بده.
- روزانه/هفتگی با مرز 17:00 نیویورک (با DST؛ 21:00 یا 22:00 UTC) ساخته می‌شود تا کندل یکشنبه جدا نیفتد.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1wk": 10080}
TAIL = {"1wk": 120, "1d": 200, "4h": 200, "1h": 300, "15m": 400}
COLS = ["time", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------- CSV (MT5)
def _parse_mt5_timestamps(values: pd.Series) -> pd.Series:
    """Parse timestamps with consistent awareness; mixed aware/naive rows are rejected."""
    flags = []
    for value in values:
        stamp = pd.Timestamp(value)
        flags.append(stamp.tzinfo is not None and stamp.utcoffset() is not None)
    if any(flags) and not all(flags):
        raise ValueError("CSV دارای ترکیب زمان‌های timezone-aware و naive است")
    if flags and all(flags):
        return pd.Series(pd.to_datetime(values, utc=True, errors="raise"), index=values.index)
    return pd.Series(pd.to_datetime(values, errors="raise"), index=values.index)


def _normalize_mt5_timestamps(
    ts: pd.Series, tz_offset_hours: float = 0.0, broker_timezone: str | None = None
) -> pd.Series:
    """تبدیل زمان CSV به UTC؛ از اعمال دوباره‌ی offset روی زمان aware جلوگیری می‌کند."""
    if not np.isfinite(tz_offset_hours) or abs(tz_offset_hours) > 14:
        raise ValueError("tz_offset_hours باید عددی بین -14 و +14 باشد")
    if broker_timezone and tz_offset_hours != 0:
        raise ValueError("از broker_timezone یا tz_offset_hours استفاده کن، نه هر دو")

    if ts.dt.tz is not None:
        if tz_offset_hours != 0 or broker_timezone:
            raise ValueError(
                "زمان CSV خودش timezone دارد؛ offset/timezone اضافی باعث جابه‌جایی دوباره می‌شود"
            )
        return ts.dt.tz_convert("UTC")

    if broker_timezone:
        try:
            # زمان‌های مبهم/ناموجود در تغییر DST عمداً رد می‌شوند تا جابه‌جایی خاموش رخ ندهد.
            return ts.dt.tz_localize(
                broker_timezone, ambiguous="raise", nonexistent="raise"
            ).dt.tz_convert("UTC")
        except Exception as exc:
            raise ValueError(
                f"زمان CSV با منطقه زمانی {broker_timezone!r} قابل تبدیل نیست: {exc}"
            ) from exc

    return (ts - pd.Timedelta(hours=tz_offset_hours)).dt.tz_localize("UTC")


def load_mt5_csv(
    path: str, tz_offset_hours: float = 0.0, broker_timezone: str | None = None
) -> pd.DataFrame:
    """
    خروجی «Export Bars» متاتریدر یا CSV عمومی را می‌خواند.
    برای ساعت سرور ثابت از tz_offset_hours؛ برای ساعت دارای DST از منطقه زمانی IANA بروکر استفاده کن.
    اگر timestamp خودش timezone داشته باشد، به UTC تبدیل می‌شود و offset اضافی مجاز نیست.
    """
    raw = pd.read_csv(path, sep=None, engine="python")
    raw.columns = [str(c).strip().strip("<>").lower() for c in raw.columns]
    if "date" in raw.columns and "time" in raw.columns:
        ts = _parse_mt5_timestamps(raw["date"].astype(str) + " " + raw["time"].astype(str))
    elif "datetime" in raw.columns:
        ts = _parse_mt5_timestamps(raw["datetime"])
    elif "time" in raw.columns:
        ts = _parse_mt5_timestamps(raw["time"])
    else:
        raise ValueError("ستون زمان پیدا نشد (date+time / datetime / time)")

    ts = _normalize_mt5_timestamps(ts, tz_offset_hours, broker_timezone)
    vol = raw["tickvol"] if "tickvol" in raw.columns else raw.get("volume", 0)
    df = pd.DataFrame({
        "time": ts,
        "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
        "volume": vol,
    })
    return _tidy(df)


def _validate_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Fail closed on malformed candles before resampling or strategy evaluation."""
    if not isinstance(df, pd.DataFrame):
        raise ValueError("داده‌ی کندل باید DataFrame باشد")
    missing = [column for column in COLS if column not in df.columns]
    if missing:
        raise ValueError(f"ستون‌های کندل ناقص است: {', '.join(missing)}")
    if df.empty:
        return df

    validated = df.copy()
    numeric = {}
    for column in ("open", "high", "low", "close", "volume"):
        converted = pd.to_numeric(df[column], errors="coerce")
        values = converted.to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"ستون {column} دارای NaN، Infinity یا مقدار غیرعددی است")
        numeric[column] = values
        validated[column] = converted

    prices = np.column_stack([numeric[name] for name in ("open", "high", "low", "close")])
    if (prices <= 0).any():
        raise ValueError("قیمت‌های OHLC باید مثبت باشند")
    open_, high, low, close = (numeric[name] for name in ("open", "high", "low", "close"))
    if (high < np.maximum(open_, close)).any():
        raise ValueError("High نمی‌تواند از Open یا Close کمتر باشد")
    if (low > np.minimum(open_, close)).any():
        raise ValueError("Low نمی‌تواند از Open یا Close بیشتر باشد")
    if (high < low).any():
        raise ValueError("High نمی‌تواند از Low کمتر باشد")
    if (numeric["volume"] < 0).any():
        raise ValueError("Volume نباید منفی باشد")

    timestamps = pd.to_datetime(df["time"], errors="raise")
    if not isinstance(timestamps.dtype, pd.DatetimeTZDtype):
        raise ValueError("زمان کندل‌ها باید timezone-aware باشد و به UTC نرمال شود")
    if timestamps.isna().any():
        raise ValueError("زمان کندل نمی‌تواند خالی باشد")
    timestamps = timestamps.dt.tz_convert("UTC")
    if timestamps.duplicated().any():
        raise ValueError("داده‌ی کندل دارای timestamp تکراری است")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("زمان کندل‌ها باید صعودی مرتب شده باشد")
    validated["time"] = timestamps
    return validated


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise ValueError("داده‌ی کندل باید DataFrame باشد")
    missing = [column for column in COLS if column not in df.columns]
    if missing:
        raise ValueError(f"ستون‌های کندل ناقص است: {', '.join(missing)}")
    # Incomplete records are discarded as before; surviving records are
    # validated strictly so corrupt prices cannot silently reach the engine.
    cleaned = df[COLS].dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return _validate_ohlc(cleaned)


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
    if tf not in TF_MINUTES:
        raise ValueError(f"تایم‌فریم پشتیبانی نمی‌شود: {tf}")
    df15 = _validate_ohlc(df15)
    if tf == "15m":
        return df15.copy()
    if tf == "1d":
        return _resample_ny(df15, "1D")
    if tf == "1wk":
        return _resample_ny(df15, "W-SUN")
    rule = {"1h": "1h", "4h": "4h"}[tf]
    d = df15.set_index("time")
    out = d.resample(rule, closed="left", label="left").agg(_AGG).dropna()
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


# ---------------------------------------------------------------- زمان بسته‌شدن کندل
def _bar_close_times(df: pd.DataFrame, tf: str) -> pd.Series:
    """زمان بسته‌شدن کندل‌ها را با لحاظ DST محاسبه می‌کند.

    کندل‌های روزانه و هفتگی فارکس در 17:00 نیویورک بسته می‌شوند؛
    بنابراین طول واقعی آن‌ها در تغییر DST می‌تواند 23/25 یا 167/169 ساعت باشد.
    """
    starts = pd.to_datetime(df["time"], utc=True)
    if tf not in TF_MINUTES:
        raise ValueError(f"تایم‌فریم پشتیبانی نمی‌شود: {tf}")

    fixed_closes = starts + pd.Timedelta(minutes=TF_MINUTES[tf])
    if tf not in ("1d", "1wk"):
        return fixed_closes

    local_starts = starts.dt.tz_convert(NY_TZ)
    # فقط کندل‌هایی که واقعاً روی مرز 17:00 نیویورک ساخته شده‌اند،
    # باید طول تقویمی DST-aware داشته باشند. کندل‌های native یاهو
    # معمولاً در مرز دیگری هستند و طول ثابت 24h/168h را حفظ می‌کنند.
    if tf == "1d":
        session_aligned = local_starts.dt.hour == 17
        session_closes = (local_starts + pd.DateOffset(days=1)).dt.tz_convert("UTC")
    else:
        session_aligned = (local_starts.dt.hour == 17) & (local_starts.dt.dayofweek == 6)
        session_closes = (local_starts + pd.DateOffset(weeks=1)).dt.tz_convert("UTC")
    return fixed_closes.where(~session_aligned, session_closes)


# ---------------------------------------------------------------- تازگی
def staleness_minutes(df: pd.DataFrame, tf: str, now: pd.Timestamp) -> float:
    """چند دقیقه از بسته‌شدن آخرین کندل گذشته؛ برای رد داده‌ی کهنه در live."""
    if df.empty:
        return float("inf")
    last_close = _bar_close_times(df.tail(1), tf).iloc[0]
    return (now - last_close).total_seconds() / 60.0


# ---------------------------------------------------------------- نمای بدون lookahead
class TopDownView:
    """
    در هر لحظه‌ی `now` فقط کندل‌هایی را نشان می‌دهد که close_time ≤ now دارند؛
    کندل در حال تشکیل (در همه‌ی تایم‌فریم‌ها، از جمله هفتگی/روزانه) هرگز دیده نمی‌شود.
    """

    def __init__(self, data: dict):
        if not isinstance(data, dict):
            raise ValueError("داده‌ی چندتایم‌فریم باید dict باشد")
        validated_data = {}
        for tf, frame in data.items():
            if tf not in TF_MINUTES:
                raise ValueError(f"تایم‌فریم پشتیبانی نمی‌شود: {tf}")
            validated_data[tf] = _validate_ohlc(frame)
        self.data = validated_data
        self._close = {
            tf: _bar_close_times(df, tf)
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
