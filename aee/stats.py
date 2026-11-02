"""جعبه‌ابزار آماری AEE — بدون وابستگی به scipy؛ همهٔ محاسبات برداری numpy/pandas."""
from __future__ import annotations

import numpy as np
import pandas as pd


def benjamini_hochberg(pvals) -> np.ndarray:
    """مقادیر q تعدیل‌شدهٔ BH. ورودی مرتب نیست؛ ترتیب خروجی = ترتیب ورودی."""
    p = np.asarray(pvals, dtype=float)
    n = p.size
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order]
    q_sorted = ranked * n / np.arange(1, n + 1)
    q_sorted = np.minimum.accumulate(q_sorted[::-1])[::-1]  # تک‌نوایی‌سازی از بزرگ به کوچک
    q_sorted = np.minimum(q_sorted, 1.0)
    q = np.empty(n)
    q[order] = q_sorted
    return q


def bootstrap_mean_ci(values, n_boot: int = 10000, alpha: float = 0.05,
                      seed: int = 42) -> tuple[float, float, float]:
    """(میانگین، حد پایین، حد بالا) با بازهٔ صدک bootstrap.

    n < 2 → میانگین همان نمونه و NaN برای حدود.
    """
    x = np.asarray([v for v in values if v == v and np.isfinite(v)], dtype=float)
    if x.size < 2:
        m = float(x.mean()) if x.size else float("nan")
        return m, float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, x.size, size=(n_boot, x.size))
    means = x[idx].mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(x.mean()), float(lo), float(hi)


def event_study_summary(r_values, label: str = "",
                        n_boot: int = 10000, alpha: float = 0.05,
                        seed: int = 42) -> dict:
    """خلاصهٔ شواهد یک مجموعهٔ R: میانگین، CI bootstrap، وین‌ریت، آمارهٔ t."""
    x = np.asarray([v for v in r_values if v == v and np.isfinite(v)], dtype=float)
    n = int(x.size)
    if n < 2:
        return {"label": label, "n": n, "er": float("nan"), "ci_lo": float("nan"),
                "ci_hi": float("nan"), "win_rate": float("nan"),
                "t_stat": float("nan"), "p_ttest": float("nan")}
    er, lo, hi = bootstrap_mean_ci(x, n_boot=n_boot, alpha=alpha, seed=seed)
    sd = x.std(ddof=1)
    t_stat = er / (sd / np.sqrt(n)) if sd > 0 else float("nan")
    # p دوطرفهٔ t با جانشینی نرمال برای n بزرگ (n>=30)؛ برای n کوچک t تحفظ‌آمیزتر است
    from math import erf, sqrt
    p_two = 2 * (1 - 0.5 * (1 + erf(abs(t_stat) / sqrt(2)))) if np.isfinite(t_stat) else float("nan")
    return {"label": label, "n": n, "er": er, "ci_lo": lo, "ci_hi": hi,
            "win_rate": float((x > 0).mean()), "t_stat": float(t_stat),
            "p_ttest": float(p_two)}
