"""
FastAPI Backend - Sina Quant v3.1.1 (security patch)
"""
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .config import (
    TEST_DURATION_DAYS, MIN_WIN_RATE,
    MIN_TRADES, MIN_PROFIT_FACTOR, MAX_DRAWDOWN,
    SCAN_INTERVAL_MINUTES, TIMEFRAMES,
)
from .database import (
    init_db, get_stats_summary,
    get_all_signals, get_open_signals,
)
from .scheduler import (
    start_scheduler, get_last_scan, get_scan_stats,
    scan_market,
)
from .data_fetcher import fetch_live_prices
from . import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

VERSION = "3.1.2"

# کلید ادمین: فقط از متغیر محیطی Render خوانده می‌شود (هرگز داخل کد نگذار)
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()
# صفحه /docs به‌صورت پیش‌فرض خاموش است؛ برای روشن کردن ENABLE_DOCS=1 بگذار
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "0").strip() == "1"


def require_admin(x_admin_key: str = Header(default="")):
    """احراز هویت ادمین با هدر X-Admin-Key (اگر ADMIN_KEY ست نباشد، مسیر بسته است)"""
    if not ADMIN_KEY:
        raise HTTPException(status_code=503, detail="Admin endpoint disabled")
    if not secrets.compare_digest(x_admin_key.encode("utf-8"), ADMIN_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("🚀 راه‌اندازی Sina Quant...")
    if not ADMIN_KEY:
        logger.warning("⚠️ ADMIN_KEY ست نشده؛ /api/scan/force غیرفعال است")
    init_db()
    start_scheduler()
    logger.info("✅ اپ آماده است")
    logger.info("=" * 50)
    yield
    logger.info("👋 خاموش شدن اپ...")


app = FastAPI(
    title="Sina Quant",
    description="Quantitative Forex Analysis with SMC/ICT",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)


# فشرده‌سازی پاسخ‌ها (سریع‌تر روی موبایل و اینترنت ضعیف)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def basic_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# کش کوتاه‌مدت قیمت‌ها: چند کلاینت/تب هر ۳۰ ثانیه صدا می‌زنن،
# ولی به منبع داده (yfinance) بیشتر از هر ۱۵ ثانیه یک‌بار درخواست نمی‌ره
_PRICES_TTL = 15
_prices_cache = {"ts": 0.0, "data": {}}
_prices_lock = threading.Lock()


def _cached_prices():
    now = time.monotonic()
    with _prices_lock:
        if _prices_cache["data"] and now - _prices_cache["ts"] < _PRICES_TTL:
            return _prices_cache["data"]
    try:
        data = fetch_live_prices()
    except Exception:
        logger.exception("خطا در دریافت قیمت‌ها")
        data = None
    with _prices_lock:
        if data:
            _prices_cache["data"] = data
            _prices_cache["ts"] = now
        # اگه دریافت شکست خورد، آخرین قیمت خوب رو برگردون
        return _prices_cache["data"]


# ============================================================
#  API Endpoints
# ============================================================

@app.get("/api/db-status")
def db_status():
    """چک کردن نوع دیتابیس فعال (بدون نشان دادن هیچ بخشی از آدرس/پسورد)"""
    url = os.getenv("DATABASE_URL", "").strip()
    return {
        "database_url_set": bool(url),
        "use_postgres": database.USE_POSTGRES,
        "db_type": "PostgreSQL" if database.USE_POSTGRES else "SQLite",
    }


@app.get("/api/health")
def health():
    stats = get_scan_stats()
    return {
        "status": "ok",
        "app": "Sina Quant",
        "version": VERSION,
        "db_type": "PostgreSQL" if database.USE_POSTGRES else "SQLite",
        "scans": stats.get("total_scans", 0),
        "last_scan": stats.get("last_scan_time"),
        "timeframes": stats.get("timeframes", []),
        "signals_today": stats.get("signals_today", 0),
        "errors": stats.get("errors", 0),
    }


@app.get("/api/config")
def get_config():
    return {
        "app_name": "Sina Quant",
        "test_duration_days": TEST_DURATION_DAYS,
        "min_win_rate": MIN_WIN_RATE,
        "min_trades": MIN_TRADES,
        "min_profit_factor": MIN_PROFIT_FACTOR,
        "max_drawdown": MAX_DRAWDOWN,
        "scan_interval_minutes": SCAN_INTERVAL_MINUTES,
        "timeframes": TIMEFRAMES,
    }


@app.get("/api/prices")
def get_prices():
    """قیمت‌های زنده"""
    prices = _cached_prices()
    return {
        "status": "ok",
        "data": list(prices.values()),
        "count": len(prices),
    }


@app.get("/api/scan")
def get_scan():
    last = get_last_scan()
    if not last:
        return JSONResponse(
            content={"status": "waiting", "message": "اولین اسکن در حال اجراست..."},
            status_code=200,
        )
    return {
        "status": "ok",
        "data": list(last.values()),
        "count": len(last),
    }


@app.post("/api/scan/force", dependencies=[Depends(require_admin)])
async def force_scan():
    """اسکن اجباری - فقط با هدر X-Admin-Key"""
    try:
        await scan_market()
        return {"ok": True, "message": "اسکن انجام شد"}
    except Exception:
        # جزئیات خطا فقط در لاگ سرور، نه در پاسخ به کلاینت
        logger.exception("خطا در اسکن اجباری")
        raise HTTPException(status_code=500, detail="Scan failed")


@app.get("/api/signals")
def get_signals(limit: int = Query(200, ge=1, le=500)):
    return get_all_signals(limit=limit)


@app.get("/api/signals/open")
def get_open():
    return get_open_signals()


@app.get("/api/stats")
def get_stats():
    stats = get_stats_summary()
    ready = (
        stats["total"] >= MIN_TRADES
        and stats["win_rate"] >= MIN_WIN_RATE
        and stats["profit_factor"] >= MIN_PROFIT_FACTOR
        and stats["max_drawdown_pct"] <= MAX_DRAWDOWN
    )
    return {
        **stats,
        "test_duration_days": TEST_DURATION_DAYS,
        "min_win_rate": MIN_WIN_RATE,
        "min_trades": MIN_TRADES,
        "min_profit_factor": MIN_PROFIT_FACTOR,
        "max_drawdown": MAX_DRAWDOWN,
        "ready_for_live": ready,
    }


# ============================================================
#  Frontend (PWA)
# ============================================================

if (FRONTEND_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
else:
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def serve_index():
    """صفحه اصلی - بدون Clear-Site-Data (که باعث reload loop می‌شد)"""
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        return JSONResponse(
            content={"error": "index.html یافت نشد"},
            status_code=404,
        )
    response = FileResponse(index)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/manifest.json")
def serve_manifest():
    manifest = FRONTEND_DIR / "manifest.json"
    if not manifest.exists():
        raise HTTPException(status_code=404)
    response = FileResponse(manifest, media_type="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/sw.js")
def serve_sw():
    """Service Worker - نسخه ساده بدون cache loop"""
    sw = FRONTEND_DIR / "sw.js"
    if not sw.exists():
        raise HTTPException(status_code=404)
    response = FileResponse(sw, media_type="application/javascript")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/favicon.ico")
def favicon():
    ico = FRONTEND_DIR / "static" / "favicon.ico"
    if ico.exists():
        return FileResponse(ico)
    raise HTTPException(status_code=404)
