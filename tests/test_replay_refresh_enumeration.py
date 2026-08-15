"""Tests for scripts/replay_refresh_enumeration.py.

The script is the pre-merge evidence instrument for the enumeration repair, so
two properties matter equally: it must report the right thing, and it must be
incapable of touching the production database it is pointed at. The read-only
property is asserted three ways — source contract, byte-identity of the file
across a real run, and the connection itself refusing a write — because a
"read-only" script is exactly the kind of claim that rots silently.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "replay_refresh_enumeration.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("replay_refresh_enumeration", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _seed(tmp_path):
    """A DB replaying the 2026-08-13 production shape.

    `volume_spike` — stale unsuppressed 30d row, opens OUTSIDE the enumeration
    window, valid closes INSIDE it. That is the combo the deployed enumeration
    misses and the proposed enumeration must newly admit.
    """
    path = tmp_path / "replay.db"
    db = Database(path)
    await db.initialize()
    now = datetime.now(timezone.utc)
    old_open = now - timedelta(days=45)
    recent_close = now - timedelta(days=1)

    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('volume_spike', '30d', 25, 5, 20, -100.0, -2.0, 20.0, 0, 0, ?)",
        ((now - timedelta(days=60)).isoformat(),),
    )
    for i in range(3):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
            " sl_price, status, pnl_usd, pnl_pct, opened_at, closed_at, "
            " signal_combo) "
            "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
            " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'closed_tp', ?, ?, ?, ?, "
            " 'volume_spike')",
            (
                f"tok_{i}",
                10.0 if i < 2 else -5.0,
                5.0 if i < 2 else -3.0,
                old_open.isoformat(),
                recent_close.isoformat(),
            ),
        )
    await db._conn.commit()
    await db.close()
    return path


# --------------------------------------------------------------------------
# Read-only posture
# --------------------------------------------------------------------------


def test_opens_mode_ro():
    src = SCRIPT.read_text("utf-8")
    assert "?mode=ro" in src and "uri=True" in src


def test_source_contains_no_mutation_sql():
    low = SCRIPT.read_text("utf-8").lower()
    for banned in (
        "insert into",
        "update combo_performance",
        "update paper_trades",
        "delete from",
        "alter table",
        "create table",
        "drop table",
    ):
        assert banned not in low, f"mutation found in source: {banned}"


def test_never_constructs_database_or_runs_migrations():
    """Importing `Database` for a type is fine; constructing one or calling
    `.initialize()` drags in the migration machinery (the PR #520 failure)."""
    tree = ast.parse(SCRIPT.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "initialize":
                raise AssertionError("calls .initialize() — migration machinery")
            if isinstance(fn, ast.Name) and fn.id == "Database":
                raise AssertionError("constructs Database()")


async def test_replay_leaves_the_database_byte_identical(tmp_path, settings_factory):
    """The strongest form of the claim: run it for real, hash the file either
    side. Beats any source-level assertion, which can only see what it greps."""
    path = await _seed(tmp_path)
    mod = _load_script()
    before = _sha256(path)

    report = await mod.replay(path, datetime.now(timezone.utc), settings_factory())

    assert report["read_only"] is True
    assert _sha256(path) == before, "replay mutated the database file"

    # And the logical contents are untouched, not merely the bytes.
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT combo_key, window, trades, wins, losses, win_rate_pct, "
            "       suppressed, last_refreshed "
            "FROM combo_performance ORDER BY combo_key, window"
        ).fetchall()
        assert rows == [
            ("volume_spike", "30d", 25, 5, 20, 20.0, 0, rows[0][7])
        ], "replay altered combo_performance"
        assert conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 3
    finally:
        conn.close()


async def test_readonly_open_creates_wal_sidecars_and_that_is_not_a_write(
    tmp_path, settings_factory
):
    """DOCUMENTS a real property of `mode=ro` against a WAL-mode database.

    The DB runs `journal_mode=wal`. Opening a WAL database read-only makes
    SQLite materialize `-wal`/`-shm` sidecars — its own read-side index
    machinery — even through the stdlib driver with no statement executed. That
    is NOT a write to the data, and the byte-identity test above is the proof.

    `immutable=1` WOULD suppress the sidecars, and is deliberately NOT used:
    it asserts the file cannot change while open, which is false for the live
    production database this script is pointed at and would license SQLite to
    serve stale or torn reads. Pinned here so nobody "tidies up" the sidecars by
    adding that flag.
    """
    path = await _seed(tmp_path)
    baseline = _sha256(path)

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.execute("SELECT COUNT(*) FROM combo_performance").fetchone()
    conn.close()

    assert (tmp_path / "replay.db-wal").exists(), (
        "expectation stale: a plain read-only open of a WAL database no longer "
        "creates sidecars — revisit the safety note in the script docstring"
    )
    assert _sha256(path) == baseline, "sidecar creation must not touch the DB"


async def test_its_connection_refuses_writes(tmp_path):
    """The connection the script opens is refused by SQLite, not merely unused."""
    import aiosqlite

    path = await _seed(tmp_path)
    conn = await aiosqlite.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError) as exc:
            await conn.execute("CREATE TABLE nope (x INTEGER)")
        assert "readonly" in str(exc.value).lower()
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


async def test_reports_the_volume_spike_class_as_newly_enumerated(
    tmp_path, settings_factory
):
    """The production deviation, replayed: opens outside the window, valid
    closes inside, unsuppressed -> invisible to the deployed enumeration and
    admitted by the proposed one."""
    path = await _seed(tmp_path)
    mod = _load_script()

    report = await mod.replay(path, datetime.now(timezone.utc), settings_factory())

    assert report["membership"]["newly_enumerated"] == ["volume_spike"]
    assert report["membership"]["dropped_vs_deployed"] == []

    entry = next(c for c in report["combos"] if c["combo_key"] == "volume_spike")
    assert entry["newly_enumerated"] is True
    assert entry["in_deployed_enumeration"] is False
    assert entry["would_change"] is True
    assert entry["change_reason"] == "economics_differ"
    # Stale row said 25/5/20; the truth in-window is 3/2/1.
    assert entry["existing_row_30d"]["trades"] == 25
    assert entry["recomputed_30d"] == {
        "trades": 3,
        "wins": 2,
        "losses": 1,
        "total_pnl_usd": 15.0,
        "avg_pnl_pct": round((5.0 + 5.0 - 3.0) / 3, 6),
        "win_rate_pct": round(200.0 / 3, 6),
    }
    assert report["rows_that_would_change"] == 1


async def test_suppression_transition_uses_real_thresholds(tmp_path, settings_factory):
    """A combo crossing the D4 gate is predicted, and the reported thresholds
    come from Settings rather than being re-typed in the script."""
    path = tmp_path / "supp.db"
    db = Database(path)
    await db.initialize()
    now = datetime.now(timezone.utc)
    s = settings_factory()
    # min_trades losing closes -> 0% WR, below the WR threshold.
    for i in range(s.FEEDBACK_SUPPRESSION_MIN_TRADES):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
            " sl_price, status, pnl_usd, pnl_pct, opened_at, closed_at, "
            " signal_combo) "
            "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
            " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'closed_sl', -5.0, -3.0, "
            " ?, ?, 'sinker')",
            (
                f"t{i}",
                (now - timedelta(days=3)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
            ),
        )
    await db._conn.commit()
    await db.close()

    mod = _load_script()
    report = await mod.replay(path, now, s)

    assert report["predicted_suppression_transitions"] == ["sinker"]
    entry = next(c for c in report["combos"] if c["combo_key"] == "sinker")
    assert entry["predicted_transition_MIRRORED_LOGIC"] == "new_suppression"
    assert (
        report["thresholds"]["FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT"]
        == s.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT
    )


async def test_terminal_incomplete_set_is_global_not_scoped_to_refreshed(
    tmp_path, settings_factory
):
    """The 2026-08-13 under-count came from reasoning about pages from the
    refreshed-combo list. The predicate is global; a suppressed exhausted
    generation with no trades at all must still appear."""
    path = tmp_path / "ti.db"
    db = Database(path)
    await db.initialize()
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES ('stuck_gen', '30d', 40, 8, 32, -9.0, -1.0, 20.0, 1, ?, ?, 0, 0, ?)",
        (
            (now - timedelta(days=20)).isoformat(),
            (now - timedelta(days=6)).isoformat(),
            now.isoformat(),
        ),
    )
    await db._conn.commit()
    await db.close()

    mod = _load_script()
    report = await mod.replay(path, now, settings_factory())

    # F2 reclassification: `stuck_gen` has NO trades at all, so it was never
    # stuck on evidence quality — it is `parole_stalled`, a cohort that never
    # existed. The globality claim this test exists for is unchanged and still
    # asserted; only the bucket moved, and the harness reports both classes so
    # the diagnostic cannot go blind on the newer one.
    assert report["terminal_incomplete_candidates_GLOBAL"] == []
    keys = [c["combo_key"] for c in report["parole_stalled_candidates_GLOBAL"]]
    assert keys == ["stuck_gen"]
    entry = report["parole_stalled_candidates_GLOBAL"][0]
    assert entry["valid_closed"] == 0
    assert entry["open_now"] == 0
    assert entry["cohort_total"] == 0
    assert entry["required"] == settings_factory().FEEDBACK_PAROLE_RETEST_TRADES


async def _seed_split_windows(tmp_path, *, persisted_7d):
    """Three valid closes inside BOTH windows, so canonical 7d == canonical 30d.

    The persisted 30d row is written EXACT against that canonical value, so the
    30d comparison contributes nothing. `persisted_7d` is the
    `(trades, wins, losses, win_rate_pct)` written for the 7d window — `None`
    writes no 7d row at all. Anything the report then says about change is
    attributable to the 7d window alone.
    """
    path = tmp_path / "split.db"
    db = Database(path)
    await db.initialize()
    now = datetime.now(timezone.utc)
    wr = round(200.0 / 3, 6)

    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('driftwood', '30d', 3, 2, 1, 15.0, ?, ?, 0, 0, ?)",
        (round((5.0 + 5.0 - 3.0) / 3, 6), wr, now.isoformat()),
    )
    if persisted_7d is not None:
        trades, wins, losses, wr_7d = persisted_7d
        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, "
            " last_refreshed) "
            "VALUES ('driftwood', '7d', ?, ?, ?, 0.0, 0.0, ?, 0, 0, ?)",
            (trades, wins, losses, wr_7d, now.isoformat()),
        )

    for i in range(3):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
            " sl_price, status, pnl_usd, pnl_pct, opened_at, closed_at, "
            " signal_combo) "
            "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
            " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'closed_tp', ?, ?, ?, ?, "
            " 'driftwood')",
            (
                f"d_{i}",
                10.0 if i < 2 else -5.0,
                5.0 if i < 2 else -3.0,
                (now - timedelta(days=3)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
            ),
        )
    await db._conn.commit()
    await db.close()
    return path


async def test_stale_7d_row_is_reported_when_the_30d_row_is_exact(
    tmp_path, settings_factory
):
    """The F1 defect, replayed.

    Before the fix the comparison fetched only the `window = '30d'` row and
    decided `would_change` from it alone, so this shape — persisted 30d exact,
    persisted 7d stale — reported `would_change = False` and
    `rows_that_would_change = 0`. The instrument under-attested by half: the
    refresh WOULD rewrite the 7d row.
    """
    path = await _seed_split_windows(tmp_path, persisted_7d=(1, 0, 1, 0.0))
    mod = _load_script()

    report = await mod.replay(path, datetime.now(timezone.utc), settings_factory())
    entry = next(c for c in report["combos"] if c["combo_key"] == "driftwood")

    assert entry["30d_match"] is True
    assert entry["would_change_30d"] is False
    assert entry["change_reason_30d"] == "no_change"

    assert entry["7d_match"] is False
    assert entry["would_change_7d"] is True
    assert entry["change_reason_7d"] == "economics_differ"

    assert entry["would_change_any"] is True
    assert entry["would_change"] is True, "the legacy aggregate must be ANY-window"
    assert report["rows_that_would_change"] == 1
    assert report["rows_that_would_change_7d"] == 1
    assert report["rows_that_would_change_30d"] == 0

    # Both persisted rows are reported, so a reader can see WHICH one is stale.
    assert entry["existing_row_7d"]["trades"] == 1
    assert entry["existing_row_30d"]["trades"] == 3
    assert entry["recomputed_7d"]["trades"] == 3
    assert entry["recomputed_30d"]["trades"] == 3


async def test_both_windows_exact_reports_no_change(tmp_path, settings_factory):
    """The other side of the discrimination: a genuinely current pair of rows
    must not be inflated into a change by the new per-window comparison."""
    exact = (3, 2, 1, round(200.0 / 3, 6))
    path = await _seed_split_windows(tmp_path, persisted_7d=exact)
    mod = _load_script()

    report = await mod.replay(path, datetime.now(timezone.utc), settings_factory())
    entry = next(c for c in report["combos"] if c["combo_key"] == "driftwood")

    assert entry["7d_match"] is True
    assert entry["30d_match"] is True
    assert entry["would_change_any"] is False
    assert entry["would_change"] is False
    assert entry["change_reason"] == "no_change"
    assert report["rows_that_would_change"] == 0
    assert report["rows_that_would_change_7d"] == 0
    assert report["rows_that_would_change_30d"] == 0


async def test_missing_7d_row_is_an_explicit_change_not_a_silent_skip(
    tmp_path, settings_factory
):
    """A window with no persisted row is a change for that window — the same
    treatment the 30d window already gave a missing row, not a skip."""
    path = await _seed_split_windows(tmp_path, persisted_7d=None)
    mod = _load_script()

    report = await mod.replay(path, datetime.now(timezone.utc), settings_factory())
    entry = next(c for c in report["combos"] if c["combo_key"] == "driftwood")

    assert entry["existing_row_7d"] is None
    assert entry["7d_match"] is False
    assert entry["would_change_7d"] is True
    assert entry["change_reason_7d"] == "row_would_be_created"
    assert entry["30d_match"] is True
    assert entry["would_change_any"] is True
    assert report["rows_that_would_change"] == 1


async def test_output_is_deterministic_for_a_fixed_at(tmp_path, settings_factory):
    """Two runs at the same `--at` must be byte-identical, or the report cannot
    be diffed across a merge."""
    import json

    path = await _seed(tmp_path)
    mod = _load_script()
    at = datetime.now(timezone.utc)

    first = json.dumps(
        await mod.replay(path, at, settings_factory()), indent=2, sort_keys=True
    )
    second = json.dumps(
        await mod.replay(path, at, settings_factory()), indent=2, sort_keys=True
    )
    assert first == second


async def test_terminal_incomplete_bucket_still_scans_globally(
    tmp_path, settings_factory
):
    """The sibling of the reclassified case: a combo whose cohort DID exist but
    produced too little usable evidence stays in the terminal-incomplete
    bucket, and is still found globally rather than via the refreshed list.

    Added alongside the F2 reclassification so that bucket keeps a fixture of
    its own — otherwise the only test covering it was the one that moved."""
    path = tmp_path / "ti_real.db"
    db = Database(path)
    await db.initialize()
    now = datetime.now(timezone.utc)
    parole_at = now - timedelta(days=6)
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES ('evidence_short', '30d', 40, 8, 32, -9.0, -1.0, 20.0, 1, ?, ?, "
        " 0, 0, ?)",
        (
            (now - timedelta(days=20)).isoformat(),
            parole_at.isoformat(),
            now.isoformat(),
        ),
    )
    # Two closed trades admitted under this parole: a real cohort, just short
    # of the required evidence.
    for i in range(2):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, "
            " sl_price, status, pnl_usd, pnl_pct, opened_at, closed_at, "
            " signal_combo) "
            "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', 1.0, 100.0, "
            " 100.0, 20.0, 10.0, 1.2, 0.9, 'closed_sl', -5.0, -5.0, ?, ?, "
            " 'evidence_short')",
            (
                f"tok-ti-{i}",
                (parole_at + timedelta(hours=1)).isoformat(),
                (now - timedelta(hours=2)).isoformat(),
            ),
        )
    await db._conn.commit()
    await db.close()

    mod = _load_script()
    report = await mod.replay(path, now, settings_factory())

    keys = [c["combo_key"] for c in report["terminal_incomplete_candidates_GLOBAL"]]
    assert keys == ["evidence_short"]
    assert report["parole_stalled_candidates_GLOBAL"] == []
    entry = report["terminal_incomplete_candidates_GLOBAL"][0]
    assert entry["cohort_total"] == 2
