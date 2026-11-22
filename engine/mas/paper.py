"""فاز ۵ — موتور اجرای کاغذی (Paper Trading) با ژورنال و attribution.

چرخه هر اجرا (قابل زمان‌بندی خارجی؛ مثلاً cron هر ۴ ساعت):
1. بارگذاری وضعیت (حقوق، معاملات باز، دنباله ضرر) از out_mas/paper/state.json
2. تسویه معاملات باز روی داده جدید (SL/TP با قاعده بدبینانهٔ بک‌تست)
3. تحلیل چندجفتی با AccountState بازسازی‌شده از SQLite Ledger
4. تأیید مجدد حجم در قیمت Fill توسط Risk Agent و ثبت PositionOpened
5. بازسازی اتمیک state.json و journal.jsonl به‌عنوان projectionهای قابل‌بازیابی

SQLite Ledger منبع اصلی وضعیت مالی است؛ JSON/JSONL فقط projection هستند.
صادقانه: اجرا «کاغذی» است؛ اسپرد لحظه‌ای/سلیپیج/سواپ ندارد و ورود با کلوز کندل
سیگنال مدل شده است. داده M15 Dukascopy باید دستی تازه شود (اسکریپت آپدیت داده موجود).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from dataclasses import asdict

import pandas as pd

from .aggregator import analyze_mas
from .backtest import _advance_position
from .contracts import BUY
from .ledger import SQLiteLedger
from .portfolio import _csv_path
from .risk_agent import AccountState, RISK_RULES, evaluate_risk
from .symbols import get_spec, realized_pnl_usd


def _projection_to_paper_state(projection: dict) -> dict:
    """Convert the authoritative Ledger projection into the legacy Paper API shape."""
    open_positions = []
    for raw in projection["open_positions"].values():
        position = dict(raw)
        position["entry"] = float(position.pop("entry_price"))
        position["lots"] = float(position["lots"])
        position["sl"] = float(position["sl"]) if position.get("sl") is not None else 0.0
        position["tp2"] = float(position["tp2"]) if position.get("tp2") is not None else 0.0
        position["risk_usd"] = float(position.get("risk_usd", "0"))
        position["risk_pct"] = float(position.get("risk_pct", "0"))
        position["unrealized_pnl"] = float(position.get("unrealized_pnl", "0"))
        open_positions.append(position)
    return {
        "equity": float(projection["equity"]),
        "consecutive_losses": int(projection["consecutive_losses"]),
        "risk_lock_active": bool(projection["risk_lock_active"]),
        "daily_loss_pct": float(projection["daily_loss_pct"]),
        "daily_risk_date": projection.get("daily_risk_date"),
        "weekly_loss_pct": float(projection["weekly_loss_pct"]),
        "weekly_risk_week": projection.get("weekly_risk_week"),
        "open_positions": open_positions,
        "runs": int(projection["runs"]),
        "last_run_ts": projection.get("last_run_ts"),
    }


def _read_legacy_closed_trades(journal_path: str) -> list[dict]:
    """Read the old JSONL journal once during migration; Ledger remains authoritative."""
    trades = []
    if not os.path.exists(journal_path):
        return trades
    with open(journal_path, encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # Ignore only malformed historical projection lines; they never affect equity.
                continue
            trades.extend(record.get("closed", []))
    return trades


def run_paper(csv_dir: str, symbols: list, out_dir: str = "out_mas/paper",
              equity: float | None = None, feeds=None, now: pd.Timestamp | None = None,
              min_gap_hours: float = 1.0) -> dict:
    """Run one Paper cycle; SQLite Ledger is the financial source of truth."""
    os.makedirs(out_dir, exist_ok=True)
    state_p = os.path.join(out_dir, "state.json")
    journal_p = os.path.join(out_dir, "journal.jsonl")
    ledger = SQLiteLedger(os.path.join(out_dir, "ledger.sqlite3"))
    account_id = "paper"

    # Legacy import is a resumable protocol. The pending/completed markers
    # make crashes between event seeding and immutable metadata recoverable.
    migration_pending_key = f"legacy_migration:{account_id}:v1:pending"
    migration_completed_key = f"legacy_migration:{account_id}:v1:completed"
    initial_balance_key = f"initial_balance:{account_id}"
    existing_events = ledger.read_events(account_id)
    stored_initial_balance = ledger.get_meta(initial_balance_key)
    migration_pending = ledger.get_meta(migration_pending_key) is not None
    migration_completed = ledger.get_meta(migration_completed_key) is not None
    has_legacy_seed_events = any(
        event.idempotency_key == "legacy-paper-state-v1"
        or event.idempotency_key.startswith("legacy-open:")
        or event.idempotency_key.startswith("legacy-history:")
        for event in existing_events
    )

    # A metadata-only account with no events is the exact crash window left by
    # the former implementation. Legacy seed events are also safe to replay:
    # their deterministic idempotency keys make the import idempotent.
    resume_legacy_import = (
        not migration_completed
        and (
            migration_pending
            or has_legacy_seed_events
            or not existing_events
        )
    )
    if stored_initial_balance is None and existing_events and not (
        migration_pending or has_legacy_seed_events
    ):
        raise RuntimeError(
            "Paper Ledger has events but immutable initial balance metadata is missing"
        )

    legacy_state = {}
    if resume_legacy_import:
        if os.path.exists(state_p):
            try:
                with open(state_p, encoding="utf-8") as stream:
                    legacy_state = json.load(stream)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "Cannot migrate legacy Paper state: state.json is unreadable and no completed migration exists"
                ) from exc
            if not isinstance(legacy_state, dict):
                raise RuntimeError(
                    "Cannot migrate legacy Paper state: state.json must contain a JSON object"
                )

        seed_equity = legacy_state.get(
            "equity", equity or RISK_RULES["equity_default"]
        )
        initial_balance = stored_initial_balance or str(seed_equity)

        # Record intent before appending any migration event. If the process
        # stops at any later point, the next run resumes this exact import.
        if not migration_pending:
            ledger.set_meta_once(migration_pending_key, "started")

        seed_events = []
        if legacy_state:
            last_run = legacy_state.get("last_run_ts")
            try:
                legacy_day = pd.Timestamp(last_run).date().isoformat() if last_run else None
            except (TypeError, ValueError):
                legacy_day = None
            legacy_lock = bool(
                legacy_state.get("risk_lock_active", False)
                or legacy_state.get("consecutive_losses", 0)
                >= RISK_RULES["consecutive_losses_stop"]
            )
            seed_events.append({
                "account_id": account_id,
                "event_type": "RiskStateSnapshot",
                "idempotency_key": "legacy-paper-state-v1",
                **({"occurred_at": str(last_run)} if legacy_day else {}),
                "payload": {
                    "consecutive_losses": int(legacy_state.get("consecutive_losses", 0)),
                    "daily_loss_pct": str(legacy_state.get("daily_loss_pct", 0)),
                    "weekly_loss_pct": str(legacy_state.get("weekly_loss_pct", 0)),
                    "risk_lock_active": legacy_lock,
                    "runs": int(legacy_state.get("runs", 0)),
                    "last_run_ts": last_run,
                    "daily_risk_date": legacy_state.get("daily_risk_date") or legacy_day,
                    "weekly_risk_week": legacy_state.get("weekly_risk_week"),
                },
            })
            for index, position in enumerate(legacy_state.get("open_positions", [])):
                trade_id = position.get("trade_id")
                if not trade_id:
                    identity = "|".join(
                        [str(index)] + [
                            str(position.get(k, ""))
                            for k in ("symbol", "direction", "entry_time", "entry")
                        ]
                    )
                    trade_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
                seed_events.append({
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "event_type": "PositionOpened",
                    "idempotency_key": f"legacy-open:{trade_id}",
                    "occurred_at": str(
                        position.get("entry_time")
                        or last_run
                        or pd.Timestamp.now(tz="UTC").isoformat()
                    ),
                    "payload": {
                        "symbol": position.get("symbol"),
                        "direction": position.get("direction"),
                        "lots": str(position.get("lots", "0")),
                        "entry_price": str(position.get("entry", "0")),
                        "sl": str(position.get("sl", "0")),
                        "tp2": str(position.get("tp2", "0")),
                        "entry_time": str(position.get("entry_time", "")),
                        "risk_usd": str(position.get("risk_usd", "0")),
                        "risk_pct": str(position.get("risk_pct", "0")),
                        "final_score": position.get("final_score", 0.0),
                        "regime": position.get("regime", ""),
                        "session": position.get("session", ""),
                        "sizing_result": position.get("sizing_result"),
                    },
                })
        for index, trade in enumerate(_read_legacy_closed_trades(journal_p)):
            legacy_trade_id = str(trade.get("trade_id") or f"legacy-closed-{index}")
            seed_events.append({
                "account_id": account_id,
                "trade_id": legacy_trade_id,
                "event_type": "HistoricalTradeImported",
                "idempotency_key": f"legacy-history:{index}:{legacy_trade_id}",
                "payload": {"trade": trade},
            })

        if seed_events:
            ledger.append_events(seed_events)
        initial_balance = ledger.set_meta_once(initial_balance_key, str(initial_balance))
        ledger.set_meta_once(migration_completed_key, "completed")
    else:
        if stored_initial_balance is None:
            raise RuntimeError(
                "Paper Ledger migration state is inconsistent: initial balance is missing"
            )
        initial_balance = stored_initial_balance

    projection = ledger.replay_account(account_id, initial_balance)
    state = _projection_to_paper_state(projection)

    spec_of = {symbol: get_spec(symbol) for symbol in symbols}
    dfs, t_now = {}, {}
    from ..data.providers import load_mt5_csv
    requested_at = pd.to_datetime(now, utc=True) if now is not None else None
    for symbol in symbols:
        path = _csv_path(csv_dir, symbol)
        if not path:
            continue
        frame = load_mt5_csv(path).dropna().drop_duplicates("time").copy()
        if frame.empty or "time" not in frame.columns:
            continue
        frame["time"] = pd.to_datetime(frame["time"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
        if not frame.empty:
            dfs[symbol] = frame

    # One UTC snapshot for all symbols: never mix different final candle times.
    analysis_at = requested_at
    if analysis_at is None and dfs:
        analysis_at = min(frame["time"].max() for frame in dfs.values())
    if analysis_at is None:
        analysis_at = pd.Timestamp.now(tz="UTC")
    for symbol, frame in dfs.items():
        if (frame["time"] <= analysis_at).any():
            t_now[symbol] = analysis_at

    analysis_point = analysis_at.isoformat()
    daily_risk_date = pd.Timestamp(analysis_point).date().isoformat()
    if state.get("daily_risk_date") != daily_risk_date:
        ledger.append_event({
            "account_id": account_id,
            "event_type": "DailyRiskReset",
            "idempotency_key": f"daily-reset:{daily_risk_date}",
            "occurred_at": analysis_point,
            "payload": {"daily_risk_date": daily_risk_date},
        })
        projection = ledger.replay_account(account_id, initial_balance)
        state = _projection_to_paper_state(projection)
    analysis_date = pd.Timestamp(analysis_point).date()
    weekly_risk_week = (analysis_date - timedelta(days=analysis_date.weekday())).isoformat()
    if state.get("weekly_risk_week") != weekly_risk_week:
        ledger.append_event({
            "account_id": account_id,
            "event_type": "WeeklyRiskReset",
            "idempotency_key": f"weekly-reset:{weekly_risk_week}",
            "occurred_at": analysis_point,
            "payload": {"weekly_risk_week": weekly_risk_week},
        })
        projection = ledger.replay_account(account_id, initial_balance)
        state = _projection_to_paper_state(projection)

    # 1. Settle all positions whose SL/TP was reached. The batch commit is atomic.
    closed, close_events = [], []
    equity_cursor = state["equity"]
    for position in state["open_positions"]:
        symbol = position["symbol"]
        if symbol not in t_now:
            continue
        frame, spec = dfs[symbol], spec_of[symbol]
        working = {**position,
                   "last_idx": int(frame["time"].searchsorted(pd.Timestamp(position["entry_time"])))}
        outcome = _advance_position(working, frame, frame["time"].to_numpy(),
                                    t_now[symbol], spec)
        if outcome is None:
            continue
        exit_price, exit_time, _idx, was_loss = outcome
        pnl = realized_pnl_usd(
            symbol, position["entry"], exit_price, position["direction"], position["lots"]
        )
        trade_id = position["trade_id"]
        exit_ts = pd.Timestamp(exit_time).isoformat()
        loss_delta = max(0.0, -pnl) / max(equity_cursor, 1e-9) * 100.0
        close_events.append({
            "account_id": account_id,
            "trade_id": trade_id,
            "event_type": "PositionClosed",
            "idempotency_key": f"paper-close:{trade_id}:{exit_ts}",
            "occurred_at": exit_ts,
            "payload": {
                "lots_closed": str(position["lots"]),
                "pnl_gross": str(pnl),
                "commission": "0",
                "slippage": "0",
                "swap": "0",
                "daily_loss_pct_delta": str(loss_delta),
                "weekly_loss_pct_delta": str(loss_delta),
                "consecutive_loss_stop": RISK_RULES["consecutive_losses_stop"],
                "exit_price": str(exit_price),
                "exit_time": exit_ts,
                "outcome": "SL" if was_loss else "TP",
            },
        })
        closed.append({**position, "exit": exit_price, "exit_time": exit_ts,
                       "pnl_usd": round(pnl, 2), "outcome": "SL" if was_loss else "TP"})
        equity_cursor += pnl

    if close_events:
        ledger.append_events(close_events)
        projection = ledger.replay_account(account_id, initial_balance)
        state = _projection_to_paper_state(projection)

    # 2. Analyze and obtain a fresh Risk Agent authorization at the simulated fill price.
    acct = AccountState(
        equity=state["equity"],
        open_trades=len(state["open_positions"]),
        open_risk_pct=sum(p.get("risk_pct", 0.0) for p in state["open_positions"]),
        daily_loss_pct=state["daily_loss_pct"],
        weekly_loss_pct=state["weekly_loss_pct"],
        consecutive_losses=state["consecutive_losses"],
        risk_lock_active=state["risk_lock_active"],
        per_symbol_open={p["symbol"]: 1 for p in state["open_positions"]},
    )
    new_signals, results = [], {}
    last_run = state.get("last_run_ts")
    for symbol in symbols:
        if symbol not in t_now:
            results[symbol] = {"error": "داده‌ای در نقطه زمانی مشترک پرتفوی موجود نیست"}
            continue
        if last_run and (t_now[symbol] - pd.Timestamp(last_run)) < pd.Timedelta(hours=min_gap_hours):
            results[symbol] = {"skipped": "فاصله کم از اجرای قبلی"}
            continue
        signal = analyze_mas(dfs[symbol], symbol, t_now[symbol], acct=acct, feeds=feeds)
        results[symbol] = signal.to_json(include_reports=False)
        if signal.signal not in ("BUY", "SELL") or not signal.cand or symbol in acct.per_symbol_open:
            continue

        index = int(dfs[symbol]["time"].searchsorted(t_now[symbol], side="right")) - 1
        if index < 0:
            continue
        row = dfs[symbol].iloc[index]
        spec = spec_of[symbol]
        direction_sign = 1 if signal.signal == BUY else -1
        fill_price = float(row["close"]) + direction_sign * spec.spread_price()
        execution_verdict = evaluate_risk(
            symbol, signal.signal, fill_price, float(signal.cand["sl"]),
            float(signal.cand["tp2"]), fill_price, acct=acct,
            fx_timestamp=t_now[symbol].isoformat(),
        )
        if not execution_verdict.approved or execution_verdict.sizing_result is None:
            results[symbol]["risk_approved"] = False
            results[symbol]["sizing_result"] = None
            results[symbol]["wait_reason"] = "رد تأیید مجدد ریسک در قیمت Fill: " + "; ".join(execution_verdict.rejection_reasons)
            continue

        fingerprint = results[symbol].get("fingerprint", "")
        trade_id = hashlib.sha256(
            f"{symbol}|{signal.signal}|{t_now[symbol].isoformat()}|{fingerprint}".encode()
        ).hexdigest()[:32]
        sizing_snapshot = asdict(execution_verdict.sizing_result)
        results[symbol]["risk_approved"] = True
        results[symbol]["sizing_result"] = sizing_snapshot
        results[symbol]["lot_size"] = f"{execution_verdict.lots:.2f}"
        results[symbol]["risk_amount"] = f"{execution_verdict.risk_amount:.2f} USD"
        new_signal = {
            "trade_id": trade_id,
            "symbol": symbol,
            "direction": signal.signal,
            "entry_time": t_now[symbol].isoformat(),
            "entry": fill_price,
            "sl": float(signal.cand["sl"]),
            "tp1": float(signal.cand["tp1"]),
            "tp2": float(signal.cand["tp2"]),
            "rr": float(signal.cand["rr"]),
            "lots": execution_verdict.lots,
            "risk_usd": execution_verdict.risk_amount,
            "sizing_result": sizing_snapshot,
            "risk_pct": execution_verdict.risk_amount / state["equity"] * 100.0,
            "final_score": round(float(signal.final_score), 1),
            "regime": signal.market_regime,
            "session": signal.session,
        }
        new_signals.append(new_signal)
        acct.open_trades += 1
        acct.open_risk_pct += new_signal["risk_pct"]
        acct.per_symbol_open[symbol] = 1

    # 3. Commit new positions and cycle marker together. If the process crashes before
    # commit, no partial financial state exists; after commit, replay rebuilds state.json.
    cycle_events = []
    for opened in new_signals:
        cycle_events.append({
            "account_id": account_id,
            "trade_id": opened["trade_id"],
            "event_type": "PositionOpened",
            "idempotency_key": f"paper-open:{opened['trade_id']}",
            "occurred_at": opened["entry_time"],
            "payload": {
                "symbol": opened["symbol"],
                "direction": opened["direction"],
                "lots": str(opened["lots"]),
                "entry_price": str(opened["entry"]),
                "sl": str(opened["sl"]),
                "tp2": str(opened["tp2"]),
                "entry_time": opened["entry_time"],
                "risk_usd": str(opened["risk_usd"]),
                "risk_pct": str(opened["risk_pct"]),
                "final_score": opened["final_score"],
                "regime": opened["regime"],
                "session": opened["session"],
                "sizing_result": opened["sizing_result"],
            },
        })
    cycle_events.append({
        "account_id": account_id,
        "event_type": "PaperCycleCompleted",
        "idempotency_key": f"paper-cycle:{analysis_point}",
        "occurred_at": analysis_point,
        "payload": {"last_run_ts": analysis_point, "daily_risk_date": daily_risk_date},
    })
    ledger.append_events(cycle_events)
    projection = ledger.replay_account(account_id, initial_balance)
    state = _projection_to_paper_state(projection)

    # Rebuild JSON/JSONL projections atomically from Ledger; neither is authoritative.
    journal_record = {
        "cycle": state["runs"],
        "ts": pd.Timestamp.now(tz="UTC").isoformat(),
        "analysis_point": state["last_run_ts"],
        "equity": round(state["equity"], 2),
        "closed": projection["closed_trades"],
        "opened": state["open_positions"],
        "open_count": len(state["open_positions"]),
        "summary": {key: {field: value.get(field) for field in ("signal", "wait_reason")}
                    for key, value in results.items()},
    }
    journal_tmp = journal_p + ".tmp"
    with open(journal_tmp, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(journal_record, ensure_ascii=False, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(journal_tmp, journal_p)

    state_tmp = state_p + ".tmp"
    with open(state_tmp, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(state_tmp, state_p)

    trades = _load_all_trades(journal_p)
    return {
        "cycle": state["runs"],
        "equity": round(state["equity"], 2),
        "closed_this_cycle": closed,
        "opened_this_cycle": new_signals,
        "open_positions": state["open_positions"],
        "results": results,
        "attribution": attribution(trades, state["equity"]),
    }


def release_paper_risk_lock(out_dir: str, reviewer: str, reason: str,
                            request_id: str, approval_reference: str) -> dict:
    """Release a Paper hard stop only through an explicit, auditable review event."""
    for name, value in (("reviewer", reviewer), ("reason", reason),
                        ("request_id", request_id), ("approval_reference", approval_reference)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")

    ledger = SQLiteLedger(os.path.join(out_dir, "ledger.sqlite3"))
    account_id = "paper"
    initial_balance = ledger.get_meta(f"initial_balance:{account_id}")
    if initial_balance is None:
        raise ValueError("Paper Ledger has not been initialized")

    idempotency_key = f"risk-lock-release:{request_id}"
    prior_event = next(
        (event for event in ledger.read_events(account_id)
         if event.idempotency_key == idempotency_key),
        None,
    )
    current = ledger.replay_account(account_id, initial_balance)
    if not current["risk_lock_active"] and prior_event is None:
        raise ValueError("No active Paper risk lock requires release")

    # Reuse the original event timestamp on retries. The Ledger then accepts an
    # identical request as idempotent and rejects a changed reviewer/reason payload.
    ledger.append_event({
        "account_id": account_id,
        "event_type": "RiskLockReleased",
        "schema_version": 2,
        "idempotency_key": idempotency_key,
        **({"occurred_at": prior_event.occurred_at} if prior_event else {
            "occurred_at": pd.Timestamp.now(tz="UTC").isoformat()
        }),
        "payload": {
            "reviewed_by": reviewer.strip(),
            "reason": reason.strip(),
            "approval_reference": approval_reference.strip(),
            "consecutive_losses": 0,
        },
    })
    projection = ledger.replay_account(account_id, initial_balance)
    state = _projection_to_paper_state(projection)
    state_path = os.path.join(out_dir, "state.json")
    temporary_path = state_path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, state_path)
    return state


def _load_all_trades(journal_p: str) -> list:
    out = []
    if not os.path.exists(journal_p):
        return out
    for line in open(journal_p, encoding="utf-8"):
        rec = json.loads(line)
        for t in rec.get("closed", []):
            item = dict(t)
            item["pnl_usd"] = float(item.get("pnl_usd", 0.0))
            out.append(item)
    return out


def attribution(trades: list, equity: float) -> dict:
    """تفکیک تجمعی عملکرد کاغذی بر اساس نماد/سشن/رژیم + نسبت به حقوق اولیه."""
    out = {"n_trades": len(trades), "by_symbol": {}, "by_session": {}, "by_regime": {}}
    if not trades:
        return out
    for key, name in (("symbol", "by_symbol"), ("session", "by_session"),
                      ("regime", "by_regime")):
        g = {}
        for t in trades:
            d = g.setdefault(t.get(key) or "-", {"n": 0, "wins": 0, "pnl": 0.0})
            d["n"] += 1
            d["wins"] += t["pnl_usd"] > 0
            d["pnl"] = round(d["pnl"] + t["pnl_usd"], 2)
        out[name] = g
    pnl = sum(t["pnl_usd"] for t in trades)
    out["total_pnl_usd"] = round(pnl, 2)
    out["return_pct"] = round(pnl / equity * 100, 2) if equity > 0 else 0.0
    out["win_rate_pct"] = round(sum(t["pnl_usd"] > 0 for t in trades) / len(trades) * 100, 1)
    return out
