"""The overlay's PRIMARY KEY must include `semantics_version`.

Without it the "versioned derived store" holds exactly ONE version: keyed on
`(source_table, source_row_id)` under `INSERT OR REPLACE`, a replay under a
bumped `RECOMPUTE_SEMANTICS` overwrites the earlier verdict in place and the
evidence is gone. That is the destroys-the-previous-evidence shape ruling C
forbids, deferred one level — the archive/overlay split exists so the archive
is never rewritten, and the overlay then reproduced the defect against itself.

Builds the OLD shape explicitly. A fresh `tmp_path` database gets the v2 table
directly, so a test that only ever creates fresh databases exercises the
already-migrated path and cannot see the upgrade at all.
"""

import sqlite3
from pathlib import Path

import pytest

from scout.db import Database

ANCHOR = "2026-08-01T00:00:00+00:00"

V1_DDL = """
CREATE TABLE chain_identity_recompute_v1 (
    source_table        TEXT NOT NULL,
    source_row_id       INTEGER NOT NULL,
    coin_id             TEXT NOT NULL,
    symbol              TEXT,
    historical_anchor   TEXT NOT NULL,
    legacy_detected     INTEGER NOT NULL,
    legacy_lead         REAL,
    canonical_detected  INTEGER NOT NULL,
    canonical_lead      REAL,
    identity_tier       TEXT NOT NULL,
    evidence_status     TEXT NOT NULL,
    semantics_version   TEXT NOT NULL,
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (source_table, source_row_id)
)
"""


def _row(conn, version, status):
    conn.execute(
        "INSERT OR REPLACE INTO chain_identity_recompute_v1 VALUES "
        "('gainers_comparisons', 1, 'c', 'C', ?, 1, 100.0, 1, 9000.0, "
        "'canonical_id', ?, ?, ?)",
        (ANCHOR, status, version, ANCHOR),
    )


async def test_a_database_on_the_OLD_key_is_upgraded(tmp_path):
    db_path = tmp_path / "scout.db"
    conn = sqlite3.connect(db_path)
    conn.execute(V1_DDL)
    conn.execute(
        "CREATE TABLE paper_migrations (name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
    )
    # The v1 migration marker is present, so it early-returns forever -- which
    # is exactly why the fix needed its own migration rather than an edit to
    # the original CREATE.
    conn.execute(
        "INSERT INTO paper_migrations VALUES ('chain_identity_recompute_v1', ?)",
        (ANCHOR,),
    )
    _row(conn, "chain_identity_recompute_v1", "verified_canonical")
    conn.commit()
    conn.close()

    d = Database(db_path)
    await d.initialize()
    try:
        cur = await d._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='chain_identity_recompute_v1'"
        )
        ddl = " ".join((await cur.fetchone())[0].split())
        assert "semantics_version)" in ddl, f"not upgraded: {ddl}"

        # The existing row survived the rebuild.
        cur = await d._conn.execute("SELECT COUNT(*) FROM chain_identity_recompute_v1")
        assert (await cur.fetchone())[0] == 1
    finally:
        await d.close()


async def test_a_second_version_does_NOT_destroy_the_first(tmp_path):
    """The property the key exists for."""
    d = Database(tmp_path / "scout.db")
    await d.initialize()
    try:
        raw = d._conn
        await raw.execute(
            "INSERT OR REPLACE INTO chain_identity_recompute_v1 VALUES "
            "('gainers_comparisons', 1, 'c', 'C', ?, 1, 100.0, 1, 9000.0, "
            "'canonical_id', 'verified_canonical', 'v1', ?)",
            (ANCHOR, ANCHOR),
        )
        await raw.execute(
            "INSERT OR REPLACE INTO chain_identity_recompute_v1 VALUES "
            "('gainers_comparisons', 1, 'c', 'C', ?, 1, 100.0, 1, 9000.0, "
            "'canonical_id', 'canonical_below_gate_indeterminate', 'v2', ?)",
            (ANCHOR, ANCHOR),
        )
        await raw.commit()

        cur = await raw.execute(
            "SELECT semantics_version, evidence_status "
            "FROM chain_identity_recompute_v1 ORDER BY semantics_version"
        )
        rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [
            ("v1", "verified_canonical"),
            ("v2", "canonical_below_gate_indeterminate"),
        ], f"a v2 replay destroyed the v1 evidence: {rows}"
    finally:
        await d.close()


async def test_replaying_the_SAME_version_still_replaces(tmp_path):
    """Idempotence must survive the key change, or a re-run double-counts."""
    d = Database(tmp_path / "scout.db")
    await d.initialize()
    try:
        for status in ("verified_canonical", "indeterminate_history"):
            await d._conn.execute(
                "INSERT OR REPLACE INTO chain_identity_recompute_v1 VALUES "
                "('gainers_comparisons', 1, 'c', 'C', ?, 1, 100.0, 1, 9000.0, "
                "'canonical_id', ?, 'v1', ?)",
                (ANCHOR, status, ANCHOR),
            )
        await d._conn.commit()

        cur = await d._conn.execute(
            "SELECT COUNT(*), MAX(evidence_status) FROM chain_identity_recompute_v1"
        )
        n, status = await cur.fetchone()
        assert n == 1, "a same-version rerun inserted instead of replacing"
        assert status == "indeterminate_history"
    finally:
        await d.close()


async def test_a_failed_rebuild_does_not_wedge_the_next_boot(tmp_path, monkeypatch):
    """The rebuild must be atomic, and a wedged database must self-heal.

    DDL runs in autocommit under this connection mode, so without an explicit
    transaction the scratch `..._pk2` table is durable the instant it is
    created and `rollback()` cannot remove it. Every subsequent
    `initialize()` then raised `table ... already exists` — uncatchable by any
    caller — until someone dropped it by hand.

    Only reachable on databases carrying real data: a fresh install takes the
    already-migrated early return and never runs the rebuild at all.
    """
    db_path = tmp_path / "scout.db"
    conn = sqlite3.connect(db_path)
    conn.execute(V1_DDL)
    conn.execute(
        "CREATE TABLE paper_migrations (name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO paper_migrations VALUES ('chain_identity_recompute_v1', ?)",
        (ANCHOR,),
    )
    _row(conn, "chain_identity_recompute_v1", "verified_canonical")
    conn.commit()
    conn.close()

    d = Database(db_path)
    real_execute = None

    async def failing_execute(sql, *args, **kwargs):
        if sql.strip().startswith("INSERT INTO chain_identity_recompute_v1_pk2"):
            raise RuntimeError("simulated failure right after the scratch CREATE")
        return await real_execute(sql, *args, **kwargs)

    import scout.db as db_mod

    orig = db_mod.Database._migrate_chain_identity_recompute_pk_v2

    async def patched(self):
        nonlocal real_execute
        real_execute = self._conn.execute
        self._conn.execute = failing_execute
        try:
            await orig(self)
        finally:
            self._conn.execute = real_execute

    monkeypatch.setattr(
        db_mod.Database, "_migrate_chain_identity_recompute_pk_v2", patched
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        await d.initialize()
    await d.close()

    monkeypatch.undo()

    # The orphan must not survive the rollback...
    check = sqlite3.connect(db_path)
    names = {
        r[0]
        for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'chain_identity_recompute%'"
        )
    }
    check.close()
    assert (
        "chain_identity_recompute_v1_pk2" not in names
    ), f"orphan scratch table survived the failure: {names}"

    # ...and the next boot must succeed and complete the upgrade.
    d2 = Database(db_path)
    await d2.initialize()
    try:
        cur = await d2._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='chain_identity_recompute_v1'"
        )
        ddl = " ".join((await cur.fetchone())[0].split())
        assert "semantics_version)" in ddl, "the retry did not complete the upgrade"
        cur = await d2._conn.execute("SELECT COUNT(*) FROM chain_identity_recompute_v1")
        assert (await cur.fetchone())[0] == 1, "the original row was lost"
    finally:
        await d2.close()


def test_the_rebuild_is_wrapped_in_an_explicit_transaction():
    """Structural, because behaviour here depends on incidental state.

    Measured on both interpreters this code runs under (3.14 local, 3.12 on the
    box): a bare `CREATE TABLE` autocommits and SURVIVES `rollback()`. So
    without an explicit transaction a failed rebuild leaves an orphan
    `..._pk2`, and every subsequent `initialize()` raises `table ... already
    exists` — uncatchable by any caller.

    The behavioural test above does not discriminate: by the time this
    migration runs, an earlier migration's DML happens to have left a
    transaction open, so the CREATE joins it and the rollback undoes it. That
    is luck, not a property — and relying on it is precisely the argument for
    stating the transaction explicitly. A mutant removing `BEGIN EXCLUSIVE`
    passes that test.
    """
    import ast

    src = (Path(__file__).resolve().parents[1] / "scout" / "db.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_migrate_chain_identity_recompute_pk_v2"
    )
    stmts = [
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    joined = " ".join(" ".join(s.split()) for s in stmts)

    assert "BEGIN EXCLUSIVE" in joined, (
        "the rebuild is not wrapped in an explicit transaction; a failure "
        "leaves an orphan scratch table that wedges every later boot"
    )
    assert "DROP TABLE IF EXISTS chain_identity_recompute_v1_pk2" in joined, (
        "no DROP before the CREATE; a database already wedged by an earlier "
        "failed attempt cannot self-heal and needs hands"
    )
    # Ordering: the DROP must come after BEGIN, or it is not covered either.
    assert joined.index("BEGIN EXCLUSIVE") < joined.index(
        "DROP TABLE IF EXISTS chain_identity_recompute_v1_pk2"
    )
