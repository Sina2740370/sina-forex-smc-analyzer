"""Regression tests for the current SMC top-down analysis implementation."""
import pandas as pd

from backend import analysis_engine as ae
from engine.core import smc
from engine.strategy import smc_topdown


def _ohlc_frame(times):
    n = len(times)
    close = [1.10 + i * 0.0001 for i in range(n)]
    return pd.DataFrame({
        "time": times,
        "open": close,
        "high": [x + 0.0005 for x in close],
        "low": [x - 0.0005 for x in close],
        "close": close,
        "volume": [1.0] * n,
    })


def test_top_down_bias_reports_full_and_soft_alignment(monkeypatch):
    states = iter(["BULLISH", "BULLISH"])
    monkeypatch.setattr(smc, "structure", lambda _df: next(states))
    monkeypatch.setattr(smc, "ema_slope", lambda _df, period: "BULLISH")

    side, strength, _ = smc_topdown.top_down_bias(pd.DataFrame(), pd.DataFrame())
    assert side == 1
    assert strength == "full"

    states = iter(["RANGING", "BEARISH"])
    monkeypatch.setattr(smc, "structure", lambda _df: next(states))
    monkeypatch.setattr(smc, "ema_slope", lambda _df, period: "BEARISH")

    side, strength, _ = smc_topdown.top_down_bias(pd.DataFrame(), pd.DataFrame())
    assert side == -1
    assert strength == "soft"


def test_kill_zone_uses_london_and_new_york_dst():
    # London 07:00 local: UTC 07:00 in winter, UTC 06:00 in summer.
    assert smc_topdown.kill_zone(pd.Timestamp("2025-01-15T07:00:00Z"))
    assert smc_topdown.kill_zone(pd.Timestamp("2025-07-15T06:00:00Z"))
    # New York 07:00 local: UTC 12:00 in winter, UTC 11:00 in summer.
    assert smc_topdown.kill_zone(pd.Timestamp("2025-01-15T12:00:00Z"))
    assert smc_topdown.kill_zone(pd.Timestamp("2025-07-15T11:00:00Z"))
    assert not smc_topdown.kill_zone(pd.Timestamp("2025-07-15T03:00:00Z"))


def test_4h_forming_candle_is_dropped_at_explicit_snapshot():
    snapshot = pd.Timestamp("2025-03-10T20:00:00Z")
    times = pd.date_range(end=snapshot - pd.Timedelta(hours=1), periods=30, freq="4h")
    data = _ohlc_frame(times)

    cleaned = ae._drop_forming(data, 240, now=snapshot, timeframe="4h")
    assert len(cleaned) == len(data) - 1


def test_daily_and_weekly_forming_candles_are_removed_before_smc(monkeypatch):
    snapshot = pd.Timestamp("2025-03-10T20:00:00Z")
    data = {
        "15m": _ohlc_frame(pd.date_range(end=snapshot - pd.Timedelta(minutes=30), periods=30, freq="15min")),
        "4h": _ohlc_frame(pd.date_range(end=snapshot - pd.Timedelta(hours=8), periods=30, freq="4h")),
        "1d": _ohlc_frame(pd.date_range(end=snapshot - pd.Timedelta(hours=1), periods=30, freq="1D")),
        "1wk": _ohlc_frame(pd.date_range(end=snapshot - pd.Timedelta(hours=1), periods=30, freq="7D")),
    }
    seen = {}

    class Decision:
        blocked = None
        direction = None
        score = 0.0
        grade = "-"
        power = 0.0
        quality = 0.0
        bias = "RANGING"
        entry = 1.1
        sl = None
        tp = None
        reasons = []
        features = {}

    def fake_analyze(cleaned, _cfg, _now):
        seen.update({tf: len(frame) for tf, frame in cleaned.items()})
        return Decision()

    monkeypatch.setattr(smc_topdown, "analyze", fake_analyze)
    ae.analyze_technical_topdown(data, symbol="EURUSD=X", now=snapshot)

    assert seen["1d"] == 29
    assert seen["1wk"] == 29
    assert seen["15m"] == 30
    assert seen["4h"] == 30


def test_drop_forming_without_time_column_is_a_noop():
    data = pd.DataFrame({"close": [1.0, 1.1, 1.2]})
    assert ae._drop_forming(data, 240, now=pd.Timestamp("2025-03-10T20:00:00Z")) is data
