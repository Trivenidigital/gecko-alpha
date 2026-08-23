"""The overlay tie-break must prefer the CREDIT-BEARING row.

All three trackers pick one overlay row per `(coin_id, historical_anchor)` with
`ORDER BY ... LIMIT 1`. Duplicates at one key are reachable: the archives are
`CREATE TABLE ... AS SELECT` copies with no primary key, and production already
carries coin_ids appearing on a surface more than once.

The ordering was written `WHEN 'verified_canonical' THEN 1 ELSE 0 END` --
ascending, so 0 sorted first and the tracker deliberately chose the
NON-credit-bearing sibling. Every downstream check then behaved correctly on
the wrong row: credit silently discarded, no error anywhere.
"""

import pytest

from scout.db import Database
from scout.gainers.tracker import get_gainers_comparisons
from scout.losers.tracker import get_losers_comparisons
from scout.trending.tracker import get_recent_comparisons

ANCHOR = "2026-08-01T00:00:00+00:00"

SURFACES = [
    ("gainers_comparisons", "appeared_on_gainers_at", get_gainers_comparisons),
    ("losers_comparisons", "appeared_on_losers_at", get_losers_comparisons),
    ("trending_comparisons", "appeared_on_trending_at", get_recent_comparisons),
]


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _overlay(db, table, coin_id, row_id, status, lead):
    await db._conn.execute(
        """INSERT INTO chain_identity_recompute_v1
           (source_table, source_row_id, coin_id, symbol, historical_anchor,
            legacy_detected, legacy_lead, canonical_detected, canonical_lead,
            identity_tier, evidence_status, semantics_version, computed_at)
           VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 1, ?, 'canonical_id', ?, 'v1', ?)""",
        (table, row_id, coin_id, ANCHOR, lead, status, ANCHOR),
    )


@pytest.mark.parametrize("table,anchor_col,reader", SURFACES)
async def test_canonical_wins_the_tiebreak(db, table, anchor_col, reader):
    await db._conn.execute(
        f"""INSERT INTO {table}
            (coin_id, symbol, name, {anchor_col}, detected_by_chains,
             chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
            VALUES ('dupe-coin', 'DC', 'DC', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
        (ANCHOR, ANCHOR),
    )
    # Insert the LOSING status first, so a stable sort that ignores the ORDER BY
    # would return it -- the test must not pass by accident of insertion order.
    await _overlay(db, table, "dupe-coin", 1, "verified_prefix_only", None)
    await _overlay(db, table, "dupe-coin", 2, "verified_canonical", 9000.0)
    await db._conn.commit()

    rows = await reader(db, limit=10)
    row = next(r for r in rows if r["coin_id"] == "dupe-coin")

    assert row["chains_recompute_status"] == "verified_canonical", (
        "the tracker chose the non-credit-bearing sibling; ascending ORDER BY "
        "must sort the credit-bearing status LOWEST"
    )
    # The lead must come from the same row as the status, or the claim and the
    # value describe different evidence.
    assert row["chains_canonical_lead"] == pytest.approx(9000.0)


@pytest.mark.parametrize("table,anchor_col,reader", SURFACES)
async def test_a_canonical_row_does_NOT_consult_the_overlay(
    db, table, anchor_col, reader
):
    """A `canonical_v1` row carries its own tier. The overlay is for LEGACY rows.

    The subqueries carried no semantics filter while the comment above them
    said they did, and `cross_surface` applies `chains_canonical_lead`
    unconditionally. So a row resolved correctly at write time had its lead
    replaced by an offline replay of a DIFFERENT, archived row that merely
    shared its coin_id and anchor -- silently stripping its chains credit.

    Reachable on the deploy day: the anchor is MIN(snapshot_at) over a trailing
    24h, so a live canonical row and an archived legacy row share one for about
    a day after the backfill runs. That is the window in which the tier_high
    question is being judged, so the corruption lands on the measurement.
    """
    await db._conn.execute(
        f"""INSERT INTO {table}
            (coin_id, symbol, name, {anchor_col}, detected_by_chains,
             chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
            VALUES ('shared-coin', 'SC', 'SC', ?, 1, 5000.0, 0, ?, 'canonical_v1')""",
        (ANCHOR, ANCHOR),
    )
    # A stale archived-legacy verdict at the SAME coin_id and anchor, whose
    # reconstructed lead is far below the gate.
    await _overlay(
        db, table, "shared-coin", 1, "canonical_below_gate_indeterminate", 60.0
    )
    await db._conn.commit()

    rows = await reader(db, limit=10)
    row = next(r for r in rows if r["coin_id"] == "shared-coin")

    assert (
        row["chains_recompute_status"] is None
    ), "a canonical_v1 row consulted the legacy overlay"
    assert row["chains_canonical_lead"] is None

    # And the consequence, end to end: it keeps its own 5000-minute lead and
    # therefore its chains credit.
    from scout.config import Settings
    from scout.conviction import cross_surface_conviction

    settings = Settings(
        TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x", ANTHROPIC_API_KEY="x"
    )
    result = cross_surface_conviction(row, settings)
    assert (
        "chains" in result.contributing
    ), "the canonical row lost its chains credit to an archived row's replay"


@pytest.mark.parametrize("table,anchor_col,reader", SURFACES)
async def test_status_and_lead_come_from_the_SAME_overlay_row(
    db, table, anchor_col, reader
):
    """Two independent scalar subqueries can select two different rows.

    Sorting only by the CASE leaves rows sharing a CASE value unordered, so
    `LIMIT 1` may take the status from one and the lead from another --
    reintroducing exactly the claim/value decoupling the pair exists to
    prevent, at the SQL layer instead of the Python one.

    Both non-credit rows here share a CASE value and carry DIFFERENT leads, so
    any row-selection disagreement shows up as a mismatched pair.
    """
    await db._conn.execute(
        f"""INSERT INTO {table}
            (coin_id, symbol, name, {anchor_col}, detected_by_chains,
             chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
            VALUES ('multi-row', 'MR', 'MR', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
        (ANCHOR, ANCHOR),
    )
    await _overlay(db, table, "multi-row", 1, "no_legacy_credit", 111.0)
    await _overlay(db, table, "multi-row", 2, "indeterminate_history", 222.0)
    await _overlay(
        db, table, "multi-row", 3, "canonical_below_gate_indeterminate", 333.0
    )
    await db._conn.commit()

    rows = await reader(db, limit=10)
    row = next(r for r in rows if r["coin_id"] == "multi-row")

    pairs = {
        "no_legacy_credit": 111.0,
        "indeterminate_history": 222.0,
        "canonical_below_gate_indeterminate": 333.0,
    }
    assert row["chains_recompute_status"] in pairs
    assert row["chains_canonical_lead"] == pytest.approx(
        pairs[row["chains_recompute_status"]]
    ), (
        f"status {row['chains_recompute_status']!r} came with lead "
        f"{row['chains_canonical_lead']!r} -- the two subqueries selected "
        "different overlay rows"
    )
