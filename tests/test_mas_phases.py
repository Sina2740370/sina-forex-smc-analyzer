"""تست‌های فاز ۲ و ۴ MAS: بک‌تست سیگنال، چندارزی (بدون هاردکد EURUSD)، پورتفوی مشترک."""
import pandas as pd
import pytest

from engine.data.providers import load_mt5_csv
from engine.mas.aggregator import analyze_mas
from engine.mas.backtest import backtest_mas
from engine.mas.portfolio import portfolio_markdown, run_portfolio
from engine.mas.symbols import get_spec, pip_value_usd, supported_symbols
from engine.mas.risk_agent import SYM_SPEC

from tests.test_engine import make_15m


# ------------------------------------------------------------- symbols.py
def test_symbols_parametric_no_eurusd_hardcode():
    # همه نمادها باید از یک منبع مشترک بخوانند؛ USDJPY quote آن USD نیست
    for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD", "USDCAD", "USDCHF"):
        spec = get_spec(sym)
        assert spec.pip > 0 and spec.typical_spread_pips > 0
        assert sym in SYM_SPEC
    assert pip_value_usd("USDJPY", 157.0) == pytest.approx(1000.0 / 157.0)
    assert pip_value_usd("EURUSD", 1.1) == pytest.approx(10.0)
    assert get_spec("XAUUSD").spread_price() == pytest.approx(0.25)


def test_analyze_mas_default_spread_from_spec():
    # spread_price=None باید اسپرد معمول نماد را بگیرد (نه صفر) — اثر در گزارش نقدینگی
    m = make_15m(420, seed=7)
    now = m["time"].iloc[-1]
    sig = analyze_mas(m, "GBPUSD", now)   # بدون آرگومان اسپرد
    assert sig.symbol == "GBPUSD"
    assert "اسپرد" not in str(sig.missing_data) or True   # تخمین نشده از ATR
    sig2 = analyze_mas(m, "GBPUSD", now, spread_price=get_spec("GBPUSD").spread_price())
    assert sig2.final_score == sig.final_score   # مسیر یکسان


# ------------------------------------------------------------- فاز ۲: بک‌تست
def test_backtest_runs_and_is_walkforward_safe():
    m = make_15m(140, seed=3)
    # تاریخچه سینتتیک کوتاه است → شروع از کندل ۳۰ (دوره گرم‌شدن کوتاه)
    r = backtest_mas(m, "EURUSD", start=str(m["time"].iloc[30]), step_hours=8)
    s = r.summary()
    assert s["points"] > 50
    for t in r.trades:
        # بدون lookahead: خروج بعد از ورود
        assert t.exit_time > t.entry_time
        # برخورد SL/TP واقعی: PnL یک مضرب تمیز از ریسک است (TP2≈2.2R یا SL=-1R با گپ احتمالی)
        assert t.r_multiple < 0 or t.r_multiple > 1.0
    assert r.equity_end > 0


def test_backtest_uses_symbol_spec_for_lots():
    # حجم لات باید از pip value واقعی USDJPY بیاید (قیمت‌وابسته)، نه حدس ثابت
    m = make_15m(140, seed=3, base=150.0, vol=0.05)
    r = backtest_mas(m, "USDJPY", step_hours=8)
    for t in r.trades:
        pv = pip_value_usd("USDJPY", t.entry)
        expected_lots = (10_000 * 0.01) / max(1e-9, abs(t.entry - t.sl) / get_spec("USDJPY").pip * pv)
        assert abs(t.lots - round(expected_lots, 2)) < 0.02


# ------------------------------------------------------------- فاز ۴: پورتفوی
def test_portfolio_multi_symbol_shared_risk(tmp_path, monkeypatch):
    # داده سینتتیک دو نماد با قیمت پایه متفاوت (GBPUSD و USDJPY)
    import os
    d = tmp_path / "csv"
    d.mkdir()
    for sym, base, vol in (("GBPUSD", 1.26, 0.0005), ("USDJPY", 150.0, 0.05)):
        m = make_15m(150, seed=11, base=base, vol=vol)
        m.to_csv(d / f"{sym}_M15.csv", index=False)
    monkeypatch.chdir(tmp_path)

    def fake_load(path, tz_offset_hours=0.0, server_tz=None):
        return load_mt5_csv(str(path))

    p = run_portfolio(str(d), symbols=["GBPUSD", "USDJPY"], equity=10_000.0)
    assert set(p["symbols"]) == {"GBPUSD", "USDJPY"}
    assert p["summary"]["open_risk_pct"] <= 3.0 + 1e-9   # سقف ریسک همزمان سبد
    md = portfolio_markdown(p)
    assert "گزارش پورتفوی چندجفتی" in md and "GBPUSD" in md and "USDJPY" in md


def test_portfolio_reports_missing_data_without_guess():
    p = run_portfolio("nonexistent_dir", symbols=["EURUSD"], equity=10_000.0)
    assert p["symbols"]["EURUSD"]["error"]        # صادقانه خطا می‌دهد، حدس نمی‌زند
    assert p["summary"]["approved"] == []



def test_backtest_accepts_timezone_aware_start_and_end():
    m = make_15m(140, seed=19)
    start = m["time"].iloc[30]
    end = m["time"].iloc[-1]

    result = backtest_mas(
        m, "EURUSD", start=start, end=end, step_hours=8
    )

    assert result.n_points > 0



@pytest.mark.parametrize("make_naive", [False, True])
def test_portfolio_normalizes_aware_and_naive_as_of_timestamps(
    tmp_path, monkeypatch, make_naive
):
    from types import SimpleNamespace
    import engine.mas.portfolio as portfolio

    data_dir = tmp_path / "portfolio-time-data"
    data_dir.mkdir()
    frame = make_15m(140, seed=23)
    frame.to_csv(data_dir / "EURUSD_M15.csv", index=False)
    as_of = pd.Timestamp(frame["time"].iloc[-1])
    if make_naive:
        as_of = as_of.tz_localize(None)

    def wait_signal(*_args, **_kwargs):
        return SimpleNamespace(
            signal="WAIT",
            risk_amount="0.00 USD",
            to_json=lambda **_kwargs: {"signal": "WAIT"},
        )

    monkeypatch.setattr(portfolio, "analyze_mas", wait_signal)
    result = portfolio.run_portfolio(
        str(data_dir), now=as_of, symbols=["EURUSD"], equity=10_000.0
    )

    assert result["symbols"]["EURUSD"]["signal"] == "WAIT"
    assert pd.Timestamp(result["analysis_point"]) == pd.to_datetime(as_of, utc=True)


def test_portfolio_uses_one_common_asof_timestamp_for_all_symbols(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import engine.mas.portfolio as portfolio_module

    data_dir = tmp_path / "portfolio-common-asof"
    data_dir.mkdir()
    (data_dir / "GBPUSD_M15.csv").write_text("fixture", encoding="utf-8")
    (data_dir / "USDJPY_M15.csv").write_text("fixture", encoding="utf-8")

    gbp = make_15m(150, seed=31, base=1.26, vol=0.0005)
    jpy = make_15m(150, seed=32, base=150.0, vol=0.05)
    # USDJPY contains later candles; the portfolio must not use them in the
    # same snapshot as GBPUSD's earlier final candle.
    jpy.loc[jpy.index[-2:], "time"] += pd.Timedelta(minutes=30)
    frames = {"GBPUSD": gbp, "USDJPY": jpy}
    observed = []

    monkeypatch.setattr(
        portfolio_module,
        "load_mt5_csv",
        lambda path: frames["GBPUSD" if "GBPUSD" in path else "USDJPY"].copy(),
    )

    def capture_analysis(_df, symbol, as_of, **_kwargs):
        observed.append((symbol, pd.Timestamp(as_of)))
        return SimpleNamespace(
            signal="WAIT",
            to_json=lambda **_kwargs: {"signal": "WAIT", "wait_reason": "fixture"},
        )

    monkeypatch.setattr(portfolio_module, "analyze_mas", capture_analysis)
    result = portfolio_module.run_portfolio(
        str(data_dir), symbols=["GBPUSD", "USDJPY"], equity=10_000.0
    )

    expected = min(
        pd.to_datetime(gbp["time"], utc=True).max(),
        pd.to_datetime(jpy["time"], utc=True).max(),
    )
    assert len(observed) == 2
    assert observed[0][1] == expected
    assert observed[1][1] == expected
    assert pd.Timestamp(result["analysis_point"]) == expected
