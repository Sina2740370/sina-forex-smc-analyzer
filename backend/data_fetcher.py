"""
دریافت داده OHLCV با کش هوشمند 5 تایم‌فریم
Top-Down: 1wk → 1d → 4h → 1h → 15m
"""
import warnings
import logging
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

from .config import CANDLE_LIMIT, MIN_CANDLES, CACHE_TTL

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

# ============================================================
#  کش هوشمند (in-memory)
# ============================================================
_cache = {}


def _cache_key(symbol: str, tf: str) -> str:
    return f"{symbol}:{tf}"


def _is_cache_valid(key: str, ttl_minutes: int) -> bool:
    if key not in _cache:
        return False
    elapsed = (time.time() - _cache[key]["ts"]) / 60
    return elapsed < ttl_minutes


def _get_cached(key: str) -> Optional[list]:
    if key in _cache:
        return _cache[key]["data"]
    return None


def _set_cache(key: str, data: list):
    _cache[key] = {"data": data, "ts": time.time()}


# ============================================================
#  تمیزکاری DataFrame
# ============================================================

def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })

    df = df.reset_index()
    for col in ("Datetime", "Date", "index"):
        if col in df.columns:
            df = df.rename(columns={col: "time"})
            break

    cols = ["time", "open", "high", "low", "close"]
    if "volume" in df.columns:
        cols.append("volume")
    else:
        df["volume"] = 0
        cols.append("volume")

    return df[cols].dropna()


# ============================================================
#  تبدیل تایم‌فریم
# ============================================================

YF_INTERVAL_MAP = {
    "15m": ("15m", "5d"),
    "30m": ("30m", "1mo"),
    "1h":  ("60m", "1mo"),
    "4h":  ("1h", "3mo"),      # از 1h می‌سازیم
    "1d":  ("1d", "1y"),
    "1wk": ("1wk", "5y"),
}


# ============================================================
#  دریافت داده اصلی
# ============================================================

def fetch_ohlcv(symbol: str, timeframe: str = "15m",
                limit: int = CANDLE_LIMIT,
                use_cache: bool = True) -> list:
    """دریافت کندل‌ها با کش هوشمند"""
    key = _cache_key(symbol, timeframe)
    ttl = CACHE_TTL.get(timeframe, 15)

    # چک کش
    if use_cache and _is_cache_valid(key, ttl):
        cached = _get_cached(key)
        if cached:
            logger.debug(f"📦 کش: {symbol} {timeframe} ({len(cached)} کندل)")
            return cached

    # دریافت از yfinance
    if timeframe not in YF_INTERVAL_MAP:
        logger.warning(f"تایم‌فریم نامعتبر: {timeframe}")
        return []

    yf_interval, period = YF_INTERVAL_MAP[timeframe]

    try:
        df = yf.download(
            symbol,
            interval=yf_interval,
            period=period,
            progress=False,
            auto_adjust=False,
            threads=False,
        )

        if df is None or df.empty:
            logger.warning(f"داده خالی: {symbol} {timeframe}")
            return []

        df = _clean_dataframe(df)

        # تبدیل 1h به 4h
        if timeframe == "4h" and yf_interval == "1h":
            df = _resample_to_4h(df)

        df = df.tail(limit)

        records = [{
            "time": row["time"].isoformat() if hasattr(row["time"], "isoformat")
                    else str(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
        } for _, row in df.iterrows()]

        # ذخیره در کش
        _set_cache(key, records)
        logger.info(f"✅ {symbol} {timeframe}: {len(records)} کندل")
        return records

    except Exception as e:
        logger.error(f"❌ خطا fetch {symbol} {timeframe}: {e}")
        # اگه کش قدیمی هست، برگردون
        old = _get_cached(key)
        if old:
            logger.info(f"🔄 استفاده از کش قدیمی {symbol} {timeframe}")
            return old
        return []


def _resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """تبدیل 1h → 4h"""
    df = df.set_index("time")
    out = pd.DataFrame({
        "open":   df["open"].resample("4h").first(),
        "high":   df["high"].resample("4h").max(),
        "low":    df["low"].resample("4h").min(),
        "close":  df["close"].resample("4h").last(),
        "volume": df["volume"].resample("4h").sum(),
    }).dropna()
    return out.reset_index()


# ============================================================
#  دریافت 5 تایم‌فریم
# ============================================================

def fetch_topdown(symbol: str) -> dict:
    """
    دریافت 5 تایم‌فریم برای تحلیل Top-Down

    Returns:
        {
            "1wk": [...], "1d": [...], "4h": [...],
            "1h": [...],  "15m": [...]
        }
        یا {"error": "..."}
    """
    result = {}
    timeframes = ["1wk", "1d", "4h", "1h", "15m"]

    for tf in timeframes:
        data = fetch_ohlcv(symbol, tf)
        min_required = 20 if tf in ("1wk", "1d") else MIN_CANDLES

        if len(data) < min_required:
            logger.warning(f"{symbol} {tf}: داده کم ({len(data)} < {min_required})")
            # تایم‌فریم‌های بالا رو می‌تونیم نادیده بگیریم
            if tf in ("1wk", "1d") and len(data) > 0:
                result[tf] = data
                continue
            return {"error": f"{tf} داده کافی نیست"}

        result[tf] = data

    return result


# ============================================================
#  سازگاری با کد قبلی
# ============================================================

def fetch_multi_timeframe(symbol: str, htf: str = "4h",
                          ltf: str = "15m") -> dict:
    """نسخه قدیمی (سازگاری با scheduler)"""
    htf_data = fetch_ohlcv(symbol, htf)
    ltf_data = fetch_ohlcv(symbol, ltf)

    if len(htf_data) < MIN_CANDLES:
        return {"error": f"HTF داده کم ({len(htf_data)})"}
    if len(ltf_data) < MIN_CANDLES:
        return {"error": f"LTF داده کم ({len(ltf_data)})"}

    return {"htf": htf_data, "ltf": ltf_data}


def get_cache_stats() -> dict:
    """آمار کش (برای دیباگ)"""
    return {
        "size": len(_cache),
        "keys": list(_cache.keys()),
            }
