import asyncio
import os

os.environ.setdefault("FRED_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from backend.analysis_engine import _directional_composite


def test_directional_composite_preserves_buy_and_sell_symmetry():
    buy = _directional_composite(
        tech_score=1.0, tech_direction="BUY",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )
    sell = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )

    buy_score, buy_threshold, buy_direction = buy
    sell_score, sell_threshold, sell_direction = sell
    assert buy_direction == "BUY"
    assert sell_direction == "SELL"
    assert buy_score > 0
    assert sell_score < 0
    assert abs(buy_score) == abs(sell_score)
    assert buy_threshold == sell_threshold


def test_configured_smc_minimum_is_preserved_after_weight_normalization():
    final_score, threshold, direction = _directional_composite(
        tech_score=0.72, tech_direction="SELL",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )

    assert abs(final_score) == threshold
    assert direction == "SELL"


def test_opposing_fundamental_evidence_can_veto_but_not_reverse_structure():
    result = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=1.0, news_score=0.0, min_score=0.72,
    )

    final_score, _threshold, direction = result
    assert direction is None
    assert final_score < 0


def test_memory_is_not_a_directional_input():
    # The helper deliberately has no memory argument: historical strategy
    # performance is not evidence that the current market should rise or fall.
    result = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )
    _final_score, _threshold, direction = result
    assert direction == "SELL"


def test_invalid_direction_and_invalid_weights_fail_closed():
    invalid_direction = _directional_composite(
        tech_score=1.0, tech_direction="WAIT",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )
    invalid_weights = _directional_composite(
        tech_score=1.0, tech_direction="BUY",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
        technical_weight=0.0, fundamental_weight=0.0, news_weight=0.0,
    )

    assert invalid_direction[2] is None
    assert invalid_weights[2] is None



def test_unsafe_news_remains_a_hard_signal_veto():
    result = _directional_composite(
        tech_score=1.5, tech_direction="BUY",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
        news_safe=False,
    )

    final_score, _threshold, direction = result
    assert final_score == 0.0
    assert direction is None



def _stub_analysis_dependencies(monkeypatch, technical_direction="SELL", memory_score=0.9):
    from backend import analysis_engine as ae
    from backend import fundamental_analyzer as fa
    from backend import market_memory as mm

    monkeypatch.setattr(ae, "analyze_technical_topdown", lambda *a, **k: {
        "blocked": False,
        "score": 1.0,
        "direction": technical_direction,
        "sl": 1.09,
        "tp": 1.12,
        "price": 1.10,
        "grade": "A",
        "power": 6.0,
        "quality": 0.7,
        "trend": "weekly+daily BEARISH",
        "zone": "PREMIUM",
        "atr": 0.002,
        "reasons": ["controlled test signal"],
        "features": {},
    })

    async def fake_fundamental(_symbol):
        return {"score": 0.0, "reasons": []}

    monkeypatch.setattr(fa, "calculate_fundamental_score", fake_fundamental)
    monkeypatch.setattr(mm, "calculate_memory_score", lambda _symbol: {
        "score": memory_score, "reasons": ["historical strategy performance"],
    })
    return ae



def test_full_analysis_sell_integration_and_memory_neutrality(monkeypatch):
    ae = _stub_analysis_dependencies(monkeypatch, technical_direction="SELL", memory_score=0.9)
    result = asyncio.run(ae.full_analysis({}, "EURUSD=X"))

    assert result["direction"] == "SELL"
    assert result["score"] < 0
    assert result["memory_score"] == 0.9


def test_full_analysis_unsafe_news_blocks_otherwise_valid_signal(monkeypatch):
    ae = _stub_analysis_dependencies(monkeypatch, technical_direction="BUY", memory_score=0.0)
    result = asyncio.run(ae.full_analysis({}, "EURUSD=X", news_safe=False))

    assert result["direction"] is None
    assert result["score"] == 0.0



def test_drop_forming_uses_explicit_snapshot_time():
    import pandas as pd
    from backend.analysis_engine import _drop_forming

    df = pd.DataFrame({
        "time": pd.to_datetime([
            "2024-01-01T09:45:00Z",
            "2024-01-01T10:00:00Z",
        ]),
        "open": [1.0, 1.1],
        "high": [1.1, 1.2],
        "low": [0.9, 1.0],
        "close": [1.0, 1.1],
    })

    before_close = _drop_forming(df, 15, now=pd.Timestamp("2024-01-01T10:14:59Z"))
    at_close = _drop_forming(df, 15, now=pd.Timestamp("2024-01-01T10:15:00Z"))

    assert len(before_close) == 1
    assert len(at_close) == 2


def test_topdown_cleaning_uses_one_normalized_asof_timestamp(monkeypatch):
    import pandas as pd
    from types import SimpleNamespace
    from backend import analysis_engine as ae
    from engine.strategy import smc_topdown

    seen = []

    def capture_snapshot(frame, minutes, now=None):
        seen.append(now)
        return frame

    monkeypatch.setattr(ae, "_drop_forming", capture_snapshot)
    monkeypatch.setattr(smc_topdown, "analyze", lambda *args: SimpleNamespace(
        direction=None, blocked="test", score=0.0, grade="-", power=0.0,
        quality=0.0, entry=None, sl=None, tp=None, bias="-",
        reasons=[], features={},
    ))

    times = pd.date_range("2024-01-01T00:00:00Z", periods=80, freq="15min")
    frame = pd.DataFrame({
        "time": times,
        "open": [1.0] * 80,
        "high": [1.1] * 80,
        "low": [0.9] * 80,
        "close": [1.0] * 80,
    })
    snapshot = pd.Timestamp("2024-01-02T00:00:00")
    ae.analyze_technical_topdown({"15m": frame, "4h": frame}, symbol="EURUSD=X", now=snapshot)

    assert len(seen) == 2
    assert all(value == snapshot.tz_localize("UTC") for value in seen)



def test_invalid_candle_timestamp_fails_closed():
    import pandas as pd
    from backend.analysis_engine import _drop_forming

    df = pd.DataFrame({
        "time": [pd.NaT],
        "open": [1.0],
        "high": [1.1],
        "low": [0.9],
        "close": [1.0],
    })

    filtered = _drop_forming(df, 15, now=pd.Timestamp("2024-01-01T10:15:00Z"))
    assert filtered.empty
