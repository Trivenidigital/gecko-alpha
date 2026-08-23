"""Ruling E deferral watch: `E = DEFERRED_BY_ECONOMICS`.

E was deferred on economics, not correctness. As ruled (cohort-closure only, no
age-based pruning) it reclaims ~309 rows of a 511,386-row table, and the whole
table is ~232 MB of a 7 GB database — roughly 140 KB of reclaim for a cohort
registry, closure classifier, durable receipts and a byte-identical proof
harness.

A deferral is only safe while something watches for the economics changing.
These tests pin the thresholds that reopen it, and — more importantly — pin
that the probe can actually SEE a crossing, since a watch that always reports
"nothing to do" is indistinguishable from no watch at all.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _emit(db, surface, kind, days_ago, label_status="complete", n=1):
    # executemany: the threshold tests need ~231K rows (100 MB / 454 B), which
    # one-at-a-time inserts make unusably slow.
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    await db._conn.executemany(
        """INSERT INTO signal_outcome_ledger
           (kind, token_id, surface, emitted_at, label_status)
           VALUES (?, 'tok', ?, ?, ?)""",
        [(kind, surface, ts, label_status)] * n,
    )
    await db._conn.commit()


async def test_probe_reports_the_four_measures_the_ruling_names(db):
    await _emit(db, "gainers_early", "dispatch", days_ago=0.5, n=5)
    r = await db.signal_outcome_ledger_growth_probe()

    assert r["status"] == "E_DEFERRED_BY_ECONOMICS"
    for key in (
        "bytes_total",
        "growth_rows_per_day",
        "reclaimable_bytes",
        "days_to_1gb",
    ):
        assert key in r, f"probe is missing the ruling's {key}"
    assert r["rows"] == 5
    assert r["growth_rows_per_day"] == 5
    assert r["bytes_total"] > 0


async def test_a_still_emitting_cohort_is_NOT_reclaimable(db):
    """Cohort-closure, not age. An active cohort is not closed however old it is."""
    await _emit(db, "losers_contrarian", "gated_out_sample", days_ago=90, n=10)
    await _emit(db, "losers_contrarian", "gated_out_sample", days_ago=0.1, n=1)

    r = await db.signal_outcome_ledger_growth_probe()
    assert (
        r["reclaimable_rows"] == 0
    ), "an age-based rule leaked in -- a cohort still emitting is not closed"


async def test_a_dormant_fully_labelled_cohort_IS_reclaimable(db):
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=7)
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["reclaimable_rows"] == 7


async def test_a_dormant_cohort_with_pending_rows_is_NOT_reclaimable(db):
    """Pending means the label can still change, so the cohort is not closed."""
    await _emit(db, "tg_social", "gated_out_sample", days_ago=40, n=3)
    await _emit(
        db, "tg_social", "gated_out_sample", days_ago=40, label_status="pending", n=1
    )
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["reclaimable_rows"] == 0


async def test_no_reopen_at_the_measured_production_shape(db):
    """The shape that justified the deferral must NOT trip the alarm."""
    await _emit(db, "gainers_early", "dispatch", days_ago=40, n=309)
    await _emit(db, "losers_contrarian", "gated_out_sample", days_ago=0.5, n=50)
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["reopen"] is False, r


async def test_reopen_fires_on_reclaimable_bytes(db):
    """Threshold 1: ~100 MB of safely reclaimable bytes."""
    need = db._LEDGER_REOPEN_RECLAIMABLE_BYTES // db._LEDGER_BYTES_PER_ROW + 10
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=need)
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["reopen"] is True
    assert "reclaimable_bytes" in r["reopen_reasons"]


async def test_reopen_fires_on_projected_size(db):
    """Threshold 2: projected past 1 GB within 30 days."""
    per_day = db._LEDGER_SIZE_CEILING_BYTES // db._LEDGER_BYTES_PER_ROW // 20
    await _emit(db, "gainers_early", "dispatch", days_ago=0.5, n=per_day)
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["reopen"] is True
    assert "projected_size" in r["reopen_reasons"]
    assert r["days_to_1gb"] is not None and r["days_to_1gb"] <= 30


async def test_zero_growth_does_not_project_a_false_crossing(db):
    """No emissions must mean no projection, not a divide-by-zero or a 0-day ETA."""
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=3)
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["growth_rows_per_day"] == 0
    assert r["days_to_1gb"] is None
    assert "projected_size" not in r["reopen_reasons"]


async def test_the_probe_can_see_a_crossing_at_all(db):
    """Pins the watch itself.

    Every test above except two asserts `reopen is False`, which is also what a
    probe hard-wired to False would report. This asserts the same probe flips on
    the same database once the population changes.
    """
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=3)
    assert (await db.signal_outcome_ledger_growth_probe())["reopen"] is False

    need = db._LEDGER_REOPEN_RECLAIMABLE_BYTES // db._LEDGER_BYTES_PER_ROW + 10
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=need)
    assert (await db.signal_outcome_ledger_growth_probe())["reopen"] is True
