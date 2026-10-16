"""Edge-case tests for pure SMC primitives."""
import numpy as np
import pandas as pd
import pytest

from engine.core import smc


def candles(n=12):
    close = np.linspace(1.1, 1.2, n)
    return pd.DataFrame({
        "open": close - 0.001,
        "high": close + 0.002,
        "low": close - 0.002,
        "close": close,
    })


@pytest.mark.parametrize("period", [0, -1, True, 1.5, float("nan"), float("inf")])
def test_atr_and_ema_reject_invalid_period(period):
    with pytest.raises(ValueError):
        smc.atr(candles(), period)
    with pytest.raises(ValueError):
        smc.ema_slope(candles(), period)


@pytest.mark.parametrize("left,right", [(0, 2), (2, 0), (-1, 2), (1.5, 2), (True, 2)])
def test_swings_reject_invalid_confirmation_windows(left, right):
    with pytest.raises(ValueError):
        smc.swings(candles(), left=left, right=right)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookback": 0},
        {"lookback": -1},
        {"lookback": 1.5},
        {"fresh_bars": 0},
        {"fresh_bars": 40, "lookback": 40},
    ],
)
def test_structure_break_rejects_invalid_windows(kwargs):
    with pytest.raises(ValueError):
        smc.structure_break(candles(), **kwargs)


@pytest.mark.parametrize("lookback", [0, -1, 1.5, True])
def test_order_blocks_and_premium_discount_reject_invalid_lookback(lookback):
    with pytest.raises(ValueError):
        smc.order_blocks(candles(), lookback)
    with pytest.raises(ValueError):
        smc.premium_discount(candles(), lookback)


@pytest.mark.parametrize("ratio", [-0.1, float("nan"), float("inf"), True])
def test_fvg_rejects_invalid_gap_ratio(ratio):
    with pytest.raises(ValueError):
        smc.fvgs(candles(), min_gap_ratio=ratio)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookback": 0},
        {"recent": 0},
        {"lookback": 10, "recent": 10},
        {"lookback": 1.5},
    ],
)
def test_liquidity_sweep_rejects_invalid_windows(kwargs):
    with pytest.raises(ValueError):
        smc.liquidity_sweep(candles(30), **kwargs)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "1"])
def test_ob_distance_rejects_invalid_numbers(bad):
    with pytest.raises(ValueError):
        smc.ob_distance(bad, {"low": 1.0, "high": 2.0}, 1.0)
    with pytest.raises(ValueError):
        smc.ob_distance(1.5, {"low": 1.0, "high": 2.0}, bad)


def test_ob_distance_rejects_inverted_block():
    with pytest.raises(ValueError):
        smc.ob_distance(1.5, {"low": 2.0, "high": 1.0}, 1.0)


@pytest.mark.parametrize("size", [-1, float("nan"), float("inf"), "1"])
def test_sweep_multiplier_rejects_invalid_size(size):
    with pytest.raises(ValueError):
        smc.sweep_multiplier({"size": size}, 0.01)


@pytest.mark.parametrize("side", [0, 2, -2, True, 1.0, "BUY"])
def test_candle_confirmation_rejects_invalid_side(side):
    with pytest.raises(ValueError):
        smc.candle_confirmation(candles(), side)


def test_empty_premium_discount_returns_unclear():
    assert smc.premium_discount(candles(0)) == "UNCLEAR"
