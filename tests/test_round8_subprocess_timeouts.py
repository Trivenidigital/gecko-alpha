"""Round 8 static lint: every blocking subprocess primitive in scripts/ is bounded.

Background: scripts/codex_systemd_failure_alert.py and
codex_systemd_auto_remediate.py are wired as systemd OnFailure handlers
for gecko-pipeline / gecko-dashboard / codex-* units. If their
subprocess.run() call to codex-telegram-send hangs (e.g. Telegram API
unreachable + urlopen-side timeout doesn't fire fast enough due to a
binary-replacement or Python-startup pathology), the OnFailure handler
hangs indefinitely.

The same defense applied to aiohttp.ClientSession in PR #252 — every
network-facing primitive gets an explicit bound.

Rule, per primitive (the place a caller can actually block):

- ``subprocess.run(...)`` blocks in the call itself, so it must carry an
  explicit ``timeout=`` kwarg.
- ``subprocess.Popen(...)`` never blocks in the constructor and accepts no
  ``timeout`` parameter (passing one raises ``TypeError``), so a ``timeout=``
  kwarg on a Popen call is itself an offender. The blocking points of a Popen
  object are ``.wait()`` and ``.communicate()``; every such call on a name bound
  to a Popen result in the same scope must carry ``timeout=``. A Popen whose
  result is never waited on through those methods is bounded by its caller's
  own loop (e.g. scripts/receipt_inventory_supervisor.py waits with
  ``os.waitpid(WNOHANG)`` under an absolute deadline).

Before 2026-09-16 this lint demanded ``timeout=`` on Popen calls, a condition
no Popen call can satisfy at runtime; scripts/ had no Popen site, so the
branch was never exercised. The correction is scoped to the Popen branch.
"""

from __future__ import annotations

import ast
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SKIP_DIRS = {"__pycache__"}
BLOCKING_METHODS = ("wait", "communicate")


def _iter_py_files():
    for path in SCRIPTS_DIR.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _subprocess_callee(node: ast.Call) -> str | None:
    """'run' / 'Popen' when the call is ``subprocess.<name>(...)``, else None."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "subprocess" and func.attr in ("run", "Popen"):
            return func.attr
    return None


def _has_timeout(node: ast.Call) -> bool:
    return any(kw.arg == "timeout" for kw in node.keywords)


def _scopes(tree: ast.AST):
    """Yield (scope node, direct statements) for the module and every function."""
    yield tree, list(ast.iter_child_nodes(tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield node, list(ast.iter_child_nodes(node))


def _popen_bound_names(scope: ast.AST) -> set[str]:
    """Names bound to a ``subprocess.Popen(...)`` result within ``scope``."""
    names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _subprocess_callee(node.value) == "Popen":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        elif isinstance(node, ast.With):
            for item in node.items:
                if (
                    isinstance(item.context_expr, ast.Call)
                    and _subprocess_callee(item.context_expr) == "Popen"
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    names.add(item.optional_vars.id)
    return names


def find_unbounded_subprocess_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, reason) for every unbounded or invalid subprocess site."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _subprocess_callee(node)
        if callee == "run" and not _has_timeout(node):
            offenders.append((node.lineno, "subprocess.run without timeout="))
        elif callee == "Popen" and _has_timeout(node):
            offenders.append((node.lineno, "subprocess.Popen does not accept timeout= (TypeError at runtime)"))
        elif (
            callee is None
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in BLOCKING_METHODS
            and isinstance(node.func.value, ast.Call)
            and _subprocess_callee(node.func.value) == "Popen"
            and not _has_timeout(node)
        ):
            offenders.append((node.lineno, f"subprocess.Popen(...).{node.func.attr}() without timeout="))
    for scope, _ in _scopes(tree):
        bound = _popen_bound_names(scope)
        if not bound:
            continue
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in BLOCKING_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in bound
                and not _has_timeout(node)
            ):
                offenders.append((node.lineno, f"Popen.{node.func.attr}() without timeout="))
    return sorted(set(offenders))


def _offenders_in(source: str) -> list[str]:
    return [reason for _, reason in find_unbounded_subprocess_sites(ast.parse(source))]


def test_run_without_timeout_is_an_offender():
    assert _offenders_in("import subprocess\nsubprocess.run(['x'])\n") == ["subprocess.run without timeout="]


def test_run_with_timeout_is_clean():
    assert _offenders_in("import subprocess\nsubprocess.run(['x'], timeout=30)\n") == []


def test_popen_with_timeout_kwarg_is_an_offender():
    # Popen(timeout=...) raises TypeError at runtime; the old rule demanded it.
    assert _offenders_in("import subprocess\np = subprocess.Popen(['x'], timeout=1)\n") == [
        "subprocess.Popen does not accept timeout= (TypeError at runtime)"
    ]


def test_popen_without_blocking_call_is_clean():
    source = "import subprocess, os\np = subprocess.Popen(['x'])\nos.waitpid(p.pid, os.WNOHANG)\n"
    assert _offenders_in(source) == []


def test_popen_wait_without_timeout_is_an_offender():
    source = "import subprocess\ndef f():\n    p = subprocess.Popen(['x'])\n    p.wait()\n"
    assert _offenders_in(source) == ["Popen.wait() without timeout="]


def test_popen_communicate_with_timeout_is_clean():
    source = "import subprocess\ndef f():\n    p = subprocess.Popen(['x'])\n    p.communicate(timeout=5)\n"
    assert _offenders_in(source) == []


def test_popen_context_manager_wait_without_timeout_is_an_offender():
    source = "import subprocess\nwith subprocess.Popen(['x']) as p:\n    p.wait()\n"
    assert _offenders_in(source) == ["Popen.wait() without timeout="]


def test_chained_popen_wait_without_timeout_is_an_offender():
    source = "import subprocess\nsubprocess.Popen(['x']).wait()\n"
    assert _offenders_in(source) == ["subprocess.Popen(...).wait() without timeout="]


def test_unrelated_wait_is_not_flagged():
    # Only names bound to a Popen result in the same scope are checked.
    source = "import threading\nevent = threading.Event()\nevent.wait()\n"
    assert _offenders_in(source) == []


def test_every_subprocess_site_in_scripts_is_bounded():
    offenders: list[tuple[Path, int, str]] = []
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for lineno, reason in find_unbounded_subprocess_sites(tree):
            offenders.append((path, lineno, reason))

    assert not offenders, (
        "Unbounded or invalid subprocess sites in scripts/ detected. Without a "
        "bound, an OnFailure-alert handler could hang indefinitely if the "
        "invoked binary stalls. Sites:\n"
        + "\n".join(
            f"  - {p.relative_to(SCRIPTS_DIR.parent)}:{ln}: {reason}"
            for p, ln, reason in offenders
        )
    )
