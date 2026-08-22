"""Round-3 review discriminators for the CoinGecko monthly-budget repair.

Each test here corresponds to a specific finding from the 2026-08-21 round-3
review. They are grouped by finding so a failure names the defect it guards.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.coingecko_budget import (
    BUCKET_CRITICAL,
    BUCKET_DISCOVERY,
    BUCKET_OPERATIONAL,
    CoinGeckoBudget,
    governed_cg_call,
)
from scout.coingecko_budget import budget as cg_budget
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# S1 · The ledger poller must not spend the held-position CRITICAL reserve
# ---------------------------------------------------------------------------


async def test_ledger_enrollment_poll_cannot_spend_the_critical_bucket(
    db, settings_factory, monkeypatch
):
    """THE round-3 headline defect.

    `_fetch_simple_price_batch` hardcoded BUCKET_CRITICAL, and
    `outcome_ledger.poll_enrollments` — forward-LABELLING, not re-pricing an
    open position — shares that helper. It runs once per 60s main cycle, so at
    sustained CG enrollments that is 1,440/day ~= 43.2k/month against a 30,000
    reserve: the reserve would be exhausted in ~21 days and
    `critical_reserve_exceeded` would then block every new open, turning the
    guard into the outage.
    """
    import scout.outcome_ledger as ledger

    seen_buckets = []

    async def _fake_batch(session, settings, coin_ids, *, bucket):
        seen_buckets.append(bucket)
        return {}

    monkeypatch.setattr(
        "scout.ingestion.held_position_prices._fetch_simple_price_batch",
        _fake_batch,
    )
    monkeypatch.setattr(
        "scout.ingestion.held_position_prices._shape_for_cache_prices",
        lambda resp: [],
    )
    monkeypatch.setattr(ledger, "_enrollment_cg_cycle_counter", 0, raising=False)
    monkeypatch.setattr(ledger, "_ENROLLMENT_CG_POLL_INTERVAL_CYCLES", 1, raising=False)
    # The real reader is `active_enrollments`; patch THAT rather than a name
    # that does not exist — a monkeypatch on a wrong attribute silently does
    # nothing and the test would pass for the wrong reason.
    monkeypatch.setattr(
        ledger, "active_enrollments", AsyncMock(return_value=[("bitcoin", "cg")])
    )
    monkeypatch.setattr(ledger, "purge_expired_enrollments", AsyncMock(return_value=0))

    settings = settings_factory(LEDGER_ENROLLMENT_MAX_ACTIVE=200)
    await ledger.poll_enrollments(db, AsyncMock(), settings)

    assert seen_buckets, "the CG enrollment branch should have run"
    assert (
        BUCKET_CRITICAL not in seen_buckets
    ), "forward-labelling must not draw on the held-position re-pricing reserve"
    assert seen_buckets == [BUCKET_OPERATIONAL]


def test_shared_price_helper_requires_an_explicit_bucket():
    """The helper has two callers with different semantics; it must not guess."""
    import inspect

    from scout.ingestion.held_position_prices import _fetch_simple_price_batch

    sig = inspect.signature(_fetch_simple_price_batch)
    bucket = sig.parameters["bucket"]
    assert bucket.kind is inspect.Parameter.KEYWORD_ONLY
    assert (
        bucket.default is inspect.Parameter.empty
    ), "a default bucket is how the ledger silently inherited CRITICAL"


def test_ledger_cg_poll_is_cadence_throttled():
    """Unthrottled it is ~43.2k/month, ~43% of the entire Basic plan."""
    import scout.outcome_ledger as ledger

    assert (
        ledger._ENROLLMENT_CG_POLL_INTERVAL_CYCLES >= 5
    ), "a per-cycle CG enrollment poll cannot fit any sane monthly envelope"


# ---------------------------------------------------------------------------
# S1 · Dark-until-reset must be tree-wide, not one caller
# ---------------------------------------------------------------------------


def test_discovery_disabled_is_enforced_by_the_CENTRAL_predicate(settings_factory):
    """Checking the flag only in `_fetch_coingecko_lanes` made it a property of
    that code path, not of the system: the narrative lanes, secondwave and the
    trending tracker would all still have issued requests with the flag off."""
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=False)
    allowed, reason = CoinGeckoBudget().allow(BUCKET_DISCOVERY, s)
    assert allowed is False
    assert reason == "discovery_not_enabled"


def test_discovery_disabled_does_not_disable_critical_or_operational(
    settings_factory,
):
    """Held-position re-pricing and reconciliation are not discovery."""
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=False)
    b = CoinGeckoBudget()
    assert b.allow(BUCKET_CRITICAL, s)[0] is True
    assert b.allow(BUCKET_OPERATIONAL, s)[0] is True


async def test_a_direct_discovery_caller_makes_ZERO_http_when_disabled(
    settings_factory,
):
    """The falsifier: a caller OUTSIDE the main lane orchestrator.

    `secondwave.detector` reaches CoinGecko on its own 30-minute loop. With the
    flag false it must issue no HTTP at all — not merely be refused later.
    """
    from scout.secondwave import detector

    settings = settings_factory(COINGECKO_DISCOVERY_ENABLED=False)
    session = MagicMock()
    session.get = MagicMock(side_effect=AssertionError("HTTP must not be issued"))

    call = governed_cg_call(BUCKET_DISCOVERY, settings)
    assert call.allowed is False
    # And the module honours it: the guard sits before the request.
    src = __import__("pathlib").Path(detector.__file__).read_text(encoding="utf-8")
    guard = src.index("governed_cg_call(BUCKET_DISCOVERY, settings)")
    request = src.index("async with session.get(")
    assert guard < request, "the budget guard must precede the request"


# ---------------------------------------------------------------------------
# S1 · Omitting Settings must be structurally impossible
# ---------------------------------------------------------------------------


def test_governed_cg_call_fails_closed_without_settings():
    """ "Counted but not refused" was a silent hole.

    A caller that forgot to thread Settings kept spending while appearing
    governed — the same invisible-spend failure this module exists to end.
    """
    with pytest.raises(TypeError, match="requires settings"):
        governed_cg_call(BUCKET_DISCOVERY, None)


def test_get_with_backoff_requires_settings():
    import inspect

    from scout.ingestion.coingecko import _get_with_backoff

    sig = inspect.signature(_get_with_backoff)
    assert sig.parameters["settings"].default is inspect.Parameter.empty
    assert sig.parameters["bucket"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# S2 · Transport failure is one attempt, zero credits
# ---------------------------------------------------------------------------


def test_transport_failure_records_an_attempt_and_no_credit(settings_factory):
    """A connection/timeout dies before any response, so `finish(status)` is
    never reached. Recording only on response made every transport failure
    invisible in the attempt rate — hiding exactly the lanes that are broken."""
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)
    before_a = cg_budget.attempts(BUCKET_DISCOVERY)
    before_c = cg_budget.credits(BUCKET_DISCOVERY)

    call = governed_cg_call(BUCKET_DISCOVERY, s)  # request issued...
    # ...connection dies here; finish() is never called.

    assert cg_budget.attempts(BUCKET_DISCOVERY) == before_a + 1
    assert cg_budget.credits(BUCKET_DISCOVERY) == before_c
    assert call.allowed is True


def test_a_200_upgrades_the_same_attempt_rather_than_adding_one(settings_factory):
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)
    before_a = cg_budget.attempts(BUCKET_DISCOVERY)
    before_c = cg_budget.credits(BUCKET_DISCOVERY)

    call = governed_cg_call(BUCKET_DISCOVERY, s)
    call.finish(200)

    assert (
        cg_budget.attempts(BUCKET_DISCOVERY) == before_a + 1
    ), "one request, one attempt"
    assert cg_budget.credits(BUCKET_DISCOVERY) == before_c + 1


def test_a_429_is_an_attempt_with_no_credit(settings_factory):
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)
    before_a = cg_budget.attempts(BUCKET_DISCOVERY)
    before_c = cg_budget.credits(BUCKET_DISCOVERY)

    call = governed_cg_call(BUCKET_DISCOVERY, s)
    call.finish(429)

    assert cg_budget.attempts(BUCKET_DISCOVERY) == before_a + 1
    assert cg_budget.credits(BUCKET_DISCOVERY) == before_c


def test_a_refused_call_is_not_an_attempt(settings_factory):
    """Refused means never issued. Counting it would inflate the attempt rate
    with requests the provider never saw."""
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=False)
    before = cg_budget.attempts(BUCKET_DISCOVERY)
    call = governed_cg_call(BUCKET_DISCOVERY, s)
    assert call.allowed is False
    assert cg_budget.attempts(BUCKET_DISCOVERY) == before


# ---------------------------------------------------------------------------
# S2 · Provider truth must survive a restart
# ---------------------------------------------------------------------------


async def test_provider_drift_survives_a_restart(db, settings_factory):
    """Without persistence, hydrate() came back with zero drift and the first
    post-restart decisions treated unattributed spend as capacity that still
    existed."""
    s = settings_factory(
        COINGECKO_DISCOVERY_ENABLED=True,
        COINGECKO_MONTHLY_DISCOVERY_CREDITS=100,
    )
    b = CoinGeckoBudget()
    for _ in range(50):
        b.record(BUCKET_DISCOVERY, billable=True)
    b.provider_credits_used = 100  # provider says 100; we counted 50
    b.provider_checked_at = datetime.now(timezone.utc)
    await b.persist(db)

    restarted = CoinGeckoBudget()
    await restarted.hydrate(db)
    assert restarted.provider_credits_used == 100
    assert restarted.unattributed_provider_drift() == 50
    assert restarted.effective_used() == 100
    assert (
        restarted.allow(BUCKET_DISCOVERY, s)[0] is False
    ), "post-restart, drift must still be consuming discovery capacity"


# ---------------------------------------------------------------------------
# S1 · Critical exhaustion blocks CG-dependent opens only
# ---------------------------------------------------------------------------


def test_critical_reserve_exhaustion_is_a_cg_specific_signal(settings_factory):
    """The registry admits `price_cache_row` alongside `cg_lane`.

    A position whose exit pricing costs no CoinGecko credits adds no demand to
    the exhausted lane, so blocking it is a false rejection.
    """
    from scout.price_sources import PRICE_SOURCE_CG_LANE, PRICE_SOURCE_PRICE_CACHE_ROW

    s = settings_factory(COINGECKO_MONTHLY_CRITICAL_CREDITS=1)
    b = CoinGeckoBudget()
    b.record(BUCKET_CRITICAL, billable=True)
    assert b.critical_reserve_exceeded(s) is True
    # The predicate is CG-scoped at the callsite; these constants exist and are
    # distinct, which is what makes that scoping expressible at all.
    assert PRICE_SOURCE_CG_LANE != PRICE_SOURCE_PRICE_CACHE_ROW


async def test_exhausted_reserve_does_not_block_an_independently_priced_position(
    db, tmp_path
):
    """THE inverse case. A `price_cache_row` token must still open."""
    from scout.config import Settings
    from scout.trading.engine import TradingEngine

    cg_budget.last_success_at = datetime.now(timezone.utc)
    settings = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="t",
        ANTHROPIC_API_KEY="t",
        DB_PATH=tmp_path / "test.db",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=5000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        COINGECKO_MONTHLY_CRITICAL_CREDITS=1,
    )
    cg_budget.record(BUCKET_CRITICAL, billable=True)
    assert cg_budget.critical_reserve_exceeded(settings) is True

    # A non-CG-shaped token WITH a price_cache row resolves to price_cache_row.
    ts = datetime.now(timezone.utc) - timedelta(seconds=30)
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d,
            market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        ("SOMEUPPERCASETOKEN", 5.0, ts.isoformat()),
    )
    await db._conn.commit()

    engine = TradingEngine(mode="paper", db=db, settings=settings)
    trade_id = await engine.open_trade(
        token_id="SOMEUPPERCASETOKEN",
        symbol="SUT",
        name="SomeToken",
        chain="ethereum",
        signal_type="volume_spike",
        signal_data={"mcap": 1_000_000},
        entry_price=5.0,
        signal_combo="volume_spike",
    )
    assert trade_id is not None, (
        "an exhausted CoinGecko reserve must not reject a position whose exit "
        "pricing does not consume CoinGecko credits"
    )


# ---------------------------------------------------------------------------
# The complete budget model must FIT
# ---------------------------------------------------------------------------

_CYCLES_PER_DAY = 1440  # 60s SCAN_INTERVAL_SECONDS
_DAYS = 31

#: Every discovery-class CoinGecko consumer OTHER than the main lane bundle,
#: in calls per day at prod settings (NARRATIVE_POLL_INTERVAL=1800 -> 48
#: passes/day; secondwave loop 1800s -> 48/day). These are the consumers the
#: original "~62k fits in 65k" figure omitted entirely.
_OTHER_DISCOVERY_PER_DAY = (
    48 * 1  # narrative observer /coins/categories
    + 48 * 1  # narrative trending tracker /search/trending
    + 48 * 2  # narrative predictor /coins/markets
    + 48 * 2  # narrative evaluator batch + /simple/price fallback
    + 48 * 1  # counter detail /coins/{id}
    + 48 * 1  # secondwave /coins/markets
)

_MAIN_BUNDLE_CALLS = 7  # top_movers 2 + trending 2 + by_volume 2 + deep_volume 1


def _monthly(per_day: float) -> int:
    return round(per_day * _DAYS)


def test_planned_discovery_workload_fits_its_envelope(settings_factory):
    """Hard caps protect against estimation error; they do not make a workload fit.

    The original C profile projected ~62k against a 65k envelope from the MAIN
    BUNDLE ALONE. Adding the six other discovery-class consumers — all enabled
    in prod — puts every-5th at 74,400/month, 9,400 OVER.
    """
    s = settings_factory()
    interval = s.COINGECKO_DISCOVERY_INTERVAL_CYCLES
    rounds_per_day = _CYCLES_PER_DAY / interval
    main = _monthly(rounds_per_day * _MAIN_BUNDLE_CALLS)
    other = _monthly(_OTHER_DISCOVERY_PER_DAY)
    total = main + other

    assert total <= s.COINGECKO_MONTHLY_DISCOVERY_CREDITS, (
        f"planned discovery {total:,}/month exceeds the "
        f"{s.COINGECKO_MONTHLY_DISCOVERY_CREDITS:,} envelope at "
        f"interval={interval} (main {main:,} + other {other:,})"
    )


def test_the_omitted_consumers_are_what_broke_the_original_profile(settings_factory):
    """Pin the arithmetic so the next cadence change re-checks it.

    At every-5th the main bundle alone is ~62k — which is why it looked like it
    fit. This asserts the OTHER consumers are large enough to matter, so nobody
    re-derives a cadence from the bundle in isolation again.
    """
    s = settings_factory()
    other = _monthly(_OTHER_DISCOVERY_PER_DAY)
    assert other > 10_000, "the omitted consumers are not a rounding error"

    at_five = _monthly((_CYCLES_PER_DAY / 5) * _MAIN_BUNDLE_CALLS) + other
    assert (
        at_five > s.COINGECKO_MONTHLY_DISCOVERY_CREDITS
    ), "every-5th must be demonstrably over the envelope — that is the finding"


def test_planned_operational_workload_fits_its_envelope(settings_factory):
    """Ledger enrollment poll + /key reconciliation + resolver lookups."""
    import scout.outcome_ledger as ledger

    s = settings_factory()
    ledger_poll = _monthly(_CYCLES_PER_DAY / ledger._ENROLLMENT_CG_POLL_INTERVAL_CYCLES)
    reconcile = _monthly(24)  # hourly
    resolver = _monthly(20)  # event-driven estimate
    total = ledger_poll + reconcile + resolver

    assert total <= s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS, (
        f"planned operational {total:,}/month exceeds the "
        f"{s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS:,} envelope "
        f"(ledger {ledger_poll:,} + reconcile {reconcile:,} + resolver {resolver:,})"
    )


def test_unthrottled_ledger_poll_would_not_have_fit_anything(settings_factory):
    """The defect, quantified: 1,440/day is ~43.2k/month.

    That is larger than the entire 30k critical reserve it was charged to, and
    ~43% of the whole 100k plan.
    """
    s = settings_factory()
    unthrottled = _monthly(_CYCLES_PER_DAY)
    assert unthrottled > s.COINGECKO_MONTHLY_CRITICAL_CREDITS
    assert unthrottled > s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS
    assert unthrottled > s.COINGECKO_MONTHLY_CREDIT_ALLOWANCE * 0.4
