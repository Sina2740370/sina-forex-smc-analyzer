"""
ابزارهای تشخیص «آیا لبه واقعی است؟»

1) random_baseline : همان تعداد معامله با ورود/جهت تصادفی و همان فاصله‌ی SL و RR
                     ← اگر استراتژی از رندوم بهتر نباشد، لبه‌ای نیست (p-value)
2) feature_table   : میانگین R معامله‌های دارای/فاقد هر ویژگی ← کدام ویژگی سهم دارد
3) fold_stability  : پایداری نتیجه در بازه‌های زمانی پشت‌سرهم
4) walk_forward    : انتخاب بهترین آستانه روی بازه‌ی train و سنجش روی بازه‌ی test بعدی
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from ..core.config import EngineConfig, spread_price
from .runner import check_exit, run_backtest, summarize


# ---------------------------------------------------------------- ۱) رندوم
def _simulate(
    side, signal_mid, risk_dist, rr, opens, highs, lows, closes, half, max_entry_slip_r
):
    """Random trade using the same next-open, spread, gap, SL/TP and expiry rules as runner."""
    if risk_dist <= 0 or len(opens) == 0:
        return None
    first_open = float(opens[0])
    if abs(first_open - signal_mid) > max_entry_slip_r * risk_dist:
        return None

    sl = signal_mid - side * risk_dist
    tp = signal_mid + side * risk_dist * rr
    entry = first_open + side * half
    if (entry - sl) * side <= 0 or (tp - entry) * side <= 0:
        return None
    risk = (entry - sl) * side

    for i in range(len(opens)):
        status, exit_px = check_exit(
            side, sl, tp, float(highs[i]), float(lows[i]), half, float(opens[i])
        )
        if status is not None:
            return (float(exit_px) - entry) * side / risk, i + 1

    exit_px = float(closes[-1]) - side * half
    return (exit_px - entry) * side / risk, len(opens)


def random_baseline(symbol: str, df15: pd.DataFrame, trades: list, cfg: EngineConfig,
                    n_sims: int = 300, seed: int = 1) -> dict:
    ts = [
        t for t in trades
        if t["symbol"] == symbol and t.get("signal_entry") is not None
    ]
    if len(ts) < 5:
        return {"n": len(ts), "note": "تعداد معامله برای مقایسه کافی نیست"}
    if n_sims < 1:
        raise ValueError("n_sims باید حداقل 1 باشد")

    half = spread_price(symbol) / 2.0
    times = df15["time"]
    opens, hi, lo, cl = (
        df15[k].to_numpy() for k in ("open", "high", "low", "close")
    )
    nb = cfg.max_hold_hours * 4
    bar = pd.Timedelta(minutes=15)
    j = [int(times.searchsorted(t["open_time"] - bar)) for t in ts]
    j_lo = max(0, min(j))
    j_hi = min(max(j), len(df15) - nb - 1)
    if j_hi < j_lo or nb < 1:
        return {"n": len(ts), "note": "داده‌ی کافی برای ساخت baseline هم‌شرایط وجود ندارد"}

    risks = np.array([
        abs(float(t["signal_entry"]) - float(t["sl"])) for t in ts
    ])
    real = float(np.mean([t["r"] for t in ts]))
    rng = np.random.default_rng(seed)
    sims = np.empty(n_sims)
    invalid_entries = 0

    for sim_i in range(n_sims):
        rs = []
        next_signal_i = j_lo
        for risk_dist in risks:
            outcome = None
            chosen_k = None
            for _ in range(100):
                lo_k = max(j_lo, next_signal_i)
                if lo_k > j_hi:
                    break
                k = int(rng.integers(lo_k, j_hi + 1))
                side = 1 if rng.random() < 0.5 else -1
                outcome = _simulate(
                    side, float(cl[k]), float(risk_dist), cfg.risk_reward,
                    opens[k + 1:k + 1 + nb],
                    hi[k + 1:k + 1 + nb],
                    lo[k + 1:k + 1 + nb],
                    cl[k + 1:k + 1 + nb],
                    half, cfg.max_entry_slip_r,
                )
                if outcome is not None:
                    chosen_k = k
                    break
            if outcome is None or chosen_k is None:
                invalid_entries += 1
                rs.append(0.0)
                next_signal_i = min(j_hi + 1, next_signal_i + 1)
                continue
            r_value, held_bars = outcome
            rs.append(float(r_value))
            # Random trades cannot overlap; next decision occurs at/after exit-bar close.
            next_signal_i = chosen_k + held_bars
        sims[sim_i] = float(np.mean(rs))

    return {
        "n": len(ts), "real_expectancy_r": round(real, 4),
        "random_mean_r": round(float(sims.mean()), 4),
        "random_p95_r": round(float(np.percentile(sims, 95)), 4),
        "p_value": round((int((sims >= real).sum()) + 1) / (n_sims + 1), 4),
        "invalid_random_entries": invalid_entries,
    }


# ---------------------------------------------------------------- ۲) ویژگی‌ها
def feature_table(trades: list) -> pd.DataFrame:
    names = sorted({k for t in trades for k in t["features"]})
    rows = []
    for name in names:
        w = [t["r"] for t in trades if name in t["features"]]
        wo = [t["r"] for t in trades if name not in t["features"]]
        rows.append({
            "feature": name,
            "n_with": len(w), "exp_with": round(float(np.mean(w)), 3) if w else None,
            "wr_with": round(float(np.mean([x > 0 for x in w])), 3) if w else None,
            "n_without": len(wo), "exp_without": round(float(np.mean(wo)), 3) if wo else None,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- ۳) پایداری
def fold_stability(trades: list, k: int = 4) -> list:
    ts = sorted(trades, key=lambda t: t["close_time"])
    if len(ts) < k * 3:
        return []
    out = []
    for i, part in enumerate(np.array_split(np.arange(len(ts)), k)):
        r = np.array([ts[x]["r"] for x in part])
        out.append({"fold": i + 1, "from": str(ts[part[0]]["close_time"])[:10],
                    "n": len(r), "expectancy_r": round(float(r.mean()), 3),
                    "total_r": round(float(r.sum()), 2)})
    return out


# ---------------------------------------------------------------- ۴) walk-forward
def walk_forward(symbol: str, data: dict, base: EngineConfig, grid=(0.5, 0.6, 0.7),
                 train_days: int = 180, test_days: int = 60, warmup_days: int = 150,
                 min_train_trades: int = 10) -> dict:
    """
    هر کانفیگ یک‌بار روی کل دیتا اجرا می‌شود و معامله‌ها بر اساس open_time به
    بازه‌های train/test تقسیم می‌شوند (تقریب؛ برای غربال overfit کافی است).
    """
    runs = {ms: run_backtest(symbol, data, replace(base, min_score=ms)) for ms in grid}
    t0 = data["15m"]["time"].iloc[0] + pd.Timedelta(days=warmup_days)
    t_end = data["15m"]["time"].iloc[-1]
    windows, oos = [], []
    s = t0
    while s + pd.Timedelta(days=train_days + test_days) <= t_end:
        a, b = s + pd.Timedelta(days=train_days), s + pd.Timedelta(days=train_days + test_days)
        best, best_exp = None, -1e9
        for ms, tr in runs.items():
            # فقط معامله‌هایی که تا انتهای train واقعاً بسته شده‌اند،
            # می‌توانند در انتخاب پارامتر train اثر بگذارند.
            tt = [
                t for t in tr
                if s <= t["open_time"] < a and t["close_time"] <= a
            ]
            if len(tt) >= min_train_trades:
                e = float(np.mean([t["r"] for t in tt]))
                if e > best_exp:
                    best, best_exp = ms, e
        if best is not None:
            # خروجی test هم باید تا انتهای پنجره‌ی test قابل مشاهده شده باشد.
            test = [
                t for t in runs[best]
                if a <= t["open_time"] < b and t["close_time"] <= b
            ]
            oos += test
            windows.append({"test_from": str(a)[:10], "chosen_min_score": best,
                            "train_exp": round(best_exp, 3), "test_n": len(test),
                            "test_exp": round(float(np.mean([t["r"] for t in test])), 3) if test else None})
        s += pd.Timedelta(days=test_days)
    return {"windows": windows, "out_of_sample": summarize(oos)}
