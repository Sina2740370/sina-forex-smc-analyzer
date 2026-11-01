"""تست‌های MAS فاز ۱: قرارداد ایجنت‌ها، ماتریس وزن، گیت‌ها، ریسک، Debate، ابطال فیبو، Replay."""
import numpy as np
import pandas as pd
import pytest

from engine.mas.agents import (run_data_agent, run_liquidity_agent, run_regime_agent,
                               run_technical_agent)
from engine.mas.aggregator import analyze_mas
from engine.mas.contracts import BUY, SELL, AgentReport, Reason, missing_report
from engine.mas.debate import run_debate
from engine.mas.risk_agent import AccountState, evaluate_risk
from engine.mas.weights import WEIGHTS, get_weights
from engine.data.providers import build_topdown

from tests.test_engine import make_15m


def _top(days=420, seed=7):
    m = make_15m(days, seed)
    return build_topdown(m), m


def test_agent_reports_are_deterministic_and_valid():
    top, _ = _top()
    now = top["15m"]["time"].iloc[-1]
    for rep in (run_regime_agent(top, now), run_technical_agent(top, now),
                run_liquidity_agent(top, now)):
        assert rep.validate() == []
    rep2 = run_technical_agent(top, now)
    rep1 = run_technical_agent(top, now)
    assert rep1.score == rep2.score and rep1.direction == rep2.direction  # قطعی


def test_reason_without_evidence_rejected():
    r = AgentReport("technical", BUY, 60, 50, reasons=[Reason("T-1", "بدون شواهد")])
    assert r.validate()  # قرارداد: هر دلیل باید evidence داشته باشد


def test_weights_sum_to_one():
    for regime, w in WEIGHTS.items():
        assert abs(sum(w.values()) - 1.0) < 1e-9, regime
        assert abs(sum(get_weights({"regime": regime}).values()) - 1.0) < 1e-9


def test_debate_no_signal_when_sides_tied():
    top, _ = _top()
    now = top["15m"]["time"].iloc[-1]
    flat = AgentReport("technical", "NEUTRAL", 50, 60)
    flat2 = AgentReport("liquidity", "NEUTRAL", 50, 40)
    d = run_debate([flat, flat2], {"htf_bias": 0})
    assert d.direction == "NEUTRAL" and d.confidence == 0.0


def test_risk_agent_rejects_low_rr_and_sizes_lots():
    v = evaluate_risk("EURUSD", "BUY", entry=1.1000, sl=1.0990, tp2=1.1015, price=1.1000)
    assert not v.approved and any(g.gate == "R-RR" for g in v.gates)   # RR=1.5 < 2
    v2 = evaluate_risk("EURUSD", "BUY", entry=1.1000, sl=1.0990, tp2=1.1020, price=1.1000)
    assert v2.approved and v2.lots > 0
    # ریاضی حجم: 1% از 10k = $100؛ ریسک 10 پیپ × $10/لات → 1 لات
    assert abs(v2.lots - 1.0) < 0.01 and abs(v2.risk_amount - 100) < 1.0
    v3 = evaluate_risk("EURUSD", "BUY", 1.1, 1.099, 1.102, 1.1,
                       acct=AccountState(equity=10_000, open_trades=3))
    assert not v3.approved and any(g.gate == "R-OPEN" for g in v3.gates)
    v4 = evaluate_risk("EURUSD", "BUY", 1.1, 1.099, 1.102, 1.1,
                       acct=AccountState(equity=10_000, consecutive_losses=3))
    assert not v4.approved  # سه ضرر متوالی → توقف
    v5 = evaluate_risk("USDJPY", "SELL", 155.00, 155.20, 153.60, 155.00)
    assert v5.approved and v5.lots > 0   # pip value با نرخ تبدیل


def test_missing_agent_report_is_neutral_and_flagged():
    r = missing_report("fundamental", ["نرخ بهره"])
    assert r.direction == "NEUTRAL" and r.confidence == 0.0 and r.missing_data


def test_mas_full_run_produces_json_contract():
    top, m = _top()
    now = m["time"].iloc[-1]
    sig = analyze_mas(m, "EURUSD", now)
    j = sig.to_json("abc")
    for key in ("symbol", "timeframe", "session", "market_regime", "signal", "final_score",
                "confidence", "entry_zone", "entry_trigger", "stop_loss", "take_profit_1",
                "take_profit_2", "risk_reward", "lot_size", "risk_amount", "invalidation",
                "agent_scores", "risk_approved", "warnings", "missing_data", "fingerprint"):
        assert key in j, key
    assert sig.signal in ("BUY", "SELL", "WAIT")
    assert len(sig.gate_results) >= 1
    if sig.signal == "WAIT":
        assert sig.wait_reason  # هر WAIT دلیل شناسه‌دار دارد
    # Replay: دو اجرای یکسان → خروجی یکسان
    sig2 = analyze_mas(m, "EURUSD", now)
    assert sig2.to_json()["final_score"] == j["final_score"]
    assert sig2.to_json()["signal"] == j["signal"]


def test_mas_wait_on_short_history():
    top, m = _top()
    sig = analyze_mas(m.iloc[:3000].reset_index(drop=True), "EURUSD", m["time"].iloc[2999])
    assert sig.signal == "WAIT" and "G7" in sig.wait_reason


def test_fib_without_momentum_is_not_entry():
    """قاعده فیبو: قیمت در ناحیه 0.618 ولی مومنتوم تأیید نکند → نه امتیاز جهت‌دار."""
    top, _ = _top(days=420, seed=3)
    now = top["15m"]["time"].iloc[-1]
    rep = run_technical_agent(top, now)
    # فقط بررسی قرارداد: اگر فیبو دیده شد ولی تأیید نشد، دلیل WAIT ثبت شده باشد
    if any("T-FB" == r.id for r in rep.reasons) and not any("T-FBv" == r.id for r in rep.reasons):
        assert any("T-FBi" == r.id for r in rep.reasons)


def test_mas_walkforward_no_lookahead():
    """سیگنال در نقطه t فقط با داده ≤ t نباید تغییر کند اگر آینده اضافه شود؟ — نه:
    درست‌تر: سیگنال در t با داده کامل ولی now=t برابر سیگنال با دیتای بریده در t است."""
    top, m = _top()
    i = len(m) - 5000
    t = m["time"].iloc[i]
    s1 = analyze_mas(m, "EURUSD", t)
    s2 = analyze_mas(m.iloc[:i + 1].reset_index(drop=True), "EURUSD", t)
    assert s1.signal == s2.signal and abs(s1.final_score - s2.final_score) < 1e-9
