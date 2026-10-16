# Sina Quant Engine — معماری و قراردادهای اجرا

این موتور، مرجع واحد تصمیم تکنیکال SMC برای تحلیل زنده و بک‌تست است. لایه‌ی Backend یک adapter برای قراردادهای API قدیمی دارد؛ تصمیم جهت، Entry، SL، TP و امضای تنظیمات از `engine.strategy.smc_topdown` می‌آید. امتیازهای فاندامنتال/خبر/حافظه در Live فقط تشخیصی‌اند؛ خبر ناایمن می‌تواند سیگنال را مسدود کند، اما امتیاز زمینه‌ای جهت موتور را عوض نمی‌کند.

## قرارداد داده و تصمیم

- زمان‌ها در موتور UTC و timezone-aware هستند.
- `TopDownView` فقط کندل‌هایی را نمایش می‌دهد که زمان بسته‌شدنشان از لحظه‌ی تصمیم گذشته باشد.
- CSV متاتریدر می‌تواند از offset ثابت یا نام منطقه‌ی زمانی IANA استفاده کند؛ زمان‌های aware به UTC تبدیل می‌شوند، ترکیب timestampهای aware و naive رد می‌شود.
- هر تصمیم یک `config_hash` دارد. این hash در معاملات بک‌تست و سیگنال‌های Backend ذخیره می‌شود.
- داده‌ی Live با کندل 15 دقیقه‌ای کهنه یا زمان آینده مسدود می‌شود. کندل در حال تشکیل به‌عنوان کندل بسته‌شده استفاده نمی‌شود.

## قرارداد اجرای معامله

- سیگنال در بسته‌شدن کندل بررسی می‌شود؛ ورود شبیه‌سازی‌شده در بازشدن کندل بعدی انجام می‌شود.
- BUY روی سمت ASK و SELL روی سمت BID وارد می‌شود؛ خروج‌ها با سمت اجرایی مخالف بررسی می‌شوند.
- اگر فاصله‌ی Open بعدی از قیمت تصمیم از `max_entry_slip_r` بیشتر شود، ورود رد می‌شود.
- اگر SL و TP در یک کندل لمس شوند، SL اولویت دارد.
- گپ نامطلوب از SL با قیمت Open همان کندل مدل می‌شود؛ معامله‌ی باز در پایان بازه با قیمت بازار و هزینه‌ی اسپرد mark-to-market می‌شود.
- ردیاب سیگنال Backend و بک‌تست از تابع مشترک بررسی SL/TP استفاده می‌کنند. این پروژه اتصال خودکار به بروکر یا حساب واقعی را در این لایه فعال نمی‌کند.

## داده‌ی طلا و هزینه‌ها

- `engine.cli fetch` از تاریخچه‌ی Dukascopy برای `XAUUSD` اسپات استفاده می‌کند و هر دو سمت BID/ASK را می‌گیرد.
- OHLC midpoint از میانگین کندل‌های BID و ASK ساخته می‌شود؛ این midpoint تیک‌به‌تیک دقیق نیست و محدودیت آن در کد مستند شده است.
- `GC=F` نماد فیوچرز COMEX است، نه اسپات XAUUSD. گزارش‌ها منبع را مشخص می‌کنند و سیگنال Live مبتنی بر این proxy به‌طور پیش‌فرض مسدود است.
- اسپردهای تنظیمات، فرض مدل هستند؛ تا زمانی که quote واقعی بروکر به موتور وصل نشده، آن‌ها را اسپرد زنده تلقی نکن.

## اجرا و آزمون

```bash
python -m pip install -r requirements.txt
python -m pytest -q engine/test_engine.py tests/test_engine.py backend/test_signal_lifecycle.py backend/test_analysis_adapter.py
python -m engine.cli fetch --symbols GBPJPY XAUUSD --years 7 --out data_duka
python -m engine.cli report --source csv --csv-dir data_duka --symbols GBPJPY XAUUSD --start 2019-12-01 --end 2022-11-01 --out reports
```

برای CSV متاتریدر با ساعت ثابت:
```bash
python -m engine.cli backtest --source csv --csv-dir data --symbols EURUSD --tz-offset 3
```

برای ساعت بروکر با DST:
```bash
python -m engine.cli backtest --source csv --csv-dir data --symbols EURUSD --broker-timezone Europe/Helsinki
```

گزارش‌های random baseline و walk-forward ابزارهای تشخیصی‌اند، نه تضمین سودآوری. در walk-forward فقط معاملاتی که تا پایان پنجره بسته شده‌اند در نتیجه‌ی همان پنجره حساب می‌شوند.
