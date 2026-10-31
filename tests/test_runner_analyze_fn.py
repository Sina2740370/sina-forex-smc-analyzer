"""run_backtest باید analyze_fn را بپذیرد و واقعاً از آن استفاده کند (cli.py آن را پاس می‌دهد)."""
import inspect

from engine.backtest.runner import run_backtest
from engine.core.config import EngineConfig
from engine.data.providers import build_topdown, load_mt5_csv
from engine.strategy.smc_topdown import Decision


def test_run_backtest_signature_accepts_analyze_fn():
    assert "analyze_fn" in inspect.signature(run_backtest).parameters


def test_run_backtest_uses_custom_analyze_fn():
    df = load_mt5_csv("engine/data/EURUSD_i_M15.csv", 3.0)
    data = build_topdown(df.iloc[:4000].reset_index(drop=True))
    calls = []

    def stub(view, cfg, now):
        calls.append(now)
        return Decision()  # direction=None → بدون معامله

    trades = run_backtest("EURUSD", data, EngineConfig(), analyze_fn=stub)
    assert len(calls) > 0
    assert trades == []
