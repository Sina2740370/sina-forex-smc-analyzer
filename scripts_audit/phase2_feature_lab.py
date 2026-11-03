"""پذیرش فاز ۲ — لابراتوار ویژگی روی دو نمونهٔ مستقل + BH-FDR.

- ۲۲ ویژگی کتابخانهٔ فاز ۲ × ۴ نماد × دو پنجرهٔ سوختهٔ ممیزی (holdout نهایی بسته است).
- رویداد: |z| ≥ 2 با cooldown=8؛ برچسب: بازده ۱۶ کندل بعد در جهت رویداد، بر حسب ATR.
- معیار پذیرش فاز ۲: ≥۵ ویژگی با q<0.05 در هر دو پنجره و علامت ثابت.
- خروجی: out_mas/phase2_features.json
"""
import json
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")

import pandas as pd

from aee.library import LIBRARY_FEATURES
from aee.scoring import run_lab, fdr_pass_two_windows
from engine.data.providers import load_mt5_csv

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
WINDOWS = [("w1_2023H2-2024H1", "2023-07-01", "2024-10-31"),
           ("w2_2024H2-2025H1", "2024-11-01", "2025-10-31")]
OUT = "/home/z/my-project/sweep-dev/out_mas/phase2_features.json"


def main():
    results = {}
    frames = {}
    for wname, start, end in WINDOWS:
        df_by_symbol = {}
        for sym in SYMBOLS:
            df = load_mt5_csv(f"data_duka/{sym}_M15.csv")
            df_by_symbol[sym] = df[(df["time"] >= start) & (df["time"] <= end)].reset_index(drop=True)
        res = run_lab(df_by_symbol, LIBRARY_FEATURES, horizon=16,
                      threshold=2.0, cooldown=8, min_events=20)
        res = res.round(6).sort_values("p_ttest")
        frames[wname] = res
        print(f"== {wname} ==")
        print(res.to_string(index=False), flush=True)
        results[wname] = res.to_dict(orient="records")

    passed = fdr_pass_two_windows(frames[WINDOWS[0][0]], frames[WINDOWS[1][0]], alpha=0.05)
    results["fdr_passed_both_windows"] = passed
    results["meta"] = {
        "note": "فاز ۲ — ۲۲ ویژگی، رویداد |z|≥2 با cooldown=8، برچسب ۱۶ کندل بر حسب ATR؛ "
                "دو پنجرهٔ مستقل سوختهٔ ممیزی؛ holdout نهایی باز نشده است",
        "n_features": len(LIBRARY_FEATURES),
        "acceptance": "≥۵ ویژگی با q<0.05 در هر دو پنجره و علامت ثابت",
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=float)
    print(f"\nویژگی‌های عبوری از FDR دومنظه: {passed or 'هیچ'}")
    print(f"ذخیره شد: {OUT}")


if __name__ == "__main__":
    main()
