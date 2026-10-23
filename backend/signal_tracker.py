"""
رصد خودکار سیگنال‌ها: چک کردن TP و SL
+ Timeout برای سیگنال‌های گیر افتاده
+ فرمت صحیح طلا
+ فیلتر همبستگی ارزی (جلوگیری از باخت هم‌زمان روی نمادهای هم‌ارز)
"""
import logging
from datetime import datetime, timezone, timedelta

from .config import PIP_SIZE, SPREAD
from .data_fetcher import fetch_ohlcv
from .database import (
    get_open_signals,
    get_recent_closed_signals,
    get_recent_signals_for_symbol,
    close_signal,
)

logger = logging.getLogger(__name__)

# حداکثر عمر سیگنال (ساعت) - بعدش EXPIRED می‌شه
MAX_SIGNAL_LIFETIME_HOURS = 72


def _parse_dt(t):
    """تبدیل هر فرمت زمانی به naive UTC datetime"""
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def _to_pips(symbol: str, price_diff: float) -> float:
    """تبدیل اختلاف قیمت به پیپ"""
    pip = PIP_SIZE.get(symbol, 0.0001)
    if pip == 0:
        return 0.0
    return round(price_diff / pip, 1)


def _apply_spread(symbol: str, price: float, direction: str) -> float:
    """اسپرد رو روی قیمت اعمال می‌کنه (شبیه‌سازی واقعی)"""
    spread_pips = SPREAD.get(symbol, 1.0)
    pip = PIP_SIZE.get(symbol, 0.0001)
    spread_price = spread_pips * pip

    if direction == "BUY":
        return price + spread_price / 2
    return price - spread_price / 2


def check_open_signals():
    """
    همه سیگنال‌های باز رو چک می‌کنه
    - TP خورد؟ → WIN
    - SL خورد؟ → LOSS
    - عمرش تموم شد؟ → EXPIRED
    """
    open_signals = get_open_signals()
    if not open_signals:
        return

    logger.info(f"🔍 بررسی {len(open_signals)} سیگنال باز...")

    closed_ids = set()
    by_symbol = {}
    for sig in open_signals:
        by_symbol.setdefault(sig["symbol"], []).append(sig)

    # ۱) اول کندل‌ها (TP/SL) — id بسته‌شده‌ها ثبت می‌شود
    for symbol, signals in by_symbol.items():
        try:
            candles = fetch_ohlcv(symbol, "15m", limit=500)
            if not candles:
                logger.warning(f"داده‌ای برای {symbol} موجود نیست")
                continue

            for sig in signals:
                if _check_signal(sig, candles):
                    closed_ids.add(sig["id"])

        except Exception as e:
            logger.error(f"خطا در بررسی {symbol}: {e}")
            # سیگنال‌های این نماد همچنان در مرحله انقضا بررسی می‌شوند

    # ۲) فقط سیگنال‌های هنوز بازمانده برای EXPIRED
    remaining = [s for s in open_signals if s["id"] not in closed_ids]
    _check_expired_signals(remaining)


def _check_expired_signals(signals: list):
    """سیگنال‌های قدیمی رو EXPIRED می‌کنه"""
    now = datetime.utcnow()
    threshold = now - timedelta(hours=MAX_SIGNAL_LIFETIME_HOURS)

    for sig in signals:
        try:
            created = _parse_dt(sig["created_at"])
            if created < threshold:
                close_signal(
                    sig["id"],
                    sig["entry"],
                    0.0,
                    "EXPIRED",
                )
                logger.info(
                    f"⏰ EXPIRED: {sig['symbol']} "
                    f"(عمر: {MAX_SIGNAL_LIFETIME_HOURS} ساعت)"
                )
        except Exception as e:
            logger.error(f"خطا در چک EXPIRED {sig['id']}: {e}")


def _check_signal(sig: dict, candles: list) -> bool:
    """
    بررسی یک سیگنال با فیلتر زمان
    فقط کندل‌های بعد از created_at بررسی می‌شن
    در صورت بستن سیگنال True و در غیر این صورت False برمی‌گرداند.
    """
    symbol = sig["symbol"]
    direction = sig["direction"]
    entry = sig["entry"]
    sl = sig["sl"]
    tp = sig["tp"]
    sig_id = sig["id"]

    # زمان ساخته شدن
    try:
        created_at = _parse_dt(sig["created_at"])
    except Exception as e:
        logger.error(f"خطا در پارس زمان سیگنال {sig_id}: {e}")
        return False

    # اسپرد روی ورود
    effective_entry = _apply_spread(symbol, entry, direction)
    # خروج روی bid/ask مثل بک‌تست: BUY روی bid=mid-half، SELL روی ask=mid+half
    half = SPREAD.get(symbol, 1.0) * PIP_SIZE.get(symbol, 0.0001) / 2

    # مرور کندل‌ها
    for c in candles:
        try:
            candle_time = _parse_dt(c["time"])
        except Exception:
            continue

        # فقط کندل‌های بعد از ساخته شدن سیگنال
        if candle_time <= created_at:
            continue

        high = c["high"]
        low = c["low"]

        if direction == "BUY":
            # اول SL (محافظه‌کارانه)
            if low - half <= sl:
                pnl = _to_pips(symbol, sl - effective_entry)
                close_signal(sig_id, sl, pnl, "LOSS")
                logger.info(f"❌ LOSS: {symbol} {pnl:+.1f} pips")
                return True
            if high - half >= tp:
                pnl = _to_pips(symbol, tp - effective_entry)
                close_signal(sig_id, tp, pnl, "WIN")
                logger.info(f"✅ WIN: {symbol} {pnl:+.1f} pips")
                return True

        elif direction == "SELL":
            # اول SL
            if high + half >= sl:
                pnl = _to_pips(symbol, effective_entry - sl)
                close_signal(sig_id, sl, pnl, "LOSS")
                logger.info(f"❌ LOSS: {symbol} {pnl:+.1f} pips")
                return True
            if low + half <= tp:
                pnl = _to_pips(symbol, effective_entry - tp)
                close_signal(sig_id, tp, pnl, "WIN")
                logger.info(f"✅ WIN: {symbol} {pnl:+.1f} pips")
                return True

    return False


def signal_gate(symbol: str, direction: str, entry: float = None):
    """
    دروازه‌ی صدور: None یعنی مجاز؛ وگرنه کد دلیل
    ('open_same_symbol' | 'cooldown' | 'loss_theme' | 'failed_zone' | 'symbol_streak').
    کاملاً مبتنی بر دیتابیس است (با ری‌استارت Render از بین نمی‌رود) و سن سیگنال باز را نادیده نمی‌گیرد.
    entry: قیمت ورود پیشنهادی برای قاعده failed_zone.
    """
    from engine.tools.signal_gate import (
        evaluate, LOSS_THEME_H, FAILED_ZONE_H, STREAK_BLOCK_H,
    )
    hours = max(LOSS_THEME_H, FAILED_ZONE_H, STREAK_BLOCK_H)
    return evaluate(
        symbol, direction,
        get_open_signals(),
        get_recent_closed_signals(hours=hours),
        entry=entry,
    )


def is_duplicate_signal(symbol: str, direction: str,
                         hours: int = 4) -> bool:
    """جلوگیری از تکرار سیگنال مشابه در بازه کوتاه (فقط همون نماد)"""
    recent = get_recent_signals_for_symbol(symbol, hours=hours)
    for sig in recent:
        if sig["direction"] == direction and sig["status"] == "OPEN":
            return True
    return False


# ============================================================
#  فیلتر همبستگی ارزی — رفع علت اصلی دو باخت هم‌زمان
#  (GBPJPY و USDJPY هر دو JPY دارن، ولی نماد متفاوته پس
#   is_duplicate_signal بالا جلشون رو نمی‌گیره)
# ============================================================

def extract_currencies(symbol: str):
    """مثلا 'GBPJPY' یا 'GBP/JPY' -> ('GBP', 'JPY')"""
    s = symbol.replace("/", "").replace("-", "").upper()
    return s[:3], s[3:6]


def has_correlated_open_signal(symbol: str):
    """
    چک می‌کند آیا سیگنال بازِ دیگری روی نمادی با ارز پایه یا کوت مشترک
    وجود دارد (مثلا GBPJPY وقتی USDJPY باز است).
    برمی‌گرداند: dict سیگنال تداخل‌دار، یا None
    """
    base, quote = extract_currencies(symbol)
    for sig in get_open_signals():
        if sig["symbol"] == symbol:
            continue
        e_base, e_quote = extract_currencies(sig["symbol"])
        if base in (e_base, e_quote) or quote in (e_base, e_quote):
            return sig
    return None
