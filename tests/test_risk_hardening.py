"""Edge and regression tests for hardened engine.tools.risk helpers."""
import pytest

from engine.tools.risk import (
    breakeven_winrate,
    loss_per_lot_per_unit,
    margin_per_lot,
    position_size,
    trade_r,
)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_prices_balance_and_rr(bad):
    with pytest.raises(ValueError):
        position_size("EURUSD", 10_000, 1, bad, 1.09)
    with pytest.raises(ValueError):
        position_size("EURUSD", 10_000, 1, 1.10, bad)
    with pytest.raises(ValueError):
        position_size("EURUSD", bad, 1, 1.10, 1.09)
    with pytest.raises(ValueError):
        breakeven_winrate(bad)
    with pytest.raises(ValueError):
        trade_r("BUY", 1.10, 1.09, bad)


@pytest.mark.parametrize("entry, sl", [(-1, -2), (0, 0.1), (1.1, 0), (1.1, -1)])
def test_rejects_non_positive_prices(entry, sl):
    with pytest.raises(ValueError):
        position_size("EURUSD", 10_000, 1, entry, sl)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lot_step": 0},
        {"lot_step": float("nan")},
        {"min_lot": -0.01},
        {"max_lot": 0},
        {"min_lot": 0.015, "lot_step": 0.01},
        {"min_lot": 0.1, "max_lot": 0.01},
        {"contract": float("inf")},
        {"leverage": 0},
        {"max_margin_pct": 0},
    ],
)
def test_rejects_invalid_sizing_parameters(kwargs):
    with pytest.raises(ValueError):
        position_size("EURUSD", 10_000, 1, 1.1, 1.098, **kwargs)


@pytest.mark.parametrize(
    "symbol, price, expected",
    [
        ("EURUSD", 1.1, 100_000.0),
        ("USDJPY", 150.0, 100_000 / 150.0),
        ("USDCAD", 1.35, 100_000 / 1.35),
    ],
)
def test_usd_conversion_defaults(symbol, price, expected):
    assert loss_per_lot_per_unit(symbol, price) == pytest.approx(expected)


def test_explicit_quote_conversion_rate():
    assert loss_per_lot_per_unit(
        "USDJPY", 150, quote_to_account_rate=0.0067
    ) == pytest.approx(670.0)


def test_usd_quote_does_not_accept_unrelated_conversion_rate():
    with pytest.raises(ValueError):
        position_size(
            "EURUSD", 10_000, 1, 1.1, 1.098,
            quote_to_account_rate=0.9,
        )


def test_lot_size_floors_to_step_without_rounding_up():
    result = position_size(
        "EURUSD", 1234, 1, 1.1000, 1.0977,
        lot_step=0.01, min_lot=0.01,
    )
    assert result.ok
    assert result.lots == 0.05
    assert result.actual_risk <= result.risk_amount


@pytest.mark.parametrize("step", [0.1, 0.01, 0.001])
def test_volume_is_multiple_of_lot_step(step):
    result = position_size(
        "EURUSD", 10_000, 1, 1.10, 1.098,
        lot_step=step, min_lot=step,
    )
    assert result.ok
    assert result.lots == pytest.approx(0.5)
    assert result.lots / step == pytest.approx(round(result.lots / step))


def test_hard_risk_invariant_and_max_lot_cap():
    result = position_size(
        "EURUSD", 100_000, 2, 1.1, 1.099,
        lot_step=0.01, min_lot=0.01, max_lot=0.5,
    )
    assert result.ok
    assert result.lots == 0.5
    assert result.actual_risk <= result.risk_amount


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_conversion_rate(bad):
    with pytest.raises(ValueError):
        position_size(
            "USDJPY", 10_000, 1, 150, 149,
            quote_to_account_rate=bad,
        )


@pytest.mark.parametrize(
    "price, leverage",
    [(-1, 100), (1.1, 0), (float("nan"), 100)],
)
def test_margin_rejects_invalid_inputs(price, leverage):
    with pytest.raises(ValueError):
        margin_per_lot("EURUSD", price, leverage)


def test_trade_r_and_breakeven_reject_invalid_values():
    with pytest.raises(ValueError):
        trade_r("BUY", 1.1, 1.09, 0)
    with pytest.raises(ValueError):
        breakeven_winrate(0)


def test_margin_cap_keeps_volume_on_step():
    result = position_size(
        "EURUSD", 1_000, 1, 1.1000, 1.0998,
        leverage=100, lot_step=0.01, min_lot=0.01,
    )
    assert result.ok
    assert result.margin_limited
    assert result.lots == pytest.approx(0.45)
    assert result.actual_risk <= result.risk_amount


def test_tp_on_wrong_side_does_not_return_rr():
    result = position_size(
        "EURUSD", 10_000, 1, 1.10, 1.098, tp=1.096
    )
    assert result.ok
    assert result.rr is None
    assert "TP" in result.note


def test_margin_pct_warning_uses_unrounded_percentage():
    # 30.04% should trigger the warning even if presentation rounds to 30.0%.
    result = position_size(
        "EURUSD", 10_000, 100, 1.0, 0.99,
        leverage=100, max_margin_pct=50, max_lot=3.004,
        lot_step=0.001, min_lot=0.001,
    )
    assert result.ok
    assert result.margin_pct == pytest.approx(30.0)
    assert "30%" in result.note
