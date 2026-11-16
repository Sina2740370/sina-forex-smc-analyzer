"""Canonical event boundary shared by Paper and Backtest account adapters.

The module is intentionally strategy-agnostic. It converts execution facts into
ledger events and applies them atomically, optionally through SQLite persistence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .ledger import RiskLedger
from .periods import build_period_roll_event


def _utc_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("timestamp must be valid ISO-8601") from None
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be a datetime or ISO-8601 string")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _next_event(ledger: RiskLedger, event_id: str, kind: str,
                timestamp: datetime | str, **fields: Any) -> dict:
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id is required")
    return {
        "event_id": event_id,
        "sequence": ledger.sequence + 1,
        "timestamp": _utc_timestamp(timestamp),
        "type": kind,
        **fields,
    }


def position_opened_event(
    ledger: RiskLedger, *, position_id: str, symbol: str, side: str,
    quantity: Any, entry: Any, stop: Any, approved_risk: Any,
    actual_risk: Any, timestamp: datetime | str,
) -> dict:
    """Build a replay-stable open event; position_id must be stable across retries."""
    if not isinstance(position_id, str) or not position_id.strip():
        raise ValueError("position_id is required")
    return _next_event(
        ledger, f"mas:position:{position_id}:opened", "PositionOpened", timestamp,
        position_id=position_id, symbol=symbol, side=side, quantity=str(quantity),
        entry=str(entry), stop=str(stop), approved_risk=str(approved_risk),
        actual_risk=str(actual_risk), currency=ledger.base_currency,
    )


def position_closed_event(
    ledger: RiskLedger, *, position_id: str, execution_id: str,
    quantity: Any, gross_pnl: Any, timestamp: datetime | str,
    commission: Any = 0, swap: Any = 0, other_fees: Any = 0,
) -> dict:
    """Build a deterministic partial/full close event from a unique execution ID."""
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ValueError("execution_id is required for deterministic close events")
    positions = ledger.snapshot()["open_positions"]
    if position_id not in positions:
        raise ValueError("cannot close an unknown position")
    remaining = Decimal(positions[position_id]["quantity"])
    closing = Decimal(str(quantity))
    if closing <= 0 or closing > remaining:
        raise ValueError("close quantity must be positive and not exceed remaining quantity")
    kind = "PositionClosed" if closing == remaining else "PositionPartiallyClosed"
    return _next_event(
        ledger, f"mas:position:{position_id}:execution:{execution_id}", kind, timestamp,
        position_id=position_id, quantity=str(quantity), gross_pnl=str(gross_pnl),
        commission=str(commission), swap=str(swap), other_fees=str(other_fees),
        currency=ledger.base_currency,
    )


def mark_to_market_event(
    ledger: RiskLedger, *, mark_id: str, unrealized_pnl: Any,
    timestamp: datetime | str,
) -> dict:
    """Build an aggregate-account mark; mark_id identifies one deterministic mark batch."""
    if not isinstance(mark_id, str) or not mark_id.strip():
        raise ValueError("mark_id is required")
    return _next_event(
        ledger, f"mas:mark:{mark_id}", "MarkToMarketUpdated", timestamp,
        unrealized_pnl=str(unrealized_pnl), currency=ledger.base_currency,
    )


def apply_ledger_event(
    ledger: RiskLedger, event: dict, *, store=None, ledger_id: str | None = None,
) -> bool:
    """Apply atomically in memory and, when configured, persist before publishing state.

    The caller's ledger is not mutated if validation or persistence fails.
    """
    if not isinstance(ledger, RiskLedger):
        raise ValueError("ledger must be a RiskLedger")
    previous_sequence = ledger.sequence
    candidate = RiskLedger.from_snapshot(ledger.snapshot())
    changed = candidate.apply(event)
    if store is not None:
        if not isinstance(ledger_id, str) or not ledger_id.strip():
            raise ValueError("ledger_id is required when a store is supplied")
        expected_sequence = (
            event["sequence"] - 1 if not changed else previous_sequence
        )
        store.append(
            ledger_id, expected_sequence=expected_sequence,
            event=event, ledger=candidate,
        )
    ledger.__dict__.update(candidate.__dict__)
    return changed


def roll_account_period(
    ledger: RiskLedger, timestamp: datetime | str, *, store=None,
    ledger_id: str | None = None,
) -> bool:
    """Persist the New-York-17:00 daily/weekly rollover before new-period trades."""
    event = build_period_roll_event(ledger, timestamp)
    if event is None:
        return False
    apply_ledger_event(ledger, event, store=store, ledger_id=ledger_id)
    return True
