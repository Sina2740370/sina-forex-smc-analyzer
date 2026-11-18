"""Strict Dukascopy CSV tick ingestion and bid/ask OHLC aggregation.

Expected source columns: timestamp (Unix milliseconds UTC), askPrice, bidPrice.
Prices are consumed as exact decimal strings from CSV; binary floats are rejected.
The provider's askVolume/bidVolume fields are deliberately ignored: they are
quote-side volume metadata, not verified traded volume.

This module performs no network access, sorting, gap filling, or price synthesis.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .execution import decimal_value
from .lifecycle import OHLCQuote


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("timestamp must be an exact integer millisecond value")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not raw.isdecimal():
            raise ValueError("timestamp must be a non-negative integer millisecond value")
        result = int(raw)
    else:
        raise ValueError("timestamp must be an exact integer millisecond value")
    if result < 0:
        raise ValueError("timestamp must be non-negative")
    return result


@dataclass(frozen=True)
class DukascopyTick:
    timestamp: datetime
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        ts = self.timestamp
        if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("tick timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", ts.astimezone(timezone.utc))
        bid = decimal_value(self.bid, "bid", positive=True)
        ask = decimal_value(self.ask, "ask", positive=True)
        if ask < bid:
            raise ValueError("ask cannot be below bid")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)


def dukascopy_tick_from_row(row: Mapping[str, Any]) -> DukascopyTick:
    """Parse one Dukascopy CSV/JSON tick without float conversion."""
    if not isinstance(row, Mapping):
        raise ValueError("tick row must be a mapping")
    missing = [name for name in ("timestamp", "askPrice", "bidPrice") if name not in row]
    if missing:
        raise ValueError(f"tick row is missing required fields: {missing}")
    timestamp = _EPOCH + timedelta(milliseconds=_timestamp_ms(row["timestamp"]))
    return DukascopyTick(
        timestamp=timestamp,
        bid=decimal_value(row["bidPrice"], "bidPrice", positive=True),
        ask=decimal_value(row["askPrice"], "askPrice", positive=True),
    )


def dukascopy_tick_series(rows: Iterable[Mapping[str, Any]]) -> tuple[DukascopyTick, ...]:
    """Validate provider order; equal millisecond timestamps retain input order."""
    if isinstance(rows, (str, bytes, Mapping)):
        raise ValueError("rows must be an iterable of tick-row mappings")
    result: list[DukascopyTick] = []
    previous: datetime | None = None
    for index, row in enumerate(rows):
        tick = dukascopy_tick_from_row(row)
        if previous is not None and tick.timestamp < previous:
            raise ValueError(f"tick timestamps must be non-decreasing (row {index})")
        result.append(tick)
        previous = tick.timestamp
    if not result:
        raise ValueError("tick series cannot be empty")
    return tuple(result)


def aggregate_dukascopy_ticks(
    rows: Iterable[Mapping[str, Any]], *, timeframe_minutes: int = 15
) -> tuple[OHLCQuote, ...]:
    """Aggregate actual ticks into UTC-aligned bid/ask OHLC bars.

    Intervals are [bar_start, bar_end), aligned to Unix epoch UTC. Only
    intervals containing at least one tick are returned. First/last bars may
    be partial; no empty bars, interpolation, forward-fill, or volume estimate
    is created. High/low are computed independently for bid and ask from the
    same ordered tick observations.
    """
    if isinstance(timeframe_minutes, bool) or not isinstance(timeframe_minutes, int):
        raise ValueError("timeframe_minutes must be a positive integer")
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be a positive integer")

    ticks = dukascopy_tick_series(rows)
    interval_ms = timeframe_minutes * 60_000
    bars: list[OHLCQuote] = []
    current_bucket: int | None = None
    bucket_ticks: list[DukascopyTick] = []

    def emit(bucket: int, values: list[DukascopyTick]) -> OHLCQuote:
        return OHLCQuote(
            timestamp=(_EPOCH + timedelta(milliseconds=bucket * interval_ms)).isoformat(),
            bid_open=values[0].bid,
            bid_high=max(t.bid for t in values),
            bid_low=min(t.bid for t in values),
            bid_close=values[-1].bid,
            ask_open=values[0].ask,
            ask_high=max(t.ask for t in values),
            ask_low=min(t.ask for t in values),
            ask_close=values[-1].ask,
        )

    for tick in ticks:
        elapsed = tick.timestamp - _EPOCH
        elapsed_ms = (elapsed.days * 86_400_000
                      + elapsed.seconds * 1_000
                      + elapsed.microseconds // 1_000)
        bucket = elapsed_ms // interval_ms
        if current_bucket is None:
            current_bucket = bucket
        elif bucket != current_bucket:
            bars.append(emit(current_bucket, bucket_ticks))
            current_bucket = bucket
            bucket_ticks = []
        bucket_ticks.append(tick)

    if current_bucket is not None:
        bars.append(emit(current_bucket, bucket_ticks))
    return tuple(bars)
