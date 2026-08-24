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

IN THIS COMPONENT, THE PROBE IS NOT AN OBSERVER -- IT IS A WRITER.
`chain_identity_recompute_coverage_probe` records, clears and re-arms the
ratchet mark. Running it before the checker therefore CHANGES the state the
checker reads, and a fixture that does so can destroy the exact condition it
was built to create. Two people hit this independently on this file: a
reviewer got a false negative from it, and a later test written to kill a
surface-conditional mutant SURVIVED because the probe cleared the incomparable
mark and re-armed a comparable one before the checker ran.

Write the state, checkpoint, then read with each layer. If a test needs the
probe's own verdict as well, checkpoint again AFTER it -- the probe's writes go
to a WAL the checker's `immutable=1` connection cannot see.
"""

import sqlite3
import re
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
    # The checker emits its own staleness warning when it sees a `-wal` that
    # `immutable=1` cannot read. This helper used to read only the return code
    # and discard stdout, so that warning was printed on every call in this file
    # and never once observed -- the tool reported the exact defect and the
    # harness threw the message away. Failing on it is self-maintaining: any
    # future test that checkpoints in the wrong order trips here rather than
    # silently measuring a stale view.
    assert "cannot see" not in r.stdout, (
        "the checker read a stale view -- checkpoint AFTER the last write:\n"
        + r.stdout
    )
    return r.returncode == 1


def _checker_marks(db_path) -> dict:
    """What mark the CHECKER sees per surface, parsed from its alert text.

    Exists because `_checker_pages` returns only a boolean, and the boolean is
    computed from `not_recovering` -- which does not depend on the ratchet mark
    at all. So the `clear_succeeds` axis this file added could vary a
    best-effort WRITE outcome and every assertion stayed identical: a reviewer
    replaced BOTH baseline write functions with total no-ops and all 40 tests
    still passed. The axis was inert in the assertion, not only in the view.
    """
    r = subprocess.run(
        [sys.executable, str(CHECKER), "--db", str(db_path), "--gate-minutes", "1440"],
        capture_output=True,
        text=True,
    )
    marks = {}
    for token in re.findall(r"(\w+_comparisons)=\d+/\d+ mark=([^\s)]+)", r.stdout):
        table, raw = token
        marks[table] = None if raw == "none" else float(raw)
    return marks


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
    # CHECKPOINT AGAIN, AFTER the probe. The one above lands the data state; the
    # probe then WRITES (`_record_coverage_baseline` / `_clear_coverage_baseline`,
    # each on its own connection), and those writes go to a WAL created after
    # that checkpoint. The checker opens `immutable=1`, which ignores the WAL by
    # definition -- so it read the pre-probe view and the entire `clear_succeeds`
    # axis was INERT: both arms produced identical checker output.
    #
    # Measured by a reviewer: replacing both baseline write functions with total
    # no-ops left all 35 tests in this file passing. A harness that varies a
    # write outcome the reader cannot observe is not covering that axis.
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db._conn.commit()

    probe_pages = bool(probe["not_recovering"])
    checker_pages = _checker_pages(db._db_path)

    # BOTH LAYERS MUST AGREE ABOUT THE MARK, not only about whether to page.
    # This is what makes the `clear_succeeds` axis mean something: the probe's
    # `best_rate` is the outcome of a best-effort write, and the checker reads
    # that same row independently. Without this assertion the write could
    # no-op entirely and nothing here would notice.
    seen = _checker_marks(db._db_path)
    for table, v in probe["per_surface"].items():
        if table in seen:
            pb, cb = v.get("best_rate"), seen[table]
            if pb is None or cb is None:
                assert pb == cb, (
                    f"{table}: probe best_rate={pb} but checker sees mark={cb} "
                    "-- the layers disagree about whether a mark exists"
                )
            else:
                assert abs(pb - cb) < 1e-3, (
                    f"{table}: probe best_rate={pb} but checker sees mark={cb}"
                )

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


# --------------------------------------------------------------------------
# SURFACE. The axis this bridge never varied.
# --------------------------------------------------------------------------

SURFACE_COLS = {
    "gainers_comparisons": "appeared_on_gainers_at",
    "losers_comparisons": "appeared_on_losers_at",
    "trending_comparisons": "appeared_on_trending_at",
}


async def _populate_surface(db, table, population, recovered, prefix=""):
    """Same shape as `_populate`, but for any surface.

    `_populate` and `_mark` hardcode `gainers_comparisons`, and so does
    `test_recompute_coverage_watchdog._build`. The only non-gainers rows
    anywhere in the tree are 5 losers rows that sit below
    `COLLAPSE_MIN_POPULATION` and describe a `dark` case -- so the CHECKER's
    collapse branch had never once been reached with a non-gainers surface.

    A reviewer proved the consequence: adding `and table ==
    "gainers_comparisons"` to the checker's collapse condition survives all
    7,135 tests. That is the F3 defect again, on the PAGING layer, where it is
    strictly worse -- the probe's verdict goes to journald and, by the
    checker's own docstring, nothing greps journald for it.

    This file is the cross-layer bridge. It varies population, rate, version,
    status and clear-outcome. It never varied surface.
    """
    anchor_col = SURFACE_COLS[table]
    for i in range(population):
        cur = await db._conn.execute(
            f"""INSERT INTO {table}
               (coin_id, symbol, name, {anchor_col}, detected_by_chains,
                chains_lead_minutes, is_gap, created_at, chains_identity_semantics)
               VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
            (f"{table}-{prefix}{i}", ANCHOR, ANCHOR),
        )
        if i < recovered:
            await db._conn.execute(
                """INSERT INTO chain_identity_recompute_v1
                   (source_table, source_row_id, coin_id, symbol, historical_anchor,
                    legacy_detected, legacy_lead, canonical_detected, canonical_lead,
                    identity_tier, evidence_status, semantics_version, computed_at)
                   VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                           'canonical_id', 'verified_canonical', ?, ?)""",
                (table, cur.lastrowid, f"{table}-{prefix}{i}", ANCHOR, RECOMPUTE_SEMANTICS, ANCHOR),
            )
    await db._conn.commit()


async def _mark_surface(db, table, rate, population):
    await db._conn.execute(
        "INSERT OR REPLACE INTO recompute_coverage_baseline VALUES (?, ?, ?, ?)",
        (table, rate, population, ANCHOR),
    )
    await db._conn.commit()


@pytest.mark.parametrize("table", sorted(SURFACE_COLS))
async def test_the_CHECKER_pages_a_collapse_on_every_surface(db, tmp_path, table):
    """The paging layer must see a collapse wherever it happens.

    Armed high, then collapsed -- on each surface in turn. Under the
    surface-conditional mutant this fails for losers and trending while gainers
    still passes, which is exactly the shape that hid for 7,135 tests.
    """
    await _populate_surface(db, table, 100, 90)
    await _mark_surface(db, table, 0.9, 100)
    # Collapse: many more credit-bearing rows, none of them recovered.
    # PREFIXED, because the overlay correlates on coin_id: without it the
    # second batch reused ids 0-99 and 90 of them still matched the FIRST
    # batch's overlay rows, so the rate landed at 180/500 = 0.36 rather than
    # the intended 90/500 = 0.18. It still collapsed, but at a third of the
    # intended margin below the 0.45 threshold -- a later threshold tweak would
    # have silently stopped exercising a collapse at all.
    await _populate_surface(db, table, 400, 0, prefix="collapse-")
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()

    assert _checker_pages(tmp_path / "scout.db"), (
        f"the checker did not page a collapse on {table} -- collapse detection "
        "is surface-conditional on the layer that actually alerts"
    )


@pytest.mark.parametrize("table", sorted(SURFACE_COLS))
async def test_the_CHECKER_stays_quiet_on_a_healthy_surface(db, tmp_path, table):
    """The other half: it must not page when nothing collapsed.

    Without this the paging test above is satisfiable by a checker that always
    exits 1.
    """
    await _populate_surface(db, table, 100, 90)
    await _mark_surface(db, table, 0.9, 100)
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()

    assert not _checker_pages(tmp_path / "scout.db"), (
        f"the checker paged on {table} while recovery was healthy"
    )


async def test_the_NOT_ARMED_notice_names_each_unarmed_surface(db, tmp_path):
    """One armed surface must not suppress the notice for the others.

    `have_baseline` was a global OR: any surface with a row silenced the notice
    for all of them. A reviewer reproduced the consequence -- alert text
    printing `mark=none` twice with no notice, then a real collapse on an
    unarmed surface exiting 0 in silence. That is the shape this file's own
    comment says it already fixed twice, repeated one layer over in the notice.
    """
    for table in sorted(SURFACE_COLS):
        await _populate_surface(db, table, 60, 30)
    await _mark_surface(db, "gainers_comparisons", 0.5, 60)  # ONLY gainers armed
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()

    r = subprocess.run(
        [sys.executable, str(CHECKER), "--db", str(tmp_path / "scout.db"),
         "--gate-minutes", "1440"],
        capture_output=True, text=True,
    )
    assert "NOT ARMED" in r.stdout, (
        "one armed surface suppressed the notice for two unarmed ones:\n" + r.stdout
    )
    assert "losers_comparisons" in r.stdout and "trending_comparisons" in r.stdout
    assert "NOT ARMED on gainers_comparisons" not in r.stdout, (
        "gainers IS armed and must not be named as unarmed"
    )
    # WHICH condition, not merely that a notice fired. The pair added to fix
    # the unreadable-table residual is asymmetric: replacing
    # ": baseline unreadable" dies, but rewording ": no baseline recorded yet"
    # INTO the unreadable wording survived the whole suite. The two states have
    # different remedies -- "run the arm script" vs "find out why the table
    # cannot be read" -- so pinning the prefix and the surface names is not
    # enough. One-branch-not-the-axis, inside the notice pair itself.
    assert "no baseline recorded yet" in r.stdout, (
        "the unarmed notice does not say WHICH condition it reports; it could "
        "be reworded as an infrastructure fault and nothing would fail:\n"
        + r.stdout
    )
    assert "unreadable" not in r.stdout, (
        "reported an unreadable baseline when the table reads fine:\n" + r.stdout
    )


async def test_a_BELOW_FLOOR_surface_is_not_called_unarmed(db, tmp_path):
    """The other direction: the notice must not cry wolf on the steady state.

    A surface under COLLAPSE_MIN_POPULATION is not judged on rate by design --
    production's trending surface drains through it. Naming it unarmed would
    make the notice permanent, and a permanent notice is an ignored one.
    """
    await _populate_surface(db, "gainers_comparisons", 60, 30)
    await _mark_surface(db, "gainers_comparisons", 0.5, 60)
    await _populate_surface(db, "trending_comparisons", 5, 2)  # below the floor
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()

    r = subprocess.run(
        [sys.executable, str(CHECKER), "--db", str(tmp_path / "scout.db"),
         "--gate-minutes", "1440"],
        capture_output=True, text=True,
    )
    assert "trending_comparisons" not in r.stdout.split("NOT ARMED")[-1] or "NOT ARMED" not in r.stdout, (
        "a below-floor surface was named unarmed; the notice would never clear:\n" + r.stdout
    )


@pytest.mark.parametrize("table", sorted(SURFACE_COLS))
async def test_the_CHECKER_comparability_guard_holds_on_every_surface(db, tmp_path, table):
    """The guard TWO LINES ABOVE the collapse condition, on the same axis.

    The previous round parametrised the checker's collapse condition over
    surfaces. The comparability guard immediately above it was left
    surface-blind, and a reviewer showed a mutant appending
    `and table == "gainers_comparisons"` to it survives all 176 tests.

    Its consequence is a probe/checker DISAGREEMENT -- the exact failure class
    this file exists to prevent. Measured under that mutant: gainers exit 0,
    losers and trending exit 1 while the probe reports `collapsed_surfaces: []`
    for all three. A false page on two surfaces, from the layer that pages.

    It hid because both new checker tests arm at population=100, where the
    guard never fires either way, and the one guard test in the tree is
    gainers-only. Same file, same loop, same axis, two lines apart -- the
    fourth instance on this PR of a fix closing one BRANCH and not the AXIS.

    State: a mark of 0.90 recorded at population 25, today's population 60 at
    rate 0.30. `recorded_pop < FLOOR*2 and pop > recorded_pop*2` holds, so the
    mark is not comparable and the checker must stay quiet.
    """
    await _populate_surface(db, table, 60, 18)  # 0.30 today
    # The mark is written LAST and the probe is NOT run. An earlier version of
    # this test ran the probe first -- which is a WRITER: it saw the
    # incomparable mark, cleared it, and re-armed at 0.30@60. The checker then
    # read a comparable mark, the guard was never reached, and the mutant
    # SURVIVED a test written to kill it. The fixture destroyed the state it
    # existed to create.
    await _mark_surface(db, table, 0.9, 25)  # 0.90 recorded against 25 rows
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()

    assert not _checker_pages(tmp_path / "scout.db"), (
        f"the CHECKER paged on {table} against a mark recorded at population "
        "25 while the probe did not -- the comparability guard is "
        "surface-conditional on the paging layer, so probe and checker "
        "disagree about the same surface"
    )


async def test_a_RAISED_mark_reaches_the_checker(db, tmp_path):
    """The write outcome must be observable, not just self-consistent.

    Agreement alone cannot see a failed write. When `_record_coverage_baseline`
    returns None the probe honestly falls back to reporting the table's
    existing value -- so probe and checker still agree, and a reviewer showed
    that replacing BOTH baseline write functions with total no-ops left every
    test in this file passing, including the mark-agreement assertion added to
    catch exactly that.

    The discriminating fact is not "do the layers match" but "did the mark
    MOVE". A genuine improvement must raise the stored mark AND that raise must
    reach the checker, which reads the row independently.
    """
    await _populate(db, 100, 90)  # 0.90 today
    await _mark(db, 0.20, 100)  # a much lower stored mark -> raise_mark

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db._conn.commit()

    v = probe["per_surface"]["gainers_comparisons"]
    assert v["mark_written"] is True, (
        "a genuine improvement did not write the mark -- the ratchet is not "
        "ratcheting, and every agreement assertion here would still pass"
    )
    assert v["best_rate"] == pytest.approx(0.90, abs=1e-3)

    seen = _checker_marks(tmp_path / "scout.db")
    assert seen["gainers_comparisons"] == pytest.approx(0.90, abs=1e-3), (
        f"the checker still sees the OLD mark {seen['gainers_comparisons']} "
        "after the probe raised it to 0.90 -- the write never reached the "
        "layer that pages"
    )


async def test_a_CLEARED_mark_reaches_the_checker(db, tmp_path):
    """The clear-side twin, for the same reason."""
    await _populate(db, 60, 18)  # 0.30 today
    await _mark(db, 0.90, 25)  # incomparable -> clear, then re-arm at 0.30

    probe = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db._conn.commit()

    v = probe["per_surface"]["gainers_comparisons"]
    assert v.get("comparison_skipped") is None, (
        "the clear failed, so this test is measuring the starved path rather "
        "than the one it names"
    )
    assert v["best_rate"] == pytest.approx(0.30, abs=1e-3)

    seen = _checker_marks(tmp_path / "scout.db")
    assert seen["gainers_comparisons"] == pytest.approx(0.30, abs=1e-3), (
        f"the checker still sees {seen['gainers_comparisons']} after the probe "
        "discarded the incomparable mark and re-armed at 0.30"
    )


async def test_an_UNREADABLE_baseline_table_says_so(db, tmp_path):
    """Residual 1: the notice branch for an absent baseline table had no test.

    A reviewer showed `if False and ...` on that branch survives the whole
    suite: the notice vanishes and the line reads as a clean all-clear -- the
    exact "no baseline yet reads identically to no collapse" wrong reassurance
    the comment three lines above says the notice exists to prevent.
    """
    await _populate(db, 60, 30)
    await db._conn.execute("DROP TABLE recompute_coverage_baseline")
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db._conn.commit()
    await db.close()

    r = subprocess.run(
        [sys.executable, str(CHECKER), "--db", str(tmp_path / "scout.db"),
         "--gate-minutes", "1440"],
        capture_output=True, text=True,
    )
    assert "NOT ARMED" in r.stdout, (
        "the baseline table is unreadable and the line reads as a clean "
        "all-clear:\n" + r.stdout
    )
    assert "unreadable" in r.stdout, r.stdout
    # Every surface must be named, not a single global mention.
    for table in SURFACE_COLS:
        assert table in r.stdout.split("NOT ARMED")[-1], (
            f"{table} was not named as unreadable:\n" + r.stdout
        )


async def test_a_TRANSIENT_read_error_on_ONE_surface_is_reported(db, tmp_path, monkeypatch):
    """The `except sqlite3.Error: continue` path, driven in-process.

    Two reviewers reached this and neither could execute it: the checker opens
    `immutable=1` for non-live paths, so holding a lock produces no error, and
    there is no deterministic way to make one surface's read raise from
    outside. One reported it as inference and said so; the other found the
    branch untested by grep. Both were right.

    It is reachable in-process, so it is tested in-process: `sqlite3.connect`
    is wrapped to raise on exactly one surface's baseline query. That is the
    MIXED case -- one surface unreadable while another holds a mark -- which is
    precisely what the `break` -> `continue` change was made to support, and
    which the notice could not describe until it went per-surface.

    The repo's own lesson applies: `except:` blocks are untested code, three
    prior instances.
    """
    import importlib.util
    import sqlite3 as _sqlite3

    for table in sorted(SURFACE_COLS):
        await _populate_surface(db, table, 60, 30)
    await _mark_surface(db, "gainers_comparisons", 0.5, 60)
    await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()

    spec = importlib.util.spec_from_file_location("chk_cov", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    real_connect = _sqlite3.connect

    class _Raising:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if "recompute_coverage_baseline" in sql and params and params[0] == (
                "losers_comparisons"
            ):
                raise _sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(
        mod.sqlite3, "connect", lambda *a, **k: _Raising(real_connect(*a, **k))
    )
    monkeypatch.setattr(
        mod.sys, "argv",
        ["check", "--db", str(tmp_path / "scout.db"), "--gate-minutes", "1440"],
    )

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main()
    out = buf.getvalue()

    assert rc in (0, 1), out
    assert "losers_comparisons" in out, out
    assert "unreadable" in out, (
        "one surface's baseline read failed and the line does not say so -- "
        "its collapse comparison was skipped silently:\n" + out
    )
    # The OTHER surfaces must still have been compared, not abandoned.
    assert "trending_comparisons" in out, out
    # BIND surface to condition in ONE string. Asserting the surfaces and the
    # wordings separately leaves the PAIRING unpinned: a reviewer swapped which
    # list goes under which wording and this test stayed green, because every
    # substring was still present -- both remedies simply pointed at the wrong
    # surface. "baseline unreadable" sends the operator hunting a filesystem
    # fault; "no baseline recorded yet" sends them to run the arm script. Same
    # words, opposite half-hour.
    #
    # The mixed case is the ONLY state where the binding can be wrong, and it
    # is the state the `break` -> `continue` change exists to support -- so it
    # is exactly the one that has to pin it.
    assert "losers_comparisons: baseline unreadable" in out, (
        "the unreadable surface is not bound to the unreadable wording:\n" + out
    )
    assert "losers_comparisons: no baseline recorded yet" not in out, (
        "the unreadable surface is reported as merely un-armed -- wrong "
        "remedy:\n" + out
    )
