"""
دانلود تاریخچه‌ی چندساله‌ی M15 از Dukascopy (رایگان، UTC، اسپات واقعی از جمله XAUUSD).
خروجی: {out_dir}/{SYMBOL}_M15.csv با ستون‌های time,open,high,low,close,volume

سازگار با dukascopy-python >= 6.x (fetch_candles API)
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
    """خواندن کش CSV با تضمین ایندکس tz-aware UTC."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    idx = df.index
    if idx.tz is None:
        df.index = idx.tz_localize("UTC")
    else:
        df.index = idx.tz_convert("UTC")
    return df


def _candles_to_df(candles) -> pd.DataFrame:
    """Convert iterable of Candle objects to DataFrame indexed by UTC time."""
    rows = []
    for c in candles:
        ts = getattr(c, "timestamp", None) or getattr(c, "time", None) or getattr(c, "start", None)
        if ts is None:
            continue
        if not isinstance(ts, datetime):
            ts = pd.to_datetime(ts, utc=True)
        elif ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        rows.append({
            "time": ts,
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(getattr(c, "volume", 0) or 0),
        })
    if not rows:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame(rows).set_index("time").sort_index()
    return df[COLS]


def fetch_symbol(symbol: str, years: float, out_dir: str, verbose: bool = True) -> str:
    import dukascopy_python as dk

    # Map symbol to Dukascopy instrument code (v1 uses NAME like EUR/USD via code EUR-USD)
    # INSTRUMENT_CODE_TO_DATA keys look like "EUR-USD", "XAU-USD"
    if symbol.upper() == "XAUUSD":
        instrument = "XAU-USD"
    else:
        instrument = f"{symbol[:3]}-{symbol[3:]}"

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=int(365 * years))).replace(day=1, hour=0)
    cache = os.path.join(out_dir, ".cache")
    os.makedirs(cache, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    parts = []
    for a, b in _quarters(start, end):
        f = os.path.join(cache, f"{symbol}_{a:%Y%m%d}.csv")
        # cache only fully closed quarters
        fresh = b < end - timedelta(days=7)
        if fresh and os.path.exists(f):
            parts.append(_read_cache_csv(f))
            continue
        if verbose:
            print(f"  {symbol} {a:%Y-%m-%d} -> {b:%Y-%m-%d} ...", flush=True)
        try:
            candles = dk.fetch_candles(
                instrument,
                15,
                dk.TimeUnit.MINUTE,
                dk.OfferSide.BID,
                a,
                b,
                version=1,
            )
            df = _candles_to_df(candles)
        except Exception as e:
            if verbose:
                print(f"    WARN {symbol} {a:%Y-%m-%d}: {type(e).__name__}: {e}", flush=True)
            df = pd.DataFrame(columns=COLS)
        if df.empty:
            continue
        if fresh and len(df) > 0:
            df.to_csv(f)
        parts.append(df)

    if not parts:
        raise RuntimeError(f"No data fetched for {symbol}")

    full = pd.concat(parts)
    full = full[~full.index.duplicated()].sort_index()
    # store tz-naive UTC for compatibility with existing loaders
    if full.index.tz is not None:
        full.index = full.index.tz_convert("UTC").tz_localize(None)
    full.index.name = "time"
    path = os.path.join(out_dir, f"{symbol}_M15.csv")
    full.to_csv(path)
    if verbose:
        print(f"OK {symbol}: {len(full)} candles {full.index[0]} -> {full.index[-1]} -> {path}", flush=True)
    return path
