"""Tests for the transactional SQLite event ledger."""
import sqlite3
import pytest
from decimal import Decimal

from engine.mas.ledger import LedgerConflictError, LedgerError, SQLiteLedger


def _event(key="fill-1", payload=None):
    return {
        "account_id": "paper-account",
        "trade_id": "trade-1",
        "event_type": "FillRecorded",
        "idempotency_key": key,
        "payload": payload or {"lots": "0.10", "price": "1.10000", "fee": "0.25"},
    }


@pytest.mark.parametrize("event_type", ["UnknownFinancialEvent", "FutureEventV9"])
def test_append_rejects_unknown_event_types(tmp_path, event_type):
    ledger = SQLiteLedger(tmp_path / "ledger-unknown-event.db")
    event = {**_event(), "event_type": event_type}
    with pytest.raises(LedgerError, match="unsupported event_type"):
        ledger.append_event(event)
    assert ledger.read_events("paper-account") == []


@pytest.mark.parametrize("schema_version", [0, 3, -1, True, "2"])
def test_append_rejects_unsupported_or_malformed_schema_versions(tmp_path, schema_version):
    ledger = SQLiteLedger(tmp_path / "ledger-unknown-schema.db")
    event = {**_event(), "schema_version": schema_version}
    with pytest.raises(LedgerError, match="schema_version"):
        ledger.append_event(event)
    assert ledger.read_events("paper-account") == []


def test_schema_v2_is_restricted_to_position_opened(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-v2-event-scope.db")
    event = {**_event(), "schema_version": 2}
    with pytest.raises(LedgerError, match="unsupported schema_version 2 for FillRecorded"):
        ledger.append_event(event)
    assert ledger.read_events("paper-account") == []


def test_stored_only_events_are_explicitly_accepted(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-stored-only.db")
    ledger.append_event(_event())
    ledger.append_event({
        "account_id": "paper-account",
        "event_type": "AuditMarker",
        "idempotency_key": "audit-marker-1",
        "payload": {"marker": "reviewed"},
    })
    assert [event.event_type for event in ledger.read_events("paper-account")] == [
        "FillRecorded", "AuditMarker"
    ]


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("event_type", "UnknownPersistedEvent", "unsupported persisted event_type"),
        ("schema_version", 99, "unsupported persisted schema_version"),
    ],
)
def test_replay_fails_closed_for_unsupported_persisted_contract(
    tmp_path, column, value, message
):
    ledger = SQLiteLedger(tmp_path / "ledger-corrupt-contract.db")
    ledger.append_event(_event())
    conn = ledger._connect()
    try:
        conn.execute("DROP TRIGGER ledger_events_no_update")
        conn.execute(
            f"UPDATE ledger_events SET {column}=? WHERE account_id=?",
            (value, "paper-account"),
        )
        conn.execute(
            """
            CREATE TRIGGER ledger_events_no_update
            BEFORE UPDATE ON ledger_events
            BEGIN
                SELECT RAISE(ABORT, 'ledger_events is append-only');
            END
            """
        )
    finally:
        conn.close()

    with pytest.raises(LedgerError, match=message):
        ledger.replay_account("paper-account", "1000")


def test_append_is_idempotent_and_has_one_financial_effect(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    first, created_first = ledger.append_event(_event())
    second, created_second = ledger.append_event(_event())

    assert created_first is True
    assert created_second is False
    assert first.event_id == second.event_id
    assert len(ledger.read_events("paper-account")) == 1


def test_reusing_idempotency_key_with_different_payload_is_rejected(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event(_event())
    with pytest.raises(LedgerConflictError):
        ledger.append_event(_event(payload={"lots": "0.20"}))


def test_batch_append_is_atomic_on_conflict(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event(_event("existing"))
    batch = [
        _event("new-event", {"lots": "0.10"}),
        _event("existing", {"lots": "9.99"}),
    ]

    with pytest.raises(LedgerConflictError):
        ledger.append_events(batch)

    assert [event.idempotency_key for event in ledger.read_events("paper-account")] == ["existing"]


def test_batch_retries_same_idempotency_key_with_same_body_once(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-batch-idempotency.db")
    first = _event("batch-retry", {"lots": "0.10", "price": "1.10000"})
    retry = _event("batch-retry", {"lots": "0.10", "price": "1.10000"})

    results = ledger.append_events([first, retry])

    assert [inserted for _stored, inserted in results] == [True, False]
    assert results[0][0].event_id == results[1][0].event_id
    events = ledger.read_events("paper-account")
    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].idempotency_key == "batch-retry"


def test_batch_conflicting_same_idempotency_key_rolls_back_first_insert(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-batch-idempotency-conflict.db")
    first = _event("batch-conflict", {"lots": "0.10", "price": "1.10000"})
    conflicting_retry = _event(
        "batch-conflict", {"lots": "0.20", "price": "1.10000"}
    )

    with pytest.raises(LedgerConflictError, match="different event body"):
        ledger.append_events([first, conflicting_retry])

    assert ledger.read_events("paper-account") == []


def test_new_cash_adjustment_requires_v2_provenance(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-cash-provenance.db")
    legacy = {
        "account_id": "paper-account", "event_type": "CashAdjustment",
        "idempotency_key": "cash-legacy-new", "payload": {"amount": "50.00"},
    }
    with pytest.raises(LedgerError, match="require schema_version 2"):
        ledger.append_event(legacy)

    incomplete = {
        **legacy, "schema_version": 2,
        "idempotency_key": "cash-v2-incomplete",
    }
    with pytest.raises(LedgerError, match="requires non-empty actor"):
        ledger.append_event(incomplete)
    assert ledger.read_events("paper-account") == []


def test_replay_keeps_historical_v1_cash_and_risk_release_compatibility(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-historical-v1-compat.db")
    conn = ledger._connect()
    try:
        # Simulate immutable historical rows created before v2 append policies.
        rows = [
            ("historical-lock", "RiskLockActivated", 1, "legacy-lock", "{}"),
            ("historical-cash", "CashAdjustment", 2, "legacy-cash",
             '{"amount":"10.00"}'),
            ("historical-release", "RiskLockReleased", 3, "legacy-release", "{}"),
        ]
        for event_id, event_type, sequence, key, payload in rows:
            conn.execute(
                """INSERT INTO ledger_events (
                    event_id, account_id, trade_id, order_id, event_type, sequence,
                    occurred_at, recorded_at, idempotency_key, schema_version, payload_json
                ) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, 1, ?)""",
                (event_id, "paper-account", event_type, sequence,
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                 key, payload),
            )
    finally:
        conn.close()

    state = ledger.replay_account("paper-account", "1000.00")
    assert state["cash_balance"] == "1010.00"
    assert state["risk_lock_active"] is False


def test_batch_idempotency_replay_is_stable_after_database_restart(tmp_path):
    path = tmp_path / "ledger-batch-replay-restart.db"
    ledger = SQLiteLedger(path)
    first = {
        "account_id": "paper-account",
        "event_type": "CashAdjustment",
        "schema_version": 2,
        "idempotency_key": "cash:deposit:1",
        "payload": {"amount": "125.50", "actor": "test-operator",
                    "reason": "opening funding", "approval_reference": "cash-ticket-001",
                    "source": "test-fixture"},
    }
    second = {
        "account_id": "paper-account",
        "event_type": "CashAdjustment",
        "schema_version": 2,
        "idempotency_key": "cash:withdrawal:1",
        "payload": {"amount": "-25.25", "actor": "test-operator",
                    "reason": "test withdrawal", "approval_reference": "cash-ticket-002",
                    "source": "test-fixture"},
    }
    retry = dict(second)

    results = ledger.append_events([first, second, retry])
    assert [inserted for _stored, inserted in results] == [True, True, False]
    assert results[1][0].event_id == results[2][0].event_id

    before_restart = ledger.replay_account("paper-account", "1000.00")
    assert before_restart["cash_balance"] == "1100.25"
    assert before_restart["last_sequence"] == 2

    restarted = SQLiteLedger(path)
    persisted_events = restarted.read_events("paper-account")
    after_restart = restarted.replay_account("paper-account", "1000.00")

    assert [event.sequence for event in persisted_events] == [1, 2]
    assert after_restart == before_restart
    assert after_restart["cash_balance"] == "1100.25"
    assert after_restart["last_sequence"] == 2


def test_sequence_is_monotonic_and_survives_restart(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(path)
    ledger.append_event(_event("one"))
    ledger.append_event(_event("two"))
    restarted = SQLiteLedger(path)
    events = restarted.read_events("paper-account")

    assert [event.sequence for event in events] == [1, 2]
    assert events[0].payload["lots"] == "0.10"


def test_money_values_must_not_be_encoded_as_non_finite_numbers(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    with pytest.raises(ValueError):
        ledger.append_event(_event(payload={"lots": float("nan")}))



def test_replay_accounts_for_partial_close_costs_and_unrealized_pnl(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionOpened", "idempotency_key": "open-1",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1.00",
                    "entry_price": "1.10000"},
    })
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionPartiallyClosed", "idempotency_key": "partial-1",
        "payload": {"lots_closed": "0.40", "pnl_gross": "20.00",
                    "commission": "1.00", "slippage": "0.50", "swap": "0.00"},
    })
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionMarked", "idempotency_key": "mark-1",
        "payload": {"unrealized_pnl": "3.25"},
    })

    state = ledger.replay_account("paper-account", "1000.00")
    assert state["realized_pnl"] == "18.50"
    assert state["unrealized_pnl"] == "3.25"
    assert state["equity"] == "1021.75"
    assert state["open_positions"]["trade-1"]["lots"] == "0.60"


def test_replay_rejects_over_close_and_incomplete_final_close(tmp_path):
    from engine.mas.ledger import LedgerError

    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionOpened", "idempotency_key": "open-1",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1.00",
                    "entry_price": "1.10000"},
    })
    # Incomplete final settlement is now rejected at append time, not
    # persisted and deferred until a later replay.
    with pytest.raises(LedgerError, match="full remaining quantity"):
        ledger.append_event({
            "account_id": "paper-account", "trade_id": "trade-1",
            "event_type": "PositionClosed", "idempotency_key": "close-1",
            "payload": {"lots_closed": "0.50", "pnl_gross": "1.00",
                        "commission": "0", "slippage": "0", "swap": "0"},
        })
    assert [event.event_type for event in ledger.read_events("paper-account")] == [
        "PositionOpened"
    ]


def test_risk_lock_state_is_replayed_from_events(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    base = {"account_id": "paper-account", "payload": {}}
    ledger.append_event({**base, "event_type": "RiskLockActivated",
                         "idempotency_key": "lock-1"})
    assert ledger.replay_account("paper-account", "1000")["risk_lock_active"] is True
    with pytest.raises(LedgerError, match="require schema_version 2"):
        ledger.append_event({**base, "event_type": "RiskLockReleased",
                             "idempotency_key": "unlock-legacy"})
    ledger.append_event({**base, "event_type": "RiskLockReleased",
                         "schema_version": 2, "idempotency_key": "unlock-1",
                         "payload": {"reviewed_by": "reviewer",
                                     "reason": "review completed",
                                     "approval_reference": "ticket-001"}})
    assert ledger.replay_account("paper-account", "1000")["risk_lock_active"] is False


def test_in_memory_ledger_persists_across_internal_connections():
    ledger = SQLiteLedger(":memory:")
    ledger.append_event(_event())
    assert len(ledger.read_events("paper-account")) == 1
    ledger.close()


def test_event_store_rejects_update_and_delete(tmp_path):
    import sqlite3
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    event, _ = ledger.append_event(_event())
    conn = sqlite3.connect(ledger.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE ledger_events SET event_type='Tampered' WHERE event_id=?",
                         (event.event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ledger_events WHERE event_id=?", (event.event_id,))
    finally:
        conn.close()


def test_idempotency_key_cannot_be_reused_for_another_trade(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event(_event())
    changed = {**_event(), "trade_id": "different-trade"}
    with pytest.raises(LedgerConflictError):
        ledger.append_event(changed)

def test_replay_reconstructs_paper_cycle_and_risk_state(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event({
        "account_id": "paper-account", "event_type": "RiskStateSnapshot",
        "idempotency_key": "seed-state",
        "occurred_at": "2026-11-17T19:00:00+00:00",
        "payload": {"consecutive_losses": 2, "daily_loss_pct": "0.5",
                    "weekly_loss_pct": "1.25", "risk_lock_active": False, "runs": 4,
                    "last_run_ts": "2026-11-17T20:00:00+00:00",
                    "daily_risk_date": "2026-11-17", "weekly_risk_week": "2026-11-16"},
    })
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionOpened", "idempotency_key": "open-trade-1",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1.0",
                    "entry_price": "1.1000", "sl": "1.0990", "tp2": "1.1020",
                    "entry_time": "2026-11-17T20:15:00+00:00", "risk_usd": "100",
                    "risk_pct": "1", "regime": "range", "session": "London"},
    })
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionClosed", "idempotency_key": "close-trade-1",
        "occurred_at": "2026-11-18T00:30:00+00:00",
        "payload": {"lots_closed": "1.0", "pnl_gross": "-50",
                    "commission": "0", "slippage": "0", "swap": "0",
                    "daily_loss_pct_delta": "0.5", "weekly_loss_pct_delta": "0.5",
                    "consecutive_loss_stop": 3,
                    "exit_price": "1.0995", "outcome": "SL"},
    })
    ledger.append_event({
        "account_id": "paper-account", "event_type": "PaperCycleCompleted",
        "idempotency_key": "cycle-2026-11-18",
        "payload": {"last_run_ts": "2026-11-18T00:00:00+00:00",
                    "daily_risk_date": "2026-11-18"},
    })

    state = ledger.replay_account("paper-account", "10000")
    assert state["equity"] == "9950"
    assert state["consecutive_losses"] == 3
    assert state["risk_lock_active"] is True
    assert state["daily_loss_pct"] == "0.5"
    assert state["weekly_loss_pct"] == "1.75"
    assert state["runs"] == 5
    assert state["last_run_ts"] == "2026-11-18T00:00:00+00:00"
    assert state["open_positions"] == {}
    assert state["closed_trades"][0]["pnl_usd"] == "-50"


def test_risk_replay_orders_delayed_closes_before_later_audited_release(tmp_path):
    ledger = SQLiteLedger(tmp_path / "delayed-risk-release.db")
    account = "paper-account"

    for trade_id in ("loss-1", "loss-2", "loss-3"):
        ledger.append_event({
            "account_id": account,
            "trade_id": trade_id,
            "event_type": "PositionOpened",
            "idempotency_key": f"open:{trade_id}",
            "occurred_at": "2026-11-18T09:00:00+00:00",
            "payload": {
                "symbol": "EURUSD",
                "direction": "BUY",
                "lots": "1",
                "entry_price": "1.1000",
            },
        })

    # The hard stop and its audited release are appended before delayed
    # settlements arrive, but their timestamps are later than the exits.
    ledger.append_event({
        "account_id": account,
        "event_type": "RiskLockActivated",
        "idempotency_key": "lock:review-window",
        "occurred_at": "2026-11-18T14:00:00+00:00",
        "payload": {},
    })
    ledger.append_event({
        "account_id": account,
        "event_type": "RiskLockReleased",
        "idempotency_key": "unlock:review-approval",
        "occurred_at": "2026-11-18T15:00:00+00:00",
        "schema_version": 2,
        "payload": {"consecutive_losses": 0, "reviewed_by": "reviewer",
                    "reason": "review completed",
                    "approval_reference": "review-ticket-001"},
    })

    for index, trade_id in enumerate(("loss-1", "loss-2", "loss-3"), start=10):
        ledger.append_event({
            "account_id": account,
            "trade_id": trade_id,
            "event_type": "PositionClosed",
            "idempotency_key": f"close:{trade_id}",
            "occurred_at": f"2026-11-18T{index:02d}:00:00+00:00",
            "payload": {
                "lots_closed": "1",
                "pnl_gross": "-10",
                "commission": "0",
                "slippage": "0",
                "swap": "0",
                "consecutive_loss_stop": 3,
            },
        })

    state = ledger.replay_account(account, "10000")
    assert state["consecutive_losses"] == 0
    assert state["risk_lock_active"] is False

    restarted = SQLiteLedger(tmp_path / "delayed-risk-release.db")
    assert restarted.replay_account(account, "10000") == state


def test_daily_risk_reset_changes_only_daily_loss_not_loss_streak(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event({
        "account_id": "paper-account", "event_type": "RiskStateSnapshot",
        "idempotency_key": "seed-state",
        "payload": {"consecutive_losses": 2, "daily_loss_pct": "2.5",
                    "risk_lock_active": False},
    })
    ledger.append_event({
        "account_id": "paper-account", "event_type": "DailyRiskReset",
        "idempotency_key": "daily-reset-1",
        "payload": {"daily_risk_date": "2026-11-18"},
    })
    state = ledger.replay_account("paper-account", "10000")
    assert state["daily_loss_pct"] == "0"
    assert state["consecutive_losses"] == 2

def test_ledger_initial_balance_metadata_is_set_once_and_immutable(tmp_path):
    import sqlite3
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    assert ledger.set_meta_once("initial_balance:paper", "10000.00") == "10000.00"
    assert ledger.set_meta_once("initial_balance:paper", "9000.00") == "10000.00"
    assert ledger.get_meta("initial_balance:paper") == "10000.00"
    conn = sqlite3.connect(ledger.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE ledger_meta SET meta_value='1' WHERE meta_key='initial_balance:paper'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM ledger_meta WHERE meta_key='initial_balance:paper'")
    finally:
        conn.close()

def test_weekly_risk_reset_does_not_clear_daily_or_consecutive_loss(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event({
        "account_id": "paper-account", "event_type": "RiskStateSnapshot",
        "idempotency_key": "seed-state",
        "payload": {"consecutive_losses": 2, "daily_loss_pct": "1.5",
                    "weekly_loss_pct": "5.5", "risk_lock_active": False},
    })
    ledger.append_event({
        "account_id": "paper-account", "event_type": "WeeklyRiskReset",
        "idempotency_key": "weekly-reset-1",
        "payload": {"weekly_risk_week": "2026-11-16"},
    })
    state = ledger.replay_account("paper-account", "10000")
    assert state["weekly_loss_pct"] == "0"
    assert state["daily_loss_pct"] == "1.5"
    assert state["consecutive_losses"] == 2


def test_replay_attributes_delayed_closes_to_actual_day_and_week(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    account = "paper-account"

    for trade_id, opened_at in (
        ("old-week", "2026-11-10T10:00:00+00:00"),
        ("current-week-old-day", "2026-11-17T10:00:00+00:00"),
        ("current-day", "2026-11-18T10:00:00+00:00"),
    ):
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionOpened", "idempotency_key": f"open:{trade_id}",
            "occurred_at": opened_at,
            "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                        "entry_price": "1.1000"},
        })

    # The Paper cycle records current-period resets before it discovers and
    # settles positions whose actual exits span earlier days and weeks.
    ledger.append_event({
        "account_id": account, "event_type": "DailyRiskReset",
        "idempotency_key": "reset:day:2026-11-18",
        "occurred_at": "2026-11-18T12:00:00+00:00",
        "payload": {"daily_risk_date": "2026-11-18"},
    })
    ledger.append_event({
        "account_id": account, "event_type": "WeeklyRiskReset",
        "idempotency_key": "reset:week:2026-11-16",
        "occurred_at": "2026-11-18T12:00:00+00:00",
        "payload": {"weekly_risk_week": "2026-11-16"},
    })

    for trade_id, closed_at, loss_delta in (
        ("old-week", "2026-11-10T11:00:00+00:00", "1.25"),
        ("current-week-old-day", "2026-11-17T11:00:00+00:00", "2.50"),
        ("current-day", "2026-11-18T13:00:00+00:00", "0.75"),
    ):
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionClosed", "idempotency_key": f"close:{trade_id}",
            "occurred_at": closed_at,
            "payload": {"lots_closed": "1", "pnl_gross": "-10",
                        "commission": "0", "slippage": "0", "swap": "0",
                        "daily_loss_pct_delta": loss_delta,
                        "weekly_loss_pct_delta": loss_delta,
                        "consecutive_loss_stop": 10, "exit_time": closed_at,
                        "outcome": "SL"},
        })

    state = ledger.replay_account(account, "10000")
    assert state["daily_risk_date"] == "2026-11-18"
    assert state["weekly_risk_week"] == "2026-11-16"
    assert Decimal(state["daily_loss_pct"]) == Decimal("10") / Decimal("9980") * 100
    assert Decimal(state["weekly_loss_pct"]) == Decimal("20") / Decimal("9990") * 100
    assert state["realized_pnl"] == "-30"
    assert state["open_positions"] == {}


def test_consecutive_losses_follow_exit_time_not_append_sequence(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    account = "paper-account"
    trades = (
        ("loss-1", "2026-11-18T10:00:00+00:00", "-10"),
        ("win", "2026-11-18T11:00:00+00:00", "15"),
        ("loss-2", "2026-11-18T12:00:00+00:00", "-12"),
        ("loss-3", "2026-11-18T13:00:00+00:00", "-8"),
    )
    for trade_id, _closed_at, _pnl in trades:
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionOpened", "idempotency_key": f"open:{trade_id}",
            "occurred_at": "2026-11-18T09:00:00+00:00",
            "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                        "entry_price": "1.1000"},
        })

    # Deliberately append in reverse exit-time order to simulate delayed Paper settlement.
    for trade_id, closed_at, pnl in reversed(trades):
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionClosed", "idempotency_key": f"close:{trade_id}",
            "occurred_at": closed_at,
            "payload": {"lots_closed": "1", "pnl_gross": pnl,
                        "commission": "0", "slippage": "0", "swap": "0",
                        "consecutive_loss_stop": 3, "exit_time": closed_at},
        })

    state = ledger.replay_account(account, "10000")
    # Actual order is loss, win, loss, loss: only the final two are consecutive.
    assert state["consecutive_losses"] == 2
    assert state["risk_lock_active"] is False


def test_late_risk_snapshot_cannot_overwrite_newer_loss_state(tmp_path):
    ledger = SQLiteLedger(tmp_path / "stale-risk-snapshot.db")
    account = "paper-account"
    ledger.append_event({
        "account_id": account, "trade_id": "trade-after-snapshot",
        "event_type": "PositionOpened", "idempotency_key": "open:trade-after-snapshot",
        "occurred_at": "2026-11-18T09:00:00+00:00",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                    "entry_price": "1.1000"},
    })
    ledger.append_event({
        "account_id": account, "trade_id": "trade-after-snapshot",
        "event_type": "PositionClosed", "idempotency_key": "close:trade-after-snapshot",
        "occurred_at": "2026-11-18T10:00:00+00:00",
        "payload": {"lots_closed": "1", "pnl_gross": "-10",
                    "commission": "0", "slippage": "0", "swap": "0",
                    "consecutive_loss_stop": 3},
    })
    ledger.append_event({
        "account_id": account, "event_type": "RiskStateSnapshot",
        "idempotency_key": "stale-risk-snapshot",
        "occurred_at": "2026-11-18T16:00:00+00:00",
        "payload": {"consecutive_losses": 0, "risk_lock_active": False},
    })
    state = ledger.replay_account(account, "10000")
    assert state["consecutive_losses"] == 1
    assert state["risk_lock_active"] is False


def test_migration_risk_snapshot_is_baseline_before_delayed_settlement(tmp_path):
    ledger = SQLiteLedger(tmp_path / "baseline-risk-snapshot.db")
    account = "paper-account"
    ledger.append_event({
        "account_id": account, "event_type": "RiskStateSnapshot",
        "idempotency_key": "legacy-risk-baseline",
        "occurred_at": "2026-11-18T16:00:00+00:00",
        "payload": {"consecutive_losses": 2, "risk_lock_active": False},
    })
    ledger.append_event({
        "account_id": account, "trade_id": "delayed-trade",
        "event_type": "PositionOpened", "idempotency_key": "open:delayed-trade",
        "occurred_at": "2026-11-18T09:00:00+00:00",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                    "entry_price": "1.1000"},
    })
    ledger.append_event({
        "account_id": account, "trade_id": "delayed-trade",
        "event_type": "PositionClosed", "idempotency_key": "close:delayed-trade",
        "occurred_at": "2026-11-18T10:00:00+00:00",
        "payload": {"lots_closed": "1", "pnl_gross": "-10",
                    "commission": "0", "slippage": "0", "swap": "0",
                    "consecutive_loss_stop": 3},
    })
    state = ledger.replay_account(account, "10000")
    assert state["consecutive_losses"] == 3
    assert state["risk_lock_active"] is True


def test_partial_closes_count_once_using_aggregate_trade_result(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    account = "paper-account"
    ledger.append_event({
        "account_id": account, "trade_id": "partial-trade",
        "event_type": "PositionOpened", "idempotency_key": "open:partial",
        "occurred_at": "2026-11-18T09:00:00+00:00",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                    "entry_price": "1.1000"},
    })
    ledger.append_event({
        "account_id": account, "trade_id": "partial-trade",
        "event_type": "PositionPartiallyClosed", "idempotency_key": "partial:1",
        "occurred_at": "2026-11-18T10:00:00+00:00",
        "payload": {"lots_closed": "0.5", "pnl_gross": "-20",
                    "commission": "0", "slippage": "0", "swap": "0"},
    })
    ledger.append_event({
        "account_id": account, "trade_id": "partial-trade",
        "event_type": "PositionClosed", "idempotency_key": "close:partial",
        "occurred_at": "2026-11-18T11:00:00+00:00",
        "payload": {"lots_closed": "0.5", "pnl_gross": "30",
                    "commission": "0", "slippage": "0", "swap": "0",
                    "consecutive_loss_stop": 3},
    })

    state = ledger.replay_account(account, "10000")
    assert state["consecutive_losses"] == 0
    assert state["risk_lock_active"] is False
    assert state["realized_pnl"] == "10"


def test_idempotent_retry_rejects_changed_financial_timestamp(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionOpened", "idempotency_key": "open:trade-1",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                    "entry_price": "1.1000"},
    })
    event = {
        "account_id": "paper-account",
        "trade_id": "trade-1",
        "event_type": "PositionClosed",
        "idempotency_key": "close:trade-1",
        "occurred_at": "2026-11-18T10:00:00+00:00",
        "payload": {"lots_closed": "1", "pnl_gross": "-10",
                    "commission": "0", "slippage": "0", "swap": "0"},
    }
    ledger.append_event(event)
    changed_time = {**event, "occurred_at": "2026-11-19T10:00:00+00:00"}
    with pytest.raises(LedgerConflictError, match="different event body"):
        ledger.append_event(changed_time)

    stored = ledger.read_events("paper-account")
    assert len(stored) == 2
    assert stored[1].occurred_at == event["occurred_at"]

def test_shared_paper_backtest_accounting_contract_is_deterministic(tmp_path):
    """Identical Paper/Backtest event streams must replay to identical accounting."""
    ledger = SQLiteLedger(tmp_path / "parity.db")
    common = [
        {
            "event_type": "RiskStateSnapshot",
            "idempotency_key": "seed-risk",
            "occurred_at": "2026-11-16T08:00:00+00:00",
            "payload": {
                "consecutive_losses": 1, "daily_loss_pct": "0",
                "weekly_loss_pct": "0", "risk_lock_active": False,
                "runs": 0, "daily_risk_date": "2026-11-16",
                "weekly_risk_week": "2026-11-16",
            },
        },
        {
            "trade_id": "parity-trade", "event_type": "PositionOpened",
            "idempotency_key": "open-parity-trade",
            "occurred_at": "2026-11-16T09:00:00+00:00",
            "payload": {
                "symbol": "EURUSD", "direction": "BUY", "lots": "0.10",
                "entry_price": "1.10000", "sl": "1.09500", "tp2": "1.11000",
                "entry_time": "2026-11-16T09:00:00+00:00", "risk_usd": "5.00",
                "risk_pct": "0.5", "regime": "range", "session": "London",
            },
        },
        {
            "trade_id": "parity-trade", "event_type": "PositionClosed",
            "idempotency_key": "close-parity-trade",
            "occurred_at": "2026-11-16T10:00:00+00:00",
            "payload": {
                "lots_closed": "0.10", "pnl_gross": "-25.00",
                "commission": "1.00", "slippage": "0.50", "swap": "0.25",
                "daily_loss_pct_delta": "2.675",
                "weekly_loss_pct_delta": "2.675",
                "consecutive_loss_stop": 3, "exit_price": "1.09750",
                "exit_time": "2026-11-16T10:00:00+00:00", "outcome": "SL",
            },
        },
    ]

    for account in ("paper", "backtest"):
        ledger.append_events([
            {
                "account_id": account,
                **event,
                "idempotency_key": f"{account}:{event['idempotency_key']}",
            }
            for event in common
        ])

    paper_state = ledger.replay_account("paper", "1000.00")
    backtest_state = ledger.replay_account("backtest", "1000.00")

    accounting_fields = (
        "cash_balance", "realized_pnl", "unrealized_pnl", "equity",
        "open_positions", "closed_trades", "risk_lock_active",
        "consecutive_losses", "daily_loss_pct", "weekly_loss_pct",
    )
    assert {key: paper_state[key] for key in accounting_fields} == {
        key: backtest_state[key] for key in accounting_fields
    }
    assert paper_state["realized_pnl"] == "-26.75"
    assert paper_state["equity"] == "973.25"
    assert paper_state["consecutive_losses"] == 2
    assert paper_state["daily_loss_pct"] == "2.675"
    assert paper_state["weekly_loss_pct"] == "2.675"




def test_three_chronological_losses_lock_risk_gate_until_audited_release(tmp_path):
    from engine.mas.risk_agent import AccountState, evaluate_risk

    ledger = SQLiteLedger(tmp_path / "loss-streak.db")
    account = "paper-account"
    closes = [
        ("loss-1", "2026-11-18T10:00:00+00:00", "-10"),
        ("loss-2", "2026-11-18T11:00:00+00:00", "-12"),
        ("loss-3", "2026-11-18T12:00:00+00:00", "-8"),
    ]

    for trade_id, _closed_at, _pnl in closes:
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionOpened", "idempotency_key": f"open:{trade_id}",
            "occurred_at": "2026-11-18T09:00:00+00:00",
            "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                        "entry_price": "1.1000"},
        })

    # Intentionally append in reverse order; replay must use actual close timestamps.
    for trade_id, closed_at, pnl in reversed(closes):
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionClosed", "idempotency_key": f"close:{trade_id}",
            "occurred_at": closed_at,
            "payload": {"lots_closed": "1", "pnl_gross": pnl,
                        "commission": "0", "slippage": "0", "swap": "0",
                        "consecutive_loss_stop": 3, "exit_time": closed_at},
        })

    locked_state = ledger.replay_account(account, "10000")
    assert locked_state["consecutive_losses"] == 3
    assert locked_state["risk_lock_active"] is True

    locked_account = AccountState(
        equity=float(locked_state["equity"]),
        consecutive_losses=locked_state["consecutive_losses"],
        risk_lock_active=locked_state["risk_lock_active"],
    )
    blocked = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
        acct=locked_account,
    )
    assert blocked.approved is False
    assert any(g.gate == "R-ADDTOLOSS" and not g.passed for g in blocked.gates)

    # A later winning close must not silently clear a hard lock.
    ledger.append_event({
        "account_id": account, "trade_id": "win-after-lock",
        "event_type": "PositionOpened", "idempotency_key": "open:win-after-lock",
        "occurred_at": "2026-11-18T13:00:00+00:00",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                    "entry_price": "1.1000"},
    })
    ledger.append_event({
        "account_id": account, "trade_id": "win-after-lock",
        "event_type": "PositionClosed", "idempotency_key": "close:win-after-lock",
        "occurred_at": "2026-11-18T14:00:00+00:00",
        "payload": {"lots_closed": "1", "pnl_gross": "15",
                    "commission": "0", "slippage": "0", "swap": "0",
                    "consecutive_loss_stop": 3},
    })
    still_locked = ledger.replay_account(account, "10000")
    assert still_locked["consecutive_losses"] == 3
    assert still_locked["risk_lock_active"] is True

    ledger.append_event({
        "account_id": account, "event_type": "RiskLockReleased",
        "idempotency_key": "review:release-after-loss-streak",
        "occurred_at": "2026-11-18T15:00:00+00:00",
        "schema_version": 2,
        "payload": {"consecutive_losses": 0, "reviewed_by": "independent-review",
                    "reason": "audited test release",
                    "approval_reference": "test-approval-001"},
    })
    released = ledger.replay_account(account, "10000")
    assert released["consecutive_losses"] == 0
    assert released["risk_lock_active"] is False

    unlocked_account = AccountState(
        equity=float(released["equity"]),
        consecutive_losses=released["consecutive_losses"],
        risk_lock_active=released["risk_lock_active"],
    )
    approved = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
        acct=unlocked_account,
    )
    assert approved.approved is True



def test_daily_and_weekly_resets_never_release_hard_loss_lock(tmp_path):
    ledger = SQLiteLedger(tmp_path / "period-lock.db")
    account = "paper-account"
    losses = (
        ("loss-1", "2026-11-20T20:00:00+00:00"),
        ("loss-2", "2026-11-20T21:00:00+00:00"),
        ("loss-3", "2026-11-20T22:00:00+00:00"),
    )
    for trade_id, closed_at in losses:
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionOpened", "idempotency_key": f"open:{trade_id}",
            "occurred_at": "2026-11-20T19:00:00+00:00",
            "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                        "entry_price": "1.1000"},
        })
        ledger.append_event({
            "account_id": account, "trade_id": trade_id,
            "event_type": "PositionClosed", "idempotency_key": f"close:{trade_id}",
            "occurred_at": closed_at,
            "payload": {"lots_closed": "1", "pnl_gross": "-10",
                        "commission": "0", "slippage": "0", "swap": "0",
                        "consecutive_loss_stop": 3},
        })

    locked = ledger.replay_account(account, "10000")
    assert locked["consecutive_losses"] == 3
    assert locked["risk_lock_active"] is True

    # Cross both the calendar-day and ISO-week boundaries. These are budget
    # resets only; neither event is an authorization to clear the hard lock.
    ledger.append_events([
        {
            "account_id": account, "event_type": "DailyRiskReset",
            "idempotency_key": "daily-reset:2026-11-23",
            "occurred_at": "2026-11-23T00:00:00+00:00",
            "payload": {"daily_risk_date": "2026-11-23"},
        },
        {
            "account_id": account, "event_type": "WeeklyRiskReset",
            "idempotency_key": "weekly-reset:2026-11-23",
            "occurred_at": "2026-11-23T00:00:00+00:00",
            "payload": {"weekly_risk_week": "2026-11-23"},
        },
    ])

    after_resets = ledger.replay_account(account, "10000")
    assert after_resets["daily_risk_date"] == "2026-11-23"
    assert after_resets["weekly_risk_week"] == "2026-11-23"
    assert after_resets["daily_loss_pct"] == "0"
    assert after_resets["weekly_loss_pct"] == "0"
    assert after_resets["consecutive_losses"] == 3
    assert after_resets["risk_lock_active"] is True

    from engine.mas.risk_agent import AccountState, evaluate_risk
    verdict = evaluate_risk(
        "EURUSD", "BUY", 1.1000, 1.0990, 1.1020, 1.1000,
        acct=AccountState(
            equity=float(after_resets["equity"]),
            consecutive_losses=after_resets["consecutive_losses"],
            risk_lock_active=after_resets["risk_lock_active"],
        ),
    )
    assert verdict.approved is False
    assert any(g.gate == "R-ADDTOLOSS" and not g.passed for g in verdict.gates)



def test_concurrent_distinct_appends_allocate_unique_contiguous_sequences(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ledger = SQLiteLedger(tmp_path / "concurrent-sequence.db")
    account = "paper-account"
    workers = 8
    barrier = Barrier(workers)

    def append_one(index):
        barrier.wait(timeout=10)
        return ledger.append_event({
            "account_id": account,
            "event_type": "AuditMarker",
            "idempotency_key": f"concurrent:{index}",
            "occurred_at": f"2026-11-19T10:00:{index:02d}+00:00",
            "payload": {"worker": str(index)},
        })

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(append_one, range(workers)))

    assert all(inserted is True for _event, inserted in results)
    events = ledger.read_events(account)
    assert len(events) == workers
    assert [event.sequence for event in events] == list(range(1, workers + 1))
    assert len({event.idempotency_key for event in events}) == workers


def test_concurrent_same_idempotency_key_commits_exactly_one_event(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ledger = SQLiteLedger(tmp_path / "concurrent-idempotency.db")
    account = "paper-account"
    workers = 8
    barrier = Barrier(workers)
    ledger.append_event({
        "account_id": account,
        "trade_id": "single-close",
        "event_type": "PositionOpened",
        "idempotency_key": "open:single-close",
        "payload": {"symbol": "EURUSD", "direction": "BUY", "lots": "1",
                    "entry_price": "1.1000"},
    })
    event = {
        "account_id": account,
        "trade_id": "single-close",
        "event_type": "PositionClosed",
        "idempotency_key": "close:single-close",
        "occurred_at": "2026-11-19T10:00:00+00:00",
        "payload": {
            "lots_closed": "1", "pnl_gross": "-12.50",
            "commission": "0", "slippage": "0", "swap": "0",
        },
    }

    def retry_same_event(_index):
        barrier.wait(timeout=10)
        return ledger.append_event(event)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(retry_same_event, range(workers)))

    assert sum(1 for _stored, inserted in results if inserted) == 1
    assert sum(1 for _stored, inserted in results if not inserted) == workers - 1
    events = ledger.read_events(account)
    assert len(events) == 2
    assert events[1].sequence == 2
    assert events[1].idempotency_key == event["idempotency_key"]
    assert events[1].payload == event["payload"]


def _v2_open_event(
    key="v2-open-1", trade_id="trade-v2", payload_overrides=None, **snapshot_overrides
):
    sizing = {
        "sizing_id": "size-001",
        "approved": True,
        "symbol": "EURUSD",
        "direction": "BUY",
        "entry_price": "1.10000",
        "stop_loss": "1.09900",
        "lots": "0.50",
        "requested_risk": "50.00",
        "actual_risk": "50.00",
        "per_lot_risk": "100.00",
        "lot_step": "0.01",
        "min_lot": "0.01",
        "max_lot": "50.00",
        "spec_version": "spec-v1",
        "fx_rate": "",
        "fx_timestamp": "",
        "rounding_mode": "ROUND_DOWN",
        "risk_config_hash": "risk-hash-v1",
        "reason": "",
    }
    sizing.update(snapshot_overrides)
    payload = {
        "symbol": "EURUSD",
        "direction": "BUY",
        "lots": "0.50",
        "entry_price": "1.10000",
        "sl": "1.09900",
        "tp2": "1.10200",
        "sizing_result": sizing,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return {
        "account_id": "paper-account",
        "trade_id": trade_id,
        "event_type": "PositionOpened",
        "idempotency_key": key,
        "schema_version": 2,
        "payload": payload,
    }


def test_v2_position_open_requires_and_preserves_authorized_sizing_snapshot(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-v2.db")
    event, created = ledger.append_event(_v2_open_event())
    assert created is True
    projection = ledger.replay_account("paper-account", "10000.00")
    assert projection["open_positions"]["trade-v2"]["sizing_result"]["sizing_id"] == "size-001"


def test_v2_sizing_snapshot_survives_database_restart_and_replay(tmp_path):
    path = tmp_path / "ledger-v2-restart.db"
    original = SQLiteLedger(path)
    stored, created = original.append_event(_v2_open_event())

    assert created is True
    assert stored.schema_version == 2
    expected_snapshot = stored.payload["sizing_result"]

    # Constructing a new ledger instance models a process restart against the
    # same durable SQLite file; no in-memory projection is reused.
    restarted = SQLiteLedger(path)
    replayed_events = restarted.read_events("paper-account")
    assert len(replayed_events) == 1
    assert replayed_events[0].payload["sizing_result"] == expected_snapshot

    projection = restarted.replay_account("paper-account", "10000.00")
    position = projection["open_positions"]["trade-v2"]
    assert position["sizing_result"] == expected_snapshot
    assert position["lots"] == "0.50"


@pytest.mark.parametrize(
    "overrides, message, payload_overrides",
    [
        ({"approved": False}, "explicitly approved", None),
        ({"symbol": "GBPUSD"}, "identity does not match", None),
        ({"lots": "0.51"}, "lots/entry/stop do not match", None),
        ({"actual_risk": "51.00"}, "risk arithmetic is inconsistent", None),
        (
            {"lots": "0.505", "actual_risk": "50.500", "requested_risk": "51.000"},
            "lot_step",
            {"lots": "0.505"},
        ),
        ({"max_lot": "0.40"}, "outside min/max", None),
        ({"stop_loss": "1.10100"}, "BUY stop_loss", {"sl": "1.10100"}),
        ({"requested_risk": "NaN"}, "finite and positive", None),
    ],
)
def test_v2_position_open_rejects_invalid_sizing_snapshot(
    tmp_path, overrides, message, payload_overrides
):
    ledger = SQLiteLedger(tmp_path / "ledger-v2.db")
    with pytest.raises(LedgerError, match=message):
        ledger.append_event(
            _v2_open_event(payload_overrides=payload_overrides, **overrides)
        )
    assert ledger.read_events("paper-account") == []



def test_v2_position_open_rejects_missing_sizing_snapshot(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-v2-missing-snapshot.db")
    event = _v2_open_event()
    event["payload"].pop("sizing_result")
    with pytest.raises(LedgerError, match="requires sizing_result"):
        ledger.append_event(event)
    assert ledger.read_events("paper-account") == []


def test_v2_position_open_rejects_missing_required_snapshot_field(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-v2-missing-field.db")
    event = _v2_open_event()
    event["payload"]["sizing_result"].pop("risk_config_hash")
    with pytest.raises(LedgerError, match="missing fields: risk_config_hash"):
        ledger.append_event(event)
    assert ledger.read_events("paper-account") == []


@pytest.mark.parametrize("invalid_step", ["0", "-0.01"])
def test_v2_position_open_rejects_non_positive_lot_step(tmp_path, invalid_step):
    ledger = SQLiteLedger(tmp_path / f"ledger-v2-lot-step-{invalid_step}.db")
    with pytest.raises(LedgerError, match="finite and positive"):
        ledger.append_event(_v2_open_event(lot_step=invalid_step))
    assert ledger.read_events("paper-account") == []

def test_v2_position_open_requires_non_empty_trade_id(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-v2-no-trade-id.db")
    with pytest.raises(LedgerError, match="non-empty trade_id"):
        ledger.append_event(_v2_open_event(trade_id=None))
    assert ledger.read_events("paper-account") == []


def test_v1_legacy_position_open_without_sizing_snapshot_remains_accepted(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-v1.db")
    legacy = {
        "account_id": "paper-account",
        "trade_id": "legacy-trade",
        "event_type": "PositionOpened",
        "idempotency_key": "legacy-open",
        "schema_version": 1,
        "payload": {
            "symbol": "EURUSD", "direction": "BUY", "lots": "1",
            "entry_price": "1.1000",
        },
    }
    ledger.append_event(legacy)
    projection = ledger.replay_account("paper-account", "1000")
    assert projection["open_positions"]["legacy-trade"]["sizing_result"] is None



def test_v2_replay_rejects_corrupted_persisted_sizing_snapshot(tmp_path):
    import json

    ledger = SQLiteLedger(tmp_path / "ledger-v2-corruption.db")
    ledger.append_event(_v2_open_event())

    # Simulate out-of-band database tampering, not a supported Ledger write.
    # Temporarily remove the append-only UPDATE trigger only in this isolated
    # test database, corrupt one financial field, then restore the trigger.
    conn = ledger._connect()
    try:
        conn.execute("DROP TRIGGER ledger_events_no_update")
        row = conn.execute(
            "SELECT payload_json FROM ledger_events WHERE account_id=?",
            ("paper-account",),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["sizing_result"]["actual_risk"] = "49.00"
        conn.execute(
            "UPDATE ledger_events SET payload_json=? WHERE account_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), "paper-account"),
        )
        conn.execute(
            """
            CREATE TRIGGER ledger_events_no_update
            BEFORE UPDATE ON ledger_events
            BEGIN
                SELECT RAISE(ABORT, 'ledger_events is append-only');
            END
            """
        )
    finally:
        conn.close()

    with pytest.raises(LedgerError, match="risk arithmetic is inconsistent"):
        ledger.replay_account("paper-account", "10000.00")


def test_append_events_rolls_back_entire_batch_on_late_insert_conflict(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger-batch-atomicity.db")
    shared_event_id = "duplicate-event-id"

    first = {
        "event_id": shared_event_id,
        "account_id": "paper-account",
        "event_type": "AuditMarker",
        "idempotency_key": "atomicity:first",
        "payload": {"marker": "first"},
    }
    second = {
        "event_id": shared_event_id,
        "account_id": "paper-account",
        "event_type": "AuditMarker",
        "idempotency_key": "atomicity:second",
        "payload": {"marker": "second"},
    }

    # Both inputs pass application validation. The second INSERT then violates
    # the event_id primary key after the first INSERT has executed in the
    # transaction. The batch must leave no partially committed event behind.
    with pytest.raises(sqlite3.IntegrityError):
        ledger.append_events([first, second])

    assert ledger.read_events("paper-account") == []



def _position_event(event_type, key, trade_id="trade-lifetime-1", payload=None):
    defaults = {
        "PositionOpened": {
            "symbol": "EURUSD", "direction": "BUY", "lots": "1.00",
            "entry_price": "1.10000",
        },
        "PositionClosed": {
            "lots_closed": "1.00", "pnl_gross": "10.00",
            "commission": "0", "slippage": "0", "swap": "0",
        },
    }
    return {
        "account_id": "paper-account",
        "trade_id": trade_id,
        "event_type": event_type,
        "idempotency_key": key,
        "payload": payload if payload is not None else defaults[event_type],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("commission", "-0.01"), ("slippage", "-1"), ("commission", "NaN"),
     ("slippage", "Infinity")],
)
def test_close_rejects_negative_or_non_finite_commission_and_slippage(
    tmp_path, field, value
):
    ledger = SQLiteLedger(tmp_path / "invalid-close-cost.db")
    ledger.append_event(_position_event("PositionOpened", "open-cost"))
    close = _position_event(
        "PositionClosed", "close-cost",
        payload={
            "lots_closed": "1.00", "pnl_gross": "10.00",
            "commission": "0", "slippage": "0", "swap": "0",
        },
    )
    close["payload"][field] = value

    with pytest.raises(LedgerError):
        ledger.append_event(close)

    assert [event.event_type for event in ledger.read_events("paper-account")] == [
        "PositionOpened"
    ]


def test_signed_swap_is_allowed_and_applied_to_net_pnl(tmp_path):
    ledger = SQLiteLedger(tmp_path / "signed-swap.db")
    ledger.append_event(_position_event("PositionOpened", "open-swap"))
    close = _position_event(
        "PositionClosed", "close-swap",
        payload={
            "lots_closed": "1.00", "pnl_gross": "10.00",
            "commission": "1.00", "slippage": "0.50", "swap": "-0.25",
        },
    )
    ledger.append_event(close)
    state = ledger.replay_account("paper-account", "1000.00")
    assert state["realized_pnl"] == "8.75"


def test_trade_id_cannot_be_reused_after_position_is_closed(tmp_path):
    ledger = SQLiteLedger(tmp_path / "trade-id-lifetime.db")
    ledger.append_events([
        _position_event("PositionOpened", "open-first"),
        _position_event("PositionClosed", "close-first"),
    ])

    with pytest.raises(LedgerConflictError, match="trade_id has already been used"):
        ledger.append_event(_position_event("PositionOpened", "open-second"))

    assert len(ledger.read_events("paper-account")) == 2


def test_duplicate_trade_id_in_batch_rolls_back_entire_batch(tmp_path):
    ledger = SQLiteLedger(tmp_path / "trade-id-batch.db")
    batch = [
        _position_event("PositionOpened", "open-one", trade_id="same-id"),
        _position_event("PositionOpened", "open-two", trade_id="same-id"),
    ]

    with pytest.raises(LedgerConflictError, match="trade_id has already been used"):
        ledger.append_events(batch)

    assert ledger.read_events("paper-account") == []


def test_position_open_idempotent_retry_does_not_consume_trade_id_twice(tmp_path):
    ledger = SQLiteLedger(tmp_path / "trade-id-idempotency.db")
    opened = _position_event("PositionOpened", "open-idempotent")
    first, inserted = ledger.append_event(opened)
    retry, inserted_retry = ledger.append_event(opened)

    assert inserted is True
    assert inserted_retry is False
    assert first.event_id == retry.event_id
    assert len(ledger.read_events("paper-account")) == 1


def test_replay_rejects_reused_trade_id_even_after_prior_close(tmp_path):
    ledger = SQLiteLedger(tmp_path / "trade-id-replay-guard.db")
    ledger.append_events([
        _position_event("PositionOpened", "open-first"),
        _position_event("PositionClosed", "close-first"),
    ])

    # Simulate an older/corrupt persisted event bypassing the append API.
    conn = ledger._connect()
    try:
        conn.execute(
            """
            INSERT INTO ledger_events (
                event_id, account_id, trade_id, order_id, event_type, sequence,
                occurred_at, recorded_at, idempotency_key, schema_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "corrupt-reopen", "paper-account", "trade-lifetime-1", None,
                "PositionOpened", 3, "2026-11-21T00:00:00+00:00",
                "2026-11-21T00:00:00+00:00", "corrupt-reopen-key", 1,
                '{"direction":"BUY","entry_price":"1.10000","lots":"1.00","symbol":"EURUSD"}',
            ),
        )
    finally:
        conn.close()

    with pytest.raises(LedgerError, match="unique for the account lifetime"):
        ledger.replay_account("paper-account", "1000.00")



def test_append_rejects_close_for_unknown_trade_without_persisting(tmp_path):
    ledger = SQLiteLedger(tmp_path / "unknown-close-precommit.db")
    with pytest.raises(LedgerError, match="unknown position"):
        ledger.append_event(_position_event("PositionClosed", "close-unknown"))
    assert ledger.read_events("paper-account") == []


def test_append_rejects_over_close_before_commit(tmp_path):
    ledger = SQLiteLedger(tmp_path / "over-close-precommit.db")
    ledger.append_event(_position_event("PositionOpened", "open-over"))
    close = _position_event(
        "PositionClosed", "close-over",
        payload={
            "lots_closed": "1.01", "pnl_gross": "1",
            "commission": "0", "slippage": "0", "swap": "0",
        },
    )
    with pytest.raises(LedgerError, match="<= remaining lots"):
        ledger.append_event(close)
    assert [event.event_type for event in ledger.read_events("paper-account")] == [
        "PositionOpened"
    ]


def test_append_rejects_incomplete_final_close_before_commit(tmp_path):
    ledger = SQLiteLedger(tmp_path / "incomplete-close-precommit.db")
    ledger.append_event(_position_event("PositionOpened", "open-incomplete"))
    close = _position_event(
        "PositionClosed", "close-incomplete",
        payload={
            "lots_closed": "0.50", "pnl_gross": "1",
            "commission": "0", "slippage": "0", "swap": "0",
        },
    )
    with pytest.raises(LedgerError, match="full remaining quantity"):
        ledger.append_event(close)
    assert len(ledger.read_events("paper-account")) == 1


def test_invalid_late_event_rolls_back_entire_position_batch(tmp_path):
    ledger = SQLiteLedger(tmp_path / "position-batch-rollback.db")
    batch = [
        _position_event("PositionOpened", "batch-open", trade_id="batch-trade"),
        _position_event(
            "PositionClosed", "batch-invalid-close", trade_id="not-opened",
            payload={
                "lots_closed": "1", "pnl_gross": "10",
                "commission": "0", "slippage": "0", "swap": "0",
            },
        ),
    ]
    with pytest.raises(LedgerError, match="unknown position"):
        ledger.append_events(batch)
    assert ledger.read_events("paper-account") == []


def test_append_validates_mark_references_active_position(tmp_path):
    ledger = SQLiteLedger(tmp_path / "unknown-mark-precommit.db")
    with pytest.raises(LedgerError, match="unknown position"):
        ledger.append_event({
            "account_id": "paper-account",
            "trade_id": "missing-trade",
            "event_type": "PositionMarked",
            "idempotency_key": "mark:missing",
            "payload": {"unrealized_pnl": "1.00"},
        })
    assert ledger.read_events("paper-account") == []



def test_floating_marks_drive_daily_and_weekly_equity_risk(tmp_path):
    from engine.mas.ledger import SQLiteLedger

    ledger = SQLiteLedger(tmp_path / "floating-risk.db")
    account = "floating-risk"
    ledger.append_event({
        "account_id": account, "trade_id": "marked-trade",
        "event_type": "PositionOpened", "idempotency_key": "open:marked-trade",
        "occurred_at": "2026-01-05T09:00:00+00:00",
        "payload": {
            "symbol": "EURUSD", "direction": "BUY", "lots": "1",
            "entry_price": "1.1000",
        },
    })

    def mark(key, at, pnl):
        ledger.append_event({
            "account_id": account, "trade_id": "marked-trade",
            "event_type": "PositionMarked", "idempotency_key": key,
            "occurred_at": at,
            "payload": {"unrealized_pnl": pnl, "mark_time": at},
        })

    mark("mark:minus-50", "2026-01-05T10:00:00+00:00", "-50")
    first = ledger.replay_account(account, "1000")
    assert first["unrealized_pnl"] == "-50"
    assert first["equity"] == "950"
    assert first["daily_loss_pct"] == "5"
    assert first["weekly_loss_pct"] == "5"

    # A newer mark replaces the previous mark; it is not added to it.
    mark("mark:minus-20", "2026-01-05T11:00:00+00:00", "-20")
    second = ledger.replay_account(account, "1000")
    assert second["unrealized_pnl"] == "-20"
    assert second["equity"] == "980"
    # Period risk retains the worst intraday equity excursion.
    assert second["daily_loss_pct"] == "5"
    assert second["weekly_loss_pct"] == "5"

    ledger.append_event({
        "account_id": account, "trade_id": "marked-trade",
        "event_type": "PositionClosed", "idempotency_key": "close:marked-trade",
        "occurred_at": "2026-01-05T12:00:00+00:00",
        "payload": {
            "lots_closed": "1", "pnl_gross": "-20",
            "commission": "0", "slippage": "0", "swap": "0",
        },
    })
    closed = ledger.replay_account(account, "1000")
    assert closed["unrealized_pnl"] == "0"
    assert closed["equity"] == "980"
    assert closed["daily_loss_pct"] == "5"
    assert closed["weekly_loss_pct"] == "5"


def test_floating_profit_then_equity_reversal_uses_period_opening_equity(tmp_path):
    from engine.mas.ledger import SQLiteLedger

    ledger = SQLiteLedger(tmp_path / "floating-profit-risk.db")
    account = "floating-profit-risk"
    ledger.append_event({
        "account_id": account, "trade_id": "profit-trade",
        "event_type": "PositionOpened", "idempotency_key": "open:profit-trade",
        "occurred_at": "2026-01-05T09:00:00+00:00",
        "payload": {
            "symbol": "EURUSD", "direction": "BUY", "lots": "1",
            "entry_price": "1.1000",
        },
    })
    for key, at, pnl in (
        ("mark:plus-100", "2026-01-05T10:00:00+00:00", "100"),
        ("mark:minus-20", "2026-01-05T11:00:00+00:00", "-20"),
    ):
        ledger.append_event({
            "account_id": account, "trade_id": "profit-trade",
            "event_type": "PositionMarked", "idempotency_key": key,
            "occurred_at": at,
            "payload": {"unrealized_pnl": pnl, "mark_time": at},
        })

    state = ledger.replay_account(account, "1000")
    assert state["equity"] == "980"
    assert state["unrealized_pnl"] == "-20"
    assert state["daily_loss_pct"] == "2"
    assert state["weekly_loss_pct"] == "2"



def test_same_timestamp_marks_are_aggregated_before_risk_measurement(tmp_path):
    from engine.mas.ledger import SQLiteLedger

    ledger = SQLiteLedger(tmp_path / "same-time-marks.db")
    account = "same-time-marks"
    ledger.append_events([
        {
            "account_id": account, "trade_id": "loss-trade",
            "event_type": "PositionOpened", "idempotency_key": "open:loss",
            "occurred_at": "2026-01-05T09:00:00+00:00",
            "payload": {"symbol": "EURUSD", "direction": "BUY",
                        "lots": "1", "entry_price": "1.1000"},
        },
        {
            "account_id": account, "trade_id": "profit-trade",
            "event_type": "PositionOpened", "idempotency_key": "open:profit",
            "occurred_at": "2026-01-05T09:00:00+00:00",
            "payload": {"symbol": "GBPUSD", "direction": "BUY",
                        "lots": "1", "entry_price": "1.3000"},
        },
        {
            "account_id": account, "trade_id": "loss-trade",
            "event_type": "PositionMarked", "idempotency_key": "mark:loss",
            "occurred_at": "2026-01-05T10:00:00+00:00",
            "payload": {"unrealized_pnl": "-50",
                        "mark_time": "2026-01-05T10:00:00+00:00"},
        },
        {
            "account_id": account, "trade_id": "profit-trade",
            "event_type": "PositionMarked", "idempotency_key": "mark:profit",
            "occurred_at": "2026-01-05T10:00:00+00:00",
            "payload": {"unrealized_pnl": "100",
                        "mark_time": "2026-01-05T10:00:00+00:00"},
        },
    ])

    state = ledger.replay_account(account, "1000")
    assert state["equity"] == "1050"
    assert state["unrealized_pnl"] == "50"
    assert state["daily_loss_pct"] == "0"
    assert state["weekly_loss_pct"] == "0"
