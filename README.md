# 📊 SMC Forex Analyzer

تحلیل تکنیکال حرفه‌ای فارکس با ترکیب سبک‌های **ICT**، **SMC** و **Multi-Timeframe**

---

## ✨ ویژگی‌ها

- 🎯 **Order Block** + **FVG** (شناسایی نواحی ورود)
- 📈 **CHOCH/BOS** (تشخیص ساختار بازار)
- 💧 **Liquidity Sweep** (شکار نقدینگی)
- 📊 **Premium/Discount Zone** (ناحیه ارزون/گرون)
- 📰 **اخبار اقتصادی** (فیلتر معاملات در زمان اخبار مهم)
- 🔄 **اسکن خودکار** هر ۱۵ دقیقه
- 📱 **PWA** — نصب روی گوشی مثل اپ واقعی
- 📊 **آمار زنده** — Win Rate، Profit Factor، ...

---

## 🌍 نمادهای پشتیبانی‌شده

| نماد | توضیح |
|---|---|
| EUR/USD | یورو / دلار |
| GBP/USD | پوند / دلار |
| USD/JPY | دلار / ین |
| AUD/USD | دلار استرالیا / دلار |
| USD/CAD | دلار / دلار کانادا |
| NZD/USD | دلار نیوزیلند / دلار |
| USD/CHF | دلار / فرانک |
| EUR/GBP | یورو / پوند |
| EUR/JPY | یورو / ین |
| GBP/JPY | پوند / ین |
| XAU/USD | طلا |

---

## ⚙️ معیارهای ارزیابی

این اعداد فقط **گیت اولیهٔ ارزیابی** هستند؛ عبور از آن‌ها به‌تنهایی مجوز معاملهٔ واقعی نیست. پیش از Live باید بک‌تست خارج از نمونه، هزینه‌های واقعی بروکر، کیفیت داده و اجرای Paper نیز تأیید شوند.

- ✅ **Win Rate** ≥ 65%
- ✅ **حداقل معاملات**: 20
- ✅ **Profit Factor** ≥ 1.5
- ✅ **Max Drawdown** ≤ 15%

---

## 📉 منبع داده و مقایسهٔ نمادها

- منبع پیش‌فرض `yfinance` برای طلا از `GC=F` (فیوچرز COMEX) استفاده می‌کند؛ این **اسپات XAUUSD نیست**.
- برای بک‌تست تاریخی طلا و GBP/JPY با دادهٔ Dukascopy، ابتدا BID/ASK را دریافت و midpoint تقریبی بساز:

```bash
python -m engine.cli fetch --symbols GBPJPY XAUUSD --years 7 --out data_duka
python -m engine.cli report --source dukascopy --csv-dir data_duka --symbols GBPJPY XAUUSD --start 2019-12-01 --end 2022-11-01 --walk-forward --out reports
```

فایل‌های Dukascopy به‌صورت کندل‌های ۱۵ دقیقه‌ای ذخیره می‌شوند. midpoint از OHLC تجمیع‌شدهٔ BID و ASK تقریب است، نه midpoint دقیق tick-by-tick؛ اسپرد و لغزش هم همچنان با مدل تنظیمات شبیه‌سازی می‌شوند. خروجی گزارش باید منبع، تعداد معاملات، expectancy، profit factor و drawdown را بررسی کند؛ نتیجه را بدون اجرای همین دستور روی دادهٔ کامل نمی‌توان اعلام کرد.

---

## 🏗️ معماری
