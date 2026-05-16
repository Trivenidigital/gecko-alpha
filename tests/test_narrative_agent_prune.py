"""Regression test for BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION cycle 2.

The cycle 1 file at this path tested ``_run_extra_table_prune`` in
``scout/narrative/agent.py``. Cycle 2 deletes that helper after migrating
all 6 narrative-owned tables to ``scout.main._run_hourly_maintenance``
via Settings-parameterized prune methods on ``Database``.

This file is replaced with a single regression test (per D8 plan-review
MUST-FIX #2 fold) that locks in BOTH halves of the migration:

1. The helper is gone (regression: reintroducing it duplicates work).
2. All 6 prune methods exist on ``Database`` (regression: dropping one
   silently leaves a table unpruned).
"""


def test_extra_table_prune_helper_removed_and_methods_exist_on_database():
    """BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION cycle 2 regression guard.

    D8 plan-review MUST-FIX #2: dropping the cycle 1 tests removed the only
    regression guard for "score_history NOT in narrative loop" and "6-table
    set is exact." Replacement test locks in both halves of the cycle 2
    migration so a future PR can't quietly reintroduce the helper or drop
    a prune method.
    """
    import scout.narrative.agent as narrative_agent
    from scout.db import Database

    assert not hasattr(narrative_agent, "_run_extra_table_prune"), (
        "Helper was migrated out in cycle 2; reintroducing it suggests "
        "a regression — narrative daily loop should not prune tables directly."
    )
    for method in (
        "prune_volume_spikes",
        "prune_momentum_7d",
        "prune_trending_snapshots",
        "prune_learn_logs",
        "prune_chain_matches",
        "prune_holder_snapshots",
    ):
        assert hasattr(Database, method), (
            f"Database.{method} missing — narrative loop's table was "
            f"migrated out but no replacement prune method exists. "
            f"Table will accumulate rows unbounded."
        )
