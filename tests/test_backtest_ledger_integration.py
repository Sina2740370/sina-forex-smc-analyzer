"""Backtest-to-Ledger accounting integration tests."""
from types import SimpleNamespace

import pandas as pd
import pytest

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


def test_paper_and_backtest_match_same_market_input_through_take_profit(
    tmp_path, monkeypatch
):
    """Same candles and deterministic decision must yield matching financial results."""
    from types import SimpleNamespace

    import engine.mas.paper as paper
    from engine.mas.ledger import SQLiteLedger

    captured_events = {"paper": [], "backtest": []}
    original_append_events = SQLiteLedger.append_events

    def capture_append_events(self, events):
        batch = list(events)
        for event in batch:
            account = event.get("account_id")
            if account == "paper":
                captured_events["paper"].append(dict(event))
            elif account == "backtest:EURUSD":
                captured_events["backtest"].append(dict(event))
        return original_append_events(self, batch)

    monkeypatch.setattr(SQLiteLedger, "append_events", capture_append_events)

    start = pd.Timestamp("2026-01-05T00:00:00Z")
    times = [start + pd.Timedelta(minutes=15 * i) for i in range(5)]
    frame = pd.DataFrame({
        "time": times,
        "open": [1.1000, 1.1000, 1.1010, 1.1020, 1.1030],
        "high": [1.1010, 1.1035, 1.1030, 1.1040, 1.1050],
        "low": [1.0990, 1.1005, 1.1010, 1.1020, 1.1030],
        "close": [1.1000, 1.1010, 1.1020, 1.1030, 1.1040],
        "volume": [1, 1, 1, 1, 1],
    })
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    frame.to_csv(data_dir / "EURUSD_M15.csv", index=False)

    def deterministic_signal(_df, _symbol, now, **_kwargs):
        if pd.Timestamp(now) > start:
            return SimpleNamespace(
                signal="WAIT", cand={}, wait_reason="parity fixture complete",
                fingerprint=lambda: "parity-fixture",
                to_json=lambda **_kw: {
                    "signal": "WAIT", "fingerprint": "parity-fixture"
                },
            )
        close = float(_df.loc[_df["time"] <= now, "close"].iloc[-1])
        candidate = {
            "entry": close,
            "sl": close - 0.0007,
            "tp1": close + 0.0010,
            "tp2": close + 0.0020,
            "rr": 2.0 / 0.7,
        }
        return SimpleNamespace(
            signal="BUY", cand=candidate, final_score=80.0,
            confidence=75.0, market_regime="range", session="London",
            fingerprint=lambda: "parity-fixture",
            to_json=lambda **_kw: {
                "signal": "BUY", "fingerprint": "parity-fixture"
            },
        )

    monkeypatch.setattr(backtest, "analyze_mas", deterministic_signal)
    monkeypatch.setattr(paper, "analyze_mas", deterministic_signal)

    paper_out = tmp_path / "paper"
    first = paper.run_paper(
        str(data_dir), ["EURUSD"], out_dir=str(paper_out), feeds=None,
        now=start.tz_localize(None),
    )
    assert len(first["opened_this_cycle"]) == 1
    second = paper.run_paper(
        str(data_dir), ["EURUSD"], out_dir=str(paper_out), feeds=None,
        now=(start + pd.Timedelta(hours=1)).tz_localize(None),
    )
    assert len(second["closed_this_cycle"]) == 1

    captured_backtest_projection = {}
    original_replay = SQLiteLedger.replay_account

    def capture_replay(self, account_id, initial_balance):
        projection = original_replay(self, account_id, initial_balance)
        if account_id == "backtest:EURUSD":
            captured_backtest_projection["value"] = projection
        return projection

    monkeypatch.setattr(SQLiteLedger, "replay_account", capture_replay)
    bt = backtest.backtest_mas(
        frame, "EURUSD", start=start.isoformat(),
        end=(start + pd.Timedelta(hours=1)).isoformat(), step_hours=1,
    )

    assert bt.n_signals == 1
    assert len(bt.trades) == 1
    assert bt.trades[0].exit_reason == "TP"
    assert bt.trades[0].entry == pytest.approx(
        second["closed_this_cycle"][0]["entry"], abs=1e-12
    )
    assert bt.trades[0].lots == pytest.approx(
        second["closed_this_cycle"][0]["lots"], abs=1e-12
    )
    assert bt.trades[0].exit_time == pd.Timestamp(
        second["closed_this_cycle"][0]["exit_time"]
    )
    assert round(bt.trades[0].pnl_usd, 2) == pytest.approx(
        second["closed_this_cycle"][0]["pnl_usd"], abs=1e-8
    )
    assert bt.equity_end == pytest.approx(
        _paper_state_for_test(paper_out)["equity"], abs=1e-8
    )
    assert float(captured_backtest_projection["value"]["equity"]) == pytest.approx(
        _paper_state_for_test(paper_out)["equity"], abs=1e-8
    )
    assert captured_backtest_projection["value"]["risk_lock_active"] is False
    assert _paper_state_for_test(paper_out)["risk_lock_active"] is False

    def lifecycle(events):
        return [
            event for event in events
            if event["event_type"] in ("PositionOpened", "PositionClosed")
        ]

    paper_lifecycle = lifecycle(captured_events["paper"])
    backtest_lifecycle = lifecycle(captured_events["backtest"])
    assert [event["event_type"] for event in paper_lifecycle] == [
        "PositionOpened", "PositionClosed"
    ]
    assert [event["event_type"] for event in backtest_lifecycle] == [
        "PositionOpened", "PositionClosed"
    ]
    assert [event["trade_id"] for event in paper_lifecycle] == [
        event["trade_id"] for event in backtest_lifecycle
    ]
    for paper_event, backtest_event in zip(paper_lifecycle, backtest_lifecycle):
        assert paper_event["occurred_at"] == backtest_event["occurred_at"]
        for field in ("lots", "entry_price", "exit_price", "pnl_gross"):
            if field in paper_event["payload"] or field in backtest_event["payload"]:
                assert float(paper_event["payload"][field]) == pytest.approx(
                    float(backtest_event["payload"][field]), abs=1e-10
                )


def _paper_state_for_test(out_dir):
    import json

    with open(out_dir / "state.json", encoding="utf-8") as stream:
        return json.load(stream)



def test_paper_and_backtest_match_same_market_input_through_stop_loss(
    tmp_path, monkeypatch
):
    """The same deterministic SL candle must produce matching Paper/BT settlement."""
    from types import SimpleNamespace

    import engine.mas.paper as paper
    from engine.mas.ledger import SQLiteLedger

    captured_events = {"paper": [], "backtest": []}
    original_append_events = SQLiteLedger.append_events

    def capture_append_events(self, events):
        batch = list(events)
        for event in batch:
            account = event.get("account_id")
            if account == "paper":
                captured_events["paper"].append(dict(event))
            elif account == "backtest:EURUSD":
                captured_events["backtest"].append(dict(event))
        return original_append_events(self, batch)

    monkeypatch.setattr(SQLiteLedger, "append_events", capture_append_events)

    start = pd.Timestamp("2026-01-05T00:00:00Z")
    times = [start + pd.Timedelta(minutes=15 * i) for i in range(5)]
    frame = pd.DataFrame({
        "time": times,
        "open": [1.1000, 1.1000, 1.0990, 1.0990, 1.0990],
        "high": [1.1010, 1.1005, 1.1000, 1.1000, 1.1000],
        "low": [1.0990, 1.0980, 1.0985, 1.0985, 1.0985],
        "close": [1.1000, 1.0990, 1.0992, 1.0991, 1.0990],
        "volume": [1, 1, 1, 1, 1],
    })
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    frame.to_csv(data_dir / "EURUSD_M15.csv", index=False)

    def deterministic_signal(_df, _symbol, now, **_kwargs):
        if pd.Timestamp(now) > start:
            return SimpleNamespace(
                signal="WAIT", cand={}, wait_reason="SL parity fixture complete",
                fingerprint=lambda: "sl-parity-fixture",
                to_json=lambda **_kw: {
                    "signal": "WAIT", "fingerprint": "sl-parity-fixture"
                },
            )
        close = float(_df.loc[_df["time"] <= now, "close"].iloc[-1])
        candidate = {
            "entry": close,
            "sl": close - 0.0007,
            "tp1": close + 0.0010,
            "tp2": close + 0.0020,
            "rr": 2.0 / 0.7,
        }
        return SimpleNamespace(
            signal="BUY", cand=candidate, final_score=80.0,
            confidence=75.0, market_regime="range", session="London",
            fingerprint=lambda: "sl-parity-fixture",
            to_json=lambda **_kw: {
                "signal": "BUY", "fingerprint": "sl-parity-fixture"
            },
        )

    monkeypatch.setattr(backtest, "analyze_mas", deterministic_signal)
    monkeypatch.setattr(paper, "analyze_mas", deterministic_signal)

    paper_out = tmp_path / "paper"
    first = paper.run_paper(
        str(data_dir), ["EURUSD"], out_dir=str(paper_out), feeds=None,
        now=start.tz_localize(None),
    )
    assert len(first["opened_this_cycle"]) == 1
    second = paper.run_paper(
        str(data_dir), ["EURUSD"], out_dir=str(paper_out), feeds=None,
        now=(start + pd.Timedelta(hours=1)).tz_localize(None),
    )
    assert len(second["closed_this_cycle"]) == 1
    paper_trade = second["closed_this_cycle"][0]
    assert paper_trade["outcome"] == "SL"

    captured_backtest_projection = {}
    original_replay = SQLiteLedger.replay_account

    def capture_replay(self, account_id, initial_balance):
        projection = original_replay(self, account_id, initial_balance)
        if account_id == "backtest:EURUSD":
            captured_backtest_projection["value"] = projection
        return projection

    monkeypatch.setattr(SQLiteLedger, "replay_account", capture_replay)
    bt = backtest.backtest_mas(
        frame, "EURUSD", start=start.isoformat(),
        end=(start + pd.Timedelta(hours=1)).isoformat(), step_hours=1,
    )
    assert bt.n_signals == 1
    assert len(bt.trades) == 1
    bt_trade = bt.trades[0]
    assert bt_trade.exit_reason == "SL"
    assert bt_trade.entry == pytest.approx(paper_trade["entry"], abs=1e-12)
    assert bt_trade.lots == pytest.approx(paper_trade["lots"], abs=1e-12)
    assert bt_trade.exit_time == pd.Timestamp(paper_trade["exit_time"])
    assert bt_trade.pnl_usd == pytest.approx(paper_trade["pnl_usd"], abs=0.01)
    paper_state = _paper_state_for_test(paper_out)
    backtest_state = captured_backtest_projection["value"]
    assert bt.equity_end == pytest.approx(paper_state["equity"], abs=0.01)
    assert backtest_state["consecutive_losses"] == paper_state["consecutive_losses"] == 1
    assert backtest_state["risk_lock_active"] is paper_state["risk_lock_active"]
    assert float(backtest_state["daily_loss_pct"]) == pytest.approx(
        float(paper_state["daily_loss_pct"]), abs=1e-10
    )
    assert float(backtest_state["weekly_loss_pct"]) == pytest.approx(
        float(paper_state["weekly_loss_pct"]), abs=1e-10
    )

    def lifecycle(events):
        return [
            event for event in events
            if event["event_type"] in ("PositionOpened", "PositionClosed")
        ]

    paper_lifecycle = lifecycle(captured_events["paper"])
    backtest_lifecycle = lifecycle(captured_events["backtest"])
    assert [event["event_type"] for event in paper_lifecycle] == [
        "PositionOpened", "PositionClosed"
    ]
    assert [event["event_type"] for event in backtest_lifecycle] == [
        "PositionOpened", "PositionClosed"
    ]
    for paper_event, backtest_event in zip(paper_lifecycle, backtest_lifecycle):
        assert paper_event["trade_id"] == backtest_event["trade_id"]
        assert paper_event["occurred_at"] == backtest_event["occurred_at"]
        for field in ("lots", "entry_price", "exit_price", "pnl_gross"):
            if field in paper_event["payload"] or field in backtest_event["payload"]:
                assert float(paper_event["payload"][field]) == pytest.approx(
                    float(backtest_event["payload"][field]), abs=1e-10
                )


def test_position_exit_waits_for_entry_candle_close():
    """An entry candle must not be evaluated using its unfinished OHLC."""
    start = pd.Timestamp("2026-01-05T00:00:00Z")
    frame = pd.DataFrame({
        "time": [start, start + pd.Timedelta(minutes=15), start + pd.Timedelta(minutes=30)],
        "open": [1.1000, 1.1000, 1.1010],
        "high": [1.1005, 1.1020, 1.1015],
        "low": [1.0995, 1.0998, 1.1005],
        "close": [1.1000, 1.1010, 1.1012],
    })
    position = {
        "direction": "BUY", "entry_time": frame["time"].iloc[1],
        "entry": 1.1000, "sl": 1.0990, "tp2": 1.1015,
        "lots": 0.01, "last_idx": 0,
    }
    from engine.mas.symbols import get_spec
    timestamps = frame["time"].to_numpy()
    early = backtest._advance_position(
        position, frame, timestamps, start + pd.Timedelta(minutes=20),
        get_spec("EURUSD"),
    )
    assert early is None
    assert position["last_idx"] == 0
    closed = backtest._advance_position(
        position, frame, timestamps, start + pd.Timedelta(minutes=30),
        get_spec("EURUSD"),
    )
    assert closed is not None
    assert closed[0] == pytest.approx(1.1015)
    assert closed[3] is False