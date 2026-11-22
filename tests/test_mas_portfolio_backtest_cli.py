"""CLI wiring for the shared-account MAS portfolio backtest."""
import json
from types import SimpleNamespace

import pandas as pd

import engine.cli as cli
import engine.mas.portfolio_backtest as portfolio_backtest


def test_mas_portfolio_backtest_cli_loads_local_frames_and_writes_summary(
    tmp_path, monkeypatch
):
    loaded = []
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-05T00:00:00Z"], utc=True),
        "open": [1.1], "high": [1.2], "low": [1.0], "close": [1.1],
    })

    monkeypatch.setattr(cli, "_mas_csv_path", lambda directory, symbol: f"{directory}/{symbol}.csv")

    def load(path):
        loaded.append(path)
        return frame.copy()

    monkeypatch.setattr(cli, "load_mt5_csv", load)
    observed = {}

    def run(frames, **kwargs):
        observed["symbols"] = sorted(frames)
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            symbols=sorted(frames),
            trades=[],
            summary=lambda: {"trades": 0, "execution_costs_certified": False},
        )

    monkeypatch.setattr(portfolio_backtest, "backtest_portfolio", run)
    args = SimpleNamespace(
        symbols=["GBPUSD", "EURUSD", "EURUSD"],
        csv_dir="fixture-data",
        start="2026-01-05T00:00:00Z",
        end="2026-01-06T00:00:00Z",
        step_hours=4,
        equity=12_500.0,
        out=str(tmp_path),
    )

    cli.cmd_mas_portfolio_backtest(args)

    assert loaded == ["fixture-data/EURUSD.csv", "fixture-data/GBPUSD.csv"]
    assert observed["symbols"] == ["EURUSD", "GBPUSD"]
    assert observed["kwargs"] == {
        "start": args.start,
        "end": args.end,
        "step_hours": 4,
        "equity": 12_500.0,
    }
    report = json.loads((tmp_path / "portfolio_backtest.json").read_text(encoding="utf-8"))
    assert report["summary"]["trades"] == 0
    assert report["summary"]["execution_costs_certified"] is False
    assert "uncertified zero placeholders" in report["execution_cost_warning"]
