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



def test_gold_futures_proxy_is_not_mapped_or_sized_as_spot_gold():
    from backend.position_sizing import broker_symbol

    assert broker_symbol("GC=F") == "GC=F"
    assert calc_lots("GC=F", 2000.0, 1990.0, 1000.0, 0.5) is None


def test_gold_futures_proxy_notification_blocks_spot_mt5_instructions(monkeypatch):
    import asyncio
    from backend import notifier

    sent = []
    async def fake_send(message):
        sent.append(message)

    monkeypatch.setattr(notifier, "send_telegram", fake_send)
    asyncio.run(notifier.notify_new_signal(
        symbol="GC=F",
        name="Gold Futures (GC=F; proxy, not XAU/USD spot)",
        direction="BUY",
        entry=2000.0,
        sl=1990.0,
        tp=2020.0,
        score=0.8,
        reasons=["test"],
    ))

    assert len(sent) == 1
    assert "برای XAUUSD اسپات در MT5 استفاده نکن" in sent[0]
    assert "ثبت در MT5" not in sent[0]
    assert "نماد: <code>XAUUSD" not in sent[0]
