"""دریافت holdout دست‌نخوردهٔ ۲۰۱۹–۲۰۲۳ برای ۱۰ نماد (بازهٔ قبل از دادهٔ موجود).

- باگ پکیج dukascopy-python==5.0.0 (annotation بدون import ران‌تایم) با builtins دور زده می‌شود.
- fetch_symbol کش فصلی دارد: بخش ۲۰۲۳-۰۶ به بعد از .cache موجود می‌آید؛ فقط فصول قدیمی دانلود می‌شوند.
- خروجی: data_duka/{SYMBOL}_M15.csv بازهٔ ~۲۰۱۹-۰۵ تا ۲۰۲۶-۱۰ (نسخهٔ قبلی در data_duka_backup پشتیبان شد).
"""
import builtins
import requests
builtins.requests = requests
builtins.DataFeed = object

import sys
sys.path.insert(0, "/home/z/my-project/sweep-dev")

from engine.data.fetch_history import fetch_symbol

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD",
           "USDCAD", "NZDUSD", "USDCHF", "EURJPY", "GBPJPY"]

if __name__ == "__main__":
    for s in SYMBOLS:
        fetch_symbol(s, 7.5, "data_duka", verbose=True)
    print("HOLDOUT FETCH DONE")
