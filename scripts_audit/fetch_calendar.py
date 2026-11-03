"""فاز ۱ — دانلود اسنپ‌شات تقویم اقتصادی ForexFactory (هفتهٔ جاری با زمان دقیق انتشار).

خروجی: data_cal/ff_calendar_{تاریخ}.json
قاعده: هر اسنپ‌شات با تاریخ گرفتنش مهر می‌شود؛ ویژگی‌های کوزال در aee/calendar.py
فقط رویدادهای بعد از لحظهٔ اسنپ‌شات را «زمان‌بندی‌شده» می‌شناسند.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/z/my-project/sweep-dev")
os.chdir("/home/z/my-project/sweep-dev")

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
OUT_DIR = "data_cal"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = f"{OUT_DIR}/ff_calendar_{today}.json"
    if os.path.exists(out_path):
        print(f"از قبل هست: {out_path}")
        return
    for attempt in range(5):
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                data = r.read()
            events = json.loads(data)
            assert isinstance(events, list) and len(events) > 0
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=1)
            high = sum(1 for e in events if e.get("impact") == "High")
            print(f"ذخیره شد: {out_path} | {len(events)} رویداد ({high} پرباثر)")
            return
        except Exception as e:
            print(f"تلاش {attempt + 1}/5 ناموفق: {e}")
            time.sleep(30)
    raise SystemExit("دانلود تقویم ناموفق ماند")


if __name__ == "__main__":
    main()
