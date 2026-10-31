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

## سوییپ و تست حساسیت پارامترها (v0.4 — ضد overfitting)
    # حساسیت One-At-a-Time: هر پارامتر کلیدی ±۲۰٪ حول پیش‌فرض (پیش‌فرض)
    python -m engine.cli sweep --source csv --csv-dir data --symbols EURUSD XAUUSD --mode oat --pct 20

    # شبکه‌ی صریح (محصول دکارتی):
    python -m engine.cli sweep --source csv --csv-dir data --symbols EURUSD --mode grid \
        --set min_score=0.5,0.55,0.6,0.65,0.7 --set risk_reward=1.5,2,2.5

    # همه‌ی پارامترهای عددی (کندتر):
    python -m engine.cli sweep --symbols EURUSD --mode oat --pct 20 --all-params

خروجی: `out/sweep_<mode>_<hash>.csv` + `.md` + حکم پایداری هر پارامتر.
معیار حکم: اگر امید ریاضی (E[r]) با ±۲۰٪ وارونه شود یا بیش از نصف افت کند → 🔴 شکننده.
لبه‌ی واقعی باید در ±۲۰٪ پارامترها زنده بماند؛ سیگنالِ «تنها با یک مقدار دقیق سودده» overfit است.
موازی‌سازی خودکار با ProcessPoolExecutor (با `--workers` کنترل کنید).

## موتور تجربی V4 (ensemble) — تست‌شده و ردّشده (v0.5)
    python -m engine.cli backtest --source csv --csv-dir data_duka --symbols EURUSD --strategy v4
`quant_v4.py` سه واریانت را روی ۴ سال M15 واقعی EURUSD آزمود:
- v4.1 breakout + sweep خام → E[r]=−0.136 (sweep چاقوگیری می‌کرد)
- v4.2 breakout + پولبک EMA20 → E[r]=−0.147
- v4.3 breakout + mean-reversion RSI(2) → E[r]=−0.094
در مقابل SMC با پیکربندی 0.72/0.24: **E[r]=+0.249، PF=1.45**.
**نتیجه: موتور SMC هسته‌ی اصلی می‌ماند؛ V4 به‌عنوان مرجع آزمایش و پلتفرم ایده‌های بعدی نگه داشته می‌شود**
(فیلتر رژیم روزانه و صدک ATR آن قابل استفاده در فیلترهای آینده است).
درس‌ها: M15 فارکس intraday mean-revert است؛ ادامه‌ی روند کوتاه‌مدت و sweep بدون تأیید ساختاری ضرر می‌دهد.

## دانلود دیتای چندساله بدون MT5 (v0.3)
    pip install dukascopy-python
    python -m engine.cli fetch --symbols XAUUSD EURUSD GBPUSD USDJPY --years 4 --out data_duka
    python -m engine.cli report --source csv --csv-dir data_duka --symbols XAUUSD EURUSD GBPUSD USDJPY --walk-forward
دیتا Bid و UTC است (tz-offset لازم نیست) و XAUUSD اسپات واقعی است.
