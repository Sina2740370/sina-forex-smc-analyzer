"""Regression tests for the live-to-shared-engine adapter."""
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from backend import analysis_engine


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
