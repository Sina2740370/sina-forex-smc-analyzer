"""Event-driven multi-symbol MAS backtest with one shared account and Ledger.

This engine is deliberately separate from backtest_mas: the latter remains a
single-symbol compatibility API. All symbols share one UTC event timeline,
one SQLite Ledger account, and one Risk Agent AccountState. It is a historical
simulation only; it does not route orders or access a broker.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import math
from datetime import timedelta

import pandas as pd

from .aggregator import analyze_mas
from .backtest import BTTrade, _advance_position
from .contracts import BUY
from .ledger import SQLiteLedger
from .risk_agent import AccountState, RISK_RULES, evaluate_risk
from .symbols import get_spec, realized_pnl_usd


@dataclass
class PortfolioBTResult:
    symbols: list[str]
    n_points: int = 0
    n_signals: int = 0
    wait_reasons: dict = field(default_factory=dict)
    trades: list[BTTrade] = field(default_factory=list)
    equity_start: float = RISK_RULES["equity_default"]
    equity_end: float = RISK_RULES["equity_default"]
    open_positions_at_end: list[dict] = field(default_factory=list)
    ledger_events: int = 0
    execution_costs_certified: bool = False

    @property
    def wins(self) -> int:
        return sum(trade.pnl_usd > 0 for trade in self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0)
        gross_loss = -sum(t.pnl_usd for t in self.trades if t.pnl_usd < 0)
        return gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)

    @property
    def max_drawdown_pct(self) -> float:
        equity = peak = self.equity_start
        max_dd = 0.0
        for trade in sorted(self.trades, key=lambda item: (item.exit_time, item.symbol)):
            equity += trade.pnl_usd
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
        return max_dd * 100.0

    def summary(self) -> dict:
        return {
            "symbols": list(self.symbols),
            "points": self.n_points,
            "signals": self.n_signals,
            "trades": len(self.trades),
            "wins": self.wins,
            "losses": len(self.trades) - self.wins,
            "win_rate_pct": round(self.wins / len(self.trades) * 100.0, 2) if self.trades else 0.0,
            "profit_factor": round(self.profit_factor, 4),
            "total_pnl_usd": round(sum(t.pnl_usd for t in self.trades), 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "equity_start": round(self.equity_start, 2),
            "equity_end": round(self.equity_end, 2),
            "open_positions_at_end": len(self.open_positions_at_end),
            "ledger_events": self.ledger_events,
            "execution_costs_certified": self.execution_costs_certified,
            "execution_cost_model": "uncertified_zero_cost_placeholder",
            "wait_reasons": dict(sorted(self.wait_reasons.items(), key=lambda item: (-item[1], item[0]))),
        }


def _account_from_projection(projection: dict) -> AccountState:
    positions = projection["open_positions"]
    return AccountState(
        equity=float(projection["equity"]),
        open_trades=len(positions),
        open_risk_pct=sum(float(p.get("risk_pct", "0")) for p in positions.values()),
        daily_loss_pct=float(projection["daily_loss_pct"]),
        weekly_loss_pct=float(projection["weekly_loss_pct"]),
        consecutive_losses=int(projection["consecutive_losses"]),
        risk_lock_active=bool(projection["risk_lock_active"]),
        per_symbol_open={p["symbol"]: 1 for p in positions.values()},
    )


def _append_period_resets(ledger: SQLiteLedger, account_id: str,
                          initial_balance: str, exit_time: pd.Timestamp) -> dict:
    """Append deterministic daily/weekly reset events before the close event."""
    projection = ledger.replay_account(account_id, initial_balance)
    stamp = pd.Timestamp(exit_time).isoformat()
    day = pd.Timestamp(exit_time).date().isoformat()
    if projection["daily_risk_date"] != day:
        ledger.append_event({
            "account_id": account_id,
            "event_type": "DailyRiskReset",
            "idempotency_key": f"portfolio-bt-daily-reset:{day}",
            "occurred_at": stamp,
            "payload": {"daily_risk_date": day},
        })
        projection = ledger.replay_account(account_id, initial_balance)
    exit_date = pd.Timestamp(exit_time).date()
    week = (exit_date - timedelta(days=exit_date.weekday())).isoformat()
    if projection["weekly_risk_week"] != week:
        ledger.append_event({
            "account_id": account_id,
            "event_type": "WeeklyRiskReset",
            "idempotency_key": f"portfolio-bt-weekly-reset:{week}",
            "occurred_at": stamp,
            "payload": {"weekly_risk_week": week},
        })
        projection = ledger.replay_account(account_id, initial_balance)
    return projection


def backtest_portfolio(
    frames: dict[str, pd.DataFrame],
    start: str | None = None,
    end: str | None = None,
    step_hours: int = 8,
    equity: float = RISK_RULES["equity_default"],
    spreads: dict[str, float] | None = None,
    feeds=None,
    cfg: dict | None = None,
    progress: bool = False,
) -> PortfolioBTResult:
    """Run deterministic, shared-account multi-symbol historical simulation.

    Event order at a timestamp is fixed:
      1) settle all existing positions by symbol;
      2) evaluate new decisions by symbol;
      3) append Ledger events in that same order.

    Every decision sees only candles whose UTC timestamp is <= its event time.
    """
    if not isinstance(frames, dict) or not frames:
        raise ValueError("frames must be a non-empty symbol-to-DataFrame mapping")
    if isinstance(step_hours, bool) or not isinstance(step_hours, int) or step_hours <= 0:
        raise ValueError("step_hours must be a positive integer")
    try:
        equity_value = float(equity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("equity must be finite and positive") from exc
    if not math.isfinite(equity_value) or equity_value <= 0:
        raise ValueError("equity must be finite and positive")

    normalized: dict[str, pd.DataFrame] = {}
    for symbol in sorted(frames):
        get_spec(symbol)
        frame = frames[symbol]
        required = {"time", "open", "high", "low", "close"}
        if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
            raise ValueError(f"{symbol}: frame must contain {sorted(required)}")
        clean = frame.dropna(subset=list(required)).copy()
        clean["time"] = pd.to_datetime(clean["time"], utc=True, errors="coerce")
        clean = (
            clean.dropna(subset=["time"])
            .drop_duplicates("time")
            .sort_values("time", kind="mergesort")
            .reset_index(drop=True)
        )
        if clean.empty:
            raise ValueError(f"{symbol}: no valid UTC candles")
        normalized[symbol] = clean

    for symbol, raw_spread in (spreads or {}).items():
        if symbol not in normalized:
            raise ValueError(f"spread supplied for unsupported input symbol: {symbol}")
        try:
            spread_value = float(raw_spread)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{symbol}: spread must be a finite non-negative price value") from exc
        if isinstance(raw_spread, bool) or not math.isfinite(spread_value) or spread_value < 0:
            raise ValueError(f"{symbol}: spread must be a finite non-negative price value")

    first_time = max(frame["time"].iloc[0] for frame in normalized.values())
    last_time = min(frame["time"].iloc[-1] for frame in normalized.values())
    requested_start = pd.to_datetime(start, utc=True) if start is not None else first_time + pd.Timedelta(days=250)
    requested_end = pd.to_datetime(end, utc=True) if end is not None else last_time
    # Even explicit caller bounds cannot extend the simulation beyond the
    # intersection of all symbol histories.
    t0 = max(requested_start, first_time)
    t1 = min(requested_end, last_time)
    if t0 >= t1:
        return PortfolioBTResult(symbols=list(normalized), equity_start=equity_value, equity_end=equity_value)

    # Explicit end bounds are authoritative. Never let terminal settlement read
    # a candle later than the requested backtest end.
    normalized = {
        symbol: frame.loc[frame["time"] <= t1].reset_index(drop=True)
        for symbol, frame in normalized.items()
    }
    if any(frame.empty for frame in normalized.values()):
        return PortfolioBTResult(symbols=list(normalized), equity_start=equity_value, equity_end=equity_value)

    step = pd.Timedelta(hours=step_hours)
    decision_times = pd.date_range(start=t0, end=t1, freq=step)
    decision_sets = {
        symbol: set(decision_times.intersection(pd.DatetimeIndex(frame["time"])))
        for symbol, frame in normalized.items()
    }
    # The union of all candle timestamps is the master clock. This ensures
    # exits are processed at their own symbol's first observable candle, not
    # delayed until another symbol happens to reach its next decision time.
    timeline = sorted({
        stamp
        for frame in normalized.values()
        for stamp in frame.loc[(frame["time"] >= t0) & (frame["time"] <= t1), "time"]
    })
    if not timeline:
        return PortfolioBTResult(symbols=list(normalized), equity_start=equity_value, equity_end=equity_value)

    from ..data.providers import BASE_TFS, resample_ohlc
    top_caches = {
        symbol: {tf: resample_ohlc(frame, tf) for tf in BASE_TFS}
        for symbol, frame in normalized.items()
    }
    arrays = {symbol: frame["time"].to_numpy() for symbol, frame in normalized.items()}
    positions: dict[str, dict] = {}
    account_id = "backtest:portfolio:" + hashlib.sha256(
        "|".join(sorted(normalized)).encode()
    ).hexdigest()[:16]
    initial_balance = str(equity_value)
    ledger = SQLiteLedger(":memory:")
    result = PortfolioBTResult(
        symbols=list(normalized), equity_start=equity_value, equity_end=equity_value
    )

    def refresh_account() -> tuple[dict, AccountState]:
        projection = ledger.replay_account(account_id, initial_balance)
        return projection, _account_from_projection(projection)

    def settle(symbol: str, position: dict, exit_px: float,
               exit_time: pd.Timestamp, last_idx: int, was_loss, outcome: str) -> None:
        spec = get_spec(symbol)
        gross = realized_pnl_usd(
            symbol, position["entry"], exit_px, position["direction"], position["lots"]
        )
        projection = _append_period_resets(ledger, account_id, initial_balance, exit_time)
        loss_pct = max(0.0, -gross) / max(float(projection["equity"]), 1e-9) * 100.0
        exit_stamp = pd.Timestamp(exit_time).isoformat()
        ledger.append_event({
            "account_id": account_id,
            "trade_id": position["trade_id"],
            "event_type": "PositionClosed",
            "idempotency_key": f"portfolio-bt-close:{position['trade_id']}:{exit_stamp}",
            "occurred_at": exit_stamp,
            "payload": {
                "lots_closed": str(position["lots"]),
                "pnl_gross": str(gross),
                "commission": "0",
                "slippage": "0",
                "swap": "0",
                "daily_loss_pct_delta": str(loss_pct),
                "weekly_loss_pct_delta": str(loss_pct),
                "consecutive_loss_stop": RISK_RULES["consecutive_losses_stop"],
                "exit_price": str(exit_px),
                "exit_time": exit_stamp,
                "outcome": outcome,
            },
        })
        after = ledger.replay_account(account_id, initial_balance)
        net = float(after["closed_trades"][-1]["pnl_usd"])
        entry_idx = int(normalized[symbol]["time"].searchsorted(position["entry_time"]))
        result.trades.append(BTTrade(
            symbol, position["direction"], position["entry_time"], exit_time,
            position["entry"], position["sl"], position["tp2"], position["lots"],
            net, net / position["risk_usd"] if position["risk_usd"] > 0 else 0.0,
            max(0, last_idx - entry_idx), position["regime"], position["session"],
            position["final_score"], position["confidence"], outcome,
        ))
        positions.pop(symbol, None)

    period_day = None
    period_week = None
    try:
        for event_index, now in enumerate(timeline):
            # Period budgets roll on day/week boundaries, not on every M15
            # event and not only when a trade closes.
            event_day = pd.Timestamp(now).date()
            day_key = event_day.isoformat()
            week_key = (event_day - timedelta(days=event_day.weekday())).isoformat()
            if day_key != period_day or week_key != period_week:
                _append_period_resets(ledger, account_id, initial_balance, now)
                period_day, period_week = day_key, week_key

            # Advance each open position through all candles up to this global
            # timestamp. The global timeline includes every symbol candle.
            for symbol in sorted(list(positions)):
                position = positions[symbol]
                frame = normalized[symbol]
                outcome = _advance_position(
                    position, frame, arrays[symbol], now, get_spec(symbol)
                )
                if outcome is None and now == frame["time"].iloc[-1]:
                    # Exhaustion of this symbol's own data is a labeled
                    # backtest boundary; it must not be deferred to another
                    # symbol's later timestamp.
                    outcome = _advance_position(
                        position, frame, arrays[symbol], now, get_spec(symbol),
                        force_exit=True,
                    )
                if outcome is not None:
                    exit_px, exit_time, last_idx, was_loss = outcome
                    reason = "TIME_EXIT" if was_loss is None else ("SL" if was_loss else "TP")
                    settle(symbol, position, exit_px, pd.Timestamp(exit_time),
                           last_idx, was_loss, reason)

            # Deterministic same-time arbitration: lexical symbol order.
            for symbol in sorted(normalized):
                if now not in decision_sets[symbol]:
                    continue
                result.n_points += 1
                if symbol in positions:
                    continue
                frame = normalized[symbol]
                # Do not open a new position on the final available candle:
                # there is no subsequent market event on which to manage it.
                if now == frame["time"].iloc[-1]:
                    continue
                row_index = int(frame["time"].searchsorted(now, side="right")) - 1
                if row_index < 0 or frame["time"].iloc[row_index] != now:
                    continue
                projection, account = refresh_account()
                past = frame.loc[frame["time"] <= now].reset_index(drop=True)
                if past.empty or past["time"].max() > now:
                    raise RuntimeError(f"{symbol}: lookahead guard rejected a future candle")
                signal = analyze_mas(
                    past, symbol, now,
                    spread_price=(spreads or {}).get(symbol, get_spec(symbol).spread_price()),
                    acct=account, feeds=feeds, top_cache=top_caches[symbol], cfg=cfg,
                )
                if signal.signal == "WAIT" or not signal.cand:
                    key = (signal.wait_reason or "WAIT").split(":")[0].strip() or "WAIT"
                    result.wait_reasons[key] = result.wait_reasons.get(key, 0) + 1
                    continue

                row = frame.iloc[row_index]
                direction_sign = 1 if signal.signal == BUY else -1
                spread = (spreads or {}).get(symbol, get_spec(symbol).spread_price())
                entry = float(row["close"]) + direction_sign * spread
                sl, tp2 = float(signal.cand["sl"]), float(signal.cand["tp2"])
                verdict = evaluate_risk(
                    symbol, signal.signal, entry, sl, tp2, entry, acct=account,
                    fx_timestamp=pd.Timestamp(now).isoformat(),
                )
                if not verdict.approved or verdict.sizing_result is None:
                    key = "FILL_RISK_REJECTED"
                    result.wait_reasons[key] = result.wait_reasons.get(key, 0) + 1
                    continue

                fingerprint = signal.fingerprint()
                trade_id = hashlib.sha256(
                    f"{account_id}|{symbol}|{signal.signal}|{now.isoformat()}|{fingerprint}".encode()
                ).hexdigest()[:32]
                sizing_id = hashlib.sha256(f"{trade_id}|sizing".encode()).hexdigest()[:32]
                sizing = replace(verdict.sizing_result, sizing_id=sizing_id)
                verdict.sizing_result = sizing
                lots, risk_usd = verdict.lots, verdict.risk_amount
                ledger.append_event({
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "event_type": "PositionOpened",
                    "schema_version": 2,
                    "idempotency_key": f"portfolio-bt-open:{trade_id}",
                    "occurred_at": now.isoformat(),
                    "payload": {
                        "symbol": symbol, "direction": signal.signal,
                        "lots": str(lots), "entry_price": str(entry),
                        "sl": str(sl), "tp2": str(tp2),
                        "entry_time": now.isoformat(), "risk_usd": str(risk_usd),
                        "risk_pct": str(risk_usd / account.equity * 100.0),
                        "final_score": float(signal.final_score),
                        "regime": signal.market_regime, "session": signal.session,
                        "sizing_result": asdict(sizing),
                    },
                })
                positions[symbol] = {
                    "trade_id": trade_id, "direction": signal.signal,
                    "entry_time": now, "entry": entry, "sl": sl, "tp2": tp2,
                    "lots": lots, "risk_usd": risk_usd, "last_idx": row_index,
                    "regime": signal.market_regime, "session": signal.session,
                    "final_score": float(signal.final_score),
                    "confidence": float(signal.confidence),
                }
                result.n_signals += 1

            if progress and (event_index + 1) % 5000 == 0:
                projection, _ = refresh_account()
                print(
                    f"[portfolio] {event_index + 1}/{len(timeline)} events; "
                    f"{result.n_signals} entries; {len(result.trades)} closes; "
                    f"equity={projection['equity']}",
                    flush=True,
                )

        # Dataset exhaustion is a backtest-only boundary, not a market close.
        # Mark remaining positions to each symbol's own final close, ordered by
        # actual timestamp and then symbol to keep the Ledger replay deterministic.
        terminal = []
        for symbol, position in positions.items():
            outcome = _advance_position(
                position, normalized[symbol], arrays[symbol],
                normalized[symbol]["time"].iloc[-1], get_spec(symbol), force_exit=True,
            )
            if outcome is not None:
                terminal.append((pd.Timestamp(outcome[1]), symbol, outcome))
        for exit_time, symbol, outcome in sorted(terminal, key=lambda item: (item[0], item[1])):
            exit_px, _time, last_idx, _was_loss = outcome
            settle(symbol, positions[symbol], exit_px, exit_time, last_idx, None, "TIME_EXIT")

        projection, _ = refresh_account()
        result.equity_end = float(projection["equity"])
        result.open_positions_at_end = [
            dict(position) for _, position in sorted(positions.items())
        ]
        result.ledger_events = len(ledger.read_events(account_id))
        result.trades.sort(key=lambda item: (item.exit_time, item.symbol, item.entry_time))
        return result
    finally:
        ledger.close()
