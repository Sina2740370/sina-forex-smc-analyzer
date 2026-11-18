import copy

import pytest

from engine.mas.migration import inspect_legacy_paper_state


def _trade():
    return {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_time": "2026-11-17T10:00:00+00:00",
        "entry": 1.1, "lots": 0.1,
    }


def _valid_fixture():
    opened = _trade()
    closed = {**opened, "pnl_usd": "25.00"}
    journal = [{
        "cycle": 1, "equity": "10025.00",
        "opened": [opened], "closed": [closed],
    }]
    state = {
        "initial_equity": "10000.00", "equity": "10025.00",
        "runs": 1, "open_positions": [],
    }
    return state, journal


def test_preflight_accepts_only_fully_reconciled_flat_account():
    state, journal = _valid_fixture()
    report = inspect_legacy_paper_state(state, journal)
    assert report["eligible"] is True
    assert report["migration_performed"] is False
    assert report["mutated_source"] is False
    assert report["journal_realized_pnl"] == "25.00"


def test_preflight_is_read_only():
    state, journal = _valid_fixture()
    before = copy.deepcopy((state, journal))
    inspect_legacy_paper_state(state, journal)
    assert (state, journal) == before


def test_preflight_blocks_missing_initial_equity():
    state, journal = _valid_fixture()
    del state["initial_equity"]
    report = inspect_legacy_paper_state(state, journal)
    assert not report["eligible"]
    assert "initial_equity_missing_manual_reconciliation_required" in report["reasons"]


def test_preflight_blocks_open_positions():
    state, journal = _valid_fixture()
    state["open_positions"] = [_trade()]
    report = inspect_legacy_paper_state(state, journal)
    assert not report["eligible"]
    assert "legacy_open_positions_require_manual_reconciliation" in report["reasons"]


def test_preflight_blocks_equity_rollforward_mismatch():
    state, journal = _valid_fixture()
    journal[0]["equity"] = "10024.99"
    state["equity"] = "10024.99"
    report = inspect_legacy_paper_state(state, journal)
    assert not report["eligible"]
    assert "journal_cycle_1_equity_rollforward_mismatch" in report["reasons"]


def test_preflight_blocks_unmatched_open_close_history():
    state, journal = _valid_fixture()
    journal[0]["closed"] = []
    state["equity"] = "10000.00"
    journal[0]["equity"] = "10000.00"
    report = inspect_legacy_paper_state(state, journal)
    assert not report["eligible"]
    assert "journal_open_close_history_not_fully_reconciled" in report["reasons"]


def test_preflight_blocks_run_count_and_state_equity_mismatch():
    state, journal = _valid_fixture()
    state["runs"] = 2
    state["equity"] = "10030.00"
    report = inspect_legacy_paper_state(state, journal)
    assert not report["eligible"]
    assert "journal_run_count_mismatch" in report["reasons"]
    assert "state_equity_does_not_match_journal" in report["reasons"]


def test_source_fingerprint_is_deterministic_and_changes_with_source():
    state, journal = _valid_fixture()
    first = inspect_legacy_paper_state(state, journal)
    second = inspect_legacy_paper_state(state, journal)
    assert first["source_fingerprint_sha256"] == second["source_fingerprint_sha256"]
    changed = copy.deepcopy(state)
    changed["equity"] = "10026.00"
    third = inspect_legacy_paper_state(changed, journal)
    assert first["source_fingerprint_sha256"] != third["source_fingerprint_sha256"]


@pytest.mark.parametrize("journal", [None, "bad", 12])
def test_preflight_rejects_invalid_journal_container(journal):
    with pytest.raises(ValueError):
        inspect_legacy_paper_state({}, journal)
