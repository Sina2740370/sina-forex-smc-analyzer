"""لایهٔ ریسک/هزینه — مدل نشست‌محور بر پایهٔ اسپرد واقعی فاز ۱.

ممیزی مدل هزینهٔ ثابت (۲.۵–۳.۴٪ از 1R) را نقص کرد؛ دادهٔ تیک فاز ۱ نشان داد
اسپرد خارج نشست (۲۱-۲۴ UTC) حدود ۲ برابر لندن/نیویورک است. این ماژول همان
واقعیت را به گیت معاملاتی و هزینهٔ R تبدیل می‌کند.
"""
from __future__ import annotations

import json
import os

# ضریب لغزش محافظه‌کارانه برای سفارش بازار: اسپرد مؤثر = 1.5 × میانهٔ تیک.
# فرض ثبت‌شده است، نه داده — در سند تصمیم نهایی آمده.
SLIPPAGE_MULT = 1.5

SESSIONS = {"asia": (0, 7), "london": (7, 13), "newyork": (13, 21), "off": (21, 24)}


def session_of_hour(h: int) -> str:
    for name, (a, b) in SESSIONS.items():
        if a <= h < b:
            return name
    return "off"


def load_spread_report(path: str = "/home/z/my-project/sweep-dev/out_mas/phase1_spread_sessions.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cost_in_R(symbol: str, hour_utc: int, mid_price: float, atr: float,
              stop_atr_mult: float = 1.0, report: dict | None = None,
              slippage_mult: float = SLIPPAGE_MULT) -> float:
    """هزینهٔ یک طرف ورود بر حسب R: اسپرد مؤثر ÷ فاصلهٔ حد ضرر.

    اسپرد از میانهٔ نشستِ واقعی فاز ۱ (bp) می‌آید؛ نماد/نشست مفقود →
    بدترین نشست همان نماد، و نماد کاملاً ناشناخته → خطا (بدون حدس).
    """
    if atr <= 0 or mid_price <= 0 or stop_atr_mult <= 0:
        raise ValueError("atr، mid_price و stop_atr_mult باید مثبت باشند")
    if report is None:
        report = load_spread_report()
    if symbol not in report:
        raise KeyError(f"هیچ دادهٔ اسپردی برای {symbol} نیست — حدس ممنوع")
    entry = report[symbol]
    sess = session_of_hour(hour_utc)
    by_session = entry.get("by_session", {})
    if sess in by_session:
        bp = by_session[sess]["median_bp"]
    elif by_session:
        bp = max(v["median_bp"] for v in by_session.values())
    else:
        bp = entry["rel_spread_median_bp"]
    spread_price = mid_price * bp / 1e4 * slippage_mult
    stop_distance = stop_atr_mult * atr
    return float(spread_price / stop_distance)


def session_gate(hour_utc: int, symbol: str, max_cost_R: float = 0.05,
                 mid_price: float | None = None, atr: float | None = None,
                 stop_atr_mult: float = 1.0, report: dict | None = None) -> dict:
    """گیت ورود: در نشست‌های گران معامله ممنوع. خروجی شامل دلیل است."""
    decision = {"allow": True, "reason": "ok", "hour_utc": hour_utc,
                "session": session_of_hour(hour_utc)}
    if mid_price is None or atr is None:
        # بدون دادهٔ قیمت فقط گیت ساعتی سخت: خارج نشست (۲۱-۲۴) بسته است.
        if decision["session"] == "off":
            decision.update({"allow": False, "reason": "off-session hard gate"})
        return decision
    c = cost_in_R(symbol, hour_utc, mid_price, atr, stop_atr_mult, report)
    decision["cost_R"] = round(c, 5)
    if c > max_cost_R:
        decision.update({"allow": False, "reason": f"cost {c:.3f}R > {max_cost_R}R"})
    return decision
