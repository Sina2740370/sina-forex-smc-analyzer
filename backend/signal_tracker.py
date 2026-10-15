"""
رصد خودکار سیگنال‌ها: چک کردن TP و SL
+ Timeout برای سیگنال‌های گیر افتاده
+ فرمت صحیح طلا
+ فیلتر همبستگی ارزی (جلوگیری از باخت هم‌زمان روی نمادهای هم‌ارز)
"""
import logging
import math
from datetime import datetime, timezone, timedelta

from .config import PIP_SIZE, SPREAD
from .data_fetcher import fetch_ohlcv
from .database import (
    get_open_signals,
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
    pip = PIP_SIZE[symbol]
    if pip == 0:
        return 0.0
    return round(price_diff / pip, 1)


def _apply_spread(symbol: str, price: float, direction: str) -> float:
    """اسپرد رو روی قیمت اعمال می‌کنه (شبیه‌سازی واقعی)"""
    spread_pips = SPREAD[symbol]
    pip = PIP_SIZE[symbol]
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

    # سیگنال‌ها فقط پس از دریافت کندل بسته می‌شوند؛ PnL صفر برای انقضا گمراه‌کننده است.
    # گروه‌بندی بر اساس نماد
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
    if symbol not in PIP_SIZE or symbol not in SPREAD:
        logger.error("نماد %s در تنظیمات پیپ/اسپرد تعریف نشده؛ سیگنال %s باز می‌ماند", symbol, sig.get("id"))
        return

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

    # Use the same half-spread for entry, executable TP/SL triggers, and expiry.
    effective_entry = _apply_spread(symbol, entry, direction)
    half_spread = SPREAD.get(symbol, 1.0) * PIP_SIZE.get(symbol, 0.0001) / 2
    expires_at = created_at + timedelta(hours=MAX_SIGNAL_LIFETIME_HOURS)

    # Parse defensively and only consume fully closed 15m candles. Yahoo may
    # return a forming candle whose high/low can still repaint before close.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    valid_candles = []
    for candle in candles:
        try:
            candle_time = _parse_dt(candle["time"])
            if candle_time + timedelta(minutes=15) > now_utc:
                continue
            o, h, l, close = (float(candle[k]) for k in ("open", "high", "low", "close"))
            if not all(math.isfinite(v) for v in (o, h, l, close)):
                raise ValueError("non-finite OHLC")
            if h < max(o, close) or l > min(o, close) or h < l:
                raise ValueError("inconsistent OHLC range")
            valid_candles.append((candle_time, candle))
        except Exception as e:
            logger.warning("نادیده گرفتن کندل نامعتبر برای %s: %s", symbol, e)

    if not valid_candles:
        logger.warning("کندل بسته‌شده‌ای برای رصد %s موجود نیست", symbol)
        return

    latest_time = max(candle_time for candle_time, _ in valid_candles)
    latest_close_time = latest_time + timedelta(minutes=15)
    age_minutes = (now_utc - latest_close_time).total_seconds() / 60.0
    if age_minutes > 45:
        # Do not mark expired signals to market using an old cached quote/bar.
        logger.warning("رد داده‌ی کهنه برای رصد %s: آخرین کندل بسته‌شده %.1f دقیقه قدیمی است",
                       symbol, age_minutes)
        return

    # مرور کندل‌های بسته‌شده به ترتیب زمانی؛ پس از انقضا با قیمت بازار می‌بندیم.
    for candle_time, c in sorted(valid_candles, key=lambda item: item[0]):
        if candle_time <= created_at:
            continue

        # OHLC cannot reveal whether TP/SL happened before expiry inside a bar
        # that straddles the deadline. For risk safety, count a touched SL as LOSS,
        # never award a TP from that ambiguous bar, and otherwise mark-to-market.
        bar_close_time = candle_time + timedelta(minutes=15)
        high = float(c["high"])
        low = float(c["low"])
        open_price = float(c["open"])
        # Match the backtest's conservative gap model: stop-market orders fill
        # at the executable opening price when the market opens beyond the SL.
        open_exit = open_price - half_spread if direction == "BUY" else open_price + half_spread
        stop_gap = open_exit <= sl if direction == "BUY" else open_exit >= sl

        if bar_close_time >= expires_at:
            # Only a bar that overlaps the deadline is ambiguous. If it starts
            # after expiry, ignore its high/low and mark-to-market at its close.
            if candle_time < expires_at:
                stop_touched = stop_gap or (
                    low - half_spread <= sl if direction == "BUY"
                    else high + half_spread >= sl
                )
                if stop_touched:
                    exit_price = open_exit if stop_gap else sl
                    pnl_price = (
                        exit_price - effective_entry if direction == "BUY"
                        else effective_entry - exit_price
                    )
                    pnl = _to_pips(symbol, pnl_price)
                    close_signal(sig_id, exit_price, pnl, "LOSS")
                    logger.info("LOSS at expiry boundary: %s (%+.1f pips)", symbol, pnl)
                    return

            close_mid = float(c["close"])
            exit_price = close_mid - half_spread if direction == "BUY" else close_mid + half_spread
            pnl_price = exit_price - effective_entry if direction == "BUY" else effective_entry - exit_price
            pnl_pips = _to_pips(symbol, pnl_price)
            close_signal(sig_id, exit_price, pnl_pips, "EXPIRED")
            logger.info("EXPIRED: %s (%+.1f pips, market mark-to-market)", symbol, pnl_pips)
            return

        if direction == "BUY":
            # اول SL (محافظه‌کارانه); opening gaps fill at the worse executable open.
            if stop_gap or low - half_spread <= sl:
                exit_price = open_exit if stop_gap else sl
                pnl = _to_pips(symbol, exit_price - effective_entry)
                close_signal(sig_id, exit_price, pnl, "LOSS")
                logger.info(f"❌ LOSS: {symbol} {pnl:+.1f} pips")
                return
            if high - half_spread >= tp:
                pnl = _to_pips(symbol, tp - effective_entry)
                close_signal(sig_id, tp, pnl, "WIN")
                logger.info(f"✅ WIN: {symbol} {pnl:+.1f} pips")
                return

        elif direction == "SELL":
            # اول SL; opening gaps fill at the worse executable open.
            if stop_gap or high + half_spread >= sl:
                exit_price = open_exit if stop_gap else sl
                pnl = _to_pips(symbol, effective_entry - exit_price)
                close_signal(sig_id, exit_price, pnl, "LOSS")
                logger.info(f"❌ LOSS: {symbol} {pnl:+.1f} pips")
                return
            if low + half_spread <= tp:
                pnl = _to_pips(symbol, effective_entry - tp)
                close_signal(sig_id, tp, pnl, "WIN")
                logger.info(f"✅ WIN: {symbol} {pnl:+.1f} pips")
                return


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
