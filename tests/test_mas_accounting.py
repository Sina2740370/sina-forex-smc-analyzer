"""Contract tests for the shared Paper/Backtest ledger event boundary."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from engine.mas.accounting import (
    apply_ledger_event,
    mark_to_market_event,
    position_closed_event,
    position_opened_event,
    roll_account_period,
)
from engine.mas.ledger import RiskLedger
from engine.mas.ledger_store import SQLiteLedgerStore


NOW = datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc)


def open_event(ledger, timestamp=NOW):
    return position_opened_event(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        symbol="EURUSD", side="BUY", quantity="1.00", entry="1.1000",
        stop="1.0990", approved_risk="100", actual_risk="99",
        timestamp=timestamp,
    )


def test_open_and_close_events_have_stable_ids_and_contiguous_sequences():
    ledger = RiskLedger("10000")
    opened = open_event(ledger)
    assert opened["event_id"] == "mas:position:paper:EURUSD:2026-11-16T22:00:00+00:00:opened"
    assert opened["sequence"] == 1
    apply_ledger_event(ledger, opened)

    closed = position_closed_event(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        execution_id="exit-1", quantity="1", gross_pnl="-20",
        commission="1", timestamp="2026-11-17T00:00:00+02:00",
    )
    assert closed["type"] == "PositionClosed"
    assert closed["sequence"] == 2
    assert closed["timestamp"] == "2026-11-16T22:00:00+00:00"
    apply_ledger_event(ledger, closed)
    assert ledger.balance == Decimal("9979")


def test_partial_close_then_final_close_books_each_fill_once():
    ledger = RiskLedger("10000")
    apply_ledger_event(ledger, open_event(ledger))
    partial = position_closed_event(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        execution_id="fill-1", quantity="0.4", gross_pnl="8",
        commission="0.4", timestamp=NOW,
    )
    assert partial["type"] == "PositionPartiallyClosed"
    apply_ledger_event(ledger, partial)
    final = position_closed_event(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        execution_id="fill-2", quantity="0.6", gross_pnl="-12",
        commission="0.6", timestamp=NOW,
    )
    assert final["type"] == "PositionClosed"
    apply_ledger_event(ledger, final)
    assert ledger.balance == Decimal("9995")
    assert ledger.fees_lifetime == Decimal("1.0")
    assert ledger.consecutive_losses == 1


def test_duplicate_event_is_idempotent_and_divergent_retry_is_rejected():
    ledger = RiskLedger("10000")
    event = open_event(ledger)
    assert apply_ledger_event(ledger, event) is True
    assert apply_ledger_event(ledger, event) is False
    altered = dict(event, actual_risk="98")
    with pytest.raises(ValueError, match="different payload"):
        apply_ledger_event(ledger, altered)


def test_failed_validation_does_not_mutate_callers_ledger():
    ledger = RiskLedger("10000")
    event = open_event(ledger)
    event["actual_risk"] = "101"
    before = ledger.snapshot()
    with pytest.raises(ValueError, match="exceeds approved"):
        apply_ledger_event(ledger, event)
    assert ledger.snapshot() == before


def test_mark_to_market_changes_equity_not_balance():
    ledger = RiskLedger("10000")
    event = mark_to_market_event(
        ledger, mark_id="cycle-1", unrealized_pnl="-25", timestamp=NOW,
    )
    apply_ledger_event(ledger, event)
    assert ledger.balance == Decimal("10000")
    assert ledger.equity == Decimal("9975")


def test_period_roll_is_idempotent_and_uses_shared_period_contract():
    ledger = RiskLedger("10000")
    assert roll_account_period(ledger, "2026-11-15T22:00:00Z") is True
    assert ledger.daily_period_id == "2026-11-16"
    assert ledger.weekly_period_id == "2026-W47"
    assert roll_account_period(ledger, "2026-11-15T22:00:00Z") is False


def test_persistence_failure_does_not_publish_candidate_state(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "ledger.sqlite")
    ledger = RiskLedger("10000")
    store.create("paper-account", ledger)
    event = open_event(ledger)
    with pytest.raises(ValueError, match="unknown ledger_id"):
        apply_ledger_event(
            ledger, event, store=store, ledger_id="wrong-account",
        )
    assert ledger.sequence == 0
    assert store.load("paper-account").sequence == 0


def test_successful_persisted_apply_recovers_identical_snapshot(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "ledger.sqlite")
    ledger = RiskLedger("10000")
    store.create("paper-account", ledger)
    event = open_event(ledger)
    apply_ledger_event(ledger, event, store=store, ledger_id="paper-account")
    assert store.load("paper-account").snapshot() == ledger.snapshot()


def test_persisted_exact_retry_is_idempotent(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "ledger.sqlite")
    ledger = RiskLedger("10000")
    store.create("paper-account", ledger)
    event = open_event(ledger)
    assert apply_ledger_event(
        ledger, event, store=store, ledger_id="paper-account"
    ) is True
    assert apply_ledger_event(
        ledger, event, store=store, ledger_id="paper-account"
    ) is False
    assert store.load("paper-account").snapshot() == ledger.snapshot()
