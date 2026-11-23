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
        f"""CREATE TABLE IF NOT EXISTS manual_positions (
            id {pk},
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price {real} NOT NULL,
            stop_loss {real} NOT NULL,
            take_profit {real},
            current_stop_loss {real} NOT NULL,
            current_take_profit {real},
            initial_lots {real} NOT NULL,
            closed_lots {real} NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            signal_id INTEGER,
            strategy_version TEXT,
            note TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS manual_position_events (
            id {pk},
            position_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            lots {real} NOT NULL,
            price {real} NOT NULL,
            gross_r {real} NOT NULL,
            weighted_r {real} NOT NULL,
            cost_amount {real} NOT NULL DEFAULT 0,
            cost_currency TEXT,
            event_at TEXT NOT NULL,
            note TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS manual_position_changes (
            id {pk},
            position_id INTEGER NOT NULL,
            old_stop_loss {real} NOT NULL,
            new_stop_loss {real} NOT NULL,
            old_take_profit {real},
            new_take_profit {real},
            event_at TEXT NOT NULL,
            note TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_manual_positions_status ON manual_positions(status, opened_at)",
        "CREATE INDEX IF NOT EXISTS idx_manual_events_position ON manual_position_events(position_id, event_at, id)",
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
    cutoff = day_start_utc()
    legacy_all = list_trades(limit=1000)
    rows = [row for row in legacy_all if row.get("closed_at") and row["closed_at"] >= cutoff]
    legacy = jr.normalize(to_records(rows))
    timeline = [(t["close_time"].isoformat(), float(t["r"])) for t in legacy]
    timeline.extend(manual_position_daily_results(cutoff))
    timeline.sort(key=lambda item: item[0])
    r_today = [value for _, value in timeline]
    legacy_opened_today = sum(
        1 for row in legacy_all
        if (row.get("opened_at") or row.get("closed_at") or "") >= cutoff
    )
    manual_opened_today = sum(
        1 for position in list_manual_positions(limit=1000)
        if (position.get("opened_at") or "") >= cutoff
    )
    opened_today = legacy_opened_today + manual_opened_today
    g = jr.daily_guard(r_today, float(lim["maxloss"]), int(lim["maxtrades"]),
                       int(lim["consec"]), trades_today=opened_today)
    g["limits"] = {"maxloss": lim["maxloss"], "maxtrades": lim["maxtrades"], "consec": lim["consec"]}
    g["r_basis"] = "GROSS_R_BEFORE_RECORDED_COSTS"
    g["recorded_costs_by_currency_today"] = manual_position_costs_by_currency(since=cutoff)
    return g


# ------------------------------------------------------------
#  چرخه عمر معامله واقعی با ثبت دستی کاربر (بدون اتصال بروکر)
# ------------------------------------------------------------
def _finite_positive(value, name: str) -> float:
    import math
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} باید عدد معتبر باشد") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} باید عدد مثبت و متناهی باشد")
    return number


def _manual_row(cur):
    rows = _process_rows(cur)
    return rows[0] if rows else None


def _lock_manual_position(conn, position_id: int) -> None:
    """Serialize lifecycle writes for one position before reading mutable state."""
    if USE_POSTGRES:
        _execute(conn, "SELECT id FROM manual_positions WHERE id = ? FOR UPDATE",
                 (int(position_id),))
    else:
        # Acquire the SQLite write reservation before the read/validate/write sequence.
        conn.execute("BEGIN IMMEDIATE")


def create_manual_position(
    symbol: str, side: str, entry_price: float, stop_loss: float, lots: float,
    *, take_profit=None, opened_at=None, signal_id=None, strategy_version=None, note=None,
    idempotency_key: str,
) -> dict:
    """ثبت موقعیت فقط پس از ورود واقعی که کاربر دستی اعلام کرده است."""
    symbol = str(symbol or "").strip().upper()
    side = str(side or "").strip().upper()
    if not 3 <= len(symbol) <= 20:
        raise ValueError("نماد نامعتبر است")
    if side not in {"BUY", "SELL"}:
        raise ValueError("جهت باید BUY یا SELL باشد")
    entry = _finite_positive(entry_price, "entry_price")
    sl = _finite_positive(stop_loss, "stop_loss")
    size = _finite_positive(lots, "lots")
    if (side == "BUY" and sl >= entry) or (side == "SELL" and sl <= entry):
        raise ValueError("حد ضرر باید در سمت زیان‌آور قیمت ورود باشد")
    tp = _finite_positive(take_profit, "take_profit") if take_profit is not None else None
    if tp is not None and ((side == "BUY" and tp <= entry) or (side == "SELL" and tp >= entry)):
        raise ValueError("حد سود باید در سمت سودآور قیمت ورود باشد")
    key = str(idempotency_key or "").strip()
    if not 8 <= len(key) <= 128:
        raise ValueError("idempotency_key باید بین ۸ تا ۱۲۸ نویسه باشد")
    if signal_id is not None and int(signal_id) <= 0:
        raise ValueError("signal_id باید مثبت باشد")
    opened = norm_ts(opened_at)
    now = norm_ts(None)
    with get_conn() as conn:
        cur = _execute(conn, "SELECT * FROM manual_positions WHERE idempotency_key = ?", (key,))
        existing = _manual_row(cur)
        if existing:
            same_request = (
                existing["symbol"] == symbol and existing["side"] == side
                and abs(float(existing["entry_price"]) - entry) <= 1e-12
                and abs(float(existing["stop_loss"]) - sl) <= 1e-12
                and ((existing.get("take_profit") is None and tp is None) or
                     (existing.get("take_profit") is not None and tp is not None and
                      abs(float(existing["take_profit"]) - tp) <= 1e-12))
                and abs(float(existing["initial_lots"]) - size) <= 1e-12
                and (opened_at is None or existing["opened_at"] == opened)
                and existing.get("signal_id") == signal_id
                and existing.get("strategy_version") == strategy_version
                and existing.get("note") == note
            )
            if not same_request:
                raise ValueError("idempotency_key قبلاً برای ورودی متفاوت استفاده شده است")
            return existing
        insert = """INSERT INTO manual_positions
            (symbol, side, entry_price, stop_loss, take_profit, current_stop_loss,
             current_take_profit, initial_lots, closed_lots, status, opened_at, closed_at,
             signal_id, strategy_version, note, idempotency_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'OPEN', ?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (idempotency_key) DO NOTHING"""
        values = (symbol, side, entry, sl, tp, sl, tp, size, opened, signal_id,
                  strategy_version, note, key, now, now)
        if USE_POSTGRES:
            cur = _execute(conn, insert + " RETURNING id", values)
            inserted = cur.fetchone()
            position_id = inserted[0] if inserted else None
        else:
            cur = _execute(conn, insert, values)
            position_id = cur.lastrowid if cur.rowcount == 1 else None
        if position_id is None:
            cur = _execute(conn, "SELECT * FROM manual_positions WHERE idempotency_key = ?", (key,))
            existing = _manual_row(cur)
            if not existing:
                raise RuntimeError("idempotent manual position insert returned no row")
            same_request = (
                existing["symbol"] == symbol and existing["side"] == side
                and abs(float(existing["entry_price"]) - entry) <= 1e-12
                and abs(float(existing["stop_loss"]) - sl) <= 1e-12
                and ((existing.get("take_profit") is None and tp is None) or
                     (existing.get("take_profit") is not None and tp is not None and
                      abs(float(existing["take_profit"]) - tp) <= 1e-12))
                and abs(float(existing["initial_lots"]) - size) <= 1e-12
                and (opened_at is None or existing["opened_at"] == opened)
                and existing.get("signal_id") == signal_id
                and existing.get("strategy_version") == strategy_version
                and existing.get("note") == note
            )
            if not same_request:
                raise ValueError("idempotency_key قبلاً برای ورودی متفاوت استفاده شده است")
            return existing
        _execute(conn, """INSERT INTO manual_position_events
            (position_id, event_type, lots, price, gross_r, weighted_r, cost_amount,
             cost_currency, event_at, note, idempotency_key, created_at)
            VALUES (?, 'OPEN', ?, ?, 0, 0, 0, NULL, ?, ?, ?, ?)""",
            (position_id, size, entry, opened, note, key, now))
        cur = _execute(conn, "SELECT * FROM manual_positions WHERE id = ?", (position_id,))
        return _manual_row(cur)


def list_manual_positions(status: Optional[str] = None, limit: int = 200) -> list:
    limit = int(limit)
    if not 1 <= limit <= 1000:
        raise ValueError("limit باید بین ۱ تا ۱۰۰۰ باشد")
    status = str(status).upper() if status else None
    if status not in {None, "OPEN", "CLOSED"}:
        raise ValueError("status نامعتبر است")
    with get_conn() as conn:
        if status:
            cur = _execute(conn, """SELECT * FROM manual_positions
                WHERE status = ? ORDER BY opened_at DESC, id DESC LIMIT ?""", (status, limit))
        else:
            cur = _execute(conn, """SELECT * FROM manual_positions
                ORDER BY opened_at DESC, id DESC LIMIT ?""", (limit,))
        return _process_rows(cur)


def list_manual_position_events(position_id: int) -> list:
    with get_conn() as conn:
        cur = _execute(conn, """SELECT * FROM manual_position_events
            WHERE position_id = ? ORDER BY event_at ASC, id ASC""", (int(position_id),))
        return _process_rows(cur)


def close_manual_position(
    position_id: int, lots: float, price: float, *, event_at=None,
    cost_amount: float = 0, cost_currency: Optional[str] = None,
    note: Optional[str] = None, idempotency_key: str,
) -> dict:
    """ثبت خروج جزئی/کامل دستی؛ هر رویداد append-only و idempotent است."""
    import math
    size = _finite_positive(lots, "lots")
    exit_price = _finite_positive(price, "price")
    try:
        costs = float(cost_amount)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("cost_amount نامعتبر است") from exc
    if not math.isfinite(costs) or costs < 0:
        raise ValueError("cost_amount باید صفر یا مثبت و متناهی باشد")
    currency = str(cost_currency).strip().upper() if cost_currency else None
    if currency and not 3 <= len(currency) <= 8:
        raise ValueError("cost_currency نامعتبر است")
    if costs > 0 and not currency:
        raise ValueError("برای هزینه مثبت، cost_currency الزامی است")
    key = str(idempotency_key or "").strip()
    if not 8 <= len(key) <= 128:
        raise ValueError("idempotency_key باید بین ۸ تا ۱۲۸ نویسه باشد")
    timestamp = norm_ts(event_at)
    now = norm_ts(None)
    with get_conn() as conn:
        _lock_manual_position(conn, position_id)
        cur = _execute(conn, "SELECT * FROM manual_position_events WHERE idempotency_key = ?", (key,))
        duplicate = _manual_row(cur)
        if duplicate:
            same_request = (
                int(duplicate["position_id"]) == int(position_id)
                and abs(float(duplicate["lots"]) - size) <= 1e-12
                and abs(float(duplicate["price"]) - exit_price) <= 1e-12
                and abs(float(duplicate["cost_amount"]) - costs) <= 1e-12
                and (duplicate.get("cost_currency") or None) == currency
                and (event_at is None or duplicate["event_at"] == timestamp)
                and duplicate.get("note") == note
            )
            if not same_request:
                raise ValueError("idempotency_key قبلاً برای رویداد خروج متفاوت استفاده شده است")
            return duplicate
        cur = _execute(conn, "SELECT * FROM manual_positions WHERE id = ?", (int(position_id),))
        position = _manual_row(cur)
        if not position:
            raise LookupError("موقعیت پیدا نشد")
        if position["status"] != "OPEN":
            raise ValueError("موقعیت قبلاً به‌طور کامل بسته شده است")
        if timestamp < str(position["opened_at"]):
            raise ValueError("زمان خروج نمی‌تواند قبل از زمان ورود باشد")
        cur = _execute(conn, """SELECT MAX(event_at) FROM manual_position_events
            WHERE position_id = ? AND event_type IN ('PARTIAL_CLOSE', 'CLOSE')""", (int(position_id),))
        latest_exit = cur.fetchone()[0]
        if latest_exit and timestamp < str(latest_exit):
            raise ValueError("زمان خروج نمی‌تواند قبل از رویداد خروج قبلی باشد")
        initial = float(position["initial_lots"])
        closed = float(position["closed_lots"])
        remaining = initial - closed
        if size > remaining + 1e-9:
            raise ValueError("حجم خروج از حجم بازِ باقی‌مانده بیشتر است")
        side = position["side"]
        entry = float(position["entry_price"])
        risk_distance = abs(entry - float(position["stop_loss"]))
        if risk_distance <= 0:
            raise ValueError("فاصله حد ضرر نامعتبر است")
        gross_r = ((exit_price - entry) if side == "BUY" else (entry - exit_price)) / risk_distance
        weighted_r = gross_r * size / initial
        is_final = size >= remaining - 1e-9
        new_closed_at = timestamp if is_final else None
        event_type = "CLOSE" if is_final else "PARTIAL_CLOSE"
        insert = """INSERT INTO manual_position_events
            (position_id, event_type, lots, price, gross_r, weighted_r, cost_amount,
             cost_currency, event_at, note, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (idempotency_key) DO NOTHING"""
        values = (int(position_id), event_type, size, exit_price, gross_r, weighted_r,
                  costs, currency, timestamp, note, key, now)
        cur = _execute(conn, insert, values)
        if cur.rowcount != 1:
            cur = _execute(conn, "SELECT * FROM manual_position_events WHERE idempotency_key = ?", (key,))
            duplicate = _manual_row(cur)
            if not duplicate:
                raise RuntimeError("idempotent close insert returned no row")
            same_request = (
                int(duplicate["position_id"]) == int(position_id)
                and abs(float(duplicate["lots"]) - size) <= 1e-12
                and abs(float(duplicate["price"]) - exit_price) <= 1e-12
                and abs(float(duplicate["cost_amount"]) - costs) <= 1e-12
                and (duplicate.get("cost_currency") or None) == currency
                and duplicate["event_at"] == timestamp
                and duplicate.get("note") == note
            )
            if not same_request:
                raise ValueError("idempotency_key قبلاً برای رویداد خروج متفاوت استفاده شده است")
            return duplicate
        cur = _execute(conn, """UPDATE manual_positions
            SET closed_lots = closed_lots + ?,
                status = CASE WHEN closed_lots + ? >= initial_lots - 1e-9 THEN 'CLOSED' ELSE 'OPEN' END,
                closed_at = CASE WHEN closed_lots + ? >= initial_lots - 1e-9 THEN ? ELSE closed_at END,
                updated_at = ?
            WHERE id = ? AND status = 'OPEN'
              AND initial_lots - closed_lots >= ? - 1e-9""",
            (size, size, size, new_closed_at, now, int(position_id), size))
        if cur.rowcount != 1:
            raise ValueError("موقعیت هم‌زمان تغییر کرده یا حجم خروج معتبر نیست؛ فهرست را تازه‌سازی کنید")
        cur = _execute(conn, "SELECT * FROM manual_position_events WHERE idempotency_key = ?", (key,))
        return _manual_row(cur)


def get_manual_position(position_id: int) -> Optional[dict]:
    with get_conn() as conn:
        cur = _execute(conn, "SELECT * FROM manual_positions WHERE id = ?", (int(position_id),))
        return _manual_row(cur)


def list_manual_position_changes(position_id: int) -> list:
    with get_conn() as conn:
        cur = _execute(conn, """SELECT * FROM manual_position_changes
            WHERE position_id = ? ORDER BY event_at ASC, id ASC""", (int(position_id),))
        return _process_rows(cur)


def update_manual_position_stops(
    position_id: int, *, new_stop_loss=None, new_take_profit=None,
    event_at=None, note=None, idempotency_key: str,
) -> dict:
    """Record user-entered SL/TP changes; initial risk anchor remains immutable."""
    if new_stop_loss is None and new_take_profit is None:
        raise ValueError("حداقل یکی از SL یا TP جدید باید وارد شود")
    new_sl = _finite_positive(new_stop_loss, "new_stop_loss") if new_stop_loss is not None else None
    new_tp = _finite_positive(new_take_profit, "new_take_profit") if new_take_profit is not None else None
    key = str(idempotency_key or "").strip()
    if not 8 <= len(key) <= 128:
        raise ValueError("idempotency_key باید بین ۸ تا ۱۲۸ نویسه باشد")
    timestamp = norm_ts(event_at)
    now = norm_ts(None)
    with get_conn() as conn:
        _lock_manual_position(conn, position_id)
        cur = _execute(conn, "SELECT * FROM manual_position_changes WHERE idempotency_key = ?", (key,))
        duplicate = _manual_row(cur)
        if duplicate:
            same_request = (
                int(duplicate["position_id"]) == int(position_id)
                and ((new_sl is None and abs(float(duplicate["new_stop_loss"]) -
                                                   float(duplicate["old_stop_loss"])) <= 1e-12)
                     or (new_sl is not None and
                         abs(float(duplicate["new_stop_loss"]) - new_sl) <= 1e-12))
                and ((new_tp is None and
                      ((duplicate.get("new_take_profit") is None and
                        duplicate.get("old_take_profit") is None) or
                       (duplicate.get("new_take_profit") is not None and
                        duplicate.get("old_take_profit") is not None and
                        abs(float(duplicate["new_take_profit"]) -
                            float(duplicate["old_take_profit"])) <= 1e-12)))
                     or (new_tp is not None and duplicate.get("new_take_profit") is not None and
                         abs(float(duplicate["new_take_profit"]) - new_tp) <= 1e-12))
                and (event_at is None or duplicate["event_at"] == timestamp)
                and duplicate.get("note") == note
            )
            if not same_request:
                raise ValueError("idempotency_key قبلاً برای تغییر SL/TP متفاوت استفاده شده است")
            return duplicate
        cur = _execute(conn, "SELECT * FROM manual_positions WHERE id = ?", (int(position_id),))
        position = _manual_row(cur)
        if not position:
            raise LookupError("موقعیت پیدا نشد")
        if position["status"] != "OPEN":
            raise ValueError("تغییر حدها برای موقعیت بسته‌شده مجاز نیست")
        if timestamp < str(position["opened_at"]):
            raise ValueError("زمان تغییر نمی‌تواند قبل از زمان ورود باشد")
        cur = _execute(conn, "SELECT MAX(event_at) FROM manual_position_changes WHERE position_id = ?",
                       (int(position_id),))
        latest_change = cur.fetchone()[0]
        cur = _execute(conn, """SELECT MAX(event_at) FROM manual_position_events
            WHERE position_id = ? AND event_type IN ('PARTIAL_CLOSE', 'CLOSE')""", (int(position_id),))
        latest_exit = cur.fetchone()[0]
        latest = max([x for x in (latest_change, latest_exit) if x], default=None)
        if latest and timestamp < str(latest):
            raise ValueError("زمان تغییر نمی‌تواند قبل از رویداد قبلی باشد")
        old_sl = float(position["current_stop_loss"])
        old_tp = float(position["current_take_profit"]) if position.get("current_take_profit") is not None else None
        final_sl = new_sl if new_sl is not None else old_sl
        final_tp = new_tp if new_tp is not None else old_tp
        entry = float(position["entry_price"])
        # Break-even is valid; a stop moved through entry is not.
        if ((position["side"] == "BUY" and final_sl > entry) or
                (position["side"] == "SELL" and final_sl < entry)):
            raise ValueError("حد ضرر جدید نباید از قیمت ورود عبور کند")
        if final_tp is not None and ((position["side"] == "BUY" and final_tp <= entry) or
                                     (position["side"] == "SELL" and final_tp >= entry)):
            raise ValueError("حد سود باید در سمت سودآور قیمت ورود باشد")
        insert = """INSERT INTO manual_position_changes
            (position_id, old_stop_loss, new_stop_loss, old_take_profit, new_take_profit,
             event_at, note, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (idempotency_key) DO NOTHING"""
        cur = _execute(conn, insert, (int(position_id), old_sl, final_sl, old_tp, final_tp,
                                      timestamp, note, key, now))
        if cur.rowcount != 1:
            cur = _execute(conn, "SELECT * FROM manual_position_changes WHERE idempotency_key = ?", (key,))
            duplicate = _manual_row(cur)
            if not duplicate:
                raise RuntimeError("idempotent stop update returned no row")
            same_request = (
                int(duplicate["position_id"]) == int(position_id)
                and abs(float(duplicate["new_stop_loss"]) - final_sl) <= 1e-12
                and ((duplicate.get("new_take_profit") is None and final_tp is None) or
                     (duplicate.get("new_take_profit") is not None and final_tp is not None and
                      abs(float(duplicate["new_take_profit"]) - final_tp) <= 1e-12))
                and (event_at is None or duplicate["event_at"] == timestamp)
                and duplicate.get("note") == note
            )
            if not same_request:
                raise ValueError("idempotency_key قبلاً برای تغییر SL/TP متفاوت استفاده شده است")
            return duplicate
        cur = _execute(conn, """UPDATE manual_positions
            SET current_stop_loss = ?, current_take_profit = ?, updated_at = ?
            WHERE id = ? AND status = 'OPEN'
              AND current_stop_loss = ?
              AND ((current_take_profit IS NULL AND ? IS NULL) OR current_take_profit = ?)""",
            (final_sl, final_tp, now, int(position_id), old_sl, old_tp, old_tp))
        if cur.rowcount != 1:
            raise ValueError("موقعیت هم‌زمان بسته شده؛ تغییر حدها انجام نشد")
        cur = _execute(conn, "SELECT * FROM manual_position_changes WHERE idempotency_key = ?", (key,))
        return _manual_row(cur)



def manual_positions_as_journal_records(since: Optional[str] = None) -> list:
    """Closed manual positions represented as volume-weighted gross-R records.

    Partial exits are combined into one equivalent weighted-average exit price,
    preserving the sum of each exit's R contribution. Costs remain separately
    recorded and are NOT silently subtracted without account-currency risk data.
    """
    positions = list_manual_positions(status="CLOSED", limit=1000)
    cutoff = norm_ts(since) if since else None
    records = []
    for position in positions:
        if cutoff and position.get("closed_at") and position["closed_at"] < cutoff:
            continue
        events = list_manual_position_events(position["id"])
        exits = [event for event in events if event["event_type"] in {"PARTIAL_CLOSE", "CLOSE"}]
        closed_lots = sum(float(event["lots"]) for event in exits)
        if closed_lots <= 0:
            continue
        average_exit = sum(float(event["price"]) * float(event["lots"]) for event in exits) / closed_lots
        records.append({
            "symbol": position["symbol"],
            "side": position["side"],
            "entry": float(position["entry_price"]),
            "sl": float(position["stop_loss"]),
            "exit": average_exit,
            "close_time": position["closed_at"],
            "open_time": position["opened_at"],
            "tag": "MANUAL_POSITION",
            "note": "volume-weighted gross R; recorded costs are not netted",
        })
    return records


def manual_position_daily_results(since: str) -> list:
    """One realized gross-R observation per position for the selected day.

    Multiple partial exits of the same position are aggregated, so partial fills
    do not inflate the daily trade-count guard.
    """
    cutoff = norm_ts(since)
    results = []
    for position in list_manual_positions(limit=1000):
        events = [
            event for event in list_manual_position_events(position["id"])
            if event["event_type"] in {"PARTIAL_CLOSE", "CLOSE"}
            and event["event_at"] >= cutoff
        ]
        if events:
            results.append((
                max(event["event_at"] for event in events),
                sum(float(event["weighted_r"]) for event in events),
            ))
    return results


def manual_position_costs_by_currency(since: Optional[str] = None) -> dict:
    """Return recorded close costs by currency; no FX conversion is inferred."""
    cutoff = norm_ts(since) if since else None
    totals = {}
    positions = list_manual_positions(limit=1000)
    for position in positions:
        for event in list_manual_position_events(position["id"]):
            if cutoff and event["event_at"] < cutoff:
                continue
            amount = float(event.get("cost_amount") or 0)
            if amount <= 0:
                continue
            currency = event.get("cost_currency") or "UNSPECIFIED"
            totals[currency] = totals.get(currency, 0.0) + amount
    return {key: round(value, 8) for key, value in sorted(totals.items())}
