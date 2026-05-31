"""Tests for the per-subsystem health status enum on /api/system/health.

Covers the pure deriver (dashboard.health_status.derive_subsystem_status) and its
wiring into dashboard.db.get_system_health, per operator decision D1:
closed 4-value enum (ok|degraded|down|unknown) + an EMPTY operator-fillable SLO
map (HEALTH_FRESHNESS_SLO_MINUTES).

These tests import dashboard.db / dashboard.health_status only -- NOT the FastAPI
app -- so they run on Windows regardless of the documented aiohttp/OpenSSL issue
(reference_windows_openssl_workaround). App-level smoke coverage lives in the
existing tests/test_dashboard_api.py TestSystemHealth cases.
"""

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from dashboard import db as dashboard_db
from dashboard import health_status
from dashboard.health_status import (
    READ_ERROR_COUNT_SENTINEL,
    derive_subsystem_status,
)

FIXED_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    """Render like SQLite MAX(time_col) over ISO-8601 columns (naive, no tz)."""
    return dt.replace(tzinfo=None).isoformat(sep="T")


# ---------------------------------------------------------------------------
# Pure deriver: derive_subsystem_status
# ---------------------------------------------------------------------------


def test_fresh_with_slo_is_ok():
    stats = {"count": 10, "latest": _iso(FIXED_NOW - timedelta(minutes=5))}
    assert derive_subsystem_status(stats, 60, FIXED_NOW) == "ok"


def test_stale_with_slo_is_degraded():
    stats = {"count": 10, "latest": _iso(FIXED_NOW - timedelta(minutes=120))}
    assert derive_subsystem_status(stats, 60, FIXED_NOW) == "degraded"


def test_read_error_sentinel_is_down():
    # count == -1 -> down regardless of SLO presence/absence.
    assert (
        derive_subsystem_status(
            {"count": READ_ERROR_COUNT_SENTINEL, "latest": None}, 60, FIXED_NOW
        )
        == "down"
    )
    assert (
        derive_subsystem_status(
            {"count": READ_ERROR_COUNT_SENTINEL, "latest": None}, None, FIXED_NOW
        )
        == "down"
    )


def test_empty_table_with_slo_is_unknown():
    # count == 0 + SLO defined -> unknown (cannot assess freshness, NOT down).
    stats = {"count": 0, "latest": None}
    assert derive_subsystem_status(stats, 60, FIXED_NOW) == "unknown"


def test_empty_table_without_slo_is_unknown():
    stats = {"count": 0, "latest": None}
    assert derive_subsystem_status(stats, None, FIXED_NOW) == "unknown"


def test_nonempty_without_slo_is_unknown():
    # The ship-state default: readable table, no SLO -> unknown (never guess).
    stats = {"count": 100, "latest": _iso(FIXED_NOW - timedelta(minutes=5))}
    assert derive_subsystem_status(stats, None, FIXED_NOW) == "unknown"


def test_boundary_age_equals_slo_is_ok():
    # age exactly == SLO -> ok (strict > for degraded).
    stats = {"count": 10, "latest": _iso(FIXED_NOW - timedelta(minutes=60))}
    assert derive_subsystem_status(stats, 60, FIXED_NOW) == "ok"


def test_boundary_age_just_over_slo_is_degraded():
    stats = {
        "count": 10,
        "latest": _iso(FIXED_NOW - timedelta(minutes=60, seconds=1)),
    }
    assert derive_subsystem_status(stats, 60, FIXED_NOW) == "degraded"


def test_unparseable_latest_with_slo_is_unknown():
    # Cannot compute age -> unknown, NOT a guessed degraded.
    stats = {"count": 10, "latest": "not-a-timestamp"}
    assert derive_subsystem_status(stats, 60, FIXED_NOW) == "unknown"


def test_latest_none_with_positive_count_with_slo_is_unknown():
    # Structurally impossible via _table_stats, but if it occurs we do not guess.
    stats = {"count": 10, "latest": None}
    assert derive_subsystem_status(stats, 60, FIXED_NOW) == "unknown"


def test_timezone_naive_and_z_suffix_parse_identically():
    naive = {"count": 10, "latest": "2026-05-30T11:55:00"}
    zsuffix = {"count": 10, "latest": "2026-05-30T11:55:00Z"}
    # 5 minutes old vs FIXED_NOW, SLO 60 -> both ok and equal.
    assert (
        derive_subsystem_status(naive, 60, FIXED_NOW)
        == derive_subsystem_status(zsuffix, 60, FIXED_NOW)
        == "ok"
    )


def test_injected_now_determinism_and_no_wall_clock():
    # Same stats+SLO, two now values straddling the threshold -> deterministic flip.
    # If the deriver consulted the wall clock instead of the injected `now`, this
    # flip could not be made to occur on demand -- so the flip itself is the
    # behavioral proof that `now` (not datetime.now()) drives the result.
    stats = {"count": 10, "latest": _iso(FIXED_NOW - timedelta(minutes=60))}
    now_before = FIXED_NOW  # age == 60 -> ok
    now_after = FIXED_NOW + timedelta(minutes=1)  # age == 61 -> degraded
    assert derive_subsystem_status(stats, 60, now_before) == "ok"
    assert derive_subsystem_status(stats, 60, now_after) == "degraded"

    # A now far in the past makes a "stale" row look fresh -- impossible unless the
    # injected value is the sole clock source.
    past_now = FIXED_NOW - timedelta(days=365)
    very_stale = {"count": 10, "latest": _iso(FIXED_NOW - timedelta(minutes=1))}
    assert derive_subsystem_status(very_stale, 60, past_now) == "ok"

    # Structural guarantee: `now` is a required parameter with no default, so the
    # function cannot silently fall back to datetime.now().
    import inspect

    sig = inspect.signature(derive_subsystem_status)
    assert sig.parameters["now"].default is inspect.Parameter.empty
    # And the source contains no datetime.now()/utcnow() wall-clock call.
    src = inspect.getsource(health_status.derive_subsystem_status)
    assert "datetime.now" not in src and "utcnow" not in src


# ---------------------------------------------------------------------------
# Integration: get_system_health wiring (additive key, empty-map ship-state)
# ---------------------------------------------------------------------------

_HEALTH_TABLE_DDL = {
    "candidates": ("candidates", "first_seen_at"),
    "alerts": ("alerts", "alerted_at"),
    "predictions": ("predictions", "predicted_at"),
}


async def _make_db(tmp_path) -> str:
    """Create a minimal DB with the three tables get_system_health probes here."""
    db_path = str(tmp_path / "health.db")
    async with aiosqlite.connect(db_path) as conn:
        # Create all 15 tables get_system_health iterates, so none hit the
        # read-error path; each gets its real time column.
        table_cols = [
            ("category_snapshots", "snapshot_at"),
            ("narrative_signals", "created_at"),
            ("predictions", "predicted_at"),
            ("second_wave_candidates", "detected_at"),
            ("signal_events", "created_at"),
            ("active_chains", "last_step_time"),
            ("chain_matches", "completed_at"),
            ("chain_patterns", "created_at"),
            ("briefings", "created_at"),
            ("trending_snapshots", "snapshot_at"),
            ("trending_comparisons", "created_at"),
            ("candidates", "first_seen_at"),
            ("alerts", "alerted_at"),
            ("learn_logs", "created_at"),
            ("agent_strategy", "updated_at"),
        ]
        for table, col in table_cols:
            await conn.execute(f"CREATE TABLE {table} ({col} TEXT)")
        # Seed a couple of tables with one fresh row each.
        fresh = _iso(FIXED_NOW - timedelta(minutes=5))
        await conn.execute(
            "INSERT INTO candidates (first_seen_at) VALUES (?)", (fresh,)
        )
        await conn.execute("INSERT INTO alerts (alerted_at) VALUES (?)", (fresh,))
        await conn.commit()
    return db_path


async def test_get_system_health_additive_key_and_shape(tmp_path):
    db_path = await _make_db(tmp_path)
    result = await dashboard_db.get_system_health(db_path, now=FIXED_NOW)

    # Every per-table value has exactly {count, latest, status}.
    for table, value in result.items():
        assert set(value.keys()) == {"count", "latest", "status"}, table
        assert value["status"] in {"ok", "degraded", "down", "unknown"}

    # count/latest preserved with their original meaning: candidates has 1 row.
    assert result["candidates"]["count"] == 1
    assert result["candidates"]["latest"] is not None
    # An empty present table still reports count == 0 (not the -1 sentinel).
    assert result["learn_logs"]["count"] == 0
    assert result["learn_logs"]["latest"] is None


async def test_get_system_health_empty_slo_map_all_unknown(tmp_path):
    # Ship-state: HEALTH_FRESHNESS_SLO_MINUTES is empty -> every readable table
    # (present, count >= 0) reports unknown.
    assert dashboard_db.HEALTH_FRESHNESS_SLO_MINUTES == {}
    db_path = await _make_db(tmp_path)
    result = await dashboard_db.get_system_health(db_path, now=FIXED_NOW)
    for table, value in result.items():
        assert value["status"] == "unknown", (table, value)


async def test_get_system_health_with_populated_slo(tmp_path, monkeypatch):
    # Temporarily populate the map (does NOT touch the shipped constant value).
    monkeypatch.setattr(
        dashboard_db, "HEALTH_FRESHNESS_SLO_MINUTES", {"candidates": 60, "alerts": 1}
    )
    db_path = await _make_db(tmp_path)
    result = await dashboard_db.get_system_health(db_path, now=FIXED_NOW)
    # candidates: fresh (5 min) within 60 -> ok.
    assert result["candidates"]["status"] == "ok"
    # alerts: 5 min old vs SLO 1 min -> degraded.
    assert result["alerts"]["status"] == "degraded"
    # predictions: SLO not in map, present-but-empty -> unknown.
    assert result["predictions"]["status"] == "unknown"


async def test_get_system_health_missing_table_is_down(tmp_path, monkeypatch):
    # Build a DB missing one of the probed tables -> _table_stats hits the
    # read-error path -> count == -1 -> status down.
    db_path = str(tmp_path / "partial.db")
    async with aiosqlite.connect(db_path) as conn:
        # Create only candidates; every other probed table is absent.
        await conn.execute("CREATE TABLE candidates (first_seen_at TEXT)")
        await conn.execute(
            "INSERT INTO candidates (first_seen_at) VALUES (?)",
            (_iso(FIXED_NOW - timedelta(minutes=5)),),
        )
        await conn.commit()
    result = await dashboard_db.get_system_health(db_path, now=FIXED_NOW)
    # A genuinely-absent table reads as the sentinel -> down.
    assert result["learn_logs"]["count"] == READ_ERROR_COUNT_SENTINEL
    assert result["learn_logs"]["status"] == "down"
    # The present table is not down (empty map -> unknown).
    assert result["candidates"]["status"] == "unknown"
    assert result["candidates"]["count"] == 1
