"""
دریافت اخبار اقتصادی از ForexFactory
برای فیلتر کردن معاملات در زمان اخبار مهم
"""
import logging
from datetime import datetime, timedelta, timezone
import httpx

logger = logging.getLogger(__name__)

# آدرس رایگان ForexFactory (JSON)
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# کش ساده برای جلوگیری از درخواست زیاد
_cache = {"data": [], "fetched_at": None}
CACHE_MINUTES = 60  # هر ۱ ساعت یک بار


async def fetch_news(force: bool = False) -> list:
    """
    دریافت تقویم اقتصادی این هفته
    با کش ۱ ساعته
    """
    now = datetime.now(timezone.utc)

    # چک کش
    if not force and _cache["fetched_at"]:
        elapsed = (now - _cache["fetched_at"]).total_seconds() / 60
        if elapsed < CACHE_MINUTES:
            return _cache["data"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                FF_URL,
                headers={"User-Agent": "Mozilla/5.0 ForexSMCBot/1.0"}
            )
            r.raise_for_status()
            data = r.json()

        _cache["data"] = data
        _cache["fetched_at"] = now
        logger.info(f"✅ {len(data)} خبر دریافت شد")
        return data

    except Exception as e:
        logger.error(f"خطا در دریافت اخبار: {e}")
        return _cache["data"]  # از کش قدیمی استفاده کن


def upcoming_high_impact(news: list,
                          within_minutes: int = 30,
                          after_minutes: int = None) -> list:
    """
    اخبار High Impact که در بازه‌ی مشخصی (قبل/بعد) هستن
    """
    if not news:
        return []

    now = datetime.now(timezone.utc)
    after = within_minutes if after_minutes is None else after_minutes
    start = now - timedelta(minutes=after)
    end = now + timedelta(minutes=within_minutes)
    result = []

    for item in news:
        # فقط High Impact
        if item.get("impact", "").lower() not in ("high", "medium"):
            continue

        date_str = item.get("date", "")
        if not date_str:
            continue

        try:
            # فرمت ForexFactory: 2025-01-15T13:30:00-05:00
            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)

            if start <= event_time <= end:
                result.append({
                    "title": item.get("title", ""),
                    "country": item.get("country", ""),
                    "currency": item.get("country") or item.get("currency", ""),
                    "impact": item.get("impact", ""),
                    "time": event_time.isoformat(),
                })
        except Exception:
            continue

    return result


def is_news_safe(news: list, symbol: str,
                  buffer_min: int = 30) -> tuple[bool, str]:
    """
    آیا معامله روی این نماد امن هست؟

    Args:
        news: لیست اخبار
        symbol: نماد (مثل EURUSD=X یا GC=F)
        buffer_min: چند دقیقه قبل/بعد خبر = فیلتر

    Returns:
        (safe: bool, reason: str)
    """
    # استخراج ارزهای مربوطه از نماد
    base = symbol.split("=")[0]
    currencies = []

    if symbol == "GC=F":
        currencies = ["USD"]  # طلا به USD وابسته‌ست
    elif len(base) >= 6:
        currencies = [base[:3], base[3:6]]

    if not currencies:
        return True, ""

    events = upcoming_high_impact(
        news, within_minutes=buffer_min, after_minutes=buffer_min * 2
    )

    for ev in events:
        ev_currency = ev.get("currency", "")
        if ev_currency in currencies and ev["impact"].lower() == "high":
            return False, f"⚠️ خبر مهم {ev_currency}: {ev['title']}"

    return True, ""
