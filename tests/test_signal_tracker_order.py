import os
os.environ.setdefault("FRED_API_KEY", "x"); os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x"); os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
from datetime import datetime, timedelta
from backend import signal_tracker as st

def _sig(age_h):
    created = (datetime.utcnow() - timedelta(hours=age_h)).isoformat()
    return {"id": 1, "symbol": "EURUSD=X", "direction": "BUY", "entry": 1.1000,
            "sl": 1.0950, "tp": 1.1100, "created_at": created}

def _run(monkeypatch, sig, low):
    calls = []
    t = datetime.utcnow() - timedelta(hours=70)
    candles = [{"time": t.isoformat(), "open": 1.1, "high": 1.1, "low": low, "close": 1.1}]
    monkeypatch.setattr(st, "get_open_signals", lambda: [sig])
    monkeypatch.setattr(st, "fetch_ohlcv", lambda *a, **k: candles)
    monkeypatch.setattr(st, "close_signal", lambda *a: calls.append(a))
    st.check_open_signals()
    return calls

def test_old_signal_that_hit_sl_is_closed_once_as_loss(monkeypatch):
    calls = _run(monkeypatch, _sig(80), low=1.0900)
    assert len(calls) == 1 and calls[0][3] == "LOSS", calls

def test_old_signal_without_hit_expires_once(monkeypatch):
    calls = _run(monkeypatch, _sig(80), low=1.0990)
    assert len(calls) == 1 and calls[0][3] == "EXPIRED", calls
