"""Regression tests for the hard consecutive-loss risk lock."""

import pytest

from engine.mas.risk_agent import AccountState, evaluate_risk


@pytest.mark.parametrize("size_factor", [1.0, 0.75, 0.5, 0.1])
def test_three_consecutive_losses_cannot_be_bypassed_by_size_factor(size_factor):
    account = AccountState(equity=10_000, consecutive_losses=3)

    verdict = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
        acct=account, size_factor=size_factor,
    )

    assert not verdict.approved
    assert verdict.lots == 0
    assert verdict.risk_amount == 0
    assert any(g.gate == "R-ADDTOLOSS" and not g.passed for g in verdict.gates)


def test_explicit_persistent_risk_lock_rejects_even_after_loss_counter_reset():
    account = AccountState(equity=10_000, consecutive_losses=0, risk_lock_active=True)

    verdict = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
        acct=account, size_factor=0.5,
    )

    assert not verdict.approved
    assert any(g.gate == "R-ADDTOLOSS" and not g.passed for g in verdict.gates)


def test_risk_lock_can_only_be_cleared_by_explicit_state_reset():
    locked = AccountState(equity=10_000, consecutive_losses=0, risk_lock_active=True)
    unlocked = AccountState(equity=10_000, consecutive_losses=0, risk_lock_active=False)

    locked_verdict = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000, acct=locked,
    )
    unlocked_verdict = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000, acct=unlocked,
    )

    assert not locked_verdict.approved
    assert unlocked_verdict.approved
