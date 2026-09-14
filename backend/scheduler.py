"""
زمان‌بند خودکار:
- هر ۱۵ دقیقه اسکن بازار
- رصد سیگنال‌های باز
- ذخیره نتایج
"""
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import (
    SYMBOLS, SYMBOL_TIMEFRAMES, SCAN_INTERVAL_MINUTES
)
from .data_fetcher import fetch_multi_timeframe
from .analysis_engine import analyze_multi_timeframe
from .news_fetcher import fetch_news, is_news_safe
from .signal_tracker import check_open_signals, is_duplicate_signal
from .database import insert_signal

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_last_scan: dict = {}          # آخرین نتایج هر نماد
_scan_stats: dict = {          # آمار اسکن
    "total_scans": 0,
    "last_scan_time": None,
    "signals_today": 0,
    "errors": 0,
}


# ============================================================
#  اسکن اصلی
# ============================================================

async def scan_market():
    """
    اسکن همه نمادها:
    1. دریافت داده HTF و LTF
    2. تحلیل SMC/ICT
    3. فیلتر اخبار
    4. ثبت سیگنال (اگه معتبر بود)
    """
    logger.info("=" * 50)
    logger.info("🔍 شروع اسکن بازار...")
    start_time = datetime.now(timezone.utc)

    # دریافت اخبار (با کش)
    news = await fetch_news()
    logger.info(f"📰 {len(news)} خبر بارگذاری شد")

    signals_created = 0
    errors = 0

    for symbol, name in SYMBOLS.items():
        try:
            result = await _scan_symbol(symbol, name, news)

            if result:
                _last_scan[symbol] = result

                # اگه سیگنال معتبر داشت
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
                            f"✅ سیگنال جدید: {name} {result['direction']} "
                            f"@ {result['current_price']}"
                        )

        except Exception as e:
            errors += 1
            logger.error(f"خطا در اسکن {name}: {e}")

    # رصد سیگنال‌های باز
    try:
        check_open_signals()
    except Exception as e:
        logger.error(f"خطا در رصد سیگنال‌ها: {e}")

    # آپدیت آمار
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    _scan_stats["total_scans"] += 1
    _scan_stats["last_scan_time"] = start_time.isoformat()
    _scan_stats["signals_today"] += signals_created
    _scan_stats["errors"] += errors

    logger.info(
        f"✔️ اسکن تمام شد ({elapsed:.1f}s) | "
        f"سیگنال جدید: {signals_created} | خطا: {errors}"
    )
    logger.info("=" * 50)


async def _scan_symbol(symbol: str, name: str, news: list) -> dict | None:
    """اسکن یک نماد"""
    htf, ltf = SYMBOL_TIMEFRAMES.get(symbol, ("1h", "15m"))

    # دریافت داده
    data = fetch_multi_timeframe(symbol, htf, ltf)
    if "error" in data:
        logger.warning(f"{name}: {data['error']}")
        return None

    # بررسی اخبار
    safe, news_reason = is_news_safe(news, symbol, buffer_min=30)

    # تحلیل
    result = analyze_multi_timeframe(data["htf"], data["ltf"], symbol)

    if result.get("score") is None:
        return None

    # فیلتر خبر (بعد از تحلیل، چون می‌خوایم نتیجه تحلیل هم ببینیم)
    if result.get("direction") and not safe:
        result["reasons"].append(news_reason)
        result["direction"] = None  # لغو سیگنال
        result["summary"] = "⛔ سیگنال به دلیل خبر لغو شد"

    result["symbol"] = symbol
    result["name"] = name
    result["htf_tf"] = htf
    result["ltf_tf"] = ltf
    result["scanned_at"] = datetime.now(timezone.utc).isoformat()
    result["news_safe"] = safe

    return result


# ============================================================
#  مدیریت Scheduler
# ============================================================

def start_scheduler():
    """راه‌اندازی زمان‌بند"""
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
    """توقف زمان‌بند"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        _scheduler = None
        logger.info("⏰ Scheduler متوقف شد")


def get_last_scan() -> dict:
    """آخرین نتایج اسکن"""
    return _last_scan


def get_scan_stats() -> dict:
    """آمار اسکن"""
    return _scan_stats
