from decimal import Decimal

import pytest

from engine.mas.backtest_adapter import BacktestLedgerAdapter


TS = "2025-01-06T10:00:00+00:00"


def make_adapter():
    return BacktestLedgerAdapter.create(
        initial_balance="10000", timestamp=TS,
        account_namespace="test-account",
    )


def open_facts(symbol, identity):
    return {
        "symbol": symbol, "side": "BUY", "quantity": "0.10",
        "entry": "1.1000", "stop": "1.0900",
        "approved_risk": "100", "actual_risk": "100",
        "decision_timestamp": TS, "candidate_identity": identity,
    }


def test_backtest_adapter_uses_one_shared_multi_symbol_ledger():
    adapter = make_adapter()
    first = adapter.commit_open_position(**open_facts("EURUSD", "a"))
    second = adapter.commit_open_position(**open_facts("GBPUSD", "b"))

    positions = adapter.ledger.snapshot()["open_positions"]
    assert set(positions) == {first, second}
    assert adapter.ledger.sequence == 2
    assert adapter.ledger.equity == Decimal("10000")


def test_event_batch_order_is_independent_of_symbol_input_order():
    left = make_adapter()
    right = make_adapter()
    a1, e1 = left.build_open_event(**open_facts("EURUSD", "a"))
    a2, e2 = left.build_open_event(**open_facts("GBPUSD", "b"))
    b1, f1 = right.build_open_event(**open_facts("EURUSD", "a"))
    b2, f2 = right.build_open_event(**open_facts("GBPUSD", "b"))

    left.commit_events([e1, e2])
    right.commit_events([f2, f1])

    assert (a1, a2) == (b1, b2)
    assert left.ledger.snapshot() == right.ledger.snapshot()


def test_float_money_is_rejected_at_backtest_boundary():
    adapter = make_adapter()
    with pytest.raises(ValueError, match="exact Decimal"):
        adapter.build_open_event(**{**open_facts("EURUSD", "a"), "quantity": 0.1})


def test_backtest_adapter_rejects_invalid_batch_without_mutating_ledger():
    adapter = make_adapter()
    _, event = adapter.build_open_event(**open_facts("EURUSD", "a"))
    invalid = dict(event)
    invalid["event_id"] = "bad-event"
    invalid["sequence"] = 1
    invalid["actual_risk"] = "101"

    before = adapter.ledger.snapshot()
    with pytest.raises(ValueError):
        adapter.commit_events([event, invalid])
    assert adapter.ledger.snapshot() == before


def test_duplicate_exact_event_batch_is_idempotent():
    adapter = make_adapter()
    _, event = adapter.build_open_event(**open_facts("EURUSD", "a"))
    assert adapter.commit_events([event]) is True
    snapshot = adapter.ledger.snapshot()
    assert adapter.commit_events([event]) is False
    assert adapter.ledger.snapshot() == snapshot


def test_period_roll_is_explicit_and_idempotent_for_same_period():
    adapter = make_adapter()
    assert adapter.roll_period(TS) is False
    assert adapter.ledger.sequence == 0
