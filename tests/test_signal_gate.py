from datetime import datetime, timedelta

from engine.tools.signal_gate import evaluate, exposure, norm_symbol

NOW = datetime(2026, 10, 12, 12, 0, 0)


def _closed(sym, d, status, hours_ago):
    return {"symbol": sym, "direction": d, "status": status,
            "closed_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_exposure_and_norm():
    assert exposure("USDCAD", "BUY") == {"USD": 1, "CAD": -1}
    assert exposure("EURUSD=X", "SELL") == {"EUR": -1, "USD": 1}
    assert norm_symbol("USD/CAD") == "USDCAD" and norm_symbol("GC=F") == "XAUUSD"


def test_old_open_signal_still_blocks_stacking():
    # باگ قبلی: فقط سیگنال باز ۴ ساعت اخیر دیده می‌شد؛ سیگنال باز ۵۰ ساعته نادیده بود
    old_open = [{"symbol": "USDCAD", "direction": "BUY", "created_at": (NOW - timedelta(hours=50)).isoformat()}]
    assert evaluate("USDCAD", "BUY", old_open, [], NOW) == "open_same_symbol"
    assert evaluate("USDCAD", "SELL", old_open, [], NOW) == "open_same_symbol"   # رفت‌وبرگشت هم نه
    assert evaluate("EURUSD", "BUY", old_open, [], NOW) is None


def test_cooldown_after_loss_and_win():
    assert evaluate("USDCAD", "BUY", [], [_closed("USDCAD", "BUY", "LOSS", 5)], NOW) == "cooldown"
    # بعد از ۸ ساعت cooldown تمام می‌شود ولی قاعده‌ی «تم ضرر» تا ۱۲ ساعت همان معامله را هم نگه می‌دارد
    assert evaluate("USDCAD", "BUY", [], [_closed("USDCAD", "BUY", "LOSS", 9)], NOW) == "loss_theme"
    assert evaluate("USDCAD", "BUY", [], [_closed("USDCAD", "BUY", "LOSS", 13)], NOW) is None
    assert evaluate("USDCAD", "BUY", [], [_closed("USDCAD", "BUY", "WIN", 3)], NOW) == "cooldown"
    assert evaluate("USDCAD", "BUY", [], [_closed("USDCAD", "BUY", "WIN", 5)], NOW) is None
    # جهت مخالف بعد از ضرر مجاز است
    assert evaluate("USDCAD", "SELL", [], [_closed("USDCAD", "BUY", "LOSS", 1)], NOW) is None


def test_loss_theme_blocks_same_currency_same_sign_only():
    lost = [_closed("USDCAD", "BUY", "LOSS", 2)]           # لانگ USD و شورت CAD
    assert evaluate("USDJPY", "BUY", [], lost, NOW) == "loss_theme"     # لانگ USD
    assert evaluate("EURUSD", "SELL", [], lost, NOW) == "loss_theme"    # لانگ USD
    assert evaluate("AUDCAD", "BUY", [], lost, NOW) == "loss_theme"     # شورت CAD
    assert evaluate("AUDCAD", "SELL", [], lost, NOW) is None            # لانگ CAD: مخالف
    assert evaluate("AUDUSD", "BUY", [], lost, NOW) is None             # شورت USD: مخالف
    assert evaluate("EURGBP", "BUY", [], lost, NOW) is None             # بی‌ربط
    assert evaluate("USDJPY", "BUY", [], [_closed("USDCAD", "BUY", "LOSS", 13)], NOW) is None  # قدیمی
    assert evaluate("USDJPY", "BUY", [], [_closed("USDCAD", "BUY", "WIN", 2)], NOW) is None     # سود: بی‌اثر


def _closed_full(sym, d, status, hours_ago, entry=1.1000, sl=1.0950):
    return {
        "symbol": sym, "direction": d, "status": status,
        "closed_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "entry": entry, "sl": sl,
    }


def test_failed_zone():
    risk = 0.005
    closed = [_closed_full("EURUSD", "BUY", "LOSS", 20, entry=1.1000, sl=1.0950)]
    assert evaluate("EURUSD", "BUY", [], closed, NOW, entry=1.1000) == "failed_zone"
    assert evaluate("EURUSD", "BUY", [], closed, NOW, entry=1.1000 + risk * 0.5) == "failed_zone"
    assert evaluate("EURUSD", "BUY", [], closed, NOW, entry=1.1000 + risk * 1.5) is None
    assert evaluate("EURUSD", "BUY", [], closed, NOW) is None  # بدون entry
    assert evaluate("EURUSD", "SELL", [], closed, NOW, entry=1.1000) is None


def test_symbol_streak():
    # خارج از cooldown (۸h) و loss_theme (۱۲h) تا فقط symbol_streak دیده شود
    closed = [
        _closed_full("GBPJPY", "BUY", "LOSS", 15, entry=190.0, sl=189.0),
        _closed_full("GBPJPY", "SELL", "LOSS", 18, entry=191.0, sl=192.0),
    ]
    assert evaluate("GBPJPY", "BUY", [], closed, NOW, entry=200.0) == "symbol_streak"
    old = [
        _closed_full("GBPJPY", "BUY", "LOSS", 30, entry=190.0, sl=189.0),
        _closed_full("GBPJPY", "SELL", "LOSS", 28, entry=191.0, sl=192.0),
    ]
    assert evaluate("GBPJPY", "BUY", [], old, NOW, entry=195.0) is None
