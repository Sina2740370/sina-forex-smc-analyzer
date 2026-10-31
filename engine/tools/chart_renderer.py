"""
موتور رندر گرافیکی و متنی تحلیل چارت (Chart Visualizer):
رندر چارت شمعی و سطوح کلیدی SMC (ورود، SL، TP، OB، FVG) جهت نمایش در ربات تلگرام و وب‌اپلیکیشن PWA.
"""
from __future__ import annotations

from ..core.config import INSTRUMENTS


def _digits_for(symbol: str) -> int:
    """تعداد رقم اعشار مناسب نماد: JPY/طلا با نمادهای خودشان، بقیه ۵ رقم.
    قبلاً فرمت ثابت ۵ رقمی برای جفت‌های JPY و طلا نامناسب بود."""
    s = (symbol or "").upper().replace("/", "").replace("=X", "")
    s = "XAUUSD" if s in ("GC=F", "GC", "XAU", "GOLD") else s
    ins = INSTRUMENTS.get(s)
    if ins is None:
        return 5
    if ins.pip == 0.01:      # جفت‌های JPY
        return 3
    if ins.pip == 0.1:       # طلا
        return 2
    return 5


def _fmt(symbol: str, price) -> str:
    try:
        return f"{float(price):.{_digits_for(symbol)}f}"
    except (TypeError, ValueError):
        return "-"


def render_ascii_chart_summary(symbol: str, direction: str, entry: float, sl: float, tp: float,
                              ob_zone: dict = None, fvg_zone: dict = None) -> str:
    """
    تولید نمای دیداری و متنی چارت معاملاتی SMC برای تلگرام
    """
    side = direction.upper()
    is_buy = side == "BUY"

    tp_mark = f"🎯 [TP]  {_fmt(symbol, tp)}  --------------------+ (Target)"
    entry_mark = f"🔹 [ENTRY] {_fmt(symbol, entry)}  <=== {side} ORDER"
    sl_mark = f"🛑 [SL]  {_fmt(symbol, sl)}  --------------------+ (Stop Loss)"

    lines = [
        f"📊 === چارت تحلیلی SMC ({symbol}) ===",
        f"جهة معامله: {'🟢 خرید (BUY)' if is_buy else '🔴 فروش (SELL)'}",
        "-----------------------------------------",
    ]

    if is_buy:
        lines.extend([tp_mark, "         |", entry_mark, "         |", sl_mark])
    else:
        lines.extend([sl_mark, "         |", entry_mark, "         |", tp_mark])

    lines.append("-----------------------------------------")

    # ناحیه‌ها فقط وقتی واقعاً مقدار دارند چاپ می‌شوند (قبلاً 0.00000 چاپ می‌شد)
    if ob_zone and ob_zone.get("low") and ob_zone.get("high"):
        lines.append(f"📦 [Order Block 4H]: {_fmt(symbol, ob_zone['low'])} - {_fmt(symbol, ob_zone['high'])}")
    if fvg_zone and fvg_zone.get("bottom") and fvg_zone.get("top"):
        lines.append(f"⚡ [FVG 15m]: {_fmt(symbol, fvg_zone['bottom'])} - {_fmt(symbol, fvg_zone['top'])}")

    return "\n".join(lines)
