"""
تحلیل فاندامنتال با FRED API
- نرخ بهره فدرال رزرو
- شاخص دلار (DXY)
- تورم و اشتغال
- محاسبه امتیاز فاندامنتال برای هر جفت‌ارز
"""
import os
import logging
from datetime import datetime, timedelta, timezone
import httpx

from .config import FRED_API_KEY

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# کش ساده (۶ ساعت)
_cache = {"data": {}, "fetched_at": None}
CACHE_HOURS = 6


# ============================================================
#  دریافت داده از FRED
# ============================================================

async def _fetch_series(series_id: str, limit: int = 30) -> list:
    """دریافت آخرین N مقدار یه series از FRED"""
    if not FRED_API_KEY:
        return []

    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(FRED_BASE, params=params)
            r.raise_for_status()
            data = r.json().get("observations", [])
        return data
    except Exception as e:
        logger.error(f"خطا در FRED {series_id}: {e}")
        return []


async def fetch_all_fundamental_data() -> dict:
    """دریافت همه داده‌های فاندامنتال (با کش)"""
    now = datetime.now(timezone.utc)

    # چک کش
    if _cache["fetched_at"]:
        elapsed = (now - _cache["fetched_at"]).total_seconds() / 3600
        if elapsed < CACHE_HOURS and _cache["data"]:
            return _cache["data"]

    data = {}

    # شاخص دلار
    data["dxy"] = await _fetch_series("DTWEXBGS", limit=30)
    # نرخ بهره فدرال رزرو
    data["fed_rate"] = await _fetch_series("DFF", limit=30)
    # نرخ تورم آمریکا
    data["us_cpi"] = await _fetch_series("CPIAUCSL", limit=15)
    # نرخ بیکاری
    data["us_unemp"] = await _fetch_series("UNRATE", limit=15)

    _cache["data"] = data
    _cache["fetched_at"] = now
    logger.info("📊 داده‌های فاندامنتال FRED دریافت شد")
    return data


def _to_floats(observations: list) -> list:
    """تبدیل مشاهدات به لیست اعداد"""
    result = []
    for obs in observations:
        try:
            v = float(obs.get("value", "."))
            result.append(v)
        except (ValueError, TypeError):
            continue
    return result


# ============================================================
#  تحلیل‌گرهای اختصاصی
# ============================================================

def analyze_dxy(data: dict) -> tuple[float, str]:
    """
    تحلیل شاخص دلار
    Returns: (score, reason)
    score: -1 (دلار ضعیف) تا +1 (دلار قوی)
    """
    values = _to_floats(data.get("dxy", []))
    if len(values) < 10:
        return 0.0, ""

    # آخرین مقدار و میانگین ۱۰ روز قبل
    latest = values[0]
    prev = sum(values[1:10]) / 9

    change_pct = (latest - prev) / prev * 100

    # شاخص دلار: تغییر > 0.5% در ۱۰ روز = trend قوی
    if change_pct > 0.5:
        score = min(change_pct / 2, 1.0)
        return score, f"دلار قوی ({change_pct:+.1f}% در 10 روز)"
    elif change_pct < -0.5:
        score = max(change_pct / 2, -1.0)
        return score, f"دلار ضعیف ({change_pct:+.1f}% در 10 روز)"
    return 0.0, ""


def analyze_fed_rate(data: dict) -> tuple[float, str]:
    """روند نرخ بهره فدرال رزرو"""
    values = _to_floats(data.get("fed_rate", []))
    if len(values) < 10:
        return 0.0, ""

    latest = values[0]
    prev = sum(values[5:10]) / 5
    change = latest - prev

    if change > 0.1:
        return 0.5, f"فدرال: افزایش نرخ ({change:+.2f}%)"
    elif change < -0.1:
        return -0.5, f"فدرال: کاهش نرخ ({change:+.2f}%)"
    return 0.0, ""


def analyze_us_cpi(data: dict) -> tuple[float, str]:
    """تحلیل تورم آمریکا (CPI) - افزایش تورم معمولاً bullish دلار"""
    values = _to_floats(data.get("us_cpi", []))
    if len(values) < 4:
        return 0.0, ""

    latest = values[0]
    prev = values[1] if len(values) > 1 else latest
    change = (latest - prev) / prev * 100

    if change > 0.3:
        return 0.3, f"تورم آمریکا افزایشی ({change:+.1f}%)"
    elif change < -0.3:
        return -0.3, f"تورم آمریکا کاهشی ({change:+.1f}%)"
    return 0.0, ""


def analyze_us_unemployment(data: dict) -> tuple[float, str]:
    """نرخ بیکاری - کاهش = قوت اقتصادی = دلار قوی"""
    values = _to_floats(data.get("us_unemp", []))
    if len(values) < 3:
        return 0.0, ""

    latest = values[0]
    prev = values[1]
    change = latest - prev

    if change < -0.1:
        return 0.3, f"بیکاری کاهش ({change:+.1f}%) → دلار قوی"
    elif change > 0.1:
        return -0.3, f"بیکاری افزایش ({change:+.1f}%) → دلار ضعیف"
    return 0.0, ""


# ============================================================
#  تحلیلگر اصلی
# ============================================================

async def calculate_fundamental_score(symbol: str) -> dict:
    """
    محاسبه امتیاز فاندامنتال برای یه جفت‌ارز
    Returns: {score: -1..+1, reasons: [...]}
    """
    try:
        data = await fetch_all_fundamental_data()

        dxy_score, dxy_reason = analyze_dxy(data)
        fed_score, fed_reason = analyze_fed_rate(data)
        cpi_score, cpi_reason = analyze_us_cpi(data)
        unemp_score, unemp_reason = analyze_us_unemployment(data)

        # جمع امتیاز دلار (US Dollar Strength)
        usd_strength = dxy_score + fed_score + cpi_score + unemp_score
        usd_strength = max(-1.0, min(1.0, usd_strength))

        reasons = []
        for r in (dxy_reason, fed_reason, cpi_reason, unemp_reason):
            if r:
                reasons.append(r)

        # تبدیل به امتیاز جفت‌ارز
        # اگه USD base هست (مثل USDJPY): USD قوی → pair صعودی
        # اگه USD quote هست (مثل EURUSD): USD قوی → pair نزولی

        base = symbol.split("=")[0]

        if symbol == "GC=F":
            # طلا: با USD معکوس
            score = -usd_strength * 0.7
        elif base.startswith("USD"):
            # USD/XXX
            score = usd_strength
        elif base.endswith("USD"):
            # XXX/USD
            score = -usd_strength
        else:
            # کراس‌ها (EURGBP, EURJPY, GBPJPY) - تأثیر USD کم
            score = 0.0

        return {
            "score": round(score, 2),
            "reasons": reasons,
            "usd_strength": round(usd_strength, 2),
        }

    except Exception as e:
        logger.exception(f"خطا در فاندامنتال {symbol}")
        return {"score": 0.0, "reasons": [], "usd_strength": 0.0}
