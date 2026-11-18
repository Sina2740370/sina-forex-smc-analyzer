from copy import deepcopy
import pytest

from engine.mas.ledger import RiskLedger
from engine.mas.portfolio_loop import (
    commit_portfolio_events,
    order_portfolio_events,
)


T0 = "2026-11-18T12:00:00Z"
T1 = "2026-11-18T12:15:00+00:00"


def _ledger():
    return RiskLedger("10000")


def _open(event_id, symbol, position_id, timestamp=T0, side="BUY"):
    entry, stop = ("1.1000", "1.0900") if side == "BUY" else ("1.2500", "1.2600")
    return {
        "event_id": event_id, "sequence": 999, "timestamp": timestamp,
        "type": "PositionOpened", "position_id": position_id,
        "symbol": symbol, "side": side, "quantity": "1",
        "entry": entry, "stop": stop, "approved_risk": "100",
        "actual_risk": "90", "currency": "USD",
    }


def _mark(event_id, timestamp=T1, pnl="0"):
    return {
        "event_id": event_id, "sequence": 999, "timestamp": timestamp,
        "type": "MarkToMarketUpdated", "unrealized_pnl": pnl,
        "currency": "USD",
    }


def test_order_is_invariant_to_symbol_input_permutation():
    first = [_open("open-z", "GBPUSD", "p-z"), _open("open-a", "EURUSD", "p-a")]
    second = list(reversed(first))
    ordered_a = order_portfolio_events(_ledger(), first)
    ordered_b = order_portfolio_events(_ledger(), second)
    assert ordered_a == ordered_b
    assert [e["sequence"] for e in ordered_a] == [1, 2]
    assert [e["symbol"] for e in ordered_a] == ["EURUSD", "GBPUSD"]


def test_same_timestamp_period_settlement_open_mark_priority():
    events = [
        _mark("mark", timestamp=T0),
        _open("open", "EURUSD", "p1"),
        {
            "event_id": "roll", "sequence": 999, "timestamp": T0,
            "type": "PeriodRolled", "daily_period_id": "D2",
            "currency": "USD",
        },
    ]
    # The synthetic rollover is valid for a non-initial current period only.
    ledger = RiskLedger("10000", daily_period_id="D1", weekly_period_id="W1")
    ordered = order_portfolio_events(ledger, events)
    assert [e["type"] for e in ordered] == [
        "PeriodRolled", "PositionOpened", "MarkToMarketUpdated",
    ]
    assert [e["sequence"] for e in ordered] == [1, 2, 3]


def test_chronological_order_prevents_backwards_ledger_timestamps():
    later = _open("later", "GBPUSD", "p-later", timestamp=T1)
    earlier = _open("earlier", "EURUSD", "p-earlier", timestamp=T0)
    ordered = order_portfolio_events(_ledger(), [later, earlier])
    assert [e["event_id"] for e in ordered] == ["earlier", "later"]
    assert [e["sequence"] for e in ordered] == [1, 2]


def test_exact_retry_reuses_original_events_and_sequences():
    ledger = _ledger()
    original = order_portfolio_events(
        ledger, [_open("open-a", "EURUSD", "p-a")]
    )
    for event in original:
        ledger.apply(event)
    retry = order_portfolio_events(ledger, [_open("open-a", "EURUSD", "p-a")])
    assert retry == original


def test_divergent_retry_and_mixed_retry_batch_fail_closed():
    ledger = _ledger()
    original = order_portfolio_events(ledger, [_open("open-a", "EURUSD", "p-a")])
    ledger.apply(original[0])
    changed = _open("open-a", "EURUSD", "p-a")
    changed["quantity"] = "2"
    with pytest.raises(ValueError, match="different portfolio facts"):
        order_portfolio_events(ledger, [changed])
    with pytest.raises(ValueError, match="mix exact retries"):
        order_portfolio_events(
            ledger,
            [original[0], _open("open-b", "GBPUSD", "p-b")],
        )


def test_invalid_timestamp_unknown_event_and_duplicate_ids_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        order_portfolio_events(_ledger(), [_open("naive", "EURUSD", "p", "2026-11-18T12:00:00")])
    bad = _open("bad", "EURUSD", "p")
    bad["type"] = "UnknownEvent"
    with pytest.raises(ValueError, match="unsupported"):
        order_portfolio_events(_ledger(), [bad])
    duplicate = _open("same", "EURUSD", "p1")
    with pytest.raises(ValueError, match="duplicate event IDs"):
        order_portfolio_events(_ledger(), [duplicate, _open("same", "GBPUSD", "p2")])


def test_invalid_batch_does_not_mutate_ledger():
    ledger = _ledger()
    before = deepcopy(ledger.snapshot())
    events = [_open("valid", "EURUSD", "p1"), _open("invalid", "GBPUSD", "p2", side="BUY")]
    events[1]["stop"] = "1.3000"
    with pytest.raises(ValueError):
        order_portfolio_events(ledger, events)
    assert ledger.snapshot() == before


def test_commit_adapter_uses_ordered_atomic_batch(tmp_path):
    from engine.mas.ledger_store import SQLiteLedgerStore
    from engine.mas.paper_adapter import PaperLedgerAdapter

    store = SQLiteLedgerStore(tmp_path / "ordered-loop.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:ordered",
        initial_balance="10000", timestamp=T0,
    )
    events = [
        _open("open-b", "GBPUSD", "p-b"),
        _open("open-a", "EURUSD", "p-a"),
        _mark("mark", timestamp=T1, pnl="12.5"),
    ]
    assert commit_portfolio_events(adapter, events) is True
    snapshot = adapter.ledger.snapshot()
    assert snapshot["sequence"] == 3
    assert snapshot["unrealized_pnl"] == "12.5"
    assert [e["event_id"] for e in snapshot["applied_events"]] == [
        "open-a", "open-b", "mark",
    ]
    assert store.load("paper:ordered").snapshot() == snapshot


def test_commit_adapter_exact_retry_is_idempotent(tmp_path):
    from engine.mas.ledger_store import SQLiteLedgerStore
    from engine.mas.paper_adapter import PaperLedgerAdapter

    store = SQLiteLedgerStore(tmp_path / "ordered-retry.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:retry",
        initial_balance="10000", timestamp=T0,
    )
    events = [_open("open-a", "EURUSD", "p-a")]
    assert commit_portfolio_events(adapter, events) is True
    before = adapter.ledger.snapshot()
    assert commit_portfolio_events(adapter, events) is False
    assert adapter.ledger.snapshot() == before
    assert store.load("paper:retry").snapshot() == before



def test_multiple_aggregate_marks_at_same_timestamp_are_rejected():
    with pytest.raises(ValueError, match="one aggregate MTM"):
        order_portfolio_events(
            _ledger(),
            [_mark("mark-a", timestamp=T0), _mark("mark-b", timestamp=T0)],
        )
