import numpy as np
import pandas as pd

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
    from engine.data.providers import TF_MINUTES
    for tf, df in out.items():
        assert (df["time"] + pd.Timedelta(minutes=TF_MINUTES[tf])).max() <= now


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

    trades = run_backtest("EURUSD", data, cfg, analyze_fn=custom_analyzer)
    assert calls, "custom analyzer should be invoked by the backtester"
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
    weekly_close = candle_close_times(frame.iloc[:1], "1wk").iloc[0].tz_convert("UTC")
    assert daily_close == pd.Timestamp("2025-11-02T22:00:00Z")
    assert weekly_close == pd.Timestamp("2025-11-08T21:00:00Z")

    daily_view = TopDownView({"1d": frame})
    weekly_view = TopDownView({"1wk": frame.iloc[:1]})
    before_boundary = pd.Timestamp("2025-11-02T21:30:00Z")
    assert daily_view.at(before_boundary)["1d"].empty
    assert weekly_view.at(before_boundary)["1wk"].empty
    assert len(daily_view.at(daily_close)["1d"]) == 1
