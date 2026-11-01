"""فاز ۲ — بک‌تست سیگنال MAS: هر سیگنال عبوری از ۷ گیت = یک معامله.

قواعد شبیه‌سازی (بدون lookahead و بدون خوش‌بینی):
- نقاط تصمیم: هر `step_hours` ساعت (پیش‌فرض ۸ = دو بار در روز کاری) روی کلوز M15.
- ورود: کلوز نقطه تصمیم + اسپرد جهت‌دار (اسپرد معمول نماد از symbols.py).
- خروج: برخورد SL یا TP2 در کندل‌های M15 بعدی؛ اگر هر دو در یک کندل → بدبینانه SL.
- ریسک ثابت ۱٪ از حقوق متغیر (compounding)؛ حجم از pip value واقعی نماد.
- اگر معامله باز باشد، در همان نماد ورود جدید انجام نمی‌شود (قانون R-SYM).
- آمار تفکیکی بر اساس رژیم بازار و سشن (برای attribution).

هشدار صادقانه: داده M15 Dukascopy است؛ اسپرد لحظه‌ای/سلیپیج/سواپ شبیه‌سازی نشده‌اند.
نتایج «توان تاریخی ستاپ‌ها» را نشان می‌دهند، نه تضمین آینده.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .aggregator import analyze_mas
from .contracts import BUY
from .risk_agent import AccountState, RISK_RULES
from .symbols import get_spec, pip_value_usd


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

    @property
    def max_drawdown_pct(self) -> float:
        eq, peak, mdd = self.equity_start, self.equity_start, 0.0
        for t in self.trades:
            eq += t.pnl_usd
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
                 progress: bool = False, feeds=None) -> BTResult:
    """بک‌تست walk-forward: در هر نقطه تصمیم فقط گذشته دیده می‌شود؛ خروج با M15 آینده."""
    spec = get_spec(symbol)
    sp = spec.spread_price() if spread_price is None else spread_price
    df = df15.dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
    t0 = pd.Timestamp(start, tz="UTC") if start else df["time"].iloc[0] + pd.Timedelta(days=250)
    t1 = pd.Timestamp(end, tz="UTC") if end else df["time"].iloc[-1]
    if t0 >= t1:      # بازه خالی (مثلاً تاریخچه کوتاه‌تر از دوره گرم‌شدن)
        return BTResult(symbol=symbol)
    step = pd.Timedelta(hours=step_hours)

    res = BTResult(symbol=symbol)
    equity = res.equity_start
    acct = AccountState(equity=equity, consecutive_losses=0)
    times = pd.date_range(t0, t1, freq=step, tz="UTC")
    res.n_points = len(times)

    pos = None
    # ایندکس‌های عددی برای پیمایش سریع M15 (بدون lookup رشته‌ای)
    t_arr = df["time"].to_numpy()

    for i, now in enumerate(times):
        # ۱) مدیریت معامله باز روی کندل‌های M15 از آخرین ایندکس تا now
        if pos is not None:
            outcome = _advance_position(pos, df, t_arr, now, spec)
            if outcome is not None:
                exit_px, exit_time, last_idx, was_loss = outcome
                d = 1 if pos["direction"] == BUY else -1
                pips = (exit_px - pos["entry"]) / spec.pip * d
                pnl = pips * pip_value_usd(symbol, pos["entry"]) * pos["lots"]
                r = pnl / pos["risk_usd"] if pos["risk_usd"] > 0 else 0.0
                entry_idx = int(df["time"].searchsorted(pos["entry_time"]))
                res.trades.append(BTTrade(symbol, pos["direction"], pos["entry_time"],
                                          exit_time, pos["entry"], pos["sl"], pos["tp2"],
                                          pos["lots"], pnl, r, last_idx - entry_idx,
                                          pos["regime"], pos["session"]))
                equity += pnl
                acct.equity = equity
                acct.consecutive_losses = acct.consecutive_losses + 1 if was_loss else 0
                pos = None
            else:
                continue     # معامله باز است؛ قانون R-SYM ورود جدید را بسته نگه می‌دارد

        # ۲) تحلیل در نقطه تصمیم (فقط داده ≤ now — داخل analyze_mas)
        sig = analyze_mas(df, symbol, now, spread_price=sp, acct=acct, feeds=feeds)
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
        entry = float(row["close"]) + d * sp
        sl, tp2 = float(sig.cand["sl"]), float(sig.cand["tp2"])
        if not (0 < abs(entry - sl) and abs(tp2 - entry) > 0):
            continue
        risk_usd = equity * RISK_RULES["risk_per_trade_pct"] / 100.0
        pips = abs(entry - sl) / spec.pip
        lots = round(risk_usd / max(1e-9, pips * pip_value_usd(symbol, float(row["close"]))), 2)
        pos = {"direction": sig.signal, "entry_time": now, "entry": entry,
               "sl": sl, "tp2": tp2, "lots": lots, "risk_usd": risk_usd,
               "last_idx": j, "regime": sig.market_regime, "session": sig.session}
        res.n_signals += 1
        if progress and (i + 1) % 500 == 0:
            print(f"  [{symbol}] {i + 1}/{res.n_points} نقطه، {res.n_signals} سیگنال، "
                  f"{len(res.trades)} معامله بسته", flush=True)

    if pos is not None:      # بستن معامله بازِ باقیمانده با آخرین کلوز (نشانه‌گذاری‌شده)
        outcome = _advance_position(pos, df, t_arr, df["time"].iloc[-1], spec,
                                    force_exit=True)
        if outcome is not None:
            exit_px, exit_time, last_idx, _ = outcome
            d = 1 if pos["direction"] == BUY else -1
            pnl = (exit_px - pos["entry"]) / spec.pip * d * \
                pip_value_usd(symbol, pos["entry"]) * pos["lots"]
            r = pnl / pos["risk_usd"] if pos["risk_usd"] > 0 else 0.0
            entry_idx = int(df["time"].searchsorted(pos["entry_time"]))
            res.trades.append(BTTrade(symbol, pos["direction"], pos["entry_time"],
                                      exit_time, pos["entry"], pos["sl"], pos["tp2"],
                                      pos["lots"], pnl, r, last_idx - entry_idx,
                                      pos["regime"], pos["session"]))
            equity += pnl

    res.equity_end = equity
    return res


def _advance_position(pos: dict, df: pd.DataFrame, t_arr, now: pd.Timestamp,
                      spec, force_exit: bool = False):
    """معامله باز را روی کندل‌های M15 جلو می‌برد.

    خروجی None = هنوز باز است؛ در غیر این صورت (exit_px, exit_time, last_idx, was_loss).
    برخورد هر دو سطح در یک کندل = SL (بدبینانه). force_exit = کلوز با آخرین کندل موجود.
    """
    d = 1 if pos["direction"] == BUY else -1
    last_idx = pos["last_idx"]
    limit = len(df) - 1 if force_exit else int(df["time"].searchsorted(now, side="right")) - 1
    while last_idx < limit:
        last_idx += 1
        hi, lo = float(df["high"].iloc[last_idx]), float(df["low"].iloc[last_idx])
        hit_sl = (lo <= pos["sl"]) if d > 0 else (hi >= pos["sl"])
        hit_tp = (hi >= pos["tp2"]) if d > 0 else (lo <= pos["tp2"])
        if hit_sl:
            return pos["sl"], df["time"].iloc[last_idx], last_idx, True
        if hit_tp:
            return pos["tp2"], df["time"].iloc[last_idx], last_idx, False
    if force_exit and last_idx >= len(df) - 1:
        return float(df["close"].iloc[-1]), df["time"].iloc[-1], last_idx, False
    if not force_exit:
        pos["last_idx"] = limit
    return None
