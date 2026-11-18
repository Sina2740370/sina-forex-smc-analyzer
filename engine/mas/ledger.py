"""Transactional, append-only SQLite event ledger for Paper/Backtest accounting.

This module deliberately contains no broker or live-execution integration.
Financial quantities in event payloads must be serialized as decimal strings.
"""
from __future__ import annotations

import json
import sqlite3
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
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ledger_events WHERE account_id=? AND sequence>? "
                "ORDER BY sequence ASC",
                (account_id, int(after_sequence)),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def _row_to_event(self, row: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent(
            event_id=row["event_id"], account_id=row["account_id"],
            trade_id=row["trade_id"], order_id=row["order_id"],
            event_type=row["event_type"], sequence=row["sequence"],
            occurred_at=row["occurred_at"], recorded_at=row["recorded_at"],
            idempotency_key=row["idempotency_key"], schema_version=row["schema_version"],
            payload=json.loads(row["payload_json"]),
        )
