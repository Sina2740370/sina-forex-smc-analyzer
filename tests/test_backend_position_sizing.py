import os

import pytest

os.environ.setdefault("FRED_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("DB_PATH", ":memory:")

from backend.position_sizing import calc_lots


def test_calc_lots_includes_entry_half_spread_and_rounds_risk_down():
    result = calc_lots(
        symbol="EURUSD=X",
        entry=1.1000,
        sl=1.0990,
        balance=1000,
        risk_pct=0.5,
    )

    assert result is not None
    # $5 target / ($104 loss per lot) => round down to 0.04 lot.
    assert result["lots"] == pytest.approx(0.04)
    assert result["risk_usd"] == pytest.approx(4.16)
    assert result["risk_usd"] <= 5.0
    assert result["risk_pct"] == pytest.approx(0.416)


def test_calc_lots_rejects_symbol_without_quote_conversion():
    result = calc_lots(
        symbol="UNKNOWNPAIR",
        entry=1.1000,
        sl=1.0990,
        balance=1000,
        risk_pct=0.5,
    )

    assert result is None
