"""Deterministic FX account-period identifiers.

The account day follows the conventional New York 17:00 FX rollover.
The account week begins with the Sunday 17:00 New York session open.
Timestamps must be timezone-aware; no host-local or UTC fallback is allowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


_NEW_YORK = ZoneInfo("America/New_York")
_ROLLOVER = timedelta(hours=17)


@dataclass(frozen=True)
class TradingPeriods:
    """Stable IDs for the FX trading day and trading week containing a timestamp."""

    daily_period_id: str
    weekly_period_id: str


def trading_periods(timestamp: datetime | str) -> TradingPeriods:
    """Return New-York-17:00 FX period IDs for an aware datetime or ISO-8601 string.

    The daily ID is the calendar date on which the 17:00-to-17:00 NY session
    closes. Weekend timestamps outside an open FX session map to Friday.
    Sunday 17:00 NY starts Monday's trading day and the new trading week.
    IDs are calendar labels, not UTC dates, and therefore remain stable across
    DST transitions without hard-coded UTC offsets.
    """
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(
                timestamp.strip().replace("Z", "+00:00")
            )
        except ValueError:
            raise ValueError("timestamp must be valid ISO-8601") from None
    if not isinstance(timestamp, datetime):
        raise ValueError("timestamp must be a datetime or ISO-8601 string")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")

    new_york = timestamp.astimezone(_NEW_YORK)
    session_date = (new_york - _ROLLOVER).date() + timedelta(days=1)

    # Saturday and the pre-open Sunday are closed-market time: retain Friday's
    # last trading-day label. Sunday 17:00 rolls forward to Monday.
    if session_date.weekday() == 5:
        session_date -= timedelta(days=1)
    elif session_date.weekday() == 6:
        session_date -= timedelta(days=2)

    week_monday = session_date - timedelta(days=session_date.weekday())
    iso_year, iso_week, _ = week_monday.isocalendar()
    return TradingPeriods(
        daily_period_id=session_date.isoformat(),
        weekly_period_id=f"{iso_year}-W{iso_week:02d}",
    )


def build_period_roll_event(ledger, timestamp: datetime | str) -> dict | None:
    """Build the next deterministic PeriodRolled event, or None if unchanged.

    The caller must apply and persist the returned event before processing
    trades in the new period. The ledger remains the source of current period
    state; this helper does not mutate it.
    """
    periods = trading_periods(timestamp)
    daily_changed = periods.daily_period_id != ledger.daily_period_id
    weekly_changed = periods.weekly_period_id != ledger.weekly_period_id
    if not daily_changed and not weekly_changed:
        return None

    if isinstance(timestamp, str):
        occurred_at = datetime.fromisoformat(
            timestamp.strip().replace("Z", "+00:00")
        )
    else:
        occurred_at = timestamp
    occurred_at = occurred_at.astimezone(ZoneInfo("UTC")).isoformat()

    event = {
        "event_id": (
            f"period-roll:{periods.daily_period_id}:{periods.weekly_period_id}"
        ),
        "sequence": ledger.sequence + 1,
        "timestamp": occurred_at,
        "type": "PeriodRolled",
    }
    if daily_changed:
        event["daily_period_id"] = periods.daily_period_id
    if weekly_changed:
        event["weekly_period_id"] = periods.weekly_period_id
    return event
