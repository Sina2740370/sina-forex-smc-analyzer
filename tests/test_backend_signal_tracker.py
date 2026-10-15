import os
from datetime import datetime, timedelta, timezone

import pytest

# Backend config intentionally requires deployment secrets; dummy values are test-only.
os.environ.setdefault("FRED_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("DB_PATH", ":memory:")

from backend import signal_tracker


def test_expired_signal_is_marked_to_market_with_spread(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    latest_start = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    created = latest_start - timedelta(hours=96)
    closed = []

    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )

    signal = {
        "id": 77, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [{
        "time": latest_start.isoformat(), "open": 1.1000,
        "high": 1.1020, "low": 1.0990, "close": 1.1010, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    signal_id, exit_price, pnl_pips, status = closed[0]
    assert signal_id == 77
    assert status == "EXPIRED"
    # BUY exits on bid and entered on ask; both sides of spread count.
    assert exit_price == pytest.approx(1.10096)
    assert pnl_pips == pytest.approx(9.2)


def test_expired_signal_ignores_tp_hit_after_expiry(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    latest_start = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    # The second candle closes exactly at the 72-hour expiry boundary.
    created = latest_start - timedelta(hours=72) + timedelta(minutes=15)
    closed = []

    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 78, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [
        {
            "time": (latest_start - timedelta(minutes=15)).isoformat(),
            "open": 1.1000, "high": 1.1020, "low": 1.0990,
            "close": 1.1010, "volume": 1,
        },
        {
            "time": latest_start.isoformat(),
            "open": 1.1010, "high": 1.1250, "low": 1.1000,
            "close": 1.1230, "volume": 1,
        },
    ]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    assert closed[0][2] == "EXPIRED"
    assert closed[0][0] == pytest.approx(1.12296)



def test_malformed_candle_timestamp_is_skipped_without_aborting_tracking(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    valid_start = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    created = valid_start - timedelta(minutes=30)
    closed = []

    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 79, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [
        {"time": "not-a-timestamp", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
        {
            "time": valid_start.isoformat(), "open": 1.1, "high": 1.1210,
            "low": 1.0990, "close": 1.1205, "volume": 1,
        },
    ]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    assert closed[0][0] == 79
    assert closed[0][3] == "WIN"



def test_buy_tp_requires_executable_bid_to_reach_target(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candle_time = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 80, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": (candle_time - timedelta(minutes=30)).isoformat(),
    }
    half_spread = signal_tracker.SPREAD["EURUSD=X"] * signal_tracker.PIP_SIZE["EURUSD=X"] / 2
    candles = [{
        "time": candle_time.isoformat(), "open": 1.1190,
        "high": 1.1200 + half_spread / 2,
        "low": 1.1180, "close": 1.1190, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    # Mid-price high touches TP, but executable bid remains below TP.
    assert closed == []


def test_sell_sl_triggers_when_executable_ask_reaches_stop(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candle_time = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 81, "symbol": "EURUSD=X", "direction": "SELL",
        "entry": 1.1000, "sl": 1.1100, "tp": 1.0800,
        "created_at": (candle_time - timedelta(minutes=30)).isoformat(),
    }
    half_spread = signal_tracker.SPREAD["EURUSD=X"] * signal_tracker.PIP_SIZE["EURUSD=X"] / 2
    candles = [{
        "time": candle_time.isoformat(), "open": 1.1090,
        "high": 1.1100 - half_spread / 2,
        "low": 1.1080, "close": 1.1090, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    # Mid-price high is below SL, but executable ask crosses it after spread.
    assert len(closed) == 1
    assert closed[0][0] == 81
    assert closed[0][3] == "LOSS"



def test_stop_loss_touch_in_expiry_boundary_candle_is_counted_as_loss(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candle_time = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    created = candle_time - timedelta(hours=signal_tracker.MAX_SIGNAL_LIFETIME_HOURS) + timedelta(minutes=8)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 82, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [{
        "time": candle_time.isoformat(), "open": 1.1000,
        "high": 1.1210, "low": 1.0890, "close": 1.1100, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    assert closed[0][0] == 82
    assert closed[0][3] == "LOSS"
    assert closed[0][1] == signal["sl"]


def test_expiry_boundary_bar_does_not_award_ambiguous_take_profit(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candle_time = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    created = candle_time - timedelta(hours=signal_tracker.MAX_SIGNAL_LIFETIME_HOURS) + timedelta(minutes=8)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 83, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": created.isoformat(),
    }
    candles = [{
        "time": candle_time.isoformat(), "open": 1.1000,
        "high": 1.1210, "low": 1.0990, "close": 1.1100, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    assert closed[0][0] == 83
    assert closed[0][3] == "EXPIRED"



def test_unknown_symbol_is_not_closed_with_fallback_pip_or_spread(monkeypatch):
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda *args: closed.append(args),
    )
    signal = {
        "id": 99, "symbol": "UNKNOWN_SYMBOL", "direction": "BUY",
        "entry": 100.0, "sl": 99.0, "tp": 102.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    candles = [{
        "time": datetime.now(timezone.utc).isoformat(),
        "open": 100.0, "high": 103.0, "low": 98.0, "close": 101.0, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert closed == []



@pytest.mark.parametrize(
    "direction,entry,sl,tp,open_price,expected_exit",
    [
        ("BUY", 1.1000, 1.0990, 1.1200, 1.0980, 1.09796),
        ("SELL", 1.1000, 1.1010, 1.0800, 1.1020, 1.10204),
    ],
)
def test_stop_gap_uses_worse_executable_open(
    monkeypatch, direction, entry, sl, tp, open_price, expected_exit
):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candle_time = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda signal_id, exit_price, pnl_pips, status:
            closed.append((signal_id, exit_price, pnl_pips, status)),
    )
    signal = {
        "id": 120 if direction == "BUY" else 121,
        "symbol": "EURUSD=X", "direction": direction,
        "entry": entry, "sl": sl, "tp": tp,
        "created_at": (candle_time - timedelta(minutes=30)).isoformat(),
    }
    candles = [{
        "time": candle_time.isoformat(), "open": open_price,
        "high": open_price + 0.0005, "low": open_price - 0.0005,
        "close": open_price, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert len(closed) == 1
    assert closed[0][1] == pytest.approx(expected_exit)
    assert closed[0][2] == pytest.approx(-20.8)
    assert closed[0][3] == "LOSS"



def test_tracker_ignores_forming_candle_until_close(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    forming_start = now.replace(minute=(now.minute // 15) * 15)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda *args: closed.append(args),
    )
    signal = {
        "id": 130, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": (forming_start - timedelta(minutes=30)).isoformat(),
    }
    candles = [{
        "time": forming_start.isoformat(), "open": 1.1000,
        "high": 1.1250, "low": 1.0990, "close": 1.1230, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert closed == []


def test_tracker_refuses_stale_cached_candles(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    stale_start = now.replace(minute=(now.minute // 15) * 15) - timedelta(hours=2)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda *args: closed.append(args),
    )
    signal = {
        "id": 131, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": (stale_start - timedelta(minutes=30)).isoformat(),
    }
    candles = [{
        "time": stale_start.isoformat(), "open": 1.1000,
        "high": 1.1250, "low": 1.0890, "close": 1.1100, "volume": 1,
    }]

    signal_tracker._check_signal(signal, candles)

    assert closed == []



def test_tracker_skips_non_finite_or_inconsistent_ohlc(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candle_time = now.replace(minute=(now.minute // 15) * 15) - timedelta(minutes=15)
    closed = []
    monkeypatch.setattr(
        signal_tracker, "close_signal",
        lambda *args: closed.append(args),
    )
    signal = {
        "id": 132, "symbol": "EURUSD=X", "direction": "BUY",
        "entry": 1.1000, "sl": 1.0900, "tp": 1.1200,
        "created_at": (candle_time - timedelta(minutes=30)).isoformat(),
    }
    candles = [
        {
            "time": candle_time.isoformat(), "open": 1.1000,
            "high": float("nan"), "low": 1.0890, "close": 1.1100, "volume": 1,
        },
        {
            "time": (candle_time - timedelta(minutes=15)).isoformat(),
            "open": 1.1000, "high": 1.0990, "low": 1.1010,
            "close": 1.1000, "volume": 1,
        },
    ]

    signal_tracker._check_signal(signal, candles)

    assert closed == []
