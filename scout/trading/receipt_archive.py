"""Receipt lifecycle archive — hot SQLite + time-partitioned compressed cold store.

Approved architecture (reviewer, 2026-07-26; see
tasks/capacity_detection_receipts_2026_07.md). Under the two-identity model every
cycle writes a receipt per evaluated token, so the hot
``detection_decision_receipts`` table would grow ~360k rows/day. This module keeps
the HOT tier bounded by moving ORDINARY post-index receipts (the ~99.9% ``too_old``
re-polls) into an append-only, gzip-compressed, time-partitioned COLD archive with
a queryable integrity manifest — WITHOUT ever dropping, sampling, or reducing the
detail of any evaluation.

HOT tier (stays in scout.db, normal backup regime): cohort defs/status; analytical
INDEX receipts; in-lifecycle rows (not yet matured/reconciled); anything under
investigation. Ordinary post-index receipts leave hot ONLY via the fail-closed
7-step archival transaction (:meth:`ReceiptArchiver.archive_once`).

Nothing here changes send/gate/product behavior. The disk-pressure guard suspends
receipt ACCRUAL (never the send path) when the box is low on space.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from scout.config import Settings
from scout.db import Database

log = structlog.get_logger(__name__)

# Archive format identity — stamped into every manifest row so a future format
# change is detectable and old partitions remain readable by-version.
DETECTION_ARCHIVE_SCHEMA_VERSION = 1
DETECTION_ARCHIVE_SERIALIZATION = "jsonl-gzip-v1"
DETECTION_ARCHIVE_TOOL_VERSION = "receipt-archive-1.0"

# Full receipt column set, in a FIXED order — the canonical serialization + the
# content hash depend on this order, so it must never be reordered (only appended
# to, with a serialization-version bump).
_RECEIPT_COLUMNS = (
    "id",
    "token_id",
    "decided_at",
    "outcome",
    "reason",
    "source_observation_ts",
    "gate_version",
    "code_version",
    "score_before",
    "score_after",
    "comparator",
    "threshold_value",
    "signals_fired",
    "raw_inputs",
    "idempotency_key",
)


# ---------------------------------------------------------------------------
# Disk-pressure guard (fail-closed; suspends ACCRUAL, never the send path)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskPressure:
    healthy: bool
    free_bytes: int
    total_bytes: int
    free_pct: float
    min_free_gb: float
    min_free_pct: float

    def reason(self) -> str:
        if self.healthy:
            return "ok"
        return (
            f"free={self.free_bytes / 1e9:.2f}GB ({self.free_pct:.1f}%) < "
            f"min({self.min_free_gb}GB, {self.min_free_pct}%)"
        )


def check_disk_pressure(path: str, settings: Settings) -> DiskPressure:
    """Return the disk-pressure state for the filesystem holding ``path``.

    Fail-CLOSED: any error reading disk usage yields ``healthy=False`` (assume
    pressure) so accrual is suspended rather than risking an unbounded write.
    """
    min_gb = settings.DETECTION_RECEIPT_DISK_MIN_FREE_GB
    min_pct = settings.DETECTION_RECEIPT_DISK_MIN_FREE_PCT
    try:
        usage = shutil.disk_usage(path)
        free_pct = (usage.free / usage.total * 100.0) if usage.total else 0.0
        healthy = usage.free >= min_gb * 1e9 and free_pct >= min_pct
        return DiskPressure(
            healthy=healthy,
            free_bytes=usage.free,
            total_bytes=usage.total,
            free_pct=free_pct,
            min_free_gb=min_gb,
            min_free_pct=min_pct,
        )
    except Exception:
        log.exception("detection_receipt_disk_probe_failed", path=path)
        return DiskPressure(False, 0, 0, 0.0, min_gb, min_pct)


# ---------------------------------------------------------------------------
# Deterministic serialization + hashing
# ---------------------------------------------------------------------------


def _row_to_record(row) -> dict:
    """Map a receipt row (aiosqlite.Row or tuple in _RECEIPT_COLUMNS order)."""
    if hasattr(row, "keys"):
        return {c: row[c] for c in _RECEIPT_COLUMNS}
    return {c: row[i] for i, c in enumerate(_RECEIPT_COLUMNS)}


def canonical_bytes(records: list[dict]) -> bytes:
    """Deterministic canonical serialization of receipt records.

    Rows sorted by ``id`` ascending; each row a JSON object with sorted keys;
    newline-joined; UTF-8. Independent of the compression codec, so the content
    hash is stable across gzip/zstd/plaintext.
    """
    ordered = sorted(records, key=lambda r: r["id"])
    lines = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in ordered]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def content_hash(records: list[dict]) -> str:
    return hashlib.sha256(canonical_bytes(records)).hexdigest()


def source_ids_hash(ids: list[int]) -> str:
    """Hash of the EXACT set of source receipt ids (order-independent)."""
    joined = ",".join(str(i) for i in sorted(ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Analytical INDEX identity — deterministic, tie-free, cohort-scoped
# ---------------------------------------------------------------------------


def _shakedown_predicate(cohort: dict) -> tuple[str, list]:
    """SQL predicate + params restricting to VALID (in-cohort, post-shakedown)
    receipts. Embeds the cohort start + shakedown-range exclusion."""
    clause = "decided_at >= ?"
    params: list = [cohort["cohort_start"]]
    ss, se = cohort.get("shakedown_start"), cohort.get("shakedown_end")
    if ss and se:
        clause += " AND NOT (decided_at >= ? AND decided_at < ?)"
        params += [ss, se]
    return clause, params


async def index_decisions(db: Database, cohort: dict) -> list[dict]:
    """The token's FIRST VALID evaluation after cohort start — one deterministic
    row per token that permanently fixes its arm + endpoint anchor.

    Deterministic ordering ``(decided_at ASC, id ASC)`` scoped to the valid
    cohort (``ROW_NUMBER() … rn = 1``). ``id`` is a unique immutable primary key,
    so ties on ``decided_at`` are fully broken and **exactly one** index row per
    token is possible. First evaluated outcome permanently classifies the token
    (primary arm OR attrition class); later evaluations never reclassify.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized")
    pred, params = _shakedown_predicate(cohort)
    sql = (
        "SELECT token_id, index_receipt_id, index_decision_at, index_outcome "
        "FROM (SELECT token_id, id AS index_receipt_id, decided_at AS index_decision_at, "
        "             outcome AS index_outcome, "
        "             ROW_NUMBER() OVER (PARTITION BY token_id "
        "               ORDER BY decided_at ASC, id ASC) AS rn "
        f"      FROM detection_decision_receipts WHERE {pred}) "
        "WHERE rn = 1"
    )
    cur = await db._conn.execute(sql, params)
    return [
        {
            "token_id": r[0],
            "index_receipt_id": r[1],
            "index_decision_at": r[2],
            "index_outcome": r[3],
        }
        for r in await cur.fetchall()
    ]


async def _index_receipt_ids(db: Database, cohort: dict) -> set[int]:
    return {d["index_receipt_id"] for d in await index_decisions(db, cohort)}


# ---------------------------------------------------------------------------
# The archival transaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveResult:
    status: str  # published_and_deleted | held_hot_no_offhost | nothing_eligible
    #             | disk_pressure | verify_failed | reconcile_failed | disabled
    partition_id: str | None = None
    record_count: int = 0
    deleted_hot_rows: int = 0
    detail: str = ""


class ReceiptArchiver:
    """Executes the fail-closed 7-step archival transaction for one partition.

    Any failure or partial at any step leaves the HOT rows UNTOUCHED.
    """

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def archive_once(
        self, cohort: dict, *, now: datetime | None = None
    ) -> ArchiveResult:
        s = self.settings
        # DORMANCY: when the receipts subsystem is disabled, do NO archive work.
        if not s.DETECTION_RECEIPTS_ENABLED:
            return ArchiveResult("disabled", detail="receipts subsystem disabled")
        if (
            not s.DETECTION_RECEIPT_ARCHIVE_ENABLED
            or not s.DETECTION_RECEIPT_ARCHIVE_DIR
        ):
            return ArchiveResult("disabled", detail="archive not enabled/configured")
        now = now or datetime.now(timezone.utc)
        archive_dir = s.DETECTION_RECEIPT_ARCHIVE_DIR
        os.makedirs(archive_dir, exist_ok=True)

        # Disk-pressure gate: don't write a temp partition onto a full disk.
        dp = check_disk_pressure(archive_dir, s)
        if not dp.healthy:
            log.warning("detection_receipt_archive_disk_pressure", reason=dp.reason())
            return ArchiveResult("disk_pressure", detail=dp.reason())

        # --- Step 1: select the frozen receipt range (eligibility) -----------
        rows = await self._select_frozen_range(cohort, now)
        if not rows:
            return ArchiveResult("nothing_eligible")
        records = [_row_to_record(r) for r in rows]
        ids = [rec["id"] for rec in records]
        expected_hash = content_hash(records)
        expected_ids_hash = source_ids_hash(ids)
        min_id, max_id = min(ids), max(ids)
        min_dt = min(rec["decided_at"] for rec in records)
        max_dt = max(rec["decided_at"] for rec in records)
        partition_id = f"{cohort['cohort_key']}__{min_id:012d}_{max_id:012d}"
        final_path = os.path.join(archive_dir, partition_id + ".jsonl.gz")
        tmp_path = final_path + ".partial"

        # --- Step 2: write temp archive partition ----------------------------
        payload = canonical_bytes(records)
        try:
            with gzip.open(tmp_path, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())  # Step 3a: flush to durable storage
        except Exception:
            log.exception(
                "detection_receipt_archive_write_failed", partition=partition_id
            )
            _safe_unlink(tmp_path)
            return ArchiveResult(
                "verify_failed", partition_id, detail="temp write failed"
            )

        # --- Step 3b: verify the temp partition reads back byte-exact --------
        try:
            with gzip.open(tmp_path, "rb") as fh:
                readback = fh.read()
        except Exception:
            log.exception(
                "detection_receipt_archive_readback_failed", partition=partition_id
            )
            _safe_unlink(tmp_path)
            return ArchiveResult(
                "verify_failed", partition_id, detail="readback failed"
            )
        if readback != payload or hashlib.sha256(readback).hexdigest() != expected_hash:
            log.error(
                "detection_receipt_archive_verify_mismatch", partition=partition_id
            )
            _safe_unlink(tmp_path)
            return ArchiveResult(
                "verify_failed", partition_id, detail="content hash mismatch"
            )

        # --- Step 4: reconcile count + content hash vs the LIVE hot rows -----
        live_rows = await self._fetch_rows_by_ids(ids)
        live_records = [_row_to_record(r) for r in live_rows]
        if (
            len(live_records) != len(records)
            or content_hash(live_records) != expected_hash
        ):
            log.error(
                "detection_receipt_archive_reconcile_mismatch", partition=partition_id
            )
            _safe_unlink(tmp_path)
            return ArchiveResult(
                "reconcile_failed", partition_id, detail="hot rows changed"
            )

        # --- Step 5: atomically publish partition + manifest ----------------
        compressed_bytes = os.path.getsize(tmp_path)
        os.replace(tmp_path, final_path)  # atomic within the same filesystem
        await self._insert_manifest(
            partition_id=partition_id,
            record_count=len(records),
            min_dt=min_dt,
            max_dt=max_dt,
            cohort_ids=[cohort["id"]],
            content_hash_hex=expected_hash,
            source_min_id=min_id,
            source_max_id=max_id,
            source_ids_hash_hex=expected_ids_hash,
            partition_path=final_path,
            compressed_bytes=compressed_bytes,
            now=now,
        )

        # --- Step 6: confirm an independent durable OFF-HOST copy -----------
        offhost_ok, offhost_detail = self._confirm_offhost(
            final_path, partition_id, expected_hash
        )
        if not offhost_ok:
            # FAIL-CLOSED: partition + manifest are published, but the hot rows
            # are NOT deleted until a durable off-host copy is confirmed.
            log.warning(
                "detection_receipt_archive_held_hot_no_offhost",
                partition=partition_id,
                detail=offhost_detail,
            )
            return ArchiveResult(
                "held_hot_no_offhost",
                partition_id,
                record_count=len(records),
                detail=offhost_detail,
            )
        await self._mark_offhost_confirmed(partition_id, now)

        # --- Step 7: ONLY NOW delete the eligible hot rows ------------------
        deleted = await self._delete_hot_rows(ids)
        log.info(
            "detection_receipt_archive_published",
            partition=partition_id,
            records=len(records),
            deleted_hot_rows=deleted,
        )
        return ArchiveResult(
            "published_and_deleted",
            partition_id,
            record_count=len(records),
            deleted_hot_rows=deleted,
        )

    # -- step helpers --------------------------------------------------------

    async def _select_frozen_range(self, cohort: dict, now: datetime) -> list:
        """Eligible = matured (>= horizon) AND reconciled AND in-cohort AND NOT an
        index receipt AND NOT shakedown. Ordered by id, capped at the partition
        max. Index receipts + unreconciled/immature rows stay HOT."""
        horizon_cut = (
            now - timedelta(hours=self.settings.DETECTION_RECEIPT_ARCHIVE_HORIZON_HOURS)
        ).isoformat()
        reconciled_through = cohort.get("reconciled_through")
        if not reconciled_through:
            return []  # nothing reconciled yet → nothing eligible
        pred, params = _shakedown_predicate(cohort)
        index_ids = await _index_receipt_ids(self.db, cohort)
        cols = ", ".join(_RECEIPT_COLUMNS)
        sql = (
            f"SELECT {cols} FROM detection_decision_receipts "
            f"WHERE {pred} AND decided_at <= ? AND decided_at <= ? "
            "ORDER BY id ASC LIMIT ?"
        )
        args = params + [
            horizon_cut,
            reconciled_through,
            self.settings.DETECTION_RECEIPT_ARCHIVE_PARTITION_MAX_ROWS,
        ]
        cur = await self.db._conn.execute(sql, args)
        rows = await cur.fetchall()
        return [r for r in rows if r["id"] not in index_ids]

    async def _fetch_rows_by_ids(self, ids: list[int]) -> list:
        cols = ", ".join(_RECEIPT_COLUMNS)
        qmarks = ",".join("?" for _ in ids)
        cur = await self.db._conn.execute(
            f"SELECT {cols} FROM detection_decision_receipts WHERE id IN ({qmarks})",
            ids,
        )
        return await cur.fetchall()

    async def _insert_manifest(self, **kw) -> None:
        async with self.db._txn_lock:
            await self.db._conn.execute(
                "INSERT OR IGNORE INTO detection_receipt_archive_manifest "
                "(partition_id, schema_version, serialization, archive_tool_version, "
                " record_count, min_decided_at, max_decided_at, cohort_ids, content_hash, "
                " source_min_receipt_id, source_max_receipt_id, source_ids_hash, "
                " partition_path, compressed_bytes, created_at, verified_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published')",
                (
                    kw["partition_id"],
                    DETECTION_ARCHIVE_SCHEMA_VERSION,
                    DETECTION_ARCHIVE_SERIALIZATION,
                    DETECTION_ARCHIVE_TOOL_VERSION,
                    kw["record_count"],
                    kw["min_dt"],
                    kw["max_dt"],
                    json.dumps(kw["cohort_ids"]),
                    kw["content_hash_hex"],
                    kw["source_min_id"],
                    kw["source_max_id"],
                    kw["source_ids_hash_hex"],
                    kw["partition_path"],
                    kw["compressed_bytes"],
                    kw["now"].isoformat(),
                    kw["now"].isoformat(),
                ),
            )
            await self.db._conn.commit()

    def _confirm_offhost(self, final_path: str, partition_id: str, expected_hash: str):
        """Place + verify an independent durable off-host copy (step 6).

        A LOCAL-ONLY directory is INSUFFICIENT — ``DETECTION_RECEIPT_OFFHOST_DIR``
        MUST be an off-host / replicated location (the operator's responsibility;
        the box's gecko backup is local-only, see the capacity artifact). When the
        setting is empty, NO off-host destination is provisioned → return False so
        step 6 fails closed and the caller holds the rows hot.
        """
        offhost_dir = self.settings.DETECTION_RECEIPT_OFFHOST_DIR
        if not offhost_dir:
            return (
                False,
                "off-host destination = operator-provisioned dependency (unset)",
            )
        try:
            os.makedirs(offhost_dir, exist_ok=True)
            dest = os.path.join(offhost_dir, os.path.basename(final_path))
            dest_tmp = dest + ".partial"
            # Copy + fsync the WRITE fd (durable, cross-platform), then atomic rename.
            with open(final_path, "rb") as src, open(dest_tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(dest_tmp, dest)
            # Verify the off-host copy hashes to the same canonical content.
            with gzip.open(dest, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != expected_hash:
                    return False, "off-host copy hash mismatch"
            return True, dest
        except Exception as e:
            log.exception(
                "detection_receipt_offhost_copy_failed", partition=partition_id
            )
            return False, f"off-host copy failed: {e}"

    async def _mark_offhost_confirmed(self, partition_id: str, now: datetime) -> None:
        async with self.db._txn_lock:
            await self.db._conn.execute(
                "UPDATE detection_receipt_archive_manifest "
                "SET offhost_confirmed = 1, offhost_confirmed_at = ? WHERE partition_id = ?",
                (now.isoformat(), partition_id),
            )
            await self.db._conn.commit()

    async def _delete_hot_rows(self, ids: list[int]) -> int:
        qmarks = ",".join("?" for _ in ids)
        async with self.db._txn_lock:
            cur = await self.db._conn.execute(
                f"DELETE FROM detection_decision_receipts WHERE id IN ({qmarks})", ids
            )
            await self.db._conn.commit()
        return cur.rowcount or 0


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("detection_receipt_archive_unlink_failed", path=path)


# ---------------------------------------------------------------------------
# Cold reader + verification + restore-and-reconcile
# ---------------------------------------------------------------------------


def read_partition(partition_path: str) -> list[dict]:
    """Decompress + parse a cold partition into receipt records (queryable)."""
    with gzip.open(partition_path, "rb") as fh:
        raw = fh.read()
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]


def verify_partition(manifest_row: dict) -> tuple[bool, str]:
    """Recompute count + content hash from the partition file and compare to the
    manifest. Detects corruption / truncation / tampering."""
    path = manifest_row["partition_path"]
    if not os.path.exists(path):
        return False, "partition file missing"
    try:
        records = read_partition(path)
    except Exception as e:
        return False, f"unreadable: {e}"
    if len(records) != manifest_row["record_count"]:
        return False, (
            f"count mismatch: file={len(records)} manifest={manifest_row['record_count']}"
        )
    if content_hash(records) != manifest_row["content_hash"]:
        return False, "content hash mismatch"
    return True, "ok"


class ColdArchiveReader:
    """Queries cold partitions via the hot manifest (endpoint analysis, audits)."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def manifests_overlapping(
        self, *, min_decided_at: str, max_decided_at: str
    ) -> list[dict]:
        cur = await self.db._conn.execute(
            "SELECT * FROM detection_receipt_archive_manifest "
            "WHERE NOT (max_decided_at < ? OR min_decided_at > ?) "
            "ORDER BY min_decided_at",
            (min_decided_at, max_decided_at),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def read_range(
        self, *, min_decided_at: str, max_decided_at: str
    ) -> list[dict]:
        """Return every archived receipt whose ``decided_at`` is in the window,
        verifying each partition's integrity before yielding its rows."""
        out: list[dict] = []
        for m in await self.manifests_overlapping(
            min_decided_at=min_decided_at, max_decided_at=max_decided_at
        ):
            ok, detail = verify_partition(m)
            if not ok:
                raise RuntimeError(
                    f"cold partition {m['partition_id']} failed integrity: {detail}"
                )
            for rec in read_partition(m["partition_path"]):
                if min_decided_at <= rec["decided_at"] <= max_decided_at:
                    out.append(rec)
        return out


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    partition_id: str
    record_count: int
    detail: str


async def restore_and_reconcile(
    db: Database, settings: Settings, partition_id: str
) -> ReconcileResult:
    """End-to-end restore + reconcile of one published partition.

    Reads the manifest, restores the cold partition from disk, re-verifies count
    + content hash + source-id hash against the manifest, and confirms the source
    hot rows are gone (archived, not duplicated). Proves the cold copy is a
    faithful, queryable, reproducible replacement for the deleted hot rows.
    """
    cur = await db._conn.execute(
        "SELECT * FROM detection_receipt_archive_manifest WHERE partition_id = ?",
        (partition_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return ReconcileResult(False, partition_id, 0, "manifest not found")
    m = dict(row)
    ok, detail = verify_partition(m)
    if not ok:
        return ReconcileResult(False, partition_id, 0, f"verify failed: {detail}")
    records = read_partition(m["partition_path"])
    ids = [rec["id"] for rec in records]
    if source_ids_hash(ids) != m["source_ids_hash"]:
        return ReconcileResult(
            False, partition_id, len(records), "source-id hash mismatch"
        )
    # The archived rows must NOT still be hot (no duplication across tiers).
    qmarks = ",".join("?" for _ in ids)
    cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM detection_decision_receipts WHERE id IN ({qmarks})", ids
    )
    still_hot = (await cur.fetchone())[0]
    if still_hot:
        return ReconcileResult(
            False, partition_id, len(records), f"{still_hot} archived rows still hot"
        )
    return ReconcileResult(True, partition_id, len(records), "ok")
