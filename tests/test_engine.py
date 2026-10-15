import numpy as np
import pandas as pd
import pytest

from engine.backtest.runner import check_exit, evaluate_gate, run_backtest, summarize, wilson
from engine.core import smc
from engine.core.config import EngineConfig, spread_price
from engine.data.providers import TopDownView, build_topdown, resample_ohlc, candle_close_times
from engine.strategy.smc_topdown import analyze


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


def test_check_exit_models_adverse_gap_through_stop():
    assert check_exit(1, 99, 102, 103, 94, 0.0, open_price=95) == ("SL", 95)
    assert check_exit(-1, 101, 98, 106, 97, 0.0, open_price=105) == ("SL", 105)


def test_check_exit_does_not_assume_price_improvement_on_gap_through_target():
    assert check_exit(1, 99, 102, 104, 101, 0.0, open_price=103) == ("TP", 102)
    assert check_exit(-1, 101, 98, 99, 96, 0.0, open_price=97) == ("TP", 98)


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
    r = smc.structure_break(_zigzag([12, 20, 10, 17, 8, 15, 18]), lookback=60)
    assert r and r["dir"] == "BULL" and r["kind"] == "CHOCH"
    # HH+HL (ساختار صعودی) و بعد شکست سقف آخر → BOS
    r = smc.structure_break(_zigzag([10, 14, 11, 17, 13, 16, 19]), lookback=60)
    assert r and r["dir"] == "BULL" and r["kind"] == "BOS"


def test_view_never_shows_forming_candles():
    data = build_topdown(make_15m(60))
    view = TopDownView(data)
    now = data["15m"]["time"].iloc[2000] + pd.Timedelta(minutes=15)
    out = view.at(now)
    from engine.data.providers import candle_close_times
    for tf, df in out.items():
        assert candle_close_times(df, tf).max() <= now


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


def test_backtest_accepts_custom_analyzer():
    """CLI-selected strategies can be injected without changing the SMC default."""
    from engine.strategy.smc_topdown import Decision

    cfg = EngineConfig()
    data = build_topdown(make_15m(days=3, seed=19))
    calls = []

    def custom_analyzer(view, config, now):
        calls.append(now)
        return Decision(config_hash=config.hash())

    first = data["15m"]["time"].iloc[0]
    trades = run_backtest(
        "EURUSD", data, cfg,
        start=(first + pd.Timedelta(minutes=15)).isoformat(),
        end=(first + pd.Timedelta(hours=2)).isoformat(),
        analyze_fn=custom_analyzer,
    )
    assert calls, "custom analyzer should be invoked with timezone-aware bounds"
    assert trades == []


def test_daily_and_weekly_close_times_respect_new_york_dst():
    # New York falls back on 2025-11-02: the Sunday 17:00 NY boundary is 22:00 UTC.
    starts = pd.Series(pd.to_datetime([
        "2025-11-01T21:00:00Z",  # Saturday 17:00 EDT
        "2025-11-02T22:00:00Z",  # Sunday 17:00 EST
    ], utc=True))
    frame = pd.DataFrame({"time": starts, "open": [1.0, 1.0], "high": [1.1, 1.1],
                          "low": [0.9, 0.9], "close": [1.0, 1.0], "volume": [1, 1]})
    daily_close = candle_close_times(frame.iloc[:1], "1d").iloc[0].tz_convert("UTC")
    weekly_frame = frame.iloc[:1].copy()
    weekly_frame["time"] = pd.to_datetime(["2025-10-26T21:00:00Z"], utc=True)
    weekly_close = candle_close_times(weekly_frame, "1wk").iloc[0].tz_convert("UTC")
    assert daily_close == pd.Timestamp("2025-11-02T22:00:00Z")
    assert weekly_close == pd.Timestamp("2025-11-02T22:00:00Z")

    daily_view = TopDownView({"1d": frame})
    weekly_view = TopDownView({"1wk": weekly_frame})
    before_boundary = pd.Timestamp("2025-11-02T21:30:00Z")
    assert daily_view.at(before_boundary)["1d"].empty
    assert weekly_view.at(before_boundary)["1wk"].empty
    assert len(daily_view.at(daily_close)["1d"]) == 1



def test_walk_forward_fits_train_then_runs_selected_config_on_fresh_test(monkeypatch):
    from engine.backtest import report

    first = pd.Timestamp("2025-01-01T00:00:00Z")
    data = {"15m": pd.DataFrame({"time": pd.date_range(first, periods=4 * 96, freq="15min")})}
    calls = []

    def fake_run_backtest(symbol, data, cfg, start=None, end=None, analyze_fn=None):
        assert start is not None and end is not None, "each fold must use explicit bounds"
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        calls.append((cfg.min_score, start_ts, end_ts))
        if start_ts == first.tz_localize(None) + pd.Timedelta(days=1):
            r = 0.1 if cfg.min_score == 0.5 else 0.5
        else:
            r = 0.25 if cfg.min_score == 0.5 else -1.0
        return [{"symbol": symbol, "side": "BUY", "open_time": start_ts,
                 "close_time": start_ts + pd.Timedelta(minutes=15),
                 "r": r, "status": "TP", "features": {}}]

    monkeypatch.setattr(report, "run_backtest", fake_run_backtest)
    result = report.walk_forward(
        "EURUSD", data, EngineConfig(), grid=(0.5, 0.6),
        train_days=1, test_days=1, warmup_days=1, min_train_trades=1,
    )

    assert len(calls) == 3
    assert calls[0][1] == first.tz_localize(None) + pd.Timedelta(days=1)
    assert calls[1][1] == first.tz_localize(None) + pd.Timedelta(days=1)
    assert calls[2][0] == 0.6
    assert calls[2][1] == first.tz_localize(None) + pd.Timedelta(days=2)
    assert calls[2][2] == first.tz_localize(None) + pd.Timedelta(days=3) - pd.Timedelta(nanoseconds=1)
    assert result["windows"][0]["chosen_min_score"] == 0.6
    assert result["out_of_sample"]["expectancy_r"] == -1.0



def test_yfinance_topdown_resamples_daily_weekly_to_new_york_session(monkeypatch):
    from engine.data import providers

    start = pd.Timestamp("2025-10-20T21:00:00Z")
    hourly_times = pd.date_range(start, periods=31 * 24, freq="1h")
    hourly = pd.DataFrame({
        "time": hourly_times,
        "open": np.ones(len(hourly_times)),
        "high": np.ones(len(hourly_times)) * 1.1,
        "low": np.ones(len(hourly_times)) * 0.9,
        "close": np.ones(len(hourly_times)),
        "volume": np.ones(len(hourly_times)),
    })
    calls = []

    def fake_load(symbol, tf):
        calls.append(tf)
        if tf == "15m":
            return hourly.iloc[:100].copy()
        if tf == "1h":
            return hourly.copy()
        raise AssertionError(f"native {tf} bars must not be mixed into the NY-session topdown view")

    monkeypatch.setattr(providers, "load_yfinance", fake_load)
    data = providers.load_yfinance_topdown("EURUSD=X")

    assert calls == ["15m", "1h"]
    for tf in ("1d", "1wk"):
        local = data[tf]["time"].dt.tz_convert("America/New_York")
        assert (local.dt.hour == 17).all()
        assert (local.dt.minute == 0).all()
    assert (data["1wk"]["time"].dt.dayofweek == 6).all()
    assert set(data["1d"]["time"].dt.hour.unique()) == {21, 22}



def test_yfinance_gold_proxy_requires_explicit_opt_in(monkeypatch):
    from types import SimpleNamespace
    from engine import cli

    args = SimpleNamespace(source="yfinance", allow_gold_futures_proxy=False)
    try:
        cli._load(args, "XAUUSD")
        assert False, "XAUUSD must not silently use GC=F futures as spot"
    except SystemExit as exc:
        assert "GC=F futures, not XAUUSD spot" in str(exc)

    args.allow_gold_futures_proxy = True
    monkeypatch.setattr(cli, "load_yfinance_topdown", lambda symbol: {"loaded": symbol})
    assert cli._load(args, "XAUUSD") == {"loaded": "GC=F"}



def test_atr_uses_wilder_smoothing_not_only_last_period_mean():
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=5, freq="h", tz="UTC"),
        "open": [10.0] * 5,
        "high": [11.0, 11.5, 12.0, 12.5, 13.0],
        "low": [9.0, 8.5, 8.0, 7.5, 7.0],
        "close": [10.0] * 5,
    })

    # True ranges after the initial close are [3, 4, 5, 6]. Seed=(3+4+5)/3, then Wilder update with 6.
    expected = ((3 - 1) * 4.0 + 6.0) / 3.0
    assert smc.atr(df, period=3) == pytest.approx(expected)



def test_random_baseline_simulation_preserves_spread_adjusted_rr():
    from engine.backtest.report import _simulate

    result_r = _simulate(
        side=1, mid=1.1000, risk_dist=0.0100, rr=2.0,
        hi=np.array([1.1230]), lo=np.array([1.1100]),
        cl=np.array([1.1200]), half=0.0010, n=1,
    )

    assert result_r == pytest.approx(2.0)



def test_random_baseline_models_gap_through_stop_like_runner():
    from engine.backtest.report import _simulate

    result_r = _simulate(
        side=1, mid=100.0, risk_dist=1.0, rr=2.0,
        hi=np.array([101.0]), lo=np.array([94.0]),
        cl=np.array([95.0]), half=0.0, n=1,
        op=np.array([95.0]),
    )

    assert result_r == pytest.approx(-5.0)
