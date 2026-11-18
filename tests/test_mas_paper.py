"""تست‌های فاز ۵: موتور اجرای کاغذی — ژورنال، تسویه، attribution، بدون شبکه."""
import json
import os

import pandas as pd
import pytest

from engine.mas.paper import attribution, run_paper

from tests.test_engine import make_15m


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "data"
    d.mkdir()
    df = make_15m(420, seed=11)
    df.to_csv(d / "EURUSD_M15.csv", index=False)
    return tmp_path, d, df


def _state(out):
    return json.load(open(out / "state.json"))


def test_first_cycle_creates_state_and_journal(env):
    root, d, df = env
    r = run_paper(str(d), ["EURUSD"], out_dir=str(root / "out"), feeds=None)
    assert (root / "out" / "state.json").exists()
    assert (root / "out" / "journal.jsonl").exists()
    s = _state(root / "out")
    assert s["runs"] == 1 and s["equity"] > 0
    # داده غایب → A4/A5 stub؛ در هر صورت سیگنال WAIT یا سیگنال کامل، بدون خطا
    assert r["results"]["EURUSD"]["signal"] in ("BUY", "SELL", "WAIT")


def test_settlement_of_open_position(env):
    root, d, df = env
    out = root / "out"
    # موقعیت باز دستی: BUY با SL زیر مینیمم و TP2 بالای ماکسیمم → باز می‌ماند
    r1 = run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)
    s = _state(out)
    s["open_positions"] = [{"symbol": "EURUSD", "direction": "BUY",
                            "entry_time": str(df["time"].iloc[7000]),
                            "entry": 1.10, "sl": 1.00, "tp2": 2.00,
                            "lots": 0.10, "risk_usd": 100.0, "risk_pct": 1.0,
                            "final_score": 70.0, "regime": "strong_trend",
                            "session": "London"}]
    s["last_run_ts"] = "1900-01-01 00:00:00+00:00"   # اجازه اجرای مجدد
    json.dump(s, open(out / "state.json", "w"))
    # SL قابل‌برخورد → تسویه با ضرر
    s2 = _state(out)
    s2["open_positions"] = [{"symbol": "EURUSD", "direction": "BUY",
                             "entry_time": str(df["time"].iloc[7000]),
                             "entry": 1.10, "sl": 1.099, "tp2": 2.00,
                             "lots": 0.10, "risk_usd": 100.0, "risk_pct": 1.0,
                             "final_score": 70.0, "regime": "strong_trend",
                             "session": "London"}]
    s2["last_run_ts"] = "1900-01-01 00:00:00+00:00"
    json.dump(s2, open(out / "state.json", "w"))
    # Ledger is authoritative after migration; remove it to explicitly test legacy import.
    for suffix in ("", "-wal", "-shm"):
        path = str(out / "ledger.sqlite3") + suffix
        if os.path.exists(path):
            os.remove(path)
    r = run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)
    closed = r["closed_this_cycle"]
    assert len(closed) == 1 and closed[0]["outcome"] == "SL"
    assert closed[0]["pnl_usd"] < 0                       # ضرر مثبت بودنش منطقی
    st = _state(out)
    assert st["consecutive_losses"] == 1
    assert all(p.get("trade_id") != closed[0]["trade_id"] for p in st["open_positions"])
    # Journal is a rebuildable projection containing the complete ledger trade history.
    lines = open(out / "journal.jsonl", encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    assert float(json.loads(lines[0])["closed"][0]["pnl_usd"]) < 0


def test_gap_skip_between_runs(env):
    root, d, df = env
    out = root / "out"
    r1 = run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)
    r2 = run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)
    assert r2["results"]["EURUSD"].get("skipped")     # بلافاصله بعد از اجرای قبلی


def test_attribution_math():
    trades = [{"symbol": "A", "session": "London", "regime": "strong_trend", "pnl_usd": 50},
              {"symbol": "A", "session": "Asia", "regime": "range", "pnl_usd": -30},
              {"symbol": "B", "session": "London", "regime": "strong_trend", "pnl_usd": 20}]
    a = attribution(trades, equity=1000.0)
    assert a["n_trades"] == 3 and a["win_rate_pct"] == 66.7
    assert a["total_pnl_usd"] == 40 and a["return_pct"] == 4.0
    assert a["by_symbol"]["A"]["n"] == 2 and a["by_session"]["London"]["pnl"] == 70


def test_paper_migrates_legacy_three_loss_state_to_persistent_lock(env, monkeypatch):
    from types import SimpleNamespace
    import engine.mas.paper as paper

    root, d, df = env
    out = root / "out"
    out.mkdir()
    # Legacy state has no risk_lock_active field, but already reached the stop threshold.
    json.dump({
        "equity": 10_000.0, "consecutive_losses": 3, "daily_loss_pct": 0.0,
        "open_positions": [], "runs": 0, "last_run_ts": "1900-01-01T00:00:00+00:00",
    }, open(out / "state.json", "w"))

    captured = {}
    def fake_analyze(_df, _symbol, _now, acct=None, **_kwargs):
        captured["risk_lock_active"] = acct.risk_lock_active
        return SimpleNamespace(
            signal="WAIT", cand={}, wait_reason="test",
            to_json=lambda **_kwargs: {"signal": "WAIT", "wait_reason": "test"},
        )

    monkeypatch.setattr(paper, "analyze_mas", fake_analyze)
    paper.run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)

    persisted = _state(out)
    assert captured["risk_lock_active"] is True
    assert persisted["risk_lock_active"] is True
    assert persisted["consecutive_losses"] == 3


def test_paper_does_not_auto_release_persistent_lock_after_a_win(env, monkeypatch):
    from types import SimpleNamespace
    import engine.mas.paper as paper

    root, d, df = env
    out = root / "out"
    out.mkdir()
    state = {
        "equity": 10_000.0, "consecutive_losses": 3, "risk_lock_active": True,
        "daily_loss_pct": 0.0, "runs": 0,
        "last_run_ts": "1900-01-01T00:00:00+00:00",
        "open_positions": [{
            "symbol": "EURUSD", "direction": "BUY",
            "entry_time": str(df["time"].iloc[0]), "entry": 1.00,
            "sl": 0.95, "tp2": 1.05, "lots": 0.01,
            "risk_usd": 1.0, "risk_pct": 0.01,
            "final_score": 70.0, "regime": "range", "session": "London",
        }],
    }
    json.dump(state, open(out / "state.json", "w"))

    captured = {}
    def fake_analyze(_df, _symbol, _now, acct=None, **_kwargs):
        captured["risk_lock_active"] = acct.risk_lock_active
        return SimpleNamespace(
            signal="WAIT", cand={}, wait_reason="test",
            to_json=lambda **_kwargs: {"signal": "WAIT", "wait_reason": "test"},
        )

    monkeypatch.setattr(paper, "analyze_mas", fake_analyze)
    paper.run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)

    persisted = _state(out)
    assert captured["risk_lock_active"] is True
    assert persisted["risk_lock_active"] is True
    assert persisted["consecutive_losses"] == 3


def test_paper_uses_fill_price_risk_authorization_and_persists_snapshot(env, monkeypatch):
    from types import SimpleNamespace
    import engine.mas.paper as paper

    root, data_dir, frame = env
    out = root / "paper-fill-auth"

    def fake_analyze(df, symbol, now, acct=None, **kwargs):
        close = float(df["close"].iloc[-1])
        candidate = {"entry": close, "sl": close - 0.0010,
                     "tp1": close + 0.0015, "tp2": close + 0.0030, "rr": 3.0}
        return SimpleNamespace(
            signal="BUY", cand=candidate, final_score=75.0,
            market_regime="range", session="London",
            to_json=lambda **_kwargs: {"signal": "BUY", "risk_approved": True},
        )

    monkeypatch.setattr(paper, "analyze_mas", fake_analyze)
    result = paper.run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)

    assert len(result["opened_this_cycle"]) == 1
    opened = result["opened_this_cycle"][0]
    sizing = opened["sizing_result"]
    assert sizing["entry_price"] == str(opened["entry"])
    assert sizing["direction"] == "BUY"
    assert float(sizing["actual_risk"]) <= float(sizing["requested_risk"])
    assert result["results"]["EURUSD"]["sizing_result"]["sizing_id"] == sizing["sizing_id"]
    persisted = _state(out)["open_positions"][0]
    assert persisted["sizing_result"]["sizing_id"] == sizing["sizing_id"]

def test_paper_rebuilds_deleted_state_and_journal_from_ledger(env):
    root, data_dir, frame = env
    out = root / "paper-recovery"
    first = run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    first_state = _state(out)
    assert (out / "ledger.sqlite3").exists()

    # Simulate a crash after the financial commit but before derived projections were saved.
    os.remove(out / "state.json")
    os.remove(out / "journal.jsonl")
    second = run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    recovered = _state(out)

    assert recovered["runs"] == first_state["runs"]
    assert recovered["equity"] == first_state["equity"]
    assert recovered["open_positions"] == first_state["open_positions"]
    assert second["cycle"] == first["cycle"]
    assert (out / "journal.jsonl").exists()

def test_paper_rebuilds_tampered_state_projection_from_ledger(env):
    root, data_dir, frame = env
    out = root / "paper-authority"
    first = run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    expected = _state(out)
    tampered = dict(expected)
    tampered["equity"] = 1.0
    tampered["open_positions"] = []
    json.dump(tampered, open(out / "state.json", "w"))

    run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    restored = _state(out)
    assert restored["equity"] == expected["equity"]
    assert restored["open_positions"] == expected["open_positions"]
    assert restored["runs"] == first["cycle"]

def test_paper_risk_lock_release_requires_audited_idempotent_review(env):
    from engine.mas.paper import release_paper_risk_lock
    from engine.mas.ledger import SQLiteLedger

    root, data_dir, frame = env
    out = root / "paper-risk-release"
    out.mkdir()
    json.dump({
        "equity": 10_000.0, "consecutive_losses": 3, "risk_lock_active": True,
        "daily_loss_pct": 0.0, "open_positions": [], "runs": 0,
        "last_run_ts": "1900-01-01T00:00:00+00:00",
    }, open(out / "state.json", "w"))
    run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    assert _state(out)["risk_lock_active"] is True

    released = release_paper_risk_lock(
        str(out), reviewer="senior-reviewer", reason="manual risk review completed",
        request_id="review-2026-001",
    )
    assert released["risk_lock_active"] is False
    assert released["consecutive_losses"] == 0

    repeated = release_paper_risk_lock(
        str(out), reviewer="senior-reviewer", reason="manual risk review completed",
        request_id="review-2026-001",
    )
    assert repeated["risk_lock_active"] is False
    events = SQLiteLedger(out / "ledger.sqlite3").read_events("paper")
    releases = [event for event in events if event.event_type == "RiskLockReleased"]
    assert len(releases) == 1
    assert releases[0].payload["reviewed_by"] == "senior-reviewer"

def test_paper_imports_legacy_journal_even_without_state_snapshot(env):
    root, data_dir, frame = env
    out = root / "paper-journal-migration"
    out.mkdir()
    legacy_trade = {
        "trade_id": "old-trade-1", "symbol": "EURUSD", "direction": "BUY",
        "session": "London", "regime": "range", "pnl_usd": 25.0,
    }
    with open(out / "journal.jsonl", "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"cycle": 1, "closed": [legacy_trade]}) + "\n")

    result = run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    assert result["attribution"]["n_trades"] == 1
    assert result["attribution"]["total_pnl_usd"] == 25.0

def test_paper_recovers_when_state_projection_is_corrupt(env):
    root, d, _df = env
    out = root / "out"
    first = run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)
    ledger_path = out / "ledger.sqlite3"
    assert ledger_path.exists()
    expected_equity = first["equity"]
    expected_cycle = first["cycle"]

    # Simulate a truncated/invalid projection after a crash or interrupted write.
    (out / "state.json").write_text('{"equity":', encoding="utf-8")

    recovered = run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)
    restored = _state(out)
    assert recovered["equity"] == expected_equity
    assert restored["equity"] == expected_equity
    assert restored["runs"] == expected_cycle
    assert restored["open_positions"] == recovered["open_positions"]

def test_paper_cycle_marker_is_idempotent_after_crash_before_projection(env, monkeypatch):
    import engine.mas.paper as paper
    from engine.mas.ledger import SQLiteLedger
    from types import SimpleNamespace

    root, data_dir, _frame = env
    out = root / "paper-cycle-crash"

    def wait_signal(*_args, **_kwargs):
        return SimpleNamespace(
            signal="WAIT", cand={}, wait_reason="crash-test",
            to_json=lambda **_kwargs: {"signal": "WAIT", "wait_reason": "crash-test"},
        )

    monkeypatch.setattr(paper, "analyze_mas", wait_signal)
    original_replay = paper.SQLiteLedger.replay_account
    crashed = {"done": False}

    def crash_after_durable_cycle(self, account_id, initial_balance):
        projection = original_replay(self, account_id, initial_balance)
        if not crashed["done"] and any(
            event.event_type == "PaperCycleCompleted"
            for event in self.read_events(account_id)
        ):
            crashed["done"] = True
            raise RuntimeError("simulated process crash after cycle batch commit")
        return projection

    monkeypatch.setattr(paper.SQLiteLedger, "replay_account", crash_after_durable_cycle)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)

    # Restart with the normal replay implementation. The committed cycle marker
    # must be replayed once, not duplicated or counted again.
    monkeypatch.setattr(paper.SQLiteLedger, "replay_account", original_replay)
    recovered = run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    ledger = SQLiteLedger(out / "ledger.sqlite3")
    events = ledger.read_events("paper")
    cycles = [event for event in events if event.event_type == "PaperCycleCompleted"]

    assert len(cycles) == 1
    assert recovered["cycle"] == 1
    assert _state(out)["runs"] == 1


def test_paper_close_batch_recovers_once_after_crash_before_projection(env, monkeypatch):
    import engine.mas.paper as paper
    from engine.mas.ledger import SQLiteLedger
    from types import SimpleNamespace

    root, data_dir, frame = env
    out = root / "paper-close-crash"

    def wait_signal(*_args, **_kwargs):
        return SimpleNamespace(
            signal="WAIT", cand={}, wait_reason="crash-test",
            to_json=lambda **_kwargs: {"signal": "WAIT", "wait_reason": "crash-test"},
        )

    monkeypatch.setattr(paper, "analyze_mas", wait_signal)
    run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)

    ledger = SQLiteLedger(out / "ledger.sqlite3")
    entry_time = pd.Timestamp(frame["time"].iloc[0]).isoformat()
    ledger.append_event({
        "account_id": "paper", "trade_id": "crash-trade",
        "event_type": "PositionOpened", "idempotency_key": "crash-open:crash-trade",
        "occurred_at": entry_time,
        "payload": {
            "symbol": "EURUSD", "direction": "BUY", "lots": "0.01",
            "entry_price": "2.0000", "sl": "1.9000", "tp2": "3.0000",
            "entry_time": entry_time, "risk_usd": "1.00", "risk_pct": "0.01",
            "final_score": 50.0, "regime": "test", "session": "test",
        },
    })

    original_replay = paper.SQLiteLedger.replay_account
    crashed = {"done": False}

    def crash_after_durable_close(self, account_id, initial_balance):
        projection = original_replay(self, account_id, initial_balance)
        if not crashed["done"] and any(
            event.event_type == "PositionClosed" and event.trade_id == "crash-trade"
            for event in self.read_events(account_id)
        ):
            crashed["done"] = True
            raise RuntimeError("simulated process crash after close batch commit")
        return projection

    monkeypatch.setattr(paper.SQLiteLedger, "replay_account", crash_after_durable_close)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)

    # The close is durable although its projection write never happened. A restart
    # must reconstruct the closed trade without applying the close a second time.
    monkeypatch.setattr(paper.SQLiteLedger, "replay_account", original_replay)
    run_paper(str(data_dir), ["EURUSD"], out_dir=str(out), feeds=None)
    recovered_events = SQLiteLedger(out / "ledger.sqlite3").read_events("paper")
    close_events = [
        event for event in recovered_events
        if event.event_type == "PositionClosed" and event.trade_id == "crash-trade"
    ]
    projection = SQLiteLedger(out / "ledger.sqlite3").replay_account(
        "paper", SQLiteLedger(out / "ledger.sqlite3").get_meta("initial_balance:paper")
    )

    assert len(close_events) == 1
    assert "crash-trade" not in projection["open_positions"]
    assert len([trade for trade in projection["closed_trades"]
                if trade["trade_id"] == "crash-trade"]) == 1

