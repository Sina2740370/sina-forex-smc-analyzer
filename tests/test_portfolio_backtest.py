"""Shared-account multi-symbol Backtest invariants."""
from types import SimpleNamespace

import pandas as pd
import pytest

import engine.mas.portfolio_backtest as portfolio_bt


def _frame(symbol):
    base = 150.0 if symbol == "USDJPY" else 1.2
    step = 0.01 if symbol == "USDJPY" else 0.0001
    start = pd.Timestamp("2026-01-05T00:00:00Z")
    close = [base + step * i for i in range(5)]
    return pd.DataFrame({
        "time": [start + pd.Timedelta(minutes=15 * i) for i in range(5)],
        "open": close,
        "high": [value + step * 0.5 for value in close],
        "low": [value - step * 0.5 for value in close],
        "close": close,
        "volume": [1.0] * 5,
    })


def test_portfolio_backtest_uses_one_shared_account_and_deterministic_symbol_order(monkeypatch):
    symbols = ["USDJPY", "GBPUSD", "EURUSD", "AUDUSD"]
    frames = {symbol: _frame(symbol) for symbol in symbols}
    start = pd.Timestamp("2026-01-05T00:00:00Z")
    calls = []

    monkeypatch.setattr(
        "engine.data.providers.resample_ohlc",
        lambda frame, _tf: frame.copy(),
    )

    def deterministic_signal(_frame, symbol, now, **_kwargs):
        calls.append((pd.Timestamp(now), symbol))
        if pd.Timestamp(now) != start:
            return SimpleNamespace(
                signal="WAIT", cand={}, wait_reason="fixture wait",
            )
        close = float(_frame.loc[_frame["time"] <= now, "close"].iloc[-1])
        distance = 0.001 if symbol != "USDJPY" else 0.1
        return SimpleNamespace(
            signal="BUY",
            cand={
                "entry": close, "sl": close - distance,
                "tp2": close + distance * 2.5, "rr": 2.5,
            },
            final_score=80.0, confidence=75.0,
            market_regime="range", session="London",
            fingerprint=lambda: f"fixture-{symbol}",
        )

    monkeypatch.setattr(portfolio_bt, "analyze_mas", deterministic_signal)
    result = portfolio_bt.backtest_portfolio(
        frames, start=start.isoformat(),
        end=(start + pd.Timedelta(hours=1)).isoformat(),
        step_hours=1, equity=10_000.0,
    )

    # Three positions are authorized from the same account. A fourth symbol
    # cannot bypass the shared R-OPEN gate as it could in independent runs.
    assert result.n_signals == 3
    assert result.wait_reasons["FILL_RISK_REJECTED"] == 1
    opened = [item for item in calls if item[0] == start]
    assert [symbol for _, symbol in opened] == sorted(symbols)
    assert len(result.trades) == 3
    assert result.ledger_events >= 6
    assert result.equity_end == pytest.approx(
        result.equity_start + sum(trade.pnl_usd for trade in result.trades)
    )
    assert result.open_positions_at_end == []


def test_portfolio_backtest_rejects_invalid_step_and_missing_symbol_data():
    with pytest.raises(ValueError, match="step_hours"):
        portfolio_bt.backtest_portfolio({"EURUSD": _frame("EURUSD")}, step_hours=0)
    with pytest.raises(ValueError, match="no valid UTC candles"):
        portfolio_bt.backtest_portfolio({
            "EURUSD": pd.DataFrame({
                "time": [None], "open": [1.0], "high": [1.1],
                "low": [0.9], "close": [1.0],
            })
        }, start="2026-01-05T00:00:00Z")
