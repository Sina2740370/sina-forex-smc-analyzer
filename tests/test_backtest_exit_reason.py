"""Regression tests for explicit backtest end-of-data exit semantics."""
import pandas as pd

from engine.mas.backtest import _advance_position
from engine.mas.symbols import get_spec


def test_forced_end_of_data_close_is_classified_as_time_exit_not_take_profit():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"]),
        "high": [1.1010, 1.1010],
        "low": [1.0990, 1.0990],
        "close": [1.1000, 1.1005],
    })
    position = {
        "direction": "BUY", "last_idx": 0, "sl": 1.0900, "tp2": 1.1100,
        "entry": 1.1000,
    }
    result = _advance_position(position, frame, frame["time"].to_numpy(),
                              frame["time"].iloc[-1], get_spec("EURUSD"), force_exit=True)

    assert result is not None
    assert result[0] == 1.1005
    assert result[3] is None


def test_sell_position_stops_when_high_reaches_stop_loss():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"]),
        "high": [1.1005, 1.1015],
        "low": [1.0995, 1.0985],
        "close": [1.1000, 1.1005],
    })
    position = {
        "direction": "SELL", "last_idx": 0, "sl": 1.1010, "tp2": 1.0950,
        "entry": 1.1000,
    }

    result = _advance_position(
        position, frame, frame["time"].to_numpy(), frame["time"].iloc[-1],
        get_spec("EURUSD"),
    )

    assert result == (1.1010, frame["time"].iloc[1], 1, True)


def test_same_bar_stop_and_target_collision_is_resolved_conservatively():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"]),
        "high": [1.1010, 1.1120],
        "low": [1.0990, 1.0880],
        "close": [1.1000, 1.1000],
    })
    position = {
        "direction": "BUY", "last_idx": 0, "sl": 1.0900, "tp2": 1.1100,
        "entry": 1.1000,
    }

    result = _advance_position(
        position, frame, frame["time"].to_numpy(), frame["time"].iloc[-1],
        get_spec("EURUSD"),
    )

    assert result == (1.0900, frame["time"].iloc[1], 1, True)


def test_position_advance_does_not_consume_candles_after_decision_time():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"]),
        "high": [1.1010, 1.1120],
        "low": [1.0990, 1.1000],
        "close": [1.1000, 1.1100],
    })
    position = {
        "direction": "BUY", "last_idx": 0, "sl": 1.0900, "tp2": 1.1100,
        "entry": 1.1000,
    }

    result = _advance_position(
        position, frame, frame["time"].to_numpy(), frame["time"].iloc[0],
        get_spec("EURUSD"),
    )

    assert result is None
    assert position["last_idx"] == 0
