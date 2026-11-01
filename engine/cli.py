"""
خط فرمان موتور:

  python -m engine.cli fetch --symbols XAUUSD EURUSD GBPUSD USDJPY --years 4 --out data_duka
  python -m engine.cli backtest --source csv --csv-dir data --symbols EURUSD XAUUSD --tz-offset 3
  python -m engine.cli backtest --source csv --csv-dir engine/data --symbols EURUSD --server-tz Europe/Athens
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
    """smc پیش‌فرض؛ s1/s2 پایه‌ها؛ v4 موتور ensemble جدید."""
    if name == "smc":
        return analyze
    if name == "v4":
        from .strategy.quant_v4 import analyze_v4
        return analyze_v4
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
        server_tz = getattr(args, "server_tz", None) or None
        return build_topdown(load_mt5_csv(
            path, tz_offset_hours=args.tz_offset, server_tz=server_tz
        ))
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


def cmd_sweep(args):
    from .tools.param_sweep import (grid_variants, oat_variants, run_sweep,
                                    stability_verdict, to_markdown)
    cfg = EngineConfig()
    if args.mode == "grid":
        if not args.set:
            raise SystemExit("حالت grid به --set name=v1,v2,... نیاز دارد")
        variants = grid_variants(cfg, args.set)
    else:
        variants = oat_variants(cfg, args.pct, all_params=args.all_params,
                                only=args.params)
    print(f"{len(variants)} ترکیب × {len(args.symbols)} نماد ...", flush=True)
    data_map = {}
    for sym in args.symbols:
        print(f"> load {sym} ...", flush=True)
        data_map[sym] = _load(args, sym)

    df = run_sweep(data_map, variants, args.symbols, args.start, args.end,
                   args.strategy, args.workers)
    verdicts = stability_verdict(df, oat=(args.mode == "oat"))
    note = (f"engine.cli sweep --mode {args.mode} --symbols {' '.join(args.symbols)}"
            + (f" --set {' '.join(args.set)}" if args.set else "")
            + (f" --pct {args.pct}" if args.mode == "oat" else ""))

    os.makedirs(args.out, exist_ok=True)
    tag = f"{args.mode}_{cfg.hash()}"
    csv_path = os.path.join(args.out, f"sweep_{tag}.csv")
    md_path = os.path.join(args.out, f"sweep_{tag}.md")
    df.to_csv(csv_path, index=False)
    md = to_markdown(df, verdicts, note)
    open(md_path, "w", encoding="utf-8").write(md)
    print("\n".join(verdicts))
    print("\nجدول کامل:\n", df.to_string(index=False))
    print("\nذخیره شد ->", csv_path, "و", md_path)


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


def cmd_mas_analyze(args):
    """سیستم چندایجنتی (MAS): تحلیل یک نقطه زمانی با ۷ گیت + Risk Agent."""
    from .mas.aggregator import analyze_mas
    from .mas.reports import signal_json, signal_markdown

    df15 = load_mt5_csv(_mas_csv_path(args.csv_dir, args.symbol), tz_offset_hours=args.tz_offset)
    now = pd.Timestamp(args.now, tz="UTC") if args.now else pd.Timestamp.now(tz="UTC")
    sig = analyze_mas(df15, args.symbol, now, spread_price=args.spread)
    os.makedirs(args.out, exist_ok=True)
    jpath = os.path.join(args.out, f"mas_{args.symbol}_{sig.signal}.json")
    mpath = os.path.join(args.out, f"mas_{args.symbol}_{sig.signal}.md")
    with open(jpath, "w", encoding="utf-8") as f:
        f.write(signal_json(sig))
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(signal_markdown(sig))
    print(signal_json(sig, include_reports=False))
    print("\nذخیره شد ->", jpath, "و", mpath)


def _mas_csv_path(csv_dir: str, symbol: str) -> str:
    for suffix in ("_M15.csv", "_i_M15.csv"):
        p = os.path.join(csv_dir, f"{symbol}{suffix}")
        if os.path.exists(p):
            return p
    raise SystemExit(f"فایل {symbol}_M15.csv در {csv_dir} پیدا نشد")


def cmd_mas_portfolio(args):
    """فاز ۴: تحلیل چندجفتی هم‌زمان با AccountState مشترک + گزارش ترکیبی."""
    from .mas.portfolio import run_portfolio, save_portfolio, portfolio_markdown
    from .mas.symbols import supported_symbols
    from .mas.datafeeds import MASFeeds

    symbols = args.symbols or supported_symbols(args.csv_dir)
    now = pd.Timestamp(args.now, tz="UTC") if args.now else None
    feeds = MASFeeds(online=not args.offline)
    p = run_portfolio(args.csv_dir, now=now, symbols=symbols, equity=args.equity, feeds=feeds)
    jp, mp = save_portfolio(p, args.out)
    print(portfolio_markdown(p) if len(symbols) <= 2 else
          "\n".join(portfolio_markdown(p).splitlines()[:12]))
    print("\nذخیره شد ->", jp, "و", mp)


def cmd_mas_backtest(args):
    """فاز ۲: بک‌تست walk-forward سیگنال MAS روی نماد(ها)."""
    import json
    from .mas.backtest import backtest_mas
    from .mas.symbols import supported_symbols
    from .mas.datafeeds import MASFeeds

    feeds = MASFeeds(online=not args.offline)
    symbols = args.symbols or supported_symbols(args.csv_dir)
    os.makedirs(args.out, exist_ok=True)
    all_summaries = {}
    for sym in symbols:
        df = load_mt5_csv(_mas_csv_path(args.csv_dir, sym))
        r = backtest_mas(df, sym, start=args.start, end=args.end,
                         step_hours=args.step_hours, feeds=feeds)
        all_summaries[sym] = r.summary()
        s = all_summaries[sym]
        print(f"== {sym}: {s['trades']} معامله | برد {s['win_rate_pct']}% | "
              f"PF={s['profit_factor']} | E[R]={s['expectancy_R']} | "
              f"PnL={s['total_pnl_usd']} USD | MDD={s['max_drawdown_pct']}%")
    with open(os.path.join(args.out, "bt_all.json"), "w", encoding="utf-8") as f:
        json.dump(all_summaries, f, ensure_ascii=False, indent=2, default=str)
    print("ذخیره شد ->", os.path.join(args.out, "bt_all.json"))


def main():
    p = argparse.ArgumentParser(prog="engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    fp = sub.add_parser("fetch", help="دانلود تاریخچه از Dukascopy")
    fp.add_argument("--symbols", nargs="+", default=["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"])
    fp.add_argument("--years", type=float, default=4)
    fp.add_argument("--out", default="data_duka")
    fp.set_defaults(fn=cmd_fetch)

    for name, fn in (("backtest", cmd_backtest), ("scan", cmd_scan),
                     ("report", cmd_report)):
        sp = sub.add_parser(name)
        sp.add_argument("--strategy", choices=["smc", "s1", "s2", "v4"], default="smc")
        sp.add_argument("--source", choices=["csv", "yfinance"], default="yfinance")
        sp.add_argument("--csv-dir", default="data")
        sp.add_argument("--symbols", nargs="+", default=["EURUSD"])
        sp.add_argument("--tz-offset", type=float, default=0.0)
        sp.add_argument("--server-tz", default=None,
                        help="نام IANA، مثلاً Europe/Athens برای LiteFinance")
        sp.add_argument("--start")
        sp.add_argument("--end")
        sp.add_argument("--out", default="out")
        sp.add_argument("--sims", type=int, default=300)
        sp.add_argument("--walk-forward", action="store_true")
        sp.add_argument("--grid", nargs="+", type=float, default=[0.5, 0.6, 0.7])
        sp.add_argument("--train-days", type=int, default=180)
        sp.add_argument("--test-days", type=int, default=60)
        sp.set_defaults(fn=fn)

    sw = sub.add_parser("sweep", help="سوییپ/حساسیت پارامترها (ضد overfitting)")
    sw.add_argument("--strategy", choices=["smc", "s1", "s2", "v4"], default="smc")
    sw.add_argument("--source", choices=["csv", "yfinance"], default="yfinance")
    sw.add_argument("--csv-dir", default="data")
    sw.add_argument("--symbols", nargs="+", default=["EURUSD"])
    sw.add_argument("--tz-offset", type=float, default=0.0)
    sw.add_argument("--server-tz", default=None)
    sw.add_argument("--start", default=None)
    sw.add_argument("--end", default=None)
    sw.add_argument("--out", default="out")
    sw.add_argument("--mode", choices=["oat", "grid"], default="oat")
    sw.add_argument("--pct", type=float, default=20.0,
                    help="درصد جابه‌جایی در حالت oat (پیش‌فرض ۲۰)")
    sw.add_argument("--all-params", action="store_true",
                    help="همه‌ی پارامترهای عددی (کندتر) به‌جای CORE_PARAMS")
    sw.add_argument("--params", nargs="*", default=None,
                    help="حالت oat: فقط این پارامترها سنجیده شوند (اجرای متمرکز)")
    sw.add_argument("--set", action="append", default=None,
                    help="حالت grid: name=v1,v2,... (چند بار مجاز)")
    sw.add_argument("--workers", type=int, default=0,
                    help="تعداد پروسه‌ی موازی (۰ = تعداد هسته‌ها)")
    sw.set_defaults(fn=cmd_sweep)

    ma = sub.add_parser("mas-analyze", help="سیستم چندایجنتی (فاز ۱ MVP)")
    ma.add_argument("--symbol", default="EURUSD")
    ma.add_argument("--csv-dir", default="data_duka")
    ma.add_argument("--tz-offset", type=float, default=0.0)
    ma.add_argument("--now", default=None, help="نقطه زمانی UTC؛ خالی = الان")
    ma.add_argument("--spread", type=float, default=0.0, help="اسپرد واقعی بر حسب قیمت")
    ma.add_argument("--out", default="out")
    ma.set_defaults(fn=cmd_mas_analyze)

    mp = sub.add_parser("mas-portfolio", help="فاز ۴: تحلیل چندجفتی با ریسک مشترک")
    mp.add_argument("--symbols", nargs="*", default=None,
                    help="نمادها؛ خالی = همه نمادهایی که داده دارند")
    mp.add_argument("--csv-dir", default="data_duka")
    mp.add_argument("--now", default=None, help="نقطه زمانی UTC؛ خالی = آخرین کندل هر نماد")
    mp.add_argument("--equity", type=float, default=10_000.0)
    mp.add_argument("--out", default="out_mas")
    mp.add_argument("--offline", action="store_true", help="فقط کش؛ بدون شبکه (A4/A5 stub می‌شوند اگر کش نباشد)")
    mp.set_defaults(fn=cmd_mas_portfolio)

    mb = sub.add_parser("mas-backtest", help="فاز ۲: بک‌تست سیگنال MAS (هر سیگنال = یک معامله)")
    mb.add_argument("--symbols", nargs="*", default=None,
                    help="نمادها؛ خالی = همه نمادهایی که داده دارند")
    mb.add_argument("--csv-dir", default="data_duka")
    mb.add_argument("--start", default=None)
    mb.add_argument("--end", default=None)
    mb.add_argument("--step-hours", type=int, default=8)
    mb.add_argument("--out", default="out_mas/backtest")
    mb.add_argument("--offline", action="store_true", help="فقط کش؛ بدون شبکه")
    mb.set_defaults(fn=cmd_mas_backtest)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
