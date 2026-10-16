import math
import numpy as np
import pandas as pd
import pytest
import tempfile
from pathlib import Path

from engine.backtest.runner import check_exit, evaluate_gate, run_backtest, summarize, wilson
from engine.cli import _data_source_label
from engine.core import smc
from engine.core.config import EngineConfig, spread_price
from engine.data.providers import TopDownView, _bar_close_times, build_topdown, load_mt5_csv, resample_ohlc
from engine.data.fetch_history import _fetch_side, _midpoint_ohlc
from engine.strategy.smc_topdown import Decision, analyze, kill_zone


def make_15m(days=420, seed=7, base=1.10, vol=0.0004):
    """راه‌رفتن تصادفی با رژیم‌های روند (برای تست‌های ساختاری، نه ادعای سودآوری)."""
    rng = np.random.default_rng(seed)
    n = days * 96
    drift = np.repeat(rng.normal(0, 0.00004, n // 960 + 1), 960)[:n]
    ret = drift + rng.normal(0, vol, n)
    close = base * np.exp(np.cumsum(ret))
    open_ = np.r_[base, close[:-1]]
    wick = np.abs(rng.normal(0, vol * 0.6, (2, n))) * close
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    t = pd.date_range("2025-01-05 22:00", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"time": t, "open": open_, "high": high, "low": low,
                         "close": close, "volume": 1.0})


def test_check_exit_sl_wins_ties_and_spread():
    assert check_exit(1, 99, 102, 103, 98, 0.0) == ("SL", 99)       # هر دو لمس → SL
    assert check_exit(1, 99, 102, 103, 99.5, 0.0) == ("TP", 102)
    assert check_exit(-1, 101, 98, 100.5, 97, 0.0) == ("TP", 98)
    assert check_exit(1, 99, 102, 102.2, 99.8, 0.5) == (None, None)  # اسپرد TP را دور می‌کند
    assert check_exit(1, 99, 102, 100, 98, 0.5, 98) == ("SL", 97.5)  # گپ نامطلوب روی open
    assert check_exit(-1, 101, 98, 103, 100, 0.5, 102) == ("SL", 102.5)


def test_resample_weekly_and_daily_align_to_fx_week():
    m = make_15m(30)
    wk, d = resample_ohlc(m, "1wk"), resample_ohlc(m, "1d")
    # هفته با کندل یکشنبه ۲۲:۰۰ UTC شروع می‌شود و کندل یکشنبه جدا نمی‌ماند
    assert (wk["time"].dt.dayofweek == 6).all() and (wk["time"].dt.hour == 22).all()
    assert (d["time"].dt.hour == 22).all()
    assert wk["high"].max() == m["high"].max()


def test_fvg_mitigation_and_both_types():
    df = pd.DataFrame({"open": [1, 1, 1.3, 1.3, 1.3, 1.3],
                       "high": [1.1, 1.1, 1.4, 1.4, 1.4, 1.4],
                       "low": [0.9, 0.9, 1.2, 1.2, 1.2, 1.2],
                       "close": [1, 1, 1.3, 1.3, 1.3, 1.3]})
    f = smc.fvgs(df, lookback=5, min_gap_ratio=0.0)
    assert f["BULLISH"] is not None and f["BEARISH"] is None
    df.loc[5, "close"] = 1.0                                          # بسته شدن زیر gap → mitigated
    assert smc.fvgs(df, lookback=5, min_gap_ratio=0.0)["BULLISH"] is None


def _zigzag(points, seg=5):
    path = []
    for a, b in zip(points[:-1], points[1:]):
        path += list(np.linspace(a, b, seg, endpoint=False))
    path.append(points[-1])
    c = np.array(path)
    return pd.DataFrame({"open": c, "high": c + 0.2, "low": c - 0.2, "close": c})


def test_choch_vs_bos_classification():
    # LH+LL (ساختار نزولی) و بعد شکست سقف آخر → CHOCH صعودی
    r = smc.structure_break(_zigzag([12, 20, 10, 17, 8, 15, 17.5]), lookback=60)
    assert r and r["dir"] == "BULL" and r["kind"] == "CHOCH"
    # HH+HL (ساختار صعودی) و بعد شکست سقف آخر → BOS
    r = smc.structure_break(_zigzag([10, 14, 11, 17, 13, 16, 17.5]), lookback=60)
    assert r and r["dir"] == "BULL" and r["kind"] == "BOS"



def test_structure_break_does_not_repeat_after_level_was_already_broken():
    base = _zigzag([10, 14, 11, 17, 13, 16, 16.9])
    close = base["close"].iloc[-1]
    extra = pd.DataFrame({
        "open": [close, 17.3],
        "high": [17.5, 17.6],
        "low": [17.0, 17.1],
        "close": [17.3, 17.4],
    })
    stale = pd.concat([base, extra], ignore_index=True)
    assert smc.structure_break(stale, lookback=60, fresh_bars=3) is None


def test_liquidity_sweep_returns_latest_event_in_recent_window():
    df = pd.DataFrame({
        "open":  [5.0, 5.0, 5.0, 2.0, 5.0, 9.5],
        "high":  [10.0, 9.0, 8.0, 9.0, 9.0, 11.0],
        "low":   [1.0, 2.0, 3.0, 0.5, 2.0, 8.0],
        "close": [5.0, 5.0, 5.0, 2.0, 5.0, 9.0],
    })
    sweep = smc.liquidity_sweep(df, lookback=3, recent=3)
    assert sweep is not None
    assert sweep["type"] == "BEARISH_SWEEP"

def test_view_never_shows_forming_candles():
    data = build_topdown(make_15m(60))
    view = TopDownView(data)
    now = data["15m"]["time"].iloc[2000] + pd.Timedelta(minutes=15)
    out = view.at(now)
    from engine.data.providers import _bar_close_times
    for tf, df in out.items():
        assert _bar_close_times(df, tf).max() <= now


def test_no_lookahead_decision_identical_on_truncated_data():
    cfg = EngineConfig(min_score=0.3)
    full = make_15m(300)
    cut_idx = 25000
    now = full["time"].iloc[cut_idx] + pd.Timedelta(minutes=15)
    d_full = analyze(TopDownView(build_topdown(full)).at(now), cfg, now)
    d_cut = analyze(TopDownView(build_topdown(full.iloc[:cut_idx + 1])).at(now), cfg, now)
    assert (d_full.direction, d_full.score, d_full.sl, d_full.tp) == \
           (d_cut.direction, d_cut.score, d_cut.sl, d_cut.tp)


def test_backtest_runs_and_invariants():
    cfg = EngineConfig(min_score=0.35)
    data = build_topdown(make_15m(300, seed=3))
    trades = run_backtest("EURUSD", data, cfg)
    s = summarize(trades)
    assert s["n"] >= 1
    for t in trades:
        assert t["close_time"] >= t["open_time"]
        side = 1 if t["side"] == "BUY" else -1
        assert (t["entry"] - t["sl"]) * side > 0
        if t["status"] == "SL":
            assert abs(t["r"] + 1) < 1e-3
    assert s["max_drawdown_r"] >= 0



def test_backtest_enters_next_bar_open_and_includes_final_bar_close():
    data = build_topdown(make_15m(days=5, seed=17))
    df15 = data["15m"]
    signal_entry = float(df15["close"].iloc[0])
    cfg = EngineConfig(
        cooldown_hours=10000, max_hold_hours=10000, max_entry_slip_r=0.3
    )
    state = {"sent": False}

    def first_only(view, config, now):
        if state["sent"]:
            return Decision()
        state["sent"] = True
        return Decision(
            direction="BUY", score=0.8, entry=signal_entry,
            sl=signal_entry - 0.05, tp=signal_entry + 0.1,
        )

    trades = run_backtest("EURUSD", data, cfg, analyze_fn=first_only)
    assert len(trades) == 1
    trade = trades[0]
    assert trade["open_time"] == df15["time"].iloc[1]
    assert math.isclose(
        trade["entry"], float(df15["open"].iloc[1]) + spread_price("EURUSD") / 2,
        abs_tol=1e-9,
    )
    assert trade["status"] == "EOD"
    assert trade["close_time"] == df15["time"].iloc[-1] + pd.Timedelta(minutes=15)


def test_backtest_rejects_entry_after_excessive_open_gap():
    data = build_topdown(make_15m(days=2, seed=19))
    df15 = data["15m"]
    signal_entry = float(df15["close"].iloc[0])
    df15.loc[1, "open"] = signal_entry + 0.02
    cfg = EngineConfig(
        cooldown_hours=10000, max_hold_hours=10000, max_entry_slip_r=0.3
    )
    state = {"sent": False}

    def first_only(view, config, now):
        if state["sent"]:
            return Decision()
        state["sent"] = True
        return Decision(
            direction="BUY", score=0.8, entry=signal_entry,
            sl=signal_entry - 0.01, tp=signal_entry + 0.02,
        )

    assert run_backtest("EURUSD", data, cfg, analyze_fn=first_only) == []


def test_engine_config_rejects_invalid_risk_and_nonfinite_values():
    with pytest.raises(ValueError, match="risk_reward"):
        EngineConfig(risk_reward=0)
    with pytest.raises(ValueError, match="max_entry_slip_r"):
        EngineConfig(max_entry_slip_r=-0.1)
    with pytest.raises(ValueError, match="متناهی"):
        EngineConfig(min_score=float("nan"))


def test_walk_forward_does_not_use_outcome_closed_after_train_boundary(monkeypatch):
    from engine.backtest import report

    base_time = pd.Timestamp("2020-01-01T00:00:00Z")
    data = {
        "15m": pd.DataFrame({
            "time": pd.date_range(base_time, periods=100 * 96, freq="15min")
        })
    }

    def trade(open_day, close_day, r):
        return {
            "symbol": "EURUSD",
            "open_time": base_time + pd.Timedelta(days=open_day),
            "close_time": base_time + pd.Timedelta(days=close_day),
            "r": r, "status": "TP",
        }

    by_threshold = {
        0.5: [trade(15, 20, 0.1), trade(30, 45, 100.0), trade(42, 48, 1.0)],
        0.6: [trade(15, 20, 0.5), trade(42, 48, 0.3)],
    }
    monkeypatch.setattr(
        report, "run_backtest",
        lambda symbol, dataset, cfg: by_threshold[cfg.min_score],
    )
    result = report.walk_forward(
        "EURUSD", data, EngineConfig(), grid=(0.5, 0.6),
        train_days=30, test_days=10, warmup_days=10, min_train_trades=1,
    )
    assert result["windows"]
    assert result["windows"][0]["chosen_min_score"] == 0.6
    assert result["windows"][0]["test_exp"] == 0.3

def test_stats_helpers():
    lo, hi = wilson(13, 20)
    assert 0.40 < lo < 0.45 and 0.80 < hi < 0.88
    s = summarize([{"r": 2, "close_time": pd.Timestamp(i, unit="D"), "status": "TP",
                    "symbol": "X"} for i in range(5)])
    assert s["profit_factor"] == 99.0 and s["max_drawdown_r"] == 0
    assert evaluate_gate(s)["ready"] is False                          # n<100


def test_report_tools_run():
    from engine.backtest.report import feature_table, fold_stability, random_baseline
    cfg = EngineConfig(min_score=0.35)
    data = build_topdown(make_15m(300, seed=3))
    trades = run_backtest("EURUSD", data, cfg)
    b = random_baseline("EURUSD", data["15m"], trades, cfg, n_sims=20)
    assert "p_value" in b and 0 < b["p_value"] <= 1
    assert not feature_table(trades).empty
    assert isinstance(fold_stability(trades, 2), list)


def test_backtest_end_bounds_eod_close():
    """معامله‌ی باز در --end روی آخرین کندل بازه بسته می‌شود، نه آخر کل دیتا."""
    cfg = EngineConfig(min_score=0.35)
    data = build_topdown(make_15m(300, seed=3))
    df15 = data["15m"]
    bar = pd.Timedelta(minutes=15)
    all_trades = run_backtest("EURUSD", data, cfg)
    assert len(all_trades) >= 1
    end_ts = pd.Timestamp(all_trades[0]["open_time"]) + pd.Timedelta(hours=2)
    trades = run_backtest("EURUSD", data, cfg, end=end_ts.isoformat())
    assert len(trades) >= 1
    for t in trades:
        assert pd.Timestamp(t["close_time"]) <= end_ts
        assert pd.Timestamp(t["open_time"]) <= end_ts
        assert pd.Timestamp(t["close_time"]) >= pd.Timestamp(t["open_time"])
    eod = [t for t in trades if t["status"] == "EOD"]
    assert len(eod) >= 1
    last = df15[df15["time"] + bar <= end_ts].iloc[-1]
    half = spread_price("EURUSD") / 2.0
    for t in eod:
        side = 1 if t["side"] == "BUY" else -1
        assert pd.Timestamp(t["close_time"]) == last["time"] + bar
        assert abs(float(t["exit"]) - (float(last["close"]) - side * half)) < 1e-6


def _flat_15m(start, days):
    n = days * 96
    t = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    p = np.linspace(1.0, 1.1, n)
    return pd.DataFrame({"time": t, "open": p, "high": p + 0.001, "low": p - 0.001,
                         "close": p, "volume": 1.0})


def test_daily_bar_starts_at_ny_1700_winter_and_summer():
    # زمستان: 17:00 نیویورک = 22:00 UTC ؛ تابستان: 21:00 UTC
    win = resample_ohlc(_flat_15m("2025-01-05 22:00", 20), "1d")
    assert (win["time"].dt.hour == 22).all()
    summ = resample_ohlc(_flat_15m("2025-07-06 21:00", 20), "1d")
    assert (summ["time"].dt.hour == 21).all()
    wk = resample_ohlc(_flat_15m("2025-07-06 21:00", 20), "1wk")
    assert (wk["time"].dt.hour == 21).all() and (wk["time"].dt.dayofweek == 6).all()


def test_daily_bar_across_dst_switch_keeps_17_ny():
    # عبور از شروع DST آمریکا (۹ مارس ۲۰۲۵): قبل ۲۲ UTC، بعد ۲۱ UTC و هیچ کندل یتیمی نیست
    d = resample_ohlc(_flat_15m("2025-03-02 22:00", 14), "1d")
    hrs = d.set_index("time").index.tz_convert("America/New_York").hour
    assert (hrs == 17).all()
    assert set(d["time"].dt.hour) == {22, 21}


def test_topdown_daily_close_respects_dst_transition():
    # Spring forward: this NY daily candle is 23 hours long, not 24.
    spring = pd.DataFrame({
        "time": pd.to_datetime(["2025-03-08 22:00:00+00:00"]),
    })
    spring_view = TopDownView({"1d": spring})
    assert len(spring_view.at(pd.Timestamp("2025-03-09 21:30:00", tz="UTC"))["1d"]) == 1

    # Fall back: this NY daily candle is 25 hours long; it must not be exposed early.
    fall = pd.DataFrame({
        "time": pd.to_datetime(["2025-11-01 21:00:00+00:00"]),
    })
    fall_view = TopDownView({"1d": fall})
    assert len(fall_view.at(pd.Timestamp("2025-11-02 21:30:00", tz="UTC"))["1d"]) == 0
    assert len(fall_view.at(pd.Timestamp("2025-11-02 22:30:00", tz="UTC"))["1d"]) == 1


def test_kill_zone_local_london_and_ny_with_dst():
    ts = lambda s: pd.Timestamp(s, tz="UTC")
    # زمستان: لندن 07-10 UTC، نیویورک 12-15 UTC
    assert kill_zone(ts("2025-01-15 07:00")) and kill_zone(ts("2025-01-15 12:00"))
    assert not kill_zone(ts("2025-01-15 10:00")) and not kill_zone(ts("2025-01-15 11:00"))
    # تابستان: لندن 06-09 UTC، نیویورک 11-14 UTC
    assert kill_zone(ts("2025-07-15 06:00")) and kill_zone(ts("2025-07-15 11:00"))
    assert not kill_zone(ts("2025-07-15 09:00")) and not kill_zone(ts("2025-07-15 14:00"))


def test_mt5_csv_broker_timezone_tracks_dst():
    # Helsinki server time changes UTC offset across the spring DST boundary.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bars.csv"
        path.write_text(
            "date,time,open,high,low,close,volume\n"
            "2025-03-28,12:00:00,1.0,1.1,0.9,1.05,10\n"
            "2025-04-01,12:00:00,1.0,1.1,0.9,1.05,10\n",
            encoding="utf-8",
        )
        df = load_mt5_csv(str(path), broker_timezone="Europe/Helsinki")
        assert df["time"].dt.hour.tolist() == [10, 9]
        assert str(df["time"].dt.tz) == "UTC"


def test_mt5_csv_aware_timestamps_are_not_shifted_twice():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "aware.csv"
        path.write_text(
            "time,open,high,low,close,volume\n"
            "2025-07-01T12:00:00+03:00,1.0,1.1,0.9,1.05,10\n",
            encoding="utf-8",
        )
        df = load_mt5_csv(str(path))
        assert df["time"].iloc[0] == pd.Timestamp("2025-07-01 09:00:00", tz="UTC")

        try:
            load_mt5_csv(str(path), tz_offset_hours=3)
        except ValueError as exc:
            assert "timezone" in str(exc)
        else:
            raise AssertionError("Expected a guard against applying a second offset")



def test_mt5_csv_with_multiple_aware_offsets_normalizes_each_row_to_utc():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dst-aware.csv"
        path.write_text(
            "time,open,high,low,close,volume\n"
            "2025-03-30T01:30:00+01:00,1.0,1.1,0.9,1.05,10\n"
            "2025-03-30T03:30:00+02:00,1.0,1.1,0.9,1.05,10\n",
            encoding="utf-8",
        )
        df = load_mt5_csv(str(path))
        assert df["time"].tolist() == [
            pd.Timestamp("2025-03-30T00:30:00Z"),
            pd.Timestamp("2025-03-30T01:30:00Z"),
        ]


def test_mt5_csv_rejects_mixed_aware_and_naive_timestamps():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mixed-time.csv"
        path.write_text(
            "time,open,high,low,close,volume\n"
            "2025-03-30T01:30:00+01:00,1.0,1.1,0.9,1.05,10\n"
            "2025-03-30T03:30:00,1.0,1.1,0.9,1.05,10\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="aware"):
            load_mt5_csv(str(path))

def test_gold_futures_proxy_is_labeled_in_reports():
    from argparse import Namespace

    label = _data_source_label(Namespace(source="yfinance"), "XAUUSD")
    assert "GC=F" in label
    assert "not spot XAUUSD" in label


def test_native_yfinance_daily_bar_keeps_fixed_utc_boundary():
    # Native Yahoo bars at 00:00 UTC must not be reinterpreted as 17:00 New York.
    native = pd.DataFrame({
        "time": pd.to_datetime(["2025-03-09T00:00:00Z"]),
    })
    closes = _bar_close_times(native, "1d")
    assert closes.iloc[0] == pd.Timestamp("2025-03-10T00:00:00Z")

    view = TopDownView({"1d": native})
    assert len(view.at(pd.Timestamp("2025-03-09T23:30:00Z"))["1d"]) == 0
    assert len(view.at(pd.Timestamp("2025-03-10T00:30:00Z"))["1d"]) == 1

def test_dukascopy_source_label_distinguishes_spot_history_from_futures_proxy():
    from argparse import Namespace
    label = _data_source_label(Namespace(source="dukascopy"), "XAUUSD")
    assert "Dukascopy BID/ASK-derived midpoint" in label
    assert "GC=F" not in label


def test_dukascopy_bid_ask_bars_are_midpoint_approximated():
    idx = pd.date_range("2025-01-01", periods=2, freq="15min", tz="UTC")
    bid = pd.DataFrame({
        "open": [100.0, 101.0], "high": [102.0, 103.0],
        "low": [99.0, 100.0], "close": [101.0, 102.0], "volume": [10.0, 12.0],
    }, index=idx)
    ask = pd.DataFrame({
        "open": [100.2, 101.2], "high": [102.2, 103.2],
        "low": [99.2, 100.2], "close": [101.2, 102.2], "volume": [14.0, 16.0],
    }, index=idx)
    mid = _midpoint_ohlc(bid, ask)
    assert mid["open"].tolist() == [100.1, 101.1]
    assert mid["high"].tolist() == [102.1, 103.1]
    assert mid["low"].tolist() == [99.1, 100.1]
    assert mid["close"].tolist() == [101.1, 102.1]
    assert mid["volume"].tolist() == [12.0, 14.0]


def test_dukascopy_bid_ask_midpoint_rejects_unaligned_bars():
    idx_bid = pd.date_range("2025-01-01", periods=1, freq="15min", tz="UTC")
    idx_ask = pd.date_range("2025-01-01T00:15:00Z", periods=1, freq="15min", tz="UTC")
    row = {"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [1.0]}
    with pytest.raises(ValueError, match="no common timestamps"):
        _midpoint_ohlc(pd.DataFrame(row, index=idx_bid), pd.DataFrame(row, index=idx_ask))


def test_dukascopy_fetch_sets_timeout_and_restores_request_handler():
    from types import SimpleNamespace

    calls = {}
    def fake_get(url, **kwargs):
        calls["timeout"] = kwargs.get("timeout")
        return object()

    class FakeDukascopy:
        INTERVAL_MIN_15 = "15MIN"
        requests = SimpleNamespace(get=fake_get)

        @staticmethod
        def fetch(instrument, interval, offer_side, start, end, max_retries):
            calls["args"] = (instrument, interval, offer_side, max_retries)
            FakeDukascopy.requests.get("https://example.invalid")
            return "bars"

    original_get = FakeDukascopy.requests.get
    result = _fetch_side(FakeDukascopy, "XAU/USD", "B", "start", "end")
    assert result == "bars"
    assert calls["timeout"] == (5, 20)
    assert calls["args"] == ("XAU/USD", "15MIN", "B", 1)
    assert FakeDukascopy.requests.get is original_get

