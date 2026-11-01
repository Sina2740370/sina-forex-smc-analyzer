"""فاز ۵ — موتور اجرای کاغذی (Paper Trading) با ژورنال و attribution.

چرخه هر اجرا (قابل زمان‌بندی خارجی؛ مثلاً cron هر ۴ ساعت):
1. بارگذاری وضعیت (حقوق، معاملات باز، دنباله ضرر) از out_mas/paper/state.json
2. تسویه معاملات باز روی داده جدید (SL/TP با قاعده بدبینانهٔ بک‌تست)
3. تحلیل چندجفتی با AccountState واقعی (ریسک مشترک، قواعد A8)
4. سیگنال‌های تأییدشده → معامله کاغذی جدید (ورود = کلوز سیگنال + اسپرد؛ همسان بک‌تست)
5. ژورنال JSONL (append-only) + attribution تجمعی (نماد/سشن/رژیم) + منحنی حقوق

صادقانه: اجرا «کاغذی» است؛ اسپرد لحظه‌ای/سلیپیج/سواپ ندارد و ورود با کلوز کندل
سیگنال مدل شده است. داده M15 Dukascopy باید دستی تازه شود (اسکریپت آپدیت داده موجود).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

import pandas as pd

from .aggregator import analyze_mas
from .backtest import _advance_position
from .contracts import BUY
from .portfolio import _csv_path
from .risk_agent import AccountState, RISK_RULES
from .symbols import get_spec, pip_value_usd


def run_paper(csv_dir: str, symbols: list, out_dir: str = "out_mas/paper",
              equity: float | None = None, feeds=None, now: pd.Timestamp | None = None,
              min_gap_hours: float = 1.0) -> dict:
    """یک چرخه اجرای کاغذی. خروجی: خلاصه چرخه (برای ژورنال و گزارش CLI)."""
    os.makedirs(out_dir, exist_ok=True)
    state_p = os.path.join(out_dir, "state.json")
    journal_p = os.path.join(out_dir, "journal.jsonl")
    state = {"equity": equity or RISK_RULES["equity_default"], "consecutive_losses": 0,
             "daily_loss_pct": 0.0, "open_positions": [], "runs": 0}
    if os.path.exists(state_p):
        state.update(json.load(open(state_p)))

    spec_of = {s: get_spec(s) for s in symbols}
    dfs, t_now = {}, {}
    from ..data.providers import load_mt5_csv
    for sym in symbols:
        p = _csv_path(csv_dir, sym)
        if not p:
            continue
        df = load_mt5_csv(p).dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
        dfs[sym] = df
        t_now[sym] = pd.Timestamp(now, tz="UTC") if now else df["time"].iloc[-1]

    # --- ۱) تسویه معاملات باز ---
    closed, still_open = [], []
    for pos in state["open_positions"]:
        sym = pos["symbol"]
        if sym not in dfs:
            still_open.append(pos)
            continue
        df, spec = dfs[sym], spec_of[sym]
        pos = {**pos, "last_idx": int(df["time"].searchsorted(pd.Timestamp(pos["entry_time"])))}
        outcome = _advance_position(pos, df, df["time"].to_numpy(), t_now[sym], spec)
        if outcome is None:
            still_open.append(pos)
            continue
        exit_px, exit_time, _idx, was_loss = outcome
        d = 1 if pos["direction"] == BUY else -1
        pnl = (exit_px - pos["entry"]) / spec.pip * d * \
            pip_value_usd(sym, pos["entry"]) * pos["lots"]
        state["equity"] += pnl
        if was_loss:
            state["consecutive_losses"] += 1
            state["daily_loss_pct"] = round(state["daily_loss_pct"]
                                            + pos["risk_usd"] / state["equity"] * 100, 3)
        else:
            state["consecutive_losses"] = 0
        closed.append({**pos, "exit": exit_px, "exit_time": str(exit_time),
                       "pnl_usd": round(pnl, 2), "outcome": "SL" if was_loss else "TP"})

    # --- ۲) تحلیل با وضعیت واقعی حساب ---
    acct = AccountState(equity=state["equity"],
                        open_trades=len(still_open),
                        open_risk_pct=sum(p.get("risk_pct", 0.0) for p in still_open),
                        daily_loss_pct=state["daily_loss_pct"],
                        consecutive_losses=state["consecutive_losses"],
                        per_symbol_open={p["symbol"]: 1 for p in still_open})
    new_signals, results = [], {}
    last_run = state.get("last_run_ts")
    for sym in symbols:
        if sym not in dfs:
            results[sym] = {"error": "داده پیدا نشد"}
            continue
        if last_run and (t_now[sym] - pd.Timestamp(last_run)) < pd.Timedelta(hours=min_gap_hours):
            results[sym] = {"skipped": "فاصله کم از اجرای قبلی"}
            continue
        sig = analyze_mas(dfs[sym], sym, t_now[sym], acct=acct, feeds=feeds)
        results[sym] = sig.to_json(include_reports=False)
        if sig.signal in ("BUY", "SELL") and sig.cand and sym not in acct.per_symbol_open:
            j = int(dfs[sym]["time"].searchsorted(t_now[sym], side="right")) - 1
            row = dfs[sym].iloc[j]
            spec = spec_of[sym]
            d = 1 if sig.signal == BUY else -1
            entry = float(row["close"]) + d * spec.spread_price()
            risk_usd = state["equity"] * RISK_RULES["risk_per_trade_pct"] / 100.0
            pips = abs(entry - float(sig.cand["sl"])) / spec.pip
            lots = round(risk_usd / max(1e-9, pips * pip_value_usd(sym, entry)), 2)
            new_signals.append({
                "symbol": sym, "direction": sig.signal, "entry_time": str(t_now[sym]),
                "entry": entry, "sl": float(sig.cand["sl"]), "tp1": float(sig.cand["tp1"]),
                "tp2": float(sig.cand["tp2"]), "rr": float(sig.cand["rr"]),
                "lots": lots, "risk_usd": round(risk_usd, 2),
                "risk_pct": round(risk_usd / state["equity"] * 100, 3),
                "final_score": round(float(sig.final_score), 1),
                "regime": sig.market_regime, "session": sig.session})
            acct.open_trades += 1
            acct.open_risk_pct = round(acct.open_risk_pct
                                       + risk_usd / state["equity"] * 100, 2)
            acct.per_symbol_open[sym] = 1

    # --- ۳) ژورنال + وضعیت ---
    state["open_positions"] = still_open + [
        {"symbol": s["symbol"], "direction": s["direction"], "entry_time": s["entry_time"],
         "entry": s["entry"], "sl": s["sl"], "tp2": s["tp2"], "lots": s["lots"],
         "risk_usd": s["risk_usd"], "risk_pct": s["risk_pct"],
         "final_score": s["final_score"], "regime": s["regime"], "session": s["session"]}
        for s in new_signals]
    state["runs"] = state.get("runs", 0) + 1
    state["last_run_ts"] = str(max(t_now.values())) if t_now else None
    state.pop("open_positions_hist", None)
    with open(journal_p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"cycle": state["runs"], "ts": pd.Timestamp.now(tz="UTC").isoformat(),
                            "analysis_point": state["last_run_ts"],
                            "equity": round(state["equity"], 2),
                            "closed": closed, "opened": new_signals,
                            "open_count": len(still_open) + len(new_signals),
                            "summary": {s: {k: r.get(k) for k in ("signal", "wait_reason")}
                                        for s, r in results.items()}},
                           ensure_ascii=False, default=str) + "\n")
    json.dump(state, open(state_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    trades = _load_all_trades(journal_p)
    return {"cycle": state["runs"], "equity": round(state["equity"], 2),
            "closed_this_cycle": closed, "opened_this_cycle": new_signals,
            "open_positions": state["open_positions"], "results": results,
            "attribution": attribution(trades, state["equity"])}


def _load_all_trades(journal_p: str) -> list:
    out = []
    if not os.path.exists(journal_p):
        return out
    for line in open(journal_p, encoding="utf-8"):
        rec = json.loads(line)
        for t in rec.get("closed", []):
            out.append(t)
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
