"""Tests for the transactional SQLite event ledger."""
import pytest

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
                    "risk_lock_active": False, "runs": 4,
                    "last_run_ts": "2026-11-17T20:00:00+00:00",
                    "daily_risk_date": "2026-11-17"},
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
                    "daily_loss_pct_delta": "0.5", "consecutive_loss_stop": 3,
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
    assert state["daily_loss_pct"] == "1.0"
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