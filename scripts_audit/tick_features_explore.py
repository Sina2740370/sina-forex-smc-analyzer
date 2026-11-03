"""آزمون اکتشافی ویژگی‌های تیک‌محور — پروکسی جریان سفارش از تیک واقعی.

ویژگی‌ها از تیک خام (M15 تجمیع‌شده): عدم‌توازن uptick/downtick (زیرخاکستری:
بر پایهٔ تغییر mid)، شدت ترید، z حجم، تغییر اسپرد. رویداد = |z|≥2 با
چارچوب aee.scoring. دادهٔ فعلی: ۱ روز × ۲ نماد → اکتشافی؛ پذیرش رسمی نیاز به
دادهٔ چند هفته دارد (در سند تصمیم ثبت می‌شود).
"""
import json
import os
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")
os.chdir("/home/z/my-project/sweep-dev")

import numpy as np
import pandas as pd

from aee.scoring import causal_zscore, extract_events, forward_outcome
from aee.stats import event_study_summary

OUT = "out_mas/tick_features_explore.json"
SYMBOLS = ["EURUSD", "GBPUSD"]


def build_m15_from_ticks(sym: str) -> pd.DataFrame:
    df = pd.read_csv(f"data_ticks/{sym}_ticks_all.csv.gz")
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True, format="mixed")
    df = df.sort_values("time_utc").reset_index(drop=True)
    mid = (df["ask"] + df["bid"]) / 2
    d = pd.DataFrame({
        "time": df["time_utc"].dt.floor("15min"),
        "mid": mid, "up": (mid.diff() > 0).astype(float),
        "dn": (mid.diff() < 0).astype(float),
        "vol": (df["ask_vol"].fillna(0) + df["bid_vol"].fillna(0)).astype(float),
        "spread": (df["ask"] - df["bid"]) / mid,
    })
    g = d.groupby("time")
    agg = pd.DataFrame({
        "n_ticks": g.size(),
        "open": g["mid"].first(), "high": g["mid"].max(),
        "low": g["mid"].min(), "close": g["mid"].last(),
        "vol_tick": g["vol"].sum(), "spread_med": g["spread"].median(),
        "imbalance": (g["up"].sum() - g["dn"].sum()) / g.size(),
    }).reset_index()
    agg["volume"] = agg["vol_tick"]
    return agg


def main():
    bars = {s: build_m15_from_ticks(s) for s in SYMBOLS}
    feats = {
        "tick_imbalance": lambda d: d["imbalance"],
        "tick_intensity": lambda d: d["n_ticks"].astype(float),
        "tick_vol_z": lambda d: d["vol_tick"],
        "spread_widen": lambda d: d["spread_med"],
    }
    results = {}
    for name, fn in feats.items():
        r_pool = []
        for sym, df in bars.items():
            z = causal_zscore(fn(df), window=32)   # یک روز = ۹۶ کندل M15؛ پنجرهٔ کوتاه برای نمونهٔ کوتاه
            ev = extract_events(z, threshold=1.0, cooldown=4)
            if len(ev) == 0:
                continue
            r_pool.append(forward_outcome(df, ev, horizon=8).dropna().to_numpy())
        r_all = np.concatenate(r_pool) if r_pool else np.array([])
        s = event_study_summary(r_all, n_boot=2000, seed=42) if r_all.size >= 10 \
            else {"n": int(r_all.size), "note": "رویداد کافی نیست"}
        results[name] = s
        print(f"{name}: n={s.get('n')} er={s.get('er')} p={s.get('p_ttest')}")
    results["meta"] = {
        "note": "اکتشافی — تیک ۱ روز (دوشنبه ۲۰۲۶-۱۰-۲۶) × ۲ نماد؛ p با این حجم "
                "رویداد فاقد قدرت آماری برای پذیرش/رد رسمی است و فقط جهت‌یابی است",
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=float)
    print(f"ذخیره شد: {OUT}")


if __name__ == "__main__":
    main()
