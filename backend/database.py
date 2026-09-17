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

from .config import DB_PATH

logger = logging.getLogger(__name__)

# ============================================================
#  انتخاب نوع دیتابیس
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

USE_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        logger.info("🐘 استفاده از PostgreSQL")
    except ImportError:
        logger.error("❌ psycopg2 نصب نیست → fallback به SQLite")
        USE_POSTGRES = False
else:
    import sqlite3
    logger.info("📁 استفاده از SQLite")


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
def insert_signal(symbol, name, direction, entry, sl, tp, score, reasons):
    """ثبت سیگنال جدید"""
    if isinstance(reasons, list):
        reasons_json = json.dumps(reasons, ensure_ascii=False)
    else:
        reasons_json = json.dumps([str(reasons)], ensure_ascii=False)

    with get_conn() as conn:
        cur = _execute(conn, """
            INSERT INTO signals
            (symbol, name, direction, entry, sl, tp, score, reasons, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, name, direction, entry, sl, tp, score,
            reasons_json,
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
    """بستن سیگنال"""
    with get_conn() as conn:
        _execute(conn, """
            UPDATE signals
            SET status = ?, exit_price = ?, pnl_pips = ?, closed_at = ?
            WHERE id = ?
        """, (
            status, exit_price, pnl_pips,
            datetime.utcnow().isoformat(), signal_id
        ))


def get_stats_summary():
    """آمار کلی"""
    with get_conn() as conn:
        cur = _execute(conn, """
            SELECT status, pnl_pips FROM signals
            WHERE status IN ('WIN', 'LOSS')
        """)

        if USE_POSTGRES:
            rows = cur.fetchall()
            rows = [{"status": r[0], "pnl_pips": r[1]} for r in rows]
        else:
            rows = [dict(r) for r in cur.fetchall()]

        cur2 = _execute(conn, "SELECT COUNT(*) FROM signals WHERE status = 'OPEN'")
        open_count = cur2.fetchone()[0]

        cur3 = _execute(conn, "SELECT COUNT(*) FROM signals WHERE status = 'EXPIRED'")
        expired_count = cur3.fetchone()[0]

    total = len(rows)

    if total == 0:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pips": 0.0,
            "open_signals": open_count,
            "expired_signals": expired_count,
        }

    wins_list = [r["pnl_pips"] or 0 for r in rows if r["status"] == "WIN"]
    losses_list = [r["pnl_pips"] or 0 for r in rows if r["status"] == "LOSS"]

    wins_count = len(wins_list)
    losses_count = len(losses_list)

    gross_profit = sum(p for p in wins_list if p > 0)
    gross_loss = abs(sum(p for p in losses_list if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 0.0

    return {
        "total": total,
        "wins": wins_count,
        "losses": losses_count,
        "win_rate": round(wins_count / total, 4) if total else 0.0,
        "profit_factor": round(pf, 2),
        "total_pips": round(sum(wins_list) + sum(losses_list), 1),
        "open_signals": open_count,
        "expired_signals": expired_count,
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
