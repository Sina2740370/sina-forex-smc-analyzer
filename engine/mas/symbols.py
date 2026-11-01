"""مشخصات نمادها — تک منبع حقیقت برای pip / ارزش پیپ / اسپرد معمول.
قبلاً این مقادیر در aggregator و risk_agent هاردکد بودند؛ هر نماد جدید فقط
اینجا اضافه می‌شود و همه‌ی لایه‌ها (تحلیل، ریسک، بک‌تست، گزارش) خودکار می‌گیرند.
اسپردها «معمولِ» Dukascopy در سشن لندن/نیویورک‌اند (تخمین صادقانه؛ اسپرد لحظه‌ای
زنده از MT5/بروکر وقتی متصل شود جایگزین می‌شود)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSpec:
    pip: float
    pip_value_quote_per_lot: float   # ارزش یک پیپ برای ۱ لات استاندارد، بر حسب ارز کوت
    quote_is_usd: bool
    typical_spread_pips: float       # اسپرد معمول بر حسب پیپ
    digits: int                      # برای فرمت قیمت در گزارش

    def spread_price(self) -> float:
        return self.typical_spread_pips * self.pip

    def fmt(self, x: float) -> str:
        return f"{x:.{self.digits}f}"


SYM_SPECS: dict = {
    "EURUSD": SymbolSpec(0.0001, 10.0, True, 0.8, 5),
    "GBPUSD": SymbolSpec(0.0001, 10.0, True, 1.1, 5),
    "AUDUSD": SymbolSpec(0.0001, 10.0, True, 1.0, 5),
    "USDCAD": SymbolSpec(0.0001, 10.0, False, 1.4, 5),
    "USDCHF": SymbolSpec(0.0001, 10.0, False, 1.4, 5),
    "USDJPY": SymbolSpec(0.01, 1000.0, False, 1.0, 3),
    "XAUUSD": SymbolSpec(0.1, 1.0, True, 2.5, 2),
}


def get_spec(symbol: str) -> SymbolSpec:
    if symbol not in SYM_SPECS:
        raise KeyError(f"نماد {symbol} در SYM_SPECS تعریف نشده؛ در engine/mas/symbols.py اضافه کنید")
    return SYM_SPECS[symbol]


def pip_value_usd(symbol: str, price: float) -> float:
    """ارزش پیپ بر حسب دلار (اگر کوت USD نیست، با نرخ فعلی تبدیل می‌شود)."""
    spec = get_spec(symbol)
    return spec.pip_value_quote_per_lot if spec.quote_is_usd else spec.pip_value_quote_per_lot / price


def supported_symbols(csv_dir: str | None = None, suffixes: tuple = ("_M15.csv", "_i_M15.csv")) -> list:
    """نمادهای تعریف‌شده؛ اگر csv_dir داده شود فقط نمادهایی که داده دارند."""
    have = set(SYM_SPECS)
    if csv_dir:
        import os
        files = {f for f in os.listdir(csv_dir) if f.endswith(suffixes)}
        have &= {f.split("_M15.csv")[0].removesuffix("_i") for f in files}
    return sorted(have)
