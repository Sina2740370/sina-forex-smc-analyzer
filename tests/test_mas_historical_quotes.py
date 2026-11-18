from datetime import datetime, timezone
from decimal import Decimal

import pytest

from engine.mas.historical_quotes import (
    historical_quote_from_row,
    historical_quote_series,
)


def row(timestamp="2025-01-06T10:00:00Z"):
    return {
        "timestamp": timestamp,
        "bid_open": "1.1000", "bid_high": "1.1020",
        "bid_low": "1.0990", "bid_close": "1.1010",
        "ask_open": "1.1002", "ask_high": "1.1022",
        "ask_low": "1.0992", "ask_close": "1.1012",
    }


def test_explicit_bid_ask_row_becomes_utc_decimal_ohlc_quote():
    quote = historical_quote_from_row(row("2025-01-06T12:00:00+02:00"))
    assert quote.timestamp == "2025-01-06T10:00:00+00:00"
    assert quote.bid_open == Decimal("1.1000")
    assert quote.ask_open == Decimal("1.1002")


def test_mid_only_ohlc_is_rejected_without_invented_spread():
    mid = {
        "timestamp": "2025-01-06T10:00:00Z",
        "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.15",
    }
    with pytest.raises(ValueError, match="missing required fields"):
        historical_quote_from_row(mid)


@pytest.mark.parametrize("field", [
    "bid_open", "bid_high", "bid_low", "bid_close",
    "ask_open", "ask_high", "ask_low", "ask_close",
])
def test_binary_float_prices_are_rejected(field):
    data = row()
    data[field] = 1.1001
    with pytest.raises(ValueError, match="exact Decimal"):
        historical_quote_from_row(data)


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        historical_quote_from_row(row("2025-01-06T10:00:00"))


def test_crossed_quote_is_rejected():
    data = row()
    data["ask_open"] = "1.0999"
    with pytest.raises(ValueError, match="ask quote cannot be below bid"):
        historical_quote_from_row(data)


def test_inconsistent_side_ohlc_is_rejected():
    data = row()
    data["bid_high"] = "1.1005"
    with pytest.raises(ValueError, match="OHLC values are inconsistent"):
        historical_quote_from_row(data)


def test_series_requires_strict_chronological_order_and_preserves_input_order():
    quotes = historical_quote_series([
        row("2025-01-06T10:00:00Z"),
        row("2025-01-06T10:15:00Z"),
    ])
    assert len(quotes) == 2
    with pytest.raises(ValueError, match="strictly increasing"):
        historical_quote_series([
            row("2025-01-06T10:15:00Z"),
            row("2025-01-06T10:00:00Z"),
        ])


def test_duplicate_timestamps_are_rejected():
    with pytest.raises(ValueError, match="strictly increasing"):
        historical_quote_series([row(), row()])


def test_empty_series_is_rejected():
    with pytest.raises(ValueError, match="cannot be empty"):
        historical_quote_series([])
