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

import math
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


#: A corrupted _LEDGER_BYTES_PER_ROW makes the derived row counts explode --
#: at 1 byte/row the reopen threshold needs 104,857,600 rows. Without a bound
#: the suite HANGS instead of failing, which is the worst of both worlds: no
#: signal and no result. Fail fast and name the likely cause.
_MAX_FIXTURE_ROWS = 1_000_000


async def _emit(db, surface, kind, days_ago, label_status="complete", n=1):
    # executemany: the threshold tests need ~231K rows (100 MB / 454 B), which
    # one-at-a-time inserts make unusably slow.
    assert n <= _MAX_FIXTURE_ROWS, (
        f"fixture asked for {n} rows (limit {_MAX_FIXTURE_ROWS}). If this came "
        "from a threshold divided by _LEDGER_BYTES_PER_ROW, that constant is "
        "probably wrong."
    )
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    await db._conn.executemany(
        """INSERT INTO signal_outcome_ledger
           (kind, token_id, surface, emitted_at, label_status)
           VALUES (?, 'tok', ?, ?, ?)""",
        [(kind, surface, ts, label_status)] * n,
    )
    await db._conn.commit()


def test_the_bytes_per_row_constant_is_pinned_to_its_measurement():
    """The constant gets its own assertion, derived independently.

    Every threshold test below computes its row count AS
    `_LEDGER_REOPEN_RECLAIMABLE_BYTES // _LEDGER_BYTES_PER_ROW`, so they stay
    self-consistent under ANY corruption of the constant and cannot detect a
    wrong value -- they would simply insert a different number of rows and still
    pass. That is not hypothetical: a stray edit set it to 1 during review,
    which would have made every byte figure read 454x low, put
    `reclaimable_bytes` permanently below the reopen threshold, and projected
    ~4.7 million days to 1 GB, all while the hourly log looked green.

    Measured 2026-08-23 via dbstat on production:
        table   171,900,928 B
        indexes  59,994,112 B
        rows        511,386
        -> 231,895,040 / 511,386 == 453.42 B/row

    The constant rounds UP to 454, and the direction is deliberate: an
    overestimate makes the probe report more bytes than exist, so the reopen
    threshold fires slightly EARLY. Rounding down would make it fire late, and
    late is the direction that lets a deferral quietly stop being safe.
    """
    measured_bytes = 171_900_928 + 59_994_112
    measured_rows = 511_386
    exact = measured_bytes / measured_rows
    assert Database._LEDGER_BYTES_PER_ROW == math.ceil(exact)
    assert Database._LEDGER_BYTES_PER_ROW == 454
    assert (
        Database._LEDGER_BYTES_PER_ROW >= exact
    ), "rounding DOWN makes the reopen threshold fire late"


def test_thresholds_are_sane_relative_to_each_other():
    """A corrupted constant also shows up as an absurd row count."""
    rows_for_reopen = (
        Database._LEDGER_REOPEN_RECLAIMABLE_BYTES // Database._LEDGER_BYTES_PER_ROW
    )
    assert 100_000 < rows_for_reopen < 1_000_000, (
        f"{rows_for_reopen} rows to reach the reclaimable threshold is not a "
        "plausible number for a ~232 MB table -- check _LEDGER_BYTES_PER_ROW"
    )


async def test_probe_reports_the_four_measures_the_ruling_names(db):
    await _emit(db, "gainers_early", "dispatch", days_ago=0.5, n=5)
    r = await db.signal_outcome_ledger_growth_probe()

    assert r["status"] == "E_DEFERRED_BY_ECONOMICS"
    for key in (
        "bytes_total_est",
        "growth_rows_per_day",
        "reclaimable_bytes_est",
        "days_to_1gb",
    ):
        assert key in r, f"probe is missing the ruling's {key}"
    assert r["rows"] == 5
    assert r["growth_rows_per_day"] == 5
    assert r["bytes_total_est"] > 0


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
    """No emissions must mean no projection, not a divide-by-zero or a 0-day ETA.

    Scoped deliberately to a SMALL table. An earlier version of this test made
    the same assertions with no size qualifier, which encoded "zero growth =>
    never alarm" as intended behaviour -- and that is exactly what hid the
    over-ceiling defect below. Zero growth suppresses the PROJECTION only; it
    must never suppress the fact that the ceiling has already been crossed.
    """
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=3)
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["growth_rows_per_day"] == 0
    assert r["days_to_1gb"] is None
    assert "projected_size" not in r["reopen_reasons"]
    assert r["bytes_total_est"] < Database._LEDGER_SIZE_CEILING_BYTES


async def test_over_the_ceiling_reopens_even_with_ZERO_growth(db, monkeypatch):
    """THE defect the zero-growth test was hiding.

    `reopen` on size fired only through the projection, which is gated on
    growth > 0. So a table that crossed 1 GB and then stopped growing -- the
    ledger kill switch flipped off, or any outage longer than 24h -- reported
    `reopen: False` with a green hourly line. The watch went quiet at exactly
    the moment the thing it watches for had already happened.
    """
    # Lower the CEILING rather than inflating the table: crossing the real 1 GB
    # needs ~2.37M rows, and a fixture that large tests the machine, not the
    # logic. The condition under test is `bytes_total >= ceiling`, which does
    # not care which side moved.
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=1000)
    # EXACTLY equal, not one byte under: the condition is `>=`, and a fixture
    # that sets the ceiling BELOW the table passes under `>` too, leaving the
    # boundary itself unpinned.
    monkeypatch.setattr(Database, "_LEDGER_SIZE_CEILING_BYTES", 1000 * 454)

    r = await db.signal_outcome_ledger_growth_probe()
    assert (
        r["growth_rows_per_day"] == 0
    ), "fixture grew; the zero-growth path is untested"
    assert r["bytes_total_est"] >= Database._LEDGER_SIZE_CEILING_BYTES
    assert r["reopen"] is True, "over the ceiling and silent"
    assert "over_ceiling" in r["reopen_reasons"]


async def test_negative_headroom_with_growth_still_reopens(db, monkeypatch):
    """Pins the max(0.0, ...) clamp, which was wholly untested."""
    # The ceiling must be far enough below the table that an UNCLAMPED value is
    # unambiguously negative. My first version set it one byte below, so the
    # unclamped result was -2.2e-6 and `round(..., 1)` gave -0.0 -- which
    # compares EQUAL to 0.0, and the mutant deleting the clamp survived.
    await _emit(db, "gainers_early", "dispatch", days_ago=0.5, n=1000)
    monkeypatch.setattr(Database, "_LEDGER_SIZE_CEILING_BYTES", 1000)

    r = await db.signal_outcome_ledger_growth_probe()
    assert r["days_to_1gb"] == 0.0, "negative headroom did not clamp to 0"
    assert r["reopen"] is True


async def test_growth_is_a_RATE_not_a_running_total(db):
    """`growth_rows_per_day` must mean per DAY.

    Widening the window to 30 days survived every other test, so nothing pinned
    the unit -- and the unit is what the whole projection is denominated in.
    """
    await _emit(db, "gainers_early", "dispatch", days_ago=0.5, n=10)
    # 1.5 days: just OUTSIDE a one-day window. Without it any width between 0.5
    # and 5 days passes -- a 2-day window survived mutation.
    await _emit(db, "gainers_early", "dispatch", days_ago=1.5, n=77)
    await _emit(db, "gainers_early", "dispatch", days_ago=5, n=500)
    r = await db.signal_outcome_ledger_growth_probe()
    assert (
        r["growth_rows_per_day"] == 10
    ), "rows outside the 24h window were counted as today's growth"


async def test_kind_is_part_of_the_cohort_key(db):
    """`GROUP BY surface, kind` -- dropping `kind` survived every test.

    Every other fixture uses one kind per surface, so the second axis was never
    exercised. A dormant dispatch cohort must not be dragged out of closure by
    a still-active gated_out_sample cohort on the same surface, nor vice versa.
    """
    await _emit(db, "volume_spike", "dispatch", days_ago=40, n=7)
    await _emit(db, "volume_spike", "gated_out_sample", days_ago=0.1, n=3)

    r = await db.signal_outcome_ledger_growth_probe()
    assert r["reclaimable_rows"] == 7, (
        "the still-active gated_out_sample cohort suppressed the dormant "
        "dispatch cohort -- kind is not being grouped"
    )


async def test_partial_rows_are_NOT_closed(db):
    """`partial` is still in the labeler's own queue.

    outcome_ledger selects `WHERE label_status IN ('pending','partial')`, so a
    partial row is work the system has not finished. Declaring it safely
    prunable would let the prune race the labeler whenever the labeler is
    disabled or backlogged.
    """
    await _emit(db, "tg_social", "gated_out_sample", days_ago=40, n=5)
    await _emit(
        db, "tg_social", "gated_out_sample", days_ago=40, label_status="partial", n=2
    )
    r = await db.signal_outcome_ledger_growth_probe()
    assert r["reclaimable_rows"] == 0


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


# ---------------------------------------------------------------------------
# The invariant the sargable rewrite depends on
# ---------------------------------------------------------------------------


async def test_a_caller_supplied_emitted_at_is_normalised_not_stored_verbatim(db):
    """The growth query compares `emitted_at` LEXICOGRAPHICALLY.

    That is only equivalent to a chronological compare while every stored value
    is canonical isoformat-T/UTC. `record_emission_with_status` exposes
    `emitted_at` for backfills, and backfills source timestamps from other
    tables and journals -- a space separator (0x20) or a negative offset both
    sort BELOW 'T' (0x54), so a row chronologically inside the 24h window falls
    lexicographically below the threshold and is silently dropped. Growth then
    undercounts, `days_to_1gb` inflates, and the probe UNDER-alarms.

    Note the timestamps below share a DATE. Vary the date instead and the date
    field dominates before the comparison ever reaches the separator, which is
    how this hides.
    """
    from scout.outcome_ledger import _normalise_emitted_at

    space = _normalise_emitted_at("2026-08-22 06:54:00")
    offset = _normalise_emitted_at("2026-08-22T01:54:00-05:00")
    canonical = _normalise_emitted_at("2026-08-22T06:54:00+00:00")

    assert space == canonical, "space-separated value was not normalised"
    assert offset == canonical, "non-UTC offset was not normalised to UTC"
    assert "T" in canonical and canonical.endswith("+00:00")

    # All three now sort identically against a text threshold.
    threshold = "2026-08-22T05:54:00"
    for value in (space, offset, canonical):
        assert value >= threshold


def test_an_unparseable_emitted_at_is_refused_rather_than_guessed():
    """A timestamp we cannot place in time must not enter the durable record."""
    from scout.outcome_ledger import _normalise_emitted_at

    with pytest.raises(ValueError, match="ISO-8601"):
        _normalise_emitted_at("last tuesday")


def test_a_naive_emitted_at_is_treated_as_UTC():
    from scout.outcome_ledger import _normalise_emitted_at

    assert _normalise_emitted_at("2026-08-22T06:54:00") == "2026-08-22T06:54:00+00:00"


async def test_the_PUBLIC_API_normalises_a_backfill_timestamp(db, settings_factory):
    """Drives `record_emission_with_status`, not the helper.

    Testing the normaliser directly proves the function works; it does NOT prove
    the writer calls it. Removing the call from the writer left every other test
    in this file green, because they all reached the helper directly -- the
    fixture could not see the defect it existed to rule out.

    This asserts what is actually IN the column after a backfill-shaped call.
    """
    from scout.outcome_ledger import record_emission_with_status

    s = settings_factory(LEDGER_ENABLED=True)
    result = await record_emission_with_status(
        db,
        s,
        kind="alert",
        token_id="pepe",
        surface="gainers_early",
        emitted_at="2026-08-22 06:54:00",  # space-separated, as a backfill gives
    )
    assert (
        result is not None
    ), "the emission did not record; fixture is not exercising it"

    cur = await db._conn.execute(
        "SELECT emitted_at FROM signal_outcome_ledger WHERE token_id='pepe'"
    )
    stored = (await cur.fetchone())[0]
    assert (
        stored == "2026-08-22T06:54:00+00:00"
    ), f"stored {stored!r} verbatim -- a lexicographic compare will mis-sort it"
    assert stored >= "2026-08-22T05:54:00", "row fell below a threshold it is inside"
