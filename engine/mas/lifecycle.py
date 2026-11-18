"""Deterministic portfolio marking and conservative OHLC bracket resolution.

This module is pure: it consumes explicit market quotes and instrument
specifications, performs no data retrieval, and never mutates the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from .execution import ExecutableQuote, ExecutionCostModel, decimal_value
from .ledger import RiskLedger


ZERO = Decimal("0")


def _aware_utc(value: str | datetime, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{field} must be ISO-8601") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class PositionMarketMark:
    quote: ExecutableQuote
    quote_timestamp: str | datetime
    model: ExecutionCostModel
    accrued_swap_account: Decimal = ZERO
    accrued_other_fees_account: Decimal = ZERO


@dataclass(frozen=True)
class PortfolioMark:
    timestamp: datetime
    unrealized_pnl_account: Decimal
    position_pnl_account: Mapping[str, Decimal]


def calculate_portfolio_mark(
    ledger: RiskLedger,
    marks: Mapping[str, PositionMarketMark],
    *,
    timestamp: str | datetime,
) -> PortfolioMark:
    """Calculate one coherent liquidation-side MTM for every open position.

    Longs liquidate at bid; shorts liquidate at ask. Exit-side commission and
    accrued financing/fees are included so equity is marked net of liquidation
    costs. All quotes must represent the exact same UTC instant.
    """
    if not isinstance(ledger, RiskLedger):
        raise ValueError("ledger must be a RiskLedger")
    if not isinstance(marks, Mapping):
        raise ValueError("marks must be a position-id mapping")
    mark_time = _aware_utc(timestamp, "timestamp")
    positions = ledger.snapshot()["open_positions"]
    if set(marks) != set(positions):
        missing = sorted(set(positions) - set(marks))
        extra = sorted(set(marks) - set(positions))
        raise ValueError(f"mark set must exactly match open positions; missing={missing}, extra={extra}")

    pnl_by_position: dict[str, Decimal] = {}
    for position_id in sorted(positions):
        market = marks[position_id]
        if not isinstance(market, PositionMarketMark):
            raise ValueError(f"invalid mark for position {position_id}")
        if _aware_utc(market.quote_timestamp, "quote_timestamp") != mark_time:
            raise ValueError("all position quotes must match the portfolio mark timestamp")
        position = positions[position_id]
        side = position["side"]
        quantity = Decimal(position["quantity"])
        entry = Decimal(position["entry"])
        exit_price = market.model.exit_fill(side=side, quote=market.quote)
        direction = Decimal("1") if side == "BUY" else Decimal("-1")
        gross_quote = (exit_price - entry) * direction * market.model.contract_size * quantity
        gross_account = gross_quote * market.model.quote_to_account_rate
        exit_commission = market.model.commission_per_lot_per_side * quantity
        accrued_swap = decimal_value(market.accrued_swap_account, "accrued_swap_account")
        other_fees = decimal_value(
            market.accrued_other_fees_account, "accrued_other_fees_account",
            non_negative=True,
        )
        pnl_by_position[position_id] = (
            gross_account - exit_commission - accrued_swap - other_fees
        )

    total = sum(pnl_by_position.values(), ZERO)
    return PortfolioMark(
        timestamp=mark_time,
        unrealized_pnl_account=total,
        position_pnl_account=pnl_by_position,
    )


@dataclass(frozen=True)
class OHLCQuote:
    """Executable bid/ask OHLC quote; each side must be a valid OHLC candle."""
    timestamp: str | datetime
    bid_open: Decimal
    bid_high: Decimal
    bid_low: Decimal
    bid_close: Decimal
    ask_open: Decimal
    ask_high: Decimal
    ask_low: Decimal
    ask_close: Decimal

    def __post_init__(self) -> None:
        _aware_utc(self.timestamp, "bar timestamp")
        for field in (
            "bid_open", "bid_high", "bid_low", "bid_close",
            "ask_open", "ask_high", "ask_low", "ask_close",
        ):
            object.__setattr__(
                self, field, decimal_value(getattr(self, field), field, positive=True)
            )
        for prefix in ("bid", "ask"):
            op = getattr(self, f"{prefix}_open")
            hi = getattr(self, f"{prefix}_high")
            lo = getattr(self, f"{prefix}_low")
            close = getattr(self, f"{prefix}_close")
            if hi < max(op, close, lo) or lo > min(op, close, hi):
                raise ValueError(f"{prefix} OHLC values are inconsistent")
        for suffix in ("open", "high", "low", "close"):
            if getattr(self, f"ask_{suffix}") < getattr(self, f"bid_{suffix}"):
                raise ValueError("ask quote cannot be below bid quote")


@dataclass(frozen=True)
class BracketExit:
    triggered: bool
    reason: str | None
    executable_price: Decimal | None
    ambiguous_intrabar: bool
    timestamp: datetime


def resolve_bracket_exit(
    *,
    side: str,
    stop_price: Decimal,
    target_price: Decimal,
    bar: OHLCQuote,
    slippage_price: Decimal = ZERO,
) -> BracketExit:
    """Resolve a bar containing stop/target touches with deterministic STOP_FIRST.

    Trigger checks use liquidation-side prices: bid for long positions and ask
    for shorts. If both levels are touched within one OHLC bar, stop wins.
    Gaps execute at the worse opening quote; otherwise at the triggered level.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not isinstance(bar, OHLCQuote):
        raise ValueError("bar must be an OHLCQuote")
    stop = decimal_value(stop_price, "stop_price", positive=True)
    target = decimal_value(target_price, "target_price", positive=True)
    slippage = decimal_value(slippage_price, "slippage_price", non_negative=True)
    if side == "BUY":
        if stop >= target:
            raise ValueError("BUY stop must be below target")
        op, hi, lo = bar.bid_open, bar.bid_high, bar.bid_low
        stop_hit, target_hit = lo <= stop, hi >= target
        if stop_hit:
            raw = min(stop, op) if op <= stop else stop
            price = decimal_value(raw - slippage, "stop fill", positive=True)
            return BracketExit(True, "STOP", price, target_hit, _aware_utc(bar.timestamp, "bar timestamp"))
        if target_hit:
            raw = max(target, op) if op >= target else target
            price = decimal_value(raw - slippage, "target fill", positive=True)
            return BracketExit(True, "TARGET", price, False, _aware_utc(bar.timestamp, "bar timestamp"))
    else:
        if stop <= target:
            raise ValueError("SELL stop must be above target")
        op, hi, lo = bar.ask_open, bar.ask_high, bar.ask_low
        stop_hit, target_hit = hi >= stop, lo <= target
        if stop_hit:
            raw = max(stop, op) if op >= stop else stop
            price = decimal_value(raw + slippage, "stop fill", positive=True)
            return BracketExit(True, "STOP", price, target_hit, _aware_utc(bar.timestamp, "bar timestamp"))
        if target_hit:
            raw = min(target, op) if op <= target else target
            price = decimal_value(raw + slippage, "target fill", positive=True)
            return BracketExit(True, "TARGET", price, False, _aware_utc(bar.timestamp, "bar timestamp"))
    return BracketExit(False, None, None, False, _aware_utc(bar.timestamp, "bar timestamp"))
