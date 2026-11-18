"""Tests for the transactional SQLite event ledger."""
import pytest
from decimal import Decimal

from engine.mas.ledger import LedgerConflictError, SQLiteLedger


def _event(key="fill-1", payload=None):
    return {
        "account_id": "paper-account",
        "trade_id": "trade-1",
        "event_type": "FillRecorded",
        "idempotency_key": key,
        "payload": payload or {"lots": "0.10", "price": "1.10000", "fee": "0.25"},
    }


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
    ledger.append_event({
        "account_id": "paper-account", "trade_id": "trade-1",
        "event_type": "PositionClosed", "idempotency_key": "close-1",
        "payload": {"lots_closed": "0.50", "pnl_gross": "1.00",
                    "commission": "0", "slippage": "0", "swap": "0"},
    })

    with pytest.raises(LedgerError):
        ledger.replay_account("paper-account", "1000.00")


def test_risk_lock_state_is_replayed_from_events(tmp_path):
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    base = {"account_id": "paper-account", "payload": {}}
    ledger.append_event({**base, "event_type": "RiskLockActivated",
                         "idempotency_key": "lock-1"})
    assert ledger.replay_account("paper-account", "1000")["risk_lock_active"] is True
    ledger.append_event({**base, "event_type": "RiskLockReleased",
                         "idempotency_key": "unlock-1"})
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
