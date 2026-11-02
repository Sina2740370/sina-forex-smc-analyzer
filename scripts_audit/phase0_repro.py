"""پذیرش فاز ۰: بازتولید نظام‌مند نتایج ممیزی با چارچوب AEE.

- دو پنجرهٔ از قبل سوختهٔ ممیزی (۲۰۲۳-۰۷→۲۰۲۴-۱۰ و ۲۰۲۴-۱۱→۲۰۲۵-۱۰) — holdout نهایی باز نمی‌شود.
- برای هر پنجره: E[R] با CI bootstrap، آزمون t، و BH-FDR روی ۱۰ نماد.
- معیار پذیرش فاز ۰: بازتولید همان حکم ممیزی (شواهد لبه منفی/صفر، بدون پاس مثبت OOS).
"""
import json
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")

from aee import event_study_summary, benjamini_hochberg
from engine.data.providers import load_mt5_csv
from engine.mas.backtest import backtest_mas
from engine.mas.datafeeds import MASFeeds

SYMBOLS = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF",
           "USDCAD", "EURJPY", "GBPJPY", "XAUUSD"]
WINDOWS = [("w1_2023H2-2024H1", "2023-07-01", "2024-10-31"),
           ("w2_2024H2-2025H1", "2024-11-01", "2025-10-31")]
OUT = "/home/z/my-project/sweep-dev/out_mas/phase0_repro.json"


def main():
    feeds = MASFeeds(online=False)
    report = {"windows": {}, "meta": {
        "note": "فاز ۰ — بازتولید نتایج ممیزی با چارچوب AEE؛ holdout نهایی باز نشده است",
    }}
    for wname, start, end in WINDOWS:
        per_symbol = {}
        for sym in SYMBOLS:
            df = load_mt5_csv(f"data_duka/{sym}_M15.csv")
            r = backtest_mas(df, sym, start=start, end=end,
                             step_hours=8, feeds=feeds, cfg={})
            rs = [t.r_multiple for t in r.trades]
            s = event_study_summary(rs, sym, n_boot=10000, seed=42)
            per_symbol[sym] = s
            print(f"{wname} {sym}: n={s['n']} E[R]={s['er']:+.4f} "
                  f"CI=[{s['ci_lo']:+.4f},{s['ci_hi']:+.4f}] p={s['p_ttest']:.3f}", flush=True)
        # BH-FDR روی نمادهایی که نمونه دارند
        valid = [s for s in per_symbol.values() if s["n"] >= 2]
        qvals = benjamini_hochberg([s["p_ttest"] for s in valid])
        passed = []
        for s, q in zip(valid, qvals):
            s["q_bh"] = round(float(q), 4)
            if q < 0.05 and s["er"] > 0:
                passed.append(s["label"])
        report["windows"][wname] = {"per_symbol": per_symbol,
                                    "fdr_passed_positive": passed}
        print(f"== {wname}: FDR-passed(positive) = {passed or 'هیچ'}", flush=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=float)
    print(f"\nذخیره شد: {OUT}")


if __name__ == "__main__":
    main()
