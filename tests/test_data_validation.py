"""Boundary tests for provider-level OHLC validation."""
import numpy as np
import pandas as pd
import pytest

from engine.data.providers import TopDownView, resample_ohlc


def candle_frame(**updates):
    row = {
        "time": pd.Timestamp("2026-01-05 12:00", tz="UTC"),
        "open": 1.1000,
        "high": 1.1020,
        "low": 1.0980,
        "close": 1.1010,
        "volume": 100.0,
    }
    row.update(updates)
    return pd.DataFrame([row])


def test_valid_closed_candle_passes_provider_validation():
    frame = candle_frame()
    assert resample_ohlc(frame, "15m").equals(frame)
    view = TopDownView({"15m": frame})
    assert len(view.at(pd.Timestamp("2026-01-05 12:15", tz="UTC"))["15m"]) == 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume"])
def test_rejects_non_finite_candle_values(column, bad):
    with pytest.raises(ValueError):
        resample_ohlc(candle_frame(**{column: bad}), "15m")


@pytest.mark.parametrize(
    "updates",
    [
        {"open": 0},
        {"high": 1.099},
        {"low": 1.102},
        {"high": 1.097},
        {"volume": -1},
    ],
)
def test_rejects_impossible_ohlc_and_negative_volume(updates):
    with pytest.raises(ValueError):
        resample_ohlc(candle_frame(**updates), "15m")


def test_rejects_timezone_naive_candles():
    frame = candle_frame()
    frame["time"] = pd.to_datetime(["2026-01-05 12:00"])
    with pytest.raises(ValueError, match="timezone-aware"):
        TopDownView({"15m": frame})


def test_rejects_duplicate_or_unsorted_candle_times():
    frame = pd.concat([candle_frame(), candle_frame()], ignore_index=True)
    with pytest.raises(ValueError, match="timestamp تکراری"):
        TopDownView({"15m": frame})

    frame = pd.concat(
        [
            candle_frame(time=pd.Timestamp("2026-01-05 12:15", tz="UTC")),
            candle_frame(time=pd.Timestamp("2026-01-05 12:00", tz="UTC")),
        ],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="صعودی مرتب"):
        TopDownView({"15m": frame})


def test_rejects_unknown_timeframe():
    with pytest.raises(ValueError, match="تایم‌فریم"):
        resample_ohlc(candle_frame(), "2h")
