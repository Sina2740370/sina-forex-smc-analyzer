import pytest
import pandas as pd
from engine.core.order_engine import PendingOrder, OrderStatus, process_candle_order_fill
from engine.tools.risk import calculate_risk_of_ruin
from engine.backtest.report import crisis_stress_test


def test_order_engine_limit_fill():
    """تست اجرای دقیق سفارش Limit با سایه کندل"""
    order = PendingOrder(
        order_id="101",
        symbol="EURUSD",
        side="BUY_LIMIT",
        price=1.1000,
        sl=1.0950,
        tp=1.1100,
        requested_lots=0.10,
        half_spread=0.0001
    )

    # کندل با کف 1.1005 (عدم برخورد با Ask)
    st1 = process_candle_order_fill(order, high=1.1050, low=1.1005, open_px=1.1020, close_px=1.1010)
    assert st1 == OrderStatus.PENDING

    # کندل با کف 1.0995 (Low Ask = 1.0995 + 0.0001 = 1.0996 <= 1.1000 -> پر شدن)
    st2 = process_candle_order_fill(order, high=1.1020, low=1.0995, open_px=1.1010, close_px=1.1000)
    assert st2 == OrderStatus.FILLED
    assert order.filled_lots == 0.10


def test_risk_of_ruin_math():
    """تست فرمول ریاضی احتمال ورشکستگی (Risk of Ruin)"""
    # استراتژی با برد 60٪ و ریسک 1٪ -> RoR بسیار نزدیک به 0٪
    ror_low = calculate_risk_of_ruin(win_rate=0.60, loss_rate=0.40, risk_per_trade_pct=1.0)
    assert ror_low < 1.0

    # استراتژی با برد 40٪ -> RoR برابر 100٪
    ror_high = calculate_risk_of_ruin(win_rate=0.40, loss_rate=0.60, risk_per_trade_pct=2.0)
    assert ror_high == 100.0


def test_crisis_stress_test_simulation():
    """تست شبیه‌سازی شوک نوسان بحران و اسپرد شدید"""
    dummy_trades = [
        {"r": 2.0}, {"r": -1.0}, {"r": 1.5}, {"r": -1.0}, {"r": 2.5}
    ]
    res = crisis_stress_test(dummy_trades, slippage_penalty_r=0.2, spread_multiplier=2.0)
    assert "total_r_stressed" in res
    assert res["total_r_stressed"] < res["total_r_original"]
