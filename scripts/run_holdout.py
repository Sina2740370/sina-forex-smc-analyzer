"""بک‌تست holdout نهایی: baseline × ۱۰ نماد روی دادهٔ کاملاً دست‌نخوردهٔ ۲۰۲۰-۰۳ تا ۲۰۲۳-۰۵.

- دورهٔ holdout هیچ هم‌پوشانی با دادهٔ قبلی دارد؟ هیچ: دادهٔ قبلی ۲۰۲۳-۰۶ به بعد بود
  و همهٔ آزمایش‌های قبلی روی ۲۰۲۳-۰۷ تا ۲۰۲۶-۱۰ انجام شدند. این دوره هرگز دیده نشده.
- فیلترهای سشن/نمادِ بازمانده‌ها بعداً روی trades_detail اعمال می‌شوند (زیرمجموعه‌گیری).
"""
import json
import os
import sys
from multiprocessing import Pool

sys.path.insert(0, "/home/z/my-project/sweep-dev")
os.chdir("/home/z/my-project/sweep-dev")

from engine.data.providers import load_mt5_csv
from engine.mas.backtest import backtest_mas
from engine.mas.datafeeds import MASFeeds

OUT_DIR = "out_mas/holdout2019"
os.makedirs(OUT_DIR, exist_ok=True)
RESULTS = os.path.join(OUT_DIR, "results.json")
START, END = "2020-03-01", "2023-05-31"
SYMBOLS = ["AUDUSD", "EURJPY", "EURUSD", "GBPJPY", "GBPUSD",
           "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]


def run_one(sym: str):
    feeds = MASFeeds(online=False)
    df = load_mt5_csv(f"data_duka/{sym}_M15.csv")
    r = backtest_mas(df, sym, start=START, end=END, step_hours=8,
                     feeds=feeds, cfg={})
    s = r.summary()
    s["trades_detail"] = [{"entry_time": str(t.entry_time), "r": round(t.r_multiple, 3),
                           "regime": t.regime, "session": t.session}
                          for t in r.trades]
    print(f"[holdout] {sym}: {s['trades']}t E[R]={s['expectancy_R']} "
          f"PF={s['profit_factor']} WR={s['win_rate_pct']}%", flush=True)
    return sym, s


def _save(res):
    tmp = RESULTS + ".tmp"
    json.dump(res, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, RESULTS)


if __name__ == "__main__":
    res = {}
    if os.path.exists(RESULTS):
        res = json.load(open(RESULTS))
    todo = [s for s in SYMBOLS if s not in res]
    print(f"holdout {START} -> {END} | {len(todo)}/{len(SYMBOLS)} اجرا", flush=True)
    with Pool(2) as p:
        for sym, s in p.imap_unordered(run_one, todo):
            res[sym] = s
            _save(res)
    all_t = [t for s in res for t in res[s]["trades_detail"]]
    if all_t:
        er = sum(t["r"] for t in all_t) / len(all_t)
        wr = sum(1 for t in all_t if t["r"] > 0) / len(all_t) * 100
        print(f"POOLED: n={len(all_t)} E[R]={er:+.3f} WR={wr:.1f}%", flush=True)
    print("DONE", flush=True)
