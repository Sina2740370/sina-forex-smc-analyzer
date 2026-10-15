import os
from datetime import datetime, timezone

import pandas as pd
import pytest

# Backend configuration expects deployment secrets; test-only placeholders.
os.environ.setdefault("FRED_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("DB_PATH", ":memory:")

from backend import analysis_engine as ae


def _candles(n, tf_minutes, base_time):
    times = pd.date_range(base_time, periods=n, freq=f"{tf_minutes}min", tz="UTC")
    close = [1.0 + i * 0.001 for i in range(n)]
    return pd.DataFrame({
        "time": times,
        "open": close,
        "high": [x + 0.01 for x in close],
        "low": [x - 0.01 for x in close],
        "close": close,
        "volume": [1.0] * n,
    })


def test_live_analysis_uses_closed_15m_candle_for_price_and_atr(monkeypatch):
    base_time = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=5)
    data = {
        "15m": _candles(70, 15, base_time),
        "1h": _candles(80, 60, base_time),
        "4h": _candles(40, 240, base_time),
        "1d": _candles(40, 1440, base_time),
        "1wk": _candles(40, 10080, base_time),
    }
    seen_atr_lengths = []

    def drop_last_forming(df, minutes):
        if minutes == 15:
            return df.iloc[:-1].reset_index(drop=True)
        return df

    monkeypatch.setattr(ae, "_drop_forming", drop_last_forming)
    def atr_by_timeframe(df):
        seen_atr_lengths.append(len(df))
        return {69: 0.001, 80: 0.02, 40: 0.03}[len(df)]

    monkeypatch.setattr(ae, "calculate_atr", atr_by_timeframe)
    monkeypatch.setattr(ae, "top_down_filter", lambda _: ("BULLISH", "test bias", "BULLISH", "BULLISH", False))
    monkeypatch.setattr(ae, "current_session_label", lambda: "OFF_HOURS")
    monkeypatch.setattr(ae, "find_order_blocks", lambda *a, **k: {
        "bullish": {"high": 1.08, "low": 1.06}, "bearish": None
    })
    ob_atrs = []
    monkeypatch.setattr(
        ae, "_distance_to_ob",
        lambda price, ob, atr: ob_atrs.append(atr) or 0.0,
    )
    monkeypatch.setattr(ae, "get_premium_discount", lambda *a, **k: {"zone": "DISCOUNT", "high": 2.0, "low": 0.5, "eq": 1.25})
    monkeypatch.setattr(ae, "detect_fvg", lambda *a, **k: None)
    monkeypatch.setattr(ae, "detect_liquidity_sweep", lambda *a, **k: None)
    monkeypatch.setattr(ae, "detect_choch", lambda *a, **k: None)
    monkeypatch.setattr(ae, "detect_unicorn_setup", lambda *a, **k: None)
    monkeypatch.setattr(ae, "get_candle_confirmation", lambda *a, **k: False)

    result = ae.analyze_technical_topdown({key: frame.to_dict("records") for key, frame in data.items()})

    assert seen_atr_lengths == [69, 80, 40]
    assert result["price"] == data["15m"]["close"].iloc[-2]
    assert result["atr"] == 0.001
    assert ob_atrs == [0.03]
    # The 4h OB stop buffer is sized with 1h ATR, not 15m ATR.
    assert result["direction"] == "BUY"
    assert result["sl"] == 1.03



def test_gold_futures_proxy_requires_explicit_opt_in():
    import subprocess
    import sys

    env = os.environ.copy()
    env.update({
        "FRED_API_KEY": "test-key",
        "TELEGRAM_BOT_TOKEN": "123456:test-token",
        "TELEGRAM_CHAT_ID": "123456",
        "ALLOW_GOLD_FUTURES_PROXY": "false",
    })
    disabled = subprocess.run(
        [sys.executable, "-c", "import backend.config as c; assert 'GC=F' not in c.SYMBOLS"],
        env=env, capture_output=True, text=True,
    )
    assert disabled.returncode == 0, disabled.stderr

    env["ALLOW_GOLD_FUTURES_PROXY"] = "true"
    enabled = subprocess.run(
        [sys.executable, "-c", "import backend.config as c; assert c.SYMBOLS['GC=F'].startswith('Gold Futures')"],
        env=env, capture_output=True, text=True,
    )
    assert enabled.returncode == 0, enabled.stderr



def test_backend_topdown_uses_engine_provider_and_expected_tail_windows(monkeypatch):
    from backend import data_fetcher

    base_time = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1200)
    frames = {
        "1wk": _candles(130, 10080, base_time),
        "1d": _candles(210, 1440, base_time),
        "4h": _candles(210, 240, base_time),
        "1h": _candles(310, 60, base_time),
        "15m": _candles(410, 15, base_time),
    }
    calls = []
    data_fetcher._TOPDOWN_CACHE.clear()
    monkeypatch.setattr(
        data_fetcher, "load_yfinance_topdown",
        lambda symbol: calls.append(symbol) or frames,
    )
    monkeypatch.setattr(data_fetcher, "staleness_minutes", lambda df, tf, now: 0.0)

    result = data_fetcher.fetch_topdown("EURUSD=X")
    cached_result = data_fetcher.fetch_topdown("EURUSD=X")

    assert calls == ["EURUSD=X"]
    assert cached_result is result
    assert {tf: len(rows) for tf, rows in result.items()} == {
        "1wk": 120, "1d": 200, "4h": 200, "1h": 300, "15m": 400,
    }
    assert result["1wk"][0]["time"] == frames["1wk"]["time"].iloc[-120].isoformat()
    assert result["15m"][-1]["close"] == frames["15m"]["close"].iloc[-1]

    from engine.data.providers import candle_close_times
    now = pd.Timestamp.now(tz="UTC")
    for tf, rows in result.items():
        returned = pd.DataFrame(rows)
        assert candle_close_times(returned, tf).max() <= now



def test_backend_and_engine_atr_use_identical_wilder_calculation():
    from engine.core import smc

    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC"),
        "open": [10.0] * 6,
        "high": [11.0, 11.5, 12.0, 12.5, 13.0, 12.0],
        "low": [9.0, 8.5, 8.0, 7.5, 7.0, 8.0],
        "close": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
    })

    assert ae.calculate_atr(df, period=3) == pytest.approx(smc.atr(df, period=3))



def test_backend_topdown_rejects_stale_market_candles(monkeypatch):
    from backend import data_fetcher

    base_time = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1200)
    frames = {
        "1wk": _candles(130, 10080, base_time),
        "1d": _candles(210, 1440, base_time),
        "4h": _candles(210, 240, base_time),
        "1h": _candles(310, 60, base_time),
        "15m": _candles(410, 15, base_time),
    }
    data_fetcher._TOPDOWN_CACHE.clear()
    monkeypatch.setattr(data_fetcher, "load_yfinance_topdown", lambda symbol: frames)
    monkeypatch.setattr(
        data_fetcher, "staleness_minutes",
        lambda df, tf, now: 60.0 if tf == "15m" else 0.0,
    )

    result = data_fetcher.fetch_topdown("EURUSD=X")

    assert result == {"error": "15m داده‌ی کهنه است"}
    assert "EURUSD=X" not in data_fetcher._TOPDOWN_CACHE



def test_canonical_live_adapter_maps_sell_decision_to_signed_backend_score(monkeypatch):
    from engine.strategy import smc_topdown
    from engine.strategy.smc_topdown import Decision

    base_time = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1200)
    data = {
        "15m": _candles(130, 15, base_time).to_dict("records"),
        "1h": _candles(80, 60, base_time).to_dict("records"),
        "4h": _candles(40, 240, base_time).to_dict("records"),
        "1d": _candles(40, 1440, base_time).to_dict("records"),
        "1wk": _candles(40, 10080, base_time).to_dict("records"),
    }

    monkeypatch.setattr(
        smc_topdown, "analyze",
        lambda frames, cfg, now: Decision(
            direction="SELL", score=0.72, entry=1.2, sl=1.3, tp=1.0,
            bias="weekly+daily BEARISH", reasons=["test sell"], blocked=None,
        ),
    )
    result = ae.analyze_canonical_topdown(data)

    assert result["direction"] == "SELL"
    assert result["score"] == pytest.approx(-0.72)
    assert result["price"] == pytest.approx(1.2)
    assert result["sl"] == pytest.approx(1.3)
    assert result["tp"] == pytest.approx(1.0)
    assert result["blocked"] is False



def test_full_analysis_uses_canonical_engine_for_live_technical_layer(monkeypatch):
    import asyncio
    from backend import fundamental_analyzer, market_memory

    tech = {
        "score": -0.9, "direction": "SELL", "price": 1.1,
        "sl": 1.12, "tp": 1.06, "trend": "BEARISH",
        "wk_trend": "BEARISH", "d_trend": "BEARISH",
        "zone": "PREMIUM", "atr": 0.001, "reasons": ["canonical engine"],
        "blocked": False,
    }
    monkeypatch.setattr(ae, "analyze_canonical_topdown", lambda _: tech)
    monkeypatch.setattr(
        ae, "analyze_technical_topdown",
        lambda _: pytest.fail("legacy technical analyzer must not drive live full_analysis"),
    )

    async def fundamental(_symbol):
        return {"score": -1.0, "reasons": []}

    monkeypatch.setattr(fundamental_analyzer, "calculate_fundamental_score", fundamental)
    monkeypatch.setattr(
        market_memory, "calculate_memory_score",
        lambda _symbol: {"score": -1.0, "reasons": []},
    )

    result = asyncio.run(ae.full_analysis({}, "EURUSD=X", news_safe=True, news_sentiment=-1.0))

    assert result["direction"] == "SELL"
    assert result["technical_score"] == pytest.approx(-0.9)
    assert result["suggested_sl"] == pytest.approx(1.12)
    assert result["suggested_tp"] == pytest.approx(1.06)



def test_full_analysis_cannot_override_canonical_score_or_confluence_gate(monkeypatch):
    import asyncio
    from backend import fundamental_analyzer

    tech = {
        "score": 0.35, "direction": None, "price": 1.1,
        "sl": None, "tp": None, "trend": "BULLISH",
        "wk_trend": "BULLISH", "d_trend": "BULLISH",
        "zone": "DISCOUNT", "atr": 0.001,
        "reasons": ["محدودیت موتور: score/gate"], "blocked": True,
    }
    monkeypatch.setattr(ae, "analyze_canonical_topdown", lambda _: tech)
    monkeypatch.setattr(
        fundamental_analyzer, "calculate_fundamental_score",
        lambda _symbol: pytest.fail("fundamentals must not override a canonical strategy block"),
    )

    result = asyncio.run(ae.full_analysis({}, "EURUSD=X", news_safe=True))

    assert result["direction"] is None
    assert result["suggested_sl"] is None
    assert result["suggested_tp"] is None
