"""فاز ۱ (مکمل تاب‌آوری) — کندل‌های ساعتی روزانهٔ BID/ASK از Dukascopy.

۴ نماد × ۵ روز × ۲ فایل = ۴۰ فایل bi5 (در برابر ۴۸۰ فایل تیک) — پروفایل سریع اسپرد
نشستی از اختلاف کلوز ASK/BID در هر ساعت. ساختار رکورد ۲۴ بایتی:
ms، open، close، low، high (int × scale) و volume (float32).
خروجی: out_mas/phase1_spread_h1.json
"""
import json
import lzma
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, "/home/z/my-project/sweep-dev")
os.chdir("/home/z/my-project/sweep-dev")

import numpy as np
import pandas as pd

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
DAYS = [26, 27, 28, 29, 30]
MONTH_IDX, YEAR = 9, 2026
BASE = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/{side}_candles_hour_1.bi5"
OUT = "out_mas/phase1_spread_h1.json"
SESSIONS = {"asia": (0, 7), "london": (7, 13), "newyork": (13, 21), "off": (21, 24)}


def log(m):
    print(f"[{datetime.now(timezone.utc) if False else datetime.utcnow():%H:%M:%S}] {m}", flush=True)


def fetch(url, tries=6):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            wait = 80 if e.code in (429, 503) else 20
            log(f"  HTTP {e.code} → {wait}s ({a + 1}/{tries})")
            time.sleep(wait)
        except Exception as e:
            log(f"  {e} → 20s")
            time.sleep(20)
    return "FAILED"


def decode_candles(raw, scale):
    rec = np.frombuffer(lzma.decompress(raw),
                        dtype=np.dtype([("ms", ">u4"), ("o", ">u4"), ("c", ">u4"),
                                        ("l", ">u4"), ("h", ">u4"), ("v", ">f4")]))
    return pd.DataFrame({
        "ms": rec["ms"].astype(np.int64),
        "open": rec["o"] / scale, "close": rec["c"] / scale,
        "low": rec["l"] / scale, "high": rec["h"] / scale,
    })


def detect_scale(raw, ref):
    if ref != ref:
        return None
    rec = np.frombuffer(lzma.decompress(raw),
                        dtype=np.dtype([("ms", ">u4"), ("o", ">u4"), ("c", ">u4"),
                                        ("l", ">u4"), ("h", ">u4"), ("v", ">f4")]))
    implied = np.median(rec["c"].astype(float)) / ref
    for s in (1e5, 1e4, 1e3, 1e2):
        if abs(np.log10(implied / s)) < 0.05:
            return s
    return None


def m15_close_ref(sym):
    df = pd.read_csv(f"data_duka/{sym}_M15.csv", parse_dates=["time"])
    df["d"] = df["time"].dt.date
    df["h"] = df["time"].dt.hour
    return df.groupby(["d", "h"])["close"].first()


def main():
    report = {}
    for sym in SYMBOLS:
        fallback = 1e3 if ("JPY" in sym or sym == "XAUUSD") else 1e5
        ref = m15_close_ref(sym)
        rows = []
        for day in DAYS:
            candles = {}
            for side in ("BID", "ASK"):
                url = BASE.format(sym=sym, y=YEAR, m=MONTH_IDX, d=day, side=side)
                raw = fetch(url)
                if raw is None or raw == "FAILED":
                    log(f"{sym} {day} {side}: ندارد/ناموفق")
                    continue
                r = ref.get((datetime(YEAR, 10, day).date(), 10), np.nan)
                scale = detect_scale(raw, r) or fallback
                df = decode_candles(raw, scale)
                df["hour"] = df["ms"] // 3_600_000
                candles[side] = df.set_index("hour")["close"]
                time.sleep(8)
            if "BID" in candles and "ASK" in candles:
                j = pd.concat([candles["BID"].rename("bid"),
                               candles["ASK"].rename("ask")], axis=1).dropna()
                j["spread"] = j["ask"] - j["bid"]
                j["day"] = day
                rows.append(j.reset_index())
        if not rows:
            report[sym] = {"error": "no data"}
            continue
        big = pd.concat(rows, ignore_index=True)
        big["rel_bp"] = big["spread"] / ((big["ask"] + big["bid"]) / 2) * 1e4
        def sess(h):
            for n, (a, b) in SESSIONS.items():
                if a <= h < b:
                    return n
        big["session"] = big["hour"].map(sess)
        entry = {"n_hours": int(len(big)),
                 "rel_spread_median_bp": float(big["rel_bp"].median()),
                 "by_session": {s: float(g["rel_bp"].median())
                                for s, g in big.groupby("session")}}
        report[sym] = entry
        log(f"== {sym}: {entry['n_hours']} ساعت | میانه {entry['rel_spread_median_bp']:.2f} bp | "
            + " | ".join(f"{k}: {v:.2f}" for k, v in entry["by_session"].items()))
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)
    log(f"ذخیره شد: {OUT}")


if __name__ == "__main__":
    main()
