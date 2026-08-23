"""Deploy-without-activate watch for the legacy-provenance overlay.

`chain_identity_recompute_v1` has no runtime writer -- it is filled by an
offline ops step. So a deploy that ships the code and skips the backfill leaves
it empty, and an empty overlay is NOT a neutral state: every credit-bearing
`legacy_prefix` row fails the trust check and loses its chains credit. That is
the naive cutover this PR exists to prevent (tier_high 341 -> 187), arriving
silently, under green logs, looking exactly like a real decline in detection.

The probe has to separate two things that both produce a low number:
  - the overlay was never populated  -> operator error, one command fixes it
  - the overlay is partially covered -> expected forever, history runs out
Paging on the second trains the operator to ignore the first.
"""

import pytest

from scout.db import Database

ANCHOR = "2026-08-01T00:00:00+00:00"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _credit_bearing_legacy_row(db, coin_id, anchor=ANCHOR):
    """A pre-cutover row that currently HOLDS chains credit."""
    cur = await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, appeared_on_gainers_at,
            detected_by_chains, chains_lead_minutes, is_gap, created_at,
            chains_identity_semantics)
           VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
        (coin_id, anchor, anchor),
    )
    await db._conn.commit()
    return cur.lastrowid


async def _overlay_row(db, row_id, coin_id, anchor):
    await db._conn.execute(
        """INSERT INTO chain_identity_recompute_v1
           (source_table, source_row_id, coin_id, symbol, historical_anchor,
            legacy_detected, legacy_lead, canonical_detected, canonical_lead,
            identity_tier, evidence_status, semantics_version, computed_at)
           VALUES ('gainers_comparisons', ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                   'canonical_id', 'verified_canonical', 'v1', ?)""",
        (row_id, coin_id, anchor, ANCHOR),
    )
    await db._conn.commit()


async def test_shipped_without_the_backfill_is_reported_as_NOT_ACTIVATED(db):
    """An empty overlay against a real population must shout."""
    await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _credit_bearing_legacy_row(db, "axt-bstocks-tokenized-stock")

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_recovering"] is True
    assert probe["overlay_rows"] == 0
    assert probe["credit_bearing_legacy_rows"] == 2
    assert probe["credit_recovered"] == 0
    assert probe["uncovered"] == 2


async def test_a_fresh_install_with_no_legacy_rows_must_NOT_page(db):
    """Zero overlay rows is correct here, not a deploy error.

    Without this the alarm fires on every new deployment and gets muted, which
    is worse than not having it.
    """
    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_recovering"] is False
    assert probe["credit_bearing_legacy_rows"] == 0
    assert probe["uncovered"] == 0


async def test_a_populated_overlay_clears_the_alarm_and_the_uncovered_count(db):
    row_id = await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _overlay_row(db, row_id, "vanar-chain-2", ANCHOR)

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_recovering"] is False
    assert probe["overlay_rows"] == 1
    assert probe["uncovered"] == 0
    assert probe["credit_recovered"] == 1


async def test_partial_coverage_is_counted_but_does_NOT_page(db):
    """The expected steady state: history genuinely runs out for older anchors."""
    covered = await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _credit_bearing_legacy_row(db, "sandisk-backpack-securities")
    await _overlay_row(db, covered, "vanar-chain-2", ANCHOR)

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_recovering"] is False
    assert probe["uncovered"] == 1
    assert probe["credit_bearing_legacy_rows"] == 2


async def test_an_overlay_row_at_the_WRONG_ANCHOR_does_not_count_as_coverage(db):
    """The probe must correlate the way the trackers do, or it lies.

    Production has coin_ids appearing on a surface more than once at different
    anchors -- `axt-bstocks-tokenized-stock` and `sandisk-backpack-securities`
    both do. The trackers join overlay rows on (table, row id, anchor); an
    overlay row stamped at a different anchor is one the trackers will not use,
    so counting it as covered would report health the runtime does not have.
    """
    row_id = await _credit_bearing_legacy_row(db, "axt-bstocks-tokenized-stock")
    await _overlay_row(
        db, row_id, "axt-bstocks-tokenized-stock", "2026-08-11T14:55:09.656612+00:00"
    )

    probe = await db.chain_identity_recompute_coverage_probe()

    # Overlay is non-empty -- and that is NOT enough. Nothing was recovered,
    # so the credit collapse is total and the alarm must fire. Judging this
    # by row count reported green through exactly this state.
    assert probe["overlay_rows"] == 1
    assert probe["uncovered"] == 1
    assert probe["credit_recovered"] == 0
    assert probe["not_recovering"] is True


async def test_rows_without_chains_credit_are_outside_the_population(db):
    """A legacy row that never had chains credit cannot lose any."""
    await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, appeared_on_gainers_at,
            detected_by_chains, chains_lead_minutes, is_gap, created_at,
            chains_identity_semantics)
           VALUES ('no-credit', 'X', 'X', ?, 0, NULL, 0, ?, 'legacy_prefix')""",
        (ANCHOR, ANCHOR),
    )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["credit_bearing_legacy_rows"] == 0
    assert probe["not_recovering"] is False
    # `uncovered` runs its OWN query and must apply the same population filter.
    # Omitting this let a mutant that dropped the filter there pass all six
    # tests: the population count was right and nothing checked the other one.
    assert probe["uncovered"] == 0


async def _overlay_row_with_status(
    db, row_id, coin_id, anchor, status, table="gainers_comparisons"
):
    await db._conn.execute(
        """INSERT INTO chain_identity_recompute_v1
           (source_table, source_row_id, coin_id, symbol, historical_anchor,
            legacy_detected, legacy_lead, canonical_detected, canonical_lead,
            identity_tier, evidence_status, semantics_version, computed_at)
           VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 0, NULL,
                   'identity_unresolved', ?, 'v1', ?)""",
        (table, row_id, coin_id, anchor, status, ANCHOR),
    )
    await db._conn.commit()


async def test_a_FULL_overlay_that_recovered_NOTHING_still_pages(db):
    """The likeliest real failure, and the one row-counting cannot see.

    Only `verified_canonical` earns credit. Reconstruction depends on preserved
    /root snapshots that are being deleted over time; run the backfill once
    they are gone and roughly four rows in five land `indeterminate_history`.
    Overlay full, credit zero, tier_high collapsed -- and the original probe
    printed `uncovered: 0, not_activated: false` straight through it.
    """
    for coin in ("vanar-chain-2", "axt-bstocks-tokenized-stock", "blind-boxes"):
        row_id = await _credit_bearing_legacy_row(db, coin)
        await _overlay_row_with_status(
            db, row_id, coin, ANCHOR, "indeterminate_history"
        )

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["overlay_rows"] == 3
    assert probe["uncovered"] == 0  # every row HAS an overlay row...
    assert probe["credit_recovered"] == 0  # ...and not one of them earns credit
    assert probe["not_recovering"] is True


async def test_one_surface_backfilled_does_not_disarm_the_others(db):
    """`overlay_rows` was a global count, so a single row silenced all three.

    Reachable for free: the recompute skips any surface whose archive is
    missing, then exits 0.
    """
    row_id = await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _overlay_row(db, row_id, "vanar-chain-2", ANCHOR)
    for i in range(3):
        await db._conn.execute(
            """INSERT INTO losers_comparisons
               (coin_id, symbol, name, appeared_on_losers_at, detected_by_chains,
                chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
               VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
            (f"losers-coin-{i}", ANCHOR, ANCHOR),
        )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["per_surface"]["gainers_comparisons"]["credit_recovered"] == 1
    assert probe["per_surface"]["losers_comparisons"]["credit_recovered"] == 0
    assert probe["per_surface"]["losers_comparisons"]["uncovered"] == 3
    # Not paging here is correct -- gainers IS recovering -- but the per-surface
    # zero must be visible, or "one surface silently dark" reads as healthy.
    assert probe["not_recovering"] is False


async def test_NULL_semantics_rows_are_in_the_population(db):
    """The reader trusts only 'canonical_v1', so NULL is untrusted THERE.

    Filtering on `= 'legacy_prefix'` made rows written by rolled-back old code
    invisible to the probe while they silently lost credit in cross_surface.
    """
    await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, appeared_on_gainers_at, detected_by_chains,
            chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
           VALUES ('null-semantics', 'X', 'X', ?, 1, 8740.0, 0, ?, NULL)""",
        (ANCHOR, ANCHOR),
    )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["credit_bearing_legacy_rows"] == 1
    assert probe["credit_recovered"] == 0
    assert probe["not_recovering"] is True


async def test_an_overlay_row_for_a_DIFFERENT_SURFACE_is_not_coverage(db):
    """All three surfaces number ids from 1, and coin_ids can repeat.

    Without the `source_table` predicate a losers overlay row satisfies
    coverage for a gainers row. The shipped code was correct; nothing held it
    there until this test.
    """
    row_id = await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _overlay_row_with_status(
        db,
        row_id,
        "vanar-chain-2",
        ANCHOR,
        "verified_canonical",
        table="losers_comparisons",
    )

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["overlay_rows"] == 1
    assert probe["per_surface"]["gainers_comparisons"]["uncovered"] == 1
    assert probe["credit_recovered"] == 0
    assert probe["not_recovering"] is True


async def test_coverage_is_correlated_on_coin_id_the_way_the_READERS_are(db):
    """The trackers join on (source_table, coin_id, historical_anchor).

    NOT on row id -- they DELETE and re-insert by coin_id on every recompute,
    so ids do not survive, and the archives are CTAS copies where `id` is not a
    key. The first version of this probe keyed on `source_row_id` while its own
    docstring claimed parity with the readers, so it measured a wire nothing
    reads. Here the ids deliberately disagree and coverage must still hold.
    """
    row_id = await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _overlay_row(db, row_id + 5000, "vanar-chain-2", ANCHOR)

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["uncovered"] == 0
    assert probe["credit_recovered"] == 1
    assert probe["not_recovering"] is False
