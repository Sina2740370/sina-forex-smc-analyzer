"""Strict ingestion boundary for historical executable Bid/Ask OHLC bars.

Legacy single-price OHLCV bars are intentionally unsupported: a mid/close
series cannot be converted into executable bid/ask history without an
independently evidenced spread model. This module never synthesizes quotes,
fills missing bars, sorts input silently, or converts binary floats to money.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Iterable, Mapping
from typing import Any

from .execution import decimal_value
from .lifecycle import OHLCQuote


PRICE_FIELDS = (
    "bid_open", "bid_high", "bid_low", "bid_close",
    "ask_open", "ask_high", "ask_low", "ask_close",
)


def _utc_iso(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("timestamp must be ISO-8601 with an explicit timezone") from None
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("timestamp must be an aware datetime or ISO-8601 string")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def historical_quote_from_row(row: Mapping[str, Any]) -> OHLCQuote:
    """Convert one explicit bid/ask row to the shared lifecycle quote contract."""
    if not isinstance(row, Mapping):
        raise ValueError("historical quote row must be a mapping")

    missing = [field for field in ("timestamp", *PRICE_FIELDS) if field not in row]
    if missing:
        raise ValueError(f"historical quote row is missing required fields: {missing}")

    timestamp = _utc_iso(row["timestamp"])
    prices = {
        field: decimal_value(row[field], field, positive=True)
        for field in PRICE_FIELDS
    }
    return OHLCQuote(timestamp=timestamp, **prices)


def historical_quote_series(rows: Iterable[Mapping[str, Any]]) -> tuple[OHLCQuote, ...]:
    """Validate a chronological quote stream without reordering or gap filling.

    Timestamps must be strictly increasing. Duplicate or out-of-order bars
    fail closed because silently sorting can conceal provider/data corruption.
    Missing intervals are preserved as missing; no synthetic candle is created.
    """
    if isinstance(rows, (str, bytes, Mapping)):
        raise ValueError("rows must be an iterable of quote-row mappings")

    result: list[OHLCQuote] = []
    previous: datetime | None = None
    for index, row in enumerate(rows):
        quote = historical_quote_from_row(row)
        current = datetime.fromisoformat(quote.timestamp)
        if previous is not None and current <= previous:
            raise ValueError(
                f"historical quote timestamps must be strictly increasing (row {index})"
            )
        result.append(quote)
        previous = current

    if not result:
        raise ValueError("historical quote series cannot be empty")
    return tuple(result)
