"""CoinGecko MONTHLY-CREDIT governor.

2026-08-21 root cause: a resource-model defect. The system modeled calls/MINUTE
(coingecko_limiter) while the hard production constraint was calls/MONTH.
CoinGecko enforces those as independent limits and only the second has a wall
you cannot back off from -- the Basic plan hit 100.0% of 100,000 credits with 11
days to the reset.

The single most important invariant here is that ATTEMPTS ARE NOT CREDITS.
CoinGecko deducts a monthly credit on HTTP 200 only; 4xx/5xx do not deduct one
though they still consume the per-minute rate limit. During the incident 429s
ran at ~40/hr while billing nothing, so a naive one-counter governor would have
massively over-reported and throttled the wrong axis.
"""

from datetime import datetime, timezone

import pytest

from scout.coingecko_budget import (
    BUCKET_CRITICAL,
    BUCKET_DISCOVERY,
    CoinGeckoBudget,
    billing_month,
)
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# attempts vs credits
# ---------------------------------------------------------------------------


def test_only_billable_calls_count_as_credits():
    """THE distinction the whole governor rests on."""
    b = CoinGeckoBudget()
    b.record(BUCKET_DISCOVERY, billable=True)
    b.record(BUCKET_DISCOVERY, billable=False)  # e.g. a 429
    b.record(BUCKET_DISCOVERY, billable=False)  # e.g. a 500
    assert b.attempts(BUCKET_DISCOVERY) == 3
    assert b.credits(BUCKET_DISCOVERY) == 1


def test_a_429_storm_bills_nothing():
    """The incident's own shape: heavy traffic, zero credits consumed.

    A single counter would have read 500 here and throttled on phantom spend.
    """
    b = CoinGeckoBudget()
    for _ in range(500):
        b.record(BUCKET_DISCOVERY, billable=False)
    assert b.total_attempts() == 500
    assert b.total_credits() == 0


def test_unknown_bucket_is_rejected_not_silently_accumulated():
    b = CoinGeckoBudget()
    b.record("not_a_bucket", billable=True)
    assert b.total_credits() == 0
    assert b.total_attempts() == 0


# ---------------------------------------------------------------------------
# envelopes
# ---------------------------------------------------------------------------


def test_discovery_stops_at_its_envelope(settings_factory):
    s = settings_factory(COINGECKO_MONTHLY_DISCOVERY_CREDITS=10)
    b = CoinGeckoBudget()
    for _ in range(9):
        b.record(BUCKET_DISCOVERY, billable=True)
    assert b.discovery_exhausted(s) is False
    b.record(BUCKET_DISCOVERY, billable=True)
    assert b.discovery_exhausted(s) is True


def test_critical_spend_does_not_exhaust_discovery(settings_factory):
    """No spillover in EITHER direction: the reserve is not discovery's budget.

    The reserve exists so held positions stay re-priceable; an unpriceable open
    position is the GA-01 failure class.
    """
    s = settings_factory(COINGECKO_MONTHLY_DISCOVERY_CREDITS=10)
    b = CoinGeckoBudget()
    for _ in range(50):
        b.record(BUCKET_CRITICAL, billable=True)
    assert b.discovery_exhausted(s) is False


def test_exhausted_discovery_leaves_the_critical_reserve_spendable(settings_factory):
    s = settings_factory(COINGECKO_MONTHLY_DISCOVERY_CREDITS=2)
    b = CoinGeckoBudget()
    b.record(BUCKET_DISCOVERY, billable=True)
    b.record(BUCKET_DISCOVERY, billable=True)
    assert b.discovery_exhausted(s) is True
    b.record(BUCKET_CRITICAL, billable=True)
    assert b.credits(BUCKET_CRITICAL) == 1


def test_envelopes_may_not_oversubscribe_the_allowance(settings_factory):
    with pytest.raises(ValueError, match="exceeds"):
        settings_factory(
            COINGECKO_MONTHLY_CREDIT_ALLOWANCE=100_000,
            COINGECKO_MONTHLY_DISCOVERY_CREDITS=80_000,
            COINGECKO_MONTHLY_CRITICAL_CREDITS=30_000,
            COINGECKO_MONTHLY_OPERATIONAL_CREDITS=5_000,
        )


def test_shipped_envelope_profile_matches_the_ruling(settings_factory):
    s = settings_factory()
    assert s.COINGECKO_MONTHLY_DISCOVERY_CREDITS == 65_000
    assert s.COINGECKO_MONTHLY_CRITICAL_CREDITS == 30_000
    assert s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS == 5_000
    total = (
        s.COINGECKO_MONTHLY_DISCOVERY_CREDITS
        + s.COINGECKO_MONTHLY_CRITICAL_CREDITS
        + s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS
    )
    assert total == s.COINGECKO_MONTHLY_CREDIT_ALLOWANCE == 100_000


# ---------------------------------------------------------------------------
# pacing
# ---------------------------------------------------------------------------


def test_projection_answers_where_this_pace_lands_not_where_we_are():
    """60% on day 3 is an emergency; 60% on day 27 is fine.

    Absolute 50/75/90% marks cannot tell those apart. That is why the alarm is
    a projection rather than a threshold on spend-to-date.
    """
    b = CoinGeckoBudget()
    b._month = "2026-09"
    for _ in range(6_000):
        b.record(
            BUCKET_DISCOVERY,
            billable=True,
            now=datetime(2026, 9, 4, tzinfo=timezone.utc),
        )
    day3 = datetime(2026, 9, 4, tzinfo=timezone.utc)
    day27 = datetime(2026, 9, 28, tzinfo=timezone.utc)
    early = b.projected_month_end_credits(now=day3)
    late = b.projected_month_end_credits(now=day27)
    assert early is not None and late is not None
    assert early > 50_000, "same spend early must project far higher"
    assert late < 10_000
    assert early > late


def test_projection_is_none_before_enough_of_the_month_has_elapsed():
    b = CoinGeckoBudget()
    b._month = "2026-09"
    just_after_reset = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    b.record(BUCKET_DISCOVERY, billable=True, now=just_after_reset)
    assert b.projected_month_end_credits(now=just_after_reset) is None


# ---------------------------------------------------------------------------
# billing period
# ---------------------------------------------------------------------------


def test_billing_month_key_is_utc_calendar_month():
    assert (
        billing_month(datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)) == "2026-08"
    )
    assert billing_month(datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)) == "2026-09"


def test_counters_reset_when_the_billing_month_rolls():
    """Credits replenish on the 1st, so spend must not carry across."""
    b = CoinGeckoBudget()
    b._month = "2026-08"
    for _ in range(100):
        b.record(
            BUCKET_DISCOVERY,
            billable=True,
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
    assert b.total_credits() == 100
    b.record(
        BUCKET_DISCOVERY,
        billable=True,
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )
    assert b.month == "2026-09"
    assert b.total_credits() == 1, "September must not inherit August spend"


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------


async def test_counters_survive_a_restart(db):
    """A budget a service bounce clears is not a budget.

    Module counters reset on restart -- that is why the ledger is durable.
    """
    b1 = CoinGeckoBudget()
    for _ in range(7):
        b1.record(BUCKET_DISCOVERY, billable=True)
    b1.record(BUCKET_CRITICAL, billable=True)
    b1.record(BUCKET_DISCOVERY, billable=False)
    await b1.persist(db)

    b2 = CoinGeckoBudget()  # simulates the process restarting
    assert b2.total_credits() == 0
    await b2.hydrate(db)
    assert b2.credits(BUCKET_DISCOVERY) == 7
    assert b2.credits(BUCKET_CRITICAL) == 1
    assert b2.attempts(BUCKET_DISCOVERY) == 8


async def test_persist_is_idempotent_and_not_additive(db):
    """Write-through must SET, not increment -- else every flush inflates spend."""
    b = CoinGeckoBudget()
    for _ in range(5):
        b.record(BUCKET_DISCOVERY, billable=True)
    await b.persist(db)
    b._dirty = True
    await b.persist(db)
    b._dirty = True
    await b.persist(db)

    fresh = CoinGeckoBudget()
    await fresh.hydrate(db)
    assert fresh.credits(BUCKET_DISCOVERY) == 5


async def test_hydrate_only_loads_the_current_billing_month(db):
    b = CoinGeckoBudget()
    b._month = "2026-07"
    for _ in range(42):
        b.record(
            BUCKET_DISCOVERY,
            billable=True,
            now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
    await b.persist(db)

    fresh = CoinGeckoBudget()
    fresh._month = "2026-09"
    await fresh.hydrate(db, now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert fresh.total_credits() == 0, "last month must not load into this month"
