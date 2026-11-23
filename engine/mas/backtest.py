"""فاز ۲ — بک‌تست سیگنال MAS: هر سیگنال عبوری از ۷ گیت = یک معامله.

قواعد شبیه‌سازی (بدون lookahead و بدون خوش‌بینی):
- نقاط تصمیم: هر `step_hours` ساعت در زمان بازشدن کندل M15؛ تحلیل فقط کندل‌های بسته‌شده را می‌بیند.
- ورود: Open کندل تصمیم + اسپرد جهت‌دار؛ خروج فقط پس از بسته‌شدن کندل ارزیابی می‌شود.
- خروج: برخورد SL یا TP2 در کندل‌های M15 بعدی؛ اگر هر دو در یک کندل → بدبینانه SL.
- ریسک ثابت ۰٫۵٪ از حقوق متغیر (compounding)؛ حجم از pip value واقعی نماد.
- اگر معامله باز باشد، در همان نماد ورود جدید انجام نمی‌شود (قانون R-SYM).
- آمار تفکیکی بر اساس رژیم بازار و سشن (برای attribution).

تسویه و Equity از SQLite Ledger مشترک با Paper بازسازی می‌شوند؛ خروج اجباری TIME_EXIT است.
هشدار صادقانه: داده M15 Dukascopy است؛ سلیپیج/سواپ/کمیسیون هنوز شبیه‌سازی نشده‌اند.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from datetime import timedelta

import pandas as pd

from .aggregator import analyze_mas
from .contracts import BUY
from .ledger import SQLiteLedger
from .risk_agent import AccountState, RISK_RULES, evaluate_risk
from .symbols import get_spec, realized_pnl_usd


@dataclass
class BTTrade:
    symbol: str
    direction: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    sl: float
    tp2: float
    lots: float
    pnl_usd: float
    r_multiple: float          # سود/زیان بر حسب R (۱R = ریسک اولیه معامله)
    bars_held: int
    regime: str
    session: str
    final_score: float = 50.0    # امتیاز تجمیعی هنگام صدور سیگنال (برای کالیبراسیون Brier)
    confidence: float = 0.0
    exit_reason: str = "UNKNOWN"


@dataclass
class BTResult:
    symbol: str
    n_points: int = 0            # تعداد نقاط تحلیل‌شده
    n_signals: int = 0           # سیگنال‌های عبوری از همه گیت‌ها
    wait_reasons: dict = field(default_factory=dict)   # گیت ردکننده → تعداد
    trades: list = field(default_factory=list)
    equity_start: float = RISK_RULES["equity_default"]
    equity_end: float = RISK_RULES["equity_default"]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_usd > 0)

    @property
    def profit_factor(self) -> float:
        gp = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0)
        gl = -sum(t.pnl_usd for t in self.trades if t.pnl_usd <= 0)
        return gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)

    @property
    def expectancy_r(self) -> float:
        return (sum(t.r_multiple for t in self.trades) / len(self.trades)) if self.trades else 0.0

    equity_curve: list[dict] = field(default_factory=list)

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity_curve:
            peak = max(float(self.equity_curve[0]["equity"]), 1e-12)
            mdd = 0.0
            for point in self.equity_curve:
                current = float(point["equity"])
                peak = max(peak, current)
                mdd = min(mdd, (current - peak) / peak)
            return abs(mdd) * 100
        eq, peak, mdd = self.equity_start, self.equity_start, 0.0
        for trade in self.trades:
            eq += trade.pnl_usd
            peak = max(peak, eq)
            mdd = min(mdd, (eq - peak) / peak)
        return abs(mdd) * 100

    def by(self, key: str) -> dict:
        """تفکیک آمار بر اساس رژیم یا سشن."""
        out = {}
        for t in self.trades:
            k = getattr(t, key) or "-"
            d = out.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0})
            d["n"] += 1
            d["wins"] += t.pnl_usd > 0
            d["pnl"] = round(d["pnl"] + t.pnl_usd, 2)
            d["r_sum"] = round(d["r_sum"] + t.r_multiple, 2)
        return out

    def summary(self) -> dict:
        return {
            "symbol": self.symbol, "points": self.n_points, "signals": self.n_signals,
            "trades": len(self.trades),
            "wins": self.wins, "losses": len(self.trades) - self.wins,
            "win_rate_pct": round(self.wins / len(self.trades) * 100, 1) if self.trades else 0.0,
            "profit_factor": round(self.profit_factor, 2),
            "expectancy_R": round(self.expectancy_r, 3),
            "total_pnl_usd": round(sum(t.pnl_usd for t in self.trades), 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "equity_end": round(self.equity_end, 2),
            "by_regime": self.by("regime"), "by_session": self.by("session"),
            "top_wait_reasons": dict(sorted(self.wait_reasons.items(), key=lambda kv: -kv[1])[:6]),
        }


def backtest_mas(df15: pd.DataFrame, symbol: str,
                 start: str | None = None, end: str | None = None,
                 step_hours: int = 8, spread_price: float | None = None,
                 progress: bool = False, feeds=None, cfg: dict | None = None) -> BTResult:
    """بک‌تست walk-forward: در هر نقطه تصمیم فقط گذشته دیده می‌شود؛ خروج با M15 آینده."""
    spec = get_spec(symbol)
    sp = spec.spread_price() if spread_price is None else spread_price
    df = df15.dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
    t0 = pd.to_datetime(start, utc=True) if start else df["time"].iloc[0] + pd.Timedelta(days=250)
    t1 = pd.to_datetime(end, utc=True) if end else df["time"].iloc[-1]
    if t0 >= t1:      # بازه خالی (مثلاً تاریخچه کوتاه‌تر از دوره گرم‌شدن)
        return BTResult(symbol=symbol)
    step = pd.Timedelta(hours=step_hours)

    res = BTResult(symbol=symbol)
    equity = res.equity_start
    res.equity_curve.append({
        "time": pd.Timestamp(t0).isoformat(),
        "equity": equity, "cash_balance": equity, "unrealized_pnl": 0.0,
    })
    acct = AccountState(equity=equity, consecutive_losses=0)
    ledger = SQLiteLedger(":memory:")
    ledger_account_id = f"backtest:{symbol}"
    ledger_initial_balance = str(res.equity_start)
    times = pd.date_range(t0, t1, freq=step, tz="UTC")
    res.n_points = len(times)

    pos = None
    # ایندکس‌های عددی برای پیمایش سریع M15 (بدون lookup رشته‌ای)
    t_arr = df["time"].to_numpy()

    # بهینه‌سازی: topdown کل تاریخچه یک‌بار پیش‌محاسبه می‌شود؛ در هر نقطه فقط slice ≤ now.
    # پاک‌سازی داده سطر-به-سطر و resample داخل سبدی است → نتیجه دقیقاً برابر محاسبه از صفر است.
    from ..data.providers import BASE_TFS, resample_ohlc
    top_cache = {tf: resample_ohlc(df, tf) for tf in BASE_TFS}

    def record_position_mark(position: dict, mark_idx: int, mark_time) -> None:
        mark_price = float(df["close"].iloc[mark_idx])
        unrealized = realized_pnl_usd(
            symbol, position["entry"], mark_price,
            position["direction"], position["lots"],
        )
        mark_stamp = pd.Timestamp(mark_time).isoformat()
        ledger.append_event({
            "account_id": ledger_account_id,
            "trade_id": position["trade_id"],
            "event_type": "PositionMarked",
            "idempotency_key": f"bt-mark:{position['trade_id']}:{mark_stamp}",
            "occurred_at": mark_stamp,
            "payload": {
                "unrealized_pnl": str(unrealized),
                "mark_price": str(mark_price),
                "mark_time": mark_stamp,
            },
        })
        res.equity_curve.append({
            "time": mark_stamp,
            "equity": equity + unrealized,
            "cash_balance": equity,
            "unrealized_pnl": unrealized,
        })

    for i, now in enumerate(times):
        # ۱) مدیریت معامله باز روی کندل‌های M15 از آخرین ایندکس تا now
        if pos is not None:
            outcome = _advance_position(
                pos, df, t_arr, now, spec,
                on_mark=lambda mark_idx, mark_time: record_position_mark(
                    pos, mark_idx, mark_time
                ),
            )
            if outcome is not None:
                exit_px, exit_time, last_idx, was_loss = outcome
                gross_pnl = realized_pnl_usd(
                    symbol, pos["entry"], exit_px, pos["direction"], pos["lots"]
                )
                exit_ts = pd.Timestamp(exit_time).isoformat()
                exit_day = pd.Timestamp(exit_time).date().isoformat()
                current_projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
                if current_projection["daily_risk_date"] != exit_day:
                    ledger.append_event({
                        "account_id": ledger_account_id, "event_type": "DailyRiskReset",
                        "idempotency_key": f"bt-daily-reset:{exit_day}",
                        "occurred_at": exit_ts, "payload": {"daily_risk_date": exit_day},
                    })
                    current_projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
                exit_date = pd.Timestamp(exit_time).date()
                exit_week = (exit_date - timedelta(days=exit_date.weekday())).isoformat()
                if current_projection["weekly_risk_week"] != exit_week:
                    ledger.append_event({
                        "account_id": ledger_account_id, "event_type": "WeeklyRiskReset",
                        "idempotency_key": f"bt-weekly-reset:{exit_week}",
                        "occurred_at": exit_ts, "payload": {"weekly_risk_week": exit_week},
                    })
                    current_projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
                loss_delta = max(0.0, -gross_pnl) / max(float(current_projection["equity"]), 1e-9) * 100.0
                ledger.append_event({
                    "account_id": ledger_account_id, "trade_id": pos["trade_id"],
                    "event_type": "PositionClosed",
                    "idempotency_key": f"bt-close:{pos['trade_id']}:{exit_ts}",
                    "occurred_at": exit_ts,
                    "payload": {
                        "lots_closed": str(pos["lots"]), "pnl_gross": str(gross_pnl),
                        "commission": "0", "slippage": "0", "swap": "0",
                        "daily_loss_pct_delta": str(loss_delta),
                        "weekly_loss_pct_delta": str(loss_delta),
                        "consecutive_loss_stop": RISK_RULES["consecutive_losses_stop"],
                        "exit_price": str(exit_px), "exit_time": exit_ts,
                        "outcome": "SL" if was_loss else "TP",
                    },
                })
                projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
                pnl = float(projection["closed_trades"][-1]["pnl_usd"])
                r = pnl / pos["risk_usd"] if pos["risk_usd"] > 0 else 0.0
                entry_idx = int(df["time"].searchsorted(pos["entry_time"]))
                res.trades.append(BTTrade(symbol, pos["direction"], pos["entry_time"],
                                          exit_time, pos["entry"], pos["sl"], pos["tp2"],
                                          pos["lots"], pnl, r, last_idx - entry_idx,
                                          pos["regime"], pos["session"],
                                          pos.get("final_score", 50.0),
                                          pos.get("confidence", 0.0),
                                          "SL" if was_loss else "TP"))
                equity = float(projection["equity"])
                res.equity_curve.append({
                    "time": pd.Timestamp(exit_time).isoformat(),
                    "equity": equity, "cash_balance": equity, "unrealized_pnl": 0.0,
                })
                acct.equity = equity
                acct.consecutive_losses = int(projection["consecutive_losses"])
                acct.risk_lock_active = bool(projection["risk_lock_active"])
                acct.daily_loss_pct = float(projection["daily_loss_pct"])
                acct.weekly_loss_pct = float(projection["weekly_loss_pct"])
                pos = None
            else:
                continue     # معامله باز است؛ قانون R-SYM ورود جدید را بسته نگه می‌دارد

        # ۲) تحلیل در نقطه تصمیم (فقط داده ≤ now — داخل analyze_mas)
        sig = analyze_mas(df, symbol, now, spread_price=sp, acct=acct, feeds=feeds,
                          top_cache=top_cache, cfg=cfg)
        if sig.signal == "WAIT" or not sig.cand:
            key = (sig.wait_reason or "WAIT").split(":")[0].strip() or "WAIT"
            res.wait_reasons[key] = res.wait_reasons.get(key, 0) + 1
            continue

        # ۳) ورود: کلوز نقطه تصمیم + اسپرد جهت‌دار
        j = int(df["time"].searchsorted(now, side="right")) - 1
        if j < 0 or t_arr[j] != now:      # فقط وقتی نقطه دقیقاً روی یک کندل M15 است
            continue
        row = df.iloc[j]
        d = 1 if sig.signal == BUY else -1
        entry = float(row["open"]) + d * sp
        sl, tp2 = float(sig.cand["sl"]), float(sig.cand["tp2"])
        if not (0 < abs(entry - sl) and abs(tp2 - entry) > 0):
            continue
        execution_verdict = evaluate_risk(
            symbol, sig.signal, entry, sl, tp2, entry, acct=acct,
            fx_timestamp=pd.Timestamp(now).isoformat(),
        )
        if not execution_verdict.approved or execution_verdict.sizing_result is None:
            key = "FILL_RISK_REJECTED"
            res.wait_reasons[key] = res.wait_reasons.get(key, 0) + 1
            continue
        lots = execution_verdict.lots
        risk_usd = execution_verdict.risk_amount
        trade_id = hashlib.sha256(
            f"{symbol}|{sig.signal}|{pd.Timestamp(now).isoformat()}|{sig.fingerprint()}".encode()
        ).hexdigest()[:32]
        sizing_snapshot = asdict(execution_verdict.sizing_result)
        ledger.append_event({
            "account_id": ledger_account_id, "trade_id": trade_id,
            "event_type": "PositionOpened", "idempotency_key": f"bt-open:{trade_id}",
            "occurred_at": pd.Timestamp(now).isoformat(),
            "payload": {
                "symbol": symbol, "direction": sig.signal, "lots": str(lots),
                "entry_price": str(entry), "sl": str(sl), "tp2": str(tp2),
                "entry_time": pd.Timestamp(now).isoformat(), "risk_usd": str(risk_usd),
                "risk_pct": str(risk_usd / equity * 100.0),
                "final_score": float(sig.final_score), "regime": sig.market_regime,
                "session": sig.session, "sizing_result": sizing_snapshot,
            },
        })
        pos = {"trade_id": trade_id, "direction": sig.signal, "entry_time": now, "entry": entry,
               "sl": sl, "tp2": tp2, "lots": lots, "risk_usd": risk_usd,
               "sizing_result": execution_verdict.sizing_result,
               "last_idx": j - 1, "regime": sig.market_regime, "session": sig.session,
               "final_score": float(sig.final_score), "confidence": float(sig.confidence)}
        res.n_signals += 1
        if progress and (i + 1) % 500 == 0:
            print(f"  [{symbol}] {i + 1}/{res.n_points} نقطه، {res.n_signals} سیگنال، "
                  f"{len(res.trades)} معامله بسته", flush=True)

    if pos is not None:      # بستن معامله بازِ باقیمانده با آخرین کلوز (نشانه‌گذاری‌شده)
        outcome = _advance_position(
            pos, df, t_arr, df["time"].iloc[-1], spec,
            force_exit=True,
            on_mark=lambda mark_idx, mark_time: record_position_mark(
                pos, mark_idx, mark_time
            ),
        )
        if outcome is not None:
            exit_px, exit_time, last_idx, _ = outcome
            gross_pnl = realized_pnl_usd(
                symbol, pos["entry"], exit_px, pos["direction"], pos["lots"]
            )
            exit_ts = pd.Timestamp(exit_time).isoformat()
            exit_day = pd.Timestamp(exit_time).date().isoformat()
            current_projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
            if current_projection["daily_risk_date"] != exit_day:
                ledger.append_event({
                    "account_id": ledger_account_id, "event_type": "DailyRiskReset",
                    "idempotency_key": f"bt-daily-reset:{exit_day}",
                    "occurred_at": exit_ts, "payload": {"daily_risk_date": exit_day},
                })
                current_projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
            exit_date = pd.Timestamp(exit_time).date()
            exit_week = (exit_date - timedelta(days=exit_date.weekday())).isoformat()
            if current_projection["weekly_risk_week"] != exit_week:
                ledger.append_event({
                    "account_id": ledger_account_id, "event_type": "WeeklyRiskReset",
                    "idempotency_key": f"bt-weekly-reset:{exit_week}",
                    "occurred_at": exit_ts, "payload": {"weekly_risk_week": exit_week},
                })
                current_projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
            loss_delta = max(0.0, -gross_pnl) / max(float(current_projection["equity"]), 1e-9) * 100.0
            ledger.append_event({
                "account_id": ledger_account_id, "trade_id": pos["trade_id"],
                "event_type": "PositionClosed",
                "idempotency_key": f"bt-close:{pos['trade_id']}:{exit_ts}",
                "occurred_at": exit_ts,
                "payload": {
                    "lots_closed": str(pos["lots"]), "pnl_gross": str(gross_pnl),
                    "commission": "0", "slippage": "0", "swap": "0",
                    "daily_loss_pct_delta": str(loss_delta),
                    "weekly_loss_pct_delta": str(loss_delta),
                    "consecutive_loss_stop": RISK_RULES["consecutive_losses_stop"],
                    "exit_price": str(exit_px), "exit_time": exit_ts,
                    "outcome": "TIME_EXIT",
                },
            })
            projection = ledger.replay_account(ledger_account_id, ledger_initial_balance)
            pnl = float(projection["closed_trades"][-1]["pnl_usd"])
            r = pnl / pos["risk_usd"] if pos["risk_usd"] > 0 else 0.0
            entry_idx = int(df["time"].searchsorted(pos["entry_time"]))
            res.trades.append(BTTrade(symbol, pos["direction"], pos["entry_time"],
                                      exit_time, pos["entry"], pos["sl"], pos["tp2"],
                                      pos["lots"], pnl, r, last_idx - entry_idx,
                                      pos["regime"], pos["session"],
                                      pos.get("final_score", 50.0),
                                      pos.get("confidence", 0.0), "TIME_EXIT"))
            equity = float(projection["equity"])
            res.equity_curve.append({
                "time": pd.Timestamp(exit_time).isoformat(),
                "equity": equity, "cash_balance": equity, "unrealized_pnl": 0.0,
            })

    res.equity_end = equity
    ledger.close()
    return res


def _advance_position(pos: dict, df: pd.DataFrame, t_arr, now: pd.Timestamp,
                      spec, force_exit: bool = False, on_mark=None):
    """معامله باز را روی کندل‌های M15 جلو می‌برد.

    خروجی None = هنوز باز است؛ در غیر این صورت (exit_px, exit_time, last_idx, was_loss)، که was_loss=None یعنی TIME_EXIT.
    برخورد هر دو سطح در یک کندل = SL (بدبینانه). force_exit = کلوز با آخرین کندل موجود.
    """
    d = 1 if pos["direction"] == BUY else -1
    last_idx = pos["last_idx"]
    # OHLC belongs to the interval [time, time + 15m). Never inspect a
    # candle until its close timestamp is <= now. At dataset exhaustion the
    # final row is still an open candle (there is no following close event),
    # so terminal marking is limited to the penultimate row at most.
    close_times = df["time"] + pd.Timedelta(minutes=15)
    limit = (len(df) - 2 if force_exit else
             int(close_times.searchsorted(now, side="right")) - 1)
    while last_idx < limit:
        last_idx += 1
        hi, lo = float(df["high"].iloc[last_idx]), float(df["low"].iloc[last_idx])
        hit_sl = (lo <= pos["sl"]) if d > 0 else (hi >= pos["sl"])
        hit_tp = (hi >= pos["tp2"]) if d > 0 else (lo <= pos["tp2"])
        if hit_sl:
            # A stop crossed by the candle open is filled at the worse opening
            # price; otherwise the modeled stop price is used. This is a
            # conservative OHLC gap model and deliberately keeps SL priority
            # when both stop and target are touched in the same candle.
            exit_price = pos["sl"]
            if "open" in df.columns:
                bar_open = float(df["open"].iloc[last_idx])
                if (d > 0 and bar_open < pos["sl"]) or (d < 0 and bar_open > pos["sl"]):
                    exit_price = bar_open
            return exit_price, df["time"].iloc[last_idx] + pd.Timedelta(minutes=15), last_idx, True
        if hit_tp:
            return pos["tp2"], df["time"].iloc[last_idx] + pd.Timedelta(minutes=15), last_idx, False
        if on_mark is not None:
            on_mark(last_idx, close_times.iloc[last_idx])
    if force_exit:
        # Mark to the latest fully closed candle only. If no post-entry
        # closed candle exists, leave the position open rather than inventing
        # a terminal price from an unfinished bar.
        if limit >= 0 and last_idx >= limit:
            mark_idx = min(last_idx, limit)
            return float(df["close"].iloc[mark_idx]), close_times.iloc[mark_idx], mark_idx, None
        if limit >= 0 and last_idx < limit:
            return float(df["close"].iloc[limit]), close_times.iloc[limit], limit, None
    else:
        # Do not move the cursor backwards when `now` precedes the first
        # eligible close timestamp.
        pos["last_idx"] = max(last_idx, limit)
    return None
