"""
مدیریت دیتابیس SQLite برای ذخیره سیگنال‌ها
با فیکس جداکننده Reasons (JSON به جای |)
"""
import sqlite3
import logging
import json
from datetime import datetime, timedelta
from contextlib import contextmanager
from .config import DB_PATH

logger = logging.getLogger(__name__)


def init_db():
    """ساخت جداول در اولین اجرا"""
    with get_conn() as conn:
        conn.executescript("""
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
        """)
    logger.info("✅ دیتابیس آماده شد")


@contextmanager
def get_conn():
    """اتصال به دیتابیس با auto-commit/rollback"""
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


def insert_signal(symbol, name, direction, entry, sl, tp, score, reasons):
    """ثبت سیگنال جدید - Reasons به صورت JSON"""
    # تبدیل به JSON (به جای | )
    if isinstance(reasons, list):
        reasons_json = json.dumps(reasons, ensure_ascii=False)
    else:
        reasons_json = json.dumps([str(reasons)], ensure_ascii=False)

    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO signals
            (symbol, name, direction, entry, sl, tp, score, reasons, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, name, direction, entry, sl, tp, score,
            reasons_json,
            datetime.utcnow().isoformat()
        ))
        return cur.lastrowid


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
        # عقب‌گرد: اگه داده قدیمی بود، با | جدا کن
        if "|" in str(reasons_str):
            return [r.strip() for r in str(reasons_str).split("|") if r.strip()]
        return [str(reasons_str)]


def get_open_signals():
    """همه سیگنال‌های باز"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE status = 'OPEN' ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_all_signals(limit: int = 200):
    """همه سیگنال‌ها"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row):
    """تبدیل row به dict با پارس کردن JSON"""
    d = dict(row)
    if "reasons" in d:
        d["reasons"] = _parse_reasons(d["reasons"])
    return d


def close_signal(signal_id, exit_price, pnl_pips, status):
    """بستن سیگنال با WIN یا LOSS یا EXPIRED"""
    with get_conn() as conn:
        conn.execute("""
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
        rows = conn.execute("""
            SELECT status, pnl_pips FROM signals
            WHERE status IN ('WIN', 'LOSS')
        """).fetchall()

    total = len(rows)

    with get_conn() as conn:
        open_count = conn.execute(
            "SELECT COUNT(*) as c FROM signals WHERE status = 'OPEN'"
        ).fetchone()["c"]

        expired_count = conn.execute(
            "SELECT COUNT(*) as c FROM signals WHERE status = 'EXPIRED'"
        ).fetchone()["c"]

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
        rows = conn.execute("""
            SELECT * FROM signals
            WHERE symbol = ? AND created_at > ?
        """, (symbol, cutoff)).fetchall()
        return [_row_to_dict(r) for r in rows]
