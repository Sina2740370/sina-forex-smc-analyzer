"""
زمان‌بند خودکار - نسخه Top-Down
اسکن 5 تایم‌فریم + رصد سیگنال‌ها
"""
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import (
    SYMBOLS, SCAN_INTERVAL_MINUTES, TIMEFRAMES
)
from .data_fetcher import fetch_topdown, get_cache_stats
from .analysis_engine import full_analysis
from .news_fetcher import fetch_news, is_news_safe
from .signal_tracker import check_open_signals, is_duplicate_signal
from .database import insert_signal

logger = logging.getLogger(__name__)

_scheduler = None
_last_scan = {}
_scan_stats = {
    "total_scans": 0,
    "last_scan_time": None,
    "signals_today": 0,
    "errors": 0,
    "timeframes": TIMEFRAMES,
}


async def scan_market():
    logger.info("=" * 55)
    logger.info("🔍 شروع اسکن Top-Down (5 تایم‌فریم)...")
    start_time = datetime.now(timezone.utc)

    news = await fetch_news()
    logger.info(f"📰 {len(news)} خبر بارگذاری شد")

    signals_created = 0
    errors = 0
    blocked = 0

    for symbol, name in SYMBOLS.items():
        try:
            result = await _scan_symbol(symbol, name, news)

            if result is None:
                continue

            _last_scan[symbol] = result

            # چک سیگنال
            if result.get("direction"):
                if not is_duplicate_signal(symbol, result["direction"], hours=4):
                    insert_signal(
                        symbol=symbol,
                        name=name,
                        direction=result["direction"],
                        entry=result["current_price"],
                        sl=result["suggested_sl"],
                        tp=result["suggested_tp"],
                        score=result["score"],
                        reasons=result.get("reasons", []),
                    )
                    signals_created += 1
                    logger.info(
                        f"✅ سیگنال: {name} {result['direction']} "
                        f"@ {result['current_price']} | امتیاز {result['score']}"
                    )
            else:
                # شمارش اسکن‌های بلاک‌شده
                if result.get("htf_trend") == "CONFLICT":
                    blocked += 1

        except Exception as e:
            errors += 1
            logger.error(f"خطا در اسکن {name}: {e}")

    # رصد سیگنال‌های باز
    try:
        check_open_signals()
    except Exception as e:
        logger.error(f"خطا در رصد سیگنال‌ها: {e}")

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    _scan_stats["total_scans"] += 1
    _scan_stats["last_scan_time"] = start_time.isoformat()
    _scan_stats["signals_today"] += signals_created
    _scan_stats["errors"] += errors

    # آمار کش
    cache = get_cache_stats()

    logger.info(
        f"✔️ اسکن تمام شد ({elapsed:.1f}s) | "
        f"سیگنال: {signals_created} | "
        f"بلاک‌شده: {blocked} | "
        f"خطا: {errors} | "
        f"کش: {cache['size']}"
    )
    logger.info("=" * 55)


async def _scan_symbol(symbol, name, news):
    """اسکن یه نماد با 5 تایم‌فریم"""
    # دریافت داده Top-Down
    data = fetch_topdown(symbol)
    if "error" in data:
        logger.warning(f"{name}: {data['error']}")
        return None

    # فیلتر اخبار
    safe, news_reason = is_news_safe(news, symbol, buffer_min=30)

    # تحلیل کامل 4 لایه
    result = await full_analysis(
        data, symbol,
        news_safe=safe,
        news_reason=news_reason,
    )

    if result.get("score") is None and result.get("score") != 0.0:
        return None

    # اضافه کردن متادیتا
    result["symbol"] = symbol
    result["name"] = name
    result["timeframes"] = TIMEFRAMES
    result["scanned_at"] = datetime.now(timezone.utc).isoformat()
    result["news_safe"] = safe

    return result


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        scan_market,
        "interval",
        minutes=SCAN_INTERVAL_MINUTES,
        next_run_time=datetime.now(timezone.utc),
        id="market_scan",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        f"⏰ Scheduler شروع شد (هر {SCAN_INTERVAL_MINUTES} دقیقه)\n"
        f"📊 تایم‌فریم‌ها: {' → '.join(TIMEFRAMES)}"
    )


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        _scheduler = None


def get_last_scan():
    return _last_scan


def get_scan_stats():
    return _scan_stats
