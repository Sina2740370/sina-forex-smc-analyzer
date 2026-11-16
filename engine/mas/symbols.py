"""مشخصات نمادها — تک منبع حقیقت برای pip / ارزش پیپ / اسپرد معمول.
قبلاً این مقادیر در aggregator و risk_agent هاردکد بودند؛ هر نماد جدید فقط
اینجا اضافه می‌شود و همه‌ی لایه‌ها (تحلیل، ریسک، بک‌تست، گزارش) خودکار می‌گیرند.
اسپردها «معمولِ» Dukascopy در سشن لندن/نیویورک‌اند (تخمین صادقانه؛ اسپرد لحظه‌ای
زنده از MT5/بروکر وقتی متصل شود جایگزین می‌شود)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import math


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



def size_lots_for_risk(symbol: str, entry: float, sl: float, risk_usd: float,
                       lot_step: float = 0.01, min_lot: float = 0.01,
                       max_lots: float = 50.0) -> tuple[float, float]:
    """Return (lots, actual_risk_usd), conservatively floored using Decimal.

    All price-distance, pip-value, risk and lot-step arithmetic is performed
    in Decimal from the original string representation. Broker constraints
    remain simulation defaults, not broker certification.
    """
    from decimal import localcontext

    try:
        entry_d, sl_d, risk_d = (Decimal(str(v)) for v in (entry, sl, risk_usd))
        step_d, min_d, max_d = (Decimal(str(v)) for v in (lot_step, min_lot, max_lots))
    except Exception as exc:
        raise ValueError("position sizing inputs must be decimal-compatible") from exc

    values = (entry_d, sl_d, risk_d, step_d, min_d, max_d)
    if not all(v.is_finite() for v in values):
        raise ValueError("position sizing inputs must be finite")
    if entry_d <= 0 or sl_d <= 0 or risk_d <= 0:
        raise ValueError("entry, SL and risk_usd must be positive")
    if step_d <= 0 or min_d <= 0 or max_d < min_d:
        raise ValueError("invalid lot constraints")

    spec = get_spec(symbol)
    with localcontext() as ctx:
        ctx.prec = 40
        pip_d = Decimal(str(spec.pip))
        quote_value_d = Decimal(str(spec.pip_value_quote_per_lot))
        distance_pips = abs(entry_d - sl_d) / pip_d
        if distance_pips <= 0:
            raise ValueError("entry and SL must differ")
        per_lot_risk = quote_value_d if spec.quote_is_usd else quote_value_d / entry_d
        per_lot_risk *= distance_pips
        if not per_lot_risk.is_finite() or per_lot_risk <= 0:
            raise ValueError("per-lot risk must be finite and positive")

        if min_d % step_d != 0:
            raise ValueError("min_lot must be an exact multiple of lot_step")

        raw_steps = (risk_d / per_lot_risk / step_d).to_integral_value(rounding=ROUND_DOWN)
        max_steps = (max_d / step_d).to_integral_value(rounding=ROUND_DOWN)
        steps = min(raw_steps, max_steps)
        lots_d = steps * step_d
        if lots_d < min_d:
            return 0.0, 0.0

        # Validate the exact value that will actually be returned to callers.
        # If float serialization rounds modeled risk upward over budget, step down.
        while lots_d >= min_d:
            actual_d = lots_d * per_lot_risk
            actual_f = float(actual_d)
            if Decimal(str(actual_f)) <= risk_d:
                return float(lots_d), actual_f
            lots_d -= step_d

        return 0.0, 0.0

def supported_symbols(csv_dir: str | None = None, suffixes: tuple = ("_M15.csv", "_i_M15.csv")) -> list:
    """نمادهای تعریف‌شده؛ اگر csv_dir داده شود فقط نمادهایی که داده دارند."""
    have = set(SYM_SPECS)
    if csv_dir:
        import os
        files = {f for f in os.listdir(csv_dir) if f.endswith(suffixes)}
        have &= {f.split("_M15.csv")[0].removesuffix("_i") for f in files}
    return sorted(have)
