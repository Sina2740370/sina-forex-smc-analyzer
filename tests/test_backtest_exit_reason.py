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