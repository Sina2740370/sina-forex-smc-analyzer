"""Golden accounting contracts for the append-only Paper/Backtest ledger.

These tests intentionally assert account-level semantics rather than implementation
details. A failing test is a release blocker until the ledger projection is corrected.
"""
from decimal import Decimal

import pytest

from engine.mas.ledger import LedgerError, SQLiteLedger
from engine.mas.paper import attribution


def _open_event(*, trade_id="golden-trade", lots="1.00", risk_usd="100.00",
                risk_pct="1.00", schema_version=1, payload_extra=None):
    payload = {
        "symbol": "EURUSD",
        "direction": "BUY",
        "lots": lots,
        "entry_price": "1.10000",
        "sl": "1.09000",
        "tp2": "1.12000",
        "entry_time": "2026-11-18T09:00:00+00:00",
        "risk_usd": risk_usd,
        "risk_pct": risk_pct,
        "session": "LONDON",
        "regime": "TREND",
    }
    if payload_extra:
        payload.update(payload_extra)
    event = {
        "account_id": "golden-account",
        "trade_id": trade_id,
        "event_type": "PositionOpened",
        "idempotency_key": f"open:{trade_id}",
        "occurred_at": "2026-11-18T09:00:00+00:00",
        "payload": payload,
    }
    if schema_version != 1:
        event["schema_version"] = schema_version
    return event


def _close_event(event_type, key, *, trade_id="golden-trade", lots="0.50",
                 pnl="-20.00", occurred_at="2026-11-18T10:00:00+00:00"):
    return {
        "account_id": "golden-account",
        "trade_id": trade_id,
        "event_type": event_type,
        "idempotency_key": key,
        "occurred_at": occurred_at,
        "payload": {
            "lots_closed": lots,
            "pnl_gross": pnl,
            "commission": "0.00",
            "slippage": "0.00",
            "swap": "0.00",
        },
    }


def test_partial_exit_is_one_completed_trade_in_performance_projection(tmp_path):
    ledger = SQLiteLedger(tmp_path / "golden-partial-attribution.db")
    ledger.append_event(_open_event())
    ledger.append_event(_close_event("PositionPartiallyClosed", "partial:1"))
    ledger.append_event(_close_event(
        "PositionClosed", "close:1", lots="0.50", pnl="30.00",
        occurred_at="2026-11-18T11:00:00+00:00",
    ))

    projection = ledger.replay_account("golden-account", "10000.00")
    report = attribution(projection["closed_trades"], equity=10000.00)

    assert len(projection["closed_trades"]) == 1, (
        "partial settlements must be aggregated by trade_id after final close"
    )
    assert report["n_trades"] == 1
    assert report["win_rate_pct"] == 100.0
    assert Decimal(projection["closed_trades"][0]["pnl_usd"]) == Decimal("10.00")


def test_partial_close_recomputes_residual_position_risk(tmp_path):
    ledger = SQLiteLedger(tmp_path / "golden-residual-risk.db")
    ledger.append_event(_open_event())
    ledger.append_event(_close_event("PositionPartiallyClosed", "partial:1"))

    projection = ledger.replay_account("golden-account", "10000.00")
    remaining = projection["open_positions"]["golden-trade"]

    assert Decimal(remaining["lots"]) == Decimal("0.50")
    assert Decimal(remaining["risk_usd"]) == Decimal("50.00")
    assert Decimal(remaining["risk_pct"]) == Decimal("0.50")


def test_position_opened_v2_requires_top_level_risk_bound_to_sizing_snapshot(tmp_path):
    ledger = SQLiteLedger(tmp_path / "golden-v2-risk-binding.db")
    sizing = {
        "sizing_id": "sizing-golden-1",
        "approved": True,
        "symbol": "EURUSD",
        "direction": "BUY",
        "entry_price": "1.10000",
        "stop_loss": "1.09000",
        "lots": "1.00",
        "requested_risk": "100.00",
        "actual_risk": "100.00",
        "per_lot_risk": "100.00",
        "lot_step": "0.01",
        "min_lot": "0.01",
        "max_lot": "50.00",
        "spec_version": "eurusd-v1",
        "risk_config_hash": "risk-config-golden",
    }
    event = _open_event(
        trade_id="v2-risk-binding",
        schema_version=2,
        payload_extra={"sizing_result": sizing},
    )
    event["idempotency_key"] = "open:v2-risk-binding"
    event["payload"].pop("risk_usd")
    event["payload"].pop("risk_pct")

    with pytest.raises(LedgerError, match="risk"):
        ledger.append_event(event)

    assert ledger.read_events("golden-account") == []


def test_replay_is_deterministic_after_database_restart(tmp_path):
    path = tmp_path / "golden-replay-restart.db"
    ledger = SQLiteLedger(path)
    ledger.append_event(_open_event())
    ledger.append_event(_close_event("PositionPartiallyClosed", "partial:1"))
    ledger.append_event(_close_event(
        "PositionClosed", "close:1", lots="0.50", pnl="30.00",
        occurred_at="2026-11-18T11:00:00+00:00",
    ))

    before = ledger.replay_account("golden-account", "10000.00")
    restarted = SQLiteLedger(path)
    after = restarted.replay_account("golden-account", "10000.00")

    assert after == before
    assert after["realized_pnl"] == "10.00"
    assert after["last_sequence"] == 3
