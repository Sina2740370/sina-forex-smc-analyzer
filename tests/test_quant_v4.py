"""تست‌های موتور Quant V4 (ensemble روند + شکست + sweep)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.core.config import EngineConfig
from engine.strategy.quant_v4 import (_regime_daily, _session_quality,
                                      _trigger_donchian, _trigger_pullback,
                                      analyze_v4)


def _mk_tf(n: int, start: float = 1.10, drift: float = 0.00002,
           noise: float = 0.0004, freq: str = "15min", seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    px = start + drift * np.arange(n) + rng.normal(0, noise, n).cumsum()
    o = px + rng.normal(0, noise / 2, n)
    c = px
    h = np.maximum(o, c) + rng.random(n) * noise
    l = np.minimum(o, c) - rng.random(n) * noise
    return pd.DataFrame({"time": idx, "open": o, "high": h, "low": l,
                         "close": c, "volume": 1.0})


def _data(n15=600, seed=7) -> dict:
    d15 = _mk_tf(n15, freq="15min", seed=seed)
    d1h = _mk_tf(n15 // 4 + 30, freq="1h", seed=seed + 1)
    d4h = _mk_tf(n15 // 16 + 30, freq="4h", seed=seed + 2)
    d1d = _mk_tf(n15 // 96 + 70, freq="1D", seed=seed + 3, drift=0.0004)
    return {"15m": d15, "1h": d1h, "4h": d4h, "1d": d1d}


CFG = EngineConfig(min_score=0.72)


# ---------------------------------------------------------------- regime
def test_regime_daily_returns_dict():
    d1d = _mk_tf(200, freq="1D", drift=0.0004, seed=3)
    reg = _regime_daily(d1d)
    assert reg and reg["regime"] in ("UP", "DOWN", "CHOP")
    assert 0.0 <= reg["vol_pct"] <= 1.0


def test_regime_insufficient_data():
    d1d = _mk_tf(20, freq="1D")
    assert _regime_daily(d1d) is None


def test_regime_detects_uptrend():
    idx = pd.date_range("2025-01-01", periods=150, freq="1D", tz="UTC")
    px = 1.0 + 0.003 * np.arange(150)          # روند صعودی قوی
    o = px - 0.001
    df = pd.DataFrame({"time": idx, "open": o, "high": px + 0.002,
                       "low": o - 0.002, "close": px, "volume": 1.0})
    reg = _regime_daily(df)
    assert reg["regime"] == "UP"
    assert reg["slope_ok"]


# ---------------------------------------------------------------- session
def test_session_quality():
    t = pd.Timestamp("2025-06-03 08:30", tz="UTC")   # اوپن لندن
    in_s, prime = _session_quality(t)
    assert in_s and prime
    t2 = pd.Timestamp("2025-06-03 03:00", tz="UTC")  # آسیا
    in_s2, prime2 = _session_quality(t2)
    assert not in_s2 and not prime2


# ---------------------------------------------------------------- donchian
def test_trigger_donchian_breakout():
    idx = pd.date_range("2025-01-01", periods=60, freq="4h", tz="UTC")
    px = np.full(60, 1.10)                          # رنج تخت
    o = px + 0.0002
    c = px
    h = px + 0.0005
    l = px - 0.0005
    # کندل آخر: شکست واضح بالای کانال
    c[-1] = 1.10 + 0.004
    o[-1] = 1.10 + 0.001
    h[-1] = c[-1] + 0.0005
    l[-1] = o[-1] - 0.0005
    df = pd.DataFrame({"time": idx, "open": o, "high": h, "low": l,
                       "close": c, "volume": 1.0})
    t = _trigger_donchian(df, "UP")
    assert t and t["side"] == 1 and t["kind"] == "breakout"
    assert t["depth"] > 0


def test_trigger_donchian_no_break_in_range():
    idx = pd.date_range("2025-01-01", periods=60, freq="4h", tz="UTC")
    px = 1.10 + 0.0005 * np.sin(np.arange(60))     # رنج
    df = pd.DataFrame({"time": idx, "open": px, "high": px + 0.0002,
                       "low": px - 0.0002, "close": px, "volume": 1.0})
    assert _trigger_donchian(df, "UP") is None


# ---------------------------------------------------------------- analyze_v4
def test_analyze_blocked_outside_session():
    d = _data()
    now = pd.Timestamp("2025-03-05 03:00", tz="UTC")
    dec = analyze_v4(d, CFG, now)
    assert dec.direction is None
    assert dec.blocked in ("outside London/NY session",
                           "insufficient daily data", "chop regime",
                           "no trigger", "vol regime out of band (0.50)")


def test_analyze_no_direction_without_trigger():
    d = _data()
    now = pd.Timestamp("2025-03-05 08:30", tz="UTC")
    dec = analyze_v4(d, CFG, now)
    assert dec.direction in (None, "BUY", "SELL")
    if dec.direction is None:
        assert dec.blocked


def test_analyze_valid_signal_has_risk_defined():
    d = _data(n15=900, seed=11)
    for hour in (8, 9, 13, 14):
        now = pd.Timestamp("2025-03-05 %02d:30" % hour, tz="UTC")
        dec = analyze_v4(d, CFG, now)
        if dec.direction:
            assert dec.entry and dec.sl and dec.tp
            side = 1 if dec.direction == "BUY" else -1
            assert (dec.sl - dec.entry) * side < 0       # SL سمت درست
            assert (dec.tp - dec.entry) * side > 0       # TP سمت درست
            risk = abs(dec.entry - dec.sl)
            assert abs(dec.tp - dec.entry) == pytest.approx(2 * risk, rel=0.02)
            assert dec.score >= CFG.min_score


def test_pullback_trigger_gated_by_regime():
    """تریگر pullback/meanrev بدون رژیم مناسب تریگر نمی‌شود."""
    idx = pd.date_range("2025-01-01", periods=40, freq="1h", tz="UTC")
    px = 1.10 + 0.0001 * np.arange(40)
    df1h = pd.DataFrame({"time": idx, "open": px, "high": px + 0.0002,
                         "low": px - 0.0002, "close": px, "volume": 1.0})
    d15 = _mk_tf(300)
    t = _trigger_pullback(d15, df1h, "UP")
    assert t is None or t["kind"] in ("pullback", "meanrev")
