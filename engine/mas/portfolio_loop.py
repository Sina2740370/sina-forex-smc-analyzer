"""Deterministic chronological ordering and atomic dispatch for portfolio events.

The loop is deliberately strategy- and data-provider-agnostic. It accepts
already-priced ledger events, orders them independently of symbol iteration,
validates the complete candidate history, then delegates one atomic commit to
the durable Paper adapter. Backtest can use the same ordering function without
persistence to guarantee identical event order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .ledger import RiskLedger


_EVENT_PRIORITY = {
    "PeriodRolled": 0,
    "PositionPartiallyClosed": 10,
    "PositionClosed": 10,
    "PositionOpened": 20,
    "MarkToMarketUpdated": 90,
}


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("event timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("event timestamp must be valid ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _sort_key(event: dict) -> tuple:
    occurred_at = _utc(event.get("timestamp"))
    kind = event.get("type")
    if kind not in _EVENT_PRIORITY:
        raise ValueError(f"unsupported portfolio event type: {kind!r}")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id is required")
    symbol = event.get("symbol", "")
    position_id = event.get("position_id", "")
    if kind in ("PositionOpened", "PositionPartiallyClosed", "PositionClosed"):
        if not isinstance(position_id, str) or not position_id.strip():
            raise ValueError("position_id is required for position events")
        if kind == "PositionOpened" and (
            not isinstance(symbol, str) or not symbol.strip()
        ):
            raise ValueError("symbol is required for PositionOpened")
    return (
        occurred_at,
        _EVENT_PRIORITY[kind],
        str(symbol).upper(),
        str(position_id),
        event_id,
    )


def order_portfolio_events(
    ledger: RiskLedger, events: Iterable[dict],
) -> list[dict]:
    """Return a validated, contiguous ledger batch in deterministic time order.

    Same-timestamp priority is: period rollover, position settlements, opens,
    then one aggregate MTM. Within a priority, symbol/position/event IDs provide
    stable tie-breaking. All event timestamps are canonicalized to UTC.
    Exact retries reuse their original persisted event/sequence. Mixed
    duplicate/new batches are rejected by contract.
    """
    if not isinstance(ledger, RiskLedger):
        raise ValueError("ledger must be a RiskLedger")
    if isinstance(events, (str, bytes, dict)):
        raise ValueError("events must be an iterable of ledger event mappings")
    supplied = list(events)
    if not supplied:
        raise ValueError("events must be non-empty")
    if any(not isinstance(event, dict) for event in supplied):
        raise ValueError("every portfolio event must be a mapping")

    keys = [_sort_key(event) for event in supplied]
    ids = [event["event_id"] for event in supplied]
    if len(set(ids)) != len(ids):
        raise ValueError("portfolio batch contains duplicate event IDs")

    history = {
        event["event_id"]: event
        for event in ledger.snapshot()["applied_events"]
    }
    ordered_pairs = sorted(zip(supplied, keys), key=lambda pair: pair[1])
    known = []
    new = []
    for event, _ in ordered_pairs:
        canonical = dict(event)
        canonical["timestamp"] = _utc(event["timestamp"]).isoformat()
        prior = history.get(event["event_id"])
        if prior is not None:
            comparable = dict(canonical)
            comparable["sequence"] = prior["sequence"]
            if comparable != prior:
                raise ValueError("event_id was reused with different portfolio facts")
            known.append(prior)
        else:
            new.append(canonical)

    if known and new:
        raise ValueError("portfolio batch cannot mix exact retries and new events")
    if known:
        return known

    candidate = RiskLedger.from_snapshot(ledger.snapshot())
    result = []
    for event in new:
        event["sequence"] = candidate.sequence + 1
        candidate.apply(event)
        result.append(event)
    return result


def commit_portfolio_events(adapter: Any, events: Iterable[dict]) -> bool:
    """Order, pre-validate and atomically persist one portfolio event batch.

    The adapter must expose a RiskLedger as .ledger and a commit_events(list)
    method. No market data, strategy, broker, or clock is accessed here.
    """
    ledger = getattr(adapter, "ledger", None)
    commit = getattr(adapter, "commit_events", None)
    if not isinstance(ledger, RiskLedger) or not callable(commit):
        raise ValueError("adapter must expose ledger and commit_events")
    ordered = order_portfolio_events(ledger, events)
    return commit(ordered)
