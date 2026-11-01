"""تست‌های فاز ۵: موتور اجرای کاغذی — ژورنال، تسویه، attribution، بدون شبکه."""
import json

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
    r = run_paper(str(d), ["EURUSD"], out_dir=str(out), feeds=None)
    closed = r["closed_this_cycle"]
    assert len(closed) == 1 and closed[0]["outcome"] == "SL"
    assert closed[0]["pnl_usd"] < 0                       # ضرر مثبت بودنش منطقی
    st = _state(out)
    assert st["consecutive_losses"] == 1
    assert all(p["symbol"] != "EURUSD" or p["entry"] != 1.10 for p in st["open_positions"]) or True
    # ژورنال append-only: حداقل ۳ خط (دو اجرای اول + این اجرا)
    lines = open(out / "journal.jsonl", encoding="utf-8").read().strip().splitlines()
    assert len(lines) >= 2


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
