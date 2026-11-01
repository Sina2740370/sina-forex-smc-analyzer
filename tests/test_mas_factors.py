"""تست فاکتورهای تجربی cfg-gated: w1_bias، sl_mode=atr، session_filter."""
import pandas as pd
import pytest

from engine.mas.aggregator import _w1_bias
from engine.data.providers import build_topdown

from tests.test_engine import make_15m


def test_w1_bias_returns_pm1():
    m = make_15m(420, seed=7)
    top = build_topdown(m)
    assert _w1_bias(top) in (-1, 0, 1)


def test_sl_atr_makes_rr2():
    """با sl_mode=atr، کاندید باید SL تنگ‌تر و rr=2.0 بگیرد (وقتی به G4 برسد)."""
    from engine.mas.aggregator import analyze_mas
    m = make_15m(420, seed=7)
    now = m["time"].iloc[-1]
    sig = analyze_mas(m, "EURUSD", now, cfg={"sl_mode": "atr", "atr_k": 1.5})
    if sig.signal in ("BUY", "SELL") and sig.cand:
        assert sig.cand["rr"] == pytest.approx(2.0)


def test_session_filter_blocks_other_sessions():
    from engine.mas.backtest import backtest_mas
    m = make_15m(420, seed=7)
    r = backtest_mas(m, "EURUSD", step_hours=8, cfg={"session_filter": ["NewYork"]})
    for t in r.trades:
        assert t.session == "NewYork"


def test_default_cfg_is_noop():
    """cfg خالی نباید هیچ رفتاری تغییر دهد (بازتولید baseline)."""
    from engine.mas.weights import get_weights, get_threshold
    assert get_weights({"regime": "range"}, {}) == get_weights({"regime": "range"})
    assert get_threshold({"regime": "range"}, {}) == get_threshold({"regime": "range"})
