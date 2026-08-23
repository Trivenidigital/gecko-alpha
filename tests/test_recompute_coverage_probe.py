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
from scout.identity_recompute import RECOMPUTE_SEMANTICS

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
                   'canonical_id', 'verified_canonical', ?, ?)""",
        (row_id, coin_id, anchor, RECOMPUTE_SEMANTICS, ANCHOR),
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
                   'identity_unresolved', ?, ?, ?)""",
        (table, row_id, coin_id, anchor, status, RECOMPUTE_SEMANTICS, ANCHOR),
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
                       'canonical_id', 'verified_canonical', ?, ?)""",
            (
                surface,
                cur.lastrowid,
                f"{surface}-ok",
                ANCHOR,
                RECOMPUTE_SEMANTICS,
                ANCHOR,
            ),
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


async def test_a_lead_EXACTLY_at_the_gate_counts_as_recovered(db):
    """The boundary that defines credit, on the shared predicate.

    `cross_surface` skips when `lead_val < early_lead`, so a lead exactly equal
    to the gate DOES earn credit. A probe using `>` would report that row lost
    while the reader grants it — the two halves disagreeing about the one
    number the whole alarm is built on.
    """
    row_id = await _credit_bearing_legacy_row(db, "exactly-on-the-gate")
    await db._conn.execute(
        """INSERT INTO chain_identity_recompute_v1
           (source_table, source_row_id, coin_id, symbol, historical_anchor,
            legacy_detected, legacy_lead, canonical_detected, canonical_lead,
            identity_tier, evidence_status, semantics_version, computed_at)
           VALUES ('gainers_comparisons', ?, 'exactly-on-the-gate', 'X', ?,
                   1, 8740.0, 1, 1440.0, 'canonical_id', 'verified_canonical',
                   ?, ?)""",
        (row_id, ANCHOR, RECOMPUTE_SEMANTICS, ANCHOR),
    )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert (
        probe["credit_recovered"] == 1
    ), "a lead exactly on the gate was reported lost; cross_surface grants it"
    assert probe["not_recovering"] is False


async def _bulk(db, n, recovered, prefix="bulk"):
    """n credit-bearing gainers rows, `recovered` of them with a verified overlay.

    `prefix` exists because the overlay correlates on coin_id: two batches
    sharing coin_ids would let the first batch's overlay rows satisfy the
    second batch's rows, so the recovered count would not be what the caller
    asked for.
    """
    for i in range(n):
        row_id = await _credit_bearing_legacy_row(db, f"{prefix}-{i}")
        if i < recovered:
            await _overlay_row(db, row_id, f"{prefix}-{i}", ANCHOR)


async def test_a_COLLAPSE_in_recovery_rate_pages_even_though_it_is_not_zero(db):
    """The failure the probe's own docstring predicts, which zero-detection misses.

    Delete the /root snapshots, re-run the backfill, and roughly four rows in
    five land indeterminate. Four in five is 20% — not zero — so a fall from
    the measured ~53% baseline to 5% left every alarm green while 95% of
    readers went blind.
    """
    await _bulk(db, 100, 60)
    healthy = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    assert healthy["not_recovering"] is False
    assert healthy["per_surface"]["gainers_comparisons"]["best_rate"] == 0.6

    # The snapshots are gone; a re-run recovers a fraction of what it did.
    await db._conn.execute(
        "DELETE FROM chain_identity_recompute_v1 WHERE source_row_id > "
        "(SELECT MIN(source_row_id) + 4 FROM chain_identity_recompute_v1)"
    )
    await db._conn.commit()

    collapsed = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert collapsed["credit_recovered"] > 0, "fixture must not be the zero case"
    assert collapsed["dark_surfaces"] == [], "this is a collapse, not total loss"
    assert collapsed["collapsed_surfaces"] == ["gainers_comparisons"]
    assert collapsed["not_recovering"] is True


async def test_ordinary_attrition_does_NOT_page(db):
    """Archived rows drain to `canonical_v1` over time, shrinking both numbers.

    A count-based high-water mark would page on that. Rates hold steady, so
    this must stay quiet — an alarm that fires on drift is one that gets muted.
    """
    await _bulk(db, 100, 60)
    await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    # Half the population ages out; the surviving rows keep the same ratio.
    await db._conn.execute(
        "UPDATE gainers_comparisons SET chains_identity_semantics = 'canonical_v1' "
        "WHERE id % 2 = 0"
    )
    await db._conn.commit()

    after = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert after["credit_bearing_legacy_rows"] < 100, "fixture did not shrink"
    assert after["collapsed_surfaces"] == []
    assert after["not_recovering"] is False


async def test_a_tiny_population_is_not_judged_on_its_rate(db):
    """Below the floor one row swings the rate; that is noise, not a cliff."""
    await _bulk(db, 4, 3)
    await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    await db._conn.execute("DELETE FROM chain_identity_recompute_v1")
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["collapsed_surfaces"] == []
    # It is still DARK -- zero recovery on a real population always pages --
    # but not via the collapse path.
    assert probe["dark_surfaces"] == ["gainers_comparisons"]


async def test_the_high_water_mark_never_lowers_itself(db):
    """A ratchet. Otherwise a degraded run silently becomes the new normal."""
    await _bulk(db, 100, 60)
    await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    await db._conn.execute(
        "DELETE FROM chain_identity_recompute_v1 WHERE source_row_id > "
        "(SELECT MIN(source_row_id) + 29 FROM chain_identity_recompute_v1)"
    )
    await db._conn.commit()
    await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    cur = await db._conn.execute(
        "SELECT best_rate FROM recompute_coverage_baseline "
        "WHERE source_table='gainers_comparisons'"
    )
    assert (await cur.fetchone())[0] == 0.6, "the mark followed the degradation down"


async def test_a_mark_from_a_much_smaller_population_does_not_false_page(db):
    """A rate measured on a small sample is not comparable to a large one.

    If the first observation lands while the population is transiently small —
    mid-migration, mid-backfill — it can carry a high rate. A return to the
    normal population then reads as a collapse, and a false page in an alarm's
    first week is how it earns a mute.
    """
    await _bulk(db, 25, 25)  # small population, perfect recovery
    first = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    assert first["per_surface"]["gainers_comparisons"]["best_rate"] == 1.0

    # The real population arrives at a normal rate that is nonetheless FAR
    # below the small sample's — 45/225 = 0.20 against a mark of 1.00, well
    # under the 0.5 collapse threshold. Without the guard this pages.
    await _bulk(db, 200, 20, prefix="second")

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["credit_bearing_legacy_rows"] == 225
    assert probe["per_surface"]["gainers_comparisons"]["rate"] == 0.2
    assert (
        probe["collapsed_surfaces"] == []
    ), "a mark from a 25-row sample was applied to a 225-row population"
    assert probe["not_recovering"] is False


async def test_an_absent_archive_makes_every_row_unarchivable(db):
    """Probe and watchdog must agree, or the runbook's rule is undecidable.

    "credit_recovered is 0 and unarchivable equals the population" is the
    documented test for design-limit-versus-incident. With the archive absent
    the checker reported `unarchivable = population` while the probe reported
    0, so the same database answered INCIDENT from journald and DESIGN LIMIT
    from Telegram.
    """
    for i in range(3):
        await _credit_bearing_legacy_row(db, f"orphan-{i}")
    await db._conn.execute("DROP TABLE gainers_comparisons_legacy_prefix_v1")
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["credit_bearing_legacy_rows"] == 3
    assert probe["credit_recovered"] == 0
    assert (
        probe["unarchivable"] == 3
    ), "the probe reported 0 unarchivable with no archive to be archived in"
    assert probe["not_recovering"] is True


async def test_the_ratchet_write_cannot_LAUNDER_a_sibling_transaction(db):
    """The probe writes; the connection is shared; `commit()` commits everything.

    `_run_hourly_maintenance` runs concurrently with the chain tracker, which
    opens a bare `BEGIN` on this same connection and calls `rollback()` on
    failure — by its own comment, several times a day in production. A probe
    that commits in between makes that rollback a no-op and the sibling's
    half-finished row durable.

    `_txn_lock` does not fix it: chains/tracker BEGINs directly and never takes
    the lock, so a lock only the polite callers hold cannot stop the impolite
    one. The write has to be on its own connection.
    """
    await _bulk(db, 25, 20)

    # A sibling opens a unit of work it will abandon.
    await db._conn.execute("BEGIN")
    await db._conn.execute(
        """INSERT INTO gainers_comparisons
           (coin_id, symbol, name, appeared_on_gainers_at, detected_by_chains,
            chains_lead_minutes, is_gap, created_at)
           VALUES ('sibling-half-finished', 'S', 'S', ?, 0, NULL, 0, ?)""",
        (ANCHOR, ANCHOR),
    )
    assert db._conn.in_transaction, "fixture did not open a sibling transaction"

    # The hourly probe fires in the middle of it and records a mark.
    await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    # The sibling then fails and rolls back, as it does in production.
    await db._conn.rollback()

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM gainers_comparisons WHERE coin_id='sibling-half-finished'"
    )
    assert (await cur.fetchone())[0] == 0, (
        "the probe committed a sibling's in-flight unit -- its rollback was "
        "laundered into a no-op"
    )

    # The mark is NOT required here: with a sibling holding the write lock the
    # separate connection cannot get it, and it gives up fast rather than
    # stalling the maintenance pass for the pipeline's full 90s timeout. The
    # next hourly pass records it. That is the trade moving off the shared
    # connection buys -- contention instead of laundering -- and the fast
    # give-up is what keeps it cheap.


async def test_a_surface_below_the_floor_says_so_rather_than_going_quiet(db):
    """Omitting the keys makes "too small to judge" look like "something broke"."""
    await _bulk(db, 5, 3)

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    v = probe["per_surface"]["gainers_comparisons"]

    assert v["rate_judged"] is False
    assert v["rate"] == 0.6, "the rate is still reported, just not judged"
    assert v["best_rate"] is None
    assert probe["per_surface"]["losers_comparisons"]["rate_judged"] is False


async def test_the_mark_IS_written_when_nothing_holds_the_lock(db):
    """The other half: with no contention the ratchet must actually record.

    Paired with the laundering test deliberately — that one asserts the write
    is skipped under contention, and on its own it would pass just as happily
    if the write never worked at all.
    """
    await _bulk(db, 25, 20)

    await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    cur = await db._conn.execute(
        "SELECT best_rate, population FROM recompute_coverage_baseline "
        "WHERE source_table='gainers_comparisons'"
    )
    row = await cur.fetchone()
    assert row is not None, "the ratchet never recorded a mark"
    assert row[0] == 0.8
    assert row[1] == 25
