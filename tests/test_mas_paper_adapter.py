from decimal import Decimal

import pytest

from engine.mas.ledger_store import SQLiteLedgerStore
from engine.mas.paper_adapter import PaperLedgerAdapter


NOW = "2026-11-18T10:15:00Z"


def _adapter(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "paper-adapter.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:test-account",
        initial_balance="10000.00", timestamp=NOW,
    )
    return store, adapter


def _open(adapter):
    return adapter.commit_open_position(
        symbol="EURUSD", side="BUY", quantity="1",
        entry="1.10000", stop="1.09500",
        approved_risk="50", actual_risk="48",
        decision_timestamp=NOW, candidate_identity="signal-42:ob-7",
    )


def test_create_open_and_reload_recover_identical_ledger(tmp_path):
    store, adapter = _adapter(tmp_path)
    position_id = _open(adapter)
    assert adapter.ledger.sequence == 1
    restored = PaperLedgerAdapter.load(
        store=store, ledger_id="paper:test-account",
    )
    assert restored.ledger.snapshot() == adapter.ledger.snapshot()
    assert position_id in restored.ledger.snapshot()["open_positions"]


def test_commit_open_position_wrapper_is_idempotent_after_retry(tmp_path):
    store, adapter = _adapter(tmp_path)
    facts = {
        "symbol": "EURUSD", "side": "BUY", "quantity": "1",
        "entry": "1.1", "stop": "1.09", "approved_risk": "50",
        "actual_risk": "48", "decision_timestamp": NOW,
        "candidate_identity": "signal-repeat",
    }
    first = adapter.commit_open_position(**facts)
    second = adapter.commit_open_position(**facts)
    assert first == second
    assert adapter.ledger.sequence == 1
    assert store.load("paper:test-account").snapshot() == adapter.ledger.snapshot()


def test_mark_update_is_idempotent_by_mark_identity(tmp_path):
    store, adapter = _adapter(tmp_path)
    assert adapter.update_mark(
        mark_id="mark-1", unrealized_pnl="-12.50", timestamp=NOW
    ) is True
    assert adapter.update_mark(
        mark_id="mark-1", unrealized_pnl="-12.50", timestamp=NOW
    ) is False
    assert adapter.ledger.sequence == 1
    assert store.load("paper:test-account").snapshot() == adapter.ledger.snapshot()


def test_open_identity_is_deterministic_and_duplicate_safe(tmp_path):
    store, adapter = _adapter(tmp_path)
    position_id, event = adapter.build_open_event(
        symbol="EURUSD", side="BUY", quantity="1", entry="1.1",
        stop="1.09", approved_risk="50", actual_risk="48",
        decision_timestamp=NOW, candidate_identity="signal-42",
    )
    assert adapter.commit_event(event) is True
    assert adapter.commit_event(event) is False
    assert position_id in store.load("paper:test-account").snapshot()["open_positions"]


def test_settlement_batch_is_atomic_persistent_and_retry_safe(tmp_path):
    store, adapter = _adapter(tmp_path)
    position_id = _open(adapter)
    events = adapter.build_settlement_events(
        position_id=position_id, execution_identity="exit-fill-1",
        quantity="1", gross_pnl="75", timestamp=NOW,
        remaining_unrealized_pnl="0", mark_id="after-exit-1",
        commission="1", swap="-0.5", other_fees="0",
    )
    assert [event["type"] for event in events] == [
        "PositionClosed", "MarkToMarketUpdated",
    ]
    assert adapter.commit_events(events) is True
    assert adapter.ledger.balance == Decimal("10074.5")
    assert adapter.ledger.equity == Decimal("10074.5")
    assert adapter.commit_events(events) is False
    assert store.load("paper:test-account").snapshot() == adapter.ledger.snapshot()


def test_binary_float_rejected_before_ledger_mutation(tmp_path):
    _, adapter = _adapter(tmp_path)
    before = adapter.ledger.snapshot()
    with pytest.raises(ValueError, match="decimal string"):
        adapter.commit_open_position(
            symbol="EURUSD", side="BUY", quantity=1.0,
            entry="1.1", stop="1.09", approved_risk="50",
            actual_risk="48", decision_timestamp=NOW,
            candidate_identity="float-input",
        )
    assert adapter.ledger.snapshot() == before


def test_adapter_never_auto_imports_legacy_state(tmp_path):
    store, adapter = _adapter(tmp_path)
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "journal.jsonl").exists()
    assert adapter.ledger.sequence == 0


def test_period_roll_is_persisted_and_recovered(tmp_path):
    store, adapter = _adapter(tmp_path)
    assert adapter.roll_period("2026-11-19T22:00:00Z") is True
    restored = PaperLedgerAdapter.load(store=store, ledger_id="paper:test-account")
    assert restored.ledger.snapshot() == adapter.ledger.snapshot()


def test_account_state_is_projected_from_ledger(tmp_path):
    _, adapter = _adapter(tmp_path)
    _open(adapter)
    account = adapter.account_state()
    assert account.open_trades == 1
    assert account.per_symbol_open == {"EURUSD": 1}
    assert account.equity == 10000.0


def test_reload_only_publishes_verified_store_state(tmp_path):
    _, adapter = _adapter(tmp_path)
    _open(adapter)
    before = adapter.ledger.snapshot()
    adapter.ledger.balance = Decimal("999")
    adapter.reload()
    assert adapter.ledger.snapshot() == before
