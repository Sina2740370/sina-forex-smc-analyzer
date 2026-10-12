"""
ذخیره‌ی ژورنال معاملات دستی (Postgres یا SQLite، با همان الگوی database.py).
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from .database import USE_POSTGRES, _execute, _process_rows, get_conn

logger = logging.getLogger(__name__)


def norm_ts(value: Optional[str]) -> str:
    """زمان ورودی (ISO، با یا بدون منطقه) → رشته‌ی ISO ثانیه‌ای به UTC. خالی = همین الان."""
    if not value:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def init_journal_table():
    pk = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if USE_POSTGRES else "REAL"
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS journal_trades (
            id {pk},
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry {real} NOT NULL,
            sl {real} NOT NULL,
            exit_price {real} NOT NULL,
            r {real} NOT NULL,
            lots {real},
            tag TEXT,
            note TEXT,
            opened_at TEXT,
            closed_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_journal_closed ON journal_trades(closed_at)",
    ]
    with get_conn() as conn:
        for s in stmts:
            _execute(conn, s)


def add_trade(symbol, side, entry, sl, exit_price, r, lots=None, tag=None, note=None,
              opened_at=None, closed_at=None) -> int:
    row = (symbol, side, entry, sl, exit_price, r, lots, tag, note,
           norm_ts(opened_at) if opened_at else None, norm_ts(closed_at),
           norm_ts(None))
    q = """INSERT INTO journal_trades
           (symbol, side, entry, sl, exit_price, r, lots, tag, note, opened_at, closed_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    with get_conn() as conn:
        if USE_POSTGRES:
            cur = _execute(conn, q + " RETURNING id", row)
            return cur.fetchone()[0]
        cur = _execute(conn, q, row)
        return cur.lastrowid


def list_trades(limit: int = 500, since: Optional[str] = None) -> list:
    """جدیدترین‌ها اول؛ since: فقط معاملاتی که closed_at ≥ آن (ISO)."""
    with get_conn() as conn:
        if since:
            cur = _execute(conn, "SELECT * FROM journal_trades WHERE closed_at >= ? "
                                 "ORDER BY closed_at DESC, id DESC LIMIT ?", (norm_ts(since), limit))
        else:
            cur = _execute(conn, "SELECT * FROM journal_trades ORDER BY closed_at DESC, id DESC LIMIT ?",
                           (limit,))
        return _process_rows(cur)


def delete_trade(trade_id: int) -> bool:
    with get_conn() as conn:
        cur = _execute(conn, "DELETE FROM journal_trades WHERE id = ?", (trade_id,))
        return cur.rowcount > 0
