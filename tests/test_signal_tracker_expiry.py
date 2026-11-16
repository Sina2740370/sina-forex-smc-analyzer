import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("FRED_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from backend import signal_tracker as tracker


def _expired_signal():
    return {
        "id": 123,
        "symbol": "EURUSD=X",
        "direction": "BUY",
        "entry": 1.1000,
        "created_at": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
    }


def test_expired_signal_stays_open_when_no_mark_to_market_price(monkeypatch):
    closed = []
    monkeypatch.setattr(tracker, "fetch_ohlcv", lambda *a, **k: [])
    monkeypatch.setattr(tracker, "close_signal", lambda *a, **k: closed.append(a))

    tracker._check_expired_signals([_expired_signal()])

    assert closed == []


def test_expired_signal_stays_open_when_latest_price_is_invalid(monkeypatch):
    closed = []
    monkeypatch.setattr(tracker, "fetch_ohlcv", lambda *a, **k: [
        {"time": datetime.now(timezone.utc).isoformat(), "close": float("nan")}
    ])
    monkeypatch.setattr(tracker, "close_signal", lambda *a, **k: closed.append(a))

    tracker._check_expired_signals([_expired_signal()])

    assert closed == []

def test_expired_signal_stays_open_when_mark_price_is_stale(monkeypatch):
    closed = []
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    monkeypatch.setattr(tracker, "fetch_ohlcv", lambda *a, **k: [
        {"time": stale_time, "close": 1.1050}
    ])
    monkeypatch.setattr(tracker, "close_signal", lambda *a, **k: closed.append(a))

    tracker._check_expired_signals([_expired_signal()])

    assert closed == []


def test_expired_signal_closes_when_mark_price_is_fresh(monkeypatch):
    closed = []
    fresh_time = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    monkeypatch.setattr(tracker, "fetch_ohlcv", lambda *a, **k: [
        {"time": fresh_time, "close": 1.1050}
    ])
    monkeypatch.setattr(tracker, "close_signal", lambda *a, **k: closed.append(a))

    tracker._check_expired_signals([_expired_signal()])

    assert len(closed) == 1
    assert closed[0][0] == 123
