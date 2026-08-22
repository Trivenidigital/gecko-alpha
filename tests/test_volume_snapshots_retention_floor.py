"""Ruling B: volume_snapshots retention 21 -> 14, with a hard floor at 14.

WHY THIS IS SAFE WHERE RULING A WAS NOT
---------------------------------------
A (score_history 21->14) was FALSIFIED on prod: 762 of 3347 tokens (22.8%)
changed their last-3 observation set at 14d, because A's binding reader
``get_recent_scores(contract_address, limit=3)`` has NO date window -- so
retention itself silently supplied the historical boundary.

volume_snapshots has exactly two readers and BOTH are date-bounded at <= 14d:
  * ``get_vol_7d_avg``     -- ``scanned_at >= now-7d``
  * ``get_volume_history`` -- ``scanned_at >= now-days``, and the only
    production caller (``scout/secondwave/detector.py``) passes
    ``days=SECONDWAVE_COOLDOWN_MAX_DAYS`` (14).

So rows older than 14 days are outside both windows by construction, and the
prod replay agreed exactly (1668 vol_7d-eligible contracts before and after).
The tests below pin that reasoning rather than restating it: each one deletes
the >14d rows and asserts the reader's answer is unchanged.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _s(**over):
    base = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="t", ANTHROPIC_API_KEY="t")
    base.update(over)
    return Settings(**base)


async def _snap(db, contract, days_ago, volume):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) "
        "VALUES (?, ?, ?)",
        (contract, volume, ts),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# The shipped value + the floor
# ---------------------------------------------------------------------------


def test_shipped_retention_is_14():
    assert _s().VOLUME_SNAPSHOTS_RETENTION_DAYS == 14


def test_retention_below_the_ruling_floor_is_rejected():
    """13 is refused. WHICH validator refuses it is deliberately not asserted.

    With the shipped cooldown of 14 the pre-existing relative check catches
    this first and gives the more actionable message; the floor is what
    catches the case the relative check cannot (below).
    """
    with pytest.raises(ValueError):
        _s(VOLUME_SNAPSHOTS_RETENTION_DAYS=13)


def test_the_floor_is_absolute_not_merely_relative_to_secondwave():
    """THE discriminating case for the new validator.

    The pre-existing validator only pins retention >= SECONDWAVE_COOLDOWN_MAX_DAYS.
    Lowering that knob would drag the permitted floor down with it and re-open
    the very deletion the ruling bounded -- silently, because the relative
    check still passes. This asserts the absolute floor survives that.
    """
    with pytest.raises(ValueError, match="hard floor"):
        _s(SECONDWAVE_COOLDOWN_MAX_DAYS=7, VOLUME_SNAPSHOTS_RETENTION_DAYS=7)


def test_retention_must_still_cover_a_raised_secondwave_window():
    """The relative check must keep biting ABOVE the floor -- 14 is not a cap."""
    # SCORE_HISTORY_RETENTION_DAYS is covered by the same relative validator,
    # so it has to clear the raised window too or it masks what is under test.
    with pytest.raises(ValueError, match="VOLUME_SNAPSHOTS_RETENTION_DAYS"):
        _s(
            SECONDWAVE_COOLDOWN_MAX_DAYS=30,
            SCORE_HISTORY_RETENTION_DAYS=30,
            VOLUME_SNAPSHOTS_RETENTION_DAYS=21,
        )
    ok = _s(
        SECONDWAVE_COOLDOWN_MAX_DAYS=30,
        SCORE_HISTORY_RETENTION_DAYS=30,
        VOLUME_SNAPSHOTS_RETENTION_DAYS=30,
    )
    assert ok.VOLUME_SNAPSHOTS_RETENTION_DAYS == 30


# ---------------------------------------------------------------------------
# Reader-equivalence: the A-class falsifier, run per reader
# ---------------------------------------------------------------------------


async def test_vol_7d_avg_is_unchanged_when_rows_older_than_14d_disappear(db):
    for d in (1.0, 2.0, 3.0):
        await _snap(db, "0xa", d, 100.0)
    for d in (15.0, 18.0, 20.5):
        await _snap(db, "0xa", d, 999_999.0)

    before = await db.get_vol_7d_avg("0xa")
    assert before == pytest.approx(
        100.0
    ), "fixture error: the >14d rows leaked into the 7d window"

    deleted = await db.prune_volume_snapshots(keep_days=14)
    assert deleted == 3, f"the falsifier deleted {deleted} rows, expected 3"
    assert await db.get_vol_7d_avg("0xa") == before


async def test_get_volume_history_at_14d_is_unchanged(db):
    for d in (0.5, 5.0, 13.5):
        await _snap(db, "0xb", d, 10.0)
    for d in (14.5, 19.0):
        await _snap(db, "0xb", d, 20.0)

    before = await db.get_volume_history("0xb", days=14)
    assert len(before) == 3, f"expected 3 in-window rows, got {len(before)}"

    await db.prune_volume_snapshots(keep_days=14)
    assert await db.get_volume_history("0xb", days=14) == before


async def test_the_falsifier_can_actually_detect_a_loss(db):
    """Pins the two tests above.

    Both assert "nothing changed" -- which is exactly what they would report if
    the fixture never created deletable rows, or if prune were a no-op. Prove
    the setup CAN observe a loss by pruning inside the reader's window.
    """
    for d in (1.0, 2.0, 3.0):
        await _snap(db, "0xc", d, 100.0)
    before = await db.get_vol_7d_avg("0xc")
    assert before is not None

    await db.prune_volume_snapshots(keep_days=2)
    after = await db.get_vol_7d_avg("0xc")
    assert after != before, "pruning inside the 7d window changed nothing -- the "
    "falsifier is blind and the two equivalence tests above prove nothing"


async def test_boundary_row_is_not_deleted_while_a_reader_still_wants_it(db):
    """The prune boundary and the reader boundary must abut without a gap.

    Prune removes ``scanned_at <= now-14d``; the reader takes
    ``scanned_at >= now-14d``. A row just inside 14 days must survive AND remain
    visible -- if the two used different clocks or comparison styles, a row
    could fall through the crack and bias the average low without erroring.
    """
    await _snap(db, "0xd", 13.98, 42.0)
    await db.prune_volume_snapshots(keep_days=14)
    assert await db.get_volume_history("0xd", days=14) == [42.0]
