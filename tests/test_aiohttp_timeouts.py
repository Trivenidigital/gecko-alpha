"""Round 4 static-lint: every aiohttp.ClientSession() instantiation must
pass an explicit timeout.

The aiohttp default ``total`` timeout is 5 minutes — way too long for
Telegram / Discord / GoPlus alert delivery. A hung remote can pin the
event loop for 300 s and miss subsequent alerts. Explicit per-session
timeout is the §12 / production-hardening discipline.

This test scans scout/ and dashboard/ for any `ClientSession(...)`
construction without a `timeout=` argument and fails.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = [REPO_ROOT / "scout", REPO_ROOT / "dashboard"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".venv", "frontend"}


def _iter_py_files():
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


def _is_client_session_construction(node: ast.Call) -> bool:
    func = node.func
    name = None
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    return name == "ClientSession"


def test_every_client_session_has_timeout_kwarg():
    offenders: list[tuple[Path, int]] = []
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_client_session_construction(node):
                continue
            has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
            if not has_timeout:
                offenders.append((path, node.lineno))

    assert not offenders, (
        "aiohttp.ClientSession(...) call sites without an explicit "
        "timeout= kwarg detected. Default aiohttp total timeout (5 min) "
        "is too long for alert delivery; pass aiohttp.ClientTimeout"
        "(total=N) to bound hangs. Sites:\n"
        + "\n".join(
            f"  - {p.relative_to(REPO_ROOT)}:{ln}" for p, ln in offenders
        )
    )
