from datetime import datetime, timezone

import pytest

from backend.news_fetcher import is_news_safe, upcoming_high_impact


def test_news_window_uses_explicit_utc_snapshot_not_wall_clock():
    news = [{
        "title": "US CPI",
        "country": "USD",
        "impact": "High",
        "date": "2024-01-01T10:00:00+00:00",
    }]
    snapshot = datetime(2024, 1, 1, 9, 45, tzinfo=timezone.utc)

    events = upcoming_high_impact(
        news, within_minutes=30, after_minutes=60, now=snapshot
    )

    assert len(events) == 1
    assert events[0]["title"] == "US CPI"


def test_news_safety_blocks_high_impact_event_at_snapshot():
    news = [{
        "title": "US CPI",
        "country": "USD",
        "impact": "High",
        "date": "2024-01-01T05:00:00-05:00",
    }]

    safe, reason = is_news_safe(
        news,
        "EURUSD=X",
        buffer_min=30,
        now="2024-01-01T09:45:00Z",
    )

    assert safe is False
    assert "US CPI" in reason


def test_invalid_explicit_news_snapshot_fails_closed():
    safe, reason = is_news_safe(
        [{"title": "US CPI", "country": "USD", "impact": "High",
          "date": "2024-01-01T10:00:00Z"}],
        "EURUSD=X",
        now="not-a-time",
    )

    assert safe is False
    assert "نامعتبر" in reason
