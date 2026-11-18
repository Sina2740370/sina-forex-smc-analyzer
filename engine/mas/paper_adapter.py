"""Explicit Paper adapter over the canonical SQLite-backed MAS ledger.

This layer accepts already-computed execution facts. It does not calculate
prices, PnL, spread, slippage, fees, or signals; those must be supplied by a
separate execution model using exact Decimal-compatible inputs.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .accounting import (
    account_state_from_ledger,
    apply_ledger_event,
    apply_ledger_events,
    mark_to_market_event,
    position_opened_event,
    position_settlement_events,
    roll_account_period,
)
from .identity import stable_execution_id, stable_position_id
from .ledger import RiskLedger
from .ledger_store import SQLiteLedgerStore
from .periods import trading_periods


def _exact_decimal_text(value: Any, field: str) -> str:
    """Reject binary floats at the adapter boundary; preserve exact decimal input."""
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ValueError(f"{field} must be supplied as Decimal, integer, or decimal string")
    if not isinstance(value, (Decimal, int, str)):
        raise ValueError(f"{field} must be supplied as Decimal, integer, or decimal string")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return format(parsed, "f")


class PaperLedgerAdapter:
    """A single Paper account bound to one durable event-sourced ledger.

    Creation is explicit. Existing accounts must be loaded explicitly; this
    class never imports or mutates legacy JSON state.
    """

    def __init__(
        self, *, store: SQLiteLedgerStore, ledger_id: str,
        ledger: RiskLedger, strategy_namespace: str = "mas-v1",
    ) -> None:
        if not isinstance(store, SQLiteLedgerStore):
            raise ValueError("store must be a SQLiteLedgerStore")
        if not isinstance(ledger, RiskLedger):
            raise ValueError("ledger must be a RiskLedger")
        if not isinstance(ledger_id, str) or not ledger_id.strip():
            raise ValueError("ledger_id is required")
        if not isinstance(strategy_namespace, str) or not strategy_namespace.strip():
            raise ValueError("strategy_namespace is required")
        self.store = store
        self.ledger_id = ledger_id.strip()
        self.ledger = ledger
        self.strategy_namespace = strategy_namespace.strip()

    @classmethod
    def create(
        cls, *, store: SQLiteLedgerStore, ledger_id: str,
        initial_balance: Any, timestamp: str,
        base_currency: str = "USD", strategy_namespace: str = "mas-v1",
    ) -> "PaperLedgerAdapter":
        balance = _exact_decimal_text(initial_balance, "initial_balance")
        periods = trading_periods(timestamp)
        ledger = RiskLedger(
            balance, base_currency=base_currency,
            daily_period_id=periods.daily_period_id,
            weekly_period_id=periods.weekly_period_id,
        )
        store.create(ledger_id, ledger)
        return cls(
            store=store, ledger_id=ledger_id, ledger=ledger,
            strategy_namespace=strategy_namespace,
        )

    @classmethod
    def load(
        cls, *, store: SQLiteLedgerStore, ledger_id: str,
        strategy_namespace: str = "mas-v1",
    ) -> "PaperLedgerAdapter":
        ledger = store.load(ledger_id)
        return cls(
            store=store, ledger_id=ledger_id, ledger=ledger,
            strategy_namespace=strategy_namespace,
        )

    def build_open_event(
        self, *, symbol: str, side: str, quantity: Any, entry: Any,
        stop: Any, approved_risk: Any, actual_risk: Any,
        decision_timestamp: str, candidate_identity: str,
    ) -> tuple[str, dict]:
        position_id = stable_position_id(
            account_namespace=self.ledger_id,
            strategy_namespace=self.strategy_namespace,
            symbol=symbol, side=side,
            decision_timestamp=decision_timestamp,
            candidate_identity=candidate_identity,
        )
        event = position_opened_event(
            self.ledger, position_id=position_id, symbol=symbol.upper(),
            side=side.upper(),
            quantity=_exact_decimal_text(quantity, "quantity"),
            entry=_exact_decimal_text(entry, "entry"),
            stop=_exact_decimal_text(stop, "stop"),
            approved_risk=_exact_decimal_text(approved_risk, "approved_risk"),
            actual_risk=_exact_decimal_text(actual_risk, "actual_risk"),
            timestamp=decision_timestamp,
        )
        return position_id, event

    def _reuse_exact_event(self, event: dict) -> dict:
        if not isinstance(event, dict):
            raise ValueError("event must be a mapping")
        event_id = event.get("event_id")
        for prior in self.ledger.snapshot()["applied_events"]:
            if prior.get("event_id") == event_id:
                comparable = dict(event)
                comparable["sequence"] = prior["sequence"]
                if comparable != prior:
                    raise ValueError("event_id was reused with different execution facts")
                return prior
        return event

    def commit_event(self, event: dict) -> bool:
        stable_event = self._reuse_exact_event(event)
        return apply_ledger_event(
            self.ledger, stable_event, store=self.store, ledger_id=self.ledger_id,
        )

    def commit_open_position(self, **facts: Any) -> str:
        position_id, event = self.build_open_event(**facts)
        self.commit_event(event)
        return position_id

    def build_settlement_events(
        self, *, position_id: str, execution_identity: str,
        quantity: Any, gross_pnl: Any, timestamp: str,
        remaining_unrealized_pnl: Any, mark_id: str,
        commission: Any = 0, swap: Any = 0, other_fees: Any = 0,
    ) -> list[dict]:
        execution_id = stable_execution_id(
            position_id=position_id, execution_identity=execution_identity,
        )
        return position_settlement_events(
            self.ledger, position_id=position_id, execution_id=execution_id,
            quantity=_exact_decimal_text(quantity, "quantity"),
            gross_pnl=_exact_decimal_text(gross_pnl, "gross_pnl"),
            timestamp=timestamp,
            remaining_unrealized_pnl=_exact_decimal_text(
                remaining_unrealized_pnl, "remaining_unrealized_pnl"
            ),
            mark_id=mark_id,
            commission=_exact_decimal_text(commission, "commission"),
            swap=_exact_decimal_text(swap, "swap"),
            other_fees=_exact_decimal_text(other_fees, "other_fees"),
        )

    def commit_events(self, events: list[dict]) -> bool:
        stable_events = [self._reuse_exact_event(event) for event in events]
        return apply_ledger_events(
            self.ledger, stable_events, store=self.store, ledger_id=self.ledger_id,
        )

    def update_mark(self, *, mark_id: str, unrealized_pnl: Any, timestamp: str) -> bool:
        event = mark_to_market_event(
            self.ledger, mark_id=mark_id,
            unrealized_pnl=_exact_decimal_text(unrealized_pnl, "unrealized_pnl"),
            timestamp=timestamp,
        )
        return self.commit_event(event)

    def roll_period(self, timestamp: str) -> bool:
        return roll_account_period(
            self.ledger, timestamp, store=self.store, ledger_id=self.ledger_id,
        )

    def account_state(self, *, imminent_news_minutes: float = -1.0):
        """Read-only projection for the existing signal risk gate."""
        return account_state_from_ledger(
            self.ledger, imminent_news_minutes=imminent_news_minutes,
        )

    def reload(self) -> None:
        """Replace in-memory state only after a complete verified SQLite replay."""
        restored = self.store.load(self.ledger_id)
        self.ledger.__dict__.update(restored.__dict__)
