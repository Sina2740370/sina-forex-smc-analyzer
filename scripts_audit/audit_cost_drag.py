"""اندازه‌گیری واقعی سهم هزینهٔ اجرا نسبت به ریسک هر معامله (اسپرد بر حسب R)."""
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")
import os
os.chdir("/home/z/my-project/sweep-dev")

from engine.data.providers import load_mt5_csv
from engine.mas.backtest import backtest_mas
from engine.mas.datafeeds import MASFeeds
from engine.mas.symbols import get_spec

for sym in ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY"]:
    feeds = MASFeeds(online=False)
    df = load_mt5_csv(f"data_duka/{sym}_M15.csv")
    r = backtest_mas(df, sym, start="2020-03-01", end="2023-05-31",
                     step_hours=8, feeds=feeds, cfg={})
    if not r.trades:
        print(f"{sym}: بدون معامله")
        continue
    spec = get_spec(sym)
    sl_pips = [abs(t.entry - t.sl) / spec.pip for t in r.trades]
    sl_pips.sort()
    med = sl_pips[len(sl_pips) // 2]
    spread_pips = spec.spread_price() / spec.pip
    bars = [t.bars_held for t in r.trades]
    bars.sort()
    print(f"{sym}: اسپرد={spread_pips:.1f} پیپ | میانهٔ SL={med:.1f} پیپ | "
          f"هزینهٔ اسپرد=میانه {spread_pips/med*100:.1f}% از 1R | "
          f"میانهٔ نگهداری={bars[len(bars)//2]} کندل M15 | n={len(r.trades)}", flush=True)
