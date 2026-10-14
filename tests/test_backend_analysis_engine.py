import os
from datetime import datetime, timezone

import pandas as pd

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
