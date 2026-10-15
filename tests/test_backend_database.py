import os
import pytest
from contextlib import contextmanager

os.environ.setdefault("FRED_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("DB_PATH", ":memory:")

from backend import database


class _Cursor:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


def test_stats_include_marked_to_market_expiry_pnl_and_executable_risk(monkeypatch):
    rows = [
        ("EURUSD=X", "EXPIRED", 1.1000, 1.0990, 10.0),
        ("EURUSD=X", "EXPIRED", 1.1000, 1.0990, -12.0),
    ]

    @contextmanager
    def fake_conn():
        yield object()

    def fake_execute(_conn, query, *_args):
        if "SELECT symbol, status, entry, sl, pnl_pips" in query:
            return _Cursor(rows=rows)
        if "SELECT COUNT(*) FROM signals WHERE status = 'OPEN'" in query:
            return _Cursor(row=(0,))
        raise AssertionError(f"Unexpected SQL: {query}")

    monkeypatch.setattr(database, "get_conn", fake_conn)
    monkeypatch.setattr(database, "_execute", fake_execute)

    stats = database.get_stats_summary()

    # 10 pip nominal risk + 0.4 pip entry half-spread = 10.4 pip executable risk.
    assert stats["expired_signals"] == 2
    assert stats["wins"] == 0
    assert stats["losses"] == 0
    assert stats["total_pips"] == -2.0
    assert stats["total_r"] == pytest.approx(-0.19)
    assert stats["max_drawdown_r"] == pytest.approx(1.15)
    assert stats["profit_factor"] == pytest.approx(0.83)



def test_close_signal_is_atomic_and_does_not_overwrite_closed_result(monkeypatch):
    captured = {}
    state = {"rowcount": 1}

    class _UpdateCursor:
        @property
        def rowcount(self):
            return state["rowcount"]

    @contextmanager
    def fake_conn():
        yield object()

    def fake_execute(_conn, query, params):
        captured["query"] = query
        captured["params"] = params
        return _UpdateCursor()

    monkeypatch.setattr(database, "get_conn", fake_conn)
    monkeypatch.setattr(database, "_execute", fake_execute)

    assert database.close_signal(17, 1.1010, 10.0, "WIN") is True
    assert "WHERE id = ? AND status = 'OPEN'" in captured["query"]
    assert captured["params"][-1] == 17

    # A second concurrent worker finds no OPEN row and cannot overwrite the first close.
    state["rowcount"] = 0
    assert database.close_signal(17, 1.0900, -100.0, "LOSS") is False
