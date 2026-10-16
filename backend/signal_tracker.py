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

from engine.runner import check_exit as engine_check_exit

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

    # گروه‌بندی بر اساس نماد؛ هر سیگنال به‌ترتیب زمانی بررسی می‌شود.
    # انقضا فقط وقتی ثبت می‌شود که قیمت قابل‌مشاهده برای خروج داشته باشیم.
    by_symbol = {}
    for sig in open_signals:
        by_symbol.setdefault(sig["symbol"], []).append(sig)

    for symbol, signals in by_symbol.items():
        try:
            candles = fetch_ohlcv(symbol, "15m", limit=500)
            if not candles:
                logger.warning(f"داده‌ای برای {symbol} موجود نیست")
                continue

            for sig in signals:
                _check_signal(sig, candles)

        except Exception as e:
            logger.error(f"خطا در بررسی {symbol}: {e}")

def _check_signal(sig: dict, candles: list):
    """
    بررسی یک سیگنال با فیلتر زمان
    فقط کندل‌های بعد از created_at بررسی می‌شن
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
        return

    # اسپرد روی ورود
    effective_entry = _apply_spread(symbol, entry, direction)

    # پس از مهلت ۷۲ ساعته، خروج با قیمت بازار و هزینه‌ی اسپرد ثبت می‌شود.
    expires_at = created_at + timedelta(hours=MAX_SIGNAL_LIFETIME_HOURS)
    half_spread = SPREAD.get(symbol, 1.0) * PIP_SIZE.get(symbol, 0.0001) / 2.0
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # مرور کندل‌ها به ترتیب زمانی؛ کندل 15m تا زمان بسته‌شدن کامل معتبر نیست.
    for c in candles:
        try:
            candle_time = _parse_dt(c["time"])
        except Exception:
            continue

        if candle_time <= created_at:
            continue
        if candle_time + timedelta(minutes=15) > now_utc:
            continue
        # Если данные начинаются уже после дедлайна, не проигрываем
        # TP/SL на свечах после срока жизни: используем ближайший видимый рынок.
        if candle_time >= expires_at:
            if candle_time - expires_at <= timedelta(minutes=15):
                market_price = float(c["open"])
            else:
                market_price = float(candles[-1]["close"])
                logger.warning(
                    f"История для точного истечения {symbol} неполная; "
                    "использован последний доступный close."
                )
            exit_price = market_price - half_spread if direction == "BUY" else market_price + half_spread
            pnl_diff = exit_price - effective_entry if direction == "BUY" else effective_entry - exit_price
            pnl = _to_pips(symbol, pnl_diff)
            close_signal(sig_id, exit_price, pnl, "EXPIRED")
            logger.info(f"⏰ EXPIRED: {symbol} {pnl:+.1f} pips")
            return

        high = float(c["high"])
        low = float(c["low"])
        side = 1 if direction == "BUY" else -1
        status, exit_price = engine_check_exit(side, float(sl), float(tp), high, low, half_spread)
        if status is not None:
            pnl_diff = exit_price - effective_entry if direction == "BUY" else effective_entry - exit_price
            pnl = _to_pips(symbol, pnl_diff)
            final_status = "LOSS" if status == "SL" else "WIN"
            close_signal(sig_id, exit_price, pnl, final_status)
            logger.info(
                f"{'❌ LOSS' if final_status == 'LOSS' else '✅ WIN'}: "
                f"{symbol} {pnl:+.1f} pips"
            )
            return

        # همان لحظه‌ی بسته‌شدن کندل را ملاک انقضا می‌گیریم؛ این با موتور بک‌تست
        # هم‌راستاست و از یک کندل کامل اضافه پس از deadline جلوگیری می‌کند.
        bar_close = candle_time + timedelta(minutes=15)
        if bar_close >= expires_at:
            market_price = float(c["close"])
            exit_price = market_price - half_spread if direction == "BUY" else market_price + half_spread
            pnl_diff = exit_price - effective_entry if direction == "BUY" else effective_entry - exit_price
            pnl = _to_pips(symbol, pnl_diff)
            close_signal(sig_id, exit_price, pnl, "EXPIRED")
            logger.info(f"⏰ EXPIRED: {symbol} {pnl:+.1f} pips")
            return


def signal_gate(symbol: str, direction: str):
    """
    دروازه‌ی صدور: None یعنی مجاز؛ وگرنه کد دلیل ('open_same_symbol' | 'cooldown' | 'loss_theme').
    کاملاً مبتنی بر دیتابیس است (با ری‌استارت Render از بین نمی‌رود) و سن سیگنال باز را نادیده نمی‌گیرد.
    """
    from engine.tools.signal_gate import evaluate, LOSS_THEME_H
    return evaluate(symbol, direction, get_open_signals(),
                    get_recent_closed_signals(hours=LOSS_THEME_H))


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
#   is_duplicate_signal بالا جلوشون رو نمی‌گیره)
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
