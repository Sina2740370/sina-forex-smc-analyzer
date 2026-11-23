from contextlib import contextmanager
import sqlite3

import pytest

from backend import journal_store as store


@pytest.fixture
def journal_db(tmp_path, monkeypatch):
    db_path = tmp_path / "manual-journal.sqlite3"

    @contextmanager
    def isolated_connection():
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(store, "get_conn", isolated_connection)
    store.init_journal_table()
    return db_path


def open_position(**overrides):
    values = {
        "symbol": "EURUSD",
        "side": "BUY",
        "entry_price": 1.1000,
        "stop_loss": 1.0900,
        "lots": 1.0,
        "idempotency_key": "manual-open-0001",
    }
    values.update(overrides)
    # Keep fixture risk geometry valid for both directions unless explicitly
    # overridden by a test that is exercising an invalid stop-loss value.
    if values["side"] == "SELL" and "stop_loss" not in overrides:
        values["stop_loss"] = 1.1200
    return store.create_manual_position(**values)


def test_manual_open_is_idempotent_and_not_a_trade_fill_from_signal(journal_db):
    first = open_position(signal_id=123, strategy_version="v1")
    second = open_position(signal_id=123, strategy_version="v1")

    assert first["id"] == second["id"]
    assert first["status"] == "OPEN"
    assert first["closed_lots"] == 0
    assert first["initial_lots"] == 1.0
    events = store.list_manual_position_events(first["id"])
    assert [event["event_type"] for event in events] == ["OPEN"]


def test_partial_and_final_close_track_weighted_gross_r_and_costs(journal_db):
    position = open_position()
    partial = store.close_manual_position(
        position["id"], 0.4, 1.1050,
        cost_amount=1.25, cost_currency="USD",
        idempotency_key="manual-close-part-01",
    )
    assert partial["event_type"] == "PARTIAL_CLOSE"
    assert partial["gross_r"] == pytest.approx(0.5)
    assert partial["weighted_r"] == pytest.approx(0.2)
    assert partial["cost_amount"] == pytest.approx(1.25)

    current = store.get_manual_position(position["id"])
    assert current["status"] == "OPEN"
    assert current["closed_lots"] == pytest.approx(0.4)
    assert current["closed_at"] is None

    final = store.close_manual_position(
        position["id"], 0.6, 1.1100,
        idempotency_key="manual-close-final-01",
    )
    assert final["event_type"] == "CLOSE"
    assert final["gross_r"] == pytest.approx(1.0)
    assert final["weighted_r"] == pytest.approx(0.6)

    current = store.get_manual_position(position["id"])
    assert current["status"] == "CLOSED"
    assert current["closed_lots"] == pytest.approx(1.0)
    assert current["closed_at"] is not None
    assert sum(e["weighted_r"] for e in store.list_manual_position_events(position["id"])) == pytest.approx(0.8)


def test_duplicate_close_event_does_not_double_count(journal_db):
    position = open_position()
    first = store.close_manual_position(
        position["id"], 0.25, 1.1040,
        idempotency_key="manual-close-idem-01",
    )
    second = store.close_manual_position(
        position["id"], 0.25, 1.1040,
        idempotency_key="manual-close-idem-01",
    )
    assert first["id"] == second["id"]
    assert store.get_manual_position(position["id"])["closed_lots"] == pytest.approx(0.25)
    assert len(store.list_manual_position_events(position["id"])) == 2


def test_over_close_is_rejected_without_mutating_position(journal_db):
    position = open_position()
    with pytest.raises(ValueError, match="حجم خروج"):
        store.close_manual_position(
            position["id"], 1.01, 1.1050,
            idempotency_key="manual-close-over-01",
        )
    current = store.get_manual_position(position["id"])
    assert current["status"] == "OPEN"
    assert current["closed_lots"] == 0
    assert len(store.list_manual_position_events(position["id"])) == 1


@pytest.mark.parametrize(
    "side,entry,stop",
    [
        ("BUY", 1.1000, 1.1000),
        ("BUY", 1.1000, 1.1100),
        ("SELL", 1.1000, 1.0900),
        ("HOLD", 1.1000, 1.0900),
    ],
)
def test_invalid_direction_or_stop_is_rejected(journal_db, side, entry, stop):
    with pytest.raises(ValueError):
        open_position(side=side, entry_price=entry, stop_loss=stop,
                      idempotency_key="manual-open-invalid")


def test_close_more_than_remaining_after_partial_is_rejected(journal_db):
    position = open_position()
    store.close_manual_position(
        position["id"], 0.7, 1.1050,
        idempotency_key="manual-close-part-02",
    )
    with pytest.raises(ValueError, match="حجم خروج"):
        store.close_manual_position(
            position["id"], 0.31, 1.1050,
            idempotency_key="manual-close-part-03",
        )
    assert store.get_manual_position(position["id"])["closed_lots"] == pytest.approx(0.7)


def test_closed_position_cannot_be_closed_again(journal_db):
    position = open_position()
    store.close_manual_position(
        position["id"], 1.0, 1.0950,
        idempotency_key="manual-close-final-02",
    )
    with pytest.raises(ValueError, match="بسته شده"):
        store.close_manual_position(
            position["id"], 0.1, 1.0960,
            idempotency_key="manual-close-after-final",
        )


def test_idempotency_key_cannot_be_reused_for_different_open_request(journal_db):
    open_position()
    with pytest.raises(ValueError, match="ورودی متفاوت"):
        open_position(entry_price=1.1010)


def test_idempotency_key_cannot_be_reused_for_different_close_request(journal_db):
    position = open_position()
    store.close_manual_position(
        position["id"], 0.2, 1.1040,
        idempotency_key="manual-close-reuse-01",
    )
    with pytest.raises(ValueError, match="خروج متفاوت"):
        store.close_manual_position(
            position["id"], 0.3, 1.1040,
            idempotency_key="manual-close-reuse-01",
        )


def test_close_timestamp_cannot_precede_open_timestamp(journal_db):
    position = open_position(opened_at="2026-11-23T10:00:00Z")
    with pytest.raises(ValueError, match="قبل از زمان ورود"):
        store.close_manual_position(
            position["id"], 0.2, 1.1040,
            event_at="2026-11-23T09:59:59Z",
            idempotency_key="manual-close-time-01",
        )


def test_manual_position_journal_record_preserves_weighted_r(journal_db):
    position = open_position()
    store.close_manual_position(
        position["id"], 0.4, 1.1050,
        cost_amount=1.0, cost_currency="USD",
        idempotency_key="manual-close-stats-01",
    )
    store.close_manual_position(
        position["id"], 0.6, 1.1100,
        idempotency_key="manual-close-stats-02",
    )
    records = store.manual_positions_as_journal_records()
    assert len(records) == 1
    from engine.tools.journal import normalize
    assert normalize(records)[0]["r"] == pytest.approx(0.8)
    assert store.manual_position_costs_by_currency() == {"USD": 1.0}


def test_positive_cost_requires_currency(journal_db):
    position = open_position()
    with pytest.raises(ValueError, match="cost_currency"):
        store.close_manual_position(
            position["id"], 0.2, 1.1040, cost_amount=1.0,
            idempotency_key="manual-close-no-currency",
        )


def test_daily_guard_aggregates_partial_exits_per_position(journal_db):
    position = open_position(opened_at="2026-11-23T08:00:00Z")
    store.close_manual_position(
        position["id"], 0.2, 1.0950,
        event_at="2026-11-23T09:00:00Z",
        idempotency_key="manual-daily-part-01",
    )
    store.close_manual_position(
        position["id"], 0.3, 1.0960,
        event_at="2026-11-23T10:00:00Z",
        idempotency_key="manual-daily-part-02",
    )
    daily = store.manual_position_daily_results("2026-11-23T00:00:00Z")
    assert len(daily) == 1
    assert daily[0][1] == pytest.approx(-0.22)


@pytest.mark.parametrize(
    "side,take_profit",
    [("BUY", 1.1000), ("BUY", 1.0900), ("SELL", 1.1000), ("SELL", 1.1100)],
)
def test_take_profit_must_be_on_profit_side(journal_db, side, take_profit):
    with pytest.raises(ValueError, match="حد سود"):
        open_position(side=side, take_profit=take_profit,
                      idempotency_key="manual-open-invalid-tp")


def test_manual_position_persists_optional_take_profit(journal_db):
    position = open_position(take_profit=1.1200)
    assert position["take_profit"] == pytest.approx(1.1200)


def test_manual_stop_update_is_audited_and_preserves_initial_risk_anchor(journal_db):
    position = open_position(take_profit=1.1200)
    change = store.update_manual_position_stops(
        position["id"], new_stop_loss=1.0950,
        idempotency_key="manual-stops-change-01",
    )
    current = store.get_manual_position(position["id"])
    assert current["stop_loss"] == pytest.approx(1.0900)
    assert current["take_profit"] == pytest.approx(1.1200)
    assert current["current_stop_loss"] == pytest.approx(1.0950)
    assert current["current_take_profit"] == pytest.approx(1.1200)
    duplicate = store.update_manual_position_stops(
        position["id"], new_stop_loss=1.0950,
        idempotency_key="manual-stops-change-01",
    )
    assert duplicate["id"] == change["id"]
    assert len(store.list_manual_position_changes(position["id"])) == 1


def test_manual_target_update_must_remain_on_profit_side(journal_db):
    position = open_position()
    with pytest.raises(ValueError, match="حد سود"):
        store.update_manual_position_stops(
            position["id"], new_take_profit=1.0950,
            idempotency_key="manual-stops-invalid-01",
        )


def test_manual_stops_cannot_be_updated_after_full_close(journal_db):
    position = open_position()
    store.close_manual_position(
        position["id"], 1.0, 1.1050,
        idempotency_key="manual-close-before-stops",
    )
    with pytest.raises(ValueError, match="بسته‌شده"):
        store.update_manual_position_stops(
            position["id"], new_stop_loss=1.0950,
            idempotency_key="manual-stops-closed-01",
        )


def test_daily_trade_limit_uses_new_positions_not_partial_exit_count(journal_db):
    from engine.tools.journal import daily_guard

    result = daily_guard([0.1], max_trades=5, trades_today=5)
    assert result["state"] == "stop"
    assert result["trades_today"] == 5

def test_manual_stop_update_rejects_stop_crossing_entry(journal_db):
    position = open_position()
    with pytest.raises(ValueError, match="عبور کند"):
        store.update_manual_position_stops(
            position["id"], new_stop_loss=1.1010,
            idempotency_key="manual-stop-cross-entry",
        )
    current = store.get_manual_position(position["id"])
    assert current["current_stop_loss"] == pytest.approx(1.0900)
    assert store.list_manual_position_changes(position["id"]) == []


def test_stop_update_idempotency_rejects_changed_request(journal_db):
    position = open_position(take_profit=1.1200)
    first = store.update_manual_position_stops(
        position["id"], new_stop_loss=1.0950,
        idempotency_key="manual-stop-reuse-key",
    )
    assert first["new_stop_loss"] == pytest.approx(1.0950)
    with pytest.raises(ValueError, match="تغییر SL/TP متفاوت"):
        store.update_manual_position_stops(
            position["id"], new_take_profit=1.1300,
            idempotency_key="manual-stop-reuse-key",
        )
    current = store.get_manual_position(position["id"])
    assert current["current_stop_loss"] == pytest.approx(1.0950)
    assert current["current_take_profit"] == pytest.approx(1.1200)
    assert len(store.list_manual_position_changes(position["id"])) == 1



def test_concurrent_closes_cannot_over_close_position(journal_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    position = open_position()
    barrier = Barrier(2)

    def close(key):
        barrier.wait(timeout=5)
        try:
            return store.close_manual_position(
                position["id"], 0.7, 1.1050,
                idempotency_key=key,
            )
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(close, [
            "manual-race-close-001",
            "manual-race-close-002",
        ]))

    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, ValueError)]
    assert len(successes) == 1
    assert len(failures) == 1
    current = store.get_manual_position(position["id"])
    assert current["closed_lots"] == pytest.approx(0.7)
    assert current["status"] == "OPEN"
    assert len(store.list_manual_position_events(position["id"])) == 2


def test_concurrent_retry_with_same_close_key_is_idempotent(journal_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    position = open_position()
    barrier = Barrier(2)

    def close():
        barrier.wait(timeout=5)
        return store.close_manual_position(
            position["id"], 0.4, 1.1050,
            idempotency_key="manual-race-same-key",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: close(), range(2)))

    assert first["id"] == second["id"]
    assert store.get_manual_position(position["id"])["closed_lots"] == pytest.approx(0.4)
    assert len(store.list_manual_position_events(position["id"])) == 2
