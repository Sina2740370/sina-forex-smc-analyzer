from datetime import datetime, timezone, timedelta

import pytest

from engine.mas.identity import stable_execution_id, stable_position_id


def _position(**overrides):
    values = {
        "account_namespace": "paper-account-1",
        "strategy_namespace": "mas-v1",
        "symbol": "EURUSD",
        "side": "BUY",
        "decision_timestamp": "2026-11-18T10:15:00Z",
        "candidate_identity": "signal-42:ob-7",
    }
    values.update(overrides)
    return stable_position_id(**values)


def test_position_id_is_deterministic():
    assert _position() == _position()


def test_position_id_normalizes_symbol_side_and_timezone():
    utc = _position(decision_timestamp="2026-11-18T10:15:00+00:00")
    offset = _position(
        symbol="eurusd", side="buy",
        decision_timestamp="2026-11-18T13:45:00+03:30",
    )
    assert utc == offset


def test_position_id_changes_for_distinct_candidates():
    assert _position(candidate_identity="candidate-a") != _position(
        candidate_identity="candidate-b"
    )


@pytest.mark.parametrize("field", [
    "account_namespace", "strategy_namespace", "symbol", "side",
    "candidate_identity",
])
def test_position_id_rejects_missing_identity_fields(field):
    with pytest.raises(ValueError):
        _position(**{field: "  "})


@pytest.mark.parametrize("timestamp", [
    "not-a-timestamp",
    "2026-11-18T10:15:00",
    datetime(2026, 11, 18, 10, 15),
])
def test_position_id_rejects_invalid_or_naive_timestamp(timestamp):
    with pytest.raises(ValueError):
        _position(decision_timestamp=timestamp)


def test_position_id_rejects_unknown_side():
    with pytest.raises(ValueError):
        _position(side="LONG")


def test_execution_id_is_deterministic_and_scoped_to_position():
    first = stable_execution_id(
        position_id="position-1", execution_identity="close-fill-1"
    )
    assert first == stable_execution_id(
        position_id="position-1", execution_identity="close-fill-1"
    )
    assert first != stable_execution_id(
        position_id="position-2", execution_identity="close-fill-1"
    )


def test_execution_id_rejects_missing_identity():
    with pytest.raises(ValueError):
        stable_execution_id(position_id="position-1", execution_identity=" ")
