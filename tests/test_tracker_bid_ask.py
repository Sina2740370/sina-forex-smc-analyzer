import os
os.environ.setdefault("FRED_API_KEY", "x"); os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x"); os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
from datetime import datetime, timedelta, timezone
import pytest
from backend import signal_tracker as st
from engine.backtest.runner import check_exit

SYM = "EURUSD=X"
HALF = st.SPREAD[SYM] * st.PIP_SIZE[SYM] / 2
LEVELS = {"BUY": (1.0950, 1.1100), "SELL": (1.1050, 1.0900)}   # (sl, tp) با ورود 1.1000
CASES = {
    "BUY": [(1.1000, 1.09503), (1.1000, 1.09505), (1.11003, 1.0960), (1.11005, 1.0960), (1.1010, 1.0990)],
    "SELL": [(1.10495, 1.0990), (1.10497, 1.0990), (1.1010, 1.08995), (1.1010, 1.08997), (1.1010, 1.0990)],
}

def _run(direction, high, low):
    sl, tp = LEVELS[direction]
    calls = []
    now = datetime.now(timezone.utc)
    sig = {"id": 1, "symbol": SYM, "direction": direction, "entry": 1.1000, "sl": sl, "tp": tp,
           "created_at": (now - timedelta(hours=1)).isoformat()}
    candle = {"time": (now - timedelta(minutes=30)).isoformat(), "high": high, "low": low}
    mp = pytest.MonkeyPatch()
    mp.setattr(st, "close_signal", lambda *a: calls.append(a))
    try:
        st._check_signal(sig, [candle])
    finally:
        mp.undo()
    return calls

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_live_exit_matches_backtest_bid_ask(direction):
    sl, tp = LEVELS[direction]
    side = 1 if direction == "BUY" else -1
    for high, low in CASES[direction]:
        status, _ = check_exit(side, sl, tp, high, low, HALF)
        expected = {"SL": "LOSS", "TP": "WIN", None: None}[status]
        calls = _run(direction, high, low)
        got = calls[0][3] if calls else None
        assert got == expected, (direction, high, low, got, expected)
