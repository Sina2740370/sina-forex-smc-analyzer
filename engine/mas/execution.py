"""Deterministic Decimal FX execution pricing and cost model.

Bid/ask spread is represented by executable quotes. Slippage is applied
adversely to each fill. Quote-currency PnL is converted with an explicit,
caller-supplied quote-to-account FX rate; no market data is fetched here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


ZERO = Decimal("0")


def decimal_value(value: Any, field: str, *, positive: bool = False,
                  non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise ValueError(f"{field} must be an exact Decimal, integer, or string")
    if not isinstance(value, (Decimal, int, str)):
        raise ValueError(f"{field} must be an exact Decimal, integer, or string")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and result <= ZERO:
        raise ValueError(f"{field} must be positive")
    if non_negative and result < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return result


@dataclass(frozen=True)
class ExecutableQuote:
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        bid = decimal_value(self.bid, "bid", positive=True)
        ask = decimal_value(self.ask, "ask", positive=True)
        if ask < bid:
            raise ValueError("ask cannot be below bid")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)


@dataclass(frozen=True)
class ExecutionCostModel:
    contract_size: Decimal
    quote_to_account_rate: Decimal
    slippage_price_per_fill: Decimal = ZERO
    commission_per_lot_per_side: Decimal = ZERO
    pip_size: Decimal = Decimal("0.0001")

    def __post_init__(self) -> None:
        for field in ("contract_size", "quote_to_account_rate", "pip_size"):
            object.__setattr__(
                self, field, decimal_value(getattr(self, field), field, positive=True)
            )
        for field in ("slippage_price_per_fill", "commission_per_lot_per_side"):
            object.__setattr__(
                self, field,
                decimal_value(getattr(self, field), field, non_negative=True),
            )

    def entry_fill(self, *, side: str, quote: ExecutableQuote) -> Decimal:
        side = _side(side)
        if side == "BUY":
            return quote.ask + self.slippage_price_per_fill
        return quote.bid - self.slippage_price_per_fill

    def exit_fill(self, *, side: str, quote: ExecutableQuote) -> Decimal:
        side = _side(side)
        if side == "BUY":
            return quote.bid - self.slippage_price_per_fill
        return quote.ask + self.slippage_price_per_fill

    def stop_exit_fill(self, *, side: str, stop_price: Any,
                       quote: ExecutableQuote) -> Decimal:
        """Conservative stop fill: a gap through the stop executes at the worse quote."""
        side = _side(side)
        stop = decimal_value(stop_price, "stop_price", positive=True)
        if side == "BUY":
            raw_fill = min(stop, quote.bid)
            return raw_fill - self.slippage_price_per_fill
        raw_fill = max(stop, quote.ask)
        return raw_fill + self.slippage_price_per_fill

    def round_trip(
        self, *, side: str, lots: Any, entry_quote: ExecutableQuote,
        exit_quote: ExecutableQuote, swap_account: Any = ZERO,
        other_fees_account: Any = ZERO,
    ) -> "ExecutionResult":
        side = _side(side)
        size = decimal_value(lots, "lots", positive=True)
        swap = decimal_value(swap_account, "swap_account")
        other = decimal_value(other_fees_account, "other_fees_account", non_negative=True)
        entry = self.entry_fill(side=side, quote=entry_quote)
        exit_price = self.exit_fill(side=side, quote=exit_quote)
        direction = Decimal("1") if side == "BUY" else Decimal("-1")
        pnl_quote = (exit_price - entry) * direction * self.contract_size * size
        gross = pnl_quote * self.quote_to_account_rate
        commission = self.commission_per_lot_per_side * size * Decimal("2")
        net = gross - commission + swap - other
        return ExecutionResult(
            side=side, lots=size, entry_fill=entry, exit_fill=exit_price,
            gross_pnl_quote=pnl_quote, gross_pnl_account=gross,
            commission_account=commission, swap_account=swap,
            other_fees_account=other, net_pnl_account=net,
            net_pips=(exit_price - entry) * direction / self.pip_size,
        )


@dataclass(frozen=True)
class ExecutionResult:
    side: str
    lots: Decimal
    entry_fill: Decimal
    exit_fill: Decimal
    gross_pnl_quote: Decimal
    gross_pnl_account: Decimal
    commission_account: Decimal
    swap_account: Decimal
    other_fees_account: Decimal
    net_pnl_account: Decimal
    net_pips: Decimal


def _side(value: str) -> str:
    if value not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    return value
