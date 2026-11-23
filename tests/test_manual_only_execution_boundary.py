"""Regression guard: SINA QUANT must remain a manual-only trading assistant.

This AST check is a defense-in-depth tripwire, not a proof against dynamic imports,
reflection, or indirect third-party execution. Human review remains required.
"""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "backend", ROOT / "engine")
FORBIDDEN_IMPORTS = {"MetaTrader5", "mt5", "oandapyV20", "ib_insync"}
FORBIDDEN_CALLS = {
    "order_send",
    "send_order",
    "place_order",
    "modify_order",
    "cancel_order",
    "close_position",
    "create_order",
}


def _python_sources():
    for base in PRODUCTION_ROOTS:
        if base.exists():
            yield from base.rglob("*.py")


def test_no_broker_execution_imports_or_order_routing_calls():
    violations = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_name = alias.name.split(".", 1)[0]
                    if root_name in FORBIDDEN_IMPORTS:
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".", 1)[0]
                if module in FORBIDDEN_IMPORTS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: from {node.module} import")
            elif isinstance(node, ast.Call):
                func = node.func
                called_name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else ""
                )
                if called_name in FORBIDDEN_CALLS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: call {called_name}")
    assert not violations, "Manual-only execution boundary violated:\n" + "\n".join(violations)


def test_simulated_order_fill_module_is_explicitly_non_live():
    from engine.core import order_engine

    doc = (order_engine.__doc__ or "").lower()
    assert "paper/backtest-only" in doc
    assert "no broker" in doc or "هیچ اتصال" in doc
