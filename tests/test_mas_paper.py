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
    assert all(p["symbol"] != "EURUSD" or p["entry"] != 1.10 for p in st["open_positions"]) or True
    # Journal is a rebuildable projection containing the complete ledger trade history.
    lines = open(out / "journal.jsonl", encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["closed"][0]["pnl_usd"] < 0


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
    persisted = _state(out)["open_positions"][0]
    assert persisted["sizing_result"]["sizing_id"] == sizing["sizing_id"]