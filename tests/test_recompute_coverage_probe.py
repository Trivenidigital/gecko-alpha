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
    # This test originally asserted the alarm stays SILENT here, on the
    # reasoning that gainers is recovering. That was wrong, and per-surface
    # commits made it reachable: a replay that fails partway leaves earlier
    # surfaces durable, so gainers alone satisfied a global predicate while
    # losers sat fully stripped. Reporting was per-surface; the predicate was
    # not. A surface with a population that recovered nothing is the alarm.
    assert probe["not_recovering"] is True
    assert probe["dark_surfaces"] == ["losers_comparisons"]


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


async def test_canonical_rows_are_not_counted_as_uncovered(db):
    """Post-cutover the surfaces fill with `canonical_v1` rows.

    They carry their own tier and consult no overlay, so counting them as
    uncovered would make the metric grow without bound as normal traffic
    accumulates. A mutant dropping the semantics filter from the `uncovered`
    query alone survived the suite until this test existed.
    """
    await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, appeared_on_gainers_at, detected_by_chains,
            chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
           VALUES ('post-cutover', 'X', 'X', ?, 1, 8740.0, 0, ?, 'canonical_v1')""",
        (ANCHOR, ANCHOR),
    )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["credit_bearing_legacy_rows"] == 0
    assert probe["uncovered"] == 0
    assert probe["not_recovering"] is False


async def test_the_overlay_correlation_uses_a_THREE_COLUMN_seek(db):
    """Assert the PLAN, not the index name.

    The first version EXPLAINed a hand-written query and asserted the plan
    mentioned an index called `idx_cir_reader`. That is a proxy: redefining the
    index as `(source_table, historical_anchor)` keeps the name, keeps the test
    green, and costs 34x at production scale (6.1ms -> 209ms) because
    `historical_anchor` is far less selective than `coin_id`. What matters is
    that all three columns are used as a seek.

    Both shapes are asserted -- the tracker's subquery AND the probe's EXISTS
    -- because the probe runs on the hourly pass over the shared pipeline
    connection, and unindexed it is O(live_rows x overlay_rows_per_surface).
    """
    shapes = {
        "tracker subquery": (
            "EXPLAIN QUERY PLAN "
            "SELECT cir.evidence_status FROM chain_identity_recompute_v1 cir "
            "WHERE cir.source_table = 'gainers_comparisons' "
            "  AND cir.coin_id = 'x' AND cir.historical_anchor = 'y' "
            "ORDER BY CASE cir.evidence_status "
            "  WHEN 'verified_canonical' THEN 0 ELSE 1 END, cir.source_row_id "
            "LIMIT 1"
        ),
        "probe EXISTS": (
            "EXPLAIN QUERY PLAN "
            "SELECT COUNT(*) FROM gainers_comparisons AS c WHERE EXISTS ("
            "  SELECT 1 FROM chain_identity_recompute_v1 AS r "
            "  WHERE r.source_table = 'gainers_comparisons' "
            "  AND r.coin_id = c.coin_id "
            "  AND r.historical_anchor = c.appeared_on_gainers_at)"
        ),
    }
    for label, sql in shapes.items():
        cur = await db._conn.execute(sql)
        plan = " ".join(str(r[-1]) for r in await cur.fetchall())
        for col in ("source_table", "coin_id", "historical_anchor"):
            assert (
                col in plan
            ), f"{label}: {col} is not part of the index seek -- plan was {plan!r}"
        assert (
            "SCAN chain_identity_recompute_v1" not in plan
        ), f"{label} is scanning the overlay: {plan!r}"


async def test_a_RAISED_GATE_is_visible_to_the_probe(db):
    """`evidence_status` is a frozen decision about the gate at backfill time.

    The reader re-tests `chains_canonical_lead` against
    CONVICTION_EARLY_LEAD_MINUTES at scoring time. So counting statuses alone
    measures TRUST, not credit, and the two agree only while the gate has not
    moved. Review measured readers granting 0 of 10 while the probe reported
    10 recovered and stayed green.
    """
    for coin in ("a", "b", "c"):
        row_id = await _credit_bearing_legacy_row(db, coin)
        await _overlay_row(db, row_id, coin, ANCHOR)  # canonical_lead 8740

    at_ship = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    assert at_ship["credit_recovered"] == 3
    assert at_ship["not_recovering"] is False

    # Operator raises the gate past every stored lead. The rows are still
    # stamped `verified_canonical`; the readers now refuse all of them.
    raised = await db.chain_identity_recompute_coverage_probe(gate_minutes=99999.0)
    assert raised["credit_recovered"] == 0
    assert (
        raised["not_recovering"] is True
    ), "the gate moved past every recovered lead and the probe stayed green"


async def test_a_verified_row_with_no_lead_earns_nothing(db):
    """`canonical_lead IS NULL` cannot satisfy the reader's comparison.

    Counting the status alone would credit a row the reader must refuse.
    """
    row_id = await _credit_bearing_legacy_row(db, "no-lead")
    await _overlay_row_with_status(
        db, row_id, "no-lead", ANCHOR, "verified_canonical"
    )  # inserts canonical_lead NULL

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["overlay_rows"] == 1
    assert probe["credit_recovered"] == 0
    assert probe["not_recovering"] is True


async def test_a_replay_that_died_partway_still_pages(db):
    """The exact state per-surface commits made reachable.

    Before per-surface commits a mid-replay failure rolled everything back:
    overlay empty, nothing recovered, alarm fires, operator re-runs. Committing
    per surface leaves the earlier ones durable — so a global predicate is
    satisfied by gainers alone while two thirds of the population sits
    stripped, and the watchdog printed `losers=0/5 trending=0/5` in its own
    alert text and exited 0.
    """
    for i in range(3):
        row_id = await _credit_bearing_legacy_row(db, f"g{i}")
        await _overlay_row(db, row_id, f"g{i}", ANCHOR)
    for surface, col in (
        ("losers_comparisons", "appeared_on_losers_at"),
        ("trending_comparisons", "appeared_on_trending_at"),
    ):
        for i in range(3):
            await db._conn.execute(
                f"""INSERT INTO {surface}
                   (coin_id, symbol, name, {col}, detected_by_chains,
                    chains_lead_minutes, is_gap, created_at,
                    chains_identity_semantics)
                   VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
                (f"{surface}-{i}", ANCHOR, ANCHOR),
            )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["credit_recovered"] == 3  # gainers landed...
    assert probe["credit_bearing_legacy_rows"] == 9
    assert probe["not_recovering"] is True  # ...and that is not good enough
    assert probe["dark_surfaces"] == [
        "losers_comparisons",
        "trending_comparisons",
    ]


async def test_a_fully_recovering_tree_names_no_dark_surfaces(db):
    """The other direction, so the predicate cannot just always fire."""
    for surface, col in (
        ("gainers_comparisons", "appeared_on_gainers_at"),
        ("losers_comparisons", "appeared_on_losers_at"),
        ("trending_comparisons", "appeared_on_trending_at"),
    ):
        cur = await db._conn.execute(
            f"""INSERT INTO {surface}
               (coin_id, symbol, name, {col}, detected_by_chains,
                chains_lead_minutes, is_gap, created_at,
                chains_identity_semantics)
               VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
            (f"{surface}-ok", ANCHOR, ANCHOR),
        )
        await db._conn.execute(
            """INSERT INTO chain_identity_recompute_v1
               (source_table, source_row_id, coin_id, symbol, historical_anchor,
                legacy_detected, legacy_lead, canonical_detected, canonical_lead,
                identity_tier, evidence_status, semantics_version, computed_at)
               VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                       'canonical_id', 'verified_canonical', 'v1', ?)""",
            (surface, cur.lastrowid, f"{surface}-ok", ANCHOR, ANCHOR),
        )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["dark_surfaces"] == []
    assert probe["not_recovering"] is False


async def test_rows_with_no_archived_twin_are_named_as_unarchivable(db):
    """The persistent-page shape, arriving by a slow path.

    The archive step self-guards and never re-runs; `stamp_unmarked_chain_semantics`
    runs every startup. So a row written by rolled-back old code AFTER the
    archives were taken joins this population permanently and can never be
    covered — the backfill only reads archives. Meanwhile archived rows drain
    out as the trackers re-insert them `canonical_v1`. In the limit the
    population is entirely unarchivable: an hourly page forever that `--apply`
    cannot clear.

    Counted separately so the alert distinguishes "run the backfill" from
    "the backfill cannot help you".
    """
    await _credit_bearing_legacy_row(db, "born-after-the-archive")

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["credit_bearing_legacy_rows"] == 1
    assert probe["credit_recovered"] == 0
    assert probe["unarchivable"] == 1
    assert probe["per_surface"]["gainers_comparisons"]["unarchivable"] == 1
    # Still pages -- credit really is being lost -- but the operator can now
    # tell that re-running the backfill is not the remedy.
    assert probe["not_recovering"] is True


async def test_an_archived_row_is_not_counted_unarchivable(db):
    """The other direction, so the counter cannot just always fire."""
    row_id = await _credit_bearing_legacy_row(db, "properly-archived")
    await db._conn.execute(
        """INSERT INTO gainers_comparisons_legacy_prefix_v1
           (id, coin_id, symbol, name, appeared_on_gainers_at,
            detected_by_chains, chains_lead_minutes, is_gap,
            chains_identity_semantics, created_at)
           VALUES (?, 'properly-archived', 'PA', 'PA', ?, 1, 8740.0, 0,
                   'legacy_prefix', ?)""",
        (row_id, ANCHOR, ANCHOR),
    )
    await _overlay_row(db, row_id, "properly-archived", ANCHOR)
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["unarchivable"] == 0
    assert probe["credit_recovered"] == 1
    assert probe["not_recovering"] is False
