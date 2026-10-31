"""
ذخیره‌ی ژورنال معاملات دستی (Postgres یا SQLite، با همان الگوی database.py).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
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
        "CREATE TABLE IF NOT EXISTS tool_settings (skey TEXT PRIMARY KEY, sval TEXT)",
        f"CREATE TABLE IF NOT EXISTS tg_updates (update_id {'BIGINT' if USE_POSTGRES else 'INTEGER'} PRIMARY KEY, created_at TEXT NOT NULL)",
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


# ------------------------------------------------------------
#  تنظیمات ابزار (موجودی، ریسک، اهرم، سقف‌های نگهبان) و dedupe آپدیت‌های تلگرام
# ------------------------------------------------------------
DEFAULTS = {"balance": None, "risk": 1.0, "lev": None, "maxloss": 3.0, "maxtrades": 5, "consec": 3}


def get_raw(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = _execute(conn, "SELECT sval FROM tool_settings WHERE skey = ?", (key,)).fetchone()
    return None if row is None else row[0]


def set_raw(key: str, value: str) -> None:
    with get_conn() as conn:
        _execute(conn, "INSERT INTO tool_settings (skey, sval) VALUES (?, ?) "
                       "ON CONFLICT (skey) DO UPDATE SET sval = EXCLUDED.sval", (key, value))


def get_settings() -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        raw = get_raw("cfg:" + k)
        if raw is not None:
            out[k] = json.loads(raw)
    return out


def set_setting(key: str, value) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"کلید ناشناخته: {key}")
    set_raw("cfg:" + key, json.dumps(value))


def mark_update(update_id: int) -> bool:
    """True اگر این update_id تازه است؛ False اگر قبلاً پردازش شده (تلگرام گاهی دوباره می‌فرستد)."""
    with get_conn() as conn:
        cutoff = norm_ts((datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        _execute(conn, "DELETE FROM tg_updates WHERE created_at < ?", (cutoff,))
        cur = _execute(conn, "INSERT INTO tg_updates (update_id, created_at) VALUES (?, ?) "
                             "ON CONFLICT (update_id) DO NOTHING", (int(update_id), norm_ts(None)))
        return cur.rowcount == 1


# ------------------------------------------------------------
#  نگهبان روزانه
# ------------------------------------------------------------
def day_start_utc(tz_name: Optional[str] = None) -> str:
    """شروع «امروز» به وقت محلی (پیش‌فرض Asia/Tehran یا GUARD_TZ) به صورت ISO و UTC."""
    name = tz_name or os.getenv("GUARD_TZ", "Asia/Tehran")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(name)
    except Exception:
        logger.warning("منطقه‌ی زمانی %s در دسترس نیست؛ UTC استفاده شد", name)
        tz = timezone.utc
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def to_records(rows: list) -> list:
    """ردیف‌های دیتابیس → قالب ورودی engine.tools.journal (قدیمی‌ترین اول)."""
    recs = [{"symbol": r["symbol"], "side": r["side"], "entry": r["entry"], "sl": r["sl"],
             "exit": r["exit_price"], "close_time": r["closed_at"],
             "open_time": r.get("opened_at"), "tag": r.get("tag")} for r in rows]
    recs.reverse()
    return recs


def compute_guard(overrides: Optional[dict] = None) -> dict:
    """وضعیت نگهبان امروز؛ سقف‌ها از تنظیمات ذخیره‌شده و در صورت نیاز override."""
    from engine.tools import journal as jr   # import تنبل تا نبود موتور کل ماژول را نشکند
    lim = get_settings()
    lim.update({k: v for k, v in (overrides or {}).items() if v is not None})
    rows = list_trades(limit=500, since=day_start_utc())
    r_today = [t["r"] for t in jr.normalize(to_records(rows))]
    g = jr.daily_guard(r_today, float(lim["maxloss"]), int(lim["maxtrades"]), int(lim["consec"]))
    g["limits"] = {"maxloss": lim["maxloss"], "maxtrades": lim["maxtrades"], "consec": lim["consec"]}
    return g
