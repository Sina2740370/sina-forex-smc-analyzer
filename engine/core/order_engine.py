"""
Paper/Backtest-only pending-order fill simulator.

این ماژول صرفاً برخورد فرضی سفارش با کندل تاریخی/شبیه‌سازی‌شده را مدل می‌کند.
هیچ اتصال شبکه‌ای، حساب بروکر یا مسیر ارسال/اصلاح/لغو سفارش واقعی ندارد و
نباید به‌عنوان MT5 live execution یا broker order router استفاده شود.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass
class PendingOrder:
    order_id: str
    symbol: str
    side: str  # "BUY_LIMIT" | "SELL_LIMIT"
    price: float
    sl: float
    tp: float
    requested_lots: float
    filled_lots: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    half_spread: float = 0.0001
    created_bar_time: Optional[str] = None


def process_candle_order_fill(order: PendingOrder, high: float, low: float, open_px: float,
                              close_px: float) -> OrderStatus:
    """
    بررسی دقیق پر شدن سفارش با سایه کندل:
    BUY_LIMIT  → پر شدن روی Ask = price + half_spread (نیاز به low + half_spread <= price)
    SELL_LIMIT → پر شدن روی Bid = price - half_spread (نیاز به high - half_spread >= price)
    """
    if order.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL_FILL):
        return order.status

    if order.side == "BUY_LIMIT":
        effective_low_ask = low + order.half_spread
        if effective_low_ask <= order.price:
            order.filled_lots = order.requested_lots
            order.status = OrderStatus.FILLED
    elif order.side == "SELL_LIMIT":
        effective_high_bid = high - order.half_spread
        if effective_high_bid >= order.price:
            order.filled_lots = order.requested_lots
            order.status = OrderStatus.FILLED

    return order.status
