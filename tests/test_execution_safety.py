"""Boundary tests for simulated execution and realized-R calculations."""
import numpy as np
import pandas as pd
import pytest

from engine.core.config import EngineConfig
from engine.runner import check_exit, run_backtest
from engine.strategy.smc_topdown import Decision


@pytest.mark.parametrize(
    "args",
    [
        (0, 99, 102, 103, 98, 0.0),
        (True, 99, 102, 103, 98, 0.0),
        (1, float("nan"), 102, 103, 98, 0.0),
        (1, 99, float("inf"), 103, 98, 0.0),
        (1, 99, 102, float("nan"), 98, 0.0),
        (1, 99, 102, 103, 98, -0.1),
        (1, 102, 99, 103, 98, 0.0),
        (1, 99, 102, 98, 103, 0.0),
    ],
)
def test_check_exit_rejects_invalid_inputs(args):
    with pytest.raises(ValueError):
        check_exit(*args)


def test_check_exit_rejects_open_outside_bar_range():
    with pytest.raises(ValueError, match="بین low و high"):
        check_exit(1, 99, 102, 103, 98, 0.0, open_price=104)


def _data():
    return {
        "15m": pd.DataFrame({
            "time": pd.date_range("2026-01-05 12:00", periods=3, freq="15min", tz="UTC"),
            "open": [1.1, 1.1, 1.101],
            "high": [1.105, 1.105, 1.106],
            "low": [1.095, 1.095, 1.096],
            "close": [1.1, 1.101, 1.102],
            "volume": [100, 100, 100],
        })
    }


@pytest.mark.parametrize("target", [float("nan"), float("inf"), float("-inf"), 0])
def test_backtest_ignores_nonfinite_or_invalid_signal_prices(target):
    def invalid_decision(view, cfg, now):
        return Decision(direction="BUY", entry=1.1, sl=1.09, tp=target)

    trades = run_backtest(
        "EURUSD", _data(), EngineConfig(), analyze_fn=invalid_decision
    )
    assert trades == []


def test_backtest_ignores_unknown_signal_direction():
    def invalid_decision(view, cfg, now):
        return Decision(direction="HOLD", entry=1.1, sl=1.09, tp=1.12)

    assert run_backtest(
        "EURUSD", _data(), EngineConfig(), analyze_fn=invalid_decision
    ) == []


def test_backtest_rejects_unknown_symbol_and_missing_15m():
    with pytest.raises(ValueError, match="نماد"):
        run_backtest("FAKE", _data(), EngineConfig())
    with pytest.raises(ValueError, match="15m"):
        run_backtest("EURUSD", {"1h": _data()["15m"]}, EngineConfig())
