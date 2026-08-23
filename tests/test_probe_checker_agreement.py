"""The probe and the checker must agree, on the same database, in every state.

This exists because a predicate deliberately duplicated across two layers was
verified in one layer four times running, and the paging layer was left behind
each time. Four of this tranche's findings are that single shape:

  - `unarchivable` counted in the probe, absent from the alert;
  - the population-comparability guard closed in the probe, open in the checker;
  - the version filter pinned in the probe, unpinned in the checker;
  - the narrowed guard landed in the probe while the checker kept the old one.

The reason it recurs is structural rather than attentional: the probe is where
the logic reads most naturally, so it is the layer anyone opens — and the
checker is a separate stdlib-only script that must keep working when the
application does not, which is exactly why it cannot import the probe and share
one implementation.

So the two implementations stay separate and this file is the bridge. It runs
BOTH against one database and asserts they reach the same verdict. A test that
exercised either alone would have passed through all four findings above.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scout.db import Database
from scout.identity_recompute import RECOMPUTE_SEMANTICS

CHECKER = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_recompute_coverage.py"
)
ANCHOR = "2026-08-01T00:00:00+00:00"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "scout.db")
    await d.initialize()
    yield d
    await d.close()


async def _populate(
    db, population, recovered, overlay_version=None, overlay_status=None
):
    for i in range(population):
        cur = await db._conn.execute(
            """INSERT INTO gainers_comparisons
               (coin_id, symbol, name, appeared_on_gainers_at, detected_by_chains,
                chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
               VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
            (f"coin-{i}", ANCHOR, ANCHOR),
        )
        if i < recovered:
            await db._conn.execute(
                """INSERT INTO chain_identity_recompute_v1
                   (source_table, source_row_id, coin_id, symbol, historical_anchor,
                    legacy_detected, legacy_lead, canonical_detected, canonical_lead,
                    identity_tier, evidence_status, semantics_version, computed_at)
                   VALUES ('gainers_comparisons', ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                           'canonical_id', ?, ?, ?)""",
                (
                    cur.lastrowid,
                    f"coin-{i}",
                    ANCHOR,
                    overlay_status or "verified_canonical",
                    overlay_version or RECOMPUTE_SEMANTICS,
                    ANCHOR,
                ),
            )
        await db._conn.execute(
            """INSERT INTO gainers_comparisons_legacy_prefix_v1
               (id, coin_id, symbol, name, appeared_on_gainers_at, detected_by_chains,
                chains_lead_minutes, is_gap, chains_identity_semantics, created_at)
               VALUES (?, ?, 'X', 'X', ?, 1, 8740.0, 0, 'legacy_prefix', ?)""",
            (cur.lastrowid, f"coin-{i}", ANCHOR, ANCHOR),
        )
    await db._conn.commit()


async def _mark(db, rate, population):
    await db._conn.execute(
        "INSERT OR REPLACE INTO recompute_coverage_baseline VALUES (?, ?, ?, ?)",
        ("gainers_comparisons", rate, population, ANCHOR),
    )
    await db._conn.commit()


def _checker_pages(db_path) -> bool:
    r = subprocess.run(
        [sys.executable, str(CHECKER), "--db", str(db_path), "--gate-minutes", "1440"],
        capture_output=True,
        text=True,
    )
    assert r.returncode in (0, 1), f"checker errored: {r.returncode} {r.stdout}"
    return r.returncode == 1


#: (label, population, recovered, mark, overlay_version)
#:
#: Chosen so that each of the four historical drifts between these two layers
#: produces a DISAGREEMENT here. The first version of this table covered every
#: state that had moved during review and caught only one of the four —
#: because "states that moved" and "states that discriminate" are different
#: sets, and only the second is worth parametrising over.
STATES = [
    ("stable at the judging floor", 20, 18, (0.90, 20), None, None),
    ("stable mid-band", 30, 27, (0.90, 30), None, None),
    ("stable at the band's top edge", 39, 35, (0.90, 39), None, None),
    ("stable just above the band", 40, 36, (0.90, 40), None, None),
    ("stable well above", 60, 54, (0.90, 60), None, None),
    ("growth from a large mark", 250, 46, (0.60, 100), None, None),
    ("growth from a small mark", 60, 18, (0.90, 25), None, None),
    ("no baseline recorded", 100, 20, None, None, None),
    ("nothing recovered at all", 40, 0, (0.90, 40), None, None),
    ("healthy at the mark", 40, 36, (0.90, 40), None, None),
    # Rate between 0.5x and 0.9x the mark: separates the two thresholds, so a
    # drifted COLLAPSE_FRACTION makes one layer page and the other not.
    ("between the two collapse thresholds", 100, 60, (0.90, 100), None, None),
    # A collapse INSIDE the judging band: a drifted MIN_POPULATION lifts one
    # layer's floor above it and the two disagree about whether to judge.
    ("collapse inside the judging band", 30, 3, (0.90, 30), None, None),
    # Every overlay row a superseded generation: without the version filter a
    # layer counts them as recovered and stays quiet while the other pages.
    ("every overlay row superseded", 40, 36, (0.90, 40), "superseded_v0", None),
    # Overlay rows present but NOT credit-bearing: a drifted CREDIT_BEARING
    # counts them in one layer and not the other.
    (
        "overlay rows that earn nothing",
        40,
        36,
        (0.90, 40),
        None,
        "indeterminate_history",
    ),
]


@pytest.mark.parametrize(
    "clear_succeeds", [True, False], ids=["clear-ok", "clear-starved"]
)
@pytest.mark.parametrize(
    "label,population,recovered,mark,overlay_version,overlay_status",
    STATES,
    ids=[s[0] for s in STATES],
)
async def test_both_layers_reach_the_same_verdict(
    db,
    tmp_path,
    label,
    population,
    recovered,
    mark,
    overlay_version,
    overlay_status,
    clear_succeeds,
):
    """One database, two implementations, one verdict.

    The assertion is AGREEMENT, deliberately — not a hardcoded expectation per
    state. A test asserting what each layer should say would drift with the
    thresholds; a test asserting they say the same thing survives any change to
    them, and disagreement is the actual defect in every one of the four
    findings this file exists for.
    """
    await _populate(db, population, recovered, overlay_version, overlay_status)
    if mark is not None:
        await _mark(db, *mark)
    # Collapse the collapse-detection state into the same file the checker
    # reads, then let each layer decide for itself.
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db._conn.commit()

    # The RUNTIME axis. Every state above varies the DATABASE; the discriminating
    # variable for a whole class of divergence is whether a best-effort write
    # SUCCEEDED, which is orthogonal to all of them. Without this the harness
    # held the exact data state that diverges and still could not see it -- a
    # "both layers agree" harness cannot cover a component whose behaviour
    # depends on the outcome of a write unless it varies that outcome.
    from scout.db import Database as _Db

    original = _Db._clear_coverage_baseline
    if not clear_succeeds:

        async def _starved(_self, _table):
            return False

        _Db._clear_coverage_baseline = _starved
    try:
        probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    finally:
        _Db._clear_coverage_baseline = original
    probe_pages = bool(probe["not_recovering"])
    checker_pages = _checker_pages(db._db_path)

    assert probe_pages == checker_pages, (
        f"{label} [clear {'ok' if clear_succeeds else 'starved'}]: probe "
        f"{'pages' if probe_pages else 'is quiet'} while the "
        f"checker {'pages' if checker_pages else 'is quiet'} — the two windows "
        "an operator has onto this system disagree"
    )


async def test_the_agreement_harness_can_actually_fail(db, tmp_path):
    """A harness that cannot disagree proves nothing.

    Constructs the exact divergence the checker's guard was fixed for — a mark
    from a large population against a grown one — and asserts BOTH layers page.
    Before the fix the probe paged and the checker exited 0, so this is the
    state that would have caught it.
    """
    await _populate(db, 250, 46)
    await _mark(db, 0.60, 100)
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db._conn.commit()

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)

    assert probe["not_recovering"] is True, "fixture no longer produces a collapse"
    assert _checker_pages(db._db_path) is True
