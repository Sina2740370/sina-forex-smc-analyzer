"""Reproducible baseline benchmark for SQLiteLedger lifecycle append validation.

Run from the repository root:
    python scripts/benchmark_ledger_lifecycle.py --sizes 100 1000 10000 --repeats 3 --samples 25

This is an observational benchmark, not a pass/fail performance test. It creates
fresh temporary databases and never touches the configured Paper account.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from engine.mas.ledger import SQLiteLedger


def lifecycle_fixture(count: int) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for index in range(count):
        trade_id = f"bench-trade-{index:08d}"
        common = {
            "account_id": "benchmark",
            "trade_id": trade_id,
        }
        events.append({
            **common,
            "event_type": "PositionOpened",
            "idempotency_key": f"open-{trade_id}",
            "payload": {
                "trade_id": trade_id,
                "symbol": "EURUSD",
                "direction": "BUY",
                "lots": "0.01",
                "entry_price": "1.10000",
                "sl": "1.09000",
            },
        })
        events.append({
            **common,
            "event_type": "PositionClosed",
            "idempotency_key": f"close-{trade_id}",
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


def run_case(trades: int, repeats: int, samples: int) -> dict[str, object]:
    timings_ms: list[float] = []
    history_events = trades * 2
    for repetition in range(repeats):
        with tempfile.TemporaryDirectory(prefix="ledger-lifecycle-bench-") as temp:
            ledger = SQLiteLedger(Path(temp) / "benchmark.sqlite3")
            try:
                seed = lifecycle_fixture(trades)
                ledger.append_events(seed)
                # Warm-up is intentionally excluded from the reported sample.
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
                # Ensure the benchmark did not alter lifecycle projection semantics.
                projection = ledger.replay_account("benchmark", "10000")
                if projection["open_positions"]:
                    raise RuntimeError("benchmark fixture unexpectedly left open positions")
                if len(projection["closed_trades"]) != trades:
                    raise RuntimeError("benchmark fixture replay trade count mismatch")
            finally:
                ledger.close()

    return {
        "trades": trades,
        "lifecycle_events": history_events,
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
    if args.repeats < 1 or args.samples < 2 or any(size < 1 for size in args.sizes):
        parser.error("sizes/repeats must be positive and samples must be at least 2")

    results = [
        run_case(size, args.repeats, args.samples)
        for size in args.sizes
    ]
    print(json.dumps({
        "benchmark": "SQLiteLedger full-account lifecycle validation baseline",
        "method": "fresh temporary SQLite DB per repetition; one batched seed; timed stored-only appends; replay equivalence guard",
        "results": results,
    }, indent=2))


if __name__ == "__main__":
    main()
