"""The backfill's history source, pinned behaviourally.

`test_signal_first_seen_sole_writer.py` caught the original mistake but cannot
catch a regression here: its `_LITERAL_ALLOWED` is FILE-level, and this file is
allowlisted for its snapshot queries. Restoring
`MIN(created_at) FROM signal_events` on the LIVE path therefore passes the
entire suite — review demonstrated exactly that mutant surviving all 57 tests.

So this file drives the functions and reads the values back. That is immune to
the allowlist's granularity, and it also pins the second half of the same
defect: the history table and the coverage interval must be derived from the
SAME table, or a token first seen in June falls outside an August-derived
interval and is marked uncovered on evidence that never applied to it.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backfill_chain_identity_recompute import (  # noqa: E402
    _history_table,
    collect_history,
    collect_intervals,
)

OLD = "2026-06-01T00:00:00+00:00"
RECENT = "2026-08-15T00:00:00+00:00"


def _db_with_pruned_events(path: Path) -> None:
    """The real production shape: substrate remembers June, events pruned to August.

    This is not a contrived fixture -- it is what age-pruning produces every
    night, and it is the exact condition that makes `signal_events` the wrong
    source for a first-seen.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE signal_first_seen (token_id TEXT PRIMARY KEY, "
        "first_seen_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO signal_first_seen VALUES ('tok', ?, ?)", (OLD, OLD))
    conn.execute("CREATE TABLE signal_events (token_id TEXT, created_at TEXT)")
    conn.execute("INSERT INTO signal_events VALUES ('tok', ?)", (RECENT,))
    conn.commit()
    conn.close()


def _snapshot_without_substrate(path: Path) -> None:
    """A preserved snapshot: predates signal_first_seen, carries only events."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE signal_events (token_id TEXT, created_at TEXT)")
    conn.execute("INSERT INTO signal_events VALUES ('tok', ?)", (OLD,))
    conn.commit()
    conn.close()


def test_history_comes_from_the_substrate_not_the_pruned_events_table(tmp_path):
    db = tmp_path / "scout.db"
    _db_with_pruned_events(db)

    history = collect_history((), live_db=str(db))

    assert history["tok"] == OLD, (
        "first-seen was derived from signal_events, which is age-pruned -- so "
        "the value is not when the token was first seen, it is the oldest row "
        "retention has not deleted yet, a floor that walks forward nightly"
    )


def test_the_interval_covers_the_history_it_was_derived_from(tmp_path):
    """The two must come from the same table or they contradict each other."""
    db = tmp_path / "scout.db"
    _db_with_pruned_events(db)

    history = collect_history((), live_db=str(db))
    intervals = collect_intervals((), live_db=str(db))

    first_seen = history["tok"]
    covered = any(start <= first_seen <= end for start, end in intervals)
    assert covered, (
        f"token first seen {first_seen} falls outside the declared coverage "
        f"{intervals} -- history and intervals were derived from different "
        "tables, so the row is marked uncovered on evidence that never "
        "applied to it"
    )


def test_a_snapshot_without_the_substrate_still_uses_its_events(tmp_path):
    """The exemption is real: preserved snapshots predate signal_first_seen."""
    snap = tmp_path / "scout.db.pre500.20260802202122"
    _snapshot_without_substrate(snap)

    assert _history_table(str(snap)) == ("created_at", "signal_events")

    live = tmp_path / "scout.db"
    _db_with_pruned_events(live)
    history = collect_history((str(snap),), live_db=str(live))
    # The snapshot's older value wins -- collect_history only ever moves a
    # first-seen EARLIER, which can only lengthen a lead.
    assert history["tok"] == OLD


def test_the_table_choice_follows_the_FILE_not_the_path_spelling(tmp_path):
    """Keyed on what the file contains, not on whether it is the live DB.

    Deciding this with `_is_live` made the answer depend on how `--db` was
    spelled: a scratch copy of the live database -- the configuration the
    refusal message recommends, and the one the acceptance replay is measured
    on -- got its history from one table and its interval from another.
    """
    copy = tmp_path / "some-scratch-copy.db"
    _db_with_pruned_events(copy)

    assert _history_table(str(copy)) == ("first_seen_at", "signal_first_seen")
