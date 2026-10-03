# Sina Quant Engine (v0.1)

هسته‌ی خالص + دیتا + بک‌تست. بخش `backend/` و `frontend/` فعلاً دست‌نخورده‌اند.

## اجرا (روی کامپیوتر/Codespaces؛ روی Render لازم نیست)
    pip install -r requirements.txt -r requirements-dev.txt
    pytest -q
    python -m engine.cli backtest --source yfinance --symbols EURUSD
    python -m engine.cli backtest --source csv --csv-dir data --symbols EURUSD XAUUSD --tz-offset 3
    python -m engine.cli scan --symbols EURUSD GBPUSD

CSV: خروجی Export Bars متاتریدر با نام `EURUSD_M15.csv` (حداقل ~۶ ماه، ترجیحاً ۱–۲ سال).

## گزارش تشخیصی (v0.2)
    python -m engine.cli report --source csv --csv-dir data --symbols EURUSD XAUUSD --tz-offset 3 --walk-forward
خروجی: مقایسه با ورود تصادفی (p-value)، پایداری زمانی، عملکرد هر ویژگی، و walk-forward.

## دانلود دیتای چندساله بدون MT5 (v0.3)
    pip install dukascopy-python
    python -m engine.cli fetch --symbols XAUUSD EURUSD GBPUSD USDJPY --years 4 --out data_duka
    python -m engine.cli report --source csv --csv-dir data_duka --symbols XAUUSD EURUSD GBPUSD USDJPY --walk-forward
دیتا Bid و UTC است (tz-offset لازم نیست) و XAUUSD اسپات واقعی است.
