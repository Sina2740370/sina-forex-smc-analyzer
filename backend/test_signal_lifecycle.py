"""Regression tests for signal lifecycle safeguards; uses stdlib unittest."""
import math
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config as backend_config, database, signal_tracker
from engine.core.config import spread_price


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
            "open": 1.1,
            "high": 1.13,
            "low": 1.08,
            "close": 1.101,
        }]

        with (
            patch.object(signal_tracker, "get_open_signals", return_value=[signal]),
            patch.object(signal_tracker, "fetch_ohlcv", return_value=candles),
            patch.object(signal_tracker, "close_signal", return_value=True) as close,
        ):
            signal_tracker.check_open_signals()

        close.assert_called_once()
        args = close.call_args.args
        self.assertEqual(args[0], 17)
        self.assertTrue(math.isclose(args[1], 1.10095, abs_tol=1e-9))
        self.assertTrue(math.isclose(args[2], 9.0, abs_tol=1e-9))
        self.assertEqual(args[3], "EXPIRED")


    def test_expiry_uses_close_of_bar_that_reaches_deadline(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_at = now - timedelta(minutes=5)
        created_at = expires_at - timedelta(hours=signal_tracker.MAX_SIGNAL_LIFETIME_HOURS)
        signal = {
            "id": 25, "symbol": "EURUSD=X", "direction": "BUY",
            "entry": 1.1, "sl": 1.09, "tp": 1.12,
            "created_at": created_at.isoformat(),
        }
        candle_time = expires_at - timedelta(minutes=15)
        candle = [{
            "time": candle_time.isoformat(), "open": 1.100,
            "high": 1.105, "low": 1.095, "close": 1.101,
        }]
        half_spread = (
            backend_config.SPREAD["EURUSD=X"] * backend_config.PIP_SIZE["EURUSD=X"] / 2
        )
        with patch.object(signal_tracker, "close_signal", return_value=True) as close:
            signal_tracker._check_signal(signal, candle)
        close.assert_called_once()
        args = close.call_args.args
        self.assertEqual(args[0], 25)
        self.assertTrue(math.isclose(args[1], 1.101 - half_spread, abs_tol=1e-9))
        self.assertEqual(args[3], "EXPIRED")

    def test_forming_15m_candle_cannot_close_signal(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        signal = {
            "id": 24, "symbol": "EURUSD", "direction": "BUY",
            "entry": 1.1, "sl": 1.099, "tp": 1.102,
            "created_at": (now - timedelta(hours=1)).isoformat(),
        }
        forming_candle = [{
            "time": (now - timedelta(minutes=5)).isoformat(),
            "open": 1.101, "high": 1.103, "low": 1.1005, "close": 1.1025,
        }]
        with patch.object(signal_tracker, "close_signal") as close:
            signal_tracker._check_signal(signal, forming_candle)
        close.assert_not_called()

    def test_gold_spread_assumption_matches_engine(self):
        backend_gold_spread = (
            backend_config.SPREAD["GC=F"] * backend_config.PIP_SIZE["GC=F"]
        )
        self.assertTrue(math.isclose(backend_gold_spread, spread_price("XAUUSD")))

    def test_buy_tp_requires_bid_to_reach_target(self):
        signal = {
            "id": 21, "symbol": "EURUSD", "direction": "BUY",
            "entry": 1.1, "sl": 1.099, "tp": 1.102,
            "created_at": "2026-10-15T00:00:00",
        }
        candles = [{
            "time": "2026-10-15T00:15:00", "high": 1.10203,
            "low": 1.1005, "close": 1.101,
        }]
        # Mid/high crosses TP, but bid (high - half-spread) does not.
        with patch.object(signal_tracker, "close_signal") as close:
            signal_tracker._check_signal(signal, candles)
        close.assert_not_called()

    def test_buy_tp_closes_when_bid_reaches_target(self):
        signal = {
            "id": 22, "symbol": "EURUSD", "direction": "BUY",
            "entry": 1.1, "sl": 1.099, "tp": 1.102,
            "created_at": "2026-10-15T00:00:00",
        }
        candles = [{
            "time": "2026-10-15T00:15:00", "high": 1.10206,
            "low": 1.1005, "close": 1.101,
        }]
        with patch.object(signal_tracker, "close_signal") as close:
            signal_tracker._check_signal(signal, candles)
        close.assert_called_once_with(22, 1.102, unittest.mock.ANY, "WIN")

    def test_sell_stop_triggers_when_ask_reaches_stop(self):
        signal = {
            "id": 23, "symbol": "EURUSD", "direction": "SELL",
            "entry": 1.1, "sl": 1.102, "tp": 1.098,
            "created_at": "2026-10-15T00:00:00",
        }
        candles = [{
            "time": "2026-10-15T00:15:00", "high": 1.10196,
            "low": 1.099, "close": 1.100,
        }]
        # Mid/high stays below SL; ask (high + half-spread) crosses it.
        with patch.object(signal_tracker, "close_signal") as close:
            signal_tracker._check_signal(signal, candles)
        close.assert_called_once_with(23, 1.102, unittest.mock.ANY, "LOSS")


    def test_signal_tracker_uses_next_eligible_bar_open_for_entry(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        created_at = now - timedelta(minutes=30)
        signal = {
            "id": 31, "symbol": "EURUSD=X", "direction": "BUY",
            "entry": 1.1, "sl": 1.09, "tp": 1.101,
            "created_at": created_at.isoformat(),
        }
        candles = [
            {
                "time": created_at.isoformat(), "open": 1.1,
                "high": 1.101, "low": 1.099, "close": 1.1,
            },
            {
                "time": (now - timedelta(minutes=15)).isoformat(),
                "open": 1.1005, "high": 1.102, "low": 1.1002, "close": 1.101,
            },
        ]
        half_spread = (
            backend_config.SPREAD["EURUSD=X"] * backend_config.PIP_SIZE["EURUSD=X"] / 2
        )
        with patch.object(signal_tracker, "close_signal", return_value=True) as close:
            signal_tracker._check_signal(signal, candles)

        close.assert_called_once()
        args = close.call_args.args
        self.assertEqual(args[0], 31)
        self.assertEqual(args[1], 1.101)
        self.assertEqual(args[3], "WIN")
        self.assertTrue(math.isclose(args[2], (1.101 - (1.1005 + half_spread)) / 0.0001, abs_tol=0.1))


    def test_signal_tracker_rejects_unfillable_entry_gap(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        created_at = now - timedelta(minutes=30)
        signal = {
            "id": 32, "symbol": "EURUSD=X", "direction": "BUY",
            "entry": 1.1, "sl": 1.09, "tp": 1.12,
            "created_at": created_at.isoformat(),
        }
        candles = [
            {
                "time": created_at.isoformat(), "open": 1.1,
                "high": 1.101, "low": 1.099, "close": 1.1,
            },
            {
                "time": (now - timedelta(minutes=15)).isoformat(),
                "open": 1.105, "high": 1.106, "low": 1.104, "close": 1.105,
            },
        ]
        with patch.object(signal_tracker, "close_signal", return_value=True) as close:
            signal_tracker._check_signal(signal, candles)

        close.assert_called_once()
        self.assertEqual(close.call_args.args[2], 0.0)
        self.assertEqual(close.call_args.args[3], "EXPIRED")

    @unittest.skipIf(database.USE_POSTGRES, "Uses an isolated SQLite database")
    def test_duplicate_open_symbol_is_rejected(self):
        original_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                database.DB_PATH = str(Path(tmp) / "duplicates.db")
                database.init_db()
                first = database.insert_signal(
                    "EURUSD", "Euro/Dollar", "BUY",
                    1.1, 1.09, 1.12, 0.8, ["first"], config_hash="cfg-test"
                )
                second = database.insert_signal(
                    "EURUSD", "Euro/Dollar", "SELL",
                    1.1, 1.11, 1.08, 0.8, ["duplicate"]
                )
                self.assertIsNotNone(first)
                self.assertIsNone(second)
                self.assertEqual(len(database.get_open_signals()), 1)
                saved = database.get_all_signals(limit=10)[0]
                self.assertEqual(saved["config_hash"], "cfg-test")
            finally:
                database.DB_PATH = original_path

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
