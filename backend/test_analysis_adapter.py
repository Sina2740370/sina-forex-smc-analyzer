"""Regression tests for the live-to-shared-engine adapter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd

from backend import analysis_engine, scheduler


def _records():
    times = pd.date_range("2026-10-15T00:00:00Z", periods=3, freq="15min")
    frame = {
        "time": times,
        "open": [1.0, 1.01, 1.02],
        "high": [1.02, 1.03, 1.04],
        "low": [0.99, 1.00, 1.01],
        "close": [1.01, 1.02, 1.03],
        "volume": [1, 1, 1],
    }
    return {tf: pd.DataFrame(frame).to_dict("records")
            for tf in ("1wk", "1d", "4h", "1h", "15m")}


def _closed_frames():
    times = pd.date_range("2026-10-15T00:00:00Z", periods=20, freq="15min")
    close = [1.0 + i * 0.001 for i in range(20)]
    return {
        "15m": pd.DataFrame({
            "time": times, "open": close, "high": [x + 0.002 for x in close],
            "low": [x - 0.002 for x in close], "close": close, "volume": 1.0,
        }),
        "1wk": pd.DataFrame(), "1d": pd.DataFrame(),
        "4h": pd.DataFrame(), "1h": pd.DataFrame(),
    }


def test_live_adapter_uses_shared_engine_decision_and_maps_buy():
    decision = SimpleNamespace(
        direction="BUY", score=0.82, entry=1.019, sl=1.010, tp=1.037,
        bias="weekly+daily BULLISH", reasons=["DISCOUNT", "sweep", "CHOCH"],
        blocked=None,
    )
    with (
        patch.object(analysis_engine, "TopDownView") as view_cls,
        patch.object(analysis_engine, "analyze_smc_engine", return_value=decision) as analyze,
    ):
        view_cls.return_value.at.return_value = _closed_frames()
        result = analysis_engine.analyze_technical_topdown(_records())

    analyze.assert_called_once()
    assert result["direction"] == "BUY"
    assert result["score"] == 0.82
    assert result["price"] == 1.019
    assert result["sl"] == 1.010 and result["tp"] == 1.037
    assert result["zone"] == "DISCOUNT"
    assert result["blocked"] is False


def test_live_adapter_blocks_when_shared_engine_has_no_direction():
    decision = SimpleNamespace(
        direction=None, score=0.72, entry=None, sl=None, tp=None,
        bias="CONFLICT weekly BULLISH / daily BEARISH",
        reasons=["bias conflict"], blocked="CONFLICT",
    )
    with (
        patch.object(analysis_engine, "TopDownView") as view_cls,
        patch.object(analysis_engine, "analyze_smc_engine", return_value=decision),
    ):
        view_cls.return_value.at.return_value = _closed_frames()
        result = analysis_engine.analyze_technical_topdown(_records())

    assert result["direction"] is None
    assert result["blocked"] is True
    assert result["sl"] is None and result["tp"] is None
    assert any("CONFLICT" in reason for reason in result["reasons"])

def test_gold_futures_proxy_signal_is_blocked_by_default():
    original = {
        "direction": "BUY", "score": 0.9, "reasons": ["SMC confluence"],
        "summary": "signal",
    }
    result = scheduler._guard_gold_futures_proxy(original, "GC=F")
    assert result["direction"] is None
    assert result["spot_data"] is False
    assert "not spot XAUUSD" in result["data_source"]
    assert any("سیگنال مسدود" in reason for reason in result["reasons"])
    assert original["direction"] == "BUY"  # helper must not mutate caller's result


def test_gold_futures_proxy_requires_explicit_opt_in_to_emit_signal():
    original = {"direction": "SELL", "score": -0.8, "reasons": []}
    result = scheduler._guard_gold_futures_proxy(original, "GC=F", allow_proxy=True)
    assert result["direction"] == "SELL"
    assert result["spot_data"] is False


def test_gold_proxy_guard_does_not_change_fx_signals():
    original = {"direction": "BUY", "score": 0.7}
    assert scheduler._guard_gold_futures_proxy(original, "GBPJPY") is original


def test_full_analysis_keeps_shared_engine_direction_when_context_score_disagrees():
    tech = {
        "blocked": False, "direction": "BUY", "score": 0.62, "price": 1.1,
        "sl": 1.09, "tp": 1.12, "trend": "weekly+daily BULLISH",
        "zone": "DISCOUNT", "atr": 0.01, "reasons": ["core setup confirmed"],
    }
    with (
        patch.object(analysis_engine, "analyze_technical_topdown", return_value=tech),
        patch(
            "backend.fundamental_analyzer.calculate_fundamental_score",
            new=AsyncMock(return_value={"score": -1.0, "reasons": ["opposes buy"]}),
        ),
        patch(
            "backend.market_memory.calculate_memory_score",
            return_value={"score": -1.0, "reasons": ["negative history"]},
        ),
    ):
        result = asyncio.run(
            analysis_engine.full_analysis(
                {}, "EURUSD=X", news_safe=True, news_sentiment=-1.0
            )
        )

    assert result["direction"] == "BUY"
    assert result["score"] == 0.62
    assert result["context_score"] < 0
    assert result["decision_source"] == "shared_smc_engine"
    assert result["suggested_sl"] == 1.09
    assert result["suggested_tp"] == 1.12


def test_full_analysis_still_vetoes_shared_engine_signal_when_news_is_unsafe():
    tech = {
        "blocked": False, "direction": "SELL", "score": -0.75, "price": 1.1,
        "sl": 1.11, "tp": 1.08, "trend": "weekly+daily BEARISH",
        "zone": "PREMIUM", "atr": 0.01, "reasons": ["core setup confirmed"],
    }
    with (
        patch.object(analysis_engine, "analyze_technical_topdown", return_value=tech),
        patch(
            "backend.fundamental_analyzer.calculate_fundamental_score",
            new=AsyncMock(return_value={"score": 0.0, "reasons": []}),
        ),
        patch(
            "backend.market_memory.calculate_memory_score",
            return_value={"score": 0.0, "reasons": []},
        ),
    ):
        result = asyncio.run(
            analysis_engine.full_analysis({}, "EURUSD=X", news_safe=False)
        )

    assert result["direction"] is None
    assert result["news_safe"] is False
    assert result["decision_source"] == "shared_smc_engine"

