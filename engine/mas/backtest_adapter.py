"""In-memory Backtest adapter for the canonical MAS portfolio ledger.

This adapter deliberately accepts exact execution/accounting facts. It does
not translate legacy float BTTrade rows, infer bid/ask quotes, or estimate
missing costs. Historical pricing must be produced by the shared execution
and lifecycle models before events reach this boundary.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .accounting import (
    account_state_from_ledger,
    apply_ledger_events,
    mark_to_market_event,
    position_opened_event,
    position_settlement_events,
)
from .identity import stable_execution_id, stable_position_id
from .ledger import RiskLedger
from .periods import build_period_roll_event
from .portfolio_loop import order_portfolio_events


def _exact_text(value: Any, field: str) -> str:
    if isinstance(value, bool) or isinstance(value, (float, type(None))):
        raise ValueError(f"{field} must be an exact Decimal, integer, or decimal string")
    if not isinstance(value, (Decimal, int, str)):
        raise ValueError(f"{field} must be an exact Decimal, integer, or decimal string")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return format(parsed, "f")


class BacktestLedgerAdapter:
    """One ephemeral, multi-symbol account using the same RiskLedger as Paper.

    The caller supplies chronological, already-priced execution facts. Events
    are canonically ordered and validated before the in-memory ledger changes.
    No SQLite, wall clock, market-data, strategy, or broker dependency exists.
    """

    def __init__(
        self, *, ledger: RiskLedger, account_namespace: str = "backtest",
        strategy_namespace: str = "mas-v1",
    ) -> None:
        if not isinstance(ledger, RiskLedger):
            raise ValueError("ledger must be a RiskLedger")
        if not isinstance(account_namespace, str) or not account_namespace.strip():
            raise ValueError("account_namespace is required")
        if not isinstance(strategy_namespace, str) or not strategy_namespace.strip():
            raise ValueError("strategy_namespace is required")
        self.ledger = ledger
        self.account_namespace = account_namespace.strip()
        self.strategy_namespace = strategy_namespace.strip()

    @classmethod
    def create(
        cls, *, initial_balance: Any, timestamp: str,
        base_currency: str = "USD", account_namespace: str = "backtest",
        strategy_namespace: str = "mas-v1",
    ) -> "BacktestLedgerAdapter":
        from .periods import trading_periods

        balance = _exact_text(initial_balance, "initial_balance")
        periods = trading_periods(timestamp)
        ledger = RiskLedger(
            balance, base_currency=base_currency,
            daily_period_id=periods.daily_period_id,
            weekly_period_id=periods.weekly_period_id,
        )
        return cls(
            ledger=ledger, account_namespace=account_namespace,
            strategy_namespace=strategy_namespace,
        )

    def build_open_event(
        self, *, symbol: str, side: str, quantity: Any, entry: Any,
        stop: Any, approved_risk: Any, actual_risk: Any,
        decision_timestamp: str, candidate_identity: str,
    ) -> tuple[str, dict]:
        position_id = stable_position_id(
            account_namespace=self.account_namespace,
            strategy_namespace=self.strategy_namespace,
            symbol=symbol, side=side,
            decision_timestamp=decision_timestamp,
            candidate_identity=candidate_identity,
        )
        event = position_opened_event(
            self.ledger, position_id=position_id, symbol=symbol.upper(),
            side=side.upper(), quantity=_exact_text(quantity, "quantity"),
            entry=_exact_text(entry, "entry"), stop=_exact_text(stop, "stop"),
            approved_risk=_exact_text(approved_risk, "approved_risk"),
            actual_risk=_exact_text(actual_risk, "actual_risk"),
            timestamp=decision_timestamp,
        )
        return position_id, event

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
            quantity=_exact_text(quantity, "quantity"),
            gross_pnl=_exact_text(gross_pnl, "gross_pnl"),
            timestamp=timestamp,
            remaining_unrealized_pnl=_exact_text(
                remaining_unrealized_pnl, "remaining_unrealized_pnl"
            ),
            mark_id=mark_id,
            commission=_exact_text(commission, "commission"),
            swap=_exact_text(swap, "swap"),
            other_fees=_exact_text(other_fees, "other_fees"),
        )

    def commit_events(self, events: list[dict]) -> bool:
        ordered = order_portfolio_events(self.ledger, events)
        return apply_ledger_events(self.ledger, ordered)

    def commit_open_position(self, **facts: Any) -> str:
        position_id, event = self.build_open_event(**facts)
        self.commit_events([event])
        return position_id

    def update_mark(self, *, mark_id: str, unrealized_pnl: Any, timestamp: str) -> bool:
        event = mark_to_market_event(
            self.ledger, mark_id=mark_id,
            unrealized_pnl=_exact_text(unrealized_pnl, "unrealized_pnl"),
            timestamp=timestamp,
        )
        return self.commit_events([event])

    def roll_period(self, timestamp: str) -> bool:
        event = build_period_roll_event(self.ledger, timestamp)
        if event is None:
            return False
        return self.commit_events([event])

    def account_state(self, *, imminent_news_minutes: float = -1.0):
        return account_state_from_ledger(
            self.ledger, imminent_news_minutes=imminent_news_minutes,
        )
