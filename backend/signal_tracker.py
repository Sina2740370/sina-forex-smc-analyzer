"""
رصد خودکار سیگنال‌ها: چک کردن TP و SL
وقتی قیمت به TP یا SL رسید، سیگنال رو WIN یا LOSS می‌کنه
"""
import logging
from datetime import datetime

from .config import PIP_SIZE, SPREAD
from .data_fetcher import fetch_ohlcv
from .database import (
    get_open_signals,
    get_recent_signals_for_symbol,
    close_signal,
)

logger = logging.getLogger(__name__)


def _to_pips(symbol: str, price_diff: float) -> float:
    """تبدیل اختلاف قیمت به پیپ"""
    pip = PIP_SIZE.get(symbol, 0.0001)
    if pip == 0:
        return 0.0
    return round(price_diff / pip, 1)


def _apply_spread(symbol: str, price: float, direction: str) -> float:
    """
    اسپرد رو روی قیمت اعمال می‌کنه (برای شبیه‌سازی واقعی)

    در واقعیت:
    - BUY با قیمت Ask باز می‌شه (بالاتر)
    - SELL با قیمت Bid باز می‌شه (پایین‌تر)
    """
    spread_pips = SPREAD.get(symbol, 1.0)
    pip = PIP_SIZE.get(symbol, 0.0001)
    spread_price = spread_pips * pip

    if direction == "BUY":
        return price + spread_price / 2
    return price - spread_price / 2


def check_open_signals():
    """
    همه سیگنال‌های باز رو چک می‌کنه:
    - اگه TP خورد → WIN
    - اگه SL خورد → LOSS
    """
    open_signals = get_open_signals()
    if not open_signals:
        return

    logger.info(f"🔍 بررسی {len(open_signals)} سیگنال باز...")

    # گروه‌بندی بر اساس نماد (برای کم کردن درخواست‌ها)
    by_symbol = {}
    for sig in open_signals:
        by_symbol.setdefault(sig["symbol"], []).append(sig)

    for symbol, signals in by_symbol.items():
        try:
            candles = fetch_ohlcv(symbol, "15m", limit=10)
            if not candles:
                continue

            # بالاترین high و پایین‌ترین low در کندل‌های اخیر
            recent_high = max(c["high"] for c in candles)
            recent_low = min(c["low"] for c in candles)

            for sig in signals:
                _check_signal(sig, recent_high, recent_low, candles)

        except Exception as e:
            logger.error(f"خطا در بررسی {symbol}: {e}")


def _check_signal(sig: dict, recent_high: float,
                   recent_low: float, candles: list):
    """چک کردن یک سیگنال"""
    symbol = sig["symbol"]
    direction = sig["direction"]
    entry = sig["entry"]
    sl = sig["sl"]
    tp = sig["tp"]
    sig_id = sig["id"]

    # اسپرد رو روی entry اعمال کن (برای شبیه‌سازی واقعی)
    effective_entry = _apply_spread(symbol, entry, direction)

    if direction == "BUY":
        # بررسی SL اول (چون اگه SL بخوره، ضرره)
        if recent_low <= sl:
            pnl_price = sl - effective_entry
            pnl_pips = _to_pips(symbol, pnl_price)
            close_signal(sig_id, sl, pnl_pips, "LOSS")
            logger.info(f"❌ LOSS: {symbol} {pnl_pips:+.1f} pips")
            return

        # بررسی TP
        if recent_high >= tp:
            pnl_price = tp - effective_entry
            pnl_pips = _to_pips(symbol, pnl_price)
            close_signal(sig_id, tp, pnl_pips, "WIN")
            logger.info(f"✅ WIN: {symbol} {pnl_pips:+.1f} pips")
            return

    elif direction == "SELL":
        # بررسی SL اول
        if recent_high >= sl:
            pnl_price = effective_entry - sl
            pnl_pips = _to_pips(symbol, pnl_price)
            close_signal(sig_id, sl, pnl_pips, "LOSS")
            logger.info(f"❌ LOSS: {symbol} {pnl_pips:+.1f} pips")
            return

        # بررسی TP
        if recent_low <= tp:
            pnl_price = effective_entry - tp
            pnl_pips = _to_pips(symbol, pnl_price)
            close_signal(sig_id, tp, pnl_pips, "WIN")
            logger.info(f"✅ WIN: {symbol} {pnl_pips:+.1f} pips")
            return


def is_duplicate_signal(symbol: str, direction: str,
                         hours: int = 4) -> bool:
    """جلوگیری از تکرار سیگنال مشابه در بازه کوتاه"""
    recent = get_recent_signals_for_symbol(symbol, hours=hours)
    for sig in recent:
        if sig["direction"] == direction and sig["status"] == "OPEN":
            return True
    return False
