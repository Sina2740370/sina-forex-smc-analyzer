"""
ژورنال معاملات و نگهبان ریسک (خالص، بدون I/O).

هدف: به سینا نشان بدهد معاملات دستی خودش واقعاً لبه دارد یا نه، با همان آمار و صداقت بک‌تست
(امید ریاضی با بازه‌ی اطمینان، نه فقط درصد برد).
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

import pandas as pd

from ..backtest.runner import summarize
from .risk import trade_r

MIN_TRADES_MEANINGFUL = 30


def normalize(rows: Iterable[dict]) -> list:
    """هر ردیف: symbol, side, entry, sl, exit, close_time (اختیاری: open_time, tag, note)."""
    out = []
    for r in rows:
        rr = trade_r(r["side"], float(r["entry"]), float(r["sl"]), float(r["exit"]))
        ct = pd.Timestamp(r.get("close_time") or r.get("open_time") or pd.Timestamp.now(tz="UTC"))
        if ct.tzinfo is None:
            ct = ct.tz_localize("UTC")
        out.append({
            "symbol": r["symbol"], "side": r["side"].upper(),
            "open_time": r.get("open_time"), "close_time": ct,
            "entry": float(r["entry"]), "sl": float(r["sl"]), "exit": float(r["exit"]),
            "r": round(rr, 4), "status": "CLOSED", "tag": r.get("tag") or "",
        })
    return sorted(out, key=lambda t: t["close_time"])


def verdict(s: dict) -> str:
    """قضاوت صادقانه به زبان ساده؛ هرگز با n کم ادعای لبه نمی‌کند."""
    n = s.get("n", 0)
    if n < MIN_TRADES_MEANINGFUL:
        return f"داده کافی نیست ({n} معامله؛ حداقل {MIN_TRADES_MEANINGFUL} برای قضاوت اولیه، ۱۰۰+ برای اعتماد)"
    lo, hi = s["expectancy_ci95"]
    if lo > 0:
        return "شواهد آماری لبه‌ی مثبت دیده می‌شود؛ هنوز با معاملات تازه تأیید شود"
    if hi < 0:
        return "شواهد آماری زیان‌ده بودن: روش یا اجرا را بازنگری کن"
    return "نامشخص: بازه‌ی اطمینان صفر را شامل می‌شود؛ تفاوت از شانس قابل تشخیص نیست"


def journal_stats(rows: Iterable[dict]) -> dict:
    trades = normalize(rows)
    if not trades:
        return {"n": 0, "verdict": verdict({"n": 0})}
    s = summarize(trades)
    s["verdict"] = verdict(s)
    return s


def by_tag(rows: Iterable[dict], key: str = "tag") -> list:
    """عملکرد به تفکیک برچسب (ستاپ، نماد، ...). هر گروه با n<30 «غیرقابل‌اعتماد» علامت می‌خورد."""
    trades = normalize(rows)
    groups = {}
    for t in trades:
        groups.setdefault(t.get(key) or "(بدون برچسب)", []).append(t["r"])
    res = []
    for k, rs in sorted(groups.items()):
        n = len(rs)
        res.append({"key": k, "n": n, "expectancy_r": round(sum(rs) / n, 3),
                    "win_rate": round(sum(1 for x in rs if x > 0) / n, 3),
                    "total_r": round(sum(rs), 2), "reliable": n >= MIN_TRADES_MEANINGFUL})
    return res


def daily_guard(today_r: list, max_daily_loss_r: float = 3.0, max_trades: int = 5,
                max_consec_losses: int = 3) -> dict:
    """
    نگهبان روزانه بر اساس R معاملات امروز (به ترتیب بسته‌شدن).
    state: ok / warn / stop. stop یعنی «امروز دیگر معامله‌ی جدید باز نکن».
    """
    reasons, state = [], "ok"
    total = sum(today_r)
    consec = 0
    for x in reversed(today_r):
        if x < 0:
            consec += 1
        else:
            break
    if total <= -max_daily_loss_r:
        state = "stop"; reasons.append(f"ضرر امروز {total:.2f}R به سقف {max_daily_loss_r}R رسید")
    elif total <= -0.7 * max_daily_loss_r:
        state = "warn"; reasons.append(f"ضرر امروز {total:.2f}R نزدیک سقف {max_daily_loss_r}R است")
    if consec >= max_consec_losses:
        state = "stop"; reasons.append(f"{consec} ضرر پشت‌سرهم")
    if len(today_r) >= max_trades:
        state = "stop"; reasons.append(f"تعداد معاملات امروز به سقف {max_trades} رسید")
    elif len(today_r) == max_trades - 1 and state == "ok":
        state = "warn"; reasons.append("یک معامله تا سقف روزانه مانده")
    return {"state": state, "today_r": round(total, 2), "trades_today": len(today_r),
            "consecutive_losses": consec, "reasons": reasons}


def drawdown_guard(r_series: list, max_dd_r: float = 10.0) -> dict:
    """افت از اوج منحنی R تجمعی؛ اگر به سقف رسید یعنی توقف و بازبینی."""
    peak = cum = 0.0
    dd = 0.0
    for x in r_series:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    cur = peak - cum
    state = "stop" if cur >= max_dd_r else "warn" if cur >= 0.7 * max_dd_r else "ok"
    return {"state": state, "current_dd_r": round(cur, 2), "max_dd_r": round(dd, 2), "total_r": round(cum, 2)}


def json_safe(obj):
    """تبدیل خروجی آمار به چیزی که JSON قبول کند: inf/nan → None و tuple → list."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
