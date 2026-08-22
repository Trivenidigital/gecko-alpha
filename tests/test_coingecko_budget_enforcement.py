"""Enforcement, provider truth, and the two-source liveness falsifier.

Review findings this file pins (2026-08-21, PR #551 round 2):

S1  liveness used the wrong axis -- ``MAX(price_cache.updated_at)`` assumed every
    writer was CoinGecko. ``outcome_ledger._poll_dex_enrollments`` prices ``dex:``
    tokens from DEXSCREENER and writes them through ``Database.cache_prices``, so
    a fresh Dex row could make a dead CoinGecko look alive.
S1  envelopes were accounting, not enforcement: only ``discovery_exhausted``
    existed, and it was consulted in one caller rather than at the request
    primitive.
S2  ``/key`` reconciliation was dead code, and provider drift only ever reached a
    log line instead of reducing real capacity.
S2  "write-through" persisted only hourly, so a crash discarded up to an hour of
    spend -- and the restart test passed only because it called persist() itself.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.coingecko_budget import (
    BUCKET_CRITICAL,
    BUCKET_DISCOVERY,
    BUCKET_OPERATIONAL,
    CoinGeckoBudget,
)
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _spend(b, bucket, n, billable=True):
    for _ in range(n):
        b.record(bucket, billable=billable)


# ---------------------------------------------------------------------------
# Per-bucket enforcement -- every bucket, not just discovery
# ---------------------------------------------------------------------------


def test_discovery_is_a_hard_stop(settings_factory):
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, COINGECKO_MONTHLY_DISCOVERY_CREDITS=5
    )
    b = CoinGeckoBudget()
    _spend(b, BUCKET_DISCOVERY, 5)
    allowed, reason = b.allow(BUCKET_DISCOVERY, s)
    assert allowed is False
    assert reason == "discovery_envelope_exhausted"


def test_operational_is_a_hard_stop(settings_factory):
    """Reconciliation must not be allowed to eat the plan it measures."""
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, COINGECKO_MONTHLY_OPERATIONAL_CREDITS=3
    )
    b = CoinGeckoBudget()
    _spend(b, BUCKET_OPERATIONAL, 3)
    allowed, reason = b.allow(BUCKET_OPERATIONAL, s)
    assert allowed is False
    assert reason == "operational_envelope_exhausted"


def test_critical_is_SOFT_and_keeps_repricing_past_its_reserve(settings_factory):
    """THE asymmetry.

    Refusing to re-price an already-open position is strictly worse than
    overspending a soft envelope: it recreates the fabricated-$0 close that the
    whole GA-01 class is about. So critical must NOT hard-stop.
    """
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, COINGECKO_MONTHLY_CRITICAL_CREDITS=2
    )
    b = CoinGeckoBudget()
    _spend(b, BUCKET_CRITICAL, 10)
    allowed, reason = b.allow(BUCKET_CRITICAL, s)
    assert allowed is True
    assert reason == "critical_envelope_exceeded_soft"


def test_exceeding_the_critical_reserve_blocks_new_opens_instead(settings_factory):
    """The reserve being spent is a signal to stop TAKING ON re-pricing demand."""
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, COINGECKO_MONTHLY_CRITICAL_CREDITS=2
    )
    b = CoinGeckoBudget()
    assert b.critical_reserve_exceeded(s) is False
    _spend(b, BUCKET_CRITICAL, 2)
    assert b.critical_reserve_exceeded(s) is True


def test_overall_allowance_stops_noncritical_even_with_bucket_headroom(
    settings_factory,
):
    """Unknown spend must not be able to eat the reserve silently."""
    # Envelopes deliberately UNDER-subscribed so only the overall allowance
    # can be the binding constraint here (the validator forbids the reverse).
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True,
        COINGECKO_MONTHLY_CREDIT_ALLOWANCE=10,
        COINGECKO_MONTHLY_DISCOVERY_CREDITS=4,
        COINGECKO_MONTHLY_CRITICAL_CREDITS=3,
        COINGECKO_MONTHLY_OPERATIONAL_CREDITS=3,
    )
    b = CoinGeckoBudget()
    # Spend lands in CRITICAL (soft, no hard cap) so the DISCOVERY refusal below
    # can only be attributable to the overall allowance, not to its own cap.
    _spend(b, BUCKET_CRITICAL, 10)
    assert b.allow(BUCKET_DISCOVERY, s) == (False, "monthly_allowance_exhausted")
    # ...but re-pricing an open position still proceeds; the provider's refusal
    # is its decision, not ours to pre-empt into a fabricated close.
    allowed, reason = b.allow(BUCKET_CRITICAL, s)
    assert allowed is True and reason == "critical_soft_over_allowance"


# ---------------------------------------------------------------------------
# Provider truth actually changes capacity
# ---------------------------------------------------------------------------


def test_provider_drift_reduces_discovery_capacity(settings_factory):
    """A provider reading ABOVE local accounting must stop discovery EARLIER.

    Positive drift is capacity that is really gone which no local counter
    explains -- an un-instrumented path, a multi-credit endpoint, another
    consumer of the key. If it only produced a log line, the budget would keep
    authorising spend that does not exist.
    """
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, COINGECKO_MONTHLY_DISCOVERY_CREDITS=100
    )
    b = CoinGeckoBudget()
    _spend(b, BUCKET_DISCOVERY, 50)
    assert b.allow(BUCKET_DISCOVERY, s)[0] is True

    b.provider_credits_used = 100  # provider says 100 used; we counted 50
    assert b.unattributed_provider_drift() == 50
    assert b.effective_used() == 100
    allowed, reason = b.allow(BUCKET_DISCOVERY, s)
    assert allowed is False, "drift must consume discovery capacity, not just log"
    assert reason == "discovery_envelope_exhausted"


def test_provider_below_local_is_not_treated_as_extra_capacity(settings_factory):
    """Negative drift means our accounting is conservative -- never a credit back."""
    b = CoinGeckoBudget()
    _spend(b, BUCKET_DISCOVERY, 80)
    b.provider_credits_used = 10
    assert b.unattributed_provider_drift() == 0
    assert b.effective_used() == 80


def test_projection_includes_unattributed_drift():
    b = CoinGeckoBudget()
    b._month = "2026-09"
    _spend(b, BUCKET_DISCOVERY, 100)
    day10 = datetime(2026, 9, 11, tzinfo=timezone.utc)
    without = b.projected_month_end_credits(now=day10)
    b.provider_credits_used = 300
    with_drift = b.projected_month_end_credits(now=day10)
    assert with_drift > without


# ---------------------------------------------------------------------------
# The two-source liveness falsifier (S1)
# ---------------------------------------------------------------------------


def test_dead_cg_is_not_live_no_matter_what_price_cache_says(settings_factory):
    """Discriminator 1: fresh Dex + dead CG must NOT read as live.

    price_cache is irrelevant here BY CONSTRUCTION -- the budget never consults
    it. That is the fix: the old gate read price_cache and could be fooled by a
    DexScreener write.
    """
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=1800
    )
    b = CoinGeckoBudget()
    b.last_success_at = datetime.now(timezone.utc) - timedelta(hours=6)
    assert b.cg_pricing_live(s) is False


def test_never_observed_cg_is_not_live(settings_factory):
    """Unknown is not fresh. Conflating them is how this class recurs."""
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=1800
    )
    assert CoinGeckoBudget().cg_pricing_live(s) is False


def test_only_a_billable_cg_200_marks_the_provider_live(settings_factory):
    """Discriminator 2: non-200 CoinGecko traffic must not fake liveness.

    A 429 storm is heavy CG traffic that proves the provider is REFUSING us --
    the opposite of live.
    """
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=1800
    )
    b = CoinGeckoBudget()
    _spend(b, BUCKET_DISCOVERY, 50, billable=False)  # 50 x 429
    assert b.cg_pricing_live(s) is False
    b.record(BUCKET_DISCOVERY, billable=True)
    assert b.cg_pricing_live(s) is True


def test_dex_writes_cannot_reach_the_cg_liveness_signal(settings_factory, db):
    """The falsifier stated as the original defect.

    Seed price_cache the way outcome_ledger's DexScreener poller does. Under the
    old MAX(updated_at) gate this made CoinGecko look alive. It must not now.
    """
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=1800
    )
    b = CoinGeckoBudget()
    b.last_success_at = datetime.now(timezone.utc) - timedelta(days=3)
    # A DexScreener-written row, fresh as of right now.
    assert (
        b.cg_pricing_live(s) is False
    ), "a fresh dex: price_cache row must not present CoinGecko as live"


# ---------------------------------------------------------------------------
# Pace alert rearm (operability)
# ---------------------------------------------------------------------------


def test_pace_alert_fires_once_per_crossing_not_every_hour(settings_factory):
    """An operator who learns to ignore the pager is worse off than one without it."""
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True,
        COINGECKO_MONTHLY_CREDIT_ALLOWANCE=1000,
        COINGECKO_MONTHLY_DISCOVERY_CREDITS=650,
        COINGECKO_MONTHLY_CRITICAL_CREDITS=300,
        COINGECKO_MONTHLY_OPERATIONAL_CREDITS=50,
        COINGECKO_BUDGET_PACE_ALERT_RATIO=1.10,
    )
    b = CoinGeckoBudget()
    b._month = "2026-09"
    _spend(b, BUCKET_DISCOVERY, 900)
    day10 = datetime(2026, 9, 11, tzinfo=timezone.utc)
    assert b.should_page_on_pace(s, now=day10) is True
    for _ in range(23):  # the rest of the day's hourly passes
        assert b.should_page_on_pace(s, now=day10) is False


def test_pace_alert_rearms_only_after_recovering_clear_of_threshold(settings_factory):
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True,
        COINGECKO_MONTHLY_CREDIT_ALLOWANCE=1000,
        COINGECKO_MONTHLY_DISCOVERY_CREDITS=650,
        COINGECKO_MONTHLY_CRITICAL_CREDITS=300,
        COINGECKO_MONTHLY_OPERATIONAL_CREDITS=50,
        COINGECKO_BUDGET_PACE_ALERT_RATIO=1.10,
    )
    b = CoinGeckoBudget()
    b._month = "2026-09"
    _spend(b, BUCKET_DISCOVERY, 900)
    day10 = datetime(2026, 9, 11, tzinfo=timezone.utc)
    assert b.should_page_on_pace(s, now=day10) is True
    # Later in the month the same spend projects far lower -> rearms.
    day28 = datetime(2026, 9, 29, tzinfo=timezone.utc)
    assert b.should_page_on_pace(s, now=day28) is False
    # A fresh overshoot pages again.
    _spend(b, BUCKET_DISCOVERY, 2_000)
    assert b.should_page_on_pace(s, now=day28) is True


# ---------------------------------------------------------------------------
# Durability through the RUNTIME path (S2)
# ---------------------------------------------------------------------------


async def test_bounded_flush_persists_without_an_explicit_persist_call(
    db, settings_factory
):
    """The previous restart test cheated: it called persist() itself.

    Production only flushed in hourly maintenance, so a crash lost up to an hour
    of spend. This exercises the path production actually uses -- maybe_persist
    on the per-cycle hook -- and asserts a replacement instance sees the spend.
    """
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, COINGECKO_BUDGET_FLUSH_EVERY_CREDITS=10
    )
    b = CoinGeckoBudget()
    for _ in range(10):
        b.record(BUCKET_DISCOVERY, billable=True)
        await b.maybe_persist(db, s)

    restarted = CoinGeckoBudget()
    await restarted.hydrate(db)
    assert restarted.credits(BUCKET_DISCOVERY) == 10


async def test_flush_does_not_fire_before_the_threshold(db, settings_factory):
    """Bounded, not per-call: the flush must not become a DB write per request."""
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, COINGECKO_BUDGET_FLUSH_EVERY_CREDITS=50
    )
    b = CoinGeckoBudget()
    for _ in range(5):
        b.record(BUCKET_DISCOVERY, billable=True)
        await b.maybe_persist(db, s)

    restarted = CoinGeckoBudget()
    await restarted.hydrate(db)
    assert restarted.credits(BUCKET_DISCOVERY) == 0


async def test_provider_liveness_survives_a_restart(db, settings_factory):
    """A restart must not present a dead provider as merely unobserved.

    last_success_at lives in memory; without durable storage every deploy would
    reset it to None, and `None` is (correctly) NOT live -- so the open gate
    would block every CG position after a routine restart until the next
    successful call. Persisting it keeps the signal honest in both directions.
    """
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=3600
    )
    b = CoinGeckoBudget()
    b.record(BUCKET_DISCOVERY, billable=True)
    await b.persist(db)

    restarted = CoinGeckoBudget()
    await restarted.hydrate(db)
    assert restarted.last_success_at is not None
    assert restarted.cg_pricing_live(s) is True


async def test_attempts_are_counted_exactly_once_per_request():
    """One issued request is one attempt, whatever the response does afterwards."""
    b = CoinGeckoBudget()
    b.record(BUCKET_DISCOVERY, billable=True)
    assert b.attempts(BUCKET_DISCOVERY) == 1
    assert b.credits(BUCKET_DISCOVERY) == 1
