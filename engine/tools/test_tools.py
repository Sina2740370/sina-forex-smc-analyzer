import pytest

from engine.tools.risk import (SPECS, breakeven_winrate, position_size, trade_r,
                               loss_per_lot_per_unit)
from engine.tools.journal import (by_tag, daily_guard, drawdown_guard, journal_stats,
                                  json_safe, normalize, verdict)


def test_position_size_eurusd_usd_quote():
    # 10000$ ، ریسک 1% = 100$ ، SL=20 پیپ → هر پیپ لات = 10$ → 0.5 لات
    r = position_size("EURUSD", 10_000, 1.0, 1.1000, 1.0980)
    assert r.ok and r.lots == 0.5 and abs(r.actual_risk - 100.0) < 0.01 and r.sl_pips == 20.0


def test_position_size_xauusd():
    # ریسک 100$ ، SL=5$ قیمت ، قرارداد 100 اونس → ضرر هر لات 500$ → 0.2 لات
    r = position_size("XAUUSD", 10_000, 1.0, 2000.0, 1995.0)
    assert r.ok and r.lots == 0.2 and abs(r.actual_risk - 100.0) < 0.01


def test_position_size_usdjpy_converts_by_price():
    # ریسک 100$ ، SL=0.5 ین ، 100000/150 = 666.67$ به‌ازای هر ین → ضرر هر لات 333.33$ → 0.3 لات
    r = position_size("USDJPY", 10_000, 1.0, 150.0, 149.5)
    assert r.ok and r.lots == 0.3 and r.actual_risk <= r.risk_amount + 1e-6


def test_position_size_rounds_down_never_exceeds_risk():
    r = position_size("EURUSD", 1_234, 1.0, 1.1, 1.0977)  # ریسک 12.34$
    assert r.ok and r.actual_risk <= r.risk_amount + 1e-9


def test_position_size_too_small_and_errors():
    r = position_size("EURUSD", 100, 1.0, 1.1, 1.09)  # ریسک 1$ ، SL=100 پیپ → کمتر از 0.01 لات
    assert not r.ok and r.lots == 0.0
    with pytest.raises(ValueError):
        position_size("EURUSD", 1000, 1, 1.1, 1.1)
    with pytest.raises(ValueError):
        position_size("EURGBP", 1000, 1, 1.1, 1.09)
    with pytest.raises(ValueError):
        position_size("EURUSD", 1000, 0, 1.1, 1.09)


def test_rr_and_breakeven():
    r = position_size("EURUSD", 10_000, 1.0, 1.10, 1.098, tp=1.104)
    assert r.rr == 2.0
    assert abs(breakeven_winrate(2.0) - 1 / 3) < 1e-12


def test_trade_r():
    assert trade_r("BUY", 1.10, 1.09, 1.12) == pytest.approx(2.0)
    assert trade_r("SELL", 1.10, 1.11, 1.12) == pytest.approx(-2.0)
    assert trade_r("BUY", 1.10, 1.09, 1.09) == pytest.approx(-1.0)
    with pytest.raises(ValueError):
        trade_r("BUY", 1.10, 1.11, 1.12)  # SL سمت اشتباه


def _row(i, side="BUY", exit_=1.12, tag="a"):
    return {"symbol": "EURUSD", "side": side, "entry": 1.10, "sl": 1.09, "exit": exit_,
            "close_time": f"2026-01-{(i % 28) + 1:02d}T10:00:00Z", "tag": tag}


def test_journal_verdict_honest_with_few_trades():
    s = journal_stats([_row(i) for i in range(5)])
    assert s["n"] == 5 and "داده کافی نیست" in s["verdict"]
    assert journal_stats([])["n"] == 0


def test_journal_verdict_with_edge_and_without():
    wins = [_row(i, exit_=1.12) for i in range(40)]            # +2R همه
    s = journal_stats(wins + [_row(i, exit_=1.09) for i in range(10)])
    assert "لبه‌ی مثبت" in s["verdict"]
    mixed = [_row(i, exit_=1.12 if i % 2 else 1.09) for i in range(40)]  # +2 و -1 متناوب ولی کم
    assert verdict(journal_stats(mixed)) != ""
    losers = [_row(i, exit_=1.09) for i in range(40)] + [_row(i, exit_=1.101) for i in range(5)]
    assert "زیان‌ده" in journal_stats(losers)["verdict"]


def test_by_tag_flags_unreliable():
    rows = [_row(i, tag="x") for i in range(3)] + [_row(i, tag="y") for i in range(35)]
    g = {x["key"]: x for x in by_tag(rows)}
    assert g["x"]["reliable"] is False and g["y"]["reliable"] is True


def test_daily_guard_states():
    assert daily_guard([1.0, -1.0])["state"] == "ok"
    assert daily_guard([-1, -1.2])["state"] == "warn"            # -2.2 ≥ 70% از 3
    assert daily_guard([-1, -1, -1])["state"] == "stop"          # سقف ضرر و 3 ضرر متوالی
    assert daily_guard([1, -1, -1, -1])["state"] == "stop"       # 3 ضرر پشت‌سرهم
    assert daily_guard([1] * 5)["state"] == "stop"               # سقف تعداد
    assert daily_guard([1, 1, 1, 1])["state"] == "warn"          # یک معامله تا سقف


def test_drawdown_guard():
    g = drawdown_guard([2, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1])  # اوج 2 ، سپس -10
    assert g["state"] == "stop" and g["current_dd_r"] == 10.0
    assert drawdown_guard([1, 1, -1])["state"] == "ok"


def test_normalize_sorted_and_tz():
    rows = [_row(5), _row(1)]
    n = normalize(rows)
    assert n[0]["close_time"] < n[1]["close_time"] and n[0]["close_time"].tzinfo is not None


def test_json_safe_handles_inf_and_tuples():
    import json
    s = journal_stats([_row(0)])  # n=1 → se بی‌نهایت
    json.dumps(json_safe(s), allow_nan=False)
    assert json_safe({"a": (1.0, float("inf")), "b": float("nan")}) == {"a": [1.0, None], "b": None}
