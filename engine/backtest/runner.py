"""
بک‌تستر کندل‌به‌کندل: همان `analyze` زنده را صدا می‌زند.

قواعد واقع‌گرایانه:
- تصمیم فقط با کندل‌های بسته‌شده تا لحظه‌ی now (TopDownView)
- ورود با اسپرد (BUY روی ask، SELL روی bid)، خروج SL/TP روی سمت مخالف
- اگر SL و TP در یک کندل لمس شوند، SL حساب می‌شود (بدبینانه)
- EXPIRED با قیمت بازار (mark-to-market) بسته می‌شود، نه با R=0
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd

from ..core.config import EngineConfig, spread_price
from ..data.providers import TopDownView
from ..strategy.smc_topdown import Decision, analyze


def check_exit(side: int, sl: float, tp: float, high: float, low: float,
               half: float, open_price: Optional[float] = None):
    """Return conservative executable fill; opening gaps through SL fill at the worse open."""
    if open_price is not None:
        open_exec = open_price - side * half
        if side > 0:
            if open_exec <= sl:
                return "SL", open_exec
            if open_exec >= tp:
                return "TP", tp  # conservative limit fill; do not assume price improvement
        else:
            if open_exec >= sl:
                return "SL", open_exec
            if open_exec <= tp:
                return "TP", tp

    if side > 0:   # خروج روی bid = mid - half
        if low - half <= sl:
            return "SL", sl
        if high - half >= tp:
            return "TP", tp
    else:          # خروج روی ask = mid + half
        if high + half >= sl:
            return "SL", sl
        if low + half <= tp:
            return "TP", tp
    return None, None


@dataclass
class _Pos:
    side: int
    open_time: pd.Timestamp
    open_idx: int
    entry: float
    sl: float
    tp: float
    dec: Decision


def _close_trade(symbol, pos, status, exit_price, t, cfg):
    risk = (pos.entry - pos.sl) * pos.side
    r = (exit_price - pos.entry) * pos.side / risk
    return {
        "symbol": symbol, "side": "BUY" if pos.side > 0 else "SELL",
        "open_time": pos.open_time, "close_time": t,
        "entry": pos.entry, "sl": pos.sl, "tp": pos.tp, "exit": exit_price,
        "status": status, "r": round(float(r), 4), "score": pos.dec.score,
        "features": pos.dec.features, "config_hash": cfg.hash(),
    }


def run_backtest(symbol: str, data: dict, cfg: EngineConfig,
                 start: Optional[str] = None, end: Optional[str] = None,
                 analyze_fn: Optional[Callable[[dict, EngineConfig, pd.Timestamp], Decision]] = None) -> list:
    """data: {tf: DataFrame} برای یک نماد. خروجی: لیست معامله‌ها (dict).

    analyze_fn permits CLI strategy selection while preserving the SMC default.
    """
    analyzer = analyze if analyze_fn is None else analyze_fn
    view = TopDownView(data)
    df15 = data["15m"]
    half = spread_price(symbol) / 2.0
    times = df15["time"]
    op, hi, lo, cl = (df15[k].to_numpy() for k in ("open", "high", "low", "close"))

    # utc=True safely handles both naive bounds and ISO timestamps that already carry a timezone.
    bar = pd.Timedelta(minutes=15)
    t0 = pd.to_datetime(start, utc=True) if start else times.iloc[0]
    # Loop timestamps are candle CLOSE times; include the final candle by default.
    t1 = pd.to_datetime(end, utc=True) if end else times.iloc[-1] + bar
    hold = pd.Timedelta(hours=cfg.max_hold_hours)
    cooldown = pd.Timedelta(hours=cfg.cooldown_hours)

    trades, pos, last_signal = [], None, {}
    last_i = None
    for i in range(len(df15)):
        t_open = times.iloc[i]
        now = t_open + bar                      # لحظه‌ی بسته‌شدن کندل i
        if now < t0:
            continue
        if now > t1:
            break
        last_i = i

        if pos is not None and i > pos.open_idx:
            status, px = check_exit(pos.side, pos.sl, pos.tp, hi[i], lo[i], half, op[i])
            if status is None and now - pos.open_time >= hold:
                status, px = "EXPIRED", cl[i] - pos.side * half
            if status:
                trades.append(_close_trade(symbol, pos, status, px, now, cfg))
                pos = None

        if pos is not None or i % cfg.scan_every_bars:
            continue
        if now - last_signal.get("t", now - cooldown - bar) < cooldown:
            continue

        dec = analyzer(view.at(now), cfg, now)
        if dec.direction is None:
            continue
        side = 1 if dec.direction == "BUY" else -1
        entry = dec.entry + side * half         # BUY روی ask، SELL روی bid
        if (entry - dec.sl) * side <= 0:
            continue
        pos = _Pos(side, now, i, entry, dec.sl, dec.tp, dec)
        last_signal["t"] = now

    if pos is not None and last_i is not None:  # پوزیشن باز آخر بازه: mark-to-market
        trades.append(_close_trade(
            symbol, pos, "EOD",
            cl[last_i] - pos.side * half,
            times.iloc[last_i] + bar,           # لحظه‌ی بسته‌شدن آخرین کندل بازه
            cfg,
        ))
    return trades


# ---------------------------------------------------------------- آمار
def wilson(wins: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def summarize(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    ts = sorted(trades, key=lambda t: t["close_time"])
    r = np.array([t["r"] for t in ts], dtype=float)
    n, wins = len(r), int((r > 0).sum())
    gain, loss = r[r > 0].sum(), -r[r < 0].sum()
    cum = np.cumsum(r)
    peak = np.maximum.accumulate(np.r_[0.0, cum])[1:]
    mean = float(r.mean())
    se = float(r.std(ddof=1) / math.sqrt(n)) if n > 1 else float("inf")
    lo, hi = wilson(wins, n)
    avg_win = float(r[r > 0].mean()) if wins else 0.0
    avg_loss = float(-r[r < 0].mean()) if (r < 0).any() else 0.0
    be_wr = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) else 0.0
    return {
        "n": n, "win_rate": round(wins / n, 4),
        "win_rate_ci95": (round(lo, 3), round(hi, 3)),
        "breakeven_win_rate": round(be_wr, 3),
        "expectancy_r": round(mean, 4),
        "expectancy_ci95": (round(mean - 1.96 * se, 3), round(mean + 1.96 * se, 3)),
        "profit_factor": round(float(gain / loss), 2) if loss > 0 else 99.0,
        "total_r": round(float(cum[-1]), 2),
        "max_drawdown_r": round(float((peak - cum).max()), 2),
        "expired": sum(t["status"] in ("EXPIRED", "EOD") for t in ts),
        "by_symbol": {s: round(sum(t["r"] for t in ts if t["symbol"] == s), 2)
                      for s in sorted({t["symbol"] for t in ts})},
    }


def evaluate_gate(s: dict, min_trades: int = 100, max_dd_r: float = 10.0) -> dict:
    """
    آماره‌سنجی استراتژی، نه تأیید آمادگی برای حساب واقعی.
    هزینه‌ی اسپرد با تخمین ثابت لحاظ شده؛ کمیسیون بروکر و لغزش عمومی قیمت
    مدل نشده‌اند، پس حتی ready=True به‌تنهایی مجوز اجرای زنده نیست.
    """
    warning = (
        "Statistical gate only: estimated fixed spread is included, but broker "
        "commission and general slippage are not modeled. Not live-trading approval."
    )
    live_blockers = [
        "broker commission is not modeled",
        "general slippage is not modeled",
        "broker-specific spread and execution behavior are not validated",
    ]
    if s.get("n", 0) == 0:
        return {
            "ready": False,
            "statistical_pass": False,
            "live_ready": False,
            "live_readiness_blockers": live_blockers,
            "checks": {"has_trades": False},
            "execution_model_warning": warning,
        }
    checks = {
        f"n>={min_trades}": s["n"] >= min_trades,
        "expectancy_ci_low>0": s["expectancy_ci95"][0] > 0,
        "profit_factor>=1.3": s["profit_factor"] >= 1.3,
        f"max_dd_r<={max_dd_r}": s["max_drawdown_r"] <= max_dd_r,
    }
    statistical_pass = all(checks.values())
    return {
        # ready is retained for CLI compatibility and means statistical checks only.
        "ready": statistical_pass,
        "statistical_pass": statistical_pass,
        # Never imply live readiness while execution costs are incomplete.
        "live_ready": False,
        "live_readiness_blockers": live_blockers,
        "checks": checks,
        "execution_model_warning": warning,
    }
