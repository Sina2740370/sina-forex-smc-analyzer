"""
دانلود تاریخچه‌ی چندساله‌ی M15 از Dukascopy (رایگان، UTC، اسپات واقعی از جمله XAUUSD).
خروجی: {out_dir}/{SYMBOL}_M15.csv با ستون‌های time,open,high,low,close,volume
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]
OHLC = ["open", "high", "low", "close"]


def _midpoint_ohlc(bid: pd.DataFrame, ask: pd.DataFrame) -> pd.DataFrame:
    """Align BID/ASK bars and approximate midpoint OHLC for the spread-aware engine.

    The midpoint of aggregated BID/ASK highs/lows is not tick-exact midpoint OHLC;
    it is nevertheless preferable to applying a mid-price spread model to BID-only bars.
    """
    if bid is None or ask is None or bid.empty or ask.empty:
        raise ValueError("Dukascopy returned empty BID or ASK data")
    b = bid[COLS].sort_index().add_suffix("_bid")
    a = ask[COLS].sort_index().add_suffix("_ask")
    joined = b.join(a, how="inner")
    if joined.empty:
        raise ValueError("Dukascopy BID/ASK bars have no common timestamps")
    out = pd.DataFrame(index=joined.index)
    for col in OHLC:
        out[col] = (joined[f"{col}_bid"] + joined[f"{col}_ask"]) / 2.0
    # FX tick volume is not a centralized traded-volume measure; retain a neutral proxy.
    out["volume"] = (joined["volume_bid"] + joined["volume_ask"]) / 2.0
    return out


def _fetch_side(dk, instrument: str, offer_side: str, start: datetime, end: datetime):
    """Fetch one side with a network timeout; upstream defaults can block indefinitely."""
    original_get = dk.requests.get

    def timeout_get(*args, **kwargs):
        kwargs.setdefault("timeout", (5, 20))
        return original_get(*args, **kwargs)

    dk.requests.get = timeout_get
    try:
        return dk.fetch(
            instrument, dk.INTERVAL_MIN_15, offer_side, start, end, max_retries=1
        )
    finally:
        dk.requests.get = original_get


def _quarters(start: datetime, end: datetime):
    cur = start
    while cur < end:
        m = ((cur.month - 1) // 3 + 1) * 3 + 1
        nxt = datetime(cur.year + (m > 12), (m - 1) % 12 + 1, 1, tzinfo=timezone.utc)
        yield cur, min(nxt, end)
        cur = nxt


def fetch_symbol(symbol: str, years: float, out_dir: str, verbose: bool = True) -> str:
    import dukascopy_python as dk

    pair = f"{symbol[:3]}/{symbol[3:]}"
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=int(365 * years))).replace(day=1, hour=0)
    cache = os.path.join(out_dir, ".cache")
    os.makedirs(cache, exist_ok=True)

    parts = []
    for a, b in _quarters(start, end):
        f = os.path.join(cache, f"{symbol}_mid_{a:%Y%m%d}.csv")
        fresh = b < end - timedelta(days=7)
        if fresh and os.path.exists(f):
            parts.append(pd.read_csv(f, index_col=0, parse_dates=True))
            continue
        if verbose:
            print(f"  {symbol} {a:%Y-%m-%d} -> {b:%Y-%m-%d} ...", flush=True)
        bid = _fetch_side(dk, pair, dk.OFFER_SIDE_BID, a, b)
        ask = _fetch_side(dk, pair, dk.OFFER_SIDE_ASK, a, b)
        df = _midpoint_ohlc(bid, ask)
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
