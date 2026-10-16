"""Regression tests for signal lifecycle safeguards."""
from unittest.mock import patch

import pytest

from backend import database, signal_tracker


def test_expired_snapshot_is_not_rechecked_for_tp_sl():
    signal = {
        "id": 17,
        "symbol": "EURUSD",
        "direction": "BUY",
        "entry": 1.1,
        "sl": 1.09,
        "tp": 1.12,
        "created_at": "2020-01-01T00:00:00",
    }
    candles = [{
        "time": "2026-10-16T00:00:00",
        "high": 1.13,
        "low": 1.08,
    }]

    with (
        patch.object(signal_tracker, "get_open_signals", return_value=[signal]),
        patch.object(signal_tracker, "fetch_ohlcv", return_value=candles),
        patch.object(signal_tracker, "_check_expired_signals", return_value={17}),
        patch.object(signal_tracker, "_check_signal") as check_signal,
    ):
        signal_tracker.check_open_signals()

    check_signal.assert_not_called()


@pytest.mark.skipif(database.USE_POSTGRES, reason="Uses an isolated SQLite database")
def test_close_signal_only_changes_an_open_signal_once(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "signals.db"))
    database.init_db()
    signal_id = database.insert_signal(
        "EURUSD", "Euro/Dollar", "BUY", 1.1, 1.09, 1.12, 0.8, ["test"]
    )

    assert database.close_signal(signal_id, 1.09, -100.0, "LOSS") is True
    # A second close attempt must not overwrite the first terminal result.
    assert database.close_signal(signal_id, 1.12, 200.0, "WIN") is False

    saved = database.get_all_signals(limit=10)[0]
    assert saved["status"] == "LOSS"
    assert saved["exit_price"] == pytest.approx(1.09)
    assert saved["pnl_pips"] == pytest.approx(-100.0)
