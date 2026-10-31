"""تست‌های اصلاحات ممیزی analysis_engine: جریمه soft، Unicorn، DST، کندل در حال تشکیل."""
import logging
import os
from datetime import datetime, timezone

os.environ.setdefault("FRED_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

import pandas as pd
import pytest

from backend import analysis_engine as ae
from engine.data.providers import build_topdown
from engine.test_engine import make_15m


def _data(days=60):
    d = build_topdown(make_15m(days, seed=5))
    return {k: v.copy() for k, v in d.items()}


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.mark.parametrize("trend,sign", [("BULLISH", -1), ("BEARISH", +1)])
def test_soft_penalty_is_direction_aware(monkeypatch, trend, sign):
    """جریمه soft همیشه به سمت صفر است: BUY منفی، SELL مثبت."""
    monkeypatch.setattr(ae, "current_session_label", lambda dt=None: "ACTIVE_SESSION")
    monkeypatch.setattr(ae, "detect_market_regime", lambda df: "TRENDING_LOW_VOL")
    data = _data()
    scores = {}
    for soft in (False, True):
        monkeypatch.setattr(ae, "top_down_filter",
                            lambda d, soft=soft: (trend, "r", trend, "RANGING", soft))
        scores[soft] = ae.analyze_technical_topdown(data, symbol="EURUSD=X")["score"]
    assert abs((scores[True] - scores[False]) - sign * 0.10) < 0.0101


def test_unicorn_requires_breaker_or_fvg(monkeypatch):
    sweep = {"type": "BULLISH_SWEEP", "level": 1.0, "size": 0.001}
    choch = {"type": "BULLISH_CHOCH", "level": 1.0}
    monkeypatch.setattr(ae, "detect_liquidity_sweep", lambda df, lookback=30: sweep)
    monkeypatch.setattr(ae, "detect_choch", lambda df, lookback=20: choch)
    monkeypatch.setattr(ae, "find_breaker_block", lambda df, lookback=40: None)
    monkeypatch.setattr(ae, "detect_fvg", lambda df, lookback=15: None)
    assert ae.detect_unicorn_setup(None, None, None) is None
    monkeypatch.setattr(ae, "detect_fvg",
                        lambda df, lookback=15: {"type": "BULLISH", "top": 1.1, "bottom": 1.0})
    u = ae.detect_unicorn_setup(None, None, None)
    assert u is not None and u["direction"] == "BUY"


def test_sessions_follow_dst():
    # لندن: زمستان UTC=محلی، تابستان محلی=UTC+1
    assert ae.is_london_killzone(_utc(2025, 1, 15, 7)) and not ae.is_london_killzone(_utc(2025, 1, 15, 10))
    assert ae.is_london_killzone(_utc(2025, 7, 15, 6)) and not ae.is_london_killzone(_utc(2025, 7, 15, 9))
    # نیویورک: زمستان محلی=UTC-5، تابستان محلی=UTC-4
    assert ae.is_ny_killzone(_utc(2025, 1, 15, 12)) and not ae.is_ny_killzone(_utc(2025, 1, 15, 11))
    assert ae.is_ny_killzone(_utc(2025, 7, 15, 11)) and not ae.is_ny_killzone(_utc(2025, 7, 15, 14))
    assert ae.current_session_label(_utc(2025, 7, 15, 6)) == "KILLZONE"
    assert ae.current_session_label(_utc(2025, 7, 15, 3)) == "OFF_HOURS"
    # dt بدون tzinfo = UTC
    assert ae.is_london_killzone(datetime(2025, 1, 15, 8))


def test_4h_forming_candle_is_dropped(monkeypatch):
    monkeypatch.setattr(ae, "top_down_filter",
                        lambda d: ("BULLISH", "r", "BULLISH", "BULLISH", False))
    data = _data()
    n = len(data["4h"])
    data["4h"].loc[data["4h"].index[-1], "time"] = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1)
    seen = []
    real = ae.find_order_blocks

    def spy(df, lookback=80):
        seen.append(len(df))
        return real(df, lookback)

    monkeypatch.setattr(ae, "find_order_blocks", spy)
    ae.analyze_technical_topdown(data, symbol="EURUSD=X")
    assert seen and all(x == n - 1 for x in seen)


def test_daily_weekly_forming_candles_dropped_in_top_down_filter(monkeypatch):
    data = _data(400)
    now = pd.Timestamp.now(tz="UTC")
    data["1wk"].loc[data["1wk"].index[-1], "time"] = now - pd.Timedelta(days=1)
    data["1d"].loc[data["1d"].index[-1], "time"] = now - pd.Timedelta(hours=1)
    nw, nd = len(data["1wk"]), len(data["1d"])
    seen = []
    real = ae.detect_structure

    def spy(df):
        seen.append(len(df))
        return real(df)

    monkeypatch.setattr(ae, "detect_structure", spy)
    ae.top_down_filter(data)
    assert seen == [nw - 1, nd - 1]


def test_drop_forming_bad_input_returns_df_and_logs(caplog):
    df = pd.DataFrame({"x": [1, 2, 3]})
    with caplog.at_level(logging.WARNING):
        out = ae._drop_forming(df, 240)
    assert out is df
    assert any("_drop_forming" in r.message for r in caplog.records)
