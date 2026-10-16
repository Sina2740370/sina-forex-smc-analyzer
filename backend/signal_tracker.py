"""
رصد خودکار سیگنال‌ها: چک کردن TP و SL
+ Timeout برای سیگنال‌های گیر افتاده
+ فرمت صحیح طلا
+ فیلتر همبستگی ارزی (جلوگیری از باخت هم‌زمان روی نمادهای هم‌ارز)
"""
import logging
from datetime import datetime, timezone, timedelta

from .config import MAX_ENTRY_DELAY_MINUTES, MAX_ENTRY_SLIP_R, PIP_SIZE, SPREAD
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


def _config_symbol(symbol: str) -> str:
    """Map engine-style symbols to the backend's configured Yahoo symbol keys."""
    if symbol in PIP_SIZE:
        return symbol
    if symbol == "XAUUSD" and "GC=F" in PIP_SIZE:
        return "GC=F"
    yahoo_symbol = f"{symbol}=X"
    return yahoo_symbol if yahoo_symbol in PIP_SIZE else symbol


def _to_pips(symbol: str, price_diff: float) -> float:
    """تبدیل اختلاف قیمت به پیپ با کلید canonical تنظیمات."""
    config_symbol = _config_symbol(symbol)
    pip = PIP_SIZE.get(config_symbol, 0.0001)
    if pip == 0:
        return 0.0
    return round(price_diff / pip, 1)


def _apply_spread(symbol: str, price: float, direction: str) -> float:
    """Apply half-spread to the executable entry side."""
    config_symbol = _config_symbol(symbol)
    spread_pips = SPREAD.get(config_symbol, 1.0)
    pip = PIP_SIZE.get(config_symbol, 0.0001)
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
    """Track the signal with the same next-eligible-bar entry and exit rules as backtest."""
    symbol = sig["symbol"]
    direction = sig["direction"]
    sig_id = sig["id"]
    try:
        entry = float(sig["entry"])
        sl = float(sig["sl"])
        tp = float(sig["tp"])
        created_at = _parse_dt(sig["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("سیگنال %s داده‌ی ورود/زمان نامعتبر دارد: %s", sig_id, exc)
        return

    side = 1 if direction == "BUY" else -1
    config_symbol = _config_symbol(symbol)
    half_spread = SPREAD.get(config_symbol, 1.0) * PIP_SIZE.get(config_symbol, 0.0001) / 2.0
    signal_risk = (entry - sl) * side
    expires_at = created_at + timedelta(hours=MAX_SIGNAL_LIFETIME_HOURS)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    parsed_candles = []
    for candle in candles:
        try:
            parsed_candles.append((_parse_dt(candle["time"]), candle))
        except Exception:
            continue
    parsed_candles.sort(key=lambda item: item[0])
    effective_entry = None

    for candle_time, candle in parsed_candles:
        # The signal is not executable on a candle that began before it was created.
        if candle_time <= created_at:
            continue
        if candle_time + timedelta(minutes=15) > now_utc:
            continue

        market_open = float(candle.get("open", entry))
        if effective_entry is None:
            if candle_time >= expires_at:
                exit_price = (
                    market_open - half_spread if direction == "BUY"
                    else market_open + half_spread
                )
                close_signal(sig_id, exit_price, 0.0, "REJECTED")
                logger.info("REJECTED without entry: %s; no eligible bar before expiry", symbol)
                return

            if candle_time - created_at > timedelta(minutes=MAX_ENTRY_DELAY_MINUTES):
                exit_price = (
                    market_open - half_spread if direction == "BUY"
                    else market_open + half_spread
                )
                close_signal(sig_id, exit_price, 0.0, "EXPIRED")
                logger.warning(
                    "%s: entry rejected because the first eligible bar arrived after %s minutes",
                    symbol, MAX_ENTRY_DELAY_MINUTES,
                )
                return

            if (
                signal_risk <= 0
                or abs(market_open - entry) > MAX_ENTRY_SLIP_R * signal_risk
            ):
                exit_price = (
                    market_open - half_spread if direction == "BUY"
                    else market_open + half_spread
                )
                close_signal(sig_id, exit_price, 0.0, "EXPIRED")
                logger.warning(
                    "%s: entry rejected; next eligible open exceeded max slippage or signal risk was invalid",
                    symbol,
                )
                return

            effective_entry = _apply_spread(symbol, market_open, direction)
            if (effective_entry - sl) * side <= 0 or (tp - effective_entry) * side <= 0:
                close_signal(sig_id, effective_entry, 0.0, "REJECTED")
                logger.warning("%s: entry rejected because executable price crossed SL/TP", symbol)
                return

        # Once entered, never count TP/SL events from candles after the 72h deadline.
        if candle_time >= expires_at:
            if candle_time - expires_at <= timedelta(minutes=15):
                market_price = market_open
            else:
                market_price = float(parsed_candles[-1][1]["close"])
                logger.warning(
                    "%s: incomplete history at expiry; using last available close as an estimate",
                    symbol,
                )
            exit_price = (
                market_price - half_spread if direction == "BUY"
                else market_price + half_spread
            )
            pnl_diff = (
                exit_price - effective_entry if direction == "BUY"
                else effective_entry - exit_price
            )
            pnl = _to_pips(symbol, pnl_diff)
            close_signal(sig_id, exit_price, pnl, "EXPIRED")
            logger.info("⏰ EXPIRED: %s %+.1f pips", symbol, pnl)
            return

        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"]) if candle.get("open") is not None else None
        status, exit_price = engine_check_exit(
            side, sl, tp, high, low, half_spread, open_price
        )
        if status is not None:
            pnl_diff = (
                exit_price - effective_entry if direction == "BUY"
                else effective_entry - exit_price
            )
            pnl = _to_pips(symbol, pnl_diff)
            final_status = "LOSS" if status == "SL" else "WIN"
            close_signal(sig_id, exit_price, pnl, final_status)
            logger.info(
                "%s: %s %+.1f pips",
                "❌ LOSS" if final_status == "LOSS" else "✅ WIN",
                symbol, pnl,
            )
            return

        bar_close = candle_time + timedelta(minutes=15)
        if bar_close >= expires_at:
            market_price = float(candle["close"])
            exit_price = (
                market_price - half_spread if direction == "BUY"
                else market_price + half_spread
            )
            pnl_diff = (
                exit_price - effective_entry if direction == "BUY"
                else effective_entry - exit_price
            )
            pnl = _to_pips(symbol, pnl_diff)
            close_signal(sig_id, exit_price, pnl, "EXPIRED")
            logger.info("⏰ EXPIRED: %s %+.1f pips", symbol, pnl)
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
