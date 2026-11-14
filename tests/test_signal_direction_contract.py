import asyncio
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


def test_configured_smc_minimum_is_preserved_after_weight_normalization():
    final_score, threshold, direction = _directional_composite(
        tech_score=0.72, tech_direction="SELL",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
    )

    assert abs(final_score) == threshold
    assert direction == "SELL"


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



def test_unsafe_news_remains_a_hard_signal_veto():
    result = _directional_composite(
        tech_score=1.5, tech_direction="BUY",
        fundamental_score=0.0, news_score=0.0, min_score=0.72,
        news_safe=False,
    )

    final_score, _threshold, direction = result
    assert final_score == 0.0
    assert direction is None



def _stub_analysis_dependencies(monkeypatch, technical_direction="SELL", memory_score=0.9):
    from backend import analysis_engine as ae
    from backend import fundamental_analyzer as fa
    from backend import market_memory as mm

    monkeypatch.setattr(ae, "analyze_technical_topdown", lambda *a, **k: {
        "blocked": False,
        "score": 1.0,
        "direction": technical_direction,
        "sl": 1.09,
        "tp": 1.12,
        "price": 1.10,
        "grade": "A",
        "power": 6.0,
        "quality": 0.7,
        "trend": "weekly+daily BEARISH",
        "zone": "PREMIUM",
        "atr": 0.002,
        "reasons": ["controlled test signal"],
        "features": {},
    })

    async def fake_fundamental(_symbol, as_of=None):
        return {"score": 0.0, "reasons": []}

    monkeypatch.setattr(fa, "calculate_fundamental_score", fake_fundamental)
    monkeypatch.setattr(mm, "calculate_memory_score", lambda _symbol, as_of=None: {
        "score": memory_score, "reasons": ["historical strategy performance"],
    })
    return ae



def test_full_analysis_sell_integration_and_memory_neutrality(monkeypatch):
    ae = _stub_analysis_dependencies(monkeypatch, technical_direction="SELL", memory_score=0.9)
    result = asyncio.run(ae.full_analysis({}, "EURUSD=X"))

    assert result["direction"] == "SELL"
    assert result["score"] < 0
    assert result["memory_score"] == 0.9


def test_full_analysis_unsafe_news_blocks_otherwise_valid_signal(monkeypatch):
    ae = _stub_analysis_dependencies(monkeypatch, technical_direction="BUY", memory_score=0.0)
    result = asyncio.run(ae.full_analysis({}, "EURUSD=X", news_safe=False))

    assert result["direction"] is None
    assert result["score"] == 0.0



def test_drop_forming_uses_explicit_snapshot_time():
    import pandas as pd
    from backend.analysis_engine import _drop_forming

    df = pd.DataFrame({
        "time": pd.to_datetime([
            "2024-01-01T09:45:00Z",
            "2024-01-01T10:00:00Z",
        ]),
        "open": [1.0, 1.1],
        "high": [1.1, 1.2],
        "low": [0.9, 1.0],
        "close": [1.0, 1.1],
    })

    before_close = _drop_forming(df, 15, now=pd.Timestamp("2024-01-01T10:14:59Z"))
    at_close = _drop_forming(df, 15, now=pd.Timestamp("2024-01-01T10:15:00Z"))

    assert len(before_close) == 1
    assert len(at_close) == 2


def test_topdown_cleaning_uses_one_normalized_asof_timestamp(monkeypatch):
    import pandas as pd
    from types import SimpleNamespace
    from backend import analysis_engine as ae
    from engine.strategy import smc_topdown

    seen = []

    def capture_snapshot(frame, minutes, now=None, timeframe=None):
        seen.append(now)
        return frame

    monkeypatch.setattr(ae, "_drop_forming", capture_snapshot)
    monkeypatch.setattr(smc_topdown, "analyze", lambda *args: SimpleNamespace(
        direction=None, blocked="test", score=0.0, grade="-", power=0.0,
        quality=0.0, entry=None, sl=None, tp=None, bias="-",
        reasons=[], features={},
    ))

    times = pd.date_range("2024-01-01T00:00:00Z", periods=80, freq="15min")
    frame = pd.DataFrame({
        "time": times,
        "open": [1.0] * 80,
        "high": [1.1] * 80,
        "low": [0.9] * 80,
        "close": [1.0] * 80,
    })
    snapshot = pd.Timestamp("2024-01-02T00:00:00")
    ae.analyze_technical_topdown({"15m": frame, "4h": frame}, symbol="EURUSD=X", now=snapshot)

    assert len(seen) == 2
    assert all(value == snapshot.tz_localize("UTC") for value in seen)



def test_invalid_candle_timestamp_fails_closed():
    import pandas as pd
    from backend.analysis_engine import _drop_forming

    df = pd.DataFrame({
        "time": [pd.NaT],
        "open": [1.0],
        "high": [1.1],
        "low": [0.9],
        "close": [1.0],
    })

    filtered = _drop_forming(df, 15, now=pd.Timestamp("2024-01-01T10:15:00Z"))
    assert filtered.empty



def test_full_analysis_forwards_historical_snapshot_to_technical_engine(monkeypatch):
    ae = _stub_analysis_dependencies(monkeypatch, technical_direction="SELL", memory_score=0.0)
    seen = {}
    original = ae.analyze_technical_topdown

    def capture_snapshot(*args, **kwargs):
        seen.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(ae, "analyze_technical_topdown", capture_snapshot)
    snapshot = __import__("pandas").Timestamp("2024-01-02T12:30:00Z")
    asyncio.run(ae.full_analysis({}, "EURUSD=X", now=snapshot))

    assert seen["now"] == snapshot



def test_daily_and_weekly_candle_closes_respect_new_york_dst():
    import pandas as pd
    from backend.analysis_engine import _drop_forming

    daily_spring = pd.DataFrame({"time": [pd.Timestamp("2025-03-08T22:00:00Z")]})
    assert _drop_forming(
        daily_spring, 1440, now=pd.Timestamp("2025-03-09T20:59:00Z"), timeframe="1d"
    ).empty
    assert len(_drop_forming(
        daily_spring, 1440, now=pd.Timestamp("2025-03-09T21:00:00Z"), timeframe="1d"
    )) == 1

    daily_fall = pd.DataFrame({"time": [pd.Timestamp("2025-11-01T21:00:00Z")]})
    assert _drop_forming(
        daily_fall, 1440, now=pd.Timestamp("2025-11-02T21:30:00Z"), timeframe="1d"
    ).empty
    assert len(_drop_forming(
        daily_fall, 1440, now=pd.Timestamp("2025-11-02T22:00:00Z"), timeframe="1d"
    )) == 1

    weekly_spring = pd.DataFrame({"time": [pd.Timestamp("2025-03-02T22:00:00Z")]})
    assert _drop_forming(
        weekly_spring, 10080, now=pd.Timestamp("2025-03-09T20:59:00Z"), timeframe="1wk"
    ).empty
    assert len(_drop_forming(
        weekly_spring, 10080, now=pd.Timestamp("2025-03-09T21:00:00Z"), timeframe="1wk"
    )) == 1



def test_historical_fundamental_snapshot_fails_closed_without_fetch(monkeypatch):
    from datetime import datetime, timezone
    from backend import fundamental_analyzer as fa

    async def unexpected_fetch():
        raise AssertionError("historical analysis must not fetch current FRED values")

    monkeypatch.setattr(fa, "fetch_all_fundamental_data", unexpected_fetch)
    result = asyncio.run(fa.calculate_fundamental_score(
        "EURUSD=X",
        as_of=datetime(2020, 1, 2, tzinfo=timezone.utc),
    ))

    assert result["score"] == 0.0
    assert result["available"] is False
    assert result["point_in_time"] is False
    assert any("Historical fundamentals disabled" in reason for reason in result["reasons"])


def test_live_fundamental_path_remains_enabled_without_explicit_asof(monkeypatch):
    from backend import fundamental_analyzer as fa

    async def fake_fetch():
        return {"dxy": [], "fed_rate": [], "us_cpi": [], "us_unemp": []}

    monkeypatch.setattr(fa, "fetch_all_fundamental_data", fake_fetch)
    result = asyncio.run(fa.calculate_fundamental_score("EURUSD=X"))

    assert result["score"] == 0.0


def test_market_memory_excludes_outcomes_not_closed_at_snapshot(monkeypatch):
    from datetime import datetime, timezone
    from backend import config
    from backend import market_memory as mm

    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    snapshot = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        rows.append({
            "symbol": "EURUSD=X", "status": "WIN", "pnl_pips": 12.0,
            "created_at": f"2024-01-01T09:{i:02d}:00+00:00",
            "closed_at": f"2024-01-01T09:{i:02d}:30+00:00",
        })
    for i in range(10):
        rows.append({
            "symbol": "EURUSD=X", "status": "LOSS", "pnl_pips": -12.0,
            "created_at": f"2024-01-01T09:{i:02d}:00+00:00",
            "closed_at": f"2024-01-02T09:{i:02d}:30+00:00",
        })

    seen = {}
    def fake_get_closed_signals(as_of=None, limit=1000):
        seen["as_of"] = as_of
        seen["limit"] = limit
        return rows

    monkeypatch.setattr(mm, "get_closed_signals", fake_get_closed_signals, raising=False)
    result = mm.calculate_memory_score("EURUSD=X", as_of=snapshot)

    assert seen["as_of"] == snapshot
    assert result["score"] == 0.7
    assert result["enabled"] is True


def test_market_memory_fails_closed_for_invalid_snapshot(monkeypatch):
    from backend import config
    from backend import market_memory as mm

    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(mm, "get_closed_signals", lambda **_kwargs: [
        {"symbol": "EURUSD=X", "status": "WIN", "created_at": "2024-01-01T00:00:00Z",
         "closed_at": "2024-01-01T01:00:00Z", "pnl_pips": 1.0},
    ], raising=False)

    result = mm.calculate_memory_score("EURUSD=X", as_of="not-a-timestamp")
    assert result["score"] == 0.0
    assert result["enabled"] is False


def test_full_analysis_forwards_snapshot_to_market_memory(monkeypatch):
    ae = _stub_analysis_dependencies(monkeypatch)
    from backend import market_memory as mm

    seen = {}
    def fake_memory(_symbol, as_of=None):
        seen["as_of"] = as_of
        return {"score": 0.0, "reasons": [], "enabled": False}

    monkeypatch.setattr(mm, "calculate_memory_score", fake_memory)
    snapshot = __import__("pandas").Timestamp("2024-02-03T10:15:00Z")
    asyncio.run(ae.full_analysis({}, "EURUSD=X", now=snapshot))

    assert seen["as_of"] == snapshot


def test_database_closed_signal_query_excludes_post_snapshot_outcomes(monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from datetime import datetime, timezone
    from backend import database as db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY, symbol TEXT, status TEXT,
            created_at TEXT, closed_at TEXT, pnl_pips REAL
        )
    """)
    conn.executemany(
        "INSERT INTO signals (id, symbol, status, created_at, closed_at, pnl_pips) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "EURUSD=X", "WIN", "2024-01-01T09:00:00+00:00", "2024-01-01T09:30:00+00:00", 10.0),
            (2, "EURUSD=X", "LOSS", "2024-01-01T09:10:00+00:00", "2024-01-02T09:30:00+00:00", -10.0),
            (3, "EURUSD=X", "OPEN", "2024-01-01T09:20:00+00:00", None, 0.0),
        ],
    )

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    result = db.get_closed_signals(
        as_of=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    )

    assert [row["id"] for row in result] == [1]


def test_explicit_fundamental_snapshot_near_wall_clock_still_fails_closed(monkeypatch):
    from datetime import datetime, timezone
    from backend import fundamental_analyzer as fa

    async def unexpected_fetch():
        raise AssertionError("an explicit replay snapshot must never fetch live FRED data")

    monkeypatch.setattr(fa, "fetch_all_fundamental_data", unexpected_fetch)
    result = asyncio.run(fa.calculate_fundamental_score(
        "EURUSD=X",
        as_of=datetime.now(timezone.utc),
    ))

    assert result["score"] == 0.0
    assert result["available"] is False
    assert result["point_in_time"] is False


def test_database_closed_signal_query_normalizes_precision_and_offsets(monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from datetime import datetime, timezone
    from backend import database as db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY, symbol TEXT, status TEXT,
            created_at TEXT, closed_at TEXT, pnl_pips REAL
        )
    """)
    conn.executemany(
        "INSERT INTO signals (id, symbol, status, created_at, closed_at, pnl_pips) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "EURUSD=X", "WIN", "2024-01-01T09:00:00+00:00", "2024-01-01T10:00:00+00:00", 10.0),
            (2, "EURUSD=X", "LOSS", "2024-01-01T09:00:00+00:00", "2024-01-01T10:00:00.000001+00:00", -10.0),
            (3, "EURUSD=X", "WIN", "2024-01-01T09:00:00+00:00", "2024-01-01T10:59:59+01:00", 8.0),
            (4, "EURUSD=X", "WIN", "2024-01-01T09:00:00+00:00", "malformed", 8.0),
        ],
    )

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    result = db.get_closed_signals(
        as_of=datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    )

    assert [row["id"] for row in result] == [3, 1]
