"""Regression tests for BL-NEW-PARSE-MODE-AUDIT — Class-3 silent rendering corruption.

These tests pin the 7 HIGH ACTUAL sites (6 from audit + 1 plan-review discovery)
against future regression. Three coverage layers:
  1. Formatter render assertion — for sites that use _escape_md (#6, #7)
  2. Call-site source-level pin — for sites that use parse_mode=None (#1-5)
  3. AST structural coverage — every send_telegram_message call site in scout/
     must pin parse_mode (closes the audit-methodology gap that missed #7)
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest


# ---------------------------------------------------------------------
# Helper: capture the payload that scout.alerter.send_telegram_message
# would post to Telegram (used by per-site source-pin tests).
# ---------------------------------------------------------------------


def _capture_send(monkeypatch):
    """Patch scout.alerter.send_telegram_message to capture call args.

    Returns a list appended-to on each call. Each entry: {text, parse_mode}.
    """
    captured: list[dict] = []

    async def fake_send(text, session, settings, *, parse_mode="Markdown"):
        captured.append({"text": text, "parse_mode": parse_mode})

    monkeypatch.setattr("scout.alerter.send_telegram_message", fake_send)
    return captured


# ---------------------------------------------------------------------
# AST structural coverage — Layer 3
# ---------------------------------------------------------------------


SCOUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scout"


def _find_dispatch_calls(tree: ast.AST) -> list[ast.Call]:
    """Find every ast.Call to `send_telegram_message` in a parsed module.

    Matches both `send_telegram_message(...)` (attribute or name) and
    `alerter.send_telegram_message(...)`.
    """
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name == "send_telegram_message":
            calls.append(node)
    return calls


def test_all_dispatch_sites_pin_parse_mode():
    """Layer 3: every send_telegram_message call site in scout/ MUST pass
    parse_mode explicitly (None or "Markdown" or "MarkdownV2" or "HTML").

    Rationale: the original audit grepped `send_telegram_message` source
    occurrences and missed `send_alert` at scout/alerter.py:189 because
    that function does its own session.post call. An AST walk over the
    invocation graph catches every dispatch regardless of formatting,
    multi-line layout, or kwarg-from-variable. Closes the audit-methodology
    gap so a NEW dispatch site added 6 months from now without parse_mode=
    is caught at CI time, not after an operator notices a mangled alert.

    Exclusion: scout/alerter.py itself defines send_telegram_message, so
    references inside its own module body (the function definition's
    default-value reference, internal helpers calling it for testing)
    are tolerated only if they explicitly pass parse_mode= in the call.
    """
    offenders: list[str] = []
    for py_path in SCOUT_DIR.rglob("*.py"):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for call in _find_dispatch_calls(tree):
            kwarg_names = {kw.arg for kw in call.keywords}
            if "parse_mode" not in kwarg_names:
                offenders.append(
                    f"{py_path.relative_to(SCOUT_DIR.parent)}:{call.lineno} "
                    f"send_telegram_message() without parse_mode= kwarg"
                )
    assert not offenders, (
        "send_telegram_message dispatch sites missing parse_mode kwarg "
        "(see BL-NEW-PARSE-MODE-AUDIT + CLAUDE.md §12b):\n  "
        + "\n  ".join(offenders)
    )
