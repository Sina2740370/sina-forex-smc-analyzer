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

    buy_score, buy_threshold, buy_direction = buy
    sell_score, sell_threshold, sell_direction = sell
    assert buy_direction == "BUY"
    assert sell_direction == "SELL"
    assert buy_score > 0
    assert sell_score < 0
    assert abs(buy_score) == abs(sell_score)
    assert buy_threshold == sell_threshold


def test_opposing_fundamental_evidence_can_veto_but_not_reverse_structure():
    result = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=1.0, news_score=0.0, min_score=0.72,
    )

    final_score, _threshold, direction = result
    assert direction is None
    assert final_score < 0


def test_memory_is_not_a_directional_input():
    # The helper deliberately has no memory argument: historical strategy
    # performance is not evidence that the current market should rise or fall.
    result = _directional_composite(
        tech_score=1.0, tech_direction="SELL",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )
    _final_score, _threshold, direction = result
    assert direction == "SELL"


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

    assert invalid_direction[2] is None
    assert invalid_weights[2] is None
