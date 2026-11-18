"""Backtest-to-Ledger accounting integration tests."""
from types import SimpleNamespace

import pandas as pd

import engine.mas.backtest as backtest


def test_backtest_settles_final_trade_through_ledger_and_marks_time_exit(monkeypatch):
    start = pd.Timestamp("2026-01-05T00:00:00Z")
    times = [start + pd.Timedelta(minutes=15 * i) for i in range(5)]
    frame = pd.DataFrame({
        "time": times,
        "open": [1.1000, 1.1000, 1.0998, 1.0997, 1.0996],
        "high": [1.1010, 1.1005, 1.1005, 1.1005, 1.1005],
        "low": [1.0990, 1.0994, 1.0994, 1.0994, 1.0994],
        "close": [1.1000, 1.0998, 1.0997, 1.0996, 1.0995],
        "volume": [1, 1, 1, 1, 1],
    })
    calls = {"count": 0}

    def fake_analyze(_df, symbol, now, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            close = float(_df.loc[_df["time"] <= now, "close"].iloc[-1])
            return SimpleNamespace(
                signal="BUY",
                cand={"entry": close, "sl": close - 0.0010,
                      "tp2": close + 0.0030, "rr": 3.0},
                final_score=80.0, confidence=75.0,
                market_regime="range", session="London",
                fingerprint=lambda: "stable-test-fingerprint",
            )
        return SimpleNamespace(signal="WAIT", cand={}, wait_reason="test wait")

    monkeypatch.setattr(backtest, "analyze_mas", fake_analyze)
    result = backtest.backtest_mas(
        frame, "EURUSD", start=start.isoformat(),
        end=(start + pd.Timedelta(hours=1)).isoformat(), step_hours=1,
    )

    assert result.n_signals == 1
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "TIME_EXIT"
    assert result.trades[0].pnl_usd < 0
    assert result.equity_end == result.equity_start + result.trades[0].pnl_usd