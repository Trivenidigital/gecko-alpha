"""The backfill must not create WAL sidecars beside a file that is not live.

On 2026-08-15 a read-only open planted a 0-byte `-wal` next to the real
backups and the rotation then treated them as the newest backup, destroying
every genuine one. That bug had shipped in three separate readers.

`_open_read_only` guards against it by opening frozen files `immutable=1`,
which makes SQLite skip locking and ignore any `-wal` -- so no sidecar is
created. The live database is the deliberate exception: it has a running
writer, its sidecars already exist, and `immutable=1` there would hide
thousands of uncheckpointed rows.

The defect this pins: `live` was decided by comparing against the `--db`
ARGUMENT, so ANY target counted as live. Pointing the script at a backup --
the cautious thing `--dry-run` exists to allow -- opened it `mode=ro` and
planted the sidecars.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backfill_chain_identity_recompute import (  # noqa: E402
    LIVE_DB,
    _is_live,
    _open_read_only,
)


def _make_wal_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE signal_events (token_id TEXT, created_at TEXT)")
    conn.execute("INSERT INTO signal_events VALUES ('t', '2026-08-01T00:00:00+00:00')")
    conn.commit()
    conn.close()


def test_a_backup_is_not_treated_as_live(tmp_path):
    backup = tmp_path / "scout.db.bak.20260815"
    _make_wal_db(backup)
    assert _is_live(str(backup)) is False
    assert _is_live(LIVE_DB) is True


def test_opening_a_backup_creates_no_sidecars(tmp_path):
    backup = tmp_path / "scout.db.bak.20260815"
    _make_wal_db(backup)
    # Prove the fixture is meaningful: a WAL database with no sidecars on disk
    # right now is exactly the state a rotated backup is in.
    for suffix in ("-wal", "-shm"):
        Path(str(backup) + suffix).unlink(missing_ok=True)

    conn = _open_read_only(str(backup), live=_is_live(str(backup)))
    conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()
    conn.close()

    created = [s for s in ("-wal", "-shm") if Path(str(backup) + s).exists()]
    assert created == [], (
        f"opening a backup created {created}; the rotation counts these as "
        "backups and they would displace the real ones"
    )


def test_the_sidecar_check_can_actually_fail(tmp_path):
    """A negative control. Without this the test above passes on any bug that
    stops sidecars being created at all -- including one that stops the file
    being opened."""
    backup = tmp_path / "other.db"
    _make_wal_db(backup)
    for suffix in ("-wal", "-shm"):
        Path(str(backup) + suffix).unlink(missing_ok=True)

    # `live=True` is the buggy classification; it must produce the sidecars.
    conn = _open_read_only(str(backup), live=True)
    conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()
    conn.close()

    created = [s for s in ("-wal", "-shm") if Path(str(backup) + s).exists()]
    assert created, (
        "misclassifying a backup as live produced no sidecars, so the guard "
        "test above proves nothing on this platform"
    )


def test_apply_refuses_to_write_into_a_forensic_snapshot(tmp_path, monkeypatch, capsys):
    """`--apply` opens `--db` READ-WRITE, so a snapshot path would be mutated.

    Pointing --db at a scratch copy is legitimate (that is how the replay is
    measured), so the guard names the preserved snapshots rather than demanding
    identity with LIVE_DB.
    """
    import asyncio

    import backfill_chain_identity_recompute as mod

    snap = tmp_path / "scout.db.pre500.20260802202122"
    _make_wal_db(snap)
    monkeypatch.setattr(mod, "SNAPSHOT_SOURCES", (str(snap),))
    monkeypatch.setattr(
        sys, "argv", ["prog", "--db", str(snap), "--apply", "--gate-minutes", "1440"]
    )

    rc = asyncio.run(mod.main())
    out = capsys.readouterr().out

    assert rc == 2
    # WHICH gate refused, not merely THAT one did. Both refusal paths return 2,
    # and this fixture's database also lacks the overlay table -- so asserting
    # the code alone passed with the snapshot guard removed entirely. The
    # sidecar check did not discriminate either: SQLite deletes -wal/-shm on
    # last close, so they were gone by the time the test looked.
    assert "preserved forensic snapshot" in out
    assert "does not exist" not in out, (
        "refused at the SCHEMA gate, not the snapshot gate -- this test would "
        "pass with the snapshot guard deleted"
    )


def test_help_works_without_a_populated_env(tmp_path, monkeypatch):
    """--help must not need Telegram and Anthropic keys.

    `default=float(get_settings()...)` evaluated at parser-construction time,
    so the sole writer of the overlay -- the one step that must be rehearsed
    with --dry-run before --apply -- was unrunnable on any dev box, rehearsal
    host or CI runner, and failed with a wall of missing-key errors that reads
    as "wrong script" rather than "missing .env".
    """
    import asyncio

    import backfill_chain_identity_recompute as mod

    monkeypatch.setattr(sys, "argv", ["prog", "--help"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(mod.main())
    assert exc.value.code == 0


def test_dry_run_works_without_a_populated_env(tmp_path, monkeypatch):
    """--dry-run is the REHEARSAL path; it must not need production secrets.

    The gate was resolved from Settings immediately after `parse_args()`, so
    `--dry-run` and the refusal path both died with a raw pydantic
    ValidationError on any box without a complete .env. Only `--help` worked --
    and the commit message claimed all three did.
    """
    import asyncio

    import backfill_chain_identity_recompute as mod

    db = tmp_path / "scout.db"
    _make_wal_db(db)
    monkeypatch.setattr(mod, "SNAPSHOT_SOURCES", ())
    monkeypatch.setattr(sys, "argv", ["prog", "--db", str(db), "--dry-run"])

    # Assert the CALL never happens, rather than deleting env vars and hoping
    # Settings fails. `get_settings()` memoises, so on a box (or a test
    # session) where it has already been built successfully, clearing the
    # environment changes nothing and the test passes with the defect intact --
    # which is exactly what it did.
    def _boom():
        raise AssertionError(
            "get_settings() was called on the --dry-run path; it needs the "
            "production .env, which the rehearsal path must not require"
        )

    monkeypatch.setattr(mod, "get_settings", _boom)

    rc = asyncio.run(mod.main())

    assert rc == 0


def test_the_snapshot_refusal_works_without_a_populated_env(tmp_path, monkeypatch):
    """Same path, same reason: the refusal exists to print guidance."""
    import asyncio

    import backfill_chain_identity_recompute as mod

    snap = tmp_path / "scout.db.pre500.20260802202122"
    _make_wal_db(snap)
    monkeypatch.setattr(mod, "SNAPSHOT_SOURCES", (str(snap),))
    monkeypatch.setattr(sys, "argv", ["prog", "--db", str(snap), "--apply"])

    def _boom():
        raise AssertionError("get_settings() was called before the refusal")

    monkeypatch.setattr(mod, "get_settings", _boom)

    assert asyncio.run(mod.main()) == 2


def test_apply_that_recovers_nothing_exits_3(tmp_path, monkeypatch):
    """The runbook documents exit 3 as "do not treat as success". Untested.

    This is the operator's remediation for a NOT_RECOVERING page; reporting
    success while resolving nothing lets them tick the box and walk away from
    an unfixed collapse.
    """
    import asyncio
    import sqlite3

    import backfill_chain_identity_recompute as mod

    db = tmp_path / "scout.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE chain_identity_recompute_v1 (source_table TEXT, "
        "source_row_id INTEGER, coin_id TEXT, symbol TEXT, historical_anchor TEXT, "
        "legacy_detected INTEGER, legacy_lead REAL, canonical_detected INTEGER, "
        "canonical_lead REAL, identity_tier TEXT, evidence_status TEXT, "
        "semantics_version TEXT, computed_at TEXT, "
        "PRIMARY KEY (source_table, source_row_id))"
    )
    conn.execute(
        "CREATE TABLE gainers_comparisons_legacy_prefix_v1 (id INTEGER, "
        "coin_id TEXT, symbol TEXT, appeared_on_gainers_at TEXT, "
        "detected_by_chains INTEGER, chains_lead_minutes REAL)"
    )
    conn.execute(
        "INSERT INTO gainers_comparisons_legacy_prefix_v1 "
        "VALUES (1, 'nothing-recoverable', 'NR', '2026-08-01T00:00:00+00:00', 1, 5000.0)"
    )
    conn.execute(
        "CREATE TABLE signal_first_seen (token_id TEXT PRIMARY KEY, "
        "first_seen_at TEXT, updated_at TEXT)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(mod, "SNAPSHOT_SOURCES", ())
    monkeypatch.setattr(
        sys, "argv", ["prog", "--db", str(db), "--apply", "--gate-minutes", "1440"]
    )

    rc = asyncio.run(mod.main())

    assert rc == 3, "an --apply that recovered nothing reported success"
