"""فاز ۳ — A4 Fundamental: ییلد دیفرنشیال از FRED.

منطق: جریان سرمایه دنبال بازده نسبی است؛ تفاضل ییلد بلندمدت (بِیس − کوت) در افق چندماهه
همبستگی جهت‌دار با جفت‌ارز دارد. برای طلا: ییلد واقعی آمریکا (DGS10 − T10YIE) هزینه فرصت
نگهداری طلاست → معکوس.
امتیاز: 50 + 25·tanh(diff/1.5) → تفاضل ±1.5٪ ≈ ±16 امتیاز؛ سقف ±25.
اطمینان: تازگی داده (سری‌های ماهانه OECD تا ~۳ ماه قدیمی‌اند) → سقف ۵۰، نه ادعای بلادرنگ.
بدون داده → stub صادقانه (missing_report) — رأی نمی‌دهد.
"""
from __future__ import annotations

import pandas as pd

from .contracts import BUY, SELL, AgentReport
from .datafeeds import MASFeeds

DIFF_SCALE = 1.5      # تفاضلی که نصفِ سقف امتیاز می‌گیرد (٪)
STALE_MAX_DAYS = 45   # قدیمی‌تر از این → confidence صفر (داده غایب تلقی می‌شود)


def run_fundamental_agent(symbol: str, now: pd.Timestamp,
                          feeds: MASFeeds | None) -> AgentReport:
    if feeds is None:
        return AgentReport(agent="fundamental", is_stub=True,
                           missing_data=["فید فاندامنتال به سیستم داده نشده (MASFeeds)"])
    d = feeds.yield_diff(symbol, now)
    if d is None:
        return AgentReport(agent="fundamental", is_stub=True,
                           missing_data=[f"ییلد FRED در دسترس نیست ({feeds.notes[:2] or 'بدون کش'})"])
    if d["staleness_days"] > STALE_MAX_DAYS:
        return AgentReport(agent="fundamental", is_stub=True,
                           missing_data=[f"داده ییلد قدیمی‌تر از {STALE_MAX_DAYS} روز "
                                         f"(asof={d['asof']}) — رأی نمی‌دهد"])
    score = 50.0 + 25.0 * _tanh(d["diff_pct"] / DIFF_SCALE)
    direction = BUY if score >= 58 else (SELL if score <= 42 else "NEUTRAL")
    conf = max(10.0, 50.0 - d["staleness_days"])   # 45 روز قدیمی → 5؛ 5 روز → 45
    return AgentReport(
        agent="fundamental", direction=direction, score=round(score, 1),
        confidence=round(conf, 1),
        reasons=[_reason(d)],
        invalidation=["واژگونی شیب ییلد (تفاضل عبور از صفر در جهت مخالف) ابطال می‌کند"],
        missing_data=[f"تازگی داده: {d['staleness_days']:.0f} روز (سری ماهانه OECD/DGS10)"]
        if d["staleness_days"] > 7 else [],
        context={"diff_pct": round(d["diff_pct"], 3), "asof": d["asof"],
                 "series": d["series"]})


def _tanh(x: float) -> float:
    import math
    return math.tanh(x)


def _reason(d: dict):
    from .contracts import Reason
    txt = (f"ییلد بلندمدت بِیس − کوت = {d['diff_pct']:+.2f}% "
           f"({'طلا: معکوس ییلد واقعی' if 'inverted' in d['series'] else 'حامل بازده نسبی'})")
    return Reason("F-YD", txt, f"series={d['series']}, asof={d['asof']}, diff={d['diff_pct']:+.3f}%")
