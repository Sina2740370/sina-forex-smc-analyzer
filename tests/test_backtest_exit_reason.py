"""Regression tests for explicit backtest end-of-data exit semantics."""
import pandas as pd

from engine.mas.backtest import _advance_position
from engine.mas.symbols import get_spec, realized_pnl_usd


def test_forced_end_of_data_close_is_classified_as_time_exit_not_take_profit():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z", "2026-01-01T00:30:00Z"]),
        "high": [1.1010, 1.1010, 1.1010],
        "low": [1.0990, 1.0990, 1.0990],
        "close": [1.1000, 1.1005, 1.1008],
    })
    position = {
        "direction": "BUY", "last_idx": 0, "sl": 1.0900, "tp2": 1.1100,
        "entry": 1.1000,
    }
    result = _advance_position(position, frame, frame["time"].to_numpy(),
                              frame["time"].iloc[-1], get_spec("EURUSD"), force_exit=True)

    assert result is not None
    assert result[0] == 1.1005
    assert result[1] == frame["time"].iloc[1]
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
        position, frame, frame["time"].to_numpy(), frame["time"].iloc[-1] + pd.Timedelta(minutes=15),
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


def test_buy_stop_gap_fills_at_worse_bar_open():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"]),
        "open": [1.1000, 1.0850],
        "high": [1.1010, 1.0950],
        "low": [1.0990, 1.0800],
        "close": [1.1000, 1.0900],
    })
    position = {
        "direction": "BUY", "last_idx": 0, "sl": 1.0900, "tp2": 1.1100,
        "entry": 1.1000,
    }
    result = _advance_position(
        position, frame, frame["time"].to_numpy(), frame["time"].iloc[-1],
        get_spec("EURUSD"),
    )
    assert result == (1.0850, frame["time"].iloc[1], 1, True)


def test_sell_stop_gap_fills_at_worse_bar_open():
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"]),
        "open": [1.1000, 1.1050],
        "high": [1.1005, 1.1100],
        "low": [1.0995, 1.1000],
        "close": [1.1000, 1.1055],
    })
    position = {
        "direction": "SELL", "last_idx": 0, "sl": 1.1010, "tp2": 1.0900,
        "entry": 1.1000,
    }
    result = _advance_position(
        position, frame, frame["time"].to_numpy(), frame["time"].iloc[-1],
        get_spec("EURUSD"),
    )
    assert result == (1.1050, frame["time"].iloc[1], 1, True)


def test_non_usd_quote_currency_pnl_uses_exit_conversion_rate():
    buy = realized_pnl_usd("USDCAD", 1.25, 1.20, "BUY", 1.0)
    sell = realized_pnl_usd("USDCAD", 1.20, 1.25, "SELL", 1.0)
    assert abs(buy - (-4166.666666666667)) < 1e-8
    assert abs(sell - (-4000.0)) < 1e-8
