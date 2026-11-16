import pandas as pd

from backend import data_fetcher


def _bars(last_open: str, count: int = 200):
    end = pd.Timestamp(last_open)
    times = pd.date_range(end=end, periods=count, freq="15min")
    return [
        {"time": t.isoformat(), "open": 1.1, "high": 1.101,
         "low": 1.099, "close": 1.1005, "volume": 100.0}
        for t in times
    ]


def test_fetch_topdown_rejects_stale_m15_cache(monkeypatch):
    now = pd.Timestamp("2026-11-16T12:00:00Z")

    def fake_fetch(_symbol, timeframe):
        if timeframe == "15m":
            return _bars("2026-11-16T10:30:00Z")
        return _bars("2026-11-16T11:45:00Z")

    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", fake_fetch)
    result = data_fetcher.fetch_topdown("EURUSD=X", now=now)

    assert "error" in result
    assert "کهنه" in result["error"]


def test_fetch_topdown_accepts_fresh_m15_cache(monkeypatch):
    now = pd.Timestamp("2026-11-16T12:00:00Z")

    def fake_fetch(_symbol, timeframe):
        if timeframe == "15m":
            return _bars("2026-11-16T11:45:00Z")
        return _bars("2026-11-16T11:45:00Z")

    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", fake_fetch)
    result = data_fetcher.fetch_topdown("EURUSD=X", now=now)

    assert "error" not in result
    assert set(result) == {"1wk", "1d", "4h", "1h", "15m"}
