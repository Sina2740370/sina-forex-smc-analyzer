# تغییرات فاز ۲ — ممیزی عمیق و ارتقاء موتور کوانت (v2)

تاریخ: اکتبر ۲۰۲۶ | نتیجه تست‌سوییت: **۹۹ از ۹۹ پاس** (۸۲ قبلی + ۱۷ تست رگرسیون جدید)

## ۱) فیکس‌های P1 بک‌اند و موتور

| شناسه | فایل | شرح |
|---|---|---|
| E-1 | `engine/backtest/runner.py` | کول‌داون بک‌تست حالا از زمان **بسته‌شدن** معامله شمرده می‌شود (هم‌راستا با `signal_gate` لایو). قبلاً از زمان ورود شمرده می‌شد و بک‌تست خوش‌بینانه‌تر از لایو بود. آمار بک‌تست قبلی قابل‌اتکا نبود — اعداد جدید مبنا هستند. |
| E-2 | `engine/tools/risk.py` + `engine/tools/tgparse.py` + `backend/telegram_bot.py` | کراس‌های `EURJPY / GBPJPY / AUDJPY / EURGBP` به `SPECS` اضافه شدند با پشتیبانی `cross_rates` (نرخ ارز کوت به دلار). دستور `/size` گزینه‌های `jpy=` / `gbp=` گرفت: `/size EURJPY 155,00 154,50 jpy=155.5`. بدون نرخ کراس، خطای واضح فارسی می‌دهد (بدون محاسبه غلط خاموش). |
| E-3 | `backend/config.py` + `backend/analysis_engine.py` + `.env.example` | وزن بی‌اثر لایه خبر (همیشه صفر) حذف شد؛ وزن‌ها: تکنیکال ۰.۷۰ / فاندامنتال ۰.۲۰ / حافظه ۰.۱۰. خبر فقط **فیلتر سخت ایمنی** است. عبارت گمراه‌کننده «فیلتر خبر» از مسیر امتیاز خارج شد. |
| E-4 | `engine/data/fetch_history.py` | کش CSV با ایندکس tz-naive دیگر `tz_convert` را کرش نمی‌کند (`_read_cache_csv` با localize شرطی). |
| E-5 | `backend/scheduler.py` + `backend/database.py` | نوتیف بسته‌شدن با واکشی هدفمند `get_signals_by_ids` — دیگر اعلان WIN/LOSS با بیش از ۵۰ سیگنال گم نمی‌شود. |

## ۲) فیکس‌های P1 فرانت‌اند

| شناسه | فایل | شرح |
|---|---|---|
| F-1 | `frontend/index.html` + `backend/main.py` | ارجاع صریح `<script src="/static/tg-auth.js">` در head؛ همه درخواست‌های `/api/` حالا هدر `Authorization: Bearer` می‌فرستند (دیگر وابسته به تزریق سروری نیستند). |
| F-2 | `frontend/app.js`، `frontend/style.css` | ۱۵۳۱ خط کد مرده‌ی UI قدیمی حذف شد. |
| F-3 | `frontend/tg-auth.js` + `index.html` | کلید ادمین یکپارچه روی `localStorage['sina_admin_key']` با مهاجرت دوطرفه از کلیدهای قدیمی. |
| F-4 | `frontend/tg-auth.js` | انتظار برای `initData` تلگرام (۳ ثانیه) + بنر خطای واضح؛ `window.prompt` فقط خارج از تلگرام. |
| F-5 | `frontend/sw.js` | کش shell در install + cache-first با fallback شبکه؛ `/api/` همیشه مستقیم به شبکه. |
| F-6 | `frontend/index.html` | دو الگوی باقی‌مانده‌ی XSS (onclick و className) با `esc()` فرار داده شدند. |

## ۳) فیکس‌های P2

- **پلتو سوئینگ** (`engine/core/smc.py` + `backend/analysis_engine.py`): دو سوئینگ هم‌قیمت با پنجره‌های هم‌پوشان یک‌بار شمرده می‌شوند؛ دیگر ساختار به‌اشتباه RANGING نمی‌رود.
- **market_regime روی داده کوتاه**: پنجره تاریخی ATR به‌اندازه داده موجود کوچک می‌شود؛ دیگر همیشه LOW_VOL نیست.
- **chart_renderer**: فرمت اعشار بر اساس نماد (JPY سه رقم، طلا دو رقم)؛ نواحی OB/FVG صفر چاپ نمی‌شوند.
- **SQLite WAL** (`backend/database.py`): نوشتن هم‌زمان دیگر `database is locked` نمی‌دهد.
- **گارد تعطیلی بازار** (`engine/tools/signal_gate.py` + `backend/scheduler.py`): از جمعه ۱۷:۰۰ نیویورک تا یکشنبه ۱۸:۰۰ نیویورک، اسکن و صدور سیگنال بسته است (`market_closed`).
- **کد مرده حذف‌شده**: `is_duplicate_signal`، `get_memory_weights`، `fetch_multi_timeframe`.
- **favicon**: پاسخ 204 به‌جای 404 نویز لاگ.
- **امنیت**: کلید واقعی FRED از `.env.example` حذف شد (⚠️ کلید قبلی را در پنل FRED باطل کن).

## ۴) ارتقاء موتور کوانت و امتیازدهی (کاهش خطای معامله)

### موتور هسته (بک‌تست‌پذیر، بدون lookahead)
- **رژیم بازار تأثیرگذار شد**: رژیم `TRENDING` هم‌جهت با bias کامل HTF → بونوس `w_regime_align=+0.06`؛ رژیم `RANGING_HIGH_VOL` (نوسان بی‌جهت) → جریمه `w_regime_chop=-0.08`. (خروجی بک‌تست: معاملات با بونوس رژیم: انتظار +0.63R در برابر +0.10R بدون آن.)
- **جریمه خارج سشن**: ورود خارج سشن لندن/نیویورک → `-0.04`. (بک‌تست: معاملات خارج سشن انتظار **0.28R−** با win rate ۲۱٪ داشتند — دقیقاً منبع اصلی استاپ‌های کاذب.)
- **گرید کیفیت ستاپ**: هر `Decision` حالا `grade` دارد (A+ / A / B بر اساس قدرت امتیاز) و در معاملات بک‌تست و ویژگی‌ها ثبت می‌شود — مبنای مدیریت حجم.

### لایه لایو (backend)
- **وتوی اسپرد**: فاصله SL باید حداقل **۳ برابر اسپرد معمول نماد** باشد (`MIN_RISK_SPREAD_MULT=3`)؛ استاپ داخل نویز اسپرد مسدود می‌شود.
- **رژیم و سشن در امتیاز لایو**: همان جریمه‌های رژیم/سشن موتور هسته در `analyze_technical_topdown` اعمال شد (دو کد یکی شد).
- **فعال‌سازی Super Quant Score**: کد مرده قبلی حالا در خروجی `full_analysis` محاسبه و منتشر می‌شود (`super_quant_score` و `super_grade` در API و پیام تلگرام).
- **پیام تلگرام غنی‌تر**: رژیم بازار + گرید ستاپ + گرید کوانت در پیام سیگنال.

## ۵) اعتبارسنجی بک‌تست (داده واقعی M15 — EURUSD/USDJPY/GBPUSD/XAUUSD)

```
n=48 | win_rate=45.8% (CI 32.6–59.7) | breakeven=35.7%
expectancy=+0.26R | PF=1.52 | total=+12.57R
```
- گیت «آماده حساب واقعی»: **آماده نیست** (n<100 و بازه اطمینان صفر را شامل می‌شود) — با داده ۲۰ روزه طبیعی است؛ پیش از معامله واقعی بک‌تست بلندمدت (`python -m engine.cli fetch --years 4`) اجرا کن.
- مهم‌تر از سطح مطلق: **ویژگی‌های جدید دقیقاً معاملات بد را جدا می‌کنند** (جدول feature در `out/report_*.txt`).

## ۶) چک‌لیست پیش از استقرار

1. کلید FRED قبلی را revoke کن و کلید جدید فقط در Render ست کن.
2. `ADMIN_KEY` و `PUBLIC_URL` در Render ست باشند (webhook تلگرام وابسته به آن است).
3. بک‌تست ۲ سال+ اجرا کن: `python -m engine.cli fetch --symbols EURUSD USDJPY GBPUSD XAUUSD --years 4 --out data_duka` سپس `python -m engine.cli report --source csv --csv-dir data_duka --symbols ... --walk-forward`.
4. در بروکر واقعی، اسپردهای `INSTRUMENTS` و `SPREAD` را با اسپرد واقعی خودت جایگزین کن.
