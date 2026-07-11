"""Static regression: every production send_telegram_message call passes
``parse_mode`` explicitly.

CLAUDE.md §12b mandates parse_mode discipline. The alerter's default is
historically ``parse_mode="Markdown"``. Any caller that omits the kwarg
inherits that default — and if the message body contains underscores in
signal names / metric names, MarkdownV1 silently mangles the rendered
text (HTTP 200, garbled body). This test scans all non-test
``send_telegram_message`` call sites and asserts ``parse_mode`` is
present in the (possibly multi-line) call.

Surfaced by PR #243/#244 post-fleet-review where
``scout/live/loops.py:251`` was discovered to rely on the default.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SCOUT_DIR = Path(__file__).resolve().parent.parent / "scout"


def _iter_scout_py_files() -> list[Path]:
    skip = {"__pycache__", ".pytest_cache", ".venv"}
    files = []
    for path in SCOUT_DIR.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        files.append(path)
    return files


def _is_send_telegram_message_call(node: ast.Call) -> bool:
    func = node.func
    name = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    return name == "send_telegram_message"


def _has_parse_mode_kwarg(node: ast.Call) -> bool:
    return any(kw.arg == "parse_mode" for kw in node.keywords)


def test_every_send_telegram_message_call_passes_parse_mode_explicitly():
    """Each call to send_telegram_message must include parse_mode= kwarg."""
    offenders: list[tuple[Path, int]] = []
    for path in _iter_scout_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            pytest.fail(f"could not parse {path}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_send_telegram_message_call(node):
                continue
            if not _has_parse_mode_kwarg(node):
                offenders.append((path, node.lineno))

    assert not offenders, (
        "the following send_telegram_message call sites omit parse_mode= "
        "and therefore inherit the alerter's Markdown default, risking "
        "CLAUDE.md §12b Class-3 rendering corruption when message bodies "
        "contain underscores in signal/metric names:\n"
        + "\n".join(f"  - {p}:{line}" for p, line in offenders)
        + "\n\nFix: pass parse_mode=None for plain-text bodies, or "
        "parse_mode='Markdown' for intentionally-formatted bodies with "
        "_escape_md coverage."
    )


def test_no_bare_parse_mode_markdown_in_scout_excl_known_intentional():
    """parse_mode='Markdown' usage is allowed but must be in a known list.

    Each entry must have tested escape coverage. Adding a new Markdown
    call site requires explicit entry here AND a corresponding escape-
    coverage test in test_parse_mode_hygiene.py.
    """
    KNOWN_MARKDOWN_SITES = {
        # Intentional Markdown bodies with _escape_md coverage:
        ("scout/velocity/detector.py", "format_velocity_alert"),
        # The alerter default itself + docstring references:
        ("scout/alerter.py", None),
    }

    pat = re.compile(r"parse_mode\s*=\s*['\"]Markdown['\"]")
    extra = []
    for path in _iter_scout_py_files():
        rel = str(path.relative_to(SCOUT_DIR.parent)).replace("\\", "/")
        src = path.read_text(encoding="utf-8")
        # The alerter module itself owns the default; skip.
        if rel == "scout/alerter.py":
            continue
        for m in pat.finditer(src):
            # Ensure this site is allowlisted.
            line = src.count("\n", 0, m.start()) + 1
            allowed = any(
                rel == site_rel
                for site_rel, _func in KNOWN_MARKDOWN_SITES
                if site_rel != "scout/alerter.py"
            )
            if not allowed:
                extra.append((rel, line))

    assert not extra, (
        "new parse_mode='Markdown' call sites detected outside the "
        "known-intentional allowlist. CLAUDE.md §12b requires every such "
        "call to have _escape_md coverage on user-data fields plus a "
        "corresponding test. Sites:\n" + "\n".join(f"  - {r}:{ln}" for r, ln in extra)
    )
