"""فاز ۱ — دانلود تیک واقعی Dukascopy برای ۴ نماد (یک هفتهٔ کاری کامل).

- فایل ساعتی bi5 (LZMA) از datafeed.dukascopy.com؛ مؤدبانه: ۸ ثانیه فاصله،
  در صورت 429 → ۸۰ ثانیه انتظار (حداکثر ۶ تلاش).
- قابل‌ازسرگیری: هر ساعتِ دانلودشده در کش CSV ذخیره و در اجرای بعدی رد می‌شود.
- مقیاس قیمت با مقایسهٔ میانگین تیک ساعت با اولین کلوز M15 همان ساعت از data_duka
  خودکار تشخیص داده می‌شود (۱e5 / 1e4 / 1e3 / 1e2)؛ پیش‌فرض نماد بدون مرجع:
  1e3 برای جفت‌های JPY و XAUUSD، 1e5 برای بقیه.
- خروجی: data_ticks/{SYM}_ticks_all.csv.gz + گزارش پوشش و اسپرد: out_mas/phase1_ticks.json
"""
import json
import lzma
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/z/my-project/sweep-dev")
os.chdir("/home/z/my-project/sweep-dev")

import numpy as np
import pandas as pd

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
DAYS = [26, 27, 28, 29, 30]          # دوشنبه تا جمعهٔ هفتهٔ کامل قبلی: ۲۰۲۶-۱۰-۲۶..۳۰
MONTH_IDX = 9                        # شمارهٔ ماه ۰-مبهم Dukascopy = اکتبر
YEAR = 2026
OUT_DIR = "data_ticks"
LOG = "out_mas/tick_fetch.log"
RESULT = "out_mas/phase1_ticks.json"

BASE = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/{h:02d}h_ticks.bi5"
TICK_DTYPE = np.dtype([("ms", ">u4"), ("ask_raw", ">u4"), ("bid_raw", ">u4"),
                       ("ask_vol", ">f4"), ("bid_vol", ">f4")])


def log(msg):
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def fetch_url(url, tries=6):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                    # ساعت بدون تیک (تعطیلی) — طبیعی
            wait = 80 if e.code == 429 else 20
            log(f"  HTTP {e.code} → انتظار {wait}s (تلاش {attempt + 1}/{tries})")
            time.sleep(wait)
        except Exception as e:
            log(f"  خطا {e} → انتظار 20s")
            time.sleep(20)
    return "FAILED"


def _unlzma(raw):
    if raw[:5] == b"?<xml" or raw[:1] == b"<":
        raise ValueError("پاسخ خطا، داده نیست")
    try:
        return lzma.decompress(raw)
    except lzma.LZMAError:
        return raw                       # از قبل خام


def decode_hour(raw, scale):
    """۲۰ بایت رکورد: ms، ask_raw، bid_raw (int) و دو حجم float32."""
    rec = np.frombuffer(_unlzma(raw), dtype=TICK_DTYPE)
    n = rec.size
    out = np.empty((n, 5), dtype=float)
    out[:, 0] = rec["ms"]
    out[:, 1] = rec["ask_raw"] / scale
    out[:, 2] = rec["bid_raw"] / scale
    out[:, 3] = rec["ask_vol"]
    out[:, 4] = rec["bid_vol"]
    return out


def detect_scale(raw, ref_price, fallback):
    if ref_price != ref_price:
        return fallback
    rec = np.frombuffer(_unlzma(raw), dtype=TICK_DTYPE)
    mid_int = (rec["ask_raw"].astype(float) + rec["bid_raw"].astype(float)) / 2
    implied = np.median(mid_int) / ref_price
    for scale in (1e5, 1e4, 1e3, 1e2):
        if abs(np.log10(implied / scale)) < 0.05:
            return scale
    return fallback


def m15_ref_table(sym):
    """اولین کلوز M15 هر (روز، ساعت) به‌عنوان مرجع تشخیص مقیاس."""
    df = pd.read_csv(f"data_duka/{sym}_M15.csv", parse_dates=["time"])
    df["d"] = df["time"].dt.date
    df["h"] = df["time"].dt.hour
    return df.groupby(["d", "h"])["close"].first()


def hour_csv_path(sym, day, hour):
    return f"{OUT_DIR}/{sym}_{YEAR}10{day:02d}_{hour:02d}.csv"


def load_cached(path):
    if os.path.exists(path):
        try:
            arr = np.loadtxt(path, delimiter=",", skiprows=1)
            return arr.reshape(1, -1) if arr.ndim == 1 else arr
        except Exception:
            return None
    return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs("out_mas", exist_ok=True)
    report = {"symbols": {}, "window": f"{YEAR}-10-{DAYS[0]:02d}..{YEAR}-10-{DAYS[-1]:02d} UTC",
              "expected_market_hours": len(DAYS) * 24}
    t0 = datetime(YEAR, 10, DAYS[0], tzinfo=timezone.utc)

    for sym in SYMBOLS:
        fallback = 1e3 if ("JPY" in sym or sym == "XAUUSD") else 1e5
        ref = m15_ref_table(sym)
        hours_ok = hours_missing = hours_failed = 0
        spreads, mids, chunks, stamps = [], [], [], []
        for didx, day in enumerate(DAYS):
            for hour in range(24):
                path = hour_csv_path(sym, day, hour)
                cached = load_cached(path)
                if cached is not None:
                    arr = cached
                    hours_ok += 1
                else:
                    url = BASE.format(sym=sym, y=YEAR, m=MONTH_IDX, d=day, h=hour)
                    raw = fetch_url(url)
                    if raw is None:
                        hours_missing += 1
                        log(f"{sym} {day:02d}/{hour:02d}: بدون فایل")
                        continue
                    if raw == "FAILED":
                        hours_failed += 1
                        log(f"{sym} {day:02d}/{hour:02d}: ناموفق")
                        continue
                    scale = detect_scale(raw, ref.get((datetime(YEAR, 10, day).date(), hour), np.nan), fallback)
                    arr = decode_hour(raw, scale)
                    np.savetxt(path, arr, delimiter=",",
                               header="ms,ask,bid,ask_vol,bid_vol", comments="", fmt="%.6f")
                    time.sleep(8)              # فاصلهٔ مؤدبانه بین دانلودها
                    hours_ok += 1
                base_ms = t0.timestamp() + didx * 86400 + hour * 3600
                stamps.append(pd.to_datetime(arr[:, 0] + base_ms * 1000, unit="ms", utc=True))
                spreads.append(arr[:, 1] - arr[:, 2])
                mids.append((arr[:, 1] + arr[:, 2]) / 2)
                chunks.append(arr)
        all_sp = np.concatenate(spreads)
        all_mid = np.concatenate(mids)
        rel = all_sp / all_mid
        report["symbols"][sym] = {
            "hours_ok": hours_ok, "hours_missing": hours_missing,
            "hours_failed": hours_failed,
            "coverage_pct": round(hours_ok / (len(DAYS) * 24) * 100, 2),
            "n_ticks": int(all_mid.size),
            "spread_median": float(np.median(all_sp)),
            "spread_p90": float(np.percentile(all_sp, 90)),
            "rel_spread_median_bp": float(np.median(rel) * 1e4),
        }
        log(f"== {sym}: پوشش {report['symbols'][sym]['coverage_pct']}% | "
            f"تیک {all_mid.size} | اسپرد میانه {report['symbols'][sym]['spread_median']:.5f} | "
            f"ناموفق {hours_failed}")
        df = pd.DataFrame({"time_utc": pd.DatetimeIndex(pd.concat(stamps)),
                           "ask": all_mid + all_sp / 2, "bid": all_mid - all_sp / 2,
                           "ask_vol": np.concatenate([c[:, 3] for c in chunks]),
                           "bid_vol": np.concatenate([c[:, 4] for c in chunks])})
        df.to_csv(f"{OUT_DIR}/{sym}_ticks_all.csv.gz", index=False, compression="gzip")

    with open(RESULT, "w") as f:
        json.dump(report, f, indent=2)
    log(f"گزارش ذخیره شد: {RESULT}")


if __name__ == "__main__":
    main()
