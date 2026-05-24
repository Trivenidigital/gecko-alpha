"""Round 8 static lint: every subprocess.run() in scripts/ must pass timeout.

Background: scripts/codex_systemd_failure_alert.py and
codex_systemd_auto_remediate.py are wired as systemd OnFailure handlers
for gecko-pipeline / gecko-dashboard / codex-* units. If their
subprocess.run() call to codex-telegram-send hangs (e.g. Telegram API
unreachable + urlopen-side timeout doesn't fire fast enough due to a
binary-replacement or Python-startup pathology), the OnFailure handler
hangs indefinitely.

The same defense applied to aiohttp.ClientSession in PR #252 — every
network-facing primitive gets an explicit bound.

Test asserts every subprocess.run / subprocess.Popen in scripts/ carries
an explicit `timeout=` kwarg.
"""

from __future__ import annotations

import ast
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SKIP_DIRS = {"__pycache__"}


def _iter_py_files():
    for path in SCRIPTS_DIR.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _is_subprocess_run(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("run", "Popen") and isinstance(func.value, ast.Name):
            return func.value.id == "subprocess"
    return False


def test_every_subprocess_run_in_scripts_has_timeout_kwarg():
    offenders: list[tuple[Path, int]] = []
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_subprocess_run(node):
                continue
            has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
            if not has_timeout:
                offenders.append((path, node.lineno))

    assert not offenders, (
        "subprocess.run/Popen call sites in scripts/ without explicit "
        "timeout= kwarg detected. Without a timeout, an OnFailure-alert "
        "handler could hang indefinitely if the invoked binary stalls. "
        "Sites:\n"
        + "\n".join(
            f"  - {p.relative_to(SCRIPTS_DIR.parent)}:{ln}"
            for p, ln in offenders
        )
    )
