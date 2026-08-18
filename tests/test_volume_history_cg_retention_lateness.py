"""volume_history_cg retention must cover the r7d horizon PLUS labeling lateness.

The config previously documented 7d as matching "the longest reader horizon
(ledger r7d labeling + spike 7d average) — harmless duplication". That claim
omitted LABEL-PROCESSING LATENESS and was false.

`scout/outcome_ledger.py::_peak_price_in_window` computes peak7d as
MAX(volume_history_cg.price) over the CLOSED window [emitted, emitted + 7d].
The left-edge row is exactly 7.0 days old at the earliest instant the ledger row
becomes finalizable (`now >= emitted + _FINALIZE_AFTER`). Pruning at 7d leaves
zero margin, so any lateness truncates the window from the left and produces a
wrong-but-plausible peak7d instead of a NULL — a silently biased label.

Measured on prod 2026-08-18 across n=273,296 completed ledger rows, lateness
past the r7d deadline was p50 0.153d / p90 2.529d / p99 2.857d / max 2.910d, so
essentially every labeled row was already losing left-edge data.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.config import LEDGER_R7D_HORIZON_DAYS
from scout.outcome_ledger import _FINALIZE_AFTER, _HORIZONS, _peak_price_in_window
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _record_price(db: Database, coin_id: str, when: datetime, price: float):
    await db._conn.execute(
        """INSERT INTO volume_history_cg
           (coin_id, symbol, name, volume_24h, market_cap, price, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (coin_id, "AAA", "Alpha", 1.0, 1000.0, price, when.isoformat()),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# The duplicated constant must not drift
# ---------------------------------------------------------------------------


def test_config_horizon_constant_matches_the_ledger_schema_constant():
    """config.LEDGER_R7D_HORIZON_DAYS mirrors outcome_ledger._FINALIZE_AFTER.

    config.py cannot import outcome_ledger (outcome_ledger -> db -> config is
    circular), so the horizon is duplicated. This is the pin that keeps the
    duplicate honest: change _FINALIZE_AFTER and this fails rather than letting
    the retention floor silently desynchronise from the window it must cover.
    """
    assert LEDGER_R7D_HORIZON_DAYS == _FINALIZE_AFTER.days
    r7d = dict(_HORIZONS)["r7d"]
    assert LEDGER_R7D_HORIZON_DAYS == r7d.days


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def test_default_retention_covers_horizon_plus_margin(settings_factory):
    s = settings_factory()
    assert s.VOLUME_HISTORY_CG_RETENTION_DAYS >= (
        LEDGER_R7D_HORIZON_DAYS + s.LEDGER_LABEL_MAX_LATENESS_DAYS
    )


def test_old_seven_day_default_is_now_rejected(settings_factory):
    """The exact value that shipped the defect must be unrepresentable."""
    with pytest.raises(ValueError, match="VOLUME_HISTORY_CG_RETENTION_DAYS"):
        settings_factory(VOLUME_HISTORY_CG_RETENTION_DAYS=7)


def test_retention_one_day_short_of_the_floor_is_rejected(settings_factory):
    """Boundary, not just a far-off value: horizon + margin - 1 must fail."""
    with pytest.raises(ValueError, match="silently biased"):
        settings_factory(
            LEDGER_LABEL_MAX_LATENESS_DAYS=3,
            VOLUME_HISTORY_CG_RETENTION_DAYS=9,
        )


def test_retention_exactly_at_the_floor_is_accepted(settings_factory):
    s = settings_factory(
        LEDGER_LABEL_MAX_LATENESS_DAYS=3, VOLUME_HISTORY_CG_RETENTION_DAYS=10
    )
    assert s.VOLUME_HISTORY_CG_RETENTION_DAYS == 10


def test_floor_tracks_the_margin_rather_than_being_a_fixed_number(settings_factory):
    """Raising permitted lateness raises the required retention with it.

    Pins the RELATIONSHIP. A hardcoded `>= 10` would pass the tests above and
    still let a larger margin be configured against too-short retention.
    """
    with pytest.raises(ValueError):
        settings_factory(
            LEDGER_LABEL_MAX_LATENESS_DAYS=10,
            VOLUME_HISTORY_CG_RETENTION_DAYS=10,
        )
    s = settings_factory(
        LEDGER_LABEL_MAX_LATENESS_DAYS=10, VOLUME_HISTORY_CG_RETENTION_DAYS=17
    )
    assert s.VOLUME_HISTORY_CG_RETENTION_DAYS == 17


# ---------------------------------------------------------------------------
# The behaviour the floor exists to protect
# ---------------------------------------------------------------------------


async def test_peak7d_is_truncated_when_left_edge_row_is_pruned(db, settings_factory):
    """The actual harm, demonstrated: a 7d prune biases peak7d LOW, silently.

    The true peak sits early in the window. Pruning at the horizon deletes it,
    and peak7d comes back as a plausible smaller number rather than NULL — which
    is why nothing downstream ever raised.
    """
    emitted = datetime.now(timezone.utc) - timedelta(days=7, hours=4)
    await _record_price(db, "alpha", emitted + timedelta(hours=1), 100.0)  # true peak
    await _record_price(db, "alpha", emitted + timedelta(days=6), 10.0)

    full = await _peak_price_in_window(db, "alpha", emitted, emitted + _FINALIZE_AFTER)
    assert full == 100.0

    # A 7d retention: the labeler runs 4h late, so the left-edge row is gone.
    await db.prune_volume_history_cg(keep_days=7)

    truncated = await _peak_price_in_window(
        db, "alpha", emitted, emitted + _FINALIZE_AFTER
    )
    assert truncated == 10.0, "expected the silently-biased value, not NULL"
    assert truncated != full


async def test_default_retention_preserves_peak7d_at_measured_max_lateness(
    db, settings_factory
):
    """At the configured floor the same window survives the worst measured lag.

    Prod max lateness was 2.910d; the margin is 3d. The left-edge row must still
    be present when a labeler that late finally runs.
    """
    s = settings_factory()
    emitted = datetime.now(timezone.utc) - timedelta(days=7) - timedelta(days=2.91)
    await _record_price(db, "alpha", emitted + timedelta(hours=1), 100.0)
    await _record_price(db, "alpha", emitted + timedelta(days=6), 10.0)

    await db.prune_volume_history_cg(keep_days=s.VOLUME_HISTORY_CG_RETENTION_DAYS)

    peak = await _peak_price_in_window(db, "alpha", emitted, emitted + _FINALIZE_AFTER)
    assert peak == 100.0


# ---------------------------------------------------------------------------
# ONE owner
# ---------------------------------------------------------------------------


async def test_detector_write_path_no_longer_prunes(db):
    """record_volume must not delete anything.

    The detector's hardcoded `-7 days` DELETE is removed: leaving a second
    implementation of this table's retention is how the floor drifts back to 7
    regardless of what the validator says, and it only ran when
    VOLUME_SPIKE_ENABLED was on, so retention silently depended on a feature
    flag. Ownership is Database.prune_volume_history_cg alone.
    """
    from scout.spikes.detector import record_volume

    stale = datetime.now(timezone.utc) - timedelta(days=30)
    await _record_price(db, "ancient", stale, 42.0)

    await record_volume(
        db,
        [{"id": "fresh", "symbol": "f", "name": "Fresh", "total_volume": 1000.0,
          "market_cap": 5000.0, "current_price": 1.0}],
    )

    async with db._conn.execute(
        "SELECT coin_id FROM volume_history_cg ORDER BY coin_id"
    ) as cur:
        rows = await cur.fetchall()
    assert {r["coin_id"] for r in rows} == {"ancient", "fresh"}, (
        "record_volume deleted rows — the duplicate prune is back"
    )


def test_detector_module_contains_no_volume_history_delete():
    """Source-text guard against the DELETE being reintroduced verbatim.

    Paired with the behavioural test above, not a substitute for it: a source
    grep cannot prove execution, and the behavioural test cannot prove a
    differently-shaped DELETE was not added elsewhere in the module.
    """
    from pathlib import Path

    import scout.spikes.detector as detector_mod

    src = Path(detector_mod.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "DELETE FROM volume_history_cg" not in code
