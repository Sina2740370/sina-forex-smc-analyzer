"""
دانلود تاریخچه‌ی چندساله‌ی M15 از Dukascopy (رایگان، UTC، اسپات واقعی از جمله XAUUSD).
خروجی: {out_dir}/{SYMBOL}_M15.csv با ستون‌های time,open,high,low,close,volume
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def _quarters(start: datetime, end: datetime):
    cur = start
    while cur < end:
        m = ((cur.month - 1) // 3 + 1) * 3 + 1
        nxt = datetime(cur.year + (m > 12), (m - 1) % 12 + 1, 1, tzinfo=timezone.utc)
        yield cur, min(nxt, end)
        cur = nxt


def _read_cache_csv(path: str) -> pd.DataFrame:
    """خواندن کش CSV با تضمین ایندکس tz-aware UTC.

    قبلاً اگر ایندکس ذخیره‌شده tz-naive خوانده می‌شد، tz_convert در دور بعد
    با خطا کرش می‌کرد؛ الان localize شرطی انجام می‌شود."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    idx = df.index
    if idx.tz is None:
        df.index = idx.tz_localize("UTC")
    else:
        df.index = idx.tz_convert("UTC")
    return df


def fetch_symbol(symbol: str, years: float, out_dir: str, verbose: bool = True) -> str:
    import dukascopy_python as dk

    pair = f"{symbol[:3]}/{symbol[3:]}"
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=int(365 * years))).replace(day=1, hour=0)
    cache = os.path.join(out_dir, ".cache")
    os.makedirs(cache, exist_ok=True)

    parts = []
    for a, b in _quarters(start, end):
        f = os.path.join(cache, f"{symbol}_{a:%Y%m%d}.csv")
        fresh = b < end - timedelta(days=7)
        if fresh and os.path.exists(f):
            parts.append(_read_cache_csv(f))
            continue
        if verbose:
            print(f"  {symbol} {a:%Y-%m-%d} -> {b:%Y-%m-%d} ...", flush=True)
        df = dk.fetch(pair, dk.INTERVAL_MIN_15, dk.OFFER_SIDE_BID, a, b)
        df = df[COLS]
        if fresh:
            df.to_csv(f)
        parts.append(df)

    full = pd.concat(parts)
    full = full[~full.index.duplicated()].sort_index()
    full.index = full.index.tz_convert("UTC").tz_localize(None)
    full.index.name = "time"
    path = os.path.join(out_dir, f"{symbol}_M15.csv")
    full.to_csv(path)
    if verbose:
        print(f"OK {symbol}: {len(full)} candles {full.index[0]} -> {full.index[-1]} -> {path}")
    return path
