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
from .runner import run_backtest, summarize


# ---------------------------------------------------------------- ۱) رندوم
def _simulate(side, mid, risk_dist, rr, hi, lo, cl, half, n):
    """همان قواعد runner: SL اولویت دارد، EXPIRED با قیمت بازار در کندل n‌ام."""
    h, l = hi[:n], lo[:n]
    entry = mid + side * half
    sl = entry - side * risk_dist
    tp = entry + side * risk_dist * rr
    if side > 0:
        sl_hit, tp_hit = l - half <= sl, h - half >= tp
    else:
        sl_hit, tp_hit = h + half >= sl, l + half <= tp
    fs = int(sl_hit.argmax()) if sl_hit.any() else n + 1
    ft = int(tp_hit.argmax()) if tp_hit.any() else n + 1
    risk = (entry - sl) * side
    if fs <= ft and fs < n:
        exit_px = sl
    elif ft < n:
        exit_px = tp
    else:
        exit_px = cl[n - 1] - side * half
    return (exit_px - entry) * side / risk


def random_baseline(symbol: str, df15: pd.DataFrame, trades: list, cfg: EngineConfig,
                    n_sims: int = 300, seed: int = 1) -> dict:
    ts = [t for t in trades if t["symbol"] == symbol]
    if len(ts) < 5:
        return {"n": len(ts), "note": "تعداد معامله برای مقایسه کافی نیست"}
    half = spread_price(symbol) / 2.0
    times = df15["time"]
    hi, lo, cl = (df15[k].to_numpy() for k in ("high", "low", "close"))
    nb = cfg.max_hold_hours * 4
    if len(df15) <= nb + 2:
        return {"n": len(ts), "note": "داده برای شبیه‌سازی مبنای تصادفی کافی نیست"}

    bar = pd.Timedelta(minutes=15)
    j = [int(times.searchsorted(t["open_time"] - bar)) for t in ts]
    j_lo, j_hi = min(j), min(max(j), len(df15) - nb - 2)
    if j_lo > j_hi:
        return {"n": len(ts), "note": "پنجره‌ی زمانی معتبری برای مبنای تصادفی وجود ندارد"}

    # Preserve each real trade's executable entry-to-stop risk in R units.
    risks = np.array([((t["entry"] - t["sl"]) *
                       (1 if t["side"] == "BUY" else -1)) for t in ts], dtype=float)
    if not np.isfinite(risks).all() or (risks <= 0).any():
        return {"n": len(ts), "note": "فاصله‌ی ریسک نامعتبر در معاملات ورودی"}
    real = float(np.mean([t["r"] for t in ts]))

    rng = np.random.default_rng(seed)
    sims = np.empty(n_sims)
    for s in range(n_sims):
        rs = []
        for rd in risks:
            k = int(rng.integers(j_lo, j_hi + 1))
            side = 1 if rng.random() < 0.5 else -1
            rs.append(_simulate(side, cl[k], rd, cfg.risk_reward,
                                hi[k + 1:k + 1 + nb], lo[k + 1:k + 1 + nb], cl[k + 1:k + 1 + nb],
                                half, nb))
        sims[s] = np.mean(rs)
    return {
        "n": len(ts), "real_expectancy_r": round(real, 4),
        "random_mean_r": round(float(sims.mean()), 4),
        "random_p95_r": round(float(np.percentile(sims, 95)), 4),
        "p_value": round((int((sims >= real).sum()) + 1) / (n_sims + 1), 4),
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
    در هر fold، کانفیگ‌ها فقط روی بازه‌ی train ارزیابی می‌شوند؛ سپس کانفیگ منتخب
    با حالت تازه فقط در بازه‌ی test اجرا می‌شود. مرزها نیمه‌بازند تا معامله‌ی مرزی دوبار شمرده نشود.
    """
    t0 = data["15m"]["time"].iloc[0] + pd.Timedelta(days=warmup_days)
    t_end = data["15m"]["time"].iloc[-1]
    windows, oos = [], []
    s = t0
    while s + pd.Timedelta(days=train_days + test_days) <= t_end:
        a, b = s + pd.Timedelta(days=train_days), s + pd.Timedelta(days=train_days + test_days)
        # run_backtest treats --end as inclusive; subtract 1 ns to keep folds half-open [start, end).
        train_end = a - pd.Timedelta(nanoseconds=1)
        test_end = b - pd.Timedelta(nanoseconds=1)
        best, best_exp = None, -1e9
        for ms in grid:
            train = run_backtest(symbol, data, replace(base, min_score=ms),
                                 start=s.tz_localize(None).isoformat(), end=train_end.tz_localize(None).isoformat())
            if len(train) >= min_train_trades:
                e = float(np.mean([t["r"] for t in train]))
                if e > best_exp:
                    best, best_exp = ms, e
        if best is not None:
            test = run_backtest(symbol, data, replace(base, min_score=best),
                                start=a.tz_localize(None).isoformat(), end=test_end.tz_localize(None).isoformat())
            oos += test
            windows.append({"test_from": str(a)[:10], "chosen_min_score": best,
                            "train_exp": round(best_exp, 3), "test_n": len(test),
                            "test_exp": round(float(np.mean([t["r"] for t in test])), 3) if test else None})
        s += pd.Timedelta(days=test_days)
    return {"windows": windows, "out_of_sample": summarize(oos)}
