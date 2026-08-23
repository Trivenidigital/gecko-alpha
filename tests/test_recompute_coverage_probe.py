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
    """The whole point: an empty overlay against a real population must shout."""
    await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _credit_bearing_legacy_row(db, "axt-bstocks-tokenized-stock")

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_activated"] is True
    assert probe["overlay_rows"] == 0
    assert probe["credit_bearing_legacy_rows"] == 2
    assert probe["uncovered"] == 2


async def test_a_fresh_install_with_no_legacy_rows_must_NOT_page(db):
    """Zero overlay rows is correct here, not a deploy error.

    Without this the alarm fires on every new deployment and gets muted, which
    is worse than not having it.
    """
    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_activated"] is False
    assert probe["credit_bearing_legacy_rows"] == 0
    assert probe["uncovered"] == 0


async def test_a_populated_overlay_clears_the_alarm_and_the_uncovered_count(db):
    row_id = await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _overlay_row(db, row_id, "vanar-chain-2", ANCHOR)

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_activated"] is False
    assert probe["overlay_rows"] == 1
    assert probe["uncovered"] == 0


async def test_partial_coverage_is_counted_but_does_NOT_page(db):
    """The expected steady state: history genuinely runs out for older anchors."""
    covered = await _credit_bearing_legacy_row(db, "vanar-chain-2")
    await _credit_bearing_legacy_row(db, "sandisk-backpack-securities")
    await _overlay_row(db, covered, "vanar-chain-2", ANCHOR)

    probe = await db.chain_identity_recompute_coverage_probe()

    assert probe["not_activated"] is False
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

    # Overlay is non-empty, so this is NOT the not-activated shape...
    assert probe["not_activated"] is False
    assert probe["overlay_rows"] == 1
    # ...but the row is still uncovered, because the tracker cannot join it.
    assert probe["uncovered"] == 1


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
    assert probe["not_activated"] is False
    # `uncovered` runs its OWN query and must apply the same population filter.
    # Omitting this let a mutant that dropped the filter there pass all six
    # tests: the population count was right and nothing checked the other one.
    assert probe["uncovered"] == 0
