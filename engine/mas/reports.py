"""خروجی استاندارد: JSON سیگنال + گزارش Markdown فارسی (جدول امتیاز ایجنت‌ها + سیگنال اجرایی)."""
from __future__ import annotations

import json

from .contracts import Signal


def signal_json(sig: Signal, inputs_hash: str = "", include_reports: bool = True) -> str:
    return json.dumps(sig.to_json(inputs_hash, include_reports=include_reports),
                      ensure_ascii=False, indent=2)


def signal_markdown(sig: Signal) -> str:
    j = sig.to_json()
    dir_fa = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "WAIT": "⏸ WAIT"}[sig.signal]
    lines = [f"### گزارش نهایی",
             f"- جفت‌ارز: {sig.symbol}",
             f"- تایم‌فریم: {sig.timeframe}",
             f"- سشن: {sig.session or '-'}",
             f"- رژیم بازار: {sig.market_regime}",
             f"- سیگنال: **{dir_fa}**",
             f"- امتیاز نهایی: {j['final_score']}",
             f"- سطح اطمینان: {j['confidence']}",
             f"- اثر انگشت: `{j['fingerprint']}`",
             (f"- دلیل WAIT: {sig.wait_reason}" if sig.signal == "WAIT" else
              f"- دلیل اصلی: {j['agent_scores'].get('technical', {}).get('direction', '')} + اجماع debate"),
             ""]
    lines += ["### جدول امتیاز ایجنت‌ها",
              "| ایجنت | جهت | امتیاز | اطمینان |",
              "|---|---|---:|---:|"]
    for name, s in j["agent_scores"].items():
        lines.append(f"| {name} | {s['direction']} | {s['score']} | {s['confidence']} |")
    lines += ["", "### گیت‌های تصمیم", "| گیت | نتیجه | توضیح |", "|---|---|---|"]
    for g in j["gate_results"]:
        lines.append(f"| {g['gate']} | {'✅' if g['passed'] else '❌'} | {g['detail']} |")
    if sig.signal != "WAIT":
        lines += ["", "### سیگنال اجرایی",
                  f"- نوع سفارش: Limit/Market (تریگر: {sig.entry_trigger})",
                  f"- ناحیه ورود: {sig.entry_zone}",
                  f"- حد ضرر: {sig.stop_loss}",
                  f"- هدف اول: {sig.take_profit_1}",
                  f"- هدف دوم: {sig.take_profit_2}",
                  f"- نسبت ریسک به ریوارد: {sig.risk_reward}",
                  f"- حجم لات: {sig.lot_size}",
                  f"- ریسک دلاری: {sig.risk_amount}",
                  f"- وضعیت ریسک حساب: {'تأیید' if sig.risk_approved else 'رد'}"]
    if sig.invalidation:
        lines += ["", "### شرایط ابطال"]
        lines += [f"- {x}" for x in sig.invalidation]
    if sig.missing_data:
        lines += ["", "### داده‌های غایب (بدون حدس)"]
        lines += [f"- {x}" for x in sorted(set(sig.missing_data))]
    if sig.warnings:
        lines += ["", "### هشدارها"]
        lines += [f"- {x}" for x in sig.warnings]
    return "\n".join(lines)
