"""
موتور رندر گرافیکی و متنی تحلیل چارت (Chart Visualizer):
رندر چارت شمعی و سطوح کلیدی SMC (ورود، SL، TP، OB، FVG) جهت نمایش در ربات تلگرام و وب‌اپلیکیشن PWA.
"""
from __future__ import annotations

import pandas as pd


def render_ascii_chart_summary(symbol: str, direction: str, entry: float, sl: float, tp: float,
                              ob_zone: dict = None, fvg_zone: dict = None) -> str:
    """
    تولید نمای دیداری و متنی چارت معاملاتی SMC برای تلگرام
    """
    side = direction.upper()
    is_buy = side == "BUY"

    tp_mark = f"🎯 [TP]  {tp:.5f}  --------------------+ (Target)"
    entry_mark = f"🔹 [ENTRY] {entry:.5f}  <=== {side} ORDER"
    sl_mark = f"🛑 [SL]  {sl:.5f}  --------------------+ (Stop Loss)"

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
    if ob_zone:
        lines.append(f"📦 [Order Block 4H]: {ob_zone.get('low', 0):.5f} - {ob_zone.get('high', 0):.5f}")
    if fvg_zone:
        lines.append(f"⚡ [FVG 15m]: {fvg_zone.get('bottom', 0):.5f} - {fvg_zone.get('top', 0):.5f}")

    return "\n".join(lines)
