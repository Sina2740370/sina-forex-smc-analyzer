"""Transactional, append-only SQLite event ledger for Paper/Backtest accounting.

This module deliberately contains no broker or live-execution integration.
Financial quantities in event payloads must be serialized as decimal strings.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


class LedgerError(Exception):
    """Base exception for ledger contract violations."""


class LedgerConflictError(LedgerError):
    """An event identifier was reused with conflicting idempotency data."""


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    account_id: str
    trade_id: str | None
    order_id: str | None
    event_type: str
    sequence: int
    occurred_at: str
    recorded_at: str
    idempotency_key: str
    schema_version: int
    payload: Mapping[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    # Decimal values are intentionally not coerced through binary float.
    # Callers must encode money/prices/quantities as decimal strings.
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


class SQLiteLedger:
    """SQLite event store with atomic batches and per-account idempotency."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._uri = False
        self._keeper = None
        if self.path == ":memory:":
            self.path = f"file:mas-ledger-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = sqlite3.connect(self.path, uri=True, timeout=30.0,
                                           isolation_level=None)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None,
                               uri=self._uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_events (
                    event_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    trade_id TEXT,
                    order_id TEXT,
                    event_type TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                    UNIQUE(account_id, sequence),
                    UNIQUE(account_id, idempotency_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_account_time "
                "ON ledger_events(account_id, occurred_at, sequence)"
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS ledger_events_no_update
                BEFORE UPDATE ON ledger_events
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_events is append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS ledger_events_no_delete
                BEFORE DELETE ON ledger_events
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_events is append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS ledger_meta_no_update
                BEFORE UPDATE ON ledger_meta
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_meta is immutable');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS ledger_meta_no_delete
                BEFORE DELETE ON ledger_meta
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_meta is immutable');
                END
                """
            )

    @staticmethod
    def _validate_input(event: Mapping[str, Any]) -> dict[str, Any]:
        required = ("account_id", "event_type", "idempotency_key", "payload")
        missing = [key for key in required if key not in event]
        if missing:
            raise ValueError(f"missing event fields: {', '.join(missing)}")
        normalized = dict(event)
        for key in ("account_id", "event_type", "idempotency_key"):
            if not isinstance(normalized[key], str) or not normalized[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
        normalized["payload_json"] = _canonical_payload(normalized["payload"])
        normalized["event_id"] = str(normalized.get("event_id") or uuid.uuid4())
        normalized["schema_version"] = int(normalized.get("schema_version", 1))
        if normalized["schema_version"] < 1:
            raise ValueError("schema_version must be >= 1")
        normalized["occurred_at"] = normalized.get("occurred_at") or _utc_now()
        normalized["recorded_at"] = _utc_now()
        return normalized

    def get_meta(self, key: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT meta_value FROM ledger_meta WHERE meta_key=?", (key,)
            ).fetchone()
        return row["meta_value"] if row else None

    def set_meta_once(self, key: str, value: str) -> str:
        """Set immutable metadata once; return the authoritative stored value."""
        if not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip():
            raise ValueError("metadata key and value must be non-empty strings")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT meta_value FROM ledger_meta WHERE meta_key=?", (key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO ledger_meta(meta_key, meta_value) VALUES (?, ?)",
                    (key, value),
                )
                stored = value
            else:
                stored = row["meta_value"]
            conn.commit()
            return stored
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_event(self, event: Mapping[str, Any]) -> tuple[LedgerEvent, bool]:
        events = self.append_events([event])
        return events[0]

    def append_events(self, events: Iterable[Mapping[str, Any]]) -> list[tuple[LedgerEvent, bool]]:
        prepared = [self._validate_input(event) for event in events]
        if not prepared:
            return []
        results: list[tuple[LedgerEvent, bool]] = []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for item in prepared:
                existing = conn.execute(
                    "SELECT * FROM ledger_events WHERE account_id=? AND idempotency_key=?",
                    (item["account_id"], item["idempotency_key"]),
                ).fetchone()
                if existing is not None:
                    identity_fields = ("event_type", "trade_id", "order_id",
                                        "schema_version", "payload_json")
                    if any(existing[field] != item.get(field) for field in identity_fields):
                        raise LedgerConflictError(
                            "idempotency_key already exists with a different event body"
                        )
                    results.append((self._row_to_event(existing), False))
                    continue

                sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq "
                    "FROM ledger_events WHERE account_id=?",
                    (item["account_id"],),
                ).fetchone()["next_seq"]
                conn.execute(
                    """
                    INSERT INTO ledger_events (
                        event_id, account_id, trade_id, order_id, event_type, sequence,
                        occurred_at, recorded_at, idempotency_key, schema_version, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (item["event_id"], item["account_id"], item.get("trade_id"),
                     item.get("order_id"), item["event_type"], sequence,
                     item["occurred_at"], item["recorded_at"], item["idempotency_key"],
                     item["schema_version"], item["payload_json"]),
                )
                row = conn.execute(
                    "SELECT * FROM ledger_events WHERE event_id=?", (item["event_id"],)
                ).fetchone()
                results.append((self._row_to_event(row), True))
            conn.commit()
            return results
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        """Release the keeper connection used by an in-memory ledger."""
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def read_events(self, account_id: str, after_sequence: int = 0) -> list[LedgerEvent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM ledger_events WHERE account_id=? AND sequence>? "
                "ORDER BY sequence ASC",
                (account_id, int(after_sequence)),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def replay_account(self, account_id: str, initial_balance: str) -> dict[str, Any]:
        """Rebuild Paper/Backtest accounting and risk state from immutable events."""
        def dec(value: Any, field: str) -> Decimal:
            if not isinstance(value, str):
                raise LedgerError(f"{field} must be encoded as a decimal string")
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise LedgerError(f"{field} is not a valid decimal") from exc
            if not number.is_finite():
                raise LedgerError(f"{field} must be finite")
            return number

        balance = dec(initial_balance, "initial_balance")
        realized = Decimal("0")
        positions: dict[str, dict[str, Any]] = {}
        closed_trades: list[dict[str, Any]] = []
        risk_lock_active = False
        consecutive_losses = 0
        daily_loss_pct = Decimal("0")
        daily_risk_date = None
        weekly_loss_pct = Decimal("0")
        weekly_risk_week = None
        # Risk usage is attributed to the period in which the close actually occurred,
        # not the sequence in which a delayed Paper cycle happened to append it.
        daily_loss_by_date: dict[str, Decimal] = {}
        weekly_loss_by_week: dict[str, Decimal] = {}
        runs = 0
        last_run_ts = None
        events = self.read_events(account_id)

        for event in events:
            payload = event.payload
            if event.event_type == "CashAdjustment":
                balance += dec(payload.get("amount"), "amount")
            elif event.event_type == "RiskStateSnapshot":
                consecutive_losses = int(payload.get("consecutive_losses", 0))
                daily_loss_pct = dec(str(payload.get("daily_loss_pct", "0")), "daily_loss_pct")
                weekly_loss_pct = dec(str(payload.get("weekly_loss_pct", "0")), "weekly_loss_pct")
                risk_lock_active = bool(payload.get("risk_lock_active", False))
                runs = int(payload.get("runs", runs))
                last_run_ts = payload.get("last_run_ts", last_run_ts)
                daily_risk_date = payload.get("daily_risk_date", daily_risk_date)
                weekly_risk_week = payload.get("weekly_risk_week", weekly_risk_week)
                if daily_risk_date:
                    daily_loss_by_date[daily_risk_date] = daily_loss_pct
                if weekly_risk_week:
                    weekly_loss_by_week[weekly_risk_week] = weekly_loss_pct
            elif event.event_type == "DailyRiskReset":
                daily_risk_date = payload.get("daily_risk_date")
                if daily_risk_date:
                    daily_loss_by_date.setdefault(daily_risk_date, Decimal("0"))
            elif event.event_type == "WeeklyRiskReset":
                weekly_risk_week = payload.get("weekly_risk_week")
                if weekly_risk_week:
                    weekly_loss_by_week.setdefault(weekly_risk_week, Decimal("0"))
            elif event.event_type == "PaperCycleCompleted":
                runs = int(payload.get("runs", runs + 1))
                last_run_ts = payload.get("last_run_ts", last_run_ts)
                cycle_date = payload.get("daily_risk_date")
                if cycle_date and (daily_risk_date is None or cycle_date > daily_risk_date):
                    daily_risk_date = cycle_date
                    daily_loss_by_date.setdefault(cycle_date, Decimal("0"))
            elif event.event_type == "HistoricalTradeImported":
                trade = dict(payload.get("trade", {}))
                if trade:
                    closed_trades.append(trade)
            elif event.event_type == "PositionOpened":
                trade_id = event.trade_id or payload.get("trade_id")
                if not trade_id or trade_id in positions:
                    raise LedgerError("PositionOpened requires a unique trade_id")
                lots = dec(payload.get("lots"), "lots")
                if lots <= 0:
                    raise LedgerError("opened lots must be positive")
                positions[trade_id] = {
                    "trade_id": trade_id,
                    "symbol": payload.get("symbol"),
                    "direction": payload.get("direction"),
                    "lots": lots,
                    "entry_price": dec(payload.get("entry_price"), "entry_price"),
                    "sl": payload.get("sl"),
                    "tp2": payload.get("tp2"),
                    "entry_time": payload.get("entry_time") or event.occurred_at,
                    "risk_usd": payload.get("risk_usd", "0"),
                    "risk_pct": payload.get("risk_pct", "0"),
                    "final_score": payload.get("final_score", 0.0),
                    "regime": payload.get("regime", ""),
                    "session": payload.get("session", ""),
                    "sizing_result": payload.get("sizing_result"),
                    "unrealized_pnl": Decimal("0"),
                }
            elif event.event_type == "PositionMarked":
                trade_id = event.trade_id or payload.get("trade_id")
                if trade_id not in positions:
                    raise LedgerError("PositionMarked references an unknown position")
                positions[trade_id]["unrealized_pnl"] = dec(
                    payload.get("unrealized_pnl"), "unrealized_pnl"
                )
            elif event.event_type in ("PositionPartiallyClosed", "PositionClosed"):
                trade_id = event.trade_id or payload.get("trade_id")
                if trade_id not in positions:
                    raise LedgerError("close event references an unknown position")
                position = positions[trade_id]
                closed_lots = dec(payload.get("lots_closed"), "lots_closed")
                if closed_lots <= 0 or closed_lots > position["lots"]:
                    raise LedgerError("closed lots must be positive and <= remaining lots")
                if event.event_type == "PositionClosed" and closed_lots != position["lots"]:
                    raise LedgerError("PositionClosed must settle the full remaining quantity")
                gross = dec(payload.get("pnl_gross"), "pnl_gross")
                commission = dec(payload.get("commission", "0"), "commission")
                slippage = dec(payload.get("slippage", "0"), "slippage")
                swap = dec(payload.get("swap", "0"), "swap")
                net = gross - commission - slippage - swap
                realized += net
                # Position lifecycle remains sequence-ordered, but period risk is
                # projected by occurred_at so delayed historical settlements cannot
                # contaminate the current day/week budget.
                try:
                    close_date = date.fromisoformat(event.occurred_at[:10])
                except (TypeError, ValueError) as exc:
                    raise LedgerError("close event has an invalid occurred_at date") from exc
                close_day = close_date.isoformat()
                close_week = (close_date - timedelta(days=close_date.weekday())).isoformat()
                daily_loss_by_date[close_day] = (
                    daily_loss_by_date.get(close_day, Decimal("0"))
                    + dec(payload.get("daily_loss_pct_delta", "0"), "daily_loss_pct_delta")
                )
                weekly_loss_by_week[close_week] = (
                    weekly_loss_by_week.get(close_week, Decimal("0"))
                    + dec(payload.get("weekly_loss_pct_delta", "0"), "weekly_loss_pct_delta")
                )
                closed_trades.append({
                    "trade_id": trade_id,
                    "symbol": position["symbol"],
                    "direction": position["direction"],
                    "entry_time": position["entry_time"],
                    "exit_time": payload.get("exit_time") or event.occurred_at,
                    "entry": str(position["entry_price"]),
                    "exit": payload.get("exit_price", ""),
                    "lots": str(closed_lots),
                    "pnl_usd": str(net),
                    "pnl_gross": str(gross),
                    "commission": str(commission),
                    "slippage": str(slippage),
                    "swap": str(swap),
                    "outcome": payload.get("outcome", "CLOSED"),
                    "session": position["session"],
                    "regime": position["regime"],
                    "final_score": position["final_score"],
                })
                position["lots"] -= closed_lots
                if position["lots"] == 0:
                    positions.pop(trade_id)
                else:
                    position["unrealized_pnl"] = Decimal("0")
                if net < 0:
                    consecutive_losses += 1
                elif not risk_lock_active:
                    consecutive_losses = 0
                if consecutive_losses >= int(payload.get("consecutive_loss_stop", 3)):
                    risk_lock_active = True
            elif event.event_type == "RiskLockActivated":
                risk_lock_active = True
            elif event.event_type == "RiskLockReleased":
                risk_lock_active = False
                if "consecutive_losses" in payload:
                    consecutive_losses = int(payload["consecutive_losses"])

        # Rebuild loss percentages from the immutable financial timeline. A delayed
        # Paper cycle may append a historical close today; its denominator must be the
        # account equity at the start of that close's actual day/week, not today's equity
        # and not a caller-supplied percentage computed from a stale cycle snapshot.
        financial_events = [
            event for event in events
            if event.event_type in ("CashAdjustment", "PositionPartiallyClosed", "PositionClosed")
        ]

        def event_time(event: LedgerEvent) -> datetime:
            raw = event.occurred_at or event.recorded_at
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise LedgerError("financial event has an invalid occurred_at timestamp") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        financial_events.sort(key=lambda item: (event_time(item), item.sequence))
        timeline_equity = dec(initial_balance, "initial_balance")
        day_open_equity: dict[str, Decimal] = {}
        week_open_equity: dict[str, Decimal] = {}
        day_loss_usd: dict[str, Decimal] = {}
        week_loss_usd: dict[str, Decimal] = {}
        for financial_event in financial_events:
            moment = event_time(financial_event)
            day_key = moment.date().isoformat()
            week_key = (moment.date() - timedelta(days=moment.date().weekday())).isoformat()
            day_open_equity.setdefault(day_key, timeline_equity)
            week_open_equity.setdefault(week_key, timeline_equity)
            if financial_event.event_type == "CashAdjustment":
                timeline_equity += dec(financial_event.payload.get("amount"), "amount")
                continue
            fp = financial_event.payload
            net_pnl = (
                dec(fp.get("pnl_gross"), "pnl_gross")
                - dec(fp.get("commission", "0"), "commission")
                - dec(fp.get("slippage", "0"), "slippage")
                - dec(fp.get("swap", "0"), "swap")
            )
            if net_pnl < 0:
                day_loss_usd[day_key] = day_loss_usd.get(day_key, Decimal("0")) - net_pnl
                week_loss_usd[week_key] = week_loss_usd.get(week_key, Decimal("0")) - net_pnl
            timeline_equity += net_pnl

        for key, loss_usd in day_loss_usd.items():
            opening_equity = day_open_equity[key]
            daily_loss_by_date[key] = (
                loss_usd / opening_equity * Decimal("100")
                if opening_equity > 0 else Decimal("0")
            )
        for key, loss_usd in week_loss_usd.items():
            opening_equity = week_open_equity[key]
            weekly_loss_by_week[key] = (
                loss_usd / opening_equity * Decimal("100")
                if opening_equity > 0 else Decimal("0")
            )

        daily_loss_pct = daily_loss_by_date.get(
            daily_risk_date, daily_loss_pct if daily_risk_date is None else Decimal("0")
        )
        weekly_loss_pct = weekly_loss_by_week.get(
            weekly_risk_week, weekly_loss_pct if weekly_risk_week is None else Decimal("0")
        )
        unrealized = sum((p["unrealized_pnl"] for p in positions.values()), Decimal("0"))
        cash = balance + realized
        open_positions = {
            trade_id: {
                **position,
                "lots": str(position["lots"]),
                "entry_price": str(position["entry_price"]),
                "unrealized_pnl": str(position["unrealized_pnl"]),
            }
            for trade_id, position in positions.items()
        }
        return {
            "account_id": account_id,
            "cash_balance": str(cash),
            "realized_pnl": str(realized),
            "unrealized_pnl": str(unrealized),
            "equity": str(cash + unrealized),
            "open_positions": open_positions,
            "closed_trades": closed_trades,
            "risk_lock_active": risk_lock_active,
            "consecutive_losses": consecutive_losses,
            "daily_loss_pct": str(daily_loss_pct),
            "daily_risk_date": daily_risk_date,
            "weekly_loss_pct": str(weekly_loss_pct),
            "weekly_risk_week": weekly_risk_week,
            "runs": runs,
            "last_run_ts": last_run_ts,
            "last_sequence": events[-1].sequence if events else 0,
        }

    def _row_to_event(self, row: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent(
            event_id=row["event_id"], account_id=row["account_id"],
            trade_id=row["trade_id"], order_id=row["order_id"],
            event_type=row["event_type"], sequence=row["sequence"],
            occurred_at=row["occurred_at"], recorded_at=row["recorded_at"],
            idempotency_key=row["idempotency_key"], schema_version=row["schema_version"],
            payload=json.loads(row["payload_json"]),
        )
