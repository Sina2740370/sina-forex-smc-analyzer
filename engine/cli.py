"""
خط فرمان موتور:

  python -m engine.cli fetch --symbols XAUUSD EURUSD GBPUSD USDJPY --years 4 --out data_duka
  python -m engine.cli backtest --source csv --csv-dir data --symbols EURUSD XAUUSD --broker-timezone Europe/Helsinki
  python -m engine.cli scan --symbols EURUSD GBPUSD
  python -m engine.cli report --source csv --csv-dir data_duka --symbols XAUUSD EURUSD --walk-forward

نام فایل CSV: {SYMBOL}_M15.csv (یا {SYMBOL}_i_M15.csv)
"""
from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from .backtest.report import fold_stability, feature_table, random_baseline, walk_forward
from .backtest.runner import evaluate_gate, run_backtest, summarize
from .core.config import INSTRUMENTS, EngineConfig
from .data.providers import (build_topdown, load_mt5_csv, load_yfinance_topdown,
                             staleness_minutes, TopDownView)
from .strategy.smc_topdown import analyze


def _strategy(name: str):
    """smc پیش‌فرض؛ s1/s2 از engine/strategy/baselines.py (import تنبل)."""
    if name == "smc":
        return analyze
    from .strategy import baselines
    return {"s1": baselines.analyze_s1, "s2": baselines.analyze_s2}[name]


def _load(args, symbol: str) -> dict:
    if args.source == "csv":
        path = os.path.join(args.csv_dir, f"{symbol}_M15.csv")
        if not os.path.exists(path):
            path = os.path.join(args.csv_dir, f"{symbol}_i_M15.csv")
        if not os.path.exists(path):
            raise SystemExit(
                f"فایل پیدا نشد: {symbol}_M15.csv یا {symbol}_i_M15.csv در {args.csv_dir}"
            )
        return build_topdown(load_mt5_csv(path, args.tz_offset, args.broker_timezone))
    return load_yfinance_topdown(INSTRUMENTS[symbol].yf)


def cmd_fetch(args):
    from .data.fetch_history import fetch_symbol
    for sym in args.symbols:
        fetch_symbol(sym, args.years, args.out)


def cmd_backtest(args):
    cfg = EngineConfig()
    trades = []
    for sym in args.symbols:
        print(f"> {sym} ...", flush=True)
        trades += run_backtest(sym, _load(args, sym), cfg, args.start, args.end,
                               analyze_fn=_strategy(args.strategy))
    s = summarize(trades)
    print(json.dumps({"config": cfg.hash(), "summary": s, "gate": evaluate_gate(s)},
                     indent=2, default=str, ensure_ascii=False))
    if trades:
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, f"trades_{cfg.hash()}.csv")
        pd.DataFrame(trades).assign(features=lambda d: d["features"].map(json.dumps)).to_csv(path, index=False)
        print("trades ->", path)


def cmd_report(args):
    cfg = EngineConfig()
    all_trades, lines = [], []
    for sym in args.symbols:
        print(f"> {sym} ...", flush=True)
        data = _load(args, sym)
        trades = run_backtest(sym, data, cfg, args.start, args.end,
                              analyze_fn=_strategy(args.strategy))
        all_trades += trades
        lines.append(f"\n===== {sym} =====")
        lines.append("خلاصه: " + json.dumps(summarize(trades), default=str, ensure_ascii=False))
        lines.append("مقایسه با ورود تصادفی: " + json.dumps(
            random_baseline(sym, data["15m"], trades, cfg, args.sims), ensure_ascii=False))
        if args.walk_forward:
            wf = walk_forward(sym, data, cfg, tuple(args.grid), args.train_days, args.test_days)
            lines.append("walk-forward (خارج از نمونه): " + json.dumps(wf, default=str, ensure_ascii=False))
    s = summarize(all_trades)
    lines.append("\n===== کل =====")
    lines.append(json.dumps({"summary": s, "gate": evaluate_gate(s)}, default=str, ensure_ascii=False, indent=1))
    lines.append("\nپایداری زمانی: " + json.dumps(fold_stability(all_trades), ensure_ascii=False))
    lines.append("\nعملکرد ویژگی‌ها:\n" + (feature_table(all_trades).to_string(index=False) if all_trades else "-"))
    text = "\n".join(lines)
    print(text)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"report_{cfg.hash()}.txt")
    open(path, "w", encoding="utf-8").write(text)
    print("\nگزارش ذخیره شد ->", path)


def cmd_scan(args):
    cfg = EngineConfig()
    now = pd.Timestamp.now(tz="UTC")
    for sym in args.symbols:
        data = _load(args, sym)
        lag = staleness_minutes(data["15m"], "15m", now)
        if lag > 45:
            print(f"{sym}: داده کهنه ({lag:.0f} دقیقه) -> رد شد")
            continue
        d = analyze(TopDownView(data).at(now), cfg, now)
        print(sym, d.direction or f"- ({d.blocked})", f"score={d.score:.2f}", d.reasons)


def main():
    p = argparse.ArgumentParser(prog="engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    fp = sub.add_parser("fetch", help="دانلود تاریخچه از Dukascopy")
    fp.add_argument("--symbols", nargs="+", default=["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"])
    fp.add_argument("--years", type=float, default=4)
    fp.add_argument("--out", default="data_duka")
    fp.set_defaults(fn=cmd_fetch)

    for name, fn in (("backtest", cmd_backtest), ("scan", cmd_scan), ("report", cmd_report)):
        sp = sub.add_parser(name)
        sp.add_argument("--strategy", choices=["smc", "s1", "s2"], default="smc")
        sp.add_argument("--source", choices=["csv", "yfinance"], default="yfinance")
        sp.add_argument("--csv-dir", default="data")
        sp.add_argument("--symbols", nargs="+", default=["EURUSD"])
        sp.add_argument("--tz-offset", type=float, default=0.0,
                       help="UTC offset ثابت برای CSV بدون timezone؛ برای بروکر دارای DST از --broker-timezone استفاده کن")
        sp.add_argument("--broker-timezone", default=None,
                       help="منطقه زمانی IANA ساعت سرور بروکر، مثلاً Europe/Helsinki")
        sp.add_argument("--start")
        sp.add_argument("--end")
        sp.add_argument("--out", default="out")
        sp.add_argument("--sims", type=int, default=300)
        sp.add_argument("--walk-forward", action="store_true")
        sp.add_argument("--grid", nargs="+", type=float, default=[0.5, 0.6, 0.7])
        sp.add_argument("--train-days", type=int, default=180)
        sp.add_argument("--test-days", type=int, default=60)
        sp.set_defaults(fn=fn)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
