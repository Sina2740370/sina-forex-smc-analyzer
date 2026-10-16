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
from typing import Optional

import numpy as np
import pandas as pd

from .core.config import EngineConfig, spread_price
from .data.providers import TopDownView
from .strategy.smc_topdown import Decision, analyze


def check_exit(
    side: int, sl: float, tp: float, high: float, low: float, half: float,
    open_price: Optional[float] = None,
):
    """خروج با سمت اجرایی قیمت؛ SL اولویت دارد و گپ نامطلوب با open پر می‌شود."""
    if open_price is not None:
        if side > 0:
            bid_open = open_price - half
            if bid_open <= sl:
                return "SL", bid_open
            if bid_open >= tp:
                return "TP", tp
        else:
            ask_open = open_price + half
            if ask_open >= sl:
                return "SL", ask_open
            if ask_open <= tp:
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
        "entry": pos.entry, "signal_entry": pos.dec.entry, "sl": pos.sl, "tp": pos.tp, "exit": exit_price,
        "status": status, "r": round(float(r), 4), "score": pos.dec.score,
        "features": pos.dec.features, "config_hash": cfg.hash(),
    }


def run_backtest(symbol: str, data: dict, cfg: EngineConfig,
                 start: Optional[str] = None, end: Optional[str] = None,
                 analyze_fn=None) -> list:
    """data: {tf: DataFrame} برای یک نماد. خروجی: لیست معامله‌ها (dict).
    analyze_fn: تابع (view_dict, cfg, now) -> Decision؛ پیش‌فرض همان analyze (SMC)."""
    analyze_fn = analyze_fn or analyze
    view = TopDownView(data)
    # Use the validated, numeric, UTC-normalized frames from TopDownView.
    df15 = view.data["15m"]
    half = spread_price(symbol) / 2.0
    times = df15["time"]
    hi, lo, cl = (df15[k].to_numpy() for k in ("high", "low", "close"))

    def _utc_bound(value):
        ts = pd.Timestamp(value)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    t0 = _utc_bound(start) if start else times.iloc[0]
    t1 = _utc_bound(end) if end else times.iloc[-1] + pd.Timedelta(minutes=15)
    bar = pd.Timedelta(minutes=15)
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

        if pos is not None and i >= pos.open_idx:
            deadline = pos.open_time + hold
            # Expiry has priority on a candle that reaches the holding deadline:
            # OHLC cannot establish whether an intrabar TP/SL happened before expiry.
            if t_open >= deadline:
                if t_open - deadline <= bar:
                    expiry_mid = float(df15["open"].iloc[i])
                else:
                    prior_i = int(times.searchsorted(deadline - bar, side="right") - 1)
                    if prior_i >= 0 and times.iloc[prior_i] + bar <= deadline:
                        expiry_mid = float(cl[prior_i])
                    else:
                        expiry_mid = float(df15["open"].iloc[i])
                status, px = "EXPIRED", expiry_mid - pos.side * half
            elif now >= deadline:
                status, px = "EXPIRED", cl[i] - pos.side * half
            else:
                status, px = check_exit(
                    pos.side, pos.sl, pos.tp, hi[i], lo[i], half,
                    float(df15["open"].iloc[i])
                )
            if status:
                trades.append(_close_trade(symbol, pos, status, px, now, cfg))
                pos = None

        if pos is not None or i % cfg.scan_every_bars:
            continue
        if now - last_signal.get("t", now - cooldown - bar) < cooldown:
            continue

        dec = analyze_fn(view.at(now), cfg, now)
        if dec.direction is None:
            continue
        if dec.entry is None or dec.sl is None or dec.tp is None:
            continue
        side = 1 if dec.direction == "BUY" else -1
        # تصمیم در close کندل i معلوم می‌شود؛ ورود زودتر از open کندل بعدی مجاز نیست.
        next_i = i + 1
        if next_i >= len(df15):
            continue
        next_open_time = times.iloc[next_i]
        if next_open_time + bar > t1:
            continue

        signal_entry = float(dec.entry)
        stop_distance = (signal_entry - float(dec.sl)) * side
        if not np.isfinite(stop_distance) or stop_distance <= 0:
            continue
        next_open = float(df15["open"].iloc[next_i])
        # اگر بازار از محدوده‌ی قابل قبول دور شده، سیگنال قدیمی را دنبال نکن.
        if abs(next_open - signal_entry) > cfg.max_entry_slip_r * stop_distance:
            continue

        entry = next_open + side * half  # BUY روی ask، SELL روی bid
        if (entry - dec.sl) * side <= 0 or (dec.tp - entry) * side <= 0:
            continue

        pos = _Pos(side, next_open_time, next_i, entry, dec.sl, dec.tp, dec)
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
    معیار «آماده برای حساب واقعی» بر پایه‌ی آمار، نه Win Rate خام:
    حد پایین بازه‌ی اطمینان امید ریاضی باید مثبت باشد (یعنی لبه از نویز جداست).
    """
    if s.get("n", 0) == 0:
        return {"ready": False, "checks": {"has_trades": False}}
    checks = {
        f"n>={min_trades}": s["n"] >= min_trades,
        "expectancy_ci_low>0": s["expectancy_ci95"][0] > 0,
        "profit_factor>=1.3": s["profit_factor"] >= 1.3,
        f"max_dd_r<={max_dd_r}": s["max_drawdown_r"] <= max_dd_r,
    }
    return {"ready": all(checks.values()), "checks": checks}
