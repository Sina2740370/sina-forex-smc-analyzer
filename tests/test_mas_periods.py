from datetime import datetime, timezone

import pytest

from engine.mas.periods import trading_periods


def test_daily_period_rolls_at_1700_new_york_in_winter():
    before = trading_periods(datetime(2026, 1, 15, 21, 59, tzinfo=timezone.utc))
    at = trading_periods(datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc))
    assert before.daily_period_id == "2026-01-15"
    assert at.daily_period_id == "2026-01-16"


def test_daily_period_rolls_at_1700_new_york_in_summer():
    before = trading_periods(datetime(2026, 7, 15, 20, 59, tzinfo=timezone.utc))
    at = trading_periods(datetime(2026, 7, 15, 21, 0, tzinfo=timezone.utc))
    assert before.daily_period_id == "2026-07-15"
    assert at.daily_period_id == "2026-07-16"


def test_sunday_1700_new_york_starts_monday_and_new_trading_week():
    before_open = trading_periods("2026-11-15T21:59:00Z")
    at_open = trading_periods("2026-11-15T22:00:00Z")
    assert before_open.daily_period_id == "2026-11-13"
    assert before_open.weekly_period_id == "2026-W46"
    assert at_open.daily_period_id == "2026-11-16"
    assert at_open.weekly_period_id == "2026-W47"


def test_weekly_period_uses_monday_session_label():
    monday_session = trading_periods("2026-11-16T22:00:00Z")
    friday_session = trading_periods("2026-11-20T21:59:00Z")
    assert monday_session.weekly_period_id == "2026-W47"
    assert friday_session.weekly_period_id == "2026-W47"


def test_equivalent_aware_instants_have_identical_period_ids():
    utc_value = datetime(2026, 6, 1, 21, 30, tzinfo=timezone.utc)
    offset_value = datetime.fromisoformat("2026-06-01T17:30:00-04:00")
    assert trading_periods(utc_value) == trading_periods(offset_value)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 1, 12, 0),
        "2026-01-01T12:00:00",
    ],
)
def test_naive_timestamps_are_rejected(value):
    with pytest.raises(ValueError, match="timezone"):
        trading_periods(value)


def test_invalid_timestamp_is_rejected():
    with pytest.raises(ValueError, match="ISO-8601"):
        trading_periods("not-a-timestamp")


def test_period_roll_event_is_deterministic_and_only_emitted_on_change():
    from engine.mas.ledger import RiskLedger
    from engine.mas.periods import build_period_roll_event

    ledger = RiskLedger("10000")
    first = build_period_roll_event(ledger, "2026-11-15T22:00:00Z")
    assert first is not None
    assert first["event_id"] == "period-roll:2026-11-16:2026-W47"
    assert first["sequence"] == 1
    assert first["daily_period_id"] == "2026-11-16"
    assert first["weekly_period_id"] == "2026-W47"

    ledger.apply(first)
    assert build_period_roll_event(ledger, "2026-11-15T22:00:00Z") is None


def test_period_roll_event_updates_daily_without_resetting_weekly():
    from engine.mas.ledger import RiskLedger
    from engine.mas.periods import build_period_roll_event

    ledger = RiskLedger(
        "10000", daily_period_id="2026-11-16", weekly_period_id="2026-W47"
    )
    event = build_period_roll_event(ledger, "2026-11-16T22:00:00Z")
    assert event is not None
    assert event["type"] == "PeriodRolled"
    assert event["daily_period_id"] == "2026-11-17"
    assert "weekly_period_id" not in event
    ledger.apply(event)
    assert ledger.daily_period_id == "2026-11-17"
    assert ledger.weekly_period_id == "2026-W47"
