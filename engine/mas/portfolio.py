"""فاز ۴ — پورتفوی چندجفتی: تحلیل همه نمادها در یک نقطه زمانی با AccountState مشترک.

تفاوت با تحلیل تک‌نماد: گیت‌های سطح حساب (سقف ریسک همزمان ۳٪، حداکثر ۳ معامله باز،
توقف پس از ضررهای متوالی) در سطح «سبد» اعمال می‌شوند — یعنی سیگنال هر نماد با
تخصیص ریسک باقی‌مانده سنجیده می‌شود، نه با حساب فرضی مستقل.

خروجی: گزارش ترکیبی Markdown + JSON (نسخه نهایی هر نماد + خلاصه سبد).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

import pandas as pd

from ..data.providers import load_mt5_csv
from .aggregator import analyze_mas
from .reports import signal_markdown
from .risk_agent import AccountState
from .symbols import SYM_SPECS, get_spec


def _csv_path(csv_dir: str, symbol: str) -> str:
    for suffix in ("_M15.csv", "_i_M15.csv"):
        p = os.path.join(csv_dir, f"{symbol}{suffix}")
        if os.path.exists(p):
            return p
    return ""


def run_portfolio(csv_dir: str, now: pd.Timestamp | None = None,
                  symbols: list | None = None, equity: float = 10_000.0) -> dict:
    """تحلیل چندجفتی هم‌زمان. نمادها با ریسک مشترک ارزیابی می‌شوند."""
    symbols = symbols or sorted(SYM_SPECS)
    now = pd.Timestamp(now, tz="UTC") if now else None

    acct = AccountState(equity=equity)
    results = {}
    for sym in symbols:
        path = _csv_path(csv_dir, sym)
        if not path:
            results[sym] = {"error": "داده پیدا نشد"}
            continue
        df = load_mt5_csv(path)
        t_now = now or df["time"].iloc[-1]      # پیش‌فرض: آخرین کندل هر نماد
        sig = analyze_mas(df, sym, t_now, acct=acct)
        results[sym] = sig.to_json(include_reports=False)
        # سهم ریسک در سبد فقط برای سیگنال‌های تأییدشده ثبت می‌شود
        if sig.signal in ("BUY", "SELL"):
            pct = float((sig.risk_amount or "0").split()[0]) / equity * 100
            acct.open_trades += 1
            acct.open_risk_pct = round(acct.open_risk_pct + pct, 2)
            acct.per_symbol_open[sym] = 1

    approved = [s for s, r in results.items() if r.get("signal") in ("BUY", "SELL")]
    waits = [s for s, r in results.items() if r.get("signal") == "WAIT"]
    errors = [s for s, r in results.items() if "error" in r]
    portfolio = {
        "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "analysis_point": str(now) if now else "آخرین کندل موجود هر نماد",
        "equity_usd": equity,
        "symbols": results,
        "summary": {
            "approved": approved, "waiting": waits, "errors": errors,
            "open_risk_pct": acct.open_risk_pct,
            "open_trades": acct.open_trades,
        },
    }
    return portfolio


def portfolio_markdown(p: dict) -> str:
    """گزارش فارسی سبد: جدول یک‌نگاهی + گزارش کامل هر نماد سیگنال‌دار."""
    s = p["summary"]
    lines = [f"# گزارش پورتفوی چندجفتی MAS",
             f"- لحظه تحلیل: {p['analysis_point']} (UTC)",
             f"- حقوق پایه: {p['equity_usd']:.0f} USD",
             f"- سیگنال‌های فعال: **{len(s['approved'])}** | در انتظار: {len(s['waiting'])} | خطا: {len(s['errors'])}",
             f"- ریسک همزمان مصرف‌شده: {s['open_risk_pct']:.1f}% از سقف ۳٪",
             "",
             "## یک‌نگاهی سبد",
             "| نماد | سیگنال | امتیاز | اطمینان | رژیم | دلیل WAIT / سطح ورود |",
             "|---|---|---:|---:|---|---|"]
    for sym, r in p["symbols"].items():
        if "error" in r:
            lines.append(f"| {sym} | ❌ | - | - | - | {r['error']} |")
            continue
        if r["signal"] == "WAIT":
            detail = (r.get("wait_reason") or "-")[:60]
            lines.append(f"| {sym} | ⏸ WAIT | {r['final_score']} | {r['confidence']} | "
                         f"{r['market_regime'] or '-'} | {detail} |")
        else:
            lines.append(f"| {sym} | **{'🟢' if r['signal'] == 'BUY' else '🔴'} {r['signal']}** | "
                         f"{r['final_score']} | {r['confidence']} | {r['market_regime'] or '-'} | "
                         f"ورود {r['entry_zone']} / SL {r['stop_loss']} / TP2 {r['take_profit_2']} |")

    lines += ["", "## گزارش کامل نمادهای سیگنال‌دار", ""]
    for sym in s["approved"]:
        r = p["symbols"][sym]
        lines += [f"### {sym} — {'🟢 BUY' if r['signal'] == 'BUY' else '🔴 SELL'}",
                  f"- امتیاز نهایی: {r['final_score']} | اطمینان: {r['confidence']} | ریسک: {r['risk_amount']} | حجم: {r['lot_size']} لات",
                  f"- ورود: {r['entry_zone']} | تریگر: {r['entry_trigger']}",
                  f"- SL: {r['stop_loss']} | TP1: {r['take_profit_1']} | TP2: {r['take_profit_2']} | RR: {r['risk_reward']}",
                  f"- اثر انگشت: `{r['fingerprint']}`",
                  "", "**شرایط ابطال:**"]
        lines += [f"- {x}" for x in r.get("invalidation", [])[:4]]
        lines.append("")
    if not s["approved"]:
        lines += ["هیچ نمادی از هر ۷ گیت عبور نکرد — سبد در حالت انتظار است. "
                  "این خروجیِ طبیعی طراحی ضد-overtrading است: هر WAIT با گیتِ ردکننده ثبت می‌شود.", ""]
    if s["waiting"]:
        lines += ["## چکیده دلایل WAIT", ""]
        for sym in s["waiting"]:
            r = p["symbols"][sym]
            lines.append(f"- **{sym}**: {r.get('wait_reason') or '-'}")
        lines.append("")
    if any("error" in r for r in p["symbols"].values()):
        lines += ["## خطاها", ""]
        for sym, r in p["symbols"].items():
            if "error" in r:
                lines.append(f"- **{sym}**: {r['error']}")
        lines.append("")
    return "\n".join(lines)


def save_portfolio(p: dict, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "portfolio.json")
    mp = os.path.join(out_dir, "portfolio.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2, default=str)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(portfolio_markdown(p))
    return jp, mp
