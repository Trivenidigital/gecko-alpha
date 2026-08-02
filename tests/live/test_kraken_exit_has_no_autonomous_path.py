"""No autonomous code path can reach the supervised Kraken exit.

Structural, not behavioural, and asserted over the AST rather than by grepping
strings: the exit lane's own docstrings and refusal messages name the very
symbols a substring search would look for, so a text search matches the
documentation and passes on a module that genuinely imports it.

Complements ``tests/live/test_closer_default_and_exit_stub.py`` on master,
which pins ``KrakenSpotAdapter.place_exit_order`` as a refusal stub. That test
covers the seam ``live_evaluator`` already calls; this one covers everything
else — that nothing schedules, imports or invokes the operator command.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SCOUT = Path(__file__).resolve().parents[2] / "scout"
_PILOT_MODULE = "scout.live.kraken_pilot"
_PILOT_FILE = _SCOUT / "live" / "kraken_pilot.py"

# The operator-only entry points. Nothing outside the module and its tests may
# name them.
_EXIT_ENTRY_POINTS = {"exit_position", "exit_cancel", "exit_status"}


def _python_sources() -> list[Path]:
    return sorted(p for p in _SCOUT.rglob("*.py") if p.name != "__pycache__")


def _imports(tree: ast.AST) -> set[str]:
    """Every module name imported anywhere in ``tree``, including inside
    functions — a deferred import is still an import path."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
                found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def test_live_evaluator_does_not_import_the_pilot():
    """The autonomous closer must not be able to reach the operator lane."""
    tree = ast.parse((_SCOUT / "live" / "live_evaluator.py").read_text("utf-8"))
    offenders = {name for name in _imports(tree) if "kraken_pilot" in name}
    assert offenders == set(), (
        f"live_evaluator.py imports {offenders} — the autonomous closer must "
        "not be able to reach the supervised exit lane"
    )


def test_live_evaluator_does_not_call_any_exit_entry_point():
    tree = ast.parse((_SCOUT / "live" / "live_evaluator.py").read_text("utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert (
        called & _EXIT_ENTRY_POINTS == set()
    ), f"live_evaluator.py calls {called & _EXIT_ENTRY_POINTS}"


def test_nothing_in_scout_imports_the_pilot_module():
    """The pilot is an operator CLI. A single import from the pipeline is the
    difference between a supervised command and a scheduled one."""
    offenders: list[str] = []
    for path in _python_sources():
        if path == _PILOT_FILE:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        if any("kraken_pilot" in name for name in _imports(tree)):
            offenders.append(str(path.relative_to(_SCOUT.parent)))
    assert offenders == [], (
        f"{offenders} import {_PILOT_MODULE}; the supervised exit must have no "
        "in-pipeline caller"
    )


def test_nothing_in_scout_names_an_exit_entry_point():
    """Belt to the import braces: no attribute access either, so a module that
    obtained the runner some other way still cannot be calling these."""
    offenders: list[tuple[str, str]] = []
    for path in _python_sources():
        if path == _PILOT_FILE:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            if name in _EXIT_ENTRY_POINTS:
                offenders.append((str(path.relative_to(_SCOUT.parent)), name))
    assert (
        offenders == []
    ), f"exit entry points referenced outside the pilot: {offenders}"


def test_the_pilot_is_only_reachable_through_its_own_cli():
    """``exit_position`` / ``exit_cancel`` / ``exit_status`` are called from
    exactly one place in the module: ``main``'s subcommand dispatch."""
    tree = ast.parse(_PILOT_FILE.read_text("utf-8"))
    main = next(
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "main"
    )
    inside_main = {
        node.func.attr
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert _EXIT_ENTRY_POINTS <= inside_main

    # And nowhere else in the module, other than the definitions themselves and
    # the internal helper the command wraps.
    other_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _EXIT_ENTRY_POINTS
    ]
    assert len(other_calls) == len(_EXIT_ENTRY_POINTS), (
        f"exit entry points are called {len(other_calls)} times inside the "
        "module; expected exactly once each, from main's dispatch"
    )


@pytest.mark.parametrize(
    "pattern", ["*.service", "*.timer", "*.sh", "*.cron", "crontab*"]
)
def test_no_scheduler_artifact_invokes_the_exit_lane(pattern):
    """Nothing in the repo schedules the exit. A cron line or a systemd unit
    naming it would make an operator-only command autonomous."""
    root = _SCOUT.parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob(pattern)
        if ".git" not in path.parts
        and "node_modules" not in path.parts
        and "kraken_pilot exit" in path.read_text("utf-8", errors="ignore")
    ]
    assert offenders == [], f"{offenders} schedule the supervised exit"
