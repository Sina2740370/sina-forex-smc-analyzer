"""Stable identities for replayable Paper/Backtest ledger events.

IDs are derived only from immutable decision/execution facts. They never depend
on random UUIDs, process-local counters, or the wall clock at retry time.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def _aware_utc(value: datetime | str) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("decision_timestamp must be valid ISO-8601") from None
    if not isinstance(value, datetime):
        raise ValueError("decision_timestamp must be a datetime or ISO-8601 string")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision_timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _digest(payload: dict) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_position_id(
    *, account_namespace: str, strategy_namespace: str, symbol: str,
    side: str, decision_timestamp: datetime | str, candidate_identity: str,
) -> str:
    """Return a deterministic position ID for one immutable signal candidate."""
    account = _required_text(account_namespace, "account_namespace")
    strategy = _required_text(strategy_namespace, "strategy_namespace")
    instrument = _required_text(symbol, "symbol").upper()
    direction = _required_text(side, "side").upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    candidate = _required_text(candidate_identity, "candidate_identity")
    payload = {
        "schema": "mas-position-v1",
        "account_namespace": account,
        "strategy_namespace": strategy,
        "symbol": instrument,
        "side": direction,
        "decision_timestamp": _aware_utc(decision_timestamp),
        "candidate_identity": candidate,
    }
    return f"mas:position:v1:{_digest(payload)}"


def stable_execution_id(*, position_id: str, execution_identity: str) -> str:
    """Return a deterministic ID for one immutable fill/settlement identity."""
    position = _required_text(position_id, "position_id")
    execution = _required_text(execution_identity, "execution_identity")
    payload = {
        "schema": "mas-execution-v1",
        "position_id": position,
        "execution_identity": execution,
    }
    return f"mas:execution:v1:{_digest(payload)}"
