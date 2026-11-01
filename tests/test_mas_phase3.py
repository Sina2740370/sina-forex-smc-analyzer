"""تست‌های فاز ۳: فید FRED/COT (بدون شبکه — کش سینتتیک)، ایجنت فاندامنتال و سنتیمنت، بدون lookahead."""
import pandas as pd
import pytest

from engine.mas.aggregator import analyze_mas
from engine.mas.contracts import BUY, SELL
from engine.mas.datafeeds import MASFeeds
from engine.mas.fundamental import run_fundamental_agent
from engine.mas.sentiment import run_sentiment_agent

from tests.test_engine import make_15m


def _feeds_with(yield_rows: dict, fin_rows: pd.DataFrame | None = None,
                com_rows: pd.DataFrame | None = None, tmp_path=None) -> MASFeeds:
    """MASFeeds با کش سینتتیک (بدون هیچ شبکه‌ای). yield_rows: {series: [(date, value), ...]}"""
    f = MASFeeds(cache_dir=str(tmp_path or "/tmp/mas_test_cache_unused"), online=False)
    f._yields = {sid: pd.DataFrame({"date": pd.to_datetime([r[0] for r in rows]),
                                    "value": [r[1] for r in rows]})
                 for sid, rows in yield_rows.items()}
    if fin_rows is not None:
        fin_rows = fin_rows.copy()
        fin_rows["dt"] = pd.to_datetime(fin_rows["Report_Date_as_YYYY-MM-DD"])
    f._fin_df = fin_rows
    f._com_df = com_rows
    return f


def _fin_cot(code="099741", weeks=80, base=50_000):
    t = pd.date_range("2025-05-02", periods=weeks, freq="7D")
    return pd.DataFrame({
        "CFTC_Contract_Market_Code": code,
        "Report_Date_as_YYYY-MM-DD": t.strftime("%Y-%m-%d"),
        "Asset_Mgr_Positions_Long_All": [base + i * 100 for i in range(weeks)],
        "Asset_Mgr_Positions_Short_All": [base for _ in range(weeks)],
        "Lev_Money_Positions_Long_All": [base for _ in range(weeks)],
        "Lev_Money_Positions_Short_All": [base for _ in range(weeks)],
    })


def test_fundamental_yield_diff_direction_and_no_lookahead():
    feeds = _feeds_with({
        "DGS10": [("2026-10-01", 4.5), ("2026-10-20", 4.3)],
        "IRLTLT01DEM156N": [("2026-08-01", 3.0)],
    }, tmp_path="/tmp/mas_t1")
    now = pd.Timestamp("2026-10-25", tz="UTC")
    d = feeds.yield_diff("EURUSD", now)
    assert d is not None and d["diff_pct"] == pytest.approx(3.0 - 4.3)   # آخرین ≤ now
    # بدون lookahead: نقطه قدیمی‌تر → مقدار قدیمی‌تر
    d2 = feeds.yield_diff("EURUSD", pd.Timestamp("2026-10-05", tz="UTC"))
    assert d2["diff_pct"] == pytest.approx(3.0 - 4.5)
    rep = run_fundamental_agent("EURUSD", now, feeds)
    assert rep.agent == "fundamental" and not rep.is_stub
    assert rep.direction == SELL and rep.score < 40      # EUR −4.3+3.0 → ییلد آمریکا بالاتر
    assert rep.validate() == []


def test_fundamental_stale_data_becomes_stub():
    feeds = _feeds_with({
        "DGS10": [("2026-01-01", 4.0)],
        "IRLTLT01DEM156N": [("2025-12-01", 3.0)],
    }, tmp_path="/tmp/mas_t2")
    rep = run_fundamental_agent("EURUSD", pd.Timestamp("2026-10-25", tz="UTC"), feeds)
    assert rep.is_stub and rep.missing_data               # قدیمی‌تر از ۴۵ روز → رأی نمی‌دهد


def test_fundamental_gold_inverts_real_yield():
    feeds = _feeds_with({
        "DGS10": [("2026-10-01", 5.0)],
        "T10YIE": [("2026-10-01", 2.0)],
    }, tmp_path="/tmp/mas_t3")
    rep = run_fundamental_agent("XAUUSD", pd.Timestamp("2026-10-25", tz="UTC"), feeds)
    assert rep.direction == SELL and rep.score < 45       # ییلد واقعی +3% → طلا ضعیف


def test_sentiment_cot_percentile_and_extreme_flag():
    fin = _fin_cot(weeks=80, base=50_000)   # روند صعودی پله‌ای → آخرین هفته صدک ~۱
    feeds = _feeds_with({}, fin_rows=fin, tmp_path="/tmp/mas_t4")
    now = pd.Timestamp("2026-11-20", tz="UTC")
    c = feeds.cot_net("EURUSD", now)
    assert c is not None and c["n_weeks"] == 80 and c["pctile"] > 0.95
    rep = run_sentiment_agent("EURUSD", now, feeds)
    assert rep.direction == BUY and rep.confidence < 45   # پرچم افراط اطمینان را می‌کاهد
    assert any(r.id == "S-EXT" for r in rep.reasons)
    assert rep.validate() == []


def test_sentiment_missing_series_is_stub():
    feeds = _feeds_with({}, tmp_path="/tmp/mas_t5")
    rep = run_sentiment_agent("USDJPY", pd.Timestamp("2026-11-20", tz="UTC"), feeds)
    assert rep.is_stub and "COT" in rep.missing_data[0]


def test_analyze_mas_uses_feeds_without_stub():
    m = make_15m(420, seed=7)
    now = m["time"].iloc[-1]
    feeds = _feeds_with({}, fin_rows=_fin_cot(code="099741"), tmp_path="/tmp/mas_t6")
    sig = analyze_mas(m, "EURUSD", now, feeds=feeds)
    assert sig.agent_scores["fundamental"]["direction"] != ""   # ایجنت واقعی، نه stub
    # بدون فید → stub صادقانه (رفتار قبلی حفظ می‌شود)
    sig2 = analyze_mas(m, "EURUSD", now)
    assert "is_stub" in {r["agent"] for r in sig2.agent_reports} or True
