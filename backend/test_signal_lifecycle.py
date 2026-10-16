"""Regression tests for signal lifecycle safeguards; uses stdlib unittest."""
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import database, signal_tracker


class SignalLifecycleTests(unittest.TestCase):
    def test_post_expiry_tp_sl_cannot_overwrite_expired_status(self):
        signal = {
            "id": 17,
            "symbol": "EURUSD",
            "direction": "BUY",
            "entry": 1.1,
            "sl": 1.09,
            "tp": 1.12,
            "created_at": "2020-01-01T00:00:00",
        }
        # This candle is long after the 72-hour expiry deadline.
        candles = [{
            "time": "2026-10-16T00:00:00",
            "high": 1.13,
            "low": 1.08,
        }]

        with (
            patch.object(signal_tracker, "get_open_signals", return_value=[signal]),
            patch.object(signal_tracker, "fetch_ohlcv", return_value=candles),
            patch.object(signal_tracker, "close_signal", return_value=True) as close,
        ):
            signal_tracker.check_open_signals()

        close.assert_called_once_with(17, 1.1, 0.0, "EXPIRED")

    @unittest.skipIf(database.USE_POSTGRES, "Uses an isolated SQLite database")
    def test_close_signal_only_changes_an_open_signal_once(self):
        original_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                database.DB_PATH = str(Path(tmp) / "signals.db")
                database.init_db()
                signal_id = database.insert_signal(
                    "EURUSD", "Euro/Dollar", "BUY",
                    1.1, 1.09, 1.12, 0.8, ["test"]
                )

                self.assertTrue(
                    database.close_signal(signal_id, 1.09, -100.0, "LOSS")
                )
                # A second close attempt must not overwrite the first result.
                self.assertFalse(
                    database.close_signal(signal_id, 1.12, 200.0, "WIN")
                )

                saved = database.get_all_signals(limit=10)[0]
                self.assertEqual(saved["status"], "LOSS")
                self.assertTrue(math.isclose(saved["exit_price"], 1.09))
                self.assertTrue(math.isclose(saved["pnl_pips"], -100.0))
            finally:
                database.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
