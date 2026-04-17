"""Regression test: ensure narrative_fit JSON key stays consistent.

Historical bug: Claude prompt asked for key `narrative_fit`, but agent.py
read `narrative_fit_score` (the DB column name). Every prediction was
silently stored with fit=0, which made trade_predictions() reject 100%
of candidates.

Guard: (1) the prompt template must contain the constant's key, and
(2) the constant must equal the literal string the prompt asks for.
"""

from scout.narrative.prompts import NARRATIVE_FIT_KEY, NARRATIVE_FIT_TEMPLATE


def test_narrative_fit_key_matches_prompt():
    assert NARRATIVE_FIT_KEY == "narrative_fit"
    assert f'"{NARRATIVE_FIT_KEY}":' in NARRATIVE_FIT_TEMPLATE


def test_agent_uses_constant_not_literal():
    """agent.py must not hardcode 'narrative_fit_score' when parsing Claude results."""
    import pathlib
    agent_path = pathlib.Path(__file__).parent.parent / "scout" / "narrative" / "agent.py"
    source = agent_path.read_text(encoding="utf-8")
    # Should not call .get("narrative_fit_score") — that's the DB column,
    # not Claude's JSON key. All Claude parsing must go through NARRATIVE_FIT_KEY.
    assert 'result.get("narrative_fit_score"' not in source, (
        "agent.py is reading the wrong JSON key. Claude returns "
        "'narrative_fit', not 'narrative_fit_score'. Use NARRATIVE_FIT_KEY."
    )
