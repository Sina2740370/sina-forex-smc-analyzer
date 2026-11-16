"""Crash-safe SQLite persistence for the deterministic MAS risk ledger.

The adapter stores an append-only event stream and a compact state snapshot in
one SQLite transaction. It deliberately has no broker, clock, network, or
strategy dependencies. SQLite durability settings are conservative defaults;
filesystem and hardware guarantees still apply.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from .ledger import RiskLedger


class SQLiteLedgerStore:
    """Persist one or more ledgers with atomic event + snapshot commits."""

    def __init__(self, database: str | Path, *, timeout: float = 5.0) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.database = str(database)
        if not self.database:
            raise ValueError("database path is required")
        self.timeout = float(timeout)
        self._initialize()

    @staticmethod
    def _ledger_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("ledger_id is required")
        return value

    @staticmethod
    def _json(value: Any) -> str:
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ValueError("ledger events and snapshots must be JSON-safe") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=self.timeout)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mas_ledger_snapshots (
                    ledger_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence >= 0),
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mas_ledger_events (
                    ledger_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    event_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (ledger_id, sequence),
                    UNIQUE (ledger_id, event_id),
                    FOREIGN KEY (ledger_id)
                        REFERENCES mas_ledger_snapshots(ledger_id)
                        ON DELETE RESTRICT
                );
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _without_history(snapshot: dict) -> dict:
        stored = dict(snapshot)
        stored.pop("applied_events", None)
        return stored

    @staticmethod
    def _replay(
        connection: sqlite3.Connection, ledger_id: str,
        schema_version: int, sequence: int, snapshot_json: str,
    ) -> RiskLedger:
        if schema_version != RiskLedger.SCHEMA_VERSION:
            raise ValueError("stored ledger schema version is unsupported")
        events = connection.execute(
            """SELECT sequence, payload_json FROM mas_ledger_events
               WHERE ledger_id = ? ORDER BY sequence""",
            (ledger_id,),
        ).fetchall()
        if len(events) != sequence:
            raise ValueError("persisted event count does not match snapshot sequence")
        for expected, (actual, _) in enumerate(events, start=1):
            if actual != expected:
                raise ValueError("persisted event sequence is not contiguous")
        snapshot = json.loads(snapshot_json)
        snapshot["applied_events"] = [json.loads(payload) for _, payload in events]
        return RiskLedger.from_snapshot(snapshot)

    def create(self, ledger_id: str, ledger: RiskLedger) -> None:
        """Create a persisted ledger only from its pristine sequence-zero state."""
        ledger_id = self._ledger_id(ledger_id)
        if not isinstance(ledger, RiskLedger):
            raise ValueError("ledger must be a RiskLedger")
        snapshot = ledger.snapshot()
        if snapshot["sequence"] != 0 or snapshot["applied_events"]:
            raise ValueError("only a pristine sequence-zero ledger can be created")
        payload = self._json(self._without_history(snapshot))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO mas_ledger_snapshots
                   (ledger_id, schema_version, sequence, snapshot_json)
                   VALUES (?, ?, 0, ?)""",
                (ledger_id, RiskLedger.SCHEMA_VERSION, payload),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            raise ValueError("ledger_id already exists") from None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append(
        self, ledger_id: str, *, expected_sequence: int,
        event: dict, ledger: RiskLedger,
    ) -> bool:
        """Atomically append one event and persist its resulting ledger state.

        Returns False for an exact retry after a successful prior commit. The
        adapter replays persisted history before accepting either a new commit
        or an idempotent retry, preventing stale writers and state divergence.
        """
        ledger_id = self._ledger_id(ledger_id)
        if isinstance(expected_sequence, bool) or not isinstance(expected_sequence, int) or expected_sequence < 0:
            raise ValueError("expected_sequence must be a non-negative integer")
        if not isinstance(event, dict):
            raise ValueError("event must be a mapping")
        if not isinstance(ledger, RiskLedger):
            raise ValueError("ledger must be a RiskLedger")
        event_json = self._json(event)
        caller_snapshot = ledger.snapshot()
        if caller_snapshot["sequence"] != expected_sequence + 1:
            raise ValueError("ledger sequence must be expected_sequence + 1")
        if not caller_snapshot["applied_events"] or caller_snapshot["applied_events"][-1] != event:
            raise ValueError("event must be the ledger's most recently applied event")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT schema_version, sequence, snapshot_json
                   FROM mas_ledger_snapshots WHERE ledger_id = ?""",
                (ledger_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown ledger_id")
            schema_version, stored_sequence, snapshot_json = row

            if stored_sequence != expected_sequence:
                if stored_sequence == expected_sequence + 1:
                    prior = connection.execute(
                        """SELECT sequence, payload_json FROM mas_ledger_events
                           WHERE ledger_id = ? AND event_id = ?""",
                        (ledger_id, event.get("event_id")),
                    ).fetchone()
                    if prior and prior[0] == expected_sequence + 1:
                        if prior[1] != event_json:
                            raise ValueError("event_id was reused with a different payload")
                        restored = self._replay(
                            connection, ledger_id, schema_version,
                            stored_sequence, snapshot_json,
                        )
                        if restored.snapshot() == caller_snapshot:
                            connection.rollback()
                            return False
                raise ValueError("stale ledger writer: persisted sequence changed")

            restored = self._replay(
                connection, ledger_id, schema_version,
                stored_sequence, snapshot_json,
            )
            restored.apply(event)
            if restored.snapshot() != caller_snapshot:
                raise ValueError("caller ledger does not match persisted event replay")

            connection.execute(
                """INSERT INTO mas_ledger_events
                   (ledger_id, sequence, event_id, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (ledger_id, expected_sequence + 1, event["event_id"], event_json),
            )
            connection.execute(
                """UPDATE mas_ledger_snapshots
                   SET schema_version = ?, sequence = ?, snapshot_json = ?
                   WHERE ledger_id = ?""",
                (
                    RiskLedger.SCHEMA_VERSION,
                    expected_sequence + 1,
                    self._json(self._without_history(caller_snapshot)),
                    ledger_id,
                ),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load(self, ledger_id: str) -> RiskLedger:
        """Load and replay-validate the complete persisted event history."""
        ledger_id = self._ledger_id(ledger_id)
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT schema_version, sequence, snapshot_json
                   FROM mas_ledger_snapshots WHERE ledger_id = ?""",
                (ledger_id,),
            ).fetchone()
            if row is None:
                raise KeyError(ledger_id)
            schema_version, sequence, snapshot_json = row
            return self._replay(
                connection, ledger_id, schema_version, sequence, snapshot_json
            )
        finally:
            connection.close()
