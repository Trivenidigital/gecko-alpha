"""*** EXECUTION CANNOT PROCEED AGAINST AN EMPTY WRONG-DIRECTORY DATABASE. ***

The trap, reproduced: SQLite creates a database for any path it is handed, and
``DB_PATH`` defaults to the RELATIVE ``scout.db``. Run anything from the wrong
directory and every safety mechanism reads an empty file as

    no positions · no prior signatures · no daily gross · kill switch clear

reporting all-clear **because it is looking at the wrong database**. A zero-byte
``/root/scout.db`` sat on the production host from 2026-05-15 to 2026-08-02,
created by exactly this mistake, and the pre-existing guards did not catch it:
they tested ``path.exists()``, which is TRUE for a zero-byte file.

The tests below assert both directions — that the real database is accepted, and
that every fingerprint of the wrong one is refused — because a guard that refuses
everything is as useless as one that refuses nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scout.db import Database
from scout.db_path import (
    DEPLOYMENT_ROOT,
    REQUIRED_TABLES,
    UnsafeDatabase,
    assert_creatable_database,
    assert_safe_database,
    describe_database,
    resolve_db_path,
)


def _minimal_env(monkeypatch) -> None:
    """`load_settings()` reads the process environment; moving cwd away from the
    repo means its `.env` is no longer found, so the required fields are supplied
    explicitly. Nothing here is a real credential."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")


async def _real_database(tmp_path) -> Path:
    """A genuine, migrated gecko database."""
    path = tmp_path / "real" / "scout.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)
    await db.initialize()
    await db.close()
    return path


# ---------------------------------------------------------------------------
# Resolution: cwd must not decide which database is in play
# ---------------------------------------------------------------------------


class TestResolution:
    def test_a_relative_path_anchors_to_the_deployment_root_not_cwd(self, monkeypatch):
        """*** THE ROOT CAUSE. ***

        Production sets `DB_PATH=scout.db` and works only because systemd sets
        `WorkingDirectory` and the cron line starts with `cd`. That makes
        correctness ambient — a property of the invocation rather than of the
        configuration. Resolution must give the same answer from anywhere.
        """
        from_here = resolve_db_path("scout.db")
        monkeypatch.chdir(Path(from_here).anchor)  # e.g. "/" or "C:\\"
        from_root = resolve_db_path("scout.db")
        assert from_here == from_root
        assert from_here == (DEPLOYMENT_ROOT / "scout.db").resolve()
        assert from_here.is_absolute()

    def test_an_absolute_path_is_used_exactly_as_given(self, tmp_path):
        target = tmp_path / "elsewhere" / "scout.db"
        assert resolve_db_path(target) == target

    def test_resolution_touches_no_filesystem(self, tmp_path):
        """It is called from error paths and log lines about databases that may
        not exist; creating anything there would be the bug it guards against."""
        ghost = tmp_path / "definitely" / "absent.db"
        resolve_db_path(ghost)
        assert not ghost.exists()
        assert not ghost.parent.exists()

    def test_describe_never_raises_on_a_hopeless_path(self):
        info = describe_database("no/such/place/scout.db")
        assert info["exists"] is False
        assert info["was_relative"] is True


# ---------------------------------------------------------------------------
# The wrong database, in every shape it actually takes
# ---------------------------------------------------------------------------


class TestRefusal:
    async def test_a_zero_byte_file_is_refused(self, tmp_path):
        """*** THE CASE THAT ACTUALLY HAPPENED, and the one `.exists()` missed. ***"""
        empty = tmp_path / "scout.db"
        empty.touch()
        assert empty.exists(), "fixture precondition: the old guard would pass this"
        with pytest.raises(UnsafeDatabase) as exc:
            assert_safe_database(empty)
        assert exc.value.reason == "empty"

    async def test_a_missing_file_is_refused_for_operator_lanes(self, tmp_path):
        with pytest.raises(UnsafeDatabase) as exc:
            assert_safe_database(tmp_path / "nope.db")
        assert exc.value.reason == "missing"

    async def test_the_check_does_not_create_the_database_it_checks_for(self, tmp_path):
        """A guard that creates the file it is guarding is the bug wearing a hat."""
        ghost = tmp_path / "ghost.db"
        with pytest.raises(UnsafeDatabase):
            assert_safe_database(ghost)
        assert not ghost.exists()

    async def test_a_non_sqlite_file_is_refused(self, tmp_path):
        junk = tmp_path / "scout.db"
        junk.write_bytes(b"this is not a database, it is a log file\n" * 10)
        with pytest.raises(UnsafeDatabase) as exc:
            assert_safe_database(junk)
        assert exc.value.reason == "not_sqlite"

    async def test_a_directory_is_refused(self, tmp_path):
        d = tmp_path / "scout.db"
        d.mkdir()
        with pytest.raises(UnsafeDatabase) as exc:
            assert_safe_database(d)
        assert exc.value.reason == "not_a_file"

    async def test_a_valid_sqlite_file_that_is_not_ours_is_refused(self, tmp_path):
        """*** THE SUBTLEST SHAPE. ***

        A real SQLite database with real tables — just not this application's.
        Header and size checks both pass; only the core-table check catches it.
        """
        other = tmp_path / "scout.db"
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE unrelated (a INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(UnsafeDatabase) as exc:
            assert_safe_database(other)
        assert exc.value.reason == "missing_tables"
        for table in REQUIRED_TABLES:
            assert table in exc.value.message

    async def test_a_schema_without_recorded_migrations_is_refused(self, tmp_path):
        """Core tables present but no migration ever recorded itself — the shape a
        hand-made or truncated file has. Its schema cannot be trusted to match
        this code, so state read from it cannot be trusted either."""
        forged = tmp_path / "scout.db"
        conn = sqlite3.connect(forged)
        for table in REQUIRED_TABLES:
            conn.execute(f"CREATE TABLE {table} (id INTEGER)")
        conn.execute("CREATE TABLE paper_migrations (name TEXT, cutover_ts TEXT)")
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER, applied_at TEXT, "
            "description TEXT)"
        )
        conn.commit()
        conn.close()
        with pytest.raises(UnsafeDatabase) as exc:
            assert_safe_database(forged)
        assert exc.value.reason == "unmigrated"

    async def test_the_refusal_names_both_paths_so_it_is_actionable(self, tmp_path):
        empty = tmp_path / "scout.db"
        empty.touch()
        with pytest.raises(UnsafeDatabase) as exc:
            assert_safe_database(empty, purpose="the supervised Kraken pilot")
        message = exc.value.message
        assert "the supervised Kraken pilot" in message
        assert str(empty) in message
        assert str(DEPLOYMENT_ROOT) in message


# ---------------------------------------------------------------------------
# Guard on the guard
# ---------------------------------------------------------------------------


class TestTheRealDatabaseIsAccepted:
    async def test_a_migrated_database_passes(self, tmp_path):
        """Without this, every refusal test above would pass on a guard that
        refuses unconditionally."""
        real = await _real_database(tmp_path)
        assert assert_safe_database(real) == real

    async def test_it_passes_from_any_working_directory(self, tmp_path, monkeypatch):
        real = await _real_database(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert assert_safe_database(real) == real


class TestBootMayStillCreate:
    async def test_a_fresh_install_is_permitted_to_create(self, tmp_path):
        """`missing` cannot be fatal at BOOT the way it is for a lane: a first run
        legitimately has no database. The lane only ever runs against an existing
        deployment, so there `missing` always means the wrong directory."""
        fresh = tmp_path / "fresh" / "scout.db"
        fresh.parent.mkdir()
        assert assert_creatable_database(fresh) == fresh
        assert not fresh.exists()

    async def test_boot_still_refuses_an_existing_empty_file(self, tmp_path):
        """The fingerprint of the wrong path, not of a first run. Migrating it
        would build a full schema with no history and then report all-clear."""
        empty = tmp_path / "scout.db"
        empty.touch()
        with pytest.raises(UnsafeDatabase) as exc:
            assert_creatable_database(empty)
        assert exc.value.reason == "empty"

    async def test_boot_still_refuses_a_non_sqlite_file(self, tmp_path):
        junk = tmp_path / "scout.db"
        junk.write_bytes(b"nope")
        with pytest.raises(UnsafeDatabase) as exc:
            assert_creatable_database(junk)
        assert exc.value.reason == "not_sqlite"

    async def test_boot_accepts_the_real_database(self, tmp_path):
        real = await _real_database(tmp_path)
        assert assert_creatable_database(real) == real


# ---------------------------------------------------------------------------
# End to end: the actual trap, through the actual entry points
# ---------------------------------------------------------------------------


class TestTheTrapThroughRealEntryPoints:
    async def test_the_kraken_pilot_refuses_an_empty_database(
        self, tmp_path, monkeypatch, capsys
    ):
        """Reproduces the incident through the real entry point: a zero-byte
        `scout.db`, which the previous `.exists()` guard accepted."""
        import scout.live.kraken_pilot as pilot

        wrong = tmp_path / "wrong_cwd"
        wrong.mkdir()
        empty = wrong / "scout.db"
        empty.touch()  # exactly what sat on the host for three months
        _minimal_env(monkeypatch)
        monkeypatch.setenv("DB_PATH", str(empty))

        code = await pilot.main(["status"])
        out = capsys.readouterr().out
        assert code == pilot.EXIT_REFUSED
        assert "REFUSED [database:empty]" in out

    async def test_a_relative_db_path_no_longer_follows_the_operator_around(
        self, tmp_path, monkeypatch, capsys
    ):
        """*** THE ROOT-CAUSE ASSERTION. ***

        With `DB_PATH=scout.db` — production's actual setting — standing in a
        directory that happens to contain a `scout.db` must NOT make that file the
        one the pilot reads. Resolution is anchored to the deployment root, so the
        decoy is ignored and the pilot reports on the real (here, absent) path.
        """
        import scout.live.kraken_pilot as pilot

        wrong = tmp_path / "wrong_cwd"
        wrong.mkdir()
        decoy = wrong / "scout.db"
        decoy.touch()
        monkeypatch.chdir(wrong)
        _minimal_env(monkeypatch)
        monkeypatch.setenv("DB_PATH", "scout.db")

        code = await pilot.main(["status"])
        out = capsys.readouterr().out
        assert code == pilot.EXIT_REFUSED
        # It refused about the DEPLOYMENT-ROOT path, never the decoy underfoot.
        assert str(DEPLOYMENT_ROOT) in out
        assert str(decoy) not in out
        # And the decoy is still zero bytes — nothing opened or migrated it.
        assert decoy.stat().st_size == 0

    async def test_the_kill_switch_cli_refuses_rather_than_reporting_clear(
        self, tmp_path, monkeypatch, capsys
    ):
        """*** THE WORST INSTANCE. ***

        `cli_kill` fell back to the literal relative "scout.db" and then called
        `initialize()`, which CREATES the database — so `--status` from the wrong
        directory created an empty file and reported the kill switch CLEAR, from
        the one command an operator reaches for when something is already wrong.
        """
        import scout.live.cli_kill as cli_kill

        wrong = tmp_path / "wrong_cwd"
        wrong.mkdir()
        monkeypatch.chdir(wrong)
        monkeypatch.setenv("DB_PATH", "scout.db")
        monkeypatch.setattr("sys.argv", ["cli_kill", "--status"])

        code = await cli_kill.main()
        err = capsys.readouterr().err
        assert code == 2
        assert "REFUSED [database:" in err
        # And it created nothing while refusing.
        assert not (wrong / "scout.db").exists()
