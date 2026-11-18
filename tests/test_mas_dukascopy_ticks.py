from decimal import Decimal

import pytest

from engine.mas.dukascopy_ticks import (
    aggregate_dukascopy_ticks,
    dukascopy_tick_from_row,
    dukascopy_tick_series,
)


def tick(ms, bid="1.1000", ask="1.1002"):
    return {"timestamp": str(ms), "bidPrice": bid, "askPrice": ask,
            "bidVolume": "0.5", "askVolume": "0.7"}


def test_tick_row_parses_epoch_milliseconds_and_exact_decimal_prices():
    parsed = dukascopy_tick_from_row(tick(0))
    assert parsed.timestamp.isoformat() == "1970-01-01T00:00:00+00:00"
    assert parsed.bid == Decimal("1.1000")
    assert parsed.ask == Decimal("1.1002")


@pytest.mark.parametrize("value", [1.1, True, -1, "1.5", ""])
def test_invalid_timestamp_is_rejected(value):
    row = tick(1)
    row["timestamp"] = value
    with pytest.raises(ValueError, match="timestamp"):
        dukascopy_tick_from_row(row)


@pytest.mark.parametrize("field", ["bidPrice", "askPrice"])
def test_binary_float_price_is_rejected(field):
    row = tick(1)
    row[field] = 1.1001
    with pytest.raises(ValueError, match="exact Decimal"):
        dukascopy_tick_from_row(row)


def test_missing_provider_field_is_rejected():
    with pytest.raises(ValueError, match="missing required fields"):
        dukascopy_tick_from_row({"timestamp": "1", "bidPrice": "1.1"})


def test_crossed_tick_is_rejected():
    with pytest.raises(ValueError, match="ask cannot be below bid"):
        dukascopy_tick_from_row(tick(1, bid="1.1003", ask="1.1002"))


def test_tick_series_rejects_decreasing_time_but_allows_equal_millisecond_order():
    ticks = dukascopy_tick_series([tick(10), tick(10, "1.1001", "1.1003")])
    assert len(ticks) == 2
    with pytest.raises(ValueError, match="non-decreasing"):
        dukascopy_tick_series([tick(11), tick(10)])


def test_tick_series_rejects_empty_and_non_iterable_inputs():
    with pytest.raises(ValueError, match="cannot be empty"):
        dukascopy_tick_series([])
    with pytest.raises(ValueError, match="iterable"):
        dukascopy_tick_series({"timestamp": "1"})


def test_aggregates_bid_ask_ohlc_from_actual_ticks():
    bars = aggregate_dukascopy_ticks([
        tick(0, "1.1000", "1.1002"),
        tick(30_000, "1.1010", "1.1013"),
        tick(60_000, "1.0990", "1.0992"),
        tick(899_999, "1.1005", "1.1007"),
    ])
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timestamp == "1970-01-01T00:00:00+00:00"
    assert (bar.bid_open, bar.bid_high, bar.bid_low, bar.bid_close) == (
        Decimal("1.1000"), Decimal("1.1010"), Decimal("1.0990"), Decimal("1.1005"))
    assert (bar.ask_open, bar.ask_high, bar.ask_low, bar.ask_close) == (
        Decimal("1.1002"), Decimal("1.1013"), Decimal("1.0992"), Decimal("1.1007"))


def test_interval_boundary_starts_new_bar():
    bars = aggregate_dukascopy_ticks([tick(899_999), tick(900_000, "1.1010", "1.1012")])
    assert len(bars) == 2
    assert bars[1].timestamp == "1970-01-01T00:15:00+00:00"


def test_missing_intervals_are_not_filled():
    bars = aggregate_dukascopy_ticks([tick(0), tick(3_600_000)])
    assert len(bars) == 2
    assert bars[1].timestamp == "1970-01-01T01:00:00+00:00"


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "15"])
def test_timeframe_must_be_positive_integer(value):
    with pytest.raises(ValueError, match="timeframe_minutes"):
        aggregate_dukascopy_ticks([tick(0)], timeframe_minutes=value)


def test_same_millisecond_ticks_preserve_input_order_for_open_and_close():
    bars = aggregate_dukascopy_ticks([
        tick(100, "1.1000", "1.1002"),
        tick(100, "1.1005", "1.1007"),
    ])
    assert bars[0].bid_open == Decimal("1.1000")
    assert bars[0].bid_close == Decimal("1.1005")


def test_quote_volume_fields_are_not_misrepresented_as_trade_volume():
    bar = aggregate_dukascopy_ticks([tick(0)])[0]
    assert isinstance(bar, object)
    assert not hasattr(bar, "volume")
