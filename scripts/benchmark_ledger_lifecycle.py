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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()
    if args.repeats < 1 or args.samples < 2 or any(size < 2 for size in args.sizes):
        parser.error("sizes must be >= 2, repeats positive, and samples at least 2")

    results = [run_case(size, args.repeats, args.samples) for size in args.sizes]
    print(json.dumps({
        "benchmark": "SQLiteLedger full-account lifecycle validation baseline",
        "method": "fresh temporary DB per repetition; one lifecycle seed batch; timed stored-only appends; replay and event-count guards",
        "results": results,
    }, indent=2))


if __name__ == "__main__":
    main()
