"""Reproducible baseline benchmark for SQLiteLedger lifecycle append validation.

Run from the repository root:
    python scripts/benchmark_ledger_lifecycle.py --sizes 100 1000 10000 --repeats 3 --samples 25

This observational benchmark uses fresh temporary databases and never touches
the configured Paper account.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
import sys
from pathlib import Path

# Executing a script by path puts scripts/ (not the repository root) on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.mas.ledger import SQLiteLedger


def lifecycle_fixture(event_count: int) -> list[dict[str, object]]:
    if event_count < 2:
        raise ValueError("event_count must be at least 2")
    trade_id = "bench-trade-single"
    common = {"account_id": "benchmark", "trade_id": trade_id}
    events: list[dict[str, object]] = [{
        **common,
        "event_type": "PositionOpened",
        "idempotency_key": "open-benchmark-trade",
        "payload": {
            "trade_id": trade_id,
            "symbol": "EURUSD",
            "direction": "BUY",
            "lots": "0.01",
            "entry_price": "1.10000",
            "sl": "1.09000",
        },
    }]
    for index in range(event_count - 2):
        events.append({
            **common,
            "event_type": "PositionMarked",
            "idempotency_key": f"mark-{index}",
            "payload": {"trade_id": trade_id, "unrealized_pnl": "0.01"},
        })
    events.append({
        **common,
        "event_type": "PositionClosed",
        "idempotency_key": "close-benchmark-trade",
        "payload": {
            "trade_id": trade_id,
            "lots_closed": "0.01",
            "pnl_gross": "0.10",
            "commission": "0",
            "slippage": "0",
            "swap": "0",
        },
    })
    return events


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def run_case(event_count: int, repeats: int, samples: int) -> dict[str, object]:
    timings_ms: list[float] = []
    for repetition in range(repeats):
        with tempfile.TemporaryDirectory(prefix="ledger-lifecycle-bench-") as temp:
            ledger = SQLiteLedger(Path(temp) / "benchmark.sqlite3")
            try:
                ledger.append_events(lifecycle_fixture(event_count))
                ledger.append_event({
                    "account_id": "benchmark",
                    "event_type": "AuditMarker",
                    "idempotency_key": f"warmup-{repetition}",
                    "payload": {"kind": "benchmark-warmup"},
                })
                for sample in range(samples):
                    start = time.perf_counter()
                    ledger.append_event({
                        "account_id": "benchmark",
                        "event_type": "AuditMarker",
                        "idempotency_key": f"sample-{repetition}-{sample}",
                        "payload": {"kind": "benchmark-sample", "sample": sample},
                    })
                    timings_ms.append((time.perf_counter() - start) * 1000.0)
                projection = ledger.replay_account("benchmark", "10000")
                if projection["open_positions"]:
                    raise RuntimeError("benchmark fixture unexpectedly left open positions")
                if len(projection["closed_trades"]) != 1:
                    raise RuntimeError("benchmark fixture replay trade count mismatch")
                if len(ledger.read_events("benchmark")) != event_count + samples + 1:
                    raise RuntimeError("benchmark event count mismatch")
            finally:
                ledger.close()

    return {
        "history_events": event_count,
        "append_samples": len(timings_ms),
        "median_ms": round(statistics.median(timings_ms), 4),
        "p95_ms": round(percentile(timings_ms, 0.95), 4),
        "min_ms": round(min(timings_ms), 4),
        "max_ms": round(max(timings_ms), 4),
    }



def open_lifecycle_fixture(event_count: int) -> list[dict[str, object]]:
    """Create an active position with a dense mark history."""
    if event_count < 2:
        raise ValueError("event_count must be at least 2")
    trade_id = "bench-dense-trade"
    common = {"account_id": "benchmark-dense", "trade_id": trade_id}
    events: list[dict[str, object]] = [{
        **common,
        "event_type": "PositionOpened",
        "idempotency_key": "dense-open",
        "payload": {
            "trade_id": trade_id,
            "symbol": "EURUSD",
            "direction": "BUY",
            "lots": "0.01",
            "entry_price": "1.10000",
        },
    }]
    for index in range(event_count - 1):
        events.append({
            **common,
            "event_type": "PositionMarked",
            "idempotency_key": f"dense-seed-mark-{index}",
            "payload": {"trade_id": trade_id, "unrealized_pnl": "0.01"},
        })
    return events


def run_dense_lifecycle_case(
    history_events: int, repeats: int, samples: int
) -> dict[str, object]:
    """Measure real lifecycle appends with/without the partial index."""
    configurations: dict[str, dict[str, object]] = {}
    for indexed in (True, False):
        timings_ms: list[float] = []
        index_bytes: int | None = None
        page_count: int | None = None
        for repetition in range(repeats):
            with tempfile.TemporaryDirectory(prefix="ledger-dense-append-") as temp:
                ledger = SQLiteLedger(Path(temp) / "dense.sqlite3")
                conn = ledger._connect()
                try:
                    if not indexed:
                        conn.execute(
                            "DROP INDEX idx_ledger_position_lifecycle_sequence"
                        )
                    conn.commit()
                finally:
                    conn.close()

                try:
                    ledger.append_events(open_lifecycle_fixture(history_events))
                    for sample in range(samples):
                        event = {
                            "account_id": "benchmark-dense",
                            "trade_id": "bench-dense-trade",
                            "event_type": "PositionMarked",
                            "idempotency_key": f"dense-sample-{repetition}-{sample}",
                            "payload": {
                                "trade_id": "bench-dense-trade",
                                "unrealized_pnl": "0.02",
                            },
                        }
                        start = time.perf_counter()
                        ledger.append_event(event)
                        timings_ms.append((time.perf_counter() - start) * 1000.0)

                    projection = ledger.replay_account("benchmark-dense", "10000")
                    if "bench-dense-trade" not in projection["open_positions"]:
                        raise RuntimeError("dense benchmark unexpectedly closed its position")
                    if len(ledger.read_events("benchmark-dense")) != history_events + samples:
                        raise RuntimeError("dense benchmark event count mismatch")

                    conn = ledger._connect()
                    try:
                        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
                        try:
                            row = conn.execute(
                                "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name=?",
                                ("idx_ledger_position_lifecycle_sequence",),
                            ).fetchone()
                            current_index_bytes = int(row[0])
                            index_bytes = (
                                current_index_bytes if index_bytes is None
                                else max(index_bytes, current_index_bytes)
                            )
                        except sqlite3.OperationalError:
                            index_bytes = None
                    finally:
                        conn.close()
                finally:
                    ledger.close()

        configurations["indexed" if indexed else "index_disabled_control"] = {
            "append_samples": len(timings_ms),
            "median_ms": round(statistics.median(timings_ms), 4),
            "p95_ms": round(percentile(timings_ms, 0.95), 4),
            "index_bytes": index_bytes if indexed else 0,
            "database_pages_last_repetition": page_count,
        }
    return {"history_events": history_events, "configurations": configurations}


def run_index_query_case(history_events: int, repeats: int, samples: int) -> dict[str, object]:
    """Compare full-account scans with indexed lifecycle-only reads on mixed history."""
    if history_events < 2:
        raise ValueError("history_events must be at least 2")

    with tempfile.TemporaryDirectory(prefix="ledger-lifecycle-index-bench-") as temp:
        ledger = SQLiteLedger(Path(temp) / "mixed-history.sqlite3")
        ledger.append_event({
            "account_id": "benchmark-index",
            "trade_id": "benchmark-open",
            "event_type": "PositionOpened",
            "idempotency_key": "benchmark-open",
            "payload": {
                "trade_id": "benchmark-open",
                "symbol": "EURUSD",
                "direction": "BUY",
                "lots": "0.01",
                "entry_price": "1.10000",
            },
        })
        conn = ledger._connect()
        try:
            conn.executemany(
                """
                INSERT INTO ledger_events (
                    event_id, account_id, trade_id, order_id, event_type, sequence,
                    occurred_at, recorded_at, idempotency_key, schema_version, payload_json
                ) VALUES (?, ?, NULL, NULL, 'AuditMarker', ?, ?, ?, ?, 1, ?)
                """,
                [
                    (
                        f"noise-{index}", "benchmark-index", index + 2,
                        "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                        f"noise-{index}", json.dumps({"noise": index}),
                    )
                    for index in range(history_events - 1)
                ],
            )
            conn.commit()

            full_sql = (
                "SELECT * FROM ledger_events WHERE account_id=? ORDER BY sequence ASC"
            )
            lifecycle_sql = """
                SELECT * FROM ledger_events
                WHERE account_id=?
                  AND event_type IN (
                      'PositionOpened', 'PositionMarked',
                      'PositionPartiallyClosed', 'PositionClosed'
                  )
                ORDER BY sequence ASC
            """
            plan = conn.execute(
                "EXPLAIN QUERY PLAN " + lifecycle_sql, ("benchmark-index",)
            ).fetchall()
            plan_detail = " ".join(row["detail"] for row in plan)
            if "idx_ledger_position_lifecycle_sequence" not in plan_detail:
                raise RuntimeError("SQLite did not select the lifecycle partial index")

            full_ms: list[float] = []
            indexed_ms: list[float] = []
            expected_lifecycle = None
            for _ in range(repeats):
                for _sample in range(samples):
                    start = time.perf_counter()
                    full_rows = conn.execute(full_sql, ("benchmark-index",)).fetchall()
                    full_ms.append((time.perf_counter() - start) * 1000.0)
                    lifecycle_rows = [row for row in full_rows if row["event_type"] in {
                        "PositionOpened", "PositionMarked",
                        "PositionPartiallyClosed", "PositionClosed",
                    }]
                    start = time.perf_counter()
                    indexed_rows = conn.execute(
                        lifecycle_sql, ("benchmark-index",)
                    ).fetchall()
                    indexed_ms.append((time.perf_counter() - start) * 1000.0)
                    if [row["event_id"] for row in lifecycle_rows] != [
                        row["event_id"] for row in indexed_rows
                    ]:
                        raise RuntimeError("indexed lifecycle query changed event selection")
                    expected_lifecycle = len(indexed_rows)
            return {
                "history_events": history_events,
                "lifecycle_events": expected_lifecycle,
                "full_account_scan_median_ms": round(statistics.median(full_ms), 4),
                "indexed_lifecycle_scan_median_ms": round(statistics.median(indexed_ms), 4),
                "query_plan": plan_detail,
            }
        finally:
            conn.close()
            ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()
    if args.repeats < 1 or args.samples < 2 or any(size < 2 for size in args.sizes):
        parser.error("sizes must be >= 2, repeats positive, and samples at least 2")

    results = [run_case(size, args.repeats, args.samples) for size in args.sizes]
    index_results = [
        run_index_query_case(size, args.repeats, args.samples) for size in args.sizes
    ]
    dense_lifecycle_results = [
        run_dense_lifecycle_case(size, args.repeats, args.samples) for size in args.sizes
    ]
    print(json.dumps({
        "benchmark": "SQLiteLedger lifecycle validation",
        "method": "fresh temporary DBs; stored-only append timing plus mixed-history full-scan versus indexed lifecycle-query comparison; replay, query-plan, and event-equivalence guards",
        "results": results,
        "indexed_query_comparison": index_results,
        "dense_lifecycle_append_comparison": dense_lifecycle_results,
    }, indent=2))


if __name__ == "__main__":
    main()
