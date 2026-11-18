from decimal import Decimal

import pytest

from engine.mas.execution import (
    ExecutableQuote,
    ExecutionCostModel,
)
from engine.mas.ledger_store import SQLiteLedgerStore
from engine.mas.paper_adapter import PaperLedgerAdapter


def _model(**overrides):
    values = {
        "contract_size": "100000",
        "quote_to_account_rate": "1",
        "slippage_price_per_fill": "0",
        "commission_per_lot_per_side": "3.5",
        "pip_size": "0.0001",
    }
    values.update(overrides)
    return ExecutionCostModel(**values)


def test_buy_uses_ask_entry_and_bid_exit_spread():
    model = _model()
    result = model.round_trip(
        side="BUY", lots="1",
        entry_quote=ExecutableQuote("1.1000", "1.1002"),
        exit_quote=ExecutableQuote("1.1010", "1.1012"),
    )
    assert result.entry_fill == Decimal("1.1002")
    assert result.exit_fill == Decimal("1.1010")
    assert result.gross_pnl_account == Decimal("80.0000")
    assert result.commission_account == Decimal("7.0")
    assert result.net_pnl_account == Decimal("73.0000")


def test_sell_uses_bid_entry_and_ask_exit():
    model = _model(commission_per_lot_per_side="0")
    result = model.round_trip(
        side="SELL", lots="2",
        entry_quote=ExecutableQuote("1.1000", "1.1002"),
        exit_quote=ExecutableQuote("1.0990", "1.0992"),
    )
    assert result.entry_fill == Decimal("1.1000")
    assert result.exit_fill == Decimal("1.0992")
    assert result.gross_pnl_account == Decimal("160.0000")


def test_adverse_slippage_is_applied_to_both_fills():
    model = _model(slippage_price_per_fill="0.0001", commission_per_lot_per_side="0")
    result = model.round_trip(
        side="BUY", lots="1",
        entry_quote=ExecutableQuote("1.1000", "1.1002"),
        exit_quote=ExecutableQuote("1.1010", "1.1012"),
    )
    assert result.entry_fill == Decimal("1.1003")
    assert result.exit_fill == Decimal("1.1009")
    assert result.gross_pnl_account == Decimal("60.0000")


def test_signed_swap_and_other_fees_are_accounted_separately():
    result = _model().round_trip(
        side="BUY", lots="1",
        entry_quote=ExecutableQuote("1.1000", "1.1000"),
        exit_quote=ExecutableQuote("1.1000", "1.1000"),
        swap_account="-2.5", other_fees_account="1",
    )
    assert result.gross_pnl_account == 0
    assert result.commission_account == Decimal("7.0")
    assert result.swap_account == Decimal("-2.5")
    assert result.other_fees_account == Decimal("1")
    assert result.net_pnl_account == Decimal("-5.5")


def test_positive_swap_is_a_financing_debit_under_ledger_convention():
    result = _model(commission_per_lot_per_side="0").round_trip(
        side="SELL", lots="1",
        entry_quote=ExecutableQuote("1.1", "1.1"),
        exit_quote=ExecutableQuote("1.1", "1.1"),
        swap_account="4.25",
    )
    assert result.net_pnl_account == Decimal("-4.25")


def test_quote_to_account_conversion_is_explicit():
    result = _model(quote_to_account_rate="0.75", commission_per_lot_per_side="0").round_trip(
        side="BUY", lots="1",
        entry_quote=ExecutableQuote("1.1000", "1.1000"),
        exit_quote=ExecutableQuote("1.1010", "1.1010"),
    )
    assert result.gross_pnl_quote == Decimal("100.0000")
    assert result.gross_pnl_account == Decimal("75.000000")


def test_stop_gap_executes_at_worse_executable_quote():
    model = _model(slippage_price_per_fill="0.0001")
    long_fill = model.stop_exit_fill(
        side="BUY", stop_price="1.0950",
        quote=ExecutableQuote("1.0900", "1.0902"),
    )
    short_fill = model.stop_exit_fill(
        side="SELL", stop_price="1.1050",
        quote=ExecutableQuote("1.1098", "1.1100"),
    )
    assert long_fill == Decimal("1.0899")
    assert short_fill == Decimal("1.1101")


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_stop_without_gap_uses_stop_price_then_slippage(side):
    model = _model(slippage_price_per_fill="0.0001")
    quote = ExecutableQuote("1.1000", "1.1002") if side == "BUY" else ExecutableQuote("1.0998", "1.1000")
    fill = model.stop_exit_fill(side=side, stop_price="1.1000", quote=quote)
    assert fill == (Decimal("1.0999") if side == "BUY" else Decimal("1.1001"))


@pytest.mark.parametrize("value", [1.0, True, None, "NaN", "Infinity", "-1"])
def test_rejects_invalid_slippage(value):
    with pytest.raises(ValueError):
        _model(slippage_price_per_fill=value)


def test_rejects_crossed_quote_and_negative_commission():
    with pytest.raises(ValueError, match="ask cannot be below bid"):
        ExecutableQuote("1.2", "1.1")
    with pytest.raises(ValueError, match="non-negative"):
        _model(commission_per_lot_per_side="-1")


def test_rejects_unknown_side_and_float_trade_size():
    model = _model()
    quote = ExecutableQuote("1.1", "1.1002")
    with pytest.raises(ValueError, match="side"):
        model.entry_fill(side="LONG", quote=quote)
    with pytest.raises(ValueError, match="exact Decimal"):
        model.round_trip(
            side="BUY", lots=1.0, entry_quote=quote, exit_quote=quote
        )



def test_execution_result_flows_into_atomic_paper_settlement(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "execution-settlement.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:execution",
        initial_balance="10000", timestamp="2026-11-18T10:15:00Z",
    )
    position_id = adapter.commit_open_position(
        symbol="EURUSD", side="BUY", quantity="1",
        entry="1.1002", stop="1.0950",
        approved_risk="50", actual_risk="48",
        decision_timestamp="2026-11-18T10:15:00Z",
        candidate_identity="execution-test",
    )
    execution = _model().round_trip(
        side="BUY", lots="1",
        entry_quote=ExecutableQuote("1.1000", "1.1002"),
        exit_quote=ExecutableQuote("1.1010", "1.1012"),
        swap_account="-1",
    )
    events = adapter.build_execution_settlement_events(
        position_id=position_id, execution_identity="fill-close-1",
        execution_result=execution, timestamp="2026-11-18T11:00:00Z",
        remaining_unrealized_pnl="0", mark_id="mark-after-close",
    )
    assert adapter.commit_events(events) is True
    assert adapter.ledger.balance == Decimal("10074.0000")
    assert store.load("paper:execution").snapshot() == adapter.ledger.snapshot()


def test_execution_result_cannot_settle_opposite_side(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "execution-side.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:side",
        initial_balance="10000", timestamp="2026-11-18T10:15:00Z",
    )
    position_id = adapter.commit_open_position(
        symbol="EURUSD", side="BUY", quantity="1",
        entry="1.1002", stop="1.0950",
        approved_risk="50", actual_risk="48",
        decision_timestamp="2026-11-18T10:15:00Z",
        candidate_identity="side-test",
    )
    execution = _model().round_trip(
        side="SELL", lots="1",
        entry_quote=ExecutableQuote("1.1", "1.1002"),
        exit_quote=ExecutableQuote("1.101", "1.1012"),
    )
    with pytest.raises(ValueError, match="side"):
        adapter.build_execution_settlement_events(
            position_id=position_id, execution_identity="wrong-side",
            execution_result=execution, timestamp="2026-11-18T11:00:00Z",
            remaining_unrealized_pnl="0", mark_id="mark-invalid",
        )



def test_rejects_non_positive_fill_after_extreme_slippage():
    model = _model(slippage_price_per_fill="1.2")
    with pytest.raises(ValueError, match="positive"):
        model.entry_fill(
            side="SELL", quote=ExecutableQuote("1.1", "1.1002")
        )


def test_rejects_untyped_quote_objects():
    model = _model()
    with pytest.raises(ValueError, match="ExecutableQuote"):
        model.entry_fill(side="BUY", quote={"bid": "1.1", "ask": "1.2"})



def test_negative_swap_is_a_financing_credit_under_ledger_convention():
    result = _model(commission_per_lot_per_side="0").round_trip(
        side="BUY", lots="1",
        entry_quote=ExecutableQuote("1.1", "1.1"),
        exit_quote=ExecutableQuote("1.1", "1.1"),
        swap_account="-3.0",
    )
    assert result.net_pnl_account == Decimal("3.0")


def test_stop_fill_rejects_non_positive_result_after_slippage():
    model = _model(slippage_price_per_fill="2")
    with pytest.raises(ValueError, match="positive"):
        model.stop_exit_fill(
            side="BUY", stop_price="1.0",
            quote=ExecutableQuote("0.5", "0.6"),
        )
