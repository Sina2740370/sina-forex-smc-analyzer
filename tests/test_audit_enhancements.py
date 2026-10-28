import os
os.environ.setdefault("FRED_API_KEY", "x")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from datetime import datetime, timedelta, timezone
import pandas as pd
import pytest

from backend import signal_tracker as st
from backend import analysis_engine as ae
from engine.tools import signal_gate as sg


def test_extract_currencies_edge_cases():
    assert st.extract_currencies("EURUSD") == ("EUR", "USD")
    assert st.extract_currencies("EUR/USD") == ("EUR", "USD")
    assert st.extract_currencies("GBP-JPY") == ("GBP", "JPY")
    assert st.extract_currencies("XAUUSD") == ("XAU", "USD")
    assert st.extract_currencies("GC=F") == ("XAU", "USD")
    assert st.extract_currencies("SHORT") == ("SHORT", "USD")


def test_signal_gate_exposure_safe_length():
    exp = sg.exposure("EURUSD", "BUY")
    assert exp == {"EUR": 1, "USD": -1}

    exp_short = sg.exposure("GC", "BUY")
    assert "XAU" in exp_short or "GC" in exp_short


def test_drop_forming_timezone_safety():
    now_utc = pd.Timestamp.now(tz="UTC")
    recent_time = now_utc - pd.Timedelta(minutes=5)
    old_time = now_utc - pd.Timedelta(minutes=30)

    # DataFrame with timezone aware time
    df_tz = pd.DataFrame({
        "time": [old_time, recent_time],
        "open": [1.1, 1.2],
        "high": [1.15, 1.25],
        "low": [1.05, 1.15],
        "close": [1.1, 1.2]
    })
    res_tz = ae._drop_forming(df_tz, 15)
    assert len(res_tz) == 1

    # DataFrame with naive time
    old_naive = old_time.tz_localize(None)
    recent_naive = recent_time.tz_localize(None)
    df_naive = pd.DataFrame({
        "time": [old_naive, recent_naive],
        "open": [1.1, 1.2],
        "high": [1.15, 1.25],
        "low": [1.05, 1.15],
        "close": [1.1, 1.2]
    })
    res_naive = ae._drop_forming(df_naive, 15)
    assert len(res_naive) == 1


def test_check_expired_signals_mark_to_market(monkeypatch):
    calls = []
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(hours=80)).isoformat()
    sig = {
        "id": 42,
        "symbol": "EURUSD=X",
        "direction": "BUY",
        "entry": 1.1000,
        "sl": 1.0950,
        "tp": 1.1100,
        "created_at": old_time
    }

    # Latest close moved to 1.1050 (floating gain)
    mock_candles = [{"time": now.isoformat(), "open": 1.1040, "high": 1.1060, "low": 1.1030, "close": 1.1050}]

    monkeypatch.setattr(st, "fetch_ohlcv", lambda *a, **k: mock_candles)
    monkeypatch.setattr(st, "close_signal", lambda *a: calls.append(a))

    st._check_expired_signals([sig])

    assert len(calls) == 1
    sig_id, exit_price, pnl, status = calls[0]
    assert sig_id == 42
    assert status == "EXPIRED"
    assert exit_price > 1.1000  # Mark to market exit price
    assert pnl > 0  # Floating profit recorded on expiration
