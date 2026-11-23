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


# Event types are explicit. Stored-only events are accepted and retained but do
# not alter the accounting projection. Adding a new event requires an intentional
# contract decision here and a replay/append test.
PROJECTION_EVENT_TYPES = frozenset({
    "CashAdjustment",
    "RiskStateSnapshot",
    "DailyRiskReset",
    "WeeklyRiskReset",
    "PaperCycleCompleted",
    "HistoricalTradeImported",
    "PositionOpened",
    "PositionMarked",
    "PositionPartiallyClosed",
    "PositionClosed",
    "RiskLockActivated",
    "RiskLockReleased",
})
STORED_ONLY_EVENT_TYPES = frozenset({
    "FillRecorded",
    "AuditMarker",
})
SUPPORTED_EVENT_TYPES = PROJECTION_EVENT_TYPES | STORED_ONLY_EVENT_TYPES
SUPPORTED_SCHEMA_VERSIONS = {
    # v1 remains replayable for historical records; new financial/admin events use v2.
    "CashAdjustment": frozenset({1, 2}),
    "PositionOpened": frozenset({1, 2}),
    "RiskLockReleased": frozenset({1, 2}),
}
DEFAULT_SCHEMA_VERSIONS = frozenset({1})


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
    def _validate_position_opened_v2(payload: Mapping[str, Any], trade_id: str | None) -> None:
        """Validate a version-2 Risk Agent snapshot against its immutable open event."""
        if not isinstance(payload, Mapping):
            raise LedgerError("PositionOpened payload must be a mapping")
        snapshot = payload.get("sizing_result")
        if not isinstance(snapshot, Mapping):
            raise LedgerError("PositionOpened schema v2 requires sizing_result")

        required_text = (
            "sizing_id", "symbol", "direction", "entry_price", "stop_loss", "lots",
            "requested_risk", "actual_risk", "per_lot_risk", "lot_step",
            "min_lot", "max_lot", "spec_version", "risk_config_hash",
        )
        missing = [key for key in required_text if key not in snapshot]
        if missing:
            raise LedgerError("sizing_result missing fields: " + ", ".join(missing))
        if snapshot.get("approved") is not True:
            raise LedgerError("sizing_result must be explicitly approved")
        for key in ("sizing_id", "symbol", "direction", "spec_version", "risk_config_hash"):
            value = snapshot.get(key)
            if not isinstance(value, str) or not value.strip():
                raise LedgerError(f"sizing_result.{key} must be a non-empty string")

        symbol = payload.get("symbol")
        direction = payload.get("direction")
        if not isinstance(symbol, str) or not symbol.strip():
            raise LedgerError("PositionOpened schema v2 requires symbol")
        if not isinstance(direction, str) or direction not in ("BUY", "SELL"):
            raise LedgerError("PositionOpened schema v2 direction must be BUY or SELL")
        if snapshot["symbol"] != symbol or snapshot["direction"] != direction:
            raise LedgerError("sizing_result identity does not match PositionOpened")

        def decimal_value(value: Any, field: str) -> Decimal:
            if not isinstance(value, str):
                raise LedgerError(f"{field} must be encoded as a decimal string")
            try:
                number = Decimal(value)
            except (InvalidOperation, ValueError):
                raise LedgerError(f"{field} is not a valid decimal") from None
            if not number.is_finite() or number <= 0:
                raise LedgerError(f"{field} must be finite and positive")
            return number

        position_lots = decimal_value(payload.get("lots"), "lots")
        position_entry = decimal_value(payload.get("entry_price"), "entry_price")
        position_stop = decimal_value(payload.get("sl"), "sl")
        snap_lots = decimal_value(snapshot.get("lots"), "sizing_result.lots")
        snap_entry = decimal_value(snapshot.get("entry_price"), "sizing_result.entry_price")
        snap_stop = decimal_value(snapshot.get("stop_loss"), "sizing_result.stop_loss")
        requested = decimal_value(snapshot.get("requested_risk"), "sizing_result.requested_risk")
        actual = decimal_value(snapshot.get("actual_risk"), "sizing_result.actual_risk")
        per_lot = decimal_value(snapshot.get("per_lot_risk"), "sizing_result.per_lot_risk")
        lot_step = decimal_value(snapshot.get("lot_step"), "sizing_result.lot_step")
        min_lot = decimal_value(snapshot.get("min_lot"), "sizing_result.min_lot")
        max_lot = decimal_value(snapshot.get("max_lot"), "sizing_result.max_lot")

        if (snap_lots, snap_entry, snap_stop) != (position_lots, position_entry, position_stop):
            raise LedgerError("sizing_result lots/entry/stop do not match PositionOpened")
        if actual > requested or actual != per_lot * snap_lots:
            raise LedgerError("sizing_result risk arithmetic is inconsistent")
        if min_lot > max_lot or not min_lot <= snap_lots <= max_lot:
            raise LedgerError("sizing_result lots are outside min/max bounds")
        if (snap_lots - min_lot) % lot_step != 0:
            raise LedgerError("sizing_result lots are not aligned to lot_step")
        if direction == "BUY" and not snap_stop < snap_entry:
            raise LedgerError("BUY stop_loss must be below entry_price")
        if direction == "SELL" and not snap_stop > snap_entry:
            raise LedgerError("SELL stop_loss must be above entry_price")
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise LedgerError("schema v2 PositionOpened requires a non-empty trade_id")

    @staticmethod
    def _validate_cash_adjustment_v2(payload: Mapping[str, Any]) -> None:
        """Require provenance and a finite non-zero amount for new cash movements."""
        amount = payload.get("amount")
        if not isinstance(amount, str):
            raise LedgerError("CashAdjustment schema v2 amount must be a decimal string")
        try:
            value = Decimal(amount)
        except (InvalidOperation, ValueError):
            raise LedgerError("CashAdjustment schema v2 amount is not a valid decimal") from None
        if not value.is_finite() or value == 0:
            raise LedgerError("CashAdjustment schema v2 amount must be finite and non-zero")
        for field in ("actor", "reason", "approval_reference", "source"):
            item = payload.get(field)
            if not isinstance(item, str) or not item.strip():
                raise LedgerError(f"CashAdjustment schema v2 requires non-empty {field}")

    @staticmethod
    def _validate_risk_lock_released_v2(payload: Mapping[str, Any]) -> None:
        """Require attributable release metadata; this is not identity authentication."""
        for field in ("reviewed_by", "reason", "approval_reference"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise LedgerError(
                    f"RiskLockReleased schema v2 requires non-empty {field}"
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
        if normalized["event_type"] not in SUPPORTED_EVENT_TYPES:
            raise LedgerError(f"unsupported event_type: {normalized['event_type']}")
        payload = normalized["payload"]
        if not isinstance(payload, Mapping):
            raise LedgerError("event payload must be a mapping")
        normalized["payload_json"] = _canonical_payload(payload)
        if normalized["event_type"] == "PositionOpened":
            trade_id = normalized.get("trade_id") or payload.get("trade_id")
            if not isinstance(trade_id, str) or not trade_id.strip():
                raise LedgerError("PositionOpened requires a non-empty trade_id")
            normalized["trade_id"] = trade_id
        normalized["event_id"] = str(normalized.get("event_id") or uuid.uuid4())
        schema_version = normalized.get("schema_version", 1)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise LedgerError("schema_version must be an integer")
        allowed_versions = SUPPORTED_SCHEMA_VERSIONS.get(
            normalized["event_type"], DEFAULT_SCHEMA_VERSIONS
        )
        if schema_version not in allowed_versions:
            raise LedgerError(
                f"unsupported schema_version {schema_version} for {normalized['event_type']}"
            )
        normalized["schema_version"] = schema_version
        if normalized["event_type"] == "CashAdjustment":
            if schema_version != 2:
                raise LedgerError("new CashAdjustment events require schema_version 2")
            SQLiteLedger._validate_cash_adjustment_v2(payload)
        if normalized["event_type"] == "RiskLockReleased":
            if schema_version != 2:
                raise LedgerError("new RiskLockReleased events require schema_version 2")
            SQLiteLedger._validate_risk_lock_released_v2(payload)
        if normalized["event_type"] in ("PositionPartiallyClosed", "PositionClosed"):
            for cost_field in ("commission", "slippage", "swap"):
                raw_cost = payload.get(cost_field, "0")
                if not isinstance(raw_cost, str):
                    raise LedgerError(f"{cost_field} must be encoded as a decimal string")
                try:
                    cost = Decimal(raw_cost)
                except InvalidOperation as exc:
                    raise LedgerError(f"{cost_field} is not a valid decimal") from exc
                if not cost.is_finite():
                    raise LedgerError(f"{cost_field} must be finite")
                if cost_field in ("commission", "slippage") and cost < 0:
                    raise LedgerError(f"{cost_field} cannot be negative")
        if normalized["schema_version"] == 2 and normalized["event_type"] == "PositionOpened":
            SQLiteLedger._validate_position_opened_v2(
                normalized["payload"], normalized.get("trade_id") or normalized["payload"].get("trade_id")
            )
        normalized["_occurred_at_explicit"] = bool(normalized.get("occurred_at"))
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

    @staticmethod
    def _validate_position_lifecycle_events(events: Iterable[LedgerEvent]) -> None:
        """Validate the complete ordered position lifecycle before transaction commit.

        This deliberately validates only lifecycle invariants and does not require
        account metadata or a balance baseline. Replay remains the full projection
        validator and independently checks the same persisted history.
        """
        positions: dict[str, Decimal] = {}
        seen_trade_ids: set[str] = set()

        def decimal_field(value: Any, field: str) -> Decimal:
            if not isinstance(value, str):
                raise LedgerError(f"{field} must be encoded as a decimal string")
            try:
                number = Decimal(value)
            except (InvalidOperation, ValueError):
                raise LedgerError(f"{field} is not a valid decimal") from None
            if not number.is_finite():
                raise LedgerError(f"{field} must be finite")
            return number

        for event in events:
            payload = event.payload
            if event.event_type == "PositionOpened":
                trade_id = event.trade_id or payload.get("trade_id")
                if not isinstance(trade_id, str) or not trade_id.strip():
                    raise LedgerError("PositionOpened requires a non-empty trade_id")
                if trade_id in seen_trade_ids:
                    raise LedgerError(
                        "PositionOpened trade_id must be unique for the account lifetime"
                    )
                lots = decimal_field(payload.get("lots"), "lots")
                entry_price = decimal_field(payload.get("entry_price"), "entry_price")
                if lots <= 0 or entry_price <= 0:
                    raise LedgerError("opened lots and entry_price must be positive")
                if event.schema_version == 2:
                    SQLiteLedger._validate_position_opened_v2(payload, trade_id)
                seen_trade_ids.add(trade_id)
                positions[trade_id] = lots
            elif event.event_type == "PositionMarked":
                trade_id = event.trade_id or payload.get("trade_id")
                if trade_id not in positions:
                    raise LedgerError("PositionMarked references an unknown position")
                decimal_field(payload.get("unrealized_pnl"), "unrealized_pnl")
            elif event.event_type in ("PositionPartiallyClosed", "PositionClosed"):
                trade_id = event.trade_id or payload.get("trade_id")
                if trade_id not in positions:
                    raise LedgerError("close event references an unknown position")
                remaining = positions[trade_id]
                closed_lots = decimal_field(payload.get("lots_closed"), "lots_closed")
                if closed_lots <= 0 or closed_lots > remaining:
                    raise LedgerError("closed lots must be positive and <= remaining lots")
                if event.event_type == "PositionClosed" and closed_lots != remaining:
                    raise LedgerError(
                        "PositionClosed must settle the full remaining quantity"
                    )
                if event.event_type == "PositionPartiallyClosed" and closed_lots >= remaining:
                    raise LedgerError(
                        "PositionPartiallyClosed must leave a positive remaining quantity"
                    )
                decimal_field(payload.get("pnl_gross"), "pnl_gross")
                for field in ("commission", "slippage", "swap"):
                    value = decimal_field(payload.get(field, "0"), field)
                    if field in ("commission", "slippage") and value < 0:
                        raise LedgerError(f"{field} cannot be negative")
                remaining -= closed_lots
                if remaining == 0:
                    positions.pop(trade_id)
                else:
                    positions[trade_id] = remaining

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
            new_trade_ids: set[tuple[str, str]] = set()
            for item in prepared:
                existing = conn.execute(
                    "SELECT * FROM ledger_events WHERE account_id=? AND idempotency_key=?",
                    (item["account_id"], item["idempotency_key"]),
                ).fetchone()
                if existing is not None:
                    identity_fields = ("event_type", "trade_id", "order_id",
                                        "schema_version", "payload_json")
                    timestamp_conflict = (
                        item.get("_occurred_at_explicit")
                        and existing["occurred_at"] != item["occurred_at"]
                    )
                    if timestamp_conflict or any(
                        existing[field] != item.get(field) for field in identity_fields
                    ):
                        raise LedgerConflictError(
                            "idempotency_key already exists with a different event body"
                        )
                    results.append((self._row_to_event(existing), False))
                    continue

                if item["event_type"] == "PositionOpened":
                    trade_id = item.get("trade_id") or item["payload"].get("trade_id")
                    if not isinstance(trade_id, str) or not trade_id.strip():
                        raise LedgerError("PositionOpened requires a non-empty trade_id")
                    identity = (item["account_id"], trade_id)
                    if identity in new_trade_ids:
                        raise LedgerConflictError(
                            "trade_id has already been used by a PositionOpened event"
                        )
                    prior_open = conn.execute(
                        "SELECT 1 FROM ledger_events "
                        "WHERE account_id=? AND event_type='PositionOpened' AND trade_id=? LIMIT 1",
                        identity,
                    ).fetchone()
                    if prior_open is not None:
                        raise LedgerConflictError(
                            "trade_id has already been used by a PositionOpened event"
                        )
                    new_trade_ids.add(identity)

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
            # Validate the post-batch lifecycle against the transaction's own
            # view before commit. Any invalid event rolls back every insert in
            # this batch; SQLite BEGIN IMMEDIATE serializes competing writers.
            for account_id in {item["account_id"] for item in prepared}:
                rows = conn.execute(
                    "SELECT * FROM ledger_events WHERE account_id=? ORDER BY sequence ASC",
                    (account_id,),
                ).fetchall()
                self._validate_position_lifecycle_events(
                    self._row_to_event(row) for row in rows
                )
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
        seen_trade_ids: set[str] = set()
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
            if event.event_type not in SUPPORTED_EVENT_TYPES:
                raise LedgerError(f"unsupported persisted event_type: {event.event_type}")
            allowed_versions = SUPPORTED_SCHEMA_VERSIONS.get(
                event.event_type, DEFAULT_SCHEMA_VERSIONS
            )
            if event.schema_version not in allowed_versions:
                raise LedgerError(
                    f"unsupported persisted schema_version {event.schema_version} "
                    f"for {event.event_type}"
                )
            payload = event.payload
            if event.event_type == "CashAdjustment" and event.schema_version == 2:
                self._validate_cash_adjustment_v2(payload)
            if event.event_type == "RiskLockReleased" and event.schema_version == 2:
                self._validate_risk_lock_released_v2(payload)
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
                if event.schema_version == 2:
                    self._validate_position_opened_v2(payload, event.trade_id or payload.get("trade_id"))
                trade_id = event.trade_id or payload.get("trade_id")
                if not trade_id or trade_id in seen_trade_ids:
                    raise LedgerError("PositionOpened trade_id must be unique for the account lifetime")
                seen_trade_ids.add(trade_id)
                lots = dec(payload.get("lots"), "lots")
                entry_price = dec(payload.get("entry_price"), "entry_price")
                if lots <= 0:
                    raise LedgerError("opened lots must be positive")
                if entry_price <= 0:
                    raise LedgerError("opened entry_price must be positive")
                positions[trade_id] = {
                    "trade_id": trade_id,
                    "symbol": payload.get("symbol"),
                    "direction": payload.get("direction"),
                    "lots": lots,
                    "entry_price": entry_price,
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
                if event.event_type == "PositionPartiallyClosed" and closed_lots >= position["lots"]:
                    raise LedgerError(
                        "PositionPartiallyClosed must leave a positive remaining quantity"
                    )
                gross = dec(payload.get("pnl_gross"), "pnl_gross")
                commission = dec(payload.get("commission", "0"), "commission")
                slippage = dec(payload.get("slippage", "0"), "slippage")
                swap = dec(payload.get("swap", "0"), "swap")
                if commission < 0 or slippage < 0:
                    raise LedgerError("commission and slippage cannot be negative")
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
                # Percentage risk is derived below from net financial events and
                # historical opening equity. Caller-supplied deltas are retained in
                # the event for audit/legacy compatibility, but must not be added:
                # doing so counts the same realized loss twice.
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

        # Rebuild daily/weekly loss from the complete equity timeline, including
        # the latest mark for every open position. Marks replace prior marks;
        # they are never accumulated. Period loss is the maximum adverse move
        # from that period's opening equity, including realized and floating PnL.
        financial_events = [
            event for event in events
            if event.event_type in (
                "CashAdjustment", "DailyRiskReset", "WeeklyRiskReset",
                "PositionOpened", "PositionMarked",
                "PositionPartiallyClosed", "PositionClosed",
            )
        ]

        def event_time(event: LedgerEvent) -> datetime:
            raw = event.occurred_at or event.recorded_at
            if event.event_type == "PositionMarked":
                raw = event.payload.get("mark_time") or raw
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise LedgerError("financial event has an invalid occurred_at timestamp") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        financial_events.sort(key=lambda item: (event_time(item), item.sequence))
        timeline_cash = dec(initial_balance, "initial_balance")
        timeline_unrealized: dict[str, Decimal] = {}
        day_open_equity: dict[str, Decimal] = {}
        week_open_equity: dict[str, Decimal] = {}
        day_loss_usd: dict[str, Decimal] = {}
        week_loss_usd: dict[str, Decimal] = {}

        for financial_event in financial_events:
            moment = event_time(financial_event)
            day_key = moment.date().isoformat()
            week_key = (moment.date() - timedelta(days=moment.date().weekday())).isoformat()
            current_equity_before = timeline_cash + sum(
                timeline_unrealized.values(), Decimal("0")
            )
            day_open_equity.setdefault(day_key, current_equity_before)
            week_open_equity.setdefault(week_key, current_equity_before)

            event_type = financial_event.event_type
            fp = financial_event.payload
            trade_id = financial_event.trade_id or fp.get("trade_id")

            if event_type == "CashAdjustment":
                timeline_cash += dec(fp.get("amount"), "amount")
            elif event_type in ("DailyRiskReset", "WeeklyRiskReset", "PositionOpened"):
                pass
            elif event_type == "PositionMarked":
                if not trade_id:
                    raise LedgerError("PositionMarked requires trade_id for risk replay")
                timeline_unrealized[trade_id] = dec(
                    fp.get("unrealized_pnl"), "unrealized_pnl"
                )
            elif event_type in ("PositionPartiallyClosed", "PositionClosed"):
                if not trade_id:
                    raise LedgerError("close event requires trade_id for risk replay")
                net_pnl = (
                    dec(fp.get("pnl_gross"), "pnl_gross")
                    - dec(fp.get("commission", "0"), "commission")
                    - dec(fp.get("slippage", "0"), "slippage")
                    - dec(fp.get("swap", "0"), "swap")
                )
                timeline_cash += net_pnl
                # A close settles the marked PnL into cash. Partial-close
                # marks are reset too, preventing the old full-position mark
                # from being counted alongside the realized partial result.
                timeline_unrealized.pop(trade_id, None)

            current_equity = timeline_cash + sum(
                timeline_unrealized.values(), Decimal("0")
            )
            day_loss_usd[day_key] = max(
                day_loss_usd.get(day_key, Decimal("0")),
                max(Decimal("0"), day_open_equity[day_key] - current_equity),
            )
            week_loss_usd[week_key] = max(
                week_loss_usd.get(week_key, Decimal("0")),
                max(Decimal("0"), week_open_equity[week_key] - current_equity),
            )

        for key, loss_usd in day_loss_usd.items():
            opening_equity = day_open_equity[key]
            calculated_pct = (
                loss_usd / opening_equity * Decimal("100")
                if opening_equity > 0 else Decimal("0")
            )
            # A legacy RiskStateSnapshot is a migration baseline for
            # losses that predate this event ledger; add only the subsequent
            # timeline excursion to that one-time baseline.
            daily_loss_by_date[key] = (
                daily_loss_by_date.get(key, Decimal("0")) + calculated_pct
            )
        for key, loss_usd in week_loss_usd.items():
            opening_equity = week_open_equity[key]
            calculated_pct = (
                loss_usd / opening_equity * Decimal("100")
                if opening_equity > 0 else Decimal("0")
            )
            weekly_loss_by_week[key] = (
                weekly_loss_by_week.get(key, Decimal("0")) + calculated_pct
            )

        # Consecutive-loss state is a trade-level metric, not an append-order
        # metric. Paper can append delayed settlements in a different order from
        # their actual exit timestamps. Aggregate partial fills per trade and apply
        # completed trades in exit-time order. Explicit lock release remains a
        # separate, auditable event and never follows automatically from a win.
        risk_events = [
            event for event in events
            if event.event_type in (
                "RiskLockActivated", "RiskLockReleased",
                "PositionPartiallyClosed", "PositionClosed",
            )
        ]
        snapshots = [event for event in events if event.event_type == "RiskStateSnapshot"]
        # RiskStateSnapshot is a one-time migration baseline, not a recurring
        # state mutation. It is authoritative only when it predates every
        # risk-changing event in append order. A late/stale snapshot must never
        # overwrite a newer lock or loss streak merely because its occurred_at
        # sorts later on the historical timeline.
        baseline = min(snapshots, key=lambda item: item.sequence) if snapshots else None
        if baseline is not None and any(item.sequence < baseline.sequence for item in risk_events):
            baseline = None
        risk_timeline = sorted(risk_events, key=lambda item: (event_time(item), item.sequence))
        chronological_streak = (
            int(baseline.payload.get("consecutive_losses", 0)) if baseline else 0
        )
        chronological_lock = (
            bool(baseline.payload.get("risk_lock_active", False)) if baseline else False
        )
        trade_net_totals: dict[str, Decimal] = {}
        for risk_event in risk_timeline:
            rp = risk_event.payload
            if risk_event.event_type == "RiskLockActivated":
                chronological_lock = True
            elif risk_event.event_type == "RiskLockReleased":
                chronological_lock = False
                chronological_streak = int(rp.get("consecutive_losses", 0))
            elif risk_event.event_type in ("PositionPartiallyClosed", "PositionClosed"):
                trade_id = risk_event.trade_id or rp.get("trade_id")
                if not trade_id:
                    raise LedgerError("close event requires trade_id for risk replay")
                net_piece = (
                    dec(rp.get("pnl_gross"), "pnl_gross")
                    - dec(rp.get("commission", "0"), "commission")
                    - dec(rp.get("slippage", "0"), "slippage")
                    - dec(rp.get("swap", "0"), "swap")
                )
                trade_net_totals[trade_id] = trade_net_totals.get(
                    trade_id, Decimal("0")
                ) + net_piece
                if risk_event.event_type == "PositionClosed":
                    total_trade_net = trade_net_totals.pop(trade_id)
                    if total_trade_net < 0:
                        chronological_streak += 1
                    elif not chronological_lock:
                        # A winning trade resets an ordinary loss streak, but it
                        # cannot clear a hard lock. Only RiskLockReleased may do that.
                        chronological_streak = 0
                    if chronological_streak >= int(rp.get("consecutive_loss_stop", 3)):
                        chronological_lock = True

        consecutive_losses = chronological_streak
        risk_lock_active = chronological_lock

        # Reset events are preferred explicit period markers, but the equity
        # timeline must remain correct for accounts that have not emitted them yet.
        if financial_events:
            latest_period_moment = event_time(financial_events[-1])
            if daily_risk_date is None:
                daily_risk_date = latest_period_moment.date().isoformat()
            if weekly_risk_week is None:
                weekly_risk_week = (
                    latest_period_moment.date()
                    - timedelta(days=latest_period_moment.date().weekday())
                ).isoformat()

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
        def decimal_text(value: Decimal) -> str:
            # Stable non-exponent representation without insignificant trailing zeros.
            return format(value.normalize(), "f")

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
            "daily_loss_pct": decimal_text(daily_loss_pct),
            "daily_risk_date": daily_risk_date,
            "weekly_loss_pct": decimal_text(weekly_loss_pct),
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
