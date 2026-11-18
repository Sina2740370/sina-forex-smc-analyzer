"""Read-only preflight for legacy JSON Paper account migration.

This module never writes, repairs, or guesses legacy account state. It only
returns a deterministic assessment; callers must not migrate unless eligible.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


def _money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _position_key(position: Mapping[str, Any]) -> tuple:
    fields = ("symbol", "direction", "entry_time", "entry", "lots")
    missing = [name for name in fields if name not in position]
    if missing:
        raise ValueError("legacy trade identity is incomplete: " + ", ".join(missing))
    return tuple(str(position[name]) for name in fields)


def _fingerprint(state: Mapping[str, Any], journal: Sequence[Mapping[str, Any]]) -> str:
    try:
        payload = json.dumps(
            {"state": state, "journal": list(journal)},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False, default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("legacy source cannot be fingerprinted safely") from None
    return hashlib.sha256(payload).hexdigest()


def inspect_legacy_paper_state(
    state: Mapping[str, Any],
    journal_records: Sequence[Mapping[str, Any]],
) -> dict:
    """Assess whether legacy Paper data is fully reconcilable, without mutation.

    Eligibility requires an explicit initial_equity, no open positions, a
    contiguous journal matching state.runs, exact equity roll-forward, and
    every journaled opening matched by one complete closing record.
    """
    if not isinstance(state, Mapping):
        raise ValueError("state must be a mapping")
    if not isinstance(journal_records, Sequence) or isinstance(
        journal_records, (str, bytes)
    ):
        raise ValueError("journal_records must be a sequence of mappings")

    reasons: list[str] = []
    try:
        fingerprint = _fingerprint(state, journal_records)
    except ValueError:
        fingerprint = None
        reasons.append("source_fingerprint_unavailable")

    if "initial_equity" not in state:
        reasons.append("initial_equity_missing_manual_reconciliation_required")
        initial_equity = None
    else:
        try:
            initial_equity = _money(state["initial_equity"], "initial_equity")
            if initial_equity <= 0:
                reasons.append("initial_equity_not_positive")
        except ValueError:
            initial_equity = None
            reasons.append("initial_equity_invalid")

    try:
        current_equity = _money(state.get("equity"), "equity")
    except ValueError:
        current_equity = None
        reasons.append("current_equity_invalid")

    runs = state.get("runs")
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 0:
        runs = None
        reasons.append("state_runs_invalid")

    open_positions = state.get("open_positions")
    if not isinstance(open_positions, list):
        reasons.append("open_positions_missing_or_invalid")
    elif open_positions:
        reasons.append("legacy_open_positions_require_manual_reconciliation")

    if runs is not None and len(journal_records) != runs:
        reasons.append("journal_run_count_mismatch")

    realized_total = Decimal("0")
    opened: dict[tuple, int] = {}
    closed: dict[tuple, int] = {}
    previous_equity = initial_equity

    for index, record in enumerate(journal_records, start=1):
        if not isinstance(record, Mapping):
            reasons.append(f"journal_record_{index}_invalid")
            continue
        cycle = record.get("cycle")
        if isinstance(cycle, bool) or cycle != index:
            reasons.append(f"journal_cycle_{index}_not_contiguous")
        try:
            cycle_equity = _money(record.get("equity"), f"journal[{index}].equity")
        except ValueError:
            reasons.append(f"journal_cycle_{index}_equity_invalid")
            cycle_equity = None

        cycle_pnl = Decimal("0")
        for kind, counts in (("opened", opened), ("closed", closed)):
            entries = record.get(kind)
            if not isinstance(entries, list):
                reasons.append(f"journal_cycle_{index}_{kind}_invalid")
                continue
            for trade in entries:
                if not isinstance(trade, Mapping):
                    reasons.append(f"journal_cycle_{index}_{kind}_trade_invalid")
                    continue
                try:
                    key = _position_key(trade)
                except ValueError:
                    reasons.append(f"journal_cycle_{index}_{kind}_identity_incomplete")
                    continue
                counts[key] = counts.get(key, 0) + 1
                if kind == "closed":
                    try:
                        trade_pnl = _money(
                            trade.get("pnl_usd"), "closed_trade.pnl_usd"
                        )
                        realized_total += trade_pnl
                        cycle_pnl += trade_pnl
                    except ValueError:
                        reasons.append(
                            f"journal_cycle_{index}_closed_pnl_invalid"
                        )

        if previous_equity is not None and cycle_equity is not None:
            expected = previous_equity + cycle_pnl
            if expected != cycle_equity:
                reasons.append(f"journal_cycle_{index}_equity_rollforward_mismatch")
        if cycle_equity is not None:
            previous_equity = cycle_equity

    if opened != closed:
        reasons.append("journal_open_close_history_not_fully_reconciled")

    if current_equity is not None and previous_equity is not None:
        if current_equity != previous_equity:
            reasons.append("state_equity_does_not_match_journal")

    if runs == 0 and initial_equity is not None and current_equity is not None:
        if initial_equity != current_equity:
            reasons.append("zero_run_equity_does_not_match_initial_equity")

    # Stable de-duplication keeps reports deterministic.
    reasons = list(dict.fromkeys(reasons))
    return {
        "eligible": not reasons,
        "status": "ELIGIBLE_FOR_SEPARATE_MIGRATION_REVIEW" if not reasons
                  else "BLOCKED_MANUAL_RECONCILIATION_REQUIRED",
        "source_fingerprint_sha256": fingerprint,
        "initial_equity": str(initial_equity) if initial_equity is not None else None,
        "current_equity": str(current_equity) if current_equity is not None else None,
        "journal_realized_pnl": str(realized_total),
        "journal_records": len(journal_records),
        "reasons": reasons,
        "mutated_source": False,
        "migration_performed": False,
    }
