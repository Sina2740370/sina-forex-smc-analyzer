"""Regression tests for strict EngineConfig boundary validation."""
from dataclasses import replace

import pytest

from engine.core.config import EngineConfig


@pytest.mark.parametrize(
    "field",
    [
        "min_score", "risk_reward", "sl_atr1h_mult", "sl_ob_buffer",
        "min_risk_atr1h", "max_risk_atr4h", "ob_gate_atr4h",
        "fvg_min_gap_ratio", "w_htf_full", "w_htf_soft", "w_sweep",
        "w_sweep_against", "w_choch", "w_bos", "w_ob_in", "w_ob_near",
        "w_ob_mid", "w_pd", "w_fvg", "w_candle", "w_killzone",
        "w_confluence", "cooldown_hours", "max_entry_slip_r",
    ],
)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "0.5"])
def test_rejects_non_numeric_or_non_finite_parameters(field, bad):
    with pytest.raises(ValueError):
        replace(EngineConfig(), **{field: bad})


@pytest.mark.parametrize(
    "field",
    [
        "sweep_lookback", "break_lookback", "fvg_lookback",
        "ob_lookback", "pd_lookback", "scan_every_bars",
    ],
)
@pytest.mark.parametrize("bad", [0, -1, True, 1.5, float("nan"), float("inf"), "10"])
def test_rejects_invalid_integer_parameters(field, bad):
    with pytest.raises(ValueError):
        replace(EngineConfig(), **{field: bad})


@pytest.mark.parametrize(
    "field,value",
    [
        ("risk_reward", 0),
        ("sl_atr1h_mult", 0),
        ("min_risk_atr1h", -0.1),
        ("max_risk_atr4h", 0),
        ("sl_ob_buffer", -0.1),
        ("ob_gate_atr4h", -0.1),
        ("fvg_min_gap_ratio", -0.1),
        ("cooldown_hours", -0.1),
        ("max_entry_slip_r", -0.1),
        ("w_sweep", -0.1),
        ("min_score", -0.1),
    ],
)
def test_rejects_out_of_range_parameters(field, value):
    with pytest.raises(ValueError):
        replace(EngineConfig(), **{field: value})


def test_default_config_remains_valid_and_hash_is_stable():
    cfg = EngineConfig()
    assert len(cfg.hash()) == 10
    assert cfg.hash() == EngineConfig().hash()


def test_allows_zero_for_non_negative_optional_thresholds():
    cfg = replace(EngineConfig(), sl_ob_buffer=0, cooldown_hours=0, max_entry_slip_r=0)
    assert cfg.sl_ob_buffer == 0
    assert cfg.cooldown_hours == 0
    assert cfg.max_entry_slip_r == 0
