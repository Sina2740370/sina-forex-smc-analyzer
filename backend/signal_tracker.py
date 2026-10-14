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

    # Parse defensively before sorting: one malformed provider timestamp must not
    # abort tracking for this signal or the surrounding scan.
    valid_candles = []
    for candle in candles:
        try:
            valid_candles.append((_parse_dt(candle["time"]), candle))
        except Exception as e:
            logger.warning("نادیده گرفتن زمان کندل نامعتبر برای %s: %s", symbol, e)

    # مرور کندل‌ها به ترتیب زمانی؛ پس از انقضا با قیمت بازار می‌بندیم.
    for candle_time, c in sorted(valid_candles, key=lambda item: item[0]):
        if candle_time <= created_at:
            continue

        # OHLC cannot reveal whether TP/SL happened before expiry inside a bar
        # that straddles the deadline. For risk safety, count a touched SL as LOSS,
        # never award a TP from that ambiguous bar, and otherwise mark-to-market.
        bar_close_time = candle_time + timedelta(minutes=15)
        high = float(c["high"])
        low = float(c["low"])
        if candle_time < expires_at <= bar_close_time:
            stop_touched = (
                low - half_spread <= sl if direction == "BUY"
                else high + half_spread >= sl
            )
            if stop_touched:
                pnl = _to_pips(symbol, sl - effective_entry if direction == "BUY" else effective_entry - sl)
                close_signal(sig_id, sl, pnl, "LOSS")
                logger.info("LOSS at expiry boundary: %s (%+.1f pips)", symbol, pnl)
                return

            close_mid = float(c["close"])
            exit_price = close_mid - half_spread if direction == "BUY" else close_mid + half_spread
            pnl_price = exit_price - effective_entry if direction == "BUY" else effective_entry - exit_price
            pnl_pips = _to_pips(symbol, pnl_price)
            close_signal(sig_id, exit_price, pnl_pips, "EXPIRED")
            logger.info("EXPIRED: %s (%+.1f pips, market mark-to-market)", symbol, pnl_pips)
            return

        # Candles fully before expiry can trigger either SL or TP.
        if bar_close_time > expires_at:
            continue

        if direction == "BUY":
            # اول SL (محافظه‌کارانه)
            if low - half_spread <= sl:
                pnl = _to_pips(symbol, sl - effective_entry)
                close_signal(sig_id, sl, pnl, "LOSS")
                logger.info(f"❌ LOSS: {symbol} {pnl:+.1f} pips")
                return
            if high - half_spread >= tp:
                pnl = _to_pips(symbol, tp - effective_entry)
                close_signal(sig_id, tp, pnl, "WIN")
                logger.info(f"✅ WIN: {symbol} {pnl:+.1f} pips")
                return

        elif direction == "SELL":
            # اول SL
            if high + half_spread >= sl:
                pnl = _to_pips(symbol, effective_entry - sl)
                close_signal(sig_id, sl, pnl, "LOSS")
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
