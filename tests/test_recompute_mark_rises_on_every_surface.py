"""F3: the ratchet must rise on EVERY surface, not just the one we test.

Found by the silent-failure reviewer at `f7a200cf`, after being warned that
their battery might share another reviewer's one-directional shape. It did not
-- it contained two rises -- but they judged "two incidental rises" not to be
coverage of an axis, built a dedicated rising battery, and with it found the
blindness was not direction at all. It was **surface**.

`test_the_mark_RISES_when_recovery_improves` hardcodes `gainers_comparisons`.
Mutate the classifier to raise the mark only for gainers --

    if rate > best and source_table == "gainers_comparisons":

-- and all 116 tests pass, while `trending_comparisons` freezes at its
pre-backfill `0.0`. A frozen `0.0` mark makes `rate < best * _COLLAPSE_FRACTION`
unsatisfiable for every rate, so that surface's collapse detector is dead and
nothing goes red. That is exactly the `6276d586` regression this tranche
already paid a blocking round for, scoped to one surface -- and it hides on the
surface most exposed to it, because trending oscillates near the floor and is
least like gainers.

No live defect: the shipped classifier has no surface conditional, which is why
the finding had to add one. This closes the hiding place.

The general rule, which outlived the finding: **a reviewer's battery inherits
the shape of the first defect they found.** The defence is not more scenarios
along the axis you know -- it is asking which axis the suite never varies. For
this component the axes are direction, surface, layer, and durability of stored
state.
"""

import pytest

from scout.db import Database
from scout.identity_recompute import RECOMPUTE_SEMANTICS

ANCHOR = "2026-08-01T00:00:00+00:00"

# Driven off the production tuple rather than a literal list, so a fourth
# surface is covered the day it is added instead of the day someone remembers.
SURFACES = list(Database._RECOMPUTE_SURFACES)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _legacy_row(db, table, anchor_col, coin_id):
    """A pre-cutover row on `table` that currently HOLDS chains credit."""
    cur = await db._conn.execute(
        f"""INSERT INTO {table}
            (coin_id, symbol, name, {anchor_col},
             detected_by_chains, chains_lead_minutes, is_gap, created_at,
             chains_identity_semantics)
            VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
        (coin_id, ANCHOR, ANCHOR),
    )
    await db._conn.commit()
    return cur.lastrowid


async def _overlay_row(db, table, row_id, coin_id):
    await db._conn.execute(
        """INSERT INTO chain_identity_recompute_v1
           (source_table, source_row_id, coin_id, symbol, historical_anchor,
            legacy_detected, legacy_lead, canonical_detected, canonical_lead,
            identity_tier, evidence_status, semantics_version, computed_at)
           VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                   'canonical_id', 'verified_canonical', ?, ?)""",
        (table, row_id, coin_id, ANCHOR, RECOMPUTE_SEMANTICS, ANCHOR),
    )
    await db._conn.commit()


async def _bulk(db, table, anchor_col, n, recovered, prefix):
    for i in range(n):
        row_id = await _legacy_row(db, table, anchor_col, f"{prefix}-{i}")
        if i < recovered:
            await _overlay_row(db, table, row_id, f"{prefix}-{i}")


@pytest.mark.parametrize("table,anchor_col", SURFACES)
async def test_the_mark_RISES_on_this_surface(db, table, anchor_col):
    """The ratchet ratchets here too -- not only on gainers."""
    await _bulk(db, table, anchor_col, 100, 20, f"{table}-a")
    first = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    assert first["per_surface"][table]["best_rate"] == 0.2

    for i in range(70):
        row_id = await _legacy_row(db, table, anchor_col, f"{table}-b-{i}")
        await _overlay_row(db, table, row_id, f"{table}-b-{i}")

    better = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    v = better["per_surface"][table]

    assert v["rate"] > 0.2
    assert v["best_rate"] == v["rate"], f"the mark did not rise on {table}"
    assert v["mark_written"] is True


@pytest.mark.parametrize("table,anchor_col", SURFACES)
async def test_a_frozen_mark_on_this_surface_makes_COLLAPSE_unsatisfiable(
    db, table, anchor_col
):
    """Why a per-surface freeze matters, stated as the consequence not the cause.

    This is the assertion the surface-conditional mutant actually breaks. It is
    kept separate from the rise test because they fail for different reasons:
    the one above says the mark moved, this one says the alarm the mark exists
    to arm can still fire afterwards.
    """
    await _bulk(db, table, anchor_col, 100, 90, f"{table}-hi")
    armed = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    best = armed["per_surface"][table]["best_rate"]
    assert best >= 0.9, f"{table} never armed a usable mark: {best}"

    # A real collapse on this surface: the /root snapshots go, the backfill is
    # re-run, and four rows in five land indeterminate.
    for i in range(400):
        await _legacy_row(db, table, anchor_col, f"{table}-collapse-{i}")

    after = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    assert table in after["collapsed_surfaces"], (
        f"{table} collapsed from {best} to "
        f"{after['per_surface'][table]['rate']} and nothing fired"
    )


def test_every_production_surface_is_covered_by_this_file():
    """The parametrisation must track the production tuple, not a copy of it.

    Without this, adding a fourth surface to `_RECOMPUTE_SURFACES` silently
    leaves it untested -- which is the same class of gap F3 was.
    """
    assert SURFACES == list(Database._RECOMPUTE_SURFACES)
    assert len(SURFACES) >= 3
