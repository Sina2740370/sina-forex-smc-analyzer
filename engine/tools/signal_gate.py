"""
دروازه‌ی صدور سیگنال (خالص، بدون I/O): جلوگیری از تکرار همان معامله و باخت‌های هم‌تم.

این‌ها کنترل ریسک‌اند، نه ادعای لبه: تعداد سیگنال‌های تکراری/همبسته را کم می‌کنند.
قواعد (به ترتیب):
  open_same_symbol : هر سیگنال باز (با هر سنی) روی همان نماد → سیگنال تازه نمی‌آید
  cooldown         : همان نماد و جهت تازه بسته شده (بعد از ضرر ۸ ساعت، بعد از سود/انقضا ۴ ساعت)
  loss_theme       : در ۱۲ ساعت اخیر ضرر داده‌ایم و سیگنال تازه روی همان ارز هم‌جهت است
                     (مثلاً بعد از ضرر خرید USDCAD یعنی «لانگ USD»، خرید USDJPY هم لانگ USD است)
  failed_zone      : بعد از LOSS، اگر ورود تازه خیلی نزدیک ورود قبلی (نسبت به ریسک قبلی) باشد
                     تا FAILED_ZONE_H ساعت همان ستاپ دوباره صادر نمی‌شود
  symbol_streak    : بعد از STREAK_LOSSES باخت روی یک نماد در پنجره STREAK_BLOCK_H، آن نماد بلاک می‌شود
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

COOLDOWN_H = 4
LOSS_COOLDOWN_H = 8
LOSS_THEME_H = 12

# بلاک منطقه شکست‌خورده: جلوگیری از ورود دوباره به همان ناحیه بعد از باخت
FAILED_ZONE_H = float(os.getenv("FAILED_ZONE_H", "48"))
ZONE_TOL = float(os.getenv("ZONE_TOL", "1.0"))

# بلاک رگبار باخت روی یک نماد
STREAK_BLOCK_H = float(os.getenv("STREAK_BLOCK_H", "24"))
STREAK_LOSSES = int(os.getenv("STREAK_LOSSES", "2"))

# حداکثر تعداد پوزیشن‌های باز هم‌ارز
MAX_CURRENCY_EXPOSURE = int(os.getenv("MAX_CURRENCY_EXPOSURE", "2"))


def is_market_closed(now: datetime) -> bool:
    """تعطیلی آخر هفته‌ی بازار فارکس: جمعه بعد از ۱۷:۰۰ نیویورک تا یکشنبه ۱۸:۰۰ نیویورک.

    قبلاً زمان‌بند ۲۴/۷ اجرا می‌شد و شنبه/یکشنبه با داده‌ی جمعه کار می‌کرد
    (سیگنال روی قیمت مرده). الان سیگنال‌دهی در تعطیلات بسته است."""
    t = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        ny = t.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        ny = t.astimezone(timezone.utc)  # fallback
    wd = ny.weekday()   # Mon=0 … Fri=4, Sat=5, Sun=6
    if wd == 5:                          # شنبه
        return True
    if wd == 4 and ny.hour >= 17:        # جمعه بعد از رول‌اور
        return True
    if wd == 6 and ny.hour < 18:         # یکشنبه قبل از باز شدن
        return True
    return False


def is_rollover_period(now: datetime) -> bool:
    """ساعات تسویه بازار فارکس و بازشدن شدید اسپرد: 17:00 نیویورک تا یک ساعت بعد.

    قبلاً ساعت 22:00 UTC هاردکد شده بود که فقط زمستان درست است؛
    در طول DST (حدوداً اسفند تا آبان) رول‌اور واقعی 21:00 UTC است.
    الان با تبدیل به America/New_York درست کار می‌کند (هم‌راستا با kill_zone)."""
    t = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        ny_hour = t.astimezone(ZoneInfo("America/New_York")).hour
    except Exception:
        ny_hour = t.astimezone(timezone.utc).hour  # fallback: رفتار قبلی
    return ny_hour == 17


def norm_symbol(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "").replace("-", "").replace("=X", "")
    return "XAUUSD" if s in ("GC=F", "GC", "XAU") else s


def exposure(symbol: str, direction: str) -> dict:
    """جهت قرارگیری ارزی: BUY EURUSD = {EUR:+1, USD:-1}."""
    s = norm_symbol(symbol)
    if len(s) >= 6:
        base, quote = s[:3], s[3:6]
    else:
        base, quote = s, "USD"
    sign = 1 if direction.upper() == "BUY" else -1
    return {base: sign, quote: -sign}


def _dt(t) -> datetime:
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def evaluate(symbol: str, direction: str, open_signals: list, closed_recent: list,
             now: Optional[datetime] = None,
             entry: Optional[float] = None) -> Optional[str]:
    """None یعنی مجاز؛ وگرنه کد دلیل رد.

    entry: قیمت ورود پیشنهادی سیگنال تازه (برای قاعده failed_zone). اختیاری برای سازگاری عقب‌رو.
    """
    now = _dt(now) if now else datetime.now(timezone.utc)
    sym = norm_symbol(symbol)
    direction = direction.upper()

    # --- market_closed: تعطیلی آخر هفته‌ی فارکس ---
    if is_market_closed(now):
        return "market_closed"

    # --- rollover_spread_guard: محافظت در برابر باز شدن اسپرد در ساعات Rollover ---
    if is_rollover_period(now):
        return "rollover_spread_guard"

    for s in open_signals:
        if norm_symbol(s["symbol"]) == sym:
            return "open_same_symbol"

    # --- portfolio_currency_limit: فیلتر تمرکز بیش از حد ریسک روی یک ارز ---
    mine = exposure(sym, direction)
    cur_counts = {}
    for osig in open_signals:
        oexp = exposure(osig["symbol"], osig.get("direction", "BUY"))
        for c in oexp:
            cur_counts[c] = cur_counts.get(c, 0) + 1

    for c in mine:
        if cur_counts.get(c, 0) >= MAX_CURRENCY_EXPOSURE:
            return "portfolio_currency_limit"

    for s in closed_recent:
        if norm_symbol(s["symbol"]) != sym or s["direction"].upper() != direction:
            continue
        hours = (LOSS_COOLDOWN_H if (s["status"] == "LOSS"
                 or float(s.get("pnl_pips") or 0) < 0) else COOLDOWN_H)
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

    # --- failed_zone: ورود دوباره به همان ناحیه بعد از LOSS ---
    if entry is not None:
        for s in closed_recent:
            if s["status"] != "LOSS" or not s.get("closed_at"):
                continue
            if norm_symbol(s["symbol"]) != sym or s["direction"].upper() != direction:
                continue
            if _dt(s["closed_at"]) <= now - timedelta(hours=FAILED_ZONE_H):
                continue
            old_entry = s.get("entry")
            old_sl = s.get("sl")
            if old_entry is None or old_sl is None:
                continue
            risk = abs(float(old_entry) - float(old_sl))
            if risk <= 0:
                continue
            if abs(float(entry) - float(old_entry)) <= ZONE_TOL * risk:
                return "failed_zone"

    # --- symbol_streak: چند باخت پشت‌سرهم روی یک نماد ---
    losses_in_window = 0
    for s in closed_recent:
        if s["status"] != "LOSS" or not s.get("closed_at"):
            continue
        if norm_symbol(s["symbol"]) != sym:
            continue
        if _dt(s["closed_at"]) <= now - timedelta(hours=STREAK_BLOCK_H):
            continue
        losses_in_window += 1
    if losses_in_window >= STREAK_LOSSES:
        return "symbol_streak"

    return None
