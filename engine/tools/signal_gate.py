"""
دروازه‌ی صدور سیگنال (خالص، بدون I/O): جلوگیری از تکرار همان معامله و باخت‌های هم‌تم.

این‌ها کنترل ریسک‌اند، نه ادعای لبه: تعداد سیگنال‌های تکراری/همبسته را کم می‌کنند.
قواعد (به ترتیب):
  open_same_symbol : هر سیگنال باز (با هر سنی) روی همان نماد → سیگنال تازه نمی‌آید
  cooldown         : همان نماد و جهت تازه بسته شده (بعد از ضرر ۸ ساعت، بعد از سود/انقضا ۴ ساعت)
  loss_theme       : در ۱۲ ساعت اخیر ضرر داده‌ایم و سیگنال تازه روی همان ارز هم‌جهت است
                     (مثلاً بعد از ضرر خرید USDCAD یعنی «لانگ USD»، خرید USDJPY هم لانگ USD است)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

COOLDOWN_H = 4
LOSS_COOLDOWN_H = 8
LOSS_THEME_H = 12


def norm_symbol(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "").replace("-", "").replace("=X", "")
    return "XAUUSD" if s in ("GC=F", "GC", "XAU") else s


def currency_pair(symbol: str) -> tuple[str, str]:
    """Pair استانداردشده؛ نماد فیوچرز GC=F را برای تحلیل مواجهه به XAUUSD نگاشت می‌کند."""
    s = norm_symbol(symbol)
    return s[:3], s[3:6]


def exposure(symbol: str, direction: str) -> dict:
    """جهت قرارگیری ارزی: BUY EURUSD = {EUR:+1, USD:-1}."""
    s = norm_symbol(symbol)
    base, quote = s[:3], s[3:6]
    sign = 1 if direction.upper() == "BUY" else -1
    return {base: sign, quote: -sign}


def _dt(t) -> datetime:
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def evaluate(symbol: str, direction: str, open_signals: list, closed_recent: list,
             now: Optional[datetime] = None) -> Optional[str]:
    """None یعنی مجاز؛ وگرنه کد دلیل رد."""
    now = _dt(now) if now else datetime.utcnow()
    sym = norm_symbol(symbol)
    direction = direction.upper()

    for s in open_signals:
        if norm_symbol(s["symbol"]) == sym:
            return "open_same_symbol"

    for s in closed_recent:
        if norm_symbol(s["symbol"]) != sym or s["direction"].upper() != direction:
            continue
        hours = LOSS_COOLDOWN_H if s["status"] == "LOSS" else COOLDOWN_H
        if s.get("closed_at") and _dt(s["closed_at"]) > now - timedelta(hours=hours):
            return "cooldown"

    mine = exposure(sym, direction)
    for s in closed_recent:
        if s["status"] != "LOSS" or not s.get("closed_at"):
            continue
        if _dt(s["closed_at"]) <= now - timedelta(hours=LOSS_THEME_H):
            continue
        lost = exposure(s["symbol"], s["direction"])
        if any(mine.get(cur) == sign for cur, sign in lost.items()):
            return "loss_theme"
    return None
