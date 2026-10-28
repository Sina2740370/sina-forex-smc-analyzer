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


def test_market_regime_and_refine_entry():
    from engine.core import smc
    df = pd.DataFrame({
        "open": [1.0 + 0.001 * i for i in range(30)],
        "high": [1.002 + 0.001 * i for i in range(30)],
        "low": [0.998 + 0.001 * i for i in range(30)],
        "close": [1.001 + 0.001 * i for i in range(30)]
    })
    regime = smc.market_regime(df)
    assert regime in ("TRENDING_HIGH_VOL", "TRENDING_LOW_VOL", "RANGING_HIGH_VOL", "RANGING_LOW_VOL")

    entry, sl, refined = smc.refine_ltf_entry(df, 1, 1.0500, 1.0400)
    assert isinstance(refined, bool)


def test_rollover_period_and_portfolio_currency_limit():
    rollover_dt = datetime(2026, 3, 10, 22, 15, tzinfo=timezone.utc)
    assert sg.is_rollover_period(rollover_dt) is True
    assert sg.evaluate("EURUSD", "BUY", [], [], now=rollover_dt) == "rollover_spread_guard"

    normal_dt = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
    open_sigs = [
        {"symbol": "EURUSD", "direction": "BUY"},
        {"symbol": "GBPUSD", "direction": "BUY"},
    ]
    # Adding a 3rd USD position should trigger portfolio_currency_limit
    res = sg.evaluate("AUDUSD", "BUY", open_sigs, [], now=normal_dt)
    assert res == "portfolio_currency_limit"


def test_monte_carlo_simulation():
    from engine.backtest.report import monte_carlo_simulation
    trades = [{"r": 2.0}, {"r": -1.0}, {"r": 1.5}, {"r": -1.0}, {"r": 3.0}]
    mc = monte_carlo_simulation(trades, n_simulations=100, seed=123)
    assert mc["n_simulations"] == 100
    assert "max_drawdown_mean" in mc
    assert "total_r_mean" in mc


def test_fractional_kelly_and_position_milestones():
    from engine.tools import risk
    k_risk = risk.fractional_kelly_risk_pct(win_rate=0.65, win_loss_ratio=2.0, fraction=0.25)
    assert 0.25 <= k_risk <= 2.0

    ms = risk.calculate_position_milestones("BUY", 1.1000, 1.0950, 1.1100, be_r_trigger=1.0, partial_r_trigger=1.5)
    assert ms["be_trigger_price"] == 1.1050
    assert ms["partial_tp_price"] == 1.1075


def test_classify_setup_grade():
    grade_a_plus = ae.classify_setup_grade(0.80, 0.75, ["r1", "r2", "r3", "r4"])
    assert grade_a_plus == "A+"

    grade_b = ae.classify_setup_grade(0.30, 0.35, ["r1"])
    assert grade_b == "B"
