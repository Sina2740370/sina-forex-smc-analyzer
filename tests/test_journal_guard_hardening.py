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

    # Keep the public return contract explicit: rounded numeric metrics are floats,
    # while state/reasons/counts retain their documented shapes.
    assert set(result) == {
        "state", "today_r", "trades_today", "consecutive_losses", "reasons"
    }
    assert result["today_r"] == pytest.approx(-2.1)
    assert result["state"] == "warn"
    assert result["trades_today"] == 2
    assert result["consecutive_losses"] == 2
    assert isinstance(result["reasons"], list)
    assert result["reasons"]


def test_drawdown_guard_uses_decimal_boundary_for_warning_threshold():
    result = drawdown_guard([1.0, -0.7], max_dd_r=1.0)

    # Ensure the complete output structure and all calculated metrics agree.
    assert set(result) == {"state", "current_dd_r", "max_dd_r", "total_r"}
    assert result["current_dd_r"] == pytest.approx(0.7)
    assert result["max_dd_r"] == pytest.approx(0.7)
    assert result["total_r"] == pytest.approx(0.3)
    assert result["state"] == "warn"


def test_empty_guard_inputs_remain_safe():
    daily = daily_guard([])
    drawdown = drawdown_guard([])

    assert daily["state"] == "ok"
    assert daily["today_r"] == pytest.approx(0.0)
    assert daily["trades_today"] == 0
    assert daily["consecutive_losses"] == 0
    assert daily["reasons"] == []

    assert drawdown["state"] == "ok"
    assert drawdown["current_dd_r"] == pytest.approx(0.0)
    assert drawdown["max_dd_r"] == pytest.approx(0.0)
    assert drawdown["total_r"] == pytest.approx(0.0)
