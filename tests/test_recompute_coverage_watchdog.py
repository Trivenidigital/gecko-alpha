"""The §12a watchdog must exit nonzero exactly when credit is not recovering.

An in-process `logger.error` is not an alert on this box -- nothing greps
journald for it. The sibling it was modelled on (`signal_outcome_ledger_REOPEN_E`)
has the same hole, so "we followed the convention" was not evidence of
coverage.

Exit codes are the contract the shell wrapper reads, so they are what gets
asserted: 0 healthy / 1 recovering nothing / 2 cannot run.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_recompute_coverage.py"
ANCHOR = "2026-08-01T00:00:00+00:00"


def _run(db_path, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), *extra],
        capture_output=True,
        text=True,
    )


def _build(
    tmp_path,
    *,
    overlay_status=None,
    population=1,
    semantics="legacy_prefix",
    canonical_lead=8740.0,
    at=None,
    checkpoint=True,
):
    # `at` lets a test build the fixture at an explicit path (needed to make
    # one the script's LIVE_DB); `checkpoint=False` leaves rows in the WAL,
    # which is what distinguishes a mode=ro open from an immutable one.
    db = Path(at) if at else tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    # WAL, explicitly. A `mode=ro` open creates no sidecars against a DELETE-mode
    # database, so the sidecar assertion below would pass vacuously -- it could
    # not have failed. The sibling backfill fixture sets this for the same reason.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE gainers_comparisons (id INTEGER PRIMARY KEY, coin_id TEXT, "
        "appeared_on_gainers_at TEXT, detected_by_chains INTEGER, "
        "chains_identity_semantics TEXT)"
    )
    for t, col in (
        ("losers", "appeared_on_losers_at"),
        ("trending", "appeared_on_trending_at"),
    ):
        conn.execute(
            f"CREATE TABLE {t}_comparisons (id INTEGER PRIMARY KEY, coin_id TEXT, "
            f"{col} TEXT, detected_by_chains INTEGER, chains_identity_semantics TEXT)"
        )
    conn.execute(
        "CREATE TABLE recompute_coverage_baseline (source_table TEXT PRIMARY KEY, "
        "best_rate REAL NOT NULL, population INTEGER NOT NULL, recorded_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE chain_identity_recompute_v1 (source_table TEXT, "
        "source_row_id INTEGER, coin_id TEXT, historical_anchor TEXT, "
        "evidence_status TEXT, canonical_lead REAL, semantics_version TEXT)"
    )
    for i in range(population):
        conn.execute(
            "INSERT INTO gainers_comparisons VALUES (?, ?, ?, 1, ?)",
            (i + 1, f"coin-{i}", ANCHOR, semantics),
        )
        if overlay_status:
            # source_row_id deliberately DISAGREES with the comparison row's
            # id. With both keys agreeing in every case, a mutant swapping the
            # correlation from coin_id to source_row_id could not be caught --
            # the same fixture blindness that hid this defect in the probe.
            conn.execute(
                "INSERT INTO chain_identity_recompute_v1 VALUES "
                "('gainers_comparisons', ?, ?, ?, ?, ?, ?)",
                (
                    i + 5000,
                    f"coin-{i}",
                    ANCHOR,
                    overlay_status,
                    canonical_lead,
                    "chain_identity_recompute_v1",
                ),
            )
    conn.commit()
    if checkpoint:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return db


def test_exits_1_when_the_overlay_was_never_populated(tmp_path):
    r = _run(_build(tmp_path, overlay_status=None))
    assert r.returncode == 1, r.stdout
    assert "recovering NOTHING" in r.stdout


def test_exits_1_when_the_overlay_is_full_but_recovers_nothing(tmp_path):
    """Row-counting reports healthy here. This is the likeliest real state."""
    r = _run(_build(tmp_path, overlay_status="indeterminate_history", population=5))
    assert r.returncode == 1, r.stdout
    assert "0 of 5" in r.stdout


def test_exits_0_when_credit_is_being_recovered(tmp_path):
    r = _run(_build(tmp_path, overlay_status="verified_canonical", population=3))
    assert r.returncode == 0, r.stdout
    assert "recovered 3 of 3" in r.stdout


def test_exits_0_on_a_fresh_install_with_nothing_to_recover(tmp_path):
    """Must not page where there is no pre-cutover population at all."""
    r = _run(_build(tmp_path, overlay_status=None, population=0))
    assert r.returncode == 0, r.stdout


def test_exits_0_when_the_schema_has_not_shipped(tmp_path):
    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    r = _run(db)
    assert r.returncode == 0, r.stdout
    assert "absent" in r.stdout


def test_exits_2_when_the_database_cannot_be_read(tmp_path):
    """Distinct from healthy: the wrapper must not treat this as an all-clear."""
    r = _run(tmp_path / "does-not-exist.db")
    assert r.returncode == 2, r.stdout


def test_NULL_semantics_rows_count_toward_the_population(tmp_path):
    """The reader trusts only 'canonical_v1', so NULL is untrusted there too."""
    r = _run(_build(tmp_path, overlay_status=None, population=2, semantics=None))
    assert r.returncode == 1, r.stdout
    assert "0 of 2" in r.stdout


def test_canonical_rows_are_outside_the_population(tmp_path):
    """A row already stamped canonical has no legacy provenance to recover."""
    r = _run(
        _build(tmp_path, overlay_status=None, population=2, semantics="canonical_v1")
    )
    assert r.returncode == 0, r.stdout


def test_the_watchdog_never_creates_wal_sidecars(tmp_path):
    db = _build(tmp_path, overlay_status="verified_canonical")
    for s in ("-wal", "-shm"):
        Path(str(db) + s).unlink(missing_ok=True)
    _run(db)
    assert [s for s in ("-wal", "-shm") if Path(str(db) + s).exists()] == []


def test_pointing_the_watchdog_at_a_COPY_creates_no_sidecars(tmp_path):
    """This is the script an operator reaches for while diagnosing.

    That is exactly when a tool gets pointed at a copy, and `mode=ro` creates
    `-wal`/`-shm` beside whatever it opens. Sidecars beside backups caused the
    integrity checker to delete every real backup on 2026-08-15, and the same
    bug was found live in three more readers a day later -- one carrying a
    comment claiming parity with the other two. This was a fourth.
    """
    db = _build(tmp_path, overlay_status="verified_canonical")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    for s in ("-wal", "-shm"):
        Path(str(db) + s).unlink(missing_ok=True)

    _run(db)

    assert [s for s in ("-wal", "-shm") if Path(str(db) + s).exists()] == []


def test_a_raised_gate_is_visible_to_the_watchdog(tmp_path):
    """`evidence_status` freezes the gate as it stood at backfill time."""
    db = _build(tmp_path, overlay_status="verified_canonical", population=4)

    assert _run(db, "--gate-minutes", "1440").returncode == 0
    raised = _run(db, "--gate-minutes", "99999")
    assert raised.returncode == 1, raised.stdout
    assert "0 of 4" in raised.stdout


def test_a_DROPPED_overlay_table_is_not_reported_as_not_deployed(tmp_path):
    """Absent means two different things, and only one of them is fine.

    Before the deploy it is normal. After it, something dropped the table --
    the loudest state there is, previously reported as an all-clear.
    """
    db = _build(tmp_path, overlay_status="verified_canonical")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE gainers_comparisons_legacy_prefix_v1 (id INTEGER)")
    conn.execute("DROP TABLE chain_identity_recompute_v1")
    conn.commit()
    conn.close()

    r = _run(db)

    assert r.returncode == 1, r.stdout
    assert "dropped after deploy" in r.stdout


def test_the_watchdog_constants_match_the_application(tmp_path):
    """The watchdog is stdlib-only on purpose -- it must run when the app is
    broken -- so it hand-copies FIVE constants. Copies drift.

    A mutant dropping losers and trending from the script's SURFACES survived
    the whole suite: the gap closed in the in-process probe had reopened in
    the out-of-process alarm, which is the one that actually pages.
    """
    import importlib.util

    from scout.db import Database
    from scout.identity_recompute import CREDIT_BEARING as APP_CREDIT_BEARING

    spec = importlib.util.spec_from_file_location("wd", SCRIPT)
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)

    # All FIVE copied constants, not two. The script's own comments claimed
    # "(parity asserted by test)" for RECOMPUTE_SEMANTICS,
    # COLLAPSE_MIN_POPULATION and COLLAPSE_FRACTION while this test covered
    # only SURFACES and CREDIT_BEARING -- the fourth documented-parity claim
    # in this tranche that the artifact did not satisfy.
    import scout.db as _dbmod

    assert (
        wd.COLLAPSE_MIN_POPULATION == _dbmod._COLLAPSE_MIN_POPULATION
    ), "the watchdog judges a different minimum population than the probe"
    assert (
        wd.COLLAPSE_FRACTION == _dbmod._COLLAPSE_FRACTION
    ), "the watchdog uses a different collapse threshold than the probe"
    assert dict(wd.SURFACES) == dict(
        Database._RECOMPUTE_SURFACES
    ), "the watchdog watches a different set of surfaces than the application"
    assert set(wd.CREDIT_BEARING) == set(
        APP_CREDIT_BEARING
    ), "the watchdog counts a different set of statuses as credit-bearing"


def test_the_watchdog_pages_when_only_ONE_surface_is_dark(tmp_path):
    """It was printing the dark surfaces in its own alert text and exiting 0.

    Reachable because the replay commits per surface: a mid-run failure leaves
    earlier surfaces durable, so a global "recovered nothing" is satisfied by
    the one healthy surface.
    """
    db = _build(tmp_path, overlay_status="verified_canonical", population=3)
    conn = sqlite3.connect(db)
    for i in range(3):
        conn.execute(
            "INSERT INTO losers_comparisons VALUES (?, ?, ?, 1, 'legacy_prefix')",
            (i + 1, f"l{i}", ANCHOR),
        )
    conn.commit()
    conn.close()

    r = _run(db)

    assert r.returncode == 1, r.stdout
    assert "losers_comparisons" in r.stdout
    assert "recovering NOTHING on" in r.stdout


def test_the_alert_says_when_the_backfill_CANNOT_help(tmp_path):
    """An unfixable page must not send the operator to a remedy that fails.

    Rows written after the archives were taken can never be covered — the
    archive step self-guards and never re-runs, and the backfill only reads
    archives. Telling someone to re-run it is how an alarm earns a mute.
    """
    db = _build(tmp_path, overlay_status=None, population=3)
    conn = sqlite3.connect(db)
    # No archive table at all: every row post-dates it.
    conn.commit()
    conn.close()

    r = _run(db)

    assert r.returncode == 1, r.stdout
    assert "3 unarchivable" in r.stdout
    assert "THE BACKFILL CANNOT HELP" in r.stdout


def test_a_normal_page_still_points_at_the_backfill(tmp_path):
    """The other direction: archived rows ARE recoverable, so say so."""
    db = _build(tmp_path, overlay_status=None, population=2)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE gainers_comparisons_legacy_prefix_v1 "
        "(id INTEGER, coin_id TEXT, appeared_on_gainers_at TEXT)"
    )
    for i in range(2):
        conn.execute(
            "INSERT INTO gainers_comparisons_legacy_prefix_v1 VALUES (?, ?, ?)",
            (i + 1, f"coin-{i}", ANCHOR),
        )
    conn.commit()
    conn.close()

    r = _run(db)

    assert r.returncode == 1, r.stdout
    assert "0 unarchivable" in r.stdout
    assert "--apply" in r.stdout
    assert "CANNOT HELP" not in r.stdout


def test_the_wrapper_never_hardcodes_a_gate_value():
    """Structural: the literal must not come back.

    Lives here rather than in the wrapper's own test file because it needs no
    bash -- that file skips on win32, which is where this would most likely be
    reintroduced.

    Behaviourally, a re-added `GATE=1440` fallback is indistinguishable from
    the correct path on any box where the code default is also 1440 — which is
    every box today.
    """
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "recompute-coverage-watchdog.sh"
    )
    text = wrapper.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert "GATE=1440" not in code.replace(
        " ", ""
    ), "a literal gate fallback reappeared in the wrapper"


def test_the_watchdog_pages_on_a_COLLAPSE_that_is_not_zero(tmp_path):
    """The runbook's own scenario, on the layer that actually notifies.

    Snapshots deleted, roughly four rows in five landing indeterminate — 20%,
    not zero. `dark` only fires at exactly zero, so this was invisible to the
    one component that sends Telegram.
    """
    db = _build(tmp_path, overlay_status="verified_canonical", population=40)
    conn = sqlite3.connect(db)
    # A recorded high-water rate of 1.0, against a population that now
    # recovers only a fraction.
    conn.execute(
        "INSERT INTO recompute_coverage_baseline VALUES "
        "('gainers_comparisons', 1.0, 40, '2026-08-01T00:00:00+00:00')"
    )
    conn.execute("DELETE FROM chain_identity_recompute_v1 WHERE source_row_id > 5004")
    conn.commit()
    conn.close()

    r = _run(db)

    assert r.returncode == 1, r.stdout
    assert "COLLAPSED" in r.stdout
    assert "0 of" not in r.stdout, "this is a collapse, not total loss"


def test_a_healthy_rate_at_the_high_water_mark_does_not_page(tmp_path):
    db = _build(tmp_path, overlay_status="verified_canonical", population=40)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO recompute_coverage_baseline VALUES "
        "('gainers_comparisons', 1.0, 40, '2026-08-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    r = _run(db)

    assert r.returncode == 0, r.stdout
    assert "COLLAPSED" not in r.stdout


def test_a_dotdot_spelling_of_the_live_db_still_sees_WAL_rows(tmp_path, monkeypatch):
    """`.resolve()` is a real behaviour difference, and it was unpinned.

    Plain `Path` equality normalises `.` and `//` but not `..`, so the
    documented invocation from the deploy directory misclassified the LIVE
    database as non-live. The consequence is not cosmetic: non-live means
    `immutable=1`, which IGNORES the WAL — so rows committed but not yet
    checkpointed are invisible and the alarm reports a stale all-clear.

    Asserted on that consequence rather than on `Path.resolve()` agreeing with
    itself, which is a property of pathlib and not of this script. The
    backfill's twin of this function got tests; this copy did not.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("wd_resolve", SCRIPT)
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)

    live = tmp_path / "gecko-alpha" / "scout.db"
    live.parent.mkdir(parents=True)
    # Population rows in the MAIN file, overlay rows in the WAL. That split is
    # what makes the test discriminate: `immutable=1` ignores the WAL, so a
    # misclassified live database sees the population but none of the recovery.
    # The first version put both in the main file and passed with `.resolve()`
    # removed.
    _build(tmp_path, overlay_status=None, population=3, at=live)
    keeper = sqlite3.connect(live)
    keeper.execute("PRAGMA journal_mode=WAL")
    for i in range(3):
        keeper.execute(
            "INSERT INTO chain_identity_recompute_v1 VALUES "
            "('gainers_comparisons', ?, ?, ?, 'verified_canonical', 8740.0, "
            "'chain_identity_recompute_v1')",
            (7000 + i, f"coin-{i}", ANCHOR),
        )
    keeper.commit()  # committed, NOT checkpointed -- lives in the -wal
    monkeypatch.setattr(wd, "LIVE_DB", str(live))

    dotdot = str(live.parent / ".." / "gecko-alpha" / "scout.db")
    assert Path(dotdot) != Path(str(live)), "fixture is not exercising `..`"

    monkeypatch.setattr(sys, "argv", ["prog", "--db", dotdot, "--gate-minutes", "1440"])
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = wd.main()
    out = buf.getvalue()

    keeper.close()
    assert "3 of 3" in out, (
        f"the `..` spelling was treated as non-live and opened immutable=1, "
        f"hiding the uncheckpointed WAL rows: {out!r}"
    )
    assert rc == 0


def test_it_says_when_collapse_detection_is_NOT_ARMED(tmp_path):
    """The collapse half is gated on the in-process probe having run.

    This script is read-only by design and cannot bootstrap its own mark. With
    the pipeline down and a backfill that degraded rather than zeroed, "no
    baseline yet" would read identically to "no collapse" — the wrong
    reassurance from the alarm built because the in-process log is silence.
    """
    db = _build(tmp_path, overlay_status="verified_canonical", population=25)

    r = _run(db)

    assert r.returncode == 0, r.stdout
    assert "NOT ARMED" in r.stdout


def test_it_does_not_say_not_armed_once_a_mark_exists(tmp_path):
    db = _build(tmp_path, overlay_status="verified_canonical", population=25)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO recompute_coverage_baseline VALUES "
        "('gainers_comparisons', 1.0, 25, '2026-08-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    r = _run(db)

    assert "NOT ARMED" not in r.stdout


def test_it_warns_when_immutable_cannot_see_a_non_empty_wal(tmp_path):
    """A scratch copy of a live database carries uncheckpointed rows.

    `immutable=1` ignores the -wal, so the read is stale while the message is
    the most reassuring one in the script. A scratch copy is exactly what the
    backfill's refusal recommends, so every rehearsal against one hits this.
    """
    db = _build(
        tmp_path, overlay_status="verified_canonical", population=25, checkpoint=False
    )
    keeper = sqlite3.connect(db)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute(
        "INSERT INTO chain_identity_recompute_v1 VALUES "
        "('gainers_comparisons', 9999, 'x', '2026-08-01T00:00:00+00:00', "
        "'verified_canonical', 8740.0, 'chain_identity_recompute_v1')"
    )
    keeper.commit()

    r = _run(db)
    keeper.close()

    assert "-wal that immutable=1 cannot see" in r.stdout, r.stdout


def test_the_watchdog_applies_the_population_comparability_guard(tmp_path):
    """A mark from a much smaller population is not comparable to today's.

    Closed in the probe and left open here — in the layer that actually sends
    Telegram — so the two windows an operator has onto this system
    contradicted each other: journald quiet, Telegram screaming COLLAPSED.
    Same shape as the `unarchivable` counter landing in the logging layer and
    not the paging one.
    """
    db = _build(tmp_path, overlay_status="verified_canonical", population=100)
    conn = sqlite3.connect(db)
    # Mark recorded when the population was 10; today it is 100.
    conn.execute(
        "INSERT INTO recompute_coverage_baseline VALUES "
        "('gainers_comparisons', 0.9, 10, '2026-08-01T00:00:00+00:00')"
    )
    conn.execute("DELETE FROM chain_identity_recompute_v1 WHERE source_row_id > 5029")
    conn.commit()
    conn.close()

    r = _run(db)

    assert (
        "COLLAPSED" not in r.stdout
    ), "paged against a mark measured on a tenth of the population"
    assert r.returncode == 0, r.stdout


def test_a_comparable_mark_still_pages(tmp_path):
    """The other direction, so the guard cannot just disable the alarm."""
    db = _build(tmp_path, overlay_status="verified_canonical", population=100)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO recompute_coverage_baseline VALUES "
        "('gainers_comparisons', 0.9, 100, '2026-08-01T00:00:00+00:00')"
    )
    conn.execute("DELETE FROM chain_identity_recompute_v1 WHERE source_row_id > 5029")
    conn.commit()
    conn.close()

    r = _run(db)

    assert r.returncode == 1, r.stdout
    assert "COLLAPSED" in r.stdout


def test_the_summary_names_the_mark_each_surface_was_compared_against(tmp_path):
    """Otherwise a collapse page cannot be told from a wrong mark."""
    db = _build(tmp_path, overlay_status="verified_canonical", population=100)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO recompute_coverage_baseline VALUES "
        "('gainers_comparisons', 0.9, 100, '2026-08-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    r = _run(db)

    assert "mark=0.9000" in r.stdout, r.stdout
    assert "mark=none" in r.stdout, "unarmed surfaces must say so per surface"
