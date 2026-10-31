"""
FastAPI Backend - Sina Quant v3.2.1 (owner-only Telegram Mini App auth)
"""
import asyncio
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .config import (
    TEST_DURATION_DAYS, MIN_WIN_RATE,
    MIN_TRADES, MIN_PROFIT_FACTOR, MAX_DRAWDOWN,
    SCAN_INTERVAL_MINUTES, TIMEFRAMES,
)
from .database import (
    init_db, get_stats_summary, get_stats_by_symbol,
    get_all_signals, get_open_signals,
)
from .scheduler import (
    start_scheduler, get_last_scan, get_scan_stats,
    scan_market,
)
from .data_fetcher import fetch_live_prices, data_feed_health
from . import database
from .database import check_db_connection
from . import journal_store
from .tg_auth import verify_init_data, init_data_from_header

from pydantic import BaseModel, Field

# ابزارهای ریسک/ژورنال از موتور (اگر import نشد، فقط همین endpointها غیرفعال می‌شوند)
try:
    from engine.tools import journal as _journal, risk as _risk
    TOOLS_OK = True
except Exception as _e:  # pragma: no cover
    _journal = _risk = None
    TOOLS_OK = False
    logging.getLogger(__name__).error("engine.tools در دسترس نیست: %s", _e)

try:
    from . import telegram_bot as tgbot
    TG_OK = True
except Exception as _e:  # pragma: no cover
    tgbot = None
    TG_OK = False
    logging.getLogger(__name__).error("telegram_bot در دسترس نیست: %s", _e)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

VERSION = "3.2.1"

# کلید ادمین: فقط از متغیر محیطی Render خوانده می‌شود (هرگز داخل کد نگذار)
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()
# صفحه /docs به‌صورت پیش‌فرض خاموش است؛ برای روشن کردن ENABLE_DOCS=1 بگذار
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "0").strip() == "1"


def _is_owner(authorization: str, x_admin_key: str) -> bool:
    """صاحب = initData معتبر تلگرام با آیدی TELEGRAM_CHAT_ID، یا کلید ادمین درست"""
    if verify_init_data(init_data_from_header(authorization)):
        return True
    if ADMIN_KEY and x_admin_key and secrets.compare_digest(
            x_admin_key.encode("utf-8"), ADMIN_KEY.encode("utf-8")):
        return True
    return False


def require_admin(authorization: str = Header(default=""), x_admin_key: str = Header(default="")):
    """احراز هویت صاحب: مینی‌اپ تلگرام (Authorization: tma ...) یا هدر X-Admin-Key"""
    if not _is_owner(authorization, x_admin_key):
        raise HTTPException(status_code=401, detail="Unauthorized")


# مسیرهای باز: health برای UptimeRobot، webhook تلگرام با secret خودش چک می‌شود
_OPEN_API_PATHS = {"/api/health", "/api/telegram/webhook"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("🚀 راه‌اندازی Sina Quant...")
    if not ADMIN_KEY:
        logger.warning("⚠️ ADMIN_KEY ست نشده؛ /api/scan/force غیرفعال است")
    init_db()
    try:
        journal_store.init_journal_table()
    except Exception as e:
        logger.error("ساخت جدول ژورنال ناموفق بود: %s", e)
    if TG_OK and ADMIN_KEY:
        asyncio.create_task(tgbot.register_webhook(ADMIN_KEY))   # بدون بلاک کردن استارت
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
async def owner_only(request: Request, call_next):
    """همه‌ی /api/* فقط برای صاحب (به‌جز مسیرهای باز)"""
    path = request.url.path
    if (path.startswith("/api/") and path not in _OPEN_API_PATHS
            and request.method != "OPTIONS"):
        if not _is_owner(request.headers.get("authorization", ""),
                         request.headers.get("x-admin-key", "")):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


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
def health(request: Request):
    stats = get_scan_stats()
    db_check = check_db_connection()
    feed_check = data_feed_health()

    overall_ok = db_check["healthy"] and feed_check["healthy"]
    status = "ok" if overall_ok else "degraded"

    # برای غیرصاحب (مثلاً UptimeRobot) فقط وضعیت کلی؛ هیچ آمار/اطلاعات سیگنال نشان داده نمی‌شود
    if not _is_owner(request.headers.get("authorization", ""),
                     request.headers.get("x-admin-key", "")):
        payload = {"status": status}
    else:
        payload = {
            "status": status,
            "app": "Sina Quant",
            "version": VERSION,
            "db_type": "PostgreSQL" if database.USE_POSTGRES else "SQLite",
            "database": db_check,
            "data_feed": feed_check,
            "scans": stats.get("total_scans", 0),
            "last_scan": stats.get("last_scan_time"),
            "timeframes": stats.get("timeframes", []),
            "signals_today": stats.get("signals_today", 0),
            "errors": stats.get("errors", 0),
        }

    if not overall_ok:
        # کد ۵۰۳ تا UptimeRobot/مانیتورینگ واقعاً قطعی رو تشخیص بده،
        # نه اینکه همیشه 200 ببینه و فکر کنه همه‌چی سالمه
        return JSONResponse(content=payload, status_code=503)
    return payload


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


@app.get("/api/stats/by-symbol")
def stats_by_symbol():
    """آمار تفکیکی هر نماد (R و پیپ) — همان auth سراسری /api/*."""
    return {"symbols": get_stats_by_symbol()}


# ============================================================
#  ابزار معامله‌ی دستی: ماشین‌حساب ریسک + ژورنال + نگهبان
# ============================================================
class SizeIn(BaseModel):
    symbol: str
    balance: float = Field(gt=0)
    risk_pct: float = Field(gt=0, le=100)
    entry: float
    sl: float
    tp: Optional[float] = None
    lot_step: float = Field(0.01, gt=0)
    min_lot: float = Field(0.01, gt=0)
    contract: Optional[float] = Field(None, gt=0)
    leverage: Optional[float] = Field(None, gt=0, le=3000)
    max_margin_pct: float = Field(50.0, gt=0, le=100)


class TradeIn(BaseModel):
    symbol: str = Field(min_length=3, max_length=12)
    side: str
    entry: float
    sl: float
    exit: float
    lots: Optional[float] = Field(None, gt=0)
    tag: Optional[str] = Field(None, max_length=40)
    note: Optional[str] = Field(None, max_length=500)
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None


def _need_tools():
    if not TOOLS_OK:
        raise HTTPException(status_code=503, detail="ابزارهای موتور در دسترس نیستند")


def _journal_rows(limit: int = 2000):
    return journal_store.to_records(journal_store.list_trades(limit=limit))


@app.post("/api/risk/size")
def risk_size(body: SizeIn):
    """حجم معامله برای ریسک درصدی ثابت (فقط محاسبه، بدون ذخیره)."""
    _need_tools()
    try:
        res = _risk.position_size(body.symbol.upper().strip(), body.balance, body.risk_pct,
                                  body.entry, body.sl, body.tp, body.lot_step, body.min_lot,
                                  contract=body.contract, leverage=body.leverage,
                                  max_margin_pct=body.max_margin_pct)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {**res.__dict__, "supported": sorted(_risk.SPECS)}


@app.post("/api/journal", dependencies=[Depends(require_admin)])
def journal_add(t: TradeIn, background: BackgroundTasks):
    _need_tools()
    side = t.side.upper().strip()
    try:
        r = _risk.trade_r(side, t.entry, t.sl, t.exit)
        new_id = journal_store.add_trade(t.symbol.upper().strip(), side, t.entry, t.sl, t.exit,
                                         round(r, 4), t.lots, t.tag, t.note,
                                         t.opened_at, t.closed_at)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if TG_OK:
        background.add_task(tgbot.check_guard_alert)   # هشدار فقط روی تغییر وضعیت
    return {"id": new_id, "r": round(r, 4)}


@app.get("/api/journal", dependencies=[Depends(require_admin)])
def journal_list(limit: int = Query(200, ge=1, le=1000)):
    return {"trades": journal_store.list_trades(limit=limit)}


@app.delete("/api/journal/{trade_id}", dependencies=[Depends(require_admin)])
def journal_delete(trade_id: int):
    if not journal_store.delete_trade(trade_id):
        raise HTTPException(status_code=404, detail="معامله پیدا نشد")
    return {"deleted": trade_id}


@app.get("/api/journal/stats", dependencies=[Depends(require_admin)])
def journal_stats():
    """آمار صادقانه‌ی معاملات دستی: امید ریاضی با بازه اطمینان + قضاوت + عملکرد هر برچسب."""
    _need_tools()
    rows = _journal_rows()
    out = {
        "summary": _journal.journal_stats(rows),
        "by_tag": _journal.by_tag(rows),
        "drawdown": _journal.drawdown_guard([t["r"] for t in _journal.normalize(rows)]),
    }
    return _journal.json_safe(out)


@app.get("/api/journal/guard", dependencies=[Depends(require_admin)])
def journal_guard(max_daily_loss_r: Optional[float] = Query(None, gt=0),
                  max_trades: Optional[int] = Query(None, ge=1),
                  max_consec_losses: Optional[int] = Query(None, ge=1)):
    """وضعیت نگهبان امروز (روز به وقت GUARD_TZ، پیش‌فرض Asia/Tehran): ok / warn / stop.
    سقف‌ها از تنظیمات ذخیره‌شده (تلگرام: /set) و در صورت ارسال پارامتر، override می‌شوند."""
    _need_tools()
    g = journal_store.compute_guard({"maxloss": max_daily_loss_r, "maxtrades": max_trades,
                                     "consec": max_consec_losses})
    return _journal.json_safe(g)


# ------------------------------ تلگرام (webhook)
@app.post("/api/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request, background: BackgroundTasks):
    """تلگرام اینجا آپدیت‌ها را می‌فرستد. فوراً ۲۰۰ برمی‌گرداند و پردازش در پس‌زمینه است
    (پاسخ کند باعث ارسال مجدد تلگرام و ثبت تکراری می‌شد)."""
    if not (TG_OK and TOOLS_OK) or not tgbot.verify_secret(
            request.headers.get("x-telegram-bot-api-secret-token"), ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    background.add_task(tgbot.handle_update, update)
    return {"ok": True}


@app.post("/api/telegram/setup", dependencies=[Depends(require_admin)])
async def telegram_setup():
    """ثبت دستی webhook (معمولاً لازم نیست؛ هنگام استارت خودکار ثبت می‌شود)."""
    if not TG_OK:
        raise HTTPException(status_code=503, detail="ماژول تلگرام در دسترس نیست")
    return {"registered": await tgbot.register_webhook(ADMIN_KEY)}


@app.get("/api/telegram/status", dependencies=[Depends(require_admin)])
async def telegram_status():
    if not TG_OK:
        raise HTTPException(status_code=503, detail="ماژول تلگرام در دسترس نیست")
    return await tgbot.webhook_status()


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
    html = index.read_text(encoding="utf-8")
    tag = '<script src="/static/tg-auth.js"></script>'
    if tag not in html:
        html = html.replace("<head>", "<head>" + tag, 1) if "<head>" in html else tag + html
    response = HTMLResponse(html)
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
    """favicon واقعی وجود ندارد؛ ۴۰۴ نویز لاگ می‌سازد → 204 خالی برمی‌گردانیم"""
    ico = FRONTEND_DIR / "static" / "favicon.ico"
    if ico.exists():
        return FileResponse(ico)
    return Response(status_code=204)
