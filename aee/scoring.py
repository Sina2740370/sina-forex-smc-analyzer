"""چارچوب نمره‌دهی فاز ۲ — هر ویژگی → رویداد → E[R] → BH-FDR.

قرارداد: برچسب (outcome) از آینده محاسبه می‌شود ولی فقط برای سنجش رویدادِ
گذشته — این همان مطالعهٔ رویداد است، نه ویژگی مدل.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from aee.stats import benjamini_hochberg, event_study_summary


def causal_zscore(s: pd.Series, window: int = 120) -> pd.Series:
    """z با آماره‌های پنجرهٔ منتهی به t (شامل خود t) — بدون آینده."""
    m = s.rolling(window, min_periods=window).mean()
    sd = s.rolling(window, min_periods=window).std()
    return (s - m) / sd.replace(0, np.nan)


def atr_series(df: pd.DataFrame, window: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()


def extract_events(z: pd.Series, threshold: float = 2.0, cooldown: int = 8) -> pd.DataFrame:
    """نقاط عبور z از ±threshold با فاصلهٔ حداقلی cooldown کندل؛ جهت = علامت z."""
    z = z.fillna(0.0)
    active_until = -1
    rows = []
    vals = z.to_numpy()
    for i, v in enumerate(vals):
        if i <= active_until:
            continue
        if abs(v) >= threshold:
            rows.append({"idx": i, "time": z.index[i], "direction": 1.0 if v > 0 else -1.0})
            active_until = i + cooldown - 1
    return pd.DataFrame(rows, columns=["idx", "time", "direction"])


def forward_outcome(df: pd.DataFrame, events: pd.DataFrame, horizon: int = 16) -> pd.Series:
    """بازده رو به جلو در جهت رویداد، نرمال با ATR لحظهٔ رویداد (بر حسب «R ساختاری»)."""
    close = df["close"].to_numpy()
    atr = atr_series(df).to_numpy()
    out = np.full(len(events), np.nan)
    for k, (_, ev) in enumerate(events.iterrows()):
        i = int(ev["idx"])
        if i + horizon >= len(df) or atr[i] != atr[i]:
            continue
        out[k] = (close[i + horizon] - close[i]) / atr[i] * ev["direction"]
    return pd.Series(out, index=events.index)


def score_feature(df: pd.DataFrame, feat: pd.Series, horizon: int = 16,
                  threshold: float = 2.0, cooldown: int = 8,
                  min_events: int = 20) -> dict:
    """امتیاز یک ویژگی روی یک نمونه: رویداد → E[R]، CI، p."""
    events = extract_events(causal_zscore(feat), threshold, cooldown)
    if len(events) < min_events:
        return {"n": int(len(events)), "er": float("nan"), "ci_lo": float("nan"),
                "ci_hi": float("nan"), "win_rate": float("nan"),
                "t_stat": float("nan"), "p_ttest": float("nan")}
    r = forward_outcome(df, events, horizon)
    return event_study_summary(r.dropna().to_numpy(), n_boot=5000, seed=42)


def run_lab(df_by_symbol: dict[str, pd.DataFrame], features: dict[str, callable],
            horizon: int = 16, threshold: float = 2.0, cooldown: int = 8,
            min_events: int = 20) -> pd.DataFrame:
    """لابراتوار: برای هر ویژگی، رویدادها روی همهٔ نمادها تجمیع و امتیاز می‌شود.

    خروجی: جدول feature × n × E[R] × CI × p.
    """
    rows = []
    for name, fn in features.items():
        r_pool = []
        for sym, df in df_by_symbol.items():
            feat = fn(df)
            events = extract_events(causal_zscore(feat), threshold, cooldown)
            if len(events) == 0:
                continue
            r = forward_outcome(df, events, horizon)
            r_pool.append(r.dropna().to_numpy())
        r_all = np.concatenate(r_pool) if r_pool else np.array([])
        s = event_study_summary(r_all, n_boot=5000, seed=42) if r_all.size >= min_events \
            else {"n": int(r_all.size), "er": float("nan"), "ci_lo": float("nan"),
                  "ci_hi": float("nan"), "win_rate": float("nan"),
                  "t_stat": float("nan"), "p_ttest": float("nan")}
        rows.append({"feature": name, **s})
    return pd.DataFrame(rows)


def fdr_pass_two_windows(res1: pd.DataFrame, res2: pd.DataFrame,
                         alpha: float = 0.05) -> list[str]:
    """ویژگی‌های عبوری: q < alpha در هر دو نمونهٔ مستقل با همان علامت E[R]."""
    def qcol(res):
        q = benjamini_hochberg(res["p_ttest"].to_numpy())
        out = res.copy()
        out["q"] = q
        return out
    a, b = qcol(res1), qcol(res2)
    merged = a.merge(b, on="feature", suffixes=("_w1", "_w2"))
    ok = merged[(merged["q_w1"] < alpha) & (merged["q_w2"] < alpha) &
                (np.sign(merged["er_w1"]) == np.sign(merged["er_w2"]))]
    return ok["feature"].tolist()
