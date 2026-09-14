"""Bounded read-only observations; never an execution eligibility decision."""

import asyncio
import contextlib
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


class LaneStatusUnavailable(Exception):
    """A fixed public reason for an unavailable observation."""


def _evidence(value, limit, *, valid=True, storage_type=None):
    kind = (
        storage_type
        or {
            type(None): "null",
            int: "integer",
            float: "real",
            str: "text",
            bytes: "blob",
        }[type(value)]
    )
    text = (
        None
        if value is None
        else value.hex() if isinstance(value, bytes) else str(value)
    )
    return {
        "sqlite_type": kind,
        "display": text[:limit] if text is not None else None,
        "truncated": text is not None and len(text) > limit,
        "valid": valid,
    }


def _stamp_valid(value, observed):
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 80:
        return False
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", value
    ):
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc) <= observed
    except (ValueError, OverflowError):
        return False


def _lane(row, observed):
    name, enabled, kind, stamp, reason, calibrated = row
    canonical = kind == "integer" and type(enabled) is int and enabled in (0, 1)
    stamp_ok = _stamp_valid(stamp, observed)
    if not canonical:
        state, code = "unknown", "invalid_enabled_value"
    elif not stamp_ok:
        state, code = "unknown", "invalid_suspension_timestamp"
    elif enabled == 1 and stamp is not None:
        state, code = "unknown", "conflicting_suspension_metadata"
    elif enabled == 1:
        state, code = "enabled", "enabled_in_store"
    elif stamp is None:
        state, code = "disabled", "disabled_in_store"
    else:
        state, code = "suspended", "suspended_in_store"
    return {
        "signal_type": name,
        "state": state,
        "reason": code,
        "evidence": {
            "enabled": _evidence(enabled, 256, valid=canonical, storage_type=kind),
            "suspended_at": _evidence(stamp, 80, valid=stamp_ok),
            "suspended_reason": _evidence(
                reason, 1024, valid=reason is None or isinstance(reason, str)
            ),
            "last_calibration_at": _evidence(
                calibrated, 80, valid=calibrated is None or isinstance(calibrated, str)
            ),
        },
    }


def metadata(ok: bool, observed_at: str | None, reason: str | None = None) -> dict:
    meta = {
        "ok": ok,
        "source": "signal_params",
        "read_only": True,
        "not_execution_eligibility": True,
        "observed_at": observed_at,
    }
    if reason is not None:
        meta["reason"] = reason
    return meta


def read_lane_status(db_path: str) -> dict:
    """The worker owns acquisition and close even if its awaiter is cancelled."""
    started = time.monotonic()
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            conn.set_progress_handler(
                lambda: int(time.monotonic() - started >= 1), 1000
            )
            observed = datetime.now(timezone.utc)
            rows = conn.execute(
                "SELECT signal_type, enabled, typeof(enabled), suspended_at, "
                "suspended_reason, last_calibration_at FROM signal_params "
                "ORDER BY signal_type LIMIT 129"
            ).fetchall()
        if len(rows) > 128:
            raise LaneStatusUnavailable("read_limit")
        keys = set()
        for row in rows:
            key = row[0]
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or any(unicodedata.category(c) == "Cc" for c in key)
                or key in keys
            ):
                raise LaneStatusUnavailable("invalid_keys")
            keys.add(key)
        return {
            "meta": metadata(True, observed.isoformat()),
            "lanes": [_lane(row, observed) for row in rows],
        }
    except sqlite3.Error as exc:
        message = str(exc).lower()
        if "no such table" in message or "no such column" in message:
            reason = "schema_unavailable"
        elif isinstance(exc, sqlite3.DataError) or "interrupted" in message:
            reason = "read_limit"
        else:
            reason = "read_unavailable"
        raise LaneStatusUnavailable(reason) from exc
    except (OSError, ValueError) as exc:
        raise LaneStatusUnavailable("read_unavailable") from exc


async def get_lane_status(db_path: str) -> dict:
    return await asyncio.to_thread(read_lane_status, db_path)
