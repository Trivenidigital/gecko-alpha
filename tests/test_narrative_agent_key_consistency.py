"""Regression test: ensure narrative_fit JSON key stays consistent.

Historical bug: Claude prompt asked for key `narrative_fit`, but agent.py
read `narrative_fit_score` (the DB column name). Every prediction was
silently stored with fit=0, which made trade_predictions() reject 100%
of candidates.

Guard: (1) the prompt template must contain the constant's key,
(2) the constant must equal the literal string the prompt asks for, and
(3) parse_fit_score tolerates missing / None / non-numeric values.
"""

import pathlib
import re

from scout.narrative.prompts import (
    NARRATIVE_FIT_KEY,
    NARRATIVE_FIT_TEMPLATE,
    parse_fit_score,
)


def test_narrative_fit_key_matches_prompt():
    assert NARRATIVE_FIT_KEY == "narrative_fit"
    assert f'"{NARRATIVE_FIT_KEY}":' in NARRATIVE_FIT_TEMPLATE


def test_agent_uses_constant_not_literal():
    """agent.py must not hardcode 'narrative_fit_score' when parsing Claude results."""
    agent_path = (
        pathlib.Path(__file__).parent.parent / "scout" / "narrative" / "agent.py"
    )
    source = agent_path.read_text(encoding="utf-8")
    # Regex allows for single or double quotes, extra whitespace, keyword args.
    pattern = re.compile(r"""\.get\s*\(\s*['"]narrative_fit_score['"]""")
    assert not pattern.search(source), (
        "agent.py is reading the wrong JSON key. Claude returns "
        "'narrative_fit', not 'narrative_fit_score'. Use NARRATIVE_FIT_KEY "
        "or parse_fit_score()."
    )


def test_parse_fit_score_handles_missing():
    assert parse_fit_score({}, default=7) == 7


def test_parse_fit_score_handles_none():
    assert parse_fit_score({NARRATIVE_FIT_KEY: None}, default=3) == 3


def test_parse_fit_score_handles_string_numeric():
    assert parse_fit_score({NARRATIVE_FIT_KEY: "42"}) == 42


def test_parse_fit_score_handles_non_numeric_string():
    assert parse_fit_score({NARRATIVE_FIT_KEY: "high"}, default=0) == 0


def test_parse_fit_score_handles_int():
    assert parse_fit_score({NARRATIVE_FIT_KEY: 75}) == 75
