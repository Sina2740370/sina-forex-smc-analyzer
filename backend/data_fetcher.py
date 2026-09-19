"""
دریافت داده OHLCV با کش هوشمند + فایل‌بک دیسک
Top-Down: 1wk → 1d → 4h → 1h → 15m
+ قیمت‌های زنده
"""
import warnings
import logging
import time
import json
import os
import threading
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

from .config import CANDLE_LIMIT, MIN_CANDLES, CACHE_TTL, SYMBOLS

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

# مسیر کش دیسک
CACHE_FILE = os.getenv("CACHE_FILE", "data_cache.json")
MAX_STORE = 500
_YF_LOCK = threading.Lock()  # حداکثر کندل ذخیره‌شده در کش

# ============================================================
#  کش هوشمند (in-memory)
# ============================================================
_cache = {}


def _cache_key(symbol: str, tf: str) -> str:
    return f"{symbol}:{tf}"


def _is_cache_valid(key: str, ttl_minutes: float) -> bool:
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
#  کش دیسک (مقاوم به ری‌استارت)
# ============================================================

def _save_cache_to_disk():
    """ذخیره کش توی فایل"""
    try:
        serializable = {}
        for k, v in _cache.items():
            serializable[k] = v
        with open(CACHE_FILE, "w") as f:
            json.dump(serializable, f)
    except Exception as e:
        logger.debug(f"خطا در ذخیره کش دیسک: {e}")


def _load_cache_from_disk():
    """بارگذاری کش از فایل"""
    global _cache
    if not os.path.exists(CACHE_FILE):
        return
    try:
        with open(CACHE_FILE, "r") as f:
            _cache = json.load(f)
        # پاک کردن کش‌های خیلی قدیمی (بیشتر از 24 ساعت)
        now = time.time()
        _cache = {
            k: v for k, v in _cache.items()
            if (now - v.get("ts", 0)) < 86400
        }
        logger.info(f"📦 کش از دیسک بارگذاری شد ({len(_cache)} آیتم)")
    except Exception as e:
        logger.warning(f"خطا در بارگذاری کش دیسک: {e}")
        _cache = {}


# بارگذاری در import
_load_cache_from_disk()


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


YF_INTERVAL_MAP = {
    "15m": ("15m", "5d"),
    "30m": ("30m", "1mo"),
    "1h":  ("60m", "1mo"),
    "4h":  ("1h", "3mo"),
    "1d":  ("1d", "1y"),
    "1wk": ("1wk", "5y"),
}


# ============================================================
#  دریافت داده OHLCV
# ============================================================

def fetch_ohlcv(symbol: str, timeframe: str = "15m",
                limit: int = CANDLE_LIMIT,
                use_cache: bool = True) -> list:
    key = _cache_key(symbol, timeframe)
    ttl = CACHE_TTL.get(timeframe, 15)

    if use_cache and _is_cache_valid(key, ttl):
        cached = _get_cached(key)
        if cached:
            return cached[-limit:]

    if timeframe not in YF_INTERVAL_MAP:
        logger.warning(f"تایم‌فریم نامعتبر: {timeframe}")
        return []

    yf_interval, period = YF_INTERVAL_MAP[timeframe]

    try:
        with _YF_LOCK:
            df = yf.download(
                symbol, interval=yf_interval, period=period,
                progress=False, auto_adjust=False, threads=False,
            )

        if df is None or df.empty:
            return []

        df = _clean_dataframe(df)

        if timeframe == "4h" and yf_interval == "1h":
            df = _resample_to_4h(df)

        df = df.tail(MAX_STORE)

        records = [{
            "time": row["time"].isoformat() if hasattr(row["time"], "isoformat")
                    else str(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
        } for _, row in df.iterrows()]

        _set_cache(key, records)
        _save_cache_to_disk()
        return records[-limit:]

    except Exception as e:
        logger.error(f"❌ خطا fetch {symbol} {timeframe}: {e}")
        old = _get_cached(key)
        if old:
            return old[-limit:]
        return []


def _resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
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
    result = {}
    timeframes = ["1wk", "1d", "4h", "1h", "15m"]

    for tf in timeframes:
        data = fetch_ohlcv(symbol, tf)
        min_required = 20 if tf in ("1wk", "1d") else MIN_CANDLES

        if len(data) < min_required:
            if tf in ("1wk", "1d") and len(data) > 0:
                result[tf] = data
                continue
            return {"error": f"{tf} داده کافی نیست"}

        result[tf] = data

    return result


# ============================================================
#  قیمت‌های زنده
# ============================================================

_prices_cache = {"data": {}, "ts": 0}
PRICES_TTL_SECONDS = 30


def fetch_live_prices(use_cache: bool = True) -> dict:
    global _prices_cache

    if use_cache and _prices_cache["data"]:
        elapsed = time.time() - _prices_cache["ts"]
        if elapsed < PRICES_TTL_SECONDS:
            return _prices_cache["data"]

    result = {}

    for symbol, name in SYMBOLS.items():
        try:
            with _YF_LOCK:
                df = yf.download(
                    symbol, period="1d", interval="5m",
                    progress=False, auto_adjust=False, threads=False,
                )

            if df is None or df.empty:
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })

            current = float(df["close"].iloc[-1])
            open_price = float(df["open"].iloc[0])

            change = current - open_price
            change_pct = (change / open_price * 100) if open_price else 0

            is_gold = symbol == "GC=F"
            decimals = 2 if is_gold else 5

            result[symbol] = {
                "symbol": symbol,
                "name": name,
                "price": round(current, decimals),
                "change": round(change, decimals),
                "change_pct": round(change_pct, 2),
                "direction": "up" if change >= 0 else "down",
            }

        except Exception as e:
            logger.warning(f"خطا در قیمت زنده {symbol}: {e}")
            continue

    if result:
        _prices_cache["data"] = result
        _prices_cache["ts"] = time.time()
        logger.info(f"💰 {len(result)} قیمت زنده دریافت شد")

    return result


# ============================================================
#  سازگاری
# ============================================================

def fetch_multi_timeframe(symbol: str, htf: str = "4h",
                          ltf: str = "15m") -> dict:
    htf_data = fetch_ohlcv(symbol, htf)
    ltf_data = fetch_ohlcv(symbol, ltf)

    if len(htf_data) < MIN_CANDLES:
        return {"error": f"HTF داده کم ({len(htf_data)})"}
    if len(ltf_data) < MIN_CANDLES:
        return {"error": f"LTF داده کم ({len(ltf_data)})"}

    return {"htf": htf_data, "ltf": ltf_data}


def get_cache_stats() -> dict:
    return {
        "size": len(_cache),
        "prices_cached": len(_prices_cache["data"]) > 0,
        "disk_cache": os.path.exists(CACHE_FILE),
    }
