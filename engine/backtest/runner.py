"""Compatibility exports for the canonical execution/backtest runner.

Live/paper integrations and historical backtests must share the same order
simulation, spread handling, exit rules, and trade statistics.
"""
from ..runner import (
    check_exit,
    evaluate_gate,
    run_backtest,
    summarize,
    wilson,
)

__all__ = [
    "check_exit",
    "evaluate_gate",
    "run_backtest",
    "summarize",
    "wilson",
]
