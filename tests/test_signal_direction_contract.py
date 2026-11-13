import os

os.environ.setdefault("FRED_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

from backend.analysis_engine import _directional_composite


def test_directional_composite_preserves_buy_and_sell_symmetry():
    buy = _directional_composite(
        tech_score=1.0, tech_direction="BUY",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )
    sell = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )

    assert buy["direction"] == "BUY"
    assert sell["direction"] == "SELL"
    assert buy["final_score"] > 0
    assert sell["final_score"] < 0
    assert abs(buy["final_score"]) == abs(sell["final_score"])
    assert buy["threshold"] == sell["threshold"]


def test_opposing_fundamental_evidence_can_veto_but_not_reverse_structure():
    result = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=1.0, news_score=0.0, min_score=0.72,
    )

    assert result["direction"] is None
    assert result["final_score"] < 0


def test_memory_is_not_a_directional_input():
    # The helper deliberately has no memory argument: historical strategy
    # performance is not evidence that the current market should rise or fall.
    result = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )
    assert result["direction"] == "SELL"


def test_invalid_direction_and_invalid_weights_fail_closed():
    invalid_direction = _directional_composite(
        tech_score=1.0, tech_direction="WAIT",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )
    invalid_weights = _directional_composite(
        tech_score=1.0, tech_direction="BUY",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
        technical_weight=0.0, fundamental_weight=0.0, news_weight=0.0,
    )

    assert invalid_direction["direction"] is None
    assert invalid_weights["direction"] is None
