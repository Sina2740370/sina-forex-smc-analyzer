"""
نسبت‌دهی اختلاف سیگنال موتور زنده و بک‌تست.
چهار حالت: پایه / آستانه ۰٫۶۰ / بدون Unicorn / هر دو.
فقط تحلیل؛ بدون شبکه، بدون تلگرام، بدون معامله، بدون تغییر فایل‌های موجود.

اجرا (از ریشه‌ی ریپو):
    python -m scripts.parity_attribution --step 8 --tz-offset 3
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager

os.environ.setdefault("FRED_API_KEY", "parity-dummy")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "parity-dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

import pandas as pd  # noqa: E402

from backend import analysis_engine as live  # noqa: E402
from engine.core.config import INSTRUMENTS, EngineConfig  # noqa: E402
from engine.data.providers import TopDownView, build_topdown, load_mt5_csv  # noqa: E402
from engine.strategy.smc_topdown import MIN_LEN  # noqa: E402
from engine.strategy.smc_topdown import analyze as engine_analyze  # noqa: E402

from scripts.parity_check import (  # noqa: E402
    BAR,
    _NOW,
    _ORIG_SESSION,
    _has_enough,
    _session_at_snapshot,
)


def _collect_snapshots(symbol: str, csv_dir: str, tz_offset: float, step: int,
                       start, end, max_n: int):
    path = os.path.join(csv_dir, f"{symbol}_i_M15.csv")
    df15 = load_mt5_csv(path, tz_offset)
    data = build_topdown(df15)
    view = TopDownView(data)
    times = df15["time"]
    t0 = pd.Timestamp(start, tz="UTC") if start else times.iloc[0]
    t1 = pd.Timestamp(end, tz="UTC") if end else times.iloc[-1]

    snaps = []
    for i in range(0, len(df15), step):
        now = times.iloc[i] + BAR
        if now < t0:
            continue
        if now > t1:
            break
        snap = view.at(now)
        if not _has_enough(snap):
            continue
        snaps.append((now, snap))
        if max_n and len(snaps) >= max_n:
            break
    return snaps


def _count_pair(live_dirs, eng_dirs) -> dict:
    c = {
        "n": len(live_dirs),
        "live_sig": 0,
        "eng_sig": 0,
        "same": 0,
        "opposite": 0,
        "live_only": 0,
        "engine_only": 0,
    }
    for l_dir, e_dir in zip(live_dirs, eng_dirs):
        if l_dir is not None:
            c["live_sig"] += 1
        if e_dir is not None:
            c["eng_sig"] += 1
        if e_dir is None and l_dir is None:
            continue
        if e_dir is not None and l_dir is not None:
            if e_dir == l_dir:
                c["same"] += 1
            else:
                c["opposite"] += 1
        elif l_dir is not None:
            c["live_only"] += 1
        else:
            c["engine_only"] += 1
    sig = c["same"] + c["opposite"] + c["live_only"] + c["engine_only"]
    c["agree_pct"] = (c["same"] / sig) if sig else None
    return c


@contextmanager
def _patch_live(mode: str):
    """mode: base | thresh | nouni | both"""
    orig_profile = live.get_symbol_profile
    orig_unicorn = live.detect_unicorn_setup

    def profile_060(symbol: str = None):
        p = orig_profile(symbol)
        return {"min_score": 0.60, "sl_atr_mult": p["sl_atr_mult"]}

    def unicorn_none(*a, **k):
        return None

    try:
        if mode in ("thresh", "both"):
            live.get_symbol_profile = profile_060
        if mode in ("nouni", "both"):
            live.detect_unicorn_setup = unicorn_none
        yield
    finally:
        live.get_symbol_profile = orig_profile
        live.detect_unicorn_setup = orig_unicorn


def _run_modes(symbol: str, snaps: list) -> dict:
    yf_symbol = INSTRUMENTS[symbol].yf
    cfg = EngineConfig()
    modes = ("base", "thresh", "nouni", "both")
    out = {}

    # engine results are independent of live patches
    eng_dirs = []
    for now, snap in snaps:
        _NOW[0] = now
        eng = engine_analyze(snap, cfg, now)
        eng_dirs.append(eng.direction)

    for mode in modes:
        live_dirs = []
        live_details = []  # only for base: (direction, score, reasons)
        with _patch_live(mode):
            for now, snap in snaps:
                _NOW[0] = now
                lv = live.analyze_technical_topdown(snap, symbol=yf_symbol)
                live_dirs.append(lv.get("direction"))
                if mode == "base":
                    live_details.append({
                        "direction": lv.get("direction"),
                        "score": lv.get("score"),
                        "reasons": list(lv.get("reasons") or []),
                    })
        stats = _count_pair(live_dirs, eng_dirs)
        out[mode] = stats
        if mode == "base":
            out["base_live_only_details"] = [
                d for d, e in zip(live_details, eng_dirs)
                if d["direction"] is not None and e is None
            ]
    return out


def _attr_live_only(details: list) -> dict:
    n = len(details)
    if n == 0:
        return {
            "n": 0,
            "score_lt_060_pct": None,
            "unicorn_pct": None,
            "sweep_and_choch_pct": None,
            "near_ob_pct": None,
        }
    score_lt = 0
    unicorn = 0
    sweep_choch = 0
    near_ob = 0
    for d in details:
        sc = d.get("score")
        if sc is not None and abs(float(sc)) < 0.60:
            score_lt += 1
        reasons = " ".join(str(r) for r in (d.get("reasons") or []))
        reasons_l = reasons.lower()
        if "unicorn" in reasons_l:
            unicorn += 1
        has_sweep = "sweep" in reasons_l or "liquidity" in reasons_l
        has_choch = "choch" in reasons_l
        if has_sweep and has_choch:
            sweep_choch += 1
        if "نزدیک ob" in reasons_l or "داخل ob" in reasons_l or "near ob" in reasons_l:
            near_ob += 1
        elif "ob" in reasons_l and ("نزدیک" in reasons or "داخل" in reasons):
            near_ob += 1
    return {
        "n": n,
        "score_lt_060_pct": score_lt / n,
        "unicorn_pct": unicorn / n,
        "sweep_and_choch_pct": sweep_choch / n,
        "near_ob_pct": near_ob / n,
    }


def _print_mode(label: str, c: dict) -> None:
    agree = c["agree_pct"]
    agree_s = f"{agree:.0%}" if agree is not None else "—"
    print(
        f"  [{label}] n={c['n']} | live={c['live_sig']} eng={c['eng_sig']} | "
        f"same={c['same']} opp={c['opposite']} live_only={c['live_only']} eng_only={c['engine_only']} | "
        f"agree={agree_s}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=["EURUSD", "USDJPY", "XAUUSD"])
    ap.add_argument("--csv-dir", default=os.path.join("engine", "data"))
    ap.add_argument("--tz-offset", type=float, default=3.0)
    ap.add_argument("--step", type=int, default=8)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--max", type=int, default=0)
    args = ap.parse_args()

    live.current_session_label = _session_at_snapshot
    try:
        for sym in args.symbols:
            if sym not in INSTRUMENTS:
                print(f"{sym}: نماد ناشناخته", file=sys.stderr)
                continue
            try:
                snaps = _collect_snapshots(
                    sym, args.csv_dir, args.tz_offset, args.step,
                    args.start, args.end, args.max,
                )
            except FileNotFoundError:
                print(f"{sym}: فایل CSV پیدا نشد در {args.csv_dir}", file=sys.stderr)
                continue

            print(f"\n=== {sym} ({len(snaps)} snapshots) ===")
            if not snaps:
                print("هیچ snapshot معتبری نبود.")
                continue

            res = _run_modes(sym, snaps)
            _print_mode("پایه", res["base"])
            _print_mode("آستانه0.60", res["thresh"])
            _print_mode("بدونUnicorn", res["nouni"])
            _print_mode("هر دو", res["both"])

            attr = _attr_live_only(res["base_live_only_details"])
            print(f"  [فقط‌زنده پایه n={attr['n']}]")
            if attr["n"]:
                def pct(x):
                    return f"{x:.0%}" if x is not None else "—"
                print(
                    f"    score|abs|<0.60={pct(attr['score_lt_060_pct'])} | "
                    f"Unicorn در reasons={pct(attr['unicorn_pct'])} | "
                    f"Sweep+CHOCH={pct(attr['sweep_and_choch_pct'])} | "
                    f"نزدیک/داخل OB={pct(attr['near_ob_pct'])}"
                )
            else:
                print("    (موردی نبود)")
    finally:
        live.current_session_label = _ORIG_SESSION
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
