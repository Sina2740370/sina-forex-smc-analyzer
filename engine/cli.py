"""
خط فرمان موتور:

  python -m engine.cli backtest --source csv --csv-dir data --symbols EURUSD XAUUSD --tz-offset 3
  python -m engine.cli backtest --source yfinance --symbols EURUSD
  python -m engine.cli scan --symbols EURUSD GBPUSD

نام فایل CSV: {SYMBOL}_M15.csv (مثلاً EURUSD_M15.csv) خروجی Export Bars متاتریدر.
"""
from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from .backtest.runner import evaluate_gate, run_backtest, summarize
from .core.config import INSTRUMENTS, EngineConfig
from .data.providers import (build_topdown, load_mt5_csv, load_yfinance_topdown,
                             staleness_minutes, TopDownView)
from .strategy.smc_topdown import analyze


def _load(args, symbol: str) -> dict:
    if args.source == "csv":
        path = os.path.join(args.csv_dir, f"{symbol}_M15.csv")
        return build_topdown(load_mt5_csv(path, args.tz_offset))
    return load_yfinance_topdown(INSTRUMENTS[symbol].yf)


def cmd_backtest(args):
    cfg = EngineConfig()
    trades = []
    for sym in args.symbols:
        print(f"▶ {sym} ...")
        trades += run_backtest(sym, _load(args, sym), cfg, args.start, args.end)
    s = summarize(trades)
    print(json.dumps({"config": cfg.hash(), "summary": s, "gate": evaluate_gate(s)},
                     indent=2, default=str, ensure_ascii=False))
    if trades:
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, f"trades_{cfg.hash()}.csv")
        pd.DataFrame(trades).assign(features=lambda d: d["features"].map(json.dumps)).to_csv(path, index=False)
        print("trades →", path)


def cmd_scan(args):
    cfg = EngineConfig()
    now = pd.Timestamp.now(tz="UTC")
    for sym in args.symbols:
        data = _load(args, sym)
        lag = staleness_minutes(data["15m"], "15m", now)
        if lag > 45:
            print(f"{sym}: داده کهنه ({lag:.0f} دقیقه) → رد شد")
            continue
        d = analyze(TopDownView(data).at(now), cfg, now)
        print(sym, d.direction or f"— ({d.blocked})", f"score={d.score:.2f}", d.reasons)


def main():
    p = argparse.ArgumentParser(prog="engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("backtest", cmd_backtest), ("scan", cmd_scan)):
        sp = sub.add_parser(name)
        sp.add_argument("--source", choices=["csv", "yfinance"], default="yfinance")
        sp.add_argument("--csv-dir", default="data")
        sp.add_argument("--symbols", nargs="+", default=["EURUSD"])
        sp.add_argument("--tz-offset", type=float, default=0.0, help="اختلاف ساعت سرور بروکر با UTC")
        sp.add_argument("--start")
        sp.add_argument("--end")
        sp.add_argument("--out", default="out")
        sp.set_defaults(fn=fn)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
