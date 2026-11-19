"""Paper dataset-boundary contract: data exhaustion must not force-close positions."""
from types import SimpleNamespace

import pandas as pd

import engine.mas.paper as paper
from engine.mas.ledger import SQLiteLedger
from tests.test_engine import make_15m


def _naive_utc(value):
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize(None) if timestamp.tzinfo else timestamp


def test_paper_keeps_untriggered_position_open_at_dataset_boundary(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    frame = make_15m(420, seed=11)
    frame.to_csv(data_dir / "EURUSD_M15.csv", index=False)
    out_dir = tmp_path / "paper"

    def wait_signal(*_args, **_kwargs):
        return SimpleNamespace(
            signal="WAIT",
            cand={},
            wait_reason="dataset-boundary-contract",
            to_json=lambda **_kw: {
                "signal": "WAIT",
                "wait_reason": "dataset-boundary-contract",
            },
        )

    monkeypatch.setattr(paper, "analyze_mas", wait_signal)

    # Initialize the Paper account at the first candle, without opening a trade.
    paper.run_paper(
        str(data_dir),
        ["EURUSD"],
        out_dir=str(out_dir),
        feeds=None,
        now=_naive_utc(frame["time"].iloc[0]),
    )

    ledger = SQLiteLedger(out_dir / "ledger.sqlite3")
    entry_time = pd.Timestamp(frame["time"].iloc[0]).isoformat()
    ledger.append_event({
        "account_id": "paper",
        "trade_id": "boundary-open-position",
        "event_type": "PositionOpened",
        "idempotency_key": "boundary-open-position",
        "occurred_at": entry_time,
        "payload": {
            "symbol": "EURUSD",
            "direction": "BUY",
            "lots": "0.01",
            "entry_price": "2.0000",
            "sl": "1.0000",
            "tp2": "3.0000",
            "entry_time": entry_time,
            "risk_usd": "1.00",
            "risk_pct": "0.01",
            "final_score": 50.0,
            "regime": "test",
            "session": "test",
        },
    })

    # The full available dataset contains no SL/TP touch. Reaching its last
    # candle is a data boundary, not an execution event in Paper.
    result = paper.run_paper(
        str(data_dir),
        ["EURUSD"],
        out_dir=str(out_dir),
        feeds=None,
        now=_naive_utc(frame["time"].iloc[-1]),
    )

    events = ledger.read_events("paper")
    projection = ledger.replay_account(
        "paper", ledger.get_meta("initial_balance:paper")
    )
    assert "boundary-open-position" in projection["open_positions"]
    assert not any(
        event.event_type == "PositionClosed"
        and event.trade_id == "boundary-open-position"
        for event in events
    )
    assert not any(
        trade["trade_id"] == "boundary-open-position"
        for trade in result["closed_this_cycle"]
    )
