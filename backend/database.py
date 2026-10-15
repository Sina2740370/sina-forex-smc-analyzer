"""
مدیریت دیتابیس - پشتیبانی از PostgreSQL + SQLite
اگر DATABASE_URL ست شده باشه → PostgreSQL
وگرنه → SQLite (محلی)
"""
import os
import logging
import json
from datetime import datetime, timedelta
from contextlib import contextmanager

from .config import DB_PATH, PIP_SIZE, SPREAD, LOT_RISK_PCT

logger = logging.getLogger(__name__)

# ============================================================
#  انتخاب نوع دیتابیس
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_RENDER = os.getenv("RENDER", "").strip() != ""  # Render خودش این متغیر رو ست می‌کنه

USE_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

if IS_RENDER and not USE_POSTGRES:
    # تو Production (Render) هرگز نباید بی‌سروصدا بره رو SQLite؛
    # چون فضای دیسک موقتیه و با هر دیپلوی/ری‌استارت داده‌ها پاک می‌شن.
    raise RuntimeError(
        "روی Render هستیم ولی DATABASE_URL معتبر (postgresql://...) تنظیم نشده. "
        "بدون Postgres، هر ری‌استارت باعث از دست رفتن کامل داده‌ها می‌شه. "
        "این رو در Render → Environment تنظیم کن."
    )

if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        logger.info("🐘 استفاده از PostgreSQL")
    except ImportError:
        if IS_RENDER:
            raise RuntimeError(
                "psycopg2 نصب نیست ولی روی Render هستیم و PostgreSQL اجباریه. "
                "psycopg2-binary رو به requirements.txt اضافه کن."
            )
        logger.error("❌ psycopg2 نصب نیست → fallback به SQLite (فقط برای تست محلی)")
        USE_POSTGRES = False

if not USE_POSTGRES:
    import sqlite3
    logger.info("📁 استفاده از SQLite (محیط محلی/تست)")


# ============================================================
#  تبدیل Placeholder
# ============================================================
def _q(query: str) -> str:
    """تبدیل ? به %s برای PostgreSQL"""
    if USE_POSTGRES:
        return query.replace("?", "%s")
    return query


# ============================================================
#  اتصال به دیتابیس
# ============================================================
@contextmanager
def get_conn():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"خطا در دیتابیس: {e}")
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"خطا در دیتابیس: {e}")
            raise
        finally:
            conn.close()


def _execute(conn, query: str, params: tuple = None):
    """اجرای کوئری با placeholder مناسب"""
    cur = conn.cursor()
    if params:
        cur.execute(_q(query), params)
    else:
        cur.execute(_q(query))
    return cur


# ============================================================
#  ساخت جداول
# ============================================================
def init_db():
    """ساخت جداول در اولین اجرا"""

    if USE_POSTGRES:
        schema = """
        CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            name TEXT,
            direction TEXT NOT NULL,
            entry DOUBLE PRECISION NOT NULL,
            sl DOUBLE PRECISION NOT NULL,
            tp DOUBLE PRECISION NOT NULL,
            score DOUBLE PRECISION NOT NULL,
            reasons TEXT,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            status TEXT DEFAULT 'OPEN',
            exit_price DOUBLE PRECISION,
            pnl_pips DOUBLE PRECISION
        );
        CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
        CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
        CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
        """
    else:
        schema = """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            name TEXT,
            direction TEXT NOT NULL,
            entry REAL NOT NULL,
            sl REAL NOT NULL,
            tp REAL NOT NULL,
            score REAL NOT NULL,
            reasons TEXT,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            status TEXT DEFAULT 'OPEN',
            exit_price REAL,
            pnl_pips REAL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
        CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
        CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
        """

    with get_conn() as conn:
        cur = conn.cursor()
        # برای PostgreSQL، هر دستور باید جدا اجرا شه
        if USE_POSTGRES:
            for stmt in schema.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
        else:
            cur.executescript(schema)

    logger.info(f"✅ دیتابیس آماده شد ({'PostgreSQL' if USE_POSTGRES else 'SQLite'})")

    # --- افزودن ستون price_at_signal (قیمت لحظه بازار موقع صدور سیگنال) ---
    # جدا از entry، چون entry سطح تحلیلی SMC است نه لزوما قیمت لحظه‌ای بازار.
    # با try/except idempotent شده چون ALTER TABLE ADD COLUMN IF NOT EXISTS
    # روی نسخه‌های قدیمی‌تر SQLite پشتیبانی نمی‌شه.
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute(_q("ALTER TABLE signals ADD COLUMN price_at_signal REAL"))
            conn.commit()
            logger.info("✅ ستون price_at_signal اضافه شد")
        except Exception:
            conn.rollback()  # ستون از قبل وجود داره، مشکلی نیست


# ============================================================
#  ردیف به Dict
# ============================================================
def _row_to_dict(row, description=None):
    """تبدیل row به dict"""
    if row is None:
        return None

    if USE_POSTGRES:
        if description is None:
            return dict(row)
        return {description[i][0]: row[i] for i in range(len(row))}
    else:
        d = dict(row)
        if "reasons" in d:
            d["reasons"] = _parse_reasons(d["reasons"])
        return d


def _parse_reasons(reasons_str):
    """تبدیل Reasons از JSON به لیست"""
    if not reasons_str:
        return []
    try:
        parsed = json.loads(reasons_str)
        if isinstance(parsed, list):
            return parsed
        return [str(parsed)]
    except (json.JSONDecodeError, TypeError):
        if "|" in str(reasons_str):
            return [r.strip() for r in str(reasons_str).split("|") if r.strip()]
        return [str(reasons_str)]


def _process_rows(cur) -> list:
    """پردازش همه ردیف‌ها"""
    if USE_POSTGRES:
        columns = [desc[0] for desc in cur.description]
        results = []
        for row in cur.fetchall():
            d = {columns[i]: row[i] for i in range(len(columns))}
            if "reasons" in d:
                d["reasons"] = _parse_reasons(d["reasons"])
            results.append(d)
        return results
    else:
        results = []
        for row in cur.fetchall():
            d = dict(row)
            if "reasons" in d:
                d["reasons"] = _parse_reasons(d["reasons"])
            results.append(d)
        return results


# ============================================================
#  عملیات‌ها
# ============================================================
def insert_signal(symbol, name, direction, entry, sl, tp, score, reasons,
                   price_at_signal=None):
    """ثبت سیگنال جدید

    price_at_signal: قیمت لحظه‌ای بازار در لحظه صدور سیگنال (اختیاری).
    جدا از entry نگه داشته می‌شه چون entry می‌تونه یه سطح تحلیلی SMC
    (مثلا وسط یک Order Block) باشه، نه لزوما قیمت جاری بازار.
    """
    if isinstance(reasons, list):
        reasons_json = json.dumps(reasons, ensure_ascii=False)
    else:
        reasons_json = json.dumps([str(reasons)], ensure_ascii=False)

    with get_conn() as conn:
        cur = _execute(conn, """
            INSERT INTO signals
            (symbol, name, direction, entry, sl, tp, score, reasons, price_at_signal, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, name, direction, entry, sl, tp, score,
            reasons_json, price_at_signal,
            datetime.utcnow().isoformat()
        ))

        if USE_POSTGRES:
            try:
                cur.execute("SELECT LASTVAL()")
                return cur.fetchone()[0]
            except Exception:
                return None
        else:
            return cur.lastrowid


def get_open_signals():
    """همه سیگنال‌های باز"""
    with get_conn() as conn:
        cur = _execute(conn,
            "SELECT * FROM signals WHERE status = 'OPEN' ORDER BY created_at DESC"
        )
        return _process_rows(cur)


def get_all_signals(limit: int = 200):
    """همه سیگنال‌ها"""
    with get_conn() as conn:
        cur = _execute(conn,
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        return _process_rows(cur)


def close_signal(signal_id, exit_price, pnl_pips, status):
    """Close an open signal once; concurrent tracker runs cannot overwrite a prior result."""
    with get_conn() as conn:
        cur = _execute(conn, """
            UPDATE signals
            SET status = ?, exit_price = ?, pnl_pips = ?, closed_at = ?
            WHERE id = ? AND status = 'OPEN'
        """, (
            status, exit_price, pnl_pips,
            datetime.utcnow().isoformat(), signal_id
        ))
        return getattr(cur, "rowcount", 0) == 1


def get_stats_summary():
    """آمار کلی: بر حسب R-multiple، EXPIRED تو مخرج، با دراودان"""
    with get_conn() as conn:
        cur = _execute(conn, """
            SELECT symbol, status, entry, sl, pnl_pips FROM signals
            WHERE status IN ('WIN', 'LOSS', 'EXPIRED')
            ORDER BY closed_at ASC
        """)
        rows = cur.fetchall()
        cur2 = _execute(conn, "SELECT COUNT(*) FROM signals WHERE status = 'OPEN'")
        open_count = cur2.fetchone()[0]

    wins = losses = expired = 0
    gross_r_win = gross_r_loss = 0.0
    total_pips = 0.0
    cum = peak = max_dd = 0.0

    for sym, status, entry, sl, pnl in rows:
        pnl = pnl or 0.0
        pip = PIP_SIZE.get(sym)
        spread_pips = SPREAD.get(sym)
        # entry is the mid-price signal; tracker PnL includes half-spread on entry
        # and exit, so risk must include the executable entry half-spread too.
        risk_pips = (
            abs(entry - sl) / pip + spread_pips / 2
            if sl is not None and pip and spread_pips is not None
            else 0.0
        )
        r = pnl / risk_pips if risk_pips > 0 else 0.0

        if status == "WIN":
            wins += 1
        elif status == "LOSS":
            losses += 1
        else:
            expired += 1

        # Expired trades are marked-to-market by signal_tracker; their realized
        # PnL must affect expectancy, profit factor, total pips, and drawdown.
        total_pips += pnl
        if r > 0:
            gross_r_win += r
        elif r < 0:
            gross_r_loss += abs(r)

        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    total = wins + losses + expired
    if gross_r_loss > 0:
        pf = gross_r_win / gross_r_loss
    else:
        pf = 99.0 if gross_r_win > 0 else 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total, 4) if total else 0.0,
        "profit_factor": round(min(pf, 99.0), 2),
        "total_pips": round(total_pips, 1),
        "total_r": round(cum, 2),
        "max_drawdown_r": round(max_dd, 2),
        # LOT_RISK_PCT عدد درصدیه (مثلا ۰.۵ یعنی ۰.۵٪)، برای همین تقسیم بر ۱۰۰ می‌کنیم
        # تا با max_dd (که بر حسب R-multiple است) هم‌واحد بشه و drawdown واقعی حساب رو نشون بده،
        # نه یه مقدار جدا که با ریسک واقعیِ استفاده‌شده تو محاسبه‌ی حجم لات (notifier.py) هم‌خوانی نداره.
        "max_drawdown_pct": round(max_dd * (LOT_RISK_PCT / 100), 4),
        "open_signals": open_count,
        "expired_signals": expired,
    }


def get_recent_signals_for_symbol(symbol: str, hours: int = 4):
    """سیگنال‌های اخیر یه نماد"""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        cur = _execute(conn, """
            SELECT * FROM signals
            WHERE symbol = ? AND created_at > ?
        """, (symbol, cutoff))
        return _process_rows(cur)


# ============================================================
#  وضعیت سلامت دیتابیس (برای /api/health)
# ============================================================
def check_db_connection() -> dict:
    """یه کوئری سبک واقعی می‌زنه تا مطمئن شه دیتابیس واقعاً در دسترسه،
    نه فقط اینکه USE_POSTGRES تنظیم شده."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"healthy": True, "reason": None}
    except Exception as e:
        logger.error(f"❌ دیتابیس در دسترس نیست: {e}")
        return {"healthy": False, "reason": str(e)}
