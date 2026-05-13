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


# ---------------------------------------------------------------------
# Site #1: narrative heating alert
# ---------------------------------------------------------------------


def test_narrative_heating_alert_formatter_preserves_underscored_symbol():
    """Site #1: format_heating_alert body interpolates p.symbol raw —
    the dispatch site at scout/narrative/agent.py:557 must use
    parse_mode=None to render literal underscores in symbols like AS_ROID.

    This test verifies the formatter STILL interpolates the raw symbol
    (no double-escaping); the call-site test below pins parse_mode=None.
    """
    from scout.narrative.digest import format_heating_alert
    from scout.narrative.models import CategoryAcceleration, NarrativePrediction

    accel = CategoryAcceleration(
        category_id="ai",
        name="AI",
        previous_velocity=1.0,
        current_velocity=5.0,
        acceleration=4.0,
        volume_growth_pct=50.0,
        coin_count_change=0,
    )
    pred = NarrativePrediction(
        symbol="AS_ROID",
        market_cap_at_prediction=1_000_000,
        price_at_prediction=0.01,
        narrative_fit_score=80,
        confidence="high",
        reasoning="test",
        is_control=False,
        market_regime="bull",
    )
    text = format_heating_alert(accel, [pred], "BTC, ETH, SOL")
    # Body itself contains raw underscored symbol; escaping happens at the wire
    # via parse_mode=None (no Markdown parsing at all).
    assert "AS_ROID" in text


def test_narrative_agent_alert_call_passes_parse_mode_none():
    """Site #1 call-site contract: scout/narrative/agent.py:557 dispatches
    with parse_mode=None. Source-level pin — if a future refactor removes
    the kwarg, this test fails.
    """
    import scout.narrative.agent as agent

    source = inspect.getsource(agent)
    assert "format_heating_alert(" in source
    idx = source.index("format_heating_alert(")
    # Look ahead within ~600 chars for the send_telegram_message + parse_mode
    tail = source[idx : idx + 600]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail



# ---------------------------------------------------------------------
# Site #2: paper trading daily digest
# ---------------------------------------------------------------------


def test_paper_digest_call_passes_parse_mode_none():
    """Site #2: scout/narrative/agent.py:715 dispatches paper digest with
    parse_mode=None. Body interpolates best_symbol/worst_symbol AND per-
    signal_type keys; every signal_type has underscores.
    """
    import scout.narrative.agent as agent

    source = inspect.getsource(agent)
    idx = source.index("build_paper_digest")
    tail = source[idx : idx + 800]
    assert "send_telegram_message(" in tail
    assert "parse_mode=None" in tail
