"""Tests for scout.trading.params (Tier 1a)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.trading.params import (
    DEFAULT_SIGNAL_TYPES,
    UnknownSignalType,
    clear_cache_for_tests,
    get_params,
    params_for_signal,
)


@pytest.fixture(autouse=True)
def _wipe_cache():
    clear_cache_for_tests()
    yield
    clear_cache_for_tests()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def test_migration_seeds_one_row_per_signal_type(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    cur = await db._conn.execute(
        "SELECT signal_type FROM signal_params ORDER BY signal_type"
    )
    seeded = {r[0] for r in await cur.fetchall()}
    assert seeded == DEFAULT_SIGNAL_TYPES

    # Cutover marker present
    cur = await db._conn.execute(
        "SELECT name FROM paper_migrations WHERE name='signal_params_v1'"
    )
    assert (await cur.fetchone()) is not None

    await db.close()


async def test_migration_idempotent(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # second run should not raise or duplicate rows
    await db._migrate_signal_params_schema()
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_params")
    assert (await cur.fetchone())[0] == len(DEFAULT_SIGNAL_TYPES)
    await db.close()


# ---------------------------------------------------------------------------
# get_params (strict)
# ---------------------------------------------------------------------------


async def test_get_params_returns_settings_when_flag_off(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(SIGNAL_PARAMS_ENABLED=False)
    sp = await get_params(db, "gainers_early", settings)
    assert sp.source == "settings"
    assert sp.sl_pct == settings.PAPER_SL_PCT
    await db.close()


async def test_get_params_reads_table_when_flag_on(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # mutate the seeded row so we can tell table from settings
    await db._conn.execute(
        "UPDATE signal_params SET sl_pct = 99.9 WHERE signal_type = 'gainers_early'"
    )
    await db._conn.commit()

    settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    sp = await get_params(db, "gainers_early", settings)
    assert sp.source == "table"
    assert sp.sl_pct == 99.9
    await db.close()


async def test_get_params_missing_row_falls_back_to_settings(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "DELETE FROM signal_params WHERE signal_type = 'gainers_early'"
    )
    await db._conn.commit()

    settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    sp = await get_params(db, "gainers_early", settings)
    # Falls back to Settings, logged as missing-row internally
    assert sp.source == "settings"
    assert sp.sl_pct == settings.PAPER_SL_PCT
    await db.close()


async def test_get_params_unknown_signal_type_raises(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    with pytest.raises(UnknownSignalType):
        await get_params(db, "totally_made_up", settings)
    await db.close()


# ---------------------------------------------------------------------------
# params_for_signal (lenient — used by evaluator hot path)
# ---------------------------------------------------------------------------


async def test_params_for_signal_falls_back_for_legacy_types(
    tmp_path, settings_factory
):
    """Evaluator must keep processing legacy rows like 'momentum_7d'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
    sp = await params_for_signal(db, "momentum_7d", settings)
    assert sp.source == "settings"
    assert sp.enabled is True
    await db.close()
