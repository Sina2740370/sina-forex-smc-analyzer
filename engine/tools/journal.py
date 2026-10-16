"""
ژورنال معاملات و نگهبان ریسک (خالص، بدون I/O).

هدف: به سینا نشان بدهد معاملات دستی خودش واقعاً لبه دارد یا نه، با همان آمار و صداقت بک‌تست
(امید ریاضی با بازه‌ی اطمینان، نه فقط درصد برد).
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from numbers import Integral, Real
from typing import Iterable, Optional

import pandas as pd

from ..backtest.runner import summarize
from .risk import trade_r

MIN_TRADES_MEANINGFUL = 30


def _finite_decimal(value, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f"{name} باید عددی معتبر باشد")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} باید عددی معتبر باشد") from None
    if not result.is_finite():
        raise ValueError(f"{name} باید متناهی باشد")
    return result


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"{name} باید عدد صحیح و مثبت باشد")
    return int(value)


def _rounded(value: Decimal, places: str = "0.01") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def normalize(rows: Iterable[dict]) -> list:
    """هر ردیف: symbol, side, entry, sl, exit, close_time (اختیاری: open_time, tag, note)."""
    out = []
    for r in rows:
        rr = trade_r(r["side"], r["entry"], r["sl"], r["exit"])
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
    values = [_finite_decimal(value, "today_r") for value in today_r]
    loss_limit = _finite_decimal(max_daily_loss_r, "max_daily_loss_r")
    if loss_limit <= 0:
        raise ValueError("max_daily_loss_r باید مثبت باشد")
    max_trades = _positive_int(max_trades, "max_trades")
    max_consec_losses = _positive_int(max_consec_losses, "max_consec_losses")

    reasons, state = [], "ok"
    total = sum(values, Decimal("0"))
    consec = 0
    for value in reversed(values):
        if value < 0:
            consec += 1
        else:
            break
    if total <= -loss_limit:
        state = "stop"
        reasons.append(f"ضرر امروز {total:.2f}R به سقف {loss_limit}R رسید")
    elif total <= -Decimal("0.7") * loss_limit:
        state = "warn"
        reasons.append(f"ضرر امروز {total:.2f}R نزدیک سقف {loss_limit}R است")
    if consec >= max_consec_losses:
        state = "stop"
        reasons.append(f"{consec} ضرر پشت‌سرهم")
    if len(values) >= max_trades:
        state = "stop"
        reasons.append(f"تعداد معاملات امروز به سقف {max_trades} رسید")
    elif len(values) == max_trades - 1 and state == "ok":
        state = "warn"
        reasons.append("یک معامله تا سقف روزانه مانده")
    return {
        "state": state,
        "today_r": _rounded(total),
        "trades_today": len(values),
        "consecutive_losses": consec,
        "reasons": reasons,
    }


def drawdown_guard(r_series: list, max_dd_r: float = 10.0) -> dict:
    """افت از اوج منحنی R تجمعی؛ اگر به سقف رسید یعنی توقف و بازبینی."""
    values = [_finite_decimal(value, "r_series") for value in r_series]
    limit = _finite_decimal(max_dd_r, "max_dd_r")
    if limit <= 0:
        raise ValueError("max_dd_r باید مثبت باشد")

    peak = cum = dd = Decimal("0")
    for value in values:
        cum += value
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    cur = peak - cum
    state = (
        "stop" if cur >= limit
        else "warn" if cur >= Decimal("0.7") * limit
        else "ok"
    )
    return {
        "state": state,
        "current_dd_r": _rounded(cur),
        "max_dd_r": _rounded(dd),
        "total_r": _rounded(cum),
    }


def json_safe(obj):
    """تبدیل خروجی آمار به چیزی که JSON قبول کند: inf/nan → None و tuple → list."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
