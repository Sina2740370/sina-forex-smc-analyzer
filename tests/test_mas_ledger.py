"""Regression tests for the pure MAS risk ledger."""
from decimal import Decimal

import pytest

from engine.mas.ledger import RiskLedger


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
