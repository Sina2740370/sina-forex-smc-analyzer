import pandas as pd

from engine.backtest import runner
from engine.core.config import EngineConfig
from engine.strategy.smc_topdown import Decision


class _View:
    def __init__(self, data):
        pass

    def at(self, now):
        return {"now": now}


def test_run_backtest_accepts_strategy_injection_and_cooldown_starts_at_close(monkeypatch):
    monkeypatch.setattr(runner, "TopDownView", _View)
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    times = pd.date_range(start, periods=8, freq="15min")
    lows = [1.10, 1.08, 1.10, 1.10, 1.10, 1.10, 1.10, 1.10]
    data = {
        "15m": pd.DataFrame({
            "time": times,
            "open": [1.10] * 8,
            "high": [1.11] * 8,
            "low": lows,
            "close": [1.105] * 8,
        })
    }
    calls = []

    def strategy(view, cfg, now):
        calls.append(now)
        return Decision(direction="BUY", entry=1.10, sl=1.09, tp=1.12, score=0.7)

    cfg = EngineConfig(cooldown_hours=0.5, max_hold_hours=100)
    trades = runner.run_backtest("EURUSD", data, cfg, analyze_fn=strategy)

    # First trade closes at the second candle; the 30-minute cooldown is measured
    # from that close, so the next strategy evaluation is at index 3, not index 2.
    assert calls == [times[0] + pd.Timedelta(minutes=15),
                     times[3] + pd.Timedelta(minutes=15)]
    assert len(trades) == 2
    assert trades[0]["status"] == "SL"
