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
from datetime import datetime, timezone
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
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
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
                    if existing["event_type"] != item["event_type"] or existing["payload_json"] != item["payload_json"]:
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

    def read_events(self, account_id: str, after_sequence: int = 0) -> list[LedgerEvent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM ledger_events WHERE account_id=? AND sequence>? "
                "ORDER BY sequence ASC",
                (account_id, int(after_sequence)),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def replay_account(self, account_id: str, initial_balance: str) -> dict[str, Any]:
        """Rebuild deterministic cash/equity/positions from immutable events.

        Supported accounting events:
        - CashAdjustment: payload.amount (signed decimal string)
        - PositionOpened: payload.lots, symbol, direction, entry_price
        - PositionMarked: payload.unrealized_pnl
        - PositionPartiallyClosed / PositionClosed: payload.lots_closed,
          pnl_gross, commission, slippage, swap (decimal strings)
        - RiskLockActivated / RiskLockReleased: no financial payload required
        """
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
        unrealized = Decimal("0")
        positions: dict[str, dict[str, Any]] = {}
        risk_lock_active = False

        for event in self.read_events(account_id):
            payload = event.payload
            if event.event_type == "CashAdjustment":
                balance += dec(payload.get("amount"), "amount")
            elif event.event_type == "PositionOpened":
                trade_id = event.trade_id or payload.get("trade_id")
                if not trade_id or trade_id in positions:
                    raise LedgerError("PositionOpened requires a unique trade_id")
                lots = dec(payload.get("lots"), "lots")
                if lots <= 0:
                    raise LedgerError("opened lots must be positive")
                positions[trade_id] = {
                    "trade_id": trade_id, "symbol": payload.get("symbol"),
                    "direction": payload.get("direction"), "lots": lots,
                    "entry_price": dec(payload.get("entry_price"), "entry_price"),
                    "unrealized_pnl": Decimal("0"),
                }
            elif event.event_type == "PositionMarked":
                trade_id = event.trade_id or payload.get("trade_id")
                if trade_id not in positions:
                    raise LedgerError("PositionMarked references an unknown position")
                mark = dec(payload.get("unrealized_pnl"), "unrealized_pnl")
                positions[trade_id]["unrealized_pnl"] = mark
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
                position["lots"] -= closed_lots
                if position["lots"] == 0:
                    positions.pop(trade_id)
                else:
                    position["unrealized_pnl"] = Decimal("0")
            elif event.event_type == "RiskLockActivated":
                risk_lock_active = True
            elif event.event_type == "RiskLockReleased":
                risk_lock_active = False

        unrealized = sum((p["unrealized_pnl"] for p in positions.values()), Decimal("0"))
        cash = balance + realized
        return {
            "account_id": account_id,
            "cash_balance": str(cash),
            "realized_pnl": str(realized),
            "unrealized_pnl": str(unrealized),
            "equity": str(cash + unrealized),
            "open_positions": {
                trade_id: {**position, "lots": str(position["lots"]),
                           "entry_price": str(position["entry_price"]),
                           "unrealized_pnl": str(position["unrealized_pnl"])}
                for trade_id, position in positions.items()
            },
            "risk_lock_active": risk_lock_active,
            "last_sequence": len(self.read_events(account_id)),
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
