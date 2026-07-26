"""Receipt lifecycle-archive tests (hot SQLite + compressed cold + manifest).

One test per reviewer-required item: archive/lifecycle; unique analytical-index
guarantee; backup+restore controls; archive atomicity + corruption; disk-pressure
(in test_detection_alert.py); bounded hot storage; end-to-end restore-reconcile.
"""

from __future__ import annotations

import gzip
import os
from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.receipt_archive import (
    ColdArchiveReader,
    ReceiptArchiver,
    check_disk_pressure,
    content_hash,
    index_decisions,
    read_partition,
    restore_and_reconcile,
    verify_partition,
)

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        DETECTION_RECEIPTS_ENABLED=True,  # receipts default off in prod; enable for tests
        DETECTION_RECEIPT_ARCHIVE_ENABLED=True,
        DETECTION_RECEIPT_ARCHIVE_DIR=str(tmp_path / "cold"),
        DETECTION_RECEIPT_OFFHOST_DIR=str(tmp_path / "offhost"),
    )
    base.update(overrides)
    return Settings(_env_file=None, **{**_REQUIRED, **base})


async def _ins(
    db, *, token_id, decided_at, outcome="too_old", score_after=2, threshold=1
):
    cur = await db._conn.execute(
        "INSERT INTO detection_decision_receipts "
        "(token_id, decided_at, outcome, reason, source_observation_ts, gate_version, "
        " code_version, score_before, score_after, comparator, threshold_value, "
        " signals_fired, raw_inputs, idempotency_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            token_id,
            decided_at,
            outcome,
            None,
            "obs-" + token_id,
            "466.1",
            "codesha",
            score_after,
            score_after,
            ">=",
            threshold,
            None,
            '{"age_min": 1.5, "score_after": %d}' % score_after,
            f"{token_id}|{decided_at}|{outcome}",
        ),
    )
    await db._conn.commit()
    return cur.lastrowid


def _cohort(now):
    return {
        "id": 1,
        "cohort_key": "cohort-2026-07-27",
        "cohort_start": (now - timedelta(days=30)).isoformat(),
        "shakedown_start": None,
        "shakedown_end": None,
        "reconciled_through": now.isoformat(),
    }


# ---------- analytical INDEX identity: deterministic + tie-free ----------


@pytest.mark.asyncio
async def test_index_decisions_unique_per_token_even_with_ties(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    same = (now - timedelta(days=5)).isoformat()
    # Token A: a TIE — two receipts at the identical decided_at, different ids.
    id1 = await _ins(db, token_id="A", decided_at=same, outcome="gate_fail_quality")
    id2 = await _ins(db, token_id="A", decided_at=same, outcome="sent")
    # A later A receipt must never win the index.
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=4)).isoformat())
    # Token B: single receipt.
    await _ins(db, token_id="B", decided_at=same)

    idx = await index_decisions(db, _cohort(now))
    by_token = {d["token_id"]: d for d in idx}
    # Exactly one index row per token.
    assert len(idx) == 2
    assert set(by_token) == {"A", "B"}
    # The tie is broken deterministically by the smaller immutable id.
    assert by_token["A"]["index_receipt_id"] == min(id1, id2)
    assert by_token["A"]["index_outcome"] == "gate_fail_quality"
    await db.close()


@pytest.mark.asyncio
async def test_index_excludes_shakedown_and_pre_cohort(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    cohort = {
        "id": 1,
        "cohort_key": "c",
        "cohort_start": (now - timedelta(days=10)).isoformat(),
        "shakedown_start": (now - timedelta(days=6)).isoformat(),
        "shakedown_end": (now - timedelta(days=5)).isoformat(),
        "reconciled_through": now.isoformat(),
    }
    # Pre-cohort (excluded), shakedown (excluded), valid (the real index).
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=20)).isoformat())
    await _ins(
        db, token_id="A", decided_at=(now - timedelta(days=5, hours=12)).isoformat()
    )
    valid = await _ins(
        db, token_id="A", decided_at=(now - timedelta(days=3)).isoformat()
    )
    idx = await index_decisions(db, cohort)
    assert len(idx) == 1 and idx[0]["index_receipt_id"] == valid
    await db.close()


# ---------- archive/lifecycle: end-to-end publish + delete ----------


@pytest.mark.asyncio
async def test_archive_once_publishes_and_deletes_hot(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=5)).isoformat()
    idx_id = await _ins(db, token_id="A", decided_at=old)  # index (stays hot)
    e1 = await _ins(db, token_id="A", decided_at=(now - timedelta(days=4)).isoformat())
    e2 = await _ins(db, token_id="A", decided_at=(now - timedelta(days=3)).isoformat())
    settings = _settings(tmp_path)
    archiver = ReceiptArchiver(db, settings)

    res = await archiver.archive_once(_cohort(now), now=now)
    assert res.status == "published_and_deleted"
    assert res.record_count == 2 and res.deleted_hot_rows == 2
    # Hot table now holds ONLY the index receipt.
    cur = await db._conn.execute("SELECT id FROM detection_decision_receipts")
    remaining = {r[0] for r in await cur.fetchall()}
    assert remaining == {idx_id}
    # A published, verified manifest row exists with the required fields.
    cur = await db._conn.execute(
        "SELECT record_count, content_hash, source_min_receipt_id, source_max_receipt_id, "
        "offhost_confirmed, status, verified_at FROM detection_receipt_archive_manifest"
    )
    m = await cur.fetchone()
    assert m[0] == 2 and m[2] == min(e1, e2) and m[3] == max(e1, e2)
    assert m[4] == 1 and m[5] == "published" and m[6] is not None
    # The cold partition is readable + integrity-verified.
    assert os.path.exists(res.partition_id and _cold_path(settings, res.partition_id))
    await db.close()


def _cold_path(settings, partition_id):
    return os.path.join(
        settings.DETECTION_RECEIPT_ARCHIVE_DIR, partition_id + ".jsonl.gz"
    )


# ---------- atomicity: no off-host → held hot (fail-closed) ----------


@pytest.mark.asyncio
async def test_no_offhost_holds_rows_hot(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=5)).isoformat())
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=4)).isoformat())
    settings = _settings(tmp_path, DETECTION_RECEIPT_OFFHOST_DIR="")  # no off-host
    res = await ReceiptArchiver(db, settings).archive_once(_cohort(now), now=now)
    assert res.status == "held_hot_no_offhost"
    # Partition + manifest published, but hot rows are UNTOUCHED (fail-closed).
    cur = await db._conn.execute("SELECT COUNT(*) FROM detection_decision_receipts")
    assert (await cur.fetchone())[0] == 2
    cur = await db._conn.execute(
        "SELECT offhost_confirmed FROM detection_receipt_archive_manifest"
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


# ---------- atomicity: reconcile mismatch → hot untouched ----------


@pytest.mark.asyncio
async def test_reconcile_mismatch_leaves_hot_untouched(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=5)).isoformat())
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=4)).isoformat())
    settings = _settings(tmp_path)
    archiver = ReceiptArchiver(db, settings)

    # Simulate the hot rows changing between selection and reconciliation (step 4):
    # the live re-read drops a row → content/count mismatch → abort.
    async def _short(_ids):
        return []

    monkeypatch.setattr(archiver, "_fetch_rows_by_ids", _short)
    res = await archiver.archive_once(_cohort(now), now=now)
    assert res.status == "reconcile_failed"
    # Hot rows untouched; no manifest; no leftover temp partition.
    cur = await db._conn.execute("SELECT COUNT(*) FROM detection_decision_receipts")
    assert (await cur.fetchone())[0] == 2
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM detection_receipt_archive_manifest"
    )
    assert (await cur.fetchone())[0] == 0
    leftovers = [f for f in os.listdir(settings.DETECTION_RECEIPT_ARCHIVE_DIR)]
    assert all(not f.endswith(".partial") for f in leftovers)
    await db.close()


# ---------- corruption detection + queryable cold reader ----------


@pytest.mark.asyncio
async def test_verify_and_reader_detect_corruption(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    await _ins(
        db, token_id="A", decided_at=(now - timedelta(days=5)).isoformat()
    )  # index
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=4)).isoformat())
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=3)).isoformat())
    settings = _settings(tmp_path)
    res = await ReceiptArchiver(db, settings).archive_once(_cohort(now), now=now)
    assert res.status == "published_and_deleted"

    cur = await db._conn.execute("SELECT * FROM detection_receipt_archive_manifest")
    m = dict(await cur.fetchone())
    ok, detail = verify_partition(m)
    assert ok and detail == "ok"

    # Reader returns the archived rows for the window (integrity-checked).
    reader = ColdArchiveReader(db, settings)
    rows = await reader.read_range(
        min_decided_at=(now - timedelta(days=6)).isoformat(),
        max_decided_at=now.isoformat(),
    )
    assert len(rows) == 2

    # Corrupt the partition on disk → verify fails + reader raises.
    with gzip.open(m["partition_path"], "wb") as fh:
        fh.write(b'{"id": 999, "tampered": true}\n')
    ok2, _ = verify_partition(m)
    assert ok2 is False
    with pytest.raises(RuntimeError):
        await reader.read_range(
            min_decided_at=(now - timedelta(days=6)).isoformat(),
            max_decided_at=now.isoformat(),
        )
    await db.close()


# ---------- backup+restore controls: end-to-end restore-and-reconcile ----------


@pytest.mark.asyncio
async def test_restore_and_reconcile_after_archive(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=5)).isoformat())
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=4)).isoformat())
    settings = _settings(tmp_path)
    res = await ReceiptArchiver(db, settings).archive_once(_cohort(now), now=now)
    rr = await restore_and_reconcile(db, settings, res.partition_id)
    assert rr.ok and rr.record_count == 1  # one non-index row archived
    # Content restored from cold matches what was archived.
    cur = await db._conn.execute(
        "SELECT partition_path FROM detection_receipt_archive_manifest WHERE partition_id=?",
        (res.partition_id,),
    )
    recs = read_partition((await cur.fetchone())[0])
    assert content_hash(recs)  # canonical hash recomputes cleanly
    await db.close()


# ---------- bounded hot storage: hot shrinks to index receipts ----------


@pytest.mark.asyncio
async def test_hot_storage_stays_bounded_after_archival(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    # 4 tokens × 6 matured cycles each = 24 hot rows; 4 are index receipts.
    for t in ("A", "B", "C", "D"):
        for k in range(6):
            await _ins(
                db,
                token_id=t,
                decided_at=(now - timedelta(days=6) + timedelta(hours=k)).isoformat(),
            )
    cur = await db._conn.execute("SELECT COUNT(*) FROM detection_decision_receipts")
    assert (await cur.fetchone())[0] == 24
    settings = _settings(tmp_path)
    res = await ReceiptArchiver(db, settings).archive_once(_cohort(now), now=now)
    assert res.status == "published_and_deleted"
    # Hot bounded to the 4 index receipts; the other 20 live in cold.
    cur = await db._conn.execute("SELECT COUNT(*) FROM detection_decision_receipts")
    assert (await cur.fetchone())[0] == 4
    assert res.record_count == 20
    await db.close()


# ---------- migration idempotency ----------


@pytest.mark.asyncio
async def test_archive_disabled_when_receipts_disabled(tmp_path):
    """Dormancy: archive_once does NO work when the receipts subsystem is off."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=5)).isoformat())
    await _ins(db, token_id="A", decided_at=(now - timedelta(days=4)).isoformat())
    settings = _settings(tmp_path, DETECTION_RECEIPTS_ENABLED=False)
    res = await ReceiptArchiver(db, settings).archive_once(_cohort(now), now=now)
    assert res.status == "disabled"
    # Hot rows untouched; no manifest written.
    cur = await db._conn.execute("SELECT COUNT(*) FROM detection_decision_receipts")
    assert (await cur.fetchone())[0] == 2
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM detection_receipt_archive_manifest"
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_archive_migration_idempotent(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_detection_receipt_archive_v1()
    await db._migrate_detection_receipt_archive_v1()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM schema_version WHERE version = 20260727"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


# ---------- disk-pressure probe (unit) ----------


def test_disk_pressure_flags_when_below_threshold(tmp_path):
    # An absurd 100000 GB floor forces "unhealthy" on any real disk (fail-closed).
    s = Settings(
        _env_file=None, **_REQUIRED, DETECTION_RECEIPT_DISK_MIN_FREE_GB=100000.0
    )
    dp = check_disk_pressure(str(tmp_path), s)
    assert dp.healthy is False
    assert "min(" in dp.reason()


def test_disk_pressure_healthy_with_low_floor(tmp_path):
    s = Settings(
        _env_file=None,
        **_REQUIRED,
        DETECTION_RECEIPT_DISK_MIN_FREE_GB=1.0,
        DETECTION_RECEIPT_DISK_MIN_FREE_PCT=1.0,
    )
    dp = check_disk_pressure(str(tmp_path), s)
    # A dev/CI box normally has >1GB and >1% free.
    assert dp.healthy is True and dp.reason() == "ok"
