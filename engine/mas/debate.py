"""
A7 — پروتکل Debate: طرف صعودی و نزولی فقط از گزارش‌های A2–A6 تغذیه می‌شوند.
هیچ طرفی سیگنال نهایی نمی‌دهد؛ خروجی: bull_score، bear_score، اجماع، تناقض‌ها.

دو دور (طبق سقف دو پاسخ):
  دور ۱ — جمع‌بندی دلایل وزنی هر طرف از گزارش‌ها
  دور ۲ — بازبینی با شواهد تأییدی بلندمدت‌تر (هم‌جهتی H4/D1 و سوئیپ)؛ فقط برای طرف‌هایی که در دور ۱ کمتر از ۵۵ آورده‌اند، فرصت تقویت دارد.
"""
from __future__ import annotations

from .contracts import BUY, SELL, NEUTRAL, AgentReport, Reason
from .weights import CONSENSUS_MARGIN

SCORING_AGENTS = ("technical", "liquidity")   # گزارش‌های جهت‌دار؛ regime خنثی است و وزن زمینه‌ای دارد


def _side_evidence(reports: list[AgentReport], side: str) -> tuple[float, list[Reason]]:
    """امتیاز یک طرف = جمع وزنی سهم ایجنت‌های هم‌جهت + جریمه مخالفان."""
    total, ev = 0.0, []
    for rep in reports:
        if rep.agent not in SCORING_AGENTS:
            continue
        conf_w = max(0.2, rep.confidence / 100.0)
        if rep.direction == side:
            contribution = (rep.score - 50.0 if side == BUY else 50.0 - rep.score) / 2.0
            total += contribution * conf_w * 2
            for r in rep.reasons:
                ev.append(Reason(f"B:{r.id}" if side == BUY else f"S:{r.id}", r.text, r.evidence))
        elif rep.direction != NEUTRAL:
            total -= 4.0 * conf_w   # مخالف مستقیم
    return total, ev


def run_debate(reports: list[AgentReport], regime_ctx: dict) -> AgentReport:
    bull, bull_ev = _side_evidence(reports, BUY)
    bear, bear_ev = _side_evidence(reports, SELL)

    conflicts = []
    dirs = {r.agent: r.direction for r in reports if r.agent in SCORING_AGENTS}
    if "technical" in dirs and "liquidity" in dirs and dirs["technical"] != dirs["liquidity"] \
            and dirs["technical"] != NEUTRAL and dirs["liquidity"] != NEUTRAL:
        conflicts.append("technical و liquidity جهت مخالف گرفته‌اند (تقابل روند و نقدینگی)")

    # دور ۲: تقویت با شواهد بلندمدت (روند H4/D1 در کانتکست رژیم)
    htf = regime_ctx.get("htf_bias", 0)
    if htf > 0:
        bull += 2.0
        bull_ev.append(Reason("B:HTF", "هم‌جهتی ساختار بلندمدت (H4/D1)", f"htf_bias={htf:+d}"))
    elif htf < 0:
        bear += 2.0
        bear_ev.append(Reason("S:HTF", "هم‌جهتی ساختار بلندمدت (H4/D1)", f"htf_bias={htf:+d}"))

    margin = abs(bull - bear)
    if margin < CONSENSUS_MARGIN:
        direction, conf = NEUTRAL, 0.0
    else:
        direction = BUY if bull > bear else SELL
        conf = min(70.0, margin * 2)

    reasons = [Reason("D-SUM", f"امتیاز صعودی {bull:.1f} در برابر نزولی {bear:.1f} (حاشیه {margin:.1f})",
                      f"agents={ {k: v for k, v in dirs.items()} }")]
    reasons += (bull_ev if bull >= bear else bear_ev)[:6]
    return AgentReport(
        "debate", direction, 50 + margin if direction == BUY else 50 - margin if direction == SELL else 50,
        conf, reasons,
        [f"بسته‌شدن در جهت مخالف با عبور سطح سوئیپ‌شده → ابطال اجماع"],
        [], context={"bull": round(bull, 1), "bear": round(bear, 1),
                     "margin": round(margin, 1), "conflicts": conflicts})
