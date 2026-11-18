"""Regression tests for the pure MAS risk ledger."""
from decimal import Decimal

import pytest

from engine.mas.ledger import RiskLedger
from engine.mas.ledger_store import SQLiteLedgerStore


def event(seq, event_type, event_id=None, timestamp=None, **fields):
    return {
        "sequence": seq,
        "event_id": event_id or f"evt-{seq}",
        "timestamp": timestamp or f"2026-11-16T10:{seq:02d}:00+00:00",
        "type": event_type,
        **fields,
    }


def opened(ledger, **overrides):
    values = {
        "position_id": "p-1", "symbol": "EURUSD", "side": "BUY",
        "quantity": "1.0", "entry": "1.1000", "stop": "1.0990",
        "approved_risk": "100", "actual_risk": "99",
    }
    values.update(overrides)
    ledger.apply(event(1, "PositionOpened", **values))


def test_open_reserves_risk_but_does_not_book_loss():
    ledger = RiskLedger("10000")
    opened(ledger)
    assert ledger.balance == Decimal("10000")
    assert ledger.daily_realized_net_pnl == 0
    assert ledger.weekly_realized_net_pnl == 0
    assert ledger.open_risk_amount == Decimal("99")
    assert ledger.daily_loss_pct == 0


def test_loss_is_based_on_realized_net_pnl_not_initial_risk():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "PositionClosed", position_id="p-1", quantity="1",
                       gross_pnl="-25", commission="2", swap="1",
                       other_fees="2"))
    assert ledger.balance == Decimal("9970")
    assert ledger.daily_realized_net_pnl == Decimal("-30")
    assert ledger.weekly_realized_net_pnl == Decimal("-30")
    assert ledger.daily_loss_pct == Decimal("0.30")
    assert ledger.weekly_loss_pct == Decimal("0.30")
    assert ledger.fees_lifetime == Decimal("5")
    assert ledger.open_risk_amount == 0
    assert ledger.consecutive_losses == 1


def test_profitable_close_updates_balance_and_resets_loss_streak():
    ledger = RiskLedger("10000")
    ledger.consecutive_losses = 2
    opened(ledger)
    ledger.apply(event(2, "PositionClosed", position_id="p-1", quantity="1",
                       gross_pnl="50"))
    assert ledger.balance == Decimal("10050")
    assert ledger.daily_realized_net_pnl == Decimal("50")
    assert ledger.daily_loss_pct == 0
    assert ledger.consecutive_losses == 0


def test_partial_close_reduces_reserved_risk_without_incrementing_streak():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "PositionPartiallyClosed", position_id="p-1",
                       quantity="0.4", gross_pnl="-8"))
    assert ledger.open_risk_amount == Decimal("59.4")
    assert ledger.consecutive_losses == 0
    assert ledger.daily_realized_net_pnl == Decimal("-8")


def test_final_close_uses_aggregate_net_result_after_partial_fills():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "PositionPartiallyClosed", position_id="p-1",
                       quantity="0.4", gross_pnl="-8"))
    ledger.apply(event(3, "PositionClosed", position_id="p-1",
                       quantity="0.6", gross_pnl="-4"))
    assert ledger.consecutive_losses == 1
    assert ledger.daily_realized_net_pnl == Decimal("-12")
    assert ledger.open_risk_amount == 0


def test_partial_close_with_fees_books_each_fee_once():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "PositionPartiallyClosed", position_id="p-1",
                       quantity="0.5", gross_pnl="10", commission="1"))
    ledger.apply(event(3, "PositionClosed", position_id="p-1",
                       quantity="0.5", gross_pnl="10", commission="1"))
    assert ledger.balance == Decimal("10018")
    assert ledger.fees_lifetime == Decimal("2")
    assert ledger.realized_pnl_lifetime == Decimal("18")


def test_duplicate_event_id_is_idempotent():
    ledger = RiskLedger("10000")
    opened(ledger)
    close = event(2, "PositionClosed", event_id="close-unique",
                  position_id="p-1", quantity="1", gross_pnl="-10")
    assert ledger.apply(close) is True
    snapshot = ledger.snapshot()
    assert ledger.apply(close) is False
    assert ledger.snapshot() == snapshot


def test_failed_event_is_atomic_and_does_not_consume_sequence():
    ledger = RiskLedger("10000")
    opened(ledger)
    before = ledger.snapshot()
    bad = event(2, "PositionClosed", position_id="p-1",
                quantity="2", gross_pnl="-50")
    with pytest.raises(ValueError, match="exceeds remaining"):
        ledger.apply(bad)
    assert ledger.snapshot() == before
    good = event(2, "PositionClosed", position_id="p-1",
                 quantity="1", gross_pnl="5")
    assert ledger.apply(good)


def test_unknown_position_cannot_be_settled():
    ledger = RiskLedger("10000")
    with pytest.raises(ValueError, match="unknown position"):
        ledger.apply(event(1, "PositionClosed", position_id="missing",
                           quantity="1", gross_pnl="-10"))
    assert ledger.sequence == 0


def test_full_close_must_match_remaining_quantity():
    ledger = RiskLedger("10000")
    opened(ledger)
    with pytest.raises(ValueError, match="full remaining quantity"):
        ledger.apply(event(2, "PositionClosed", position_id="p-1",
                           quantity="0.5", gross_pnl="-10"))


def test_mark_to_market_changes_equity_not_balance_or_realized_pnl():
    ledger = RiskLedger("10000")
    ledger.apply(event(1, "MarkToMarketUpdated", unrealized_pnl="-125"))
    assert ledger.balance == Decimal("10000")
    assert ledger.equity == Decimal("9875")
    assert ledger.realized_pnl_lifetime == 0
    assert ledger.max_drawdown_amount == Decimal("125")


def test_period_roll_resets_period_counters_from_current_balance():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "PositionClosed", position_id="p-1",
                       quantity="1", gross_pnl="-50"))
    ledger.apply(event(3, "PeriodRolled", daily_period_id="2026-11-17",
                       weekly_period_id="2026-W47"))
    assert ledger.daily_period_id == "2026-11-17"
    assert ledger.weekly_period_id == "2026-W47"
    assert ledger.daily_start_balance == Decimal("9950")
    assert ledger.weekly_start_balance == Decimal("9950")
    assert ledger.daily_realized_net_pnl == 0
    assert ledger.weekly_realized_net_pnl == 0
    assert ledger.daily_loss_pct == 0


def test_period_roll_can_reset_daily_without_weekly():
    ledger = RiskLedger("10000")
    ledger.apply(event(1, "PeriodRolled", daily_period_id="day-2"))
    assert ledger.daily_period_id == "day-2"
    assert ledger.weekly_period_id == "initial"


def test_currency_mismatch_is_rejected():
    ledger = RiskLedger("10000", base_currency="USD")
    with pytest.raises(ValueError, match="base_currency"):
        ledger.apply(event(1, "MarkToMarketUpdated", currency="EUR",
                           unrealized_pnl="10"))
    assert ledger.sequence == 0


def test_actual_risk_cannot_exceed_approved_risk():
    ledger = RiskLedger("10000")
    with pytest.raises(ValueError, match="exceeds approved"):
        opened(ledger, actual_risk="100.01", approved_risk="100")
    assert ledger.sequence == 0


def test_event_sequence_must_be_contiguous_and_timestamp_timezone_aware():
    ledger = RiskLedger("10000")
    with pytest.raises(ValueError, match="contiguous"):
        ledger.apply(event(2, "MarkToMarketUpdated", unrealized_pnl="0"))
    bad_time = event(1, "MarkToMarketUpdated", timestamp="2026-11-16T10:00:00",
                     unrealized_pnl="0")
    with pytest.raises(ValueError, match="timezone"):
        ledger.apply(bad_time)
    assert ledger.sequence == 0


def test_same_ordered_event_stream_replays_deterministically():
    first = RiskLedger("10000")
    second = RiskLedger("10000")
    events = [
        event(1, "PositionOpened", position_id="p-1", symbol="EURUSD",
              side="BUY", quantity="1", entry="1.1", stop="1.099",
              approved_risk="100", actual_risk="99"),
        event(2, "MarkToMarketUpdated", unrealized_pnl="-12.5"),
        event(3, "PositionClosed", position_id="p-1", quantity="1",
              gross_pnl="-20", commission="1.5"),
    ]
    for item in events:
        first.apply(item)
        second.apply(item)
    assert first.snapshot() == second.snapshot()


def test_non_finite_money_values_are_rejected():
    ledger = RiskLedger("10000")
    with pytest.raises(ValueError, match="finite"):
        ledger.apply(event(1, "MarkToMarketUpdated", unrealized_pnl="NaN"))
    assert ledger.sequence == 0


def test_reusing_event_id_with_different_payload_is_rejected():
    ledger = RiskLedger("10000")
    first = event(1, "MarkToMarketUpdated", event_id="same-id",
                  unrealized_pnl="-10")
    ledger.apply(first)
    altered = dict(first, unrealized_pnl="-20")
    with pytest.raises(ValueError, match="different payload"):
        ledger.apply(altered)
    assert ledger.unrealized_pnl == Decimal("-10")


def test_open_risk_ratio_fails_closed_when_equity_is_non_positive():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "MarkToMarketUpdated", unrealized_pnl="-10000"))
    with pytest.raises(ValueError, match="non-positive equity"):
        _ = ledger.open_risk_pct



def test_snapshot_restore_rejects_non_contiguous_event_sequence():
    ledger = RiskLedger("10000")
    opened(ledger)
    snapshot = ledger.snapshot()
    snapshot["applied_events"][0]["sequence"] = 2
    with pytest.raises(ValueError, match="not contiguous"):
        RiskLedger.from_snapshot(snapshot)


def test_snapshot_restore_rejects_tampered_initial_balance():
    ledger = RiskLedger("10000")
    opened(ledger)
    snapshot = ledger.snapshot()
    snapshot["initial_balance"] = "10001"
    with pytest.raises(ValueError, match="inconsistent with its event history"):
        RiskLedger.from_snapshot(snapshot)


def test_snapshot_restore_rejects_tampered_accounting_state():
    ledger = RiskLedger("10000")
    opened(ledger)
    snapshot = ledger.snapshot()
    snapshot["balance"] = "10001"
    with pytest.raises(ValueError, match="inconsistent with its event history"):
        RiskLedger.from_snapshot(snapshot)


def test_snapshot_restore_rejects_tampered_position_state():
    ledger = RiskLedger("10000")
    opened(ledger)
    snapshot = ledger.snapshot()
    snapshot["open_positions"]["p-1"]["quantity"] = "2"
    with pytest.raises(ValueError, match="inconsistent with its event history"):
        RiskLedger.from_snapshot(snapshot)


def test_snapshot_restore_rejects_last_event_timestamp_mismatch():
    ledger = RiskLedger("10000")
    opened(ledger)
    snapshot = ledger.snapshot()
    snapshot["last_event_at"] = "2026-11-16T11:00:00+00:00"
    with pytest.raises(ValueError, match="inconsistent with its event history"):
        RiskLedger.from_snapshot(snapshot)


def test_snapshot_restore_replays_period_roll_and_realized_pnl():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "PositionClosed", position_id="p-1",
                       quantity="1", gross_pnl="-25", commission="2"))
    ledger.apply(event(3, "PeriodRolled", daily_period_id="day-2",
                       weekly_period_id="week-2"))
    restored = RiskLedger.from_snapshot(ledger.snapshot())
    assert restored.snapshot() == ledger.snapshot()
    assert restored.daily_start_balance == Decimal("9973")
    assert restored.daily_realized_net_pnl == 0


def test_period_roll_rejects_non_positive_start_balance_atomically():
    ledger = RiskLedger("10000")
    opened(ledger)
    ledger.apply(event(2, "PositionClosed", position_id="p-1",
                       quantity="1", gross_pnl="-10000"))
    before = ledger.snapshot()
    with pytest.raises(ValueError, match="period start balance must be positive"):
        ledger.apply(event(3, "PeriodRolled", daily_period_id="day-zero"))
    assert ledger.snapshot() == before
    assert ledger.sequence == 2


def test_snapshot_restore_preserves_event_idempotency_and_contiguous_sequence():
    ledger = RiskLedger("10000")
    opened(ledger)
    close = event(2, "PositionPartiallyClosed", event_id="partial-1",
                  position_id="p-1", quantity="0.4", gross_pnl="-8")
    ledger.apply(close)

    restored = RiskLedger.from_snapshot(ledger.snapshot())
    before = restored.snapshot()
    assert restored.apply(close) is False
    assert restored.snapshot() == before

    restored.apply(event(3, "PositionClosed", position_id="p-1",
                         quantity="0.6", gross_pnl="-4"))
    assert restored.daily_realized_net_pnl == Decimal("-12")
    assert restored.open_risk_amount == 0
    assert restored.consecutive_losses == 1


def test_snapshot_restore_rejects_missing_event_history():
    ledger = RiskLedger("10000")
    opened(ledger)
    snapshot = ledger.snapshot()
    snapshot.pop("applied_events")
    with pytest.raises(ValueError, match="applied_events"):
        RiskLedger.from_snapshot(snapshot)


def test_snapshot_restore_rejects_event_history_sequence_mismatch():
    ledger = RiskLedger("10000")
    opened(ledger)
    snapshot = ledger.snapshot()
    snapshot["applied_events"] = []
    with pytest.raises(ValueError, match="does not match sequence"):
        RiskLedger.from_snapshot(snapshot)



def test_sqlite_store_round_trips_and_replays_events_after_reopen(tmp_path):
    database = tmp_path / "mas-ledger.sqlite3"
    ledger = RiskLedger("10000")
    SQLiteLedgerStore(database).create("paper-account", ledger)

    first = event(1, "PositionOpened", position_id="p-1", symbol="EURUSD",
                  side="BUY", quantity="1", entry="1.1", stop="1.099",
                  approved_risk="100", actual_risk="99")
    ledger.apply(first)
    SQLiteLedgerStore(database).append(
        "paper-account", expected_sequence=0, event=first, ledger=ledger
    )

    restored = SQLiteLedgerStore(database).load("paper-account")
    assert restored.snapshot() == ledger.snapshot()
    assert restored.open_risk_amount == Decimal("99")


def test_sqlite_store_appends_event_and_snapshot_atomically(tmp_path):
    database = tmp_path / "mas-ledger.sqlite3"
    store = SQLiteLedgerStore(database)
    ledger = RiskLedger("10000")
    store.create("account", ledger)

    first = event(1, "MarkToMarketUpdated", unrealized_pnl="-10")
    ledger.apply(first)
    store.append("account", expected_sequence=0, event=first, ledger=ledger)

    second = event(2, "MarkToMarketUpdated", unrealized_pnl="-25")
    ledger.apply(second)
    store.append("account", expected_sequence=1, event=second, ledger=ledger)

    assert store.load("account").snapshot() == ledger.snapshot()


def test_sqlite_store_rejects_stale_writer_without_partial_commit(tmp_path):
    database = tmp_path / "mas-ledger.sqlite3"
    store = SQLiteLedgerStore(database)
    initial = RiskLedger("10000")
    store.create("account", initial)
    writer_a = store.load("account")
    writer_b = store.load("account")

    first = event(1, "MarkToMarketUpdated", unrealized_pnl="-10")
    writer_a.apply(first)
    store.append("account", expected_sequence=0, event=first, ledger=writer_a)

    stale = event(1, "MarkToMarketUpdated", event_id="stale-event",
                  unrealized_pnl="-20")
    writer_b.apply(stale)
    with pytest.raises(ValueError, match="stale ledger writer"):
        store.append("account", expected_sequence=0, event=stale, ledger=writer_b)

    persisted = store.load("account")
    assert persisted.sequence == 1
    assert persisted.unrealized_pnl == Decimal("-10")


def test_sqlite_store_detects_event_log_tampering(tmp_path):
    import sqlite3

    database = tmp_path / "mas-ledger.sqlite3"
    store = SQLiteLedgerStore(database)
    ledger = RiskLedger("10000")
    store.create("account", ledger)
    first = event(1, "MarkToMarketUpdated", unrealized_pnl="-10")
    ledger.apply(first)
    store.append("account", expected_sequence=0, event=first, ledger=ledger)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE mas_ledger_events SET payload_json = ? WHERE ledger_id = ? AND sequence = 1",
            ('{"event_id":"evt-1","sequence":1,"timestamp":"2026-11-16T10:01:00+00:00","type":"MarkToMarketUpdated","unrealized_pnl":"-20"}', "account"),
        )

    with pytest.raises(ValueError, match="inconsistent with its event history"):
        store.load("account")


def test_sqlite_store_requires_pristine_ledger_for_creation(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "mas-ledger.sqlite3")
    ledger = RiskLedger("10000")
    ledger.apply(event(1, "MarkToMarketUpdated", unrealized_pnl="0"))
    with pytest.raises(ValueError, match="pristine"):
        store.create("account", ledger)


def test_sqlite_store_rejects_duplicate_ledger_id(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "mas-ledger.sqlite3")
    store.create("account", RiskLedger("10000"))
    with pytest.raises(ValueError, match="already exists"):
        store.create("account", RiskLedger("20000"))



def test_sqlite_store_makes_exact_commit_retries_idempotent(tmp_path):
    database = tmp_path / "mas-ledger.sqlite3"
    store = SQLiteLedgerStore(database)
    ledger = RiskLedger("10000")
    store.create("account", ledger)
    first = event(1, "MarkToMarketUpdated", unrealized_pnl="-10")
    ledger.apply(first)

    assert store.append("account", expected_sequence=0, event=first, ledger=ledger) is True
    # Simulates a client retry after the database committed but the response was lost.
    assert store.append("account", expected_sequence=0, event=first, ledger=ledger) is False
    assert store.load("account").snapshot() == ledger.snapshot()



def test_load_uses_one_consistent_snapshot_during_concurrent_append(tmp_path, monkeypatch):
    database = tmp_path / "mas-ledger.sqlite3"
    store = SQLiteLedgerStore(database)
    initial = RiskLedger("10000")
    store.create("account", initial)

    writer = store.load("account")
    new_event = event(1, "MarkToMarketUpdated", unrealized_pnl="-15")
    writer.apply(new_event)

    original_replay = SQLiteLedgerStore._replay
    raced = False

    def append_between_snapshot_and_event_reads(connection, ledger_id,
                                                  schema_version, sequence,
                                                  snapshot_json):
        nonlocal raced
        if not raced:
            raced = True
            store.append(
                "account", expected_sequence=0, event=new_event, ledger=writer
            )
        return original_replay(
            connection, ledger_id, schema_version, sequence, snapshot_json
        )

    monkeypatch.setattr(
        SQLiteLedgerStore, "_replay", staticmethod(append_between_snapshot_and_event_reads)
    )

    # load() must see the old, internally consistent read snapshot even though
    # another connection commits immediately after its snapshot-row SELECT.
    restored = store.load("account")
    assert restored.sequence == 0
    assert restored.unrealized_pnl == Decimal("0")

    latest = store.load("account")
    assert latest.sequence == 1
    assert latest.unrealized_pnl == Decimal("-15")



def test_sqlite_store_rolls_back_event_when_snapshot_update_fails(tmp_path):
    import sqlite3

    database = tmp_path / "mas-ledger.sqlite3"
    store = SQLiteLedgerStore(database)
    ledger = RiskLedger("10000")
    store.create("account", ledger)

    next_event = event(1, "MarkToMarketUpdated", unrealized_pnl="-12")
    ledger.apply(next_event)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_snapshot_update
            BEFORE UPDATE ON mas_ledger_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'forced snapshot failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced snapshot failure"):
        store.append(
            "account", expected_sequence=0, event=next_event, ledger=ledger
        )

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER reject_snapshot_update")

    # The event INSERT must roll back together with the failed snapshot UPDATE.
    restored = store.load("account")
    assert restored.sequence == 0
    assert restored.unrealized_pnl == Decimal("0")



def test_snapshot_recovery_preserves_genesis_period_ids_after_rollover():
    ledger = RiskLedger(
        "10000", daily_period_id="daily-genesis",
        weekly_period_id="weekly-genesis",
    )
    assert RiskLedger.from_snapshot(ledger.snapshot()).snapshot() == ledger.snapshot()

    ledger.apply(event(
        1, "PeriodRolled", daily_period_id="daily-next",
        weekly_period_id="weekly-next",
    ))
    restored = RiskLedger.from_snapshot(ledger.snapshot())
    assert restored.snapshot() == ledger.snapshot()
    assert restored.initial_daily_period_id == "daily-genesis"
    assert restored.initial_weekly_period_id == "weekly-genesis"


def test_snapshot_recovery_rejects_missing_genesis_period_metadata():
    snapshot = RiskLedger(
        "10000", daily_period_id="daily-genesis",
        weekly_period_id="weekly-genesis",
    ).snapshot()
    del snapshot["initial_daily_period_id"]
    with pytest.raises(ValueError, match="initial_daily_period_id is required"):
        RiskLedger.from_snapshot(snapshot)
