"""فاز ۱ — پروفایل اسپرد بر اساس تیک واقعی: تفکیک نشستی و مقایسه با مدل هزینهٔ ممیزی.

ورودی: data_ticks/{SYM}_ticks_all.csv.gz (خروجی fetch_ticks_phase1.py)
خروجی: out_mas/phase1_spread_sessions.json
"""
import json
import os
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")
os.chdir("/home/z/my-project/sweep-dev")

import numpy as np
import pandas as pd

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
SESSIONS = {"asia": (0, 7), "london": (7, 13), "newyork": (13, 21), "off": (21, 24)}
OUT = "out_mas/phase1_spread_sessions.json"


def session_of_hour(h):
    for name, (a, b) in SESSIONS.items():
        if a <= h < b:
            return name
    return "off"


def main():
    report = {}
    for sym in SYMBOLS:
        path = f"data_ticks/{sym}_ticks_all.csv.gz"
        if not os.path.exists(path):
            print(f"{sym}: فایل تیک نیست")
            continue
        df = pd.read_csv(path, usecols=["ask", "bid"])
        df["time_utc"] = pd.read_csv(path, usecols=["time_utc"])["time_utc"]
        mid = (df["ask"] + df["bid"]) / 2
        spread = df["ask"] - df["bid"]
        rel_bp = (spread / mid) * 1e4
        hour = pd.to_datetime(df["time_utc"], utc=True, format="mixed").dt.hour
        sess = hour.map(session_of_hour)
        entry = {"n_ticks": int(len(df)),
                 "rel_spread_median_bp": float(rel_bp.median()),
                 "rel_spread_p90_bp": float(rel_bp.quantile(0.9)),
                 "by_session": {}}
        for s in SESSIONS:
            m = (sess == s).to_numpy()
            if m.sum() > 0:
                entry["by_session"][s] = {
                    "median_bp": float(rel_bp[m].median()),
                    "p90_bp": float(rel_bp[m].quantile(0.9)),
                }
        report[sym] = entry
        print(f"{sym}: میانه {entry['rel_spread_median_bp']:.2f} bp | "
              + " | ".join(f"{s}: {v['median_bp']:.2f}" for s, v in entry["by_session"].items()))
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"ذخیره شد: {OUT}")


if __name__ == "__main__":
    main()
