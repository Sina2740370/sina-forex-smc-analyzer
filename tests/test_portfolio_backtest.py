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
        assert _frame["time"].max() <= pd.Timestamp(now)
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
    assert result.execution_costs_certified is False
    assert result.summary()["execution_cost_model"] == "uncertified_zero_cost_placeholder"

    repeated = portfolio_bt.backtest_portfolio(
        frames, start=start.isoformat(),
        end=(start + pd.Timedelta(hours=1)).isoformat(),
        step_hours=1, equity=10_000.0,
    )
    assert repeated.summary() == result.summary()


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



def test_two_symbol_paper_and_portfolio_backtest_match_open_and_close_accounting(
    tmp_path, monkeypatch
):
    import engine.mas.paper as paper
    from engine.mas.ledger import SQLiteLedger

    symbols = ["EURUSD", "GBPUSD"]
    frames = {symbol: _frame(symbol) for symbol in symbols}
    start = pd.Timestamp("2026-01-05T00:00:00Z")
    end = start + pd.Timedelta(hours=1)
    for frame in frames.values():
        frame.loc[1, "high"] = float(frame.loc[0, "close"]) + 0.003
    data_dir = tmp_path / "parity-data"
    data_dir.mkdir()
    for symbol, frame in frames.items():
        frame.to_csv(data_dir / f"{symbol}_M15.csv", index=False)

    def deterministic_signal(frame, symbol, now, **_kwargs):
        assert frame["time"].max() <= pd.Timestamp(now)
        if pd.Timestamp(now) != start:
            return SimpleNamespace(
                signal="WAIT", cand={}, wait_reason="parity fixture complete",
                fingerprint=lambda: f"parity-{symbol}",
                to_json=lambda **_kw: {
                    "signal": "WAIT", "fingerprint": f"parity-{symbol}",
                },
            )
        close = float(frame.loc[frame["time"] <= now, "close"].iloc[-1])
        candidate = {
            "entry": close, "sl": close - 0.001,
            "tp1": close + 0.0015, "tp2": close + 0.0025, "rr": 2.5,
        }
        return SimpleNamespace(
            signal="BUY", cand=candidate, final_score=80.0,
            confidence=75.0, market_regime="range", session="London",
            fingerprint=lambda: f"parity-{symbol}",
            to_json=lambda **_kw: {
                "signal": "BUY", "fingerprint": f"parity-{symbol}",
            },
        )

    monkeypatch.setattr(portfolio_bt, "analyze_mas", deterministic_signal)

    # The real aggregator applies this as-of slice internally. Keep that
    # behavior in the Paper fixture while asserting the portfolio engine
    # enforces the boundary before invoking its analyzer.
    def paper_asof_signal(frame, symbol, now, **kwargs):
        past = frame.loc[frame["time"] <= pd.Timestamp(now)].reset_index(drop=True)
        return deterministic_signal(past, symbol, now, **kwargs)

    monkeypatch.setattr(paper, "analyze_mas", paper_asof_signal)

    captured = {"paper": [], "portfolio": []}
    original_append_events = SQLiteLedger.append_events

    def capture_append_events(self, events):
        batch = list(events)
        for event in batch:
            account = event.get("account_id", "")
            if account == "paper":
                captured["paper"].append(dict(event))
            elif account.startswith("backtest:portfolio:"):
                captured["portfolio"].append(dict(event))
        return original_append_events(self, batch)

    monkeypatch.setattr(SQLiteLedger, "append_events", capture_append_events)

    paper_dir = tmp_path / "paper-ledger"
    first = paper.run_paper(
        str(data_dir), symbols, out_dir=str(paper_dir),
        now=start, feeds=None,
    )
    assert len(first["opened_this_cycle"]) == 2
    second = paper.run_paper(
        str(data_dir), symbols, out_dir=str(paper_dir),
        now=end, feeds=None,
    )
    assert len(second["closed_this_cycle"]) == 2

    portfolio_result = portfolio_bt.backtest_portfolio(
        frames, start=start.isoformat(), end=end.isoformat(),
        step_hours=1, equity=10_000.0,
    )
    assert len(portfolio_result.trades) == 2

    def select(events, event_type):
        return sorted(
            [event for event in events if event["event_type"] == event_type],
            key=lambda event: event["payload"].get("symbol", ""),
        )

    paper_open = select(captured["paper"], "PositionOpened")
    portfolio_open = select(captured["portfolio"], "PositionOpened")
    assert len(paper_open) == len(portfolio_open) == 2
    for left, right in zip(paper_open, portfolio_open):
        for key in ("symbol", "direction", "lots", "entry_price", "sl", "tp2", "risk_usd", "risk_pct"):
            assert left["payload"][key] == right["payload"][key]

    paper_close = select(captured["paper"], "PositionClosed")
    portfolio_close = select(captured["portfolio"], "PositionClosed")
    assert len(paper_close) == len(portfolio_close) == 2
    for left, right in zip(paper_close, portfolio_close):
        for key in ("lots_closed", "pnl_gross", "commission", "slippage", "swap",
                    "exit_price", "exit_time", "outcome"):
            assert left["payload"][key] == right["payload"][key]



def test_portfolio_backtest_resets_daily_and_weekly_budgets_on_global_timeline(
    monkeypatch
):
    from engine.mas.ledger import SQLiteLedger

    start = pd.Timestamp("2026-01-05T00:00:00Z")
    times = pd.date_range(start, periods=8, freq="1D")
    prices = [1.2 + index * 0.0001 for index in range(len(times))]
    frame = pd.DataFrame({
        "time": times, "open": prices,
        "high": [price + 0.0001 for price in prices],
        "low": [price - 0.0001 for price in prices],
        "close": prices, "volume": [1.0] * len(times),
    })
    monkeypatch.setattr(
        "engine.data.providers.resample_ohlc",
        lambda data, _tf: data.copy(),
    )

    def one_signal(_frame, _symbol, now, **_kwargs):
        if pd.Timestamp(now) != start:
            return SimpleNamespace(signal="WAIT", cand={}, wait_reason="period fixture")
        close = float(_frame["close"].iloc[-1])
        return SimpleNamespace(
            signal="BUY",
            cand={"entry": close, "sl": close - 0.001,
                  "tp2": close + 0.0025, "rr": 2.5},
            final_score=80.0, confidence=75.0,
            market_regime="range", session="London",
            fingerprint=lambda: "weekly-reset-fixture",
        )

    monkeypatch.setattr(portfolio_bt, "analyze_mas", one_signal)
    captured = []
    original = SQLiteLedger.append_events

    def capture(self, events):
        batch = list(events)
        captured.extend(dict(event) for event in batch
                        if event.get("account_id", "").startswith("backtest:portfolio:"))
        return original(self, batch)

    monkeypatch.setattr(SQLiteLedger, "append_events", capture)
    portfolio_bt.backtest_portfolio(
        {"EURUSD": frame}, start=start.isoformat(),
        end=times[-1].isoformat(), step_hours=24,
    )

    daily = [event for event in captured if event["event_type"] == "DailyRiskReset"]
    weekly = [event for event in captured if event["event_type"] == "WeeklyRiskReset"]
    assert {event["payload"]["daily_risk_date"] for event in daily} == {
        timestamp.date().isoformat() for timestamp in times
    }
    assert {event["payload"]["weekly_risk_week"] for event in weekly} == {
        "2026-01-05", "2026-01-12",
    }



def test_portfolio_backtest_never_settles_beyond_explicit_end(monkeypatch):
    frame = _frame("EURUSD")
    start = frame["time"].iloc[0]
    end = frame["time"].iloc[2]
    monkeypatch.setattr(
        "engine.data.providers.resample_ohlc",
        lambda data, _tf: data.copy(),
    )

    def one_entry(_frame, _symbol, now, **_kwargs):
        if pd.Timestamp(now) != start:
            return SimpleNamespace(signal="WAIT", cand={}, wait_reason="end bound")
        close = float(_frame["close"].iloc[-1])
        return SimpleNamespace(
            signal="BUY",
            cand={"entry": close, "sl": close - 0.001,
                  "tp2": close + 0.0025, "rr": 2.5},
            final_score=80.0, confidence=75.0,
            market_regime="range", session="London",
            fingerprint=lambda: "end-bound-fixture",
        )

    monkeypatch.setattr(portfolio_bt, "analyze_mas", one_entry)
    result = portfolio_bt.backtest_portfolio(
        {"EURUSD": frame}, start=start.isoformat(), end=end.isoformat(),
        step_hours=1,
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "TIME_EXIT"
    assert result.trades[0].exit_time == end
    from engine.mas.symbols import realized_pnl_usd
    expected_exit = float(frame.loc[1, "close"])
    assert result.trades[0].pnl_usd == pytest.approx(
        realized_pnl_usd(
            "EURUSD", result.trades[0].entry, expected_exit,
            result.trades[0].direction, result.trades[0].lots,
        )
    )



def test_portfolio_backtest_rejects_invalid_spread_configuration():
    frame = _frame("EURUSD")
    with pytest.raises(ValueError, match="spread"):
        portfolio_bt.backtest_portfolio(
            {"EURUSD": frame}, spreads={"EURUSD": float("nan")},
            start=frame["time"].iloc[0].isoformat(),
            end=frame["time"].iloc[-1].isoformat(),
        )
    with pytest.raises(ValueError, match="unsupported input symbol"):
        portfolio_bt.backtest_portfolio(
            {"EURUSD": frame}, spreads={"GBPUSD": 0.0001},
            start=frame["time"].iloc[0].isoformat(),
            end=frame["time"].iloc[-1].isoformat(),
        )
