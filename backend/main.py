"""
FastAPI Backend - Sina Quant
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("🚀 راه‌اندازی Sina Quant...")
    init_db()
    start_scheduler()
    logger.info("✅ اپ آماده است")
    logger.info("=" * 50)
    yield
    logger.info("👋 خاموش شدن اپ...")


app = FastAPI(
    title="Sina Quant",
    description="Quantitative Forex Analysis with SMC/ICT",
    version="3.0.0",
    lifespan=lifespan,
)


# ============================================================
#  API Endpoints
# ============================================================

@app.get("/api/db-status")
def db_status():
    """چک کردن نوع دیتابیس فعال"""
    url = os.getenv("DATABASE_URL", "").strip()
    return {
        "database_url_set": bool(url),
        "url_prefix": url[:30] if url else "NOT SET",
        "url_length": len(url),
        "use_postgres": database.USE_POSTGRES,
        "db_type": "PostgreSQL" if database.USE_POSTGRES else "SQLite",
    }


@app.get("/api/health")
def health():
    stats = get_scan_stats()
    return {
        "status": "ok",
        "app": "Sina Quant",
        "version": "3.0.0",
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
    prices = fetch_live_prices()
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


@app.post("/api/scan/force")
async def force_scan():
    try:
        await scan_market()
        return {"ok": True, "message": "اسکن انجام شد"}
    except Exception as e:
        logger.exception("خطا در اسکن اجباری")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/signals")
def get_signals(limit: int = 200):
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
    """صفحه اصلی - با هدر Clear-Site-Data برای پاک کردن کش"""
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        return JSONResponse(
            content={"error": "index.html یافت نشد"},
            status_code=404,
        )
    response = FileResponse(index)
    # 🔥 پاک کردن cache, cookies, storage, Service Workerها
    response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage", "executionContexts"'
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
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
    """Service Worker - همیشه نسخه جدید"""
    sw = FRONTEND_DIR / "sw.js"
    if not sw.exists():
        raise HTTPException(status_code=404)
    response = FileResponse(sw, media_type="application/javascript")
    # 🔥 Service Worker همیشه باید fresh باشه
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    # 🔥 غیرفعال کردن SW (کاملاً پاک کن)
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/favicon.ico")
def favicon():
    ico = FRONTEND_DIR / "static" / "favicon.ico"
    if ico.exists():
        return FileResponse(ico)
    raise HTTPException(status_code=404)
