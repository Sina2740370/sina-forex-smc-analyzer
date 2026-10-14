import os
from datetime import datetime, timedelta, timezone

import pytest

# Backend config intentionally requires deployment secrets; dummy values are test-only.
os.environ.setdefault("FRED_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("DB_PATH", ":memory:")

from backend import signal_tracker


def test_expired_signal_is_marked_to_market_with_spread(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    latest_start = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    created = latest_start - timedelta(hours=96)
    closed = []

    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )

    signal = {
        "id": 77, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [{
        "time": latest_start.isoformat(), "open": 1.1000,
        "high": 1.1020, "low": 1.0990, "close": 1.1010, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    signal_id, exit_price, pnl_pips, status = closed[0]
    assert signal_id == 77
    assert status == "EXPIRED"
    # BUY exits on bid and entered on ask; both sides of spread count.
    assert exit_price == pytest.approx(1.10096)
    assert pnl_pips == pytest.approx(9.2)


def test_expired_signal_ignores_tp_hit_after_expiry(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    latest_start = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    # The second candle closes exactly at the 72-hour expiry boundary.
    created = latest_start - timedelta(hours=72) + timedelta(minutes=15)
    closed = []

    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 78, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [
        {
            "time": (latest_start - timedelta(minutes=15)).isoformat(),
            "open": 1.1000, "high": 1.1020, "low": 1.0990,
            "close": 1.1010, "volume": 1,
        },
        {
            "time": latest_start.isoformat(),
            "open": 1.1010, "high": 1.1250, "low": 1.1000,
            "close": 1.1230, "volume": 1,
        },
    ]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    assert closed[0][2] == "EXPIRED"
    assert closed[0][0] == pytest.approx(1.12296)



def test_malformed_candle_timestamp_is_skipped_without_aborting_tracking(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    valid_start = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    created = valid_start - timedelta(minutes=30)
    closed = []

    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 79, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [
        {"time": "not-a-timestamp", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
        {
            "time": valid_start.isoformat(), "open": 1.1, "high": 1.1210,
            "low": 1.0990, "close": 1.1205, "volume": 1,
        },
    ]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    assert closed[0][0] == 79
    assert closed[0][3] == "WIN"
