"""One-time backfill of ledger_enrollment_evictions from the interim journal
export JSONL (scripts/ledger-eviction-export.sh output).

REQUIRED 2026-07-02 (#406 ruling): the durable DB eviction marker landed after
the eviction code + journal export were already live, so evictions between
deploy-#2 and the marker deploy exist ONLY in the journal export. This backfill
explodes each ``ledger_enrollment_evicted`` record into per-token marker rows
before journald rotation makes the export lossy (~3wk clock). Idempotent
(UNIQUE(token_id, evicted_at) + INSERT OR IGNORE), dry-run by default.

Windows-safe: scripts.backfill_ledger_eviction_markers imports only scout.db,
which does not pull aiohttp (CI runs the full suite on Linux regardless).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scripts.backfill_ledger_eviction_markers import (
    backfill_file,
    iter_eviction_records,
)


def _journald_envelope(structlog_obj: dict) -> str:
    """One `journalctl -u ... -o json` line: MESSAGE holds the structlog JSON."""
    return json.dumps({"MESSAGE": json.dumps(structlog_obj), "_HOSTNAME": "srilu"})


def _structlog_line(structlog_obj: dict) -> str:
    """A raw structlog JSON line (no journald envelope)."""
    return json.dumps(structlog_obj)


def _evict_event(**overrides) -> dict:
    obj = {
        "event": "ledger_enrollment_evicted",
        "timestamp": "2026-07-05T10:00:00+00:00",
        "evicted_token_ids": ["dex:ethereum:0xa", "dex:ethereum:0xb"],
        "n_evicted": 2,
        "max_active": 200,
        "evicted_for": "dex:ethereum:0xnew",
        "evicted_at": "2026-07-05T10:00:00.123456+00:00",
    }
    obj.update(overrides)
    return obj


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_journald_envelope_explodes_per_token(tmp_path):
    path = tmp_path / "export.jsonl"
    path.write_text(_journald_envelope(_evict_event()) + "\n", encoding="utf-8")
    rows = list(iter_eviction_records(path))
    assert [r["token_id"] for r in rows] == ["dex:ethereum:0xa", "dex:ethereum:0xb"]
    for r in rows:
        assert r["evicted_at"] == "2026-07-05T10:00:00.123456+00:00"
        assert r["evicted_for"] == "dex:ethereum:0xnew"
        assert r["max_active"] == 200
        assert r["n_evicted"] == 2


def test_parse_raw_structlog_line(tmp_path):
    path = tmp_path / "export.jsonl"
    path.write_text(_structlog_line(_evict_event()) + "\n", encoding="utf-8")
    rows = list(iter_eviction_records(path))
    assert len(rows) == 2


def test_evicted_at_falls_back_to_timestamp(tmp_path):
    """Pre-marker journal lines lack evicted_at — fall back to the structlog
    timestamp so the gap period (which has no live rows) still backfills."""
    event = _evict_event()
    del event["evicted_at"]
    path = tmp_path / "export.jsonl"
    path.write_text(_journald_envelope(event) + "\n", encoding="utf-8")
    rows = list(iter_eviction_records(path))
    assert all(r["evicted_at"] == "2026-07-05T10:00:00+00:00" for r in rows)


def test_non_eviction_and_blank_lines_ignored(tmp_path):
    path = tmp_path / "export.jsonl"
    path.write_text(
        "\n"
        + _journald_envelope({"event": "ledger_label_pass", "timestamp": "x"})
        + "\n"
        + "not json\n"
        + _journald_envelope(_evict_event())
        + "\n",
        encoding="utf-8",
    )
    rows = list(iter_eviction_records(path))
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Apply / dry-run / idempotency
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_path(tmp_path):
    p = tmp_path / "backfill.db"
    database = Database(str(p))
    await database.initialize()
    await database.close()
    return str(p)


async def _count(db_path: str) -> int:
    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_enrollment_evictions"
        )
        return int((await cur.fetchone())["n"])
    finally:
        await db.close()


async def test_dry_run_is_default_and_inserts_nothing(tmp_path, db_path):
    jp = tmp_path / "export.jsonl"
    jp.write_text(_journald_envelope(_evict_event()) + "\n", encoding="utf-8")
    matched = await backfill_file(db_path, jp, apply=False)
    assert matched == 2
    assert await _count(db_path) == 0


async def test_apply_inserts_and_is_idempotent(tmp_path, db_path):
    jp = tmp_path / "export.jsonl"
    # Same event duplicated on two lines (the export appends + dedups at READ
    # time, so the raw file can carry duplicates).
    jp.write_text(
        _journald_envelope(_evict_event())
        + "\n"
        + _journald_envelope(_evict_event())
        + "\n",
        encoding="utf-8",
    )
    first = await backfill_file(db_path, jp, apply=True)
    assert first == 2  # two distinct (token_id, evicted_at) rows
    assert await _count(db_path) == 2
    # Re-run: idempotent, zero new rows.
    second = await backfill_file(db_path, jp, apply=True)
    assert second == 0
    assert await _count(db_path) == 2


async def test_backfill_rows_marked_source_journal_backfill(tmp_path, db_path):
    jp = tmp_path / "export.jsonl"
    jp.write_text(_journald_envelope(_evict_event()) + "\n", encoding="utf-8")
    await backfill_file(db_path, jp, apply=True)
    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT DISTINCT source FROM ledger_enrollment_evictions"
        )
        sources = {r["source"] for r in await cur.fetchall()}
    finally:
        await db.close()
    assert sources == {"journal_backfill"}


async def test_backfill_parity_with_live_write(tmp_path, db_path):
    """Backfilling the journal record of a live eviction produces the SAME
    (token_id, evicted_at) key, so INSERT OR IGNORE dedups it against the live
    row rather than double-counting."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db = Database(db_path)
    await db.initialize()
    try:
        await db._conn.execute(
            "INSERT INTO ledger_enrollment_evictions "
            "(token_id, evicted_at, evicted_for, max_active, n_evicted, source) "
            "VALUES (?, ?, ?, ?, ?, 'live')",
            ("dex:ethereum:0xa", now_iso, "dex:ethereum:0xnew", 200, 1),
        )
        await db._conn.commit()
    finally:
        await db.close()

    jp = tmp_path / "export.jsonl"
    jp.write_text(
        _journald_envelope(
            _evict_event(
                evicted_token_ids=["dex:ethereum:0xa"],
                n_evicted=1,
                evicted_at=now_iso,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    inserted = await backfill_file(db_path, jp, apply=True)
    assert inserted == 0  # already present as a live row
    assert await _count(db_path) == 1
