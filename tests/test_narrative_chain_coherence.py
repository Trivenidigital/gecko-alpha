"""BL-NEW-CHAIN-COHERENCE: per-laggard category_heating emission tests.

The chain pattern detector at scout/chains/tracker.py matches sequences of
events on the SAME token_id. The patterns full_conviction and
narrative_momentum (scout/chains/patterns.py:56-124) anchor on
category_heating and require subsequent steps (laggard_picked,
narrative_scored, counter_scored) to share the same token_id.

Production prior to BL-NEW-CHAIN-COHERENCE emitted category_heating with
token_id=accel.category_id (a category id like "ton-meme-coins") while
subsequent events used token_id=token.coin_id (a real coin id like "pepe").
The patterns could never match — a structural bug evidenced by 2,770
category_heating signal_events rows producing only 2 chain_complete events
in production history (2026-05-06 sweep).

This module pins the corrected emission semantics:
  * category_heating is emitted INSIDE the laggards-scoring loop
  * token_id is the laggard's coin_id (not the parent category's id)
  * event_data preserves category_id + name so dashboard / pattern-condition
    consumers retain category metadata
"""

from __future__ import annotations

from pathlib import Path

# Read agent.py source as text rather than via inspect.getsource(module)
# because importing scout.narrative.agent transitively imports aiohttp,
# which triggers Windows OpenSSL Applink loading. Reading the file
# directly is platform-independent and sufficient for the structural
# invariants we want to pin (single emission site, token_id wiring,
# placement inside the laggards loop).
_AGENT_PATH = (
    Path(__file__).resolve().parent.parent / "scout" / "narrative" / "agent.py"
)
_AGENT_SRC = _AGENT_PATH.read_text(encoding="utf-8")


def test_category_heating_emission_uses_token_coin_id():
    """The category_heating safe_emit call in scout/narrative/agent.py must
    use token_id=token.coin_id (or equivalent per-laggard coin_id), NOT
    accel.category_id. We verify structurally on the source — running the
    full agent end-to-end requires mocking aiohttp + the Anthropic client,
    which is out of scope for a single-emission-site invariant.
    """
    src = _AGENT_SRC

    # Find every safe_emit call whose event_type is "category_heating".
    # Use a coarse-but-safe heuristic: split source on "safe_emit(" and
    # inspect each call segment for both the event_type marker AND the
    # token_id argument.
    segments = src.split("safe_emit(")[1:]  # discard prelude
    cat_heating_calls = []
    for seg in segments:
        # Each call ends at the next ")" at depth 0 — for our heuristic
        # we just look at the first ~600 chars, which covers any realistic
        # multi-line call signature.
        head = seg[:600]
        if (
            'event_type="category_heating"' in head
            or "event_type='category_heating'" in head
        ):
            cat_heating_calls.append(head)

    assert (
        cat_heating_calls
    ), "expected at least one category_heating emission in scout/narrative/agent.py"

    for call in cat_heating_calls:
        # The bug-version had `token_id=accel.category_id`. Reject it.
        assert "token_id=accel.category_id" not in call, (
            "category_heating emission must NOT use accel.category_id as token_id "
            "— that breaks chain pattern matching. Use token.coin_id (or equivalent "
            "per-laggard coin_id) so anchor + downstream events share token_id."
        )
        # Affirmative check: the per-laggard fix uses token.coin_id.
        # Accept any of: token.coin_id, laggard.coin_id, t.coin_id (defensive
        # against future variable-name refactor).
        assert any(
            marker in call
            for marker in ("token.coin_id", "token_id=token.coin_id", "laggard.coin_id")
        ), (
            "category_heating emission must use a per-laggard coin_id as token_id "
            "(token.coin_id / laggard.coin_id). Found segment:\n" + call[:300]
        )


def test_category_heating_event_data_preserves_category_metadata():
    """After moving the emission per-laggard, the event_data payload must
    still carry category_id + name so dashboard/pattern-condition code that
    inspects payload[category_id] keeps working. Otherwise we'd silently
    break consumers."""
    src = _AGENT_SRC
    segments = src.split("safe_emit(")[1:]
    for seg in segments:
        head = seg[:800]
        if (
            'event_type="category_heating"' in head
            or "event_type='category_heating'" in head
        ):
            # The event_data dict must include category_id and name.
            # We accept both `accel.category_id` and `accel.name` references
            # since those are the values we want carried forward.
            assert (
                '"category_id"' in head
            ), "category_heating event_data must include 'category_id' key"
            assert '"name"' in head, (
                "category_heating event_data must include 'name' key for "
                "dashboard category display"
            )


def test_category_heating_inside_laggards_loop():
    """Verify the emission is structurally inside the laggards-scoring loop,
    not the outer category loop. We pin this with a sandwich check:

        laggards_loop_open < emission_pos < post_loop_marker

    The post-loop marker is a known call (`store_predictions(`) that lives
    AFTER the `for token in scored_laggards:` block closes. This rules out
    the failure mode where the emission is in source-order after the loop
    opening but actually outside the loop body (e.g., placed after the
    closing dedent). Per #4 code-structural reviewer Issue 1.

    Also accepts variable renames (token / laggard / pick) to stay
    consistent with the affirmative-marker tolerance in
    test_category_heating_emission_uses_token_coin_id (per Issue 2).
    """
    src = _AGENT_SRC
    # Find positions of all category_heating emissions.
    heating_positions = []
    idx = 0
    while True:
        i = src.find('event_type="category_heating"', idx)
        if i < 0:
            break
        heating_positions.append(i)
        idx = i + 1

    assert heating_positions, "expected at least one category_heating emission"

    # Find the laggards-loop opening. Accept variable renames consistent
    # with the affirmative-marker tolerance in
    # test_category_heating_emission_uses_token_coin_id.
    laggards_loop_pos = -1
    for marker in (
        "for token in scored_laggards",
        "for laggard in scored_laggards",
        "for pick in scored_laggards",
    ):
        p = src.find(marker)
        if p > 0:
            laggards_loop_pos = p
            break
    assert laggards_loop_pos > 0, (
        "expected a 'for <var> in scored_laggards' loop marker — search "
        "source for the actual loop construct if the variable was renamed."
    )

    # Find the post-loop marker. `store_predictions(` is called AFTER the
    # inner laggards loop closes. Sandwich the emission between loop-open
    # and post-loop-marker to guarantee placement inside the loop body,
    # not merely after it in source order.
    post_loop_pos = src.find("store_predictions(", laggards_loop_pos)
    assert post_loop_pos > laggards_loop_pos, (
        "expected a 'store_predictions(' call AFTER the laggards loop to "
        "use as a post-loop marker; codebase refactor may have removed it."
    )

    for pos in heating_positions:
        assert laggards_loop_pos < pos < post_loop_pos, (
            f"category_heating emission at offset {pos} is not inside the "
            f"laggards loop body (loop opens at {laggards_loop_pos}, "
            f"closes before store_predictions at {post_loop_pos}). "
            f"Move emission inside the laggards loop so token_id can be "
            f"per-laggard coin_id."
        )
