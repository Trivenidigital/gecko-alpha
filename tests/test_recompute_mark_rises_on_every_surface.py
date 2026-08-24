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

    CORRECTION, because the earlier version of this docstring was wrong and a
    reviewer measured it: this is NOT the assertion the surface-conditional
    mutant breaks. Under that mutant this test passes on all three surfaces --
    its first probe finds no stored mark, takes the `rearm` arm, and never
    reaches `if rate > best:`. Only the rise test above kills it.

    What this one carries is a different axis: durability of stored state.
    Neutralising `await conn.commit()` in `_record_coverage_baseline` is killed
    here on all three surfaces and NOT by the rise test. Kept separate because
    they fail for genuinely different reasons -- the one above says the mark
    moved, this one says the alarm the mark exists to arm can still fire.
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


#: The surfaces known to exist when this file was written. A LITERAL, on
#: purpose. The previous version of the test below asserted
#: `SURFACES == list(Database._RECOMPUTE_SURFACES)` -- a value compared against
#: its own definition three lines up, which passes no matter what the
#: production tuple contains. A reviewer demonstrated it: adding a fourth
#: surface left that assertion GREEN.
_KNOWN_SURFACES = [
    ("gainers_comparisons", "appeared_on_gainers_at"),
    ("losers_comparisons", "appeared_on_losers_at"),
    ("trending_comparisons", "appeared_on_trending_at"),
]


def test_a_NEW_production_surface_forces_a_look_at_this_file():
    """Trip deliberately when `_RECOMPUTE_SURFACES` grows.

    The parametrisation above already covers a new surface automatically --
    that part works, and a reviewer confirmed a fourth surface is picked up
    immediately. What it cannot do is make anyone check whether the OTHER
    surface-sensitive files were updated too: the checker-side coverage in
    `test_probe_checker_agreement.py`, and the watchdog fixtures. Those are
    hardcoded per-surface and are exactly where F3's twin hid.

    So this fails on a new surface on purpose. Update the literal, and while
    you are here, add the surface to the checker tests.
    """
    assert list(Database._RECOMPUTE_SURFACES) == _KNOWN_SURFACES, (
        "_RECOMPUTE_SURFACES changed. The parametrisation here follows it "
        "automatically, but the CHECKER-side tests in "
        "test_probe_checker_agreement.py do not -- add the surface there, "
        "then update _KNOWN_SURFACES."
    )


@pytest.mark.parametrize("table,anchor_col", SURFACES)
async def test_the_incomparability_guard_holds_on_this_surface(db, table, anchor_col):
    """The OTHER branch of `_classify_coverage_mark`, three lines from the first.

    F3 closed `if rate > best:` against the surface axis. The incomparability
    test one branch up was left surface-blind, and a reviewer showed a mutant
    appending `and source_table == "gainers_comparisons"` to it survives all
    7,135 tests -- disabling the lock-race guard on losers and trending, which
    is a FALSE PAGE there rather than a silent one.

    That is the finding restated precisely: the earlier fix closed one BRANCH,
    not the AXIS. Two branches of one function, three lines apart, and only one
    was covered.

    The guard's job: a mark recorded against a transiently tiny population is
    not comparable to today's much larger one, so it must be re-established
    rather than judged against. Judging against it pages on a rate that is
    fine.
    """
    await _bulk(db, table, anchor_col, 60, 18, f"{table}-inc")  # 30% today
    await db._conn.execute(
        "INSERT OR REPLACE INTO recompute_coverage_baseline "
        "(source_table, best_rate, population, recorded_at) VALUES (?, ?, ?, ?)",
        (table, 0.9, 25, ANCHOR),  # 0.9 recorded against only 25 rows
    )
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert table not in probe["collapsed_surfaces"], (
        f"{table} paged against a mark recorded at population 25 -- the "
        "incomparability guard is surface-conditional, so this surface gets a "
        "false page the others do not"
    )
    v = probe["per_surface"][table]
    assert v["best_rate"] == pytest.approx(v["rate"], abs=1e-4), (
        f"{table}: the incomparable mark was not re-established at today's rate"
    )
