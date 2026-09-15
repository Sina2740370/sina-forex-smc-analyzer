"""
زمان‌بند خودکار با Notification
"""
import logging
import time
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
from .notifier import notify_new_signal, notify_startup

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

# آخرین زمان پیام startup (برای جلوگیری از تکرار)
_last_startup_notified = 0
STARTUP_COOLDOWN_SECONDS = 6 * 3600  # 6 ساعت


async def scan_market():
    global _last_startup_notified

    logger.info("=" * 55)
    logger.info("🔍 شروع اسکن Top-Down...")
    start_time = datetime.now(timezone.utc)

    # پیام startup فقط هر ۶ ساعت یه بار
    now = time.time()
    if now - _last_startup_notified > STARTUP_COOLDOWN_SECONDS:
        try:
            await notify_startup()
            _last_startup_notified = now
        except Exception as e:
            logger.error(f"خطا در پیام شروع: {e}")

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

                    try:
                        await notify_new_signal(
                            symbol=symbol,
                            name=name,
                            direction=result["direction"],
                            entry=result["current_price"],
                            sl=result["suggested_sl"],
                            tp=result["suggested_tp"],
                            score=result["score"],
                            reasons=result.get("reasons", []),
                            trend=result.get("htf_trend", "-"),
                            zone=result.get("zone", "-"),
                        )
                    except Exception as e:
                        logger.error(f"خطا در ارسال نوتیف: {e}")
            else:
                if result.get("htf_trend") == "CONFLICT":
                    blocked += 1

        except Exception as e:
            errors += 1
            logger.error(f"خطا در اسکن {name}: {e}")

    try:
        await check_open_signals_and_notify()
    except Exception as e:
        logger.error(f"خطا در رصد سیگنال‌ها: {e}")

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    _scan_stats["total_scans"] += 1
    _scan_stats["last_scan_time"] = start_time.isoformat()
    _scan_stats["signals_today"] += signals_created
    _scan_stats["errors"] += errors

    cache = get_cache_stats()

    logger.info(
        f"✔️ اسکن تمام شد ({elapsed:.1f}s) | "
        f"سیگنال: {signals_created} | "
        f"بلاک‌شده: {blocked} | "
        f"خطا: {errors} | "
        f"کش: {cache['size']}"
    )
    logger.info("=" * 55)


async def check_open_signals_and_notify():
    from .database import get_open_signals, get_all_signals
    from .notifier import notify_signal_closed

    before = {s["id"] for s in get_open_signals()}
    check_open_signals()
    after_open = {s["id"] for s in get_open_signals()}
    closed_ids = before - after_open

    if not closed_ids:
        return

    all_signals = get_all_signals(limit=50)
    for sig in all_signals:
        if sig["id"] in closed_ids and sig["status"] in ("WIN", "LOSS"):
            try:
                await notify_signal_closed(
                    symbol=sig["symbol"],
                    name=sig.get("name", sig["symbol"]),
                    direction=sig["direction"],
                    status=sig["status"],
                    pnl_pips=sig.get("pnl_pips") or 0,
                    entry=sig["entry"],
                    exit_price=sig.get("exit_price") or sig["entry"],
                )
            except Exception as e:
                logger.error(f"خطا در نوتیف بسته شدن: {e}")


async def _scan_symbol(symbol, name, news):
    data = fetch_topdown(symbol)
    if "error" in data:
        logger.warning(f"{name}: {data['error']}")
        return None

    safe, news_reason = is_news_safe(news, symbol, buffer_min=30)

    result = await full_analysis(
        data, symbol,
        news_safe=safe,
        news_reason=news_reason,
    )

    if result.get("score") is None and result.get("score") != 0.0:
        return None

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
        f"⏰ Scheduler شروع شد (هر {SCAN_INTERVAL_MINUTES} دقیقه)"
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
