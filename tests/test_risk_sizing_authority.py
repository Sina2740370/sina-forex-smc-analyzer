"""Acceptance tests for the immutable Risk Agent sizing authority."""
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from engine.mas.risk_agent import AccountState, RISK_RULES, evaluate_risk


def test_risk_agent_returns_immutable_auditable_sizing_result():
    verdict = evaluate_risk("EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
                            acct=AccountState(equity=10_000),
                            fx_timestamp="2026-11-18T16:00:00+00:00")

    assert verdict.approved
    sizing = verdict.sizing_result
    assert sizing is not None and sizing.approved
    assert sizing.symbol == "EURUSD" and sizing.direction == "BUY"
    assert Decimal(sizing.actual_risk) <= Decimal(sizing.requested_risk)
    assert Decimal(sizing.lots) == Decimal("0.5")
    assert Decimal(sizing.requested_risk) == Decimal("50.0")
    assert RISK_RULES["risk_per_trade_pct"] == 0.5
    assert RISK_RULES["max_daily_loss_pct"] == 2.0
    assert sizing.spec_version and sizing.risk_config_hash
    assert sizing.fx_rate == "" and sizing.fx_timestamp == ""
    with pytest.raises(FrozenInstanceError):
        sizing.lots = "99"


def test_non_usd_quote_sizing_records_conversion_price_and_timestamp():
    verdict = evaluate_risk("USDJPY", "SELL", 155.0, 155.2, 154.6, 155.0,
                            acct=AccountState(equity=10_000),
                            fx_timestamp="2026-11-18T16:00:00+00:00")

    assert verdict.approved and verdict.sizing_result is not None
    assert verdict.sizing_result.fx_rate == "155.0"
    assert verdict.sizing_result.fx_timestamp == "2026-11-18T16:00:00+00:00"
    assert Decimal(verdict.sizing_result.actual_risk) <= Decimal(verdict.sizing_result.requested_risk)


def test_execution_price_reauthorization_produces_fill_specific_sizing():
    account = AccountState(equity=10_000)
    at_signal = evaluate_risk("USDJPY", "SELL", 155.0, 155.2, 154.6, 155.0, acct=account)
    at_fill = evaluate_risk("USDJPY", "SELL", 156.0, 156.2, 155.4, 156.0, acct=account)

    assert at_signal.approved and at_fill.approved
    assert at_signal.sizing_result is not None and at_fill.sizing_result is not None
    assert at_signal.sizing_result.sizing_id != at_fill.sizing_result.sizing_id
    assert at_signal.sizing_result.entry_price == "155.0"
    assert at_fill.sizing_result.entry_price == "156.0"
    assert Decimal(at_fill.sizing_result.actual_risk) <= Decimal(at_fill.sizing_result.requested_risk)


def test_invalid_size_factor_is_rejected_by_risk_authority():
    for factor in (0.0, -0.1, 1.01, float("nan"), float("inf")):
        verdict = evaluate_risk("EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
                                size_factor=factor)
        assert not verdict.approved
        assert verdict.sizing_result is None
        assert any(g.gate == "R-SIZE" and not g.passed for g in verdict.gates)


def test_hard_risk_lock_remains_independent_of_size_factor():
    account = AccountState(equity=10_000, consecutive_losses=3, risk_lock_active=True)
    verdict = evaluate_risk("EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
                            acct=account, size_factor=0.1)
    assert not verdict.approved
    assert verdict.sizing_result is None
    assert any(g.gate == "R-ADDTOLOSS" and not g.passed for g in verdict.gates)

@pytest.mark.parametrize(
    "account_kwargs,failed_gate",
    [
        ({"open_risk_pct": 2.6}, "R-CONC"),
        ({"daily_loss_pct": 1.6}, "R-DAY"),
        ({"daily_loss_pct": 2.1}, "R-DAY"),
        ({"weekly_loss_pct": 5.6}, "R-WK"),
    ],
)
def test_risk_authority_includes_new_trade_risk_in_exposure_budgets(account_kwargs, failed_gate):
    account = AccountState(equity=10_000, **account_kwargs)
    verdict = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000, acct=account
    )
    assert not verdict.approved
    assert any(g.gate == failed_gate and not g.passed for g in verdict.gates)