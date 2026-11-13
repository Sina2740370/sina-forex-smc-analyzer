# دریافت داده ۴ ساله برای بک‌تست معتبر

## پیش‌نیاز
```bash
pip install dukascopy-python
```

## روش سریع
```bash
# از ریشه پروژه
./scripts/fetch_4y.sh data_duka_4y "EURUSD USDJPY GBPUSD"

# یا دستی
python -m engine.cli fetch --symbols EURUSD USDJPY GBPUSD --years 4 --out data_duka_4y
```

## بعد از دانلود
```bash
# گزارش کامل + walk-forward
python -m engine.cli report --source csv --csv-dir data_duka_4y \
  --symbols EURUSD USDJPY GBPUSD --walk-forward

# فقط بک‌تست
python -m engine.cli backtest --source csv --csv-dir data_duka_4y \
  --symbols EURUSD USDJPY GBPUSD
```

## نکات
- داده Dukascopy UTC و اسپات واقعی است.
- فایل‌ها به صورت `{SYMBOL}_M15.csv` ذخیره می‌شوند.
- برای مقایسه نسخه‌ها، همان داده را نگه دارید.
