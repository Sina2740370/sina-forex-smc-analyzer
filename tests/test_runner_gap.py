"""تست gap-fill در check_exit: گپ از SL → پر روی open ± half."""
from engine.backtest.runner import check_exit


def test_buy_gap_through_sl_fills_at_open_minus_half():
    """BUY با SL=99؛ کندل کامل زیر SL (open=94, high=95, low=90) → خروج < sl."""
    status, px = check_exit(1, 99.0, 110.0, high=95.0, low=90.0, half=0.0, open_=94.0)
    assert status == "SL"
    assert px == 94.0
    assert px < 99.0


def test_sell_gap_through_sl_fills_at_open_plus_half():
    """SELL با SL=101؛ کندل کامل بالای SL (open=106, high=110, low=105) → خروج > sl."""
    status, px = check_exit(-1, 101.0, 90.0, high=110.0, low=105.0, half=0.0, open_=106.0)
    assert status == "SL"
    assert px == 106.0
    assert px > 101.0


def test_no_gap_keeps_exact_sl_level():
    """بدون گپ (open بالای SL برای BUY) → پر روی سطح sl."""
    # open=100 > sl=99؛ low لمس می‌کند → fill در 99
    status, px = check_exit(1, 99.0, 110.0, high=103.0, low=98.0, half=0.0, open_=100.0)
    assert status == "SL"
    assert px == 99.0
    # open_=None هم همان رفتار قبلی
    status2, px2 = check_exit(1, 99.0, 110.0, high=103.0, low=98.0, half=0.0)
    assert (status2, px2) == ("SL", 99.0)


def test_sl_wins_when_both_touched():
    """SL و TP هم‌زمان → SL اولویت دارد."""
    status, px = check_exit(1, 99.0, 102.0, high=103.0, low=98.0, half=0.0, open_=100.0)
    assert status == "SL"
    assert px == 99.0
