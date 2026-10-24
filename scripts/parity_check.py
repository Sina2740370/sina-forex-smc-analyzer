"""
مقایسه‌ی موتور زنده (backend.analysis_engine) و موتور بک‌تست (engine.strategy.smc_topdown)
روی دقیقاً همان کندل‌های بسته‌شده.

فقط تحلیل و گزارش: بدون شبکه، بدون تلگرام، بدون معامله، بدون تغییر هیچ تنظیمی.
موتور زنده فقط لایه‌ی تکنیکال (analyze_technical_topdown) را می‌دهد؛ فاندامنتال/خبر/حافظه
شامل نیستند و با داده‌ی قیمت قابل بازسازی نیستند.

اجرا (از ریشه‌ی ریپو):
    python -m scripts.parity_check --symbols EURUSD --step 8 --tz-offset 3
    python -m scripts.parity_check --symbols EURUSD --step 8 --server-tz ny_close
"""
import argparse
import os
import statistics
import sys

# backend.config متغیرهای محیطی را اجباری می‌کند؛ این‌ها فقط برای import هستند و هیچ اتصالی ساخته نمی‌شود.
os.environ.setdefault("FRED_API_KEY", "parity-dummy")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "parity-dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

import pandas as pd  # noqa: E402

from backend import analysis_engine as live  # noqa: E402
from engine.core import smc  # noqa: E402
from engine.core.config import INSTRUMENTS, EngineConfig  # noqa: E402
from engine.data.providers import TopDownView, build_topdown, load_mt5_csv  # noqa: E402
from engine.strategy.smc_topdown import MIN_LEN  # noqa: E402
from engine.strategy.smc_topdown import analyze as engine_analyze  # noqa: E402

BAR = pd.Timedelta(minutes=15)

# موتور زنده برچسب سشن را از ساعت سیستم می‌گیرد؛ برای مقایسه‌ی تاریخی باید روی زمان هر snapshot باشد.
_NOW = [None]
_ORIG_SESSION = live.current_session_label


def _session_at_snapshot(dt=None):
    return _ORIG_SESSION(_NOW[0].to_pydatetime())


def _has_enough(data: dict) -> bool:
    return all(tf in data and len(data[tf]) >= n for tf, n in MIN_LEN.items())


def _risk_atr4h(entry, sl, atr4h):
    if entry is None or sl is None or not atr4h:
        return None
    return abs(entry - sl) / atr4h


def _med(xs):
    return round(statistics.median(xs), 2) if xs else None


def run_symbol(symbol: str, csv_dir: str, tz_offset: float, step: int,
               start, end, max_n: int, server_tz=None) -> dict:
    path = os.path.join(csv_dir, f"{symbol}_i_M15.csv")
    df15 = load_mt5_csv(path, tz_offset_hours=tz_offset, server_tz=server_tz)
    data = build_topdown(df15)
    view = TopDownView(data)
    cfg = EngineConfig()
    yf_symbol = INSTRUMENTS[symbol].yf
    times = df15["time"]

    t0 = pd.Timestamp(start, tz="UTC") if start else times.iloc[0]
    t1 = pd.Timestamp(end, tz="UTC") if end else times.iloc[-1]

    c = {"n": 0, "both_none": 0, "same": 0, "opposite": 0, "live_only": 0, "engine_only": 0}
    risk_live, risk_eng = [], []
    for i in range(0, len(df15), step):
        now = times.iloc[i] + BAR
        if now < t0:
            continue
        if now > t1:
            break
        snap = view.at(now)
        if not _has_enough(snap):
            continue

        _NOW[0] = now
        eng = engine_analyze(snap, cfg, now)
        lv = live.analyze_technical_topdown(snap, symbol=yf_symbol)

        c["n"] += 1
        e_dir, l_dir = eng.direction, lv.get("direction")
        if e_dir is None and l_dir is None:
            c["both_none"] += 1
        elif e_dir is not None and l_dir is not None:
            c["same" if e_dir == l_dir else "opposite"] += 1
            if e_dir == l_dir:
                atr4 = smc.atr(snap["4h"])
                a = _risk_atr4h(lv.get("price"), lv.get("sl"), atr4)
                b = _risk_atr4h(eng.entry, eng.sl, atr4)
                if a is not None and b is not None:
                    risk_live.append(a)
                    risk_eng.append(b)
        elif l_dir is not None:
            c["live_only"] += 1
        else:
            c["engine_only"] += 1
        if max_n and c["n"] >= max_n:
            break

    c["risk_live_med"], c["risk_eng_med"] = _med(risk_live), _med(risk_eng)
    c["risk_live_max"] = round(max(risk_live), 2) if risk_live else None
    c["risk_eng_max"] = round(max(risk_eng), 2) if risk_eng else None
    return c


def print_report(symbol: str, c: dict) -> None:
    n = c["n"]
    sig = c["same"] + c["opposite"] + c["live_only"] + c["engine_only"]
    print(f"\n=== {symbol} ===")
    if n == 0:
        print("هیچ snapshot معتبری نبود (داده‌ی کافی برای همه‌ی تایم‌فریم‌ها وجود ندارد).")
        return
    print(f"snapshots: {n} | هیچ‌کدام سیگنال نداد: {c['both_none']}")
    print(f"هر دو هم‌جهت: {c['same']} | هر دو مخالف‌جهت: {c['opposite']}")
    print(f"فقط زنده: {c['live_only']} | فقط بک‌تست: {c['engine_only']}")
    if sig:
        print(f"توافق جهت در میان snapshotهایی که حداقل یکی سیگنال داده: {c['same'] / sig:.0%} ({c['same']}/{sig})")
    else:
        print("هیچ‌کدام در این بازه سیگنالی نداد.")
    if c["risk_live_med"] is not None:
        print(f"فاصله‌ی SL در هم‌جهت‌ها (برحسب ATR 4h): زنده median={c['risk_live_med']} max={c['risk_live_max']} | "
              f"بک‌تست median={c['risk_eng_med']} max={c['risk_eng_max']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"])
    ap.add_argument("--csv-dir", default=os.path.join("engine", "data"))
    ap.add_argument("--tz-offset", type=float, default=3.0, help="اختلاف ساعت سرور MT5 با UTC (اگر server-tz نباشد)")
    ap.add_argument("--server-tz", choices=["ny_close"], default=None,
                    help="ny_close = LiteFinance (NY wall + 7h with US DST)")
    ap.add_argument("--step", type=int, default=8, help="هر چند کندل ۱۵م یک snapshot (۸ = هر ۲ ساعت)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--max", type=int, default=0, help="حداکثر تعداد snapshot برای هر نماد (۰ = بدون سقف)")
    args = ap.parse_args()

    live.current_session_label = _session_at_snapshot
    try:
        for sym in args.symbols:
            if sym not in INSTRUMENTS:
                print(f"{sym}: نماد ناشناخته", file=sys.stderr)
                continue
            try:
                res = run_symbol(sym, args.csv_dir, args.tz_offset, args.step,
                                 args.start, args.end, args.max,
                                 server_tz=args.server_tz)
            except FileNotFoundError:
                print(f"{sym}: فایل CSV پیدا نشد در {args.csv_dir}", file=sys.stderr)
                continue
            print_report(sym, res)
    finally:
        live.current_session_label = _ORIG_SESSION
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
