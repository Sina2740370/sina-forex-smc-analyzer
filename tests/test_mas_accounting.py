"""Contract tests for the shared Paper/Backtest ledger event boundary."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from engine.mas.accounting import (
    account_state_from_ledger,
    apply_ledger_event,
    apply_ledger_events,
    mark_to_market_event,
    position_closed_event,
    position_opened_event,
    position_settlement_events,
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

def test_risk_gate_projection_uses_ledger_as_single_source_of_truth():
    ledger = RiskLedger("10000")
    opened = open_event(ledger)
    apply_ledger_event(ledger, opened)
    closed = position_closed_event(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        execution_id="loss-1", quantity="1", gross_pnl="-20",
        commission="1", timestamp="2026-11-17T00:00:00+02:00",
    )
    apply_ledger_event(ledger, closed)

    account = account_state_from_ledger(ledger, imminent_news_minutes=30)
    assert account.equity == 9979.0
    assert account.open_trades == 0
    assert account.open_risk_pct == 0.0
    assert account.daily_loss_pct == pytest.approx(0.21)
    assert account.weekly_loss_pct == pytest.approx(0.21)
    assert account.consecutive_losses == 1
    assert account.per_symbol_open == {}
    assert account.imminent_news_minutes == 30

def test_negative_swap_is_accounted_as_financing_credit():
    ledger = RiskLedger("10000")
    apply_ledger_event(ledger, open_event(ledger))
    close = position_closed_event(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        execution_id="swap-credit-1", quantity="1", gross_pnl="0",
        commission="1", swap="-2", other_fees="0", timestamp=NOW,
    )

    apply_ledger_event(ledger, close)

    # A negative swap is a financing credit, not an invalid negative fee.
    assert ledger.balance == Decimal("10001")
    assert ledger.realized_pnl_lifetime == Decimal("1")
    assert ledger.fees_lifetime == Decimal("-1")
    assert ledger.daily_realized_net_pnl == Decimal("1")

def test_daily_and_weekly_loss_include_unrealized_drawdown():
    ledger = RiskLedger("10000")
    apply_ledger_event(
        ledger,
        mark_to_market_event(
            ledger, mark_id="floating-loss-350", unrealized_pnl="-350",
            timestamp=NOW,
        ),
    )

    assert ledger.balance == Decimal("10000")
    assert ledger.equity == Decimal("9650")
    assert ledger.daily_loss_pct == Decimal("3.50")
    assert ledger.weekly_loss_pct == Decimal("3.50")


def test_daily_rollover_uses_equity_as_new_period_baseline():
    ledger = RiskLedger("10000")
    apply_ledger_event(
        ledger,
        mark_to_market_event(
            ledger, mark_id="carry-loss-100", unrealized_pnl="-100",
            timestamp=NOW,
        ),
    )

    assert roll_account_period(ledger, "2026-11-17T22:00:00Z") is True
    assert ledger.daily_start_balance == Decimal("9900")
    assert ledger.daily_loss_pct == Decimal("0")

    apply_ledger_event(
        ledger,
        mark_to_market_event(
            ledger, mark_id="carry-loss-200", unrealized_pnl="-200",
            timestamp="2026-11-17T22:01:00Z",
        ),
    )
    assert ledger.daily_loss_pct == Decimal("100") / Decimal("9900") * Decimal("100")

def test_atomic_close_and_post_settlement_mark_batch_is_persisted_and_retry_safe(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "ledger-batch.sqlite")
    ledger = RiskLedger("10000")
    store.create("paper-account", ledger)

    apply_ledger_event(ledger, open_event(ledger), store=store, ledger_id="paper-account")
    second_open = position_opened_event(
        ledger, position_id="paper:GBPUSD:2026-11-16T22:00:00+00:00",
        symbol="GBPUSD", side="BUY", quantity="1", entry="1.2500",
        stop="1.2490", approved_risk="100", actual_risk="98", timestamp=NOW,
    )
    apply_ledger_event(ledger, second_open, store=store, ledger_id="paper-account")
    initial_mark = mark_to_market_event(
        ledger, mark_id="before-close", unrealized_pnl="-50", timestamp=NOW,
    )
    apply_ledger_event(ledger, initial_mark, store=store, ledger_id="paper-account")

    batch = position_settlement_events(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        execution_id="exit-with-reprice", quantity="1", gross_pnl="50",
        commission="1", timestamp=NOW, remaining_unrealized_pnl="-30",
        mark_id="after-close",
    )
    assert [event["sequence"] for event in batch] == [4, 5]
    assert [event["type"] for event in batch] == [
        "PositionClosed", "MarkToMarketUpdated",
    ]

    assert apply_ledger_events(
        ledger, batch, store=store, ledger_id="paper-account"
    ) is True
    assert ledger.balance == Decimal("10049")
    assert ledger.unrealized_pnl == Decimal("-30")
    assert ledger.equity == Decimal("10019")
    assert ledger.open_trades == 1 if hasattr(ledger, "open_trades") else len(ledger.snapshot()["open_positions"]) == 1

    # Replaying the exact same complete batch must be idempotent.
    assert apply_ledger_events(
        ledger, batch, store=store, ledger_id="paper-account"
    ) is False
    assert store.load("paper-account").snapshot() == ledger.snapshot()


def test_failed_batch_does_not_publish_partial_close_or_mark():
    ledger = RiskLedger("10000")
    apply_ledger_event(ledger, open_event(ledger))
    close = position_closed_event(
        ledger, position_id="paper:EURUSD:2026-11-16T22:00:00+00:00",
        execution_id="close-before-invalid-mark", quantity="1",
        gross_pnl="10", timestamp=NOW,
    )
    invalid_mark = {
        "event_id": "mas:mark:invalid-after-close",
        "sequence": close["sequence"] + 1,
        "timestamp": close["timestamp"],
        "type": "MarkToMarketUpdated",
        "unrealized_pnl": "NaN",
        "currency": ledger.base_currency,
    }
    before = ledger.snapshot()

    with pytest.raises(ValueError):
        apply_ledger_events(ledger, [close, invalid_mark])

    assert ledger.snapshot() == before
