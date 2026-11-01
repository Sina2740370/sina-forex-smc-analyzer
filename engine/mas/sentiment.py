"""فاز ۳ — A5 Sentiment: موقعیت خالص COT (پول هوشمند) به‌صورت صدک ۳ ساله.

منطق: صدک بالا = خوش‌بینی متراکم پول هوشمند (هم‌سو در روند = تأیید؛ در اوج = ریسک واژگونی).
امتیاز: 50 + 20·(pctile−0.5)·2 → صدک ۱.۰ = ۷۰، صدک ۰ = ۳۰.
پرچم افراط: صدک > 0.90 یا < 0.10 → هشدار «متراکم‌شدگی» در reasons و جریمه اطمینان.
بدون داده (درخواست/شبکه شکست) → stub صادقانه.
"""
from __future__ import annotations

import pandas as pd

from .contracts import BUY, SELL, AgentReport, Reason
from .datafeeds import MASFeeds

EXTREME = 0.90


def run_sentiment_agent(symbol: str, now: pd.Timestamp,
                        feeds: MASFeeds | None) -> AgentReport:
    if feeds is None:
        return AgentReport(agent="sentiment", is_stub=True,
                           missing_data=["فید سنتیمنت به سیستم داده نشده (MASFeeds)"])
    c = feeds.cot_net(symbol, now)
    if c is None:
        return AgentReport(agent="sentiment", is_stub=True,
                           missing_data=[f"COT CFTC برای {symbol} در دسترس نیست ({feeds.notes[:2] or 'بدون کش'})"])
    p = c["pctile"]
    score = 50.0 + 20.0 * (p - 0.5) * 2
    direction = BUY if score >= 56 else (SELL if score <= 44 else "NEUTRAL")
    conf = 45.0
    reasons = [Reason("S-COT", f"صدک موقعیت خالص {c['source']}: {p:.0%} (خالص {c['net']:+,.0f} قرارداد)",
                      f"asof={c['asof']}, n={c['n_weeks']} هفته، صدک در بازه ۳ سال")]
    if p > EXTREME:
        conf = 25.0
        reasons.append(Reason("S-EXT", "موقعیت لانگ متراکم (صدک > ۹۰٪) — ریسک واژگونی روایت؛ "
                              "تأیید مشروط، نه سیگنال جدید", f"pctile={p:.2f}"))
    elif p < 1 - EXTREME:
        conf = 25.0
        reasons.append(Reason("S-EXT", "موقعیت شورت متراکم (صدک < ۱۰٪) — ریسک فشردگی پوشش؛ "
                              "تأیید مشروط", f"pctile={p:.2f}"))
    return AgentReport(
        agent="sentiment", direction=direction, score=round(score, 1),
        confidence=round(conf, 1), reasons=reasons,
        invalidation=["واژگونی هفتگی COT به جهت مخالف با تغییر خالص > ۲۰ صدک ابطال می‌کند"],
        missing_data=["COT هفتگی است (تأخیر تا ۶ روز) — سنتیمنت بلادرنگ نیست"]
        if (now - pd.Timestamp(c["asof"], tz="UTC")).days > 6 else [],
        context={"cot_pctile": p, "cot_net": c["net"], "cot_asof": c["asof"], "source": c["source"]})
