"""Pure event-sourced account ledger for MAS.

No clock, filesystem, database, broker or network dependencies. Callers provide
ordered timestamped events and persist/recover snapshots in an adapter.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")


def _decimal(value: Any, field: str, *, positive: bool = False,
             non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
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


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("event timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("event timestamp must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


class RiskLedger:
    """Deterministic account ledger with atomic, idempotent event application.

    All money, fees and risk amounts use base_currency. Period boundaries are
    explicit PeriodRolled events; the core never infers local-time rollovers.
    """

    SCHEMA_VERSION = 3

    def __init__(self, initial_balance: Any, base_currency: str = "USD",
                 daily_period_id: str = "initial",
                 weekly_period_id: str = "initial") -> None:
        balance = _decimal(initial_balance, "initial_balance", positive=True)
        if not isinstance(base_currency, str) or not base_currency.strip():
            raise ValueError("base_currency is required")
        self.base_currency = base_currency.upper()
        self.initial_balance = balance
        self.balance = balance
        self.daily_start_balance = balance
        self.weekly_start_balance = balance
        self.initial_daily_period_id = str(daily_period_id)
        self.initial_weekly_period_id = str(weekly_period_id)
        self.daily_period_id = self.initial_daily_period_id
        self.weekly_period_id = self.initial_weekly_period_id
        self.daily_realized_net_pnl = ZERO
        self.weekly_realized_net_pnl = ZERO
        self.realized_pnl_lifetime = ZERO
        self.fees_lifetime = ZERO
        self.unrealized_pnl = ZERO
        self.peak_equity = balance
        self.max_drawdown_amount = ZERO
        self.max_drawdown_pct = ZERO
        self.consecutive_losses = 0
        self.sequence = 0
        self.last_event_at: datetime | None = None
        self._positions: dict[str, dict] = {}
        self._applied_event_ids: dict[str, dict] = {}

    def apply(self, event: dict) -> bool:
        """Apply atomically; False means this event ID was already applied."""
        if not isinstance(event, dict):
            raise ValueError("event must be a mapping")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event_id is required")
        if event_id in self._applied_event_ids:
            if self._applied_event_ids[event_id] != event:
                raise ValueError("event_id was reused with a different payload")
            return False
        sequence = event.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("sequence must be an integer")
        if sequence != self.sequence + 1:
            raise ValueError("event sequence must be contiguous")
        occurred_at = _timestamp(event.get("timestamp"))
        if self.last_event_at and occurred_at < self.last_event_at:
            raise ValueError("event timestamp cannot move backwards")

        candidate = deepcopy(self)
        candidate._apply_event(event, occurred_at)
        candidate.sequence = sequence
        candidate.last_event_at = occurred_at
        candidate._applied_event_ids[event_id] = deepcopy(event)
        candidate._update_drawdown()
        self.__dict__.update(candidate.__dict__)
        return True

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "RiskLedger":
        """Restore by replaying and verifying the complete event history.

        A snapshot is accepted only when its event stream reconstructs every
        persisted accounting field exactly. This detects tampering, gaps,
        reordered events and inconsistent derived state.
        """
        if not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("unsupported or invalid ledger snapshot")

        sequence = snapshot.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("snapshot sequence must be a non-negative integer")

        applied = snapshot.get("applied_events")
        if not isinstance(applied, list):
            raise ValueError("snapshot applied_events is required for idempotent recovery")
        if len(applied) != sequence:
            raise ValueError("snapshot event history does not match sequence")

        base_currency = snapshot.get("base_currency")
        if not isinstance(base_currency, str) or not base_currency.strip():
            raise ValueError("snapshot base_currency is required")
        initial_balance = _decimal(snapshot.get("initial_balance"),
                                    "initial_balance", positive=True)

        initial_daily_period_id = snapshot.get("initial_daily_period_id")
        initial_weekly_period_id = snapshot.get("initial_weekly_period_id")
        daily_period_id = snapshot.get("daily_period_id")
        weekly_period_id = snapshot.get("weekly_period_id")
        if not isinstance(initial_daily_period_id, str) or not initial_daily_period_id:
            raise ValueError("snapshot initial_daily_period_id is required")
        if not isinstance(initial_weekly_period_id, str) or not initial_weekly_period_id:
            raise ValueError("snapshot initial_weekly_period_id is required")
        if not isinstance(daily_period_id, str) or not daily_period_id:
            raise ValueError("snapshot daily_period_id is required")
        if not isinstance(weekly_period_id, str) or not weekly_period_id:
            raise ValueError("snapshot weekly_period_id is required")

        ledger = cls(
            initial_balance,
            base_currency=base_currency,
            daily_period_id=initial_daily_period_id,
            weekly_period_id=initial_weekly_period_id,
        )
        for expected_sequence, event in enumerate(applied, start=1):
            if not isinstance(event, dict):
                raise ValueError("invalid applied event in snapshot")
            event_sequence = event.get("sequence")
            if isinstance(event_sequence, bool) or event_sequence != expected_sequence:
                raise ValueError("snapshot event history sequence is not contiguous")
            ledger.apply(event)

        # Strict equality checks both persisted fields and all derived values.
        # The schema version is the compatibility boundary for this contract.
        restored = ledger.snapshot()
        if restored != snapshot:
            raise ValueError("snapshot state is inconsistent with its event history")
        return ledger

    def _apply_event(self, event: dict, occurred_at: datetime) -> None:
        kind = event.get("type")
        currency = str(event.get("currency", self.base_currency)).upper()
        if currency != self.base_currency:
            raise ValueError("event currency must match ledger base_currency")

        if kind == "PositionOpened":
            position_id = event.get("position_id")
            if not isinstance(position_id, str) or not position_id.strip():
                raise ValueError("position_id is required")
            if position_id in self._positions:
                raise ValueError("position_id already exists")
            quantity = _decimal(event.get("quantity"), "quantity", positive=True)
            approved = _decimal(event.get("approved_risk"), "approved_risk", positive=True)
            actual = _decimal(event.get("actual_risk"), "actual_risk", positive=True)
            if actual > approved:
                raise ValueError("actual modeled risk exceeds approved risk")
            entry = _decimal(event.get("entry"), "entry", positive=True)
            stop = _decimal(event.get("stop"), "stop", positive=True)
            side = event.get("side")
            if side not in ("BUY", "SELL"):
                raise ValueError("side must be BUY or SELL")
            if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
                raise ValueError("stop must be on the loss side of entry")
            symbol = event.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("symbol is required")
            self._positions[position_id] = {
                "position_id": position_id, "symbol": symbol.upper(),
                "side": side, "quantity": quantity,
                "original_quantity": quantity, "entry": entry, "stop": stop,
                "approved_risk": approved, "actual_risk": actual,
                "realized_net_pnl": ZERO, "opened_at": occurred_at.isoformat(),
            }
            return

        if kind in ("PositionPartiallyClosed", "PositionClosed"):
            position_id = event.get("position_id")
            position = self._positions.get(position_id)
            if position is None:
                raise ValueError("cannot settle an unknown position")
            quantity = _decimal(event.get("quantity"), "quantity", positive=True)
            remaining = position["quantity"]
            if quantity > remaining:
                raise ValueError("close quantity exceeds remaining quantity")
            if kind == "PositionClosed" and quantity != remaining:
                raise ValueError("PositionClosed quantity must equal full remaining quantity")
            if kind == "PositionPartiallyClosed" and quantity >= remaining:
                raise ValueError("partial close must leave positive remaining quantity")
            gross = _decimal(event.get("gross_pnl"), "gross_pnl")
            commission = _decimal(event.get("commission", 0), "commission", non_negative=True)
            # FX rollover financing can be a debit or a credit; unlike commissions
            # and explicit fees, swap is signed and must retain its direction.
            swap = _decimal(event.get("swap", 0), "swap")
            other_fees = _decimal(event.get("other_fees", 0), "other_fees", non_negative=True)
            fees = commission + swap + other_fees
            net = gross - fees

            self.balance += net
            self.realized_pnl_lifetime += net
            self.daily_realized_net_pnl += net
            self.weekly_realized_net_pnl += net
            self.fees_lifetime += fees
            position["realized_net_pnl"] += net
            position["quantity"] = remaining - quantity
            if position["quantity"] == ZERO:
                final_net = position["realized_net_pnl"]
                if final_net < ZERO:
                    self.consecutive_losses += 1
                elif final_net > ZERO:
                    self.consecutive_losses = 0
                del self._positions[position_id]
            return

        if kind == "MarkToMarketUpdated":
            self.unrealized_pnl = _decimal(event.get("unrealized_pnl"), "unrealized_pnl")
            return

        if kind == "PeriodRolled":
            daily_id = event.get("daily_period_id")
            weekly_id = event.get("weekly_period_id")
            if daily_id is not None:
                daily_id = str(daily_id)
                if not daily_id or daily_id == self.daily_period_id:
                    raise ValueError("new daily period ID must differ")
                if self.balance <= ZERO:
                    raise ValueError("daily period start balance must be positive")
                self.daily_period_id = daily_id
                # Period risk is measured from account equity at rollover,
                # including floating PnL on positions carried across sessions.
                self.daily_start_balance = self.equity
                if self.daily_start_balance <= ZERO:
                    raise ValueError("daily period start equity must be positive")
                self.daily_realized_net_pnl = ZERO
            if weekly_id is not None:
                weekly_id = str(weekly_id)
                if not weekly_id or weekly_id == self.weekly_period_id:
                    raise ValueError("new weekly period ID must differ")
                if self.balance <= ZERO:
                    raise ValueError("weekly period start balance must be positive")
                self.weekly_period_id = weekly_id
                # Weekly risk uses the same equity-based boundary as daily risk.
                self.weekly_start_balance = self.equity
                if self.weekly_start_balance <= ZERO:
                    raise ValueError("weekly period start equity must be positive")
                self.weekly_realized_net_pnl = ZERO
            if daily_id is None and weekly_id is None:
                raise ValueError("PeriodRolled must roll a period")
            return
        raise ValueError(f"unsupported event type: {kind!r}")

    def _update_drawdown(self) -> None:
        equity = self.equity
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = max(ZERO, self.peak_equity - equity)
        self.max_drawdown_amount = max(self.max_drawdown_amount, drawdown)
        if self.peak_equity > ZERO:
            pct = drawdown / self.peak_equity * Decimal("100")
            self.max_drawdown_pct = max(self.max_drawdown_pct, pct)

    @property
    def equity(self) -> Decimal:
        return self.balance + self.unrealized_pnl

    @property
    def open_risk_amount(self) -> Decimal:
        total = ZERO
        for position in self._positions.values():
            total += position["actual_risk"] * position["quantity"] / position["original_quantity"]
        return total

    @property
    def open_risk_pct(self) -> Decimal:
        if self.equity <= ZERO:
            if self.open_risk_amount > ZERO:
                raise ValueError("cannot calculate open risk with non-positive equity")
            return ZERO
        return self.open_risk_amount / self.equity * Decimal("100")

    @property
    def daily_loss_pct(self) -> Decimal:
        """Peak-independent daily loss from rollover equity, including floating PnL."""
        if self.daily_start_balance <= ZERO:
            raise ValueError("daily period start equity must be positive")
        return max(
            ZERO,
            (self.daily_start_balance - self.equity)
            / self.daily_start_balance * Decimal("100"),
        )

    @property
    def weekly_loss_pct(self) -> Decimal:
        """Peak-independent weekly loss from rollover equity, including floating PnL."""
        if self.weekly_start_balance <= ZERO:
            raise ValueError("weekly period start equity must be positive")
        return max(
            ZERO,
            (self.weekly_start_balance - self.equity)
            / self.weekly_start_balance * Decimal("100"),
        )

    def snapshot(self) -> dict:
        def money(value: Decimal) -> str:
            return format(value, "f")
        positions = {}
        for key, value in sorted(self._positions.items()):
            positions[key] = {k: money(v) if isinstance(v, Decimal) else v
                              for k, v in value.items()}
        return {
            "schema_version": self.SCHEMA_VERSION, "base_currency": self.base_currency,
            "sequence": self.sequence, "initial_balance": money(self.initial_balance),
            "balance": money(self.balance),
            "equity": money(self.equity), "unrealized_pnl": money(self.unrealized_pnl),
            "realized_pnl_lifetime": money(self.realized_pnl_lifetime),
            "fees_lifetime": money(self.fees_lifetime),
            "open_risk_amount": money(self.open_risk_amount),
            "open_risk_pct": money(self.open_risk_pct),
            "initial_daily_period_id": self.initial_daily_period_id,
            "initial_weekly_period_id": self.initial_weekly_period_id,
            "daily_period_id": self.daily_period_id,
            "daily_start_balance": money(self.daily_start_balance),
            "daily_realized_net_pnl": money(self.daily_realized_net_pnl),
            "daily_loss_pct": money(self.daily_loss_pct),
            "weekly_period_id": self.weekly_period_id,
            "weekly_start_balance": money(self.weekly_start_balance),
            "weekly_realized_net_pnl": money(self.weekly_realized_net_pnl),
            "weekly_loss_pct": money(self.weekly_loss_pct),
            "peak_equity": money(self.peak_equity),
            "max_drawdown_amount": money(self.max_drawdown_amount),
            "max_drawdown_pct": money(self.max_drawdown_pct),
            "consecutive_losses": self.consecutive_losses,
            "open_positions": positions,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "applied_events": [deepcopy(event) for _, event in sorted(
                self._applied_event_ids.items(), key=lambda item: item[1]["sequence"]
            )],
        }
