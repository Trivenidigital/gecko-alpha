"""The §12a watchdog must exit nonzero exactly when credit is not recovering.

An in-process `logger.error` is not an alert on this box -- nothing greps
journald for it. The sibling it was modelled on (`signal_outcome_ledger_REOPEN_E`)
has the same hole, so "we followed the convention" was not evidence of
coverage.

Exit codes are the contract the shell wrapper reads, so they are what gets
asserted: 0 healthy / 1 recovering nothing / 2 cannot run.
"""

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
):
    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
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
        "CREATE TABLE chain_identity_recompute_v1 (source_table TEXT, "
        "source_row_id INTEGER, coin_id TEXT, historical_anchor TEXT, "
        "evidence_status TEXT, canonical_lead REAL)"
    )
    for i in range(population):
        conn.execute(
            "INSERT INTO gainers_comparisons VALUES (?, ?, ?, 1, ?)",
            (i + 1, f"coin-{i}", ANCHOR, semantics),
        )
        if overlay_status:
            conn.execute(
                "INSERT INTO chain_identity_recompute_v1 VALUES "
                "('gainers_comparisons', ?, ?, ?, ?, ?)",
                (i + 1, f"coin-{i}", ANCHOR, overlay_status, canonical_lead),
            )
    conn.commit()
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
    broken -- so it hand-copies two constants. Copies drift.

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

    assert dict(wd.SURFACES) == dict(
        Database._RECOMPUTE_SURFACES
    ), "the watchdog watches a different set of surfaces than the application"
    assert set(wd.CREDIT_BEARING) == set(
        APP_CREDIT_BEARING
    ), "the watchdog counts a different set of statuses as credit-bearing"
