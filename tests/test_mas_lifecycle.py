from decimal import Decimal

import pytest

from engine.mas.execution import ExecutableQuote, ExecutionCostModel
from engine.mas.ledger import RiskLedger
from engine.mas.ledger_store import SQLiteLedgerStore
from engine.mas.paper_adapter import PaperLedgerAdapter
from engine.mas.lifecycle import (
    OHLCQuote,
    PositionMarketMark,
    calculate_portfolio_mark,
    resolve_bracket_exit,
)


NOW = "2026-11-18T12:00:00Z"


def _ledger():
    ledger = RiskLedger("10000")
    ledger.apply({
        "event_id": "open-long", "sequence": 1, "timestamp": NOW,
        "type": "PositionOpened", "position_id": "p-long",
        "symbol": "EURUSD", "side": "BUY", "quantity": "1",
        "entry": "1.1000", "stop": "1.0900",
        "approved_risk": "100", "actual_risk": "90",
    })
    ledger.apply({
        "event_id": "open-short", "sequence": 2, "timestamp": NOW,
        "type": "PositionOpened", "position_id": "p-short",
        "symbol": "GBPUSD", "side": "SELL", "quantity": "2",
        "entry": "1.2500", "stop": "1.2600",
        "approved_risk": "100", "actual_risk": "80",
    })
    return ledger


def _model(*, conversion="1", commission="2", slippage="0"):
    return ExecutionCostModel(
        contract_size="100000", quote_to_account_rate=conversion,
        slippage_price_per_fill=slippage,
        commission_per_lot_per_side=commission,
    )


def _marks(order=("p-long", "p-short")):
    result = {
        "p-long": PositionMarketMark(
            quote=ExecutableQuote("1.1010", "1.1012"),
            quote_timestamp=NOW, model=_model(),
        ),
        "p-short": PositionMarketMark(
            quote=ExecutableQuote("1.2490", "1.2492"),
            quote_timestamp=NOW, model=_model(conversion="0.9"),
            accrued_swap_account="1.5",
        ),
    }
    return {key: result[key] for key in order}


def test_portfolio_mark_uses_bid_for_long_and_ask_for_short():
    mark = calculate_portfolio_mark(_ledger(), _marks(), timestamp=NOW)
    # Long: (1.1010 - 1.1000) * 100000 - 2 = 98.
    # Short: (1.2500 - 1.2492) * 200000 * .9 - 4 - 1.5 = 138.5.
    assert mark.position_pnl_account == {
        "p-long": Decimal("98.0000"),
        "p-short": Decimal("138.500000"),
    }
    assert mark.unrealized_pnl_account == Decimal("236.500000")


def test_portfolio_mark_is_invariant_to_input_mapping_order():
    first = calculate_portfolio_mark(_ledger(), _marks(), timestamp=NOW)
    second = calculate_portfolio_mark(
        _ledger(), _marks(("p-short", "p-long")), timestamp=NOW
    )
    assert first == second


def test_portfolio_mark_requires_exact_open_position_coverage():
    ledger = _ledger()
    with pytest.raises(ValueError, match="missing"):
        calculate_portfolio_mark(
            ledger, {"p-long": _marks()["p-long"]}, timestamp=NOW
        )
    with pytest.raises(ValueError, match="extra"):
        calculate_portfolio_mark(
            ledger, {**_marks(), "orphan": _marks()["p-long"]}, timestamp=NOW
        )


def test_portfolio_mark_rejects_stale_or_mixed_quote_timestamps():
    marks = _marks()
    marks["p-short"] = PositionMarketMark(
        quote=marks["p-short"].quote,
        quote_timestamp="2026-11-18T11:59:59Z",
        model=marks["p-short"].model,
    )
    with pytest.raises(ValueError, match="portfolio mark timestamp"):
        calculate_portfolio_mark(_ledger(), marks, timestamp=NOW)


def _bar(*, bid_open, bid_high, bid_low, bid_close,
         ask_open, ask_high, ask_low, ask_close):
    return OHLCQuote(
        timestamp=NOW,
        bid_open=bid_open, bid_high=bid_high, bid_low=bid_low, bid_close=bid_close,
        ask_open=ask_open, ask_high=ask_high, ask_low=ask_low, ask_close=ask_close,
    )


def test_long_stop_and_target_both_hit_stop_wins_conservatively():
    bar = _bar(
        bid_open="1.1000", bid_high="1.1120", bid_low="1.0880", bid_close="1.1010",
        ask_open="1.1002", ask_high="1.1122", ask_low="1.0882", ask_close="1.1012",
    )
    result = resolve_bracket_exit(
        side="BUY", stop_price="1.0900", target_price="1.1100",
        bar=bar, slippage_price="0.0001",
    )
    assert result.triggered and result.reason == "STOP"
    assert result.ambiguous_intrabar is True
    assert result.executable_price == Decimal("1.0899")


def test_short_stop_and_target_both_hit_stop_wins_conservatively():
    bar = _bar(
        bid_open="1.2500", bid_high="1.2620", bid_low="1.2380", bid_close="1.2490",
        ask_open="1.2502", ask_high="1.2622", ask_low="1.2382", ask_close="1.2492",
    )
    result = resolve_bracket_exit(
        side="SELL", stop_price="1.2600", target_price="1.2400",
        bar=bar, slippage_price="0.0001",
    )
    assert result.triggered and result.reason == "STOP"
    assert result.ambiguous_intrabar is True
    assert result.executable_price == Decimal("1.2601")


def test_long_gap_through_stop_uses_opening_bid():
    bar = _bar(
        bid_open="1.0800", bid_high="1.0850", bid_low="1.0750", bid_close="1.0820",
        ask_open="1.0802", ask_high="1.0852", ask_low="1.0752", ask_close="1.0822",
    )
    result = resolve_bracket_exit(
        side="BUY", stop_price="1.0900", target_price="1.1100",
        bar=bar, slippage_price="0.0002",
    )
    assert result.reason == "STOP"
    assert result.executable_price == Decimal("1.0798")


def test_short_gap_through_target_gets_opening_ask():
    bar = _bar(
        bid_open="1.2300", bid_high="1.2350", bid_low="1.2250", bid_close="1.2320",
        ask_open="1.2302", ask_high="1.2352", ask_low="1.2252", ask_close="1.2322",
    )
    result = resolve_bracket_exit(
        side="SELL", stop_price="1.2600", target_price="1.2400",
        bar=bar, slippage_price="0.0001",
    )
    assert result.reason == "TARGET"
    assert result.executable_price == Decimal("1.2303")


def test_no_bracket_touch_returns_no_exit():
    bar = _bar(
        bid_open="1.1000", bid_high="1.1050", bid_low="1.0950", bid_close="1.1010",
        ask_open="1.1002", ask_high="1.1052", ask_low="1.0952", ask_close="1.1012",
    )
    result = resolve_bracket_exit(
        side="BUY", stop_price="1.0900", target_price="1.1100", bar=bar
    )
    assert result.triggered is False
    assert result.reason is None
    assert result.executable_price is None


def test_invalid_ohlc_and_inverted_bracket_are_rejected():
    with pytest.raises(ValueError, match="inconsistent"):
        _bar(
            bid_open="1.10", bid_high="1.09", bid_low="1.08", bid_close="1.10",
            ask_open="1.1002", ask_high="1.1003", ask_low="1.0802", ask_close="1.1002",
        )
    bar = _bar(
        bid_open="1.1", bid_high="1.11", bid_low="1.09", bid_close="1.1",
        ask_open="1.1002", ask_high="1.1102", ask_low="1.0902", ask_close="1.1002",
    )
    with pytest.raises(ValueError, match="BUY stop"):
        resolve_bracket_exit(side="BUY", stop_price="1.11", target_price="1.10", bar=bar)


def test_rejects_naive_timestamp_and_binary_float_quote():
    with pytest.raises(ValueError, match="timezone-aware"):
        OHLCQuote(
            timestamp="2026-11-18T12:00:00",
            bid_open="1", bid_high="1", bid_low="1", bid_close="1",
            ask_open="1", ask_high="1", ask_low="1", ask_close="1",
        )
    with pytest.raises(ValueError, match="exact Decimal"):
        ExecutableQuote(1.0, "1.1")



def test_adapter_persists_coherent_portfolio_mark(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "portfolio-mark.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:portfolio",
        initial_balance="10000", timestamp=NOW,
    )
    adapter.commit_open_position(
        symbol="EURUSD", side="BUY", quantity="1",
        entry="1.1000", stop="1.0900", approved_risk="100",
        actual_risk="90", decision_timestamp=NOW,
        candidate_identity="portfolio-mark",
    )
    result = adapter.mark_portfolio(
        marks={
            next(iter(adapter.ledger.snapshot()["open_positions"])): PositionMarketMark(
                quote=ExecutableQuote("1.1010", "1.1012"),
                quote_timestamp=NOW, model=_model(),
            )
        },
        timestamp=NOW, mark_id="portfolio-mark-1",
    )
    assert result.unrealized_pnl_account == Decimal("98.0000")
    assert adapter.ledger.equity == Decimal("10098.0000")
    assert store.load("paper:portfolio").snapshot() == adapter.ledger.snapshot()


def test_partial_close_recomputes_remaining_portfolio_mtm_atomically(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "partial-close.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:partial",
        initial_balance="10000", timestamp=NOW,
    )
    position_id = adapter.commit_open_position(
        symbol="EURUSD", side="BUY", quantity="1",
        entry="1.1000", stop="1.0900", approved_risk="100",
        actual_risk="90", decision_timestamp=NOW,
        candidate_identity="partial-close",
    )
    execution = _model().round_trip(
        side="BUY", lots="0.4",
        entry_quote=ExecutableQuote("1.1000", "1.1002"),
        exit_quote=ExecutableQuote("1.1020", "1.1022"),
    )
    events = adapter.build_execution_portfolio_settlement_events(
        position_id=position_id, execution_identity="partial-fill-1",
        execution_result=execution, timestamp="2026-11-18T12:30:00Z",
        mark_id="after-partial-1",
        remaining_position_marks={
            position_id: PositionMarketMark(
                quote=ExecutableQuote("1.1010", "1.1012"),
                quote_timestamp="2026-11-18T12:30:00Z",
                model=_model(),
            )
        },
    )
    assert [event["type"] for event in events] == [
        "PositionPartiallyClosed", "MarkToMarketUpdated",
    ]
    assert adapter.commit_events(events) is True
    snapshot = adapter.ledger.snapshot()
    assert snapshot["open_positions"][position_id]["quantity"] == "0.6"
    assert adapter.ledger.unrealized_pnl == Decimal("58.8000")
    assert store.load("paper:partial").snapshot() == snapshot


def test_partial_close_rejects_marks_that_omit_another_open_position(tmp_path):
    store = SQLiteLedgerStore(tmp_path / "partial-missing-mark.sqlite")
    adapter = PaperLedgerAdapter.create(
        store=store, ledger_id="paper:partial-missing",
        initial_balance="10000", timestamp=NOW,
    )
    first = adapter.commit_open_position(
        symbol="EURUSD", side="BUY", quantity="1",
        entry="1.1000", stop="1.0900", approved_risk="100",
        actual_risk="90", decision_timestamp=NOW,
        candidate_identity="partial-first",
    )
    adapter.commit_open_position(
        symbol="GBPUSD", side="SELL", quantity="1",
        entry="1.2500", stop="1.2600", approved_risk="100",
        actual_risk="90", decision_timestamp=NOW,
        candidate_identity="partial-second",
    )
    execution = _model().round_trip(
        side="BUY", lots="0.5",
        entry_quote=ExecutableQuote("1.1000", "1.1002"),
        exit_quote=ExecutableQuote("1.1010", "1.1012"),
    )
    with pytest.raises(ValueError, match="missing"):
        adapter.build_execution_portfolio_settlement_events(
            position_id=first, execution_identity="partial-with-missing-mark",
            execution_result=execution, timestamp="2026-11-18T12:30:00Z",
            mark_id="missing-mark",
            remaining_position_marks={
                first: PositionMarketMark(
                    quote=ExecutableQuote("1.1010", "1.1012"),
                    quote_timestamp="2026-11-18T12:30:00Z",
                    model=_model(),
                )
            },
        )
    assert adapter.ledger.sequence == 3
    assert store.load("paper:partial-missing").snapshot() == adapter.ledger.snapshot()
