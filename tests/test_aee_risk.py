"""تست‌های لایهٔ ریسک/هزینهٔ نشست‌محور."""
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")

import pytest

from aee.risk import SLIPPAGE_MULT, cost_in_R, load_spread_report, session_gate, session_of_hour

REPORT = {
    "EURUSD": {"rel_spread_median_bp": 0.18,
               "by_session": {"asia": {"median_bp": 0.27}, "london": {"median_bp": 0.18},
                              "newyork": {"median_bp": 0.18}, "off": {"median_bp": 0.36}}},
    "XAUUSD": {"rel_spread_median_bp": 13.0, "by_session": {}},
}


def test_session_of_hour_boundaries():
    assert session_of_hour(0) == "asia"
    assert session_of_hour(6) == "asia"
    assert session_of_hour(7) == "london"
    assert session_of_hour(12) == "london"
    assert session_of_hour(13) == "newyork"
    assert session_of_hour(20) == "newyork"
    assert session_of_hour(21) == "off"
    assert session_of_hour(23) == "off"


def test_cost_uses_real_session_medians():
    c_london = cost_in_R("EURUSD", 10, 1.12, 0.0010, 1.0, REPORT)
    c_off = cost_in_R("EURUSD", 22, 1.12, 0.0010, 1.0, REPORT)
    # نسبت هزینه‌ها باید نسبت اسپرد نشست‌ها باشد (۰.۳۶/۰.۱۸ = ۲)
    assert c_off == pytest.approx(2 * c_london)
    # مقدار مطلق: 1.12 × 0.18bp × 1.5 ÷ (1.0 × 0.0010) ≈ 0.0302R
    assert c_london == pytest.approx(1.12 * 0.18 / 1e4 * 1.5 / 0.0010, rel=1e-6)
    assert SLIPPAGE_MULT == 1.5


def test_cost_scales_with_stop_and_atr():
    base = cost_in_R("EURUSD", 10, 1.12, 0.0010, 1.0, REPORT)
    assert cost_in_R("EURUSD", 10, 1.12, 0.0010, 2.0, REPORT) == pytest.approx(base / 2)
    assert cost_in_R("EURUSD", 10, 1.12, 0.0020, 1.0, REPORT) == pytest.approx(base / 2)


def test_unknown_symbol_raises_no_guess():
    with pytest.raises(KeyError):
        cost_in_R("USDCHF", 10, 0.9, 0.001, 1.0, REPORT)


def test_missing_session_falls_back_to_worst():
    # XAUUSD هیچ نشستی ندارد → بدترین (بیشترین) مقدار
    c = cost_in_R("XAUUSD", 10, 4000.0, 2.0, 1.0, REPORT)
    assert c == pytest.approx(4000.0 * 13.0 / 1e4 * 1.5 / 2.0, rel=1e-6)


def test_gate_blocks_off_session_without_price_data():
    d = session_gate(22, "EURUSD")
    assert d["allow"] is False and "off-session" in d["reason"]
    assert session_gate(10, "EURUSD")["allow"] is True


def test_gate_blocks_expensive_sessions():
    # EURUSD شب با حد 0.05R: 1.12 × 0.36bp × 1.5 / 0.0010 ≈ 0.0605R → بسته
    d = session_gate(22, "EURUSD", max_cost_R=0.05, mid_price=1.12, atr=0.0010, report=REPORT)
    assert d["allow"] is False and d["cost_R"] > 0.05
    # همان لحظه با حد ضرر دو برابر: هزینه نصف → باز
    d2 = session_gate(22, "EURUSD", max_cost_R=0.05, mid_price=1.12, atr=0.0010,
                      stop_atr_mult=2.0, report=REPORT)
    assert d2["allow"] is True


def test_load_spread_report_real_file():
    rep = load_spread_report()
    assert "EURUSD" in rep and "by_session" in rep["EURUSD"]
