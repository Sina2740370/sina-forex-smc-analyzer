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
        "1h": _candles(70, 60, base_time),
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
    monkeypatch.setattr(ae, "calculate_atr", lambda df: seen_atr_lengths.append(len(df)) or 0.001)
    monkeypatch.setattr(ae, "top_down_filter", lambda _: ("BULLISH", "test bias", "BULLISH", "BULLISH", False))
    monkeypatch.setattr(ae, "current_session_label", lambda: "OFF_HOURS")
    monkeypatch.setattr(ae, "find_order_blocks", lambda *a, **k: {"bullish": None, "bearish": None})
    monkeypatch.setattr(ae, "get_premium_discount", lambda *a, **k: {"zone": "DISCOUNT", "high": 2.0, "low": 0.5, "eq": 1.25})
    monkeypatch.setattr(ae, "detect_fvg", lambda *a, **k: None)
    monkeypatch.setattr(ae, "detect_liquidity_sweep", lambda *a, **k: None)
    monkeypatch.setattr(ae, "detect_choch", lambda *a, **k: None)
    monkeypatch.setattr(ae, "detect_unicorn_setup", lambda *a, **k: None)
    monkeypatch.setattr(ae, "get_candle_confirmation", lambda *a, **k: False)

    result = ae.analyze_technical_topdown({key: frame.to_dict("records") for key, frame in data.items()})

    assert seen_atr_lengths == [69]
    assert result["price"] == data["15m"]["close"].iloc[-2]
    assert result["atr"] == 0.001



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
