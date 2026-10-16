"""Regression tests for fail-closed journal risk guards."""
import pytest

from engine.tools.journal import daily_guard, drawdown_guard


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "1.0"])
def test_daily_guard_rejects_invalid_r_values(bad):
    with pytest.raises(ValueError):
        daily_guard([0.5, bad])


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "3"])
def test_daily_guard_rejects_invalid_limits(bad):
    with pytest.raises(ValueError):
        daily_guard([0.2], max_daily_loss_r=bad)
    with pytest.raises(ValueError):
        daily_guard([0.2], max_trades=bad)
    with pytest.raises(ValueError):
        daily_guard([0.2], max_consec_losses=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "0.2"])
def test_drawdown_guard_rejects_invalid_r_values(bad):
    with pytest.raises(ValueError):
        drawdown_guard([1.0, bad])


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "10"])
def test_drawdown_guard_rejects_invalid_limit(bad):
    with pytest.raises(ValueError):
        drawdown_guard([0.2], max_dd_r=bad)


def test_daily_guard_uses_decimal_boundary_for_warning_threshold():
    result = daily_guard([-1.2, -0.9], max_daily_loss_r=3.0)
    assert result["today_r"] == -2.1
    assert result["state"] == "warn"


def test_drawdown_guard_uses_decimal_boundary_for_warning_threshold():
    result = drawdown_guard([1.0, -0.7], max_dd_r=1.0)
    assert result["current_dd_r"] == 0.7
    assert result["state"] == "warn"


def test_empty_guard_inputs_remain_safe():
    assert daily_guard([])["state"] == "ok"
    assert drawdown_guard([])["state"] == "ok"
