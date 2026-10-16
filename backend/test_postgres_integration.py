"""PostgreSQL integration checks; executed only by the CI PostgreSQL service."""
import uuid

from backend import database


def test_postgres_duplicate_guard_and_close_are_atomic():
    assert database.USE_POSTGRES, "This integration test requires DATABASE_URL to point to PostgreSQL"
    database.init_db()
    symbol = "CI_" + uuid.uuid4().hex[:20]
    first = database.insert_signal(symbol, symbol, "BUY", 1.1, 1.09, 1.12, 0.8, ["first"])
    duplicate = database.insert_signal(symbol, symbol, "SELL", 1.1, 1.11, 1.08, 0.8, ["duplicate"])

    assert first is not None
    assert duplicate is None
    assert database.close_signal(first, 1.09, -100.0, "LOSS") is True
    assert database.close_signal(first, 1.12, 200.0, "WIN") is False

    rows = database.get_all_signals(limit=500)
    saved = next(row for row in rows if row["id"] == first)
    assert saved["status"] == "LOSS"
    assert saved["exit_price"] == 1.09
