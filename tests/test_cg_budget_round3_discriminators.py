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

    async def _fake_batch(session, settings, coin_ids, *, bucket, fixed_duty=False):
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
    """THE behavioural falsifier: actually INVOKE a caller outside the main lanes.

    `secondwave.fetch_current_prices` reaches CoinGecko on its own 30-minute
    loop, nowhere near `_fetch_coingecko_lanes`. With discovery disabled it must
    issue NO HTTP AT ALL — not merely be refused somewhere downstream.

    The previous version of this test only constructed a `governed_cg_call` and
    compared source-string positions; `session.get` was never exercised, so a
    mutant deleting the `if not _call.allowed: return` early return would have
    passed it while happily issuing requests.
    """
    from scout.secondwave import detector

    settings = settings_factory(COINGECKO_DISCOVERY_ENABLED=False)

    calls = []

    class _ExplodingSession:
        def get(self, *a, **k):
            calls.append((a, k))
            raise AssertionError(
                "HTTP was issued with COINGECKO_DISCOVERY_ENABLED=False"
            )

    result = await detector.fetch_current_prices(
        _ExplodingSession(), ["bitcoin", "ethereum"], settings
    )

    assert calls == [], "discovery-disabled must mean zero requests issued"
    assert result == {}


async def test_the_same_caller_DOES_issue_http_when_discovery_is_enabled(
    settings_factory,
):
    """The complement, so the test above cannot pass by the caller being inert.

    Without this, `fetch_current_prices` returning {} for an unrelated reason
    would satisfy the zero-HTTP assertion and prove nothing.
    """
    from scout.secondwave import detector

    settings = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)

    calls = []

    class _RecordingSession:
        def get(self, *a, **k):
            calls.append((a, k))
            raise RuntimeError("stop here — reaching HTTP is the assertion")

    try:
        await detector.fetch_current_prices(_RecordingSession(), ["bitcoin"], settings)
    except RuntimeError:
        pass

    assert len(calls) == 1, (
        "with discovery enabled the same caller must reach the request; "
        "otherwise the zero-HTTP test above proves nothing"
    )


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


def test_construction_alone_is_NOT_an_attempt(settings_factory):
    """Construction happens before the limiter and before any request.

    Counting there turned a cancellation while waiting on the rate limiter --
    or any early return between construction and session.get -- into a phantom
    provider attempt, violating the "one attempt per ISSUED request" contract.
    """
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)
    before = cg_budget.attempts(BUCKET_DISCOVERY)
    governed_cg_call(BUCKET_DISCOVERY, s)  # constructed, never issued
    assert cg_budget.attempts(BUCKET_DISCOVERY) == before


def test_transport_failure_is_one_attempt_and_no_credit(settings_factory):
    """The request WAS issued, then the connection died before any response.

    `finish(status)` is never reached, so recording only on response made every
    transport failure invisible in the attempt rate -- hiding exactly the lanes
    that are broken.
    """
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)
    before_a = cg_budget.attempts(BUCKET_DISCOVERY)
    before_c = cg_budget.credits(BUCKET_DISCOVERY)

    call = governed_cg_call(BUCKET_DISCOVERY, s)
    call.issued()  # about to hit the wire...
    # ...connection dies here; finish() is never called.

    assert cg_budget.attempts(BUCKET_DISCOVERY) == before_a + 1
    assert cg_budget.credits(BUCKET_DISCOVERY) == before_c


def test_transport_failure_through_the_context_manager_settles_the_same(
    settings_factory,
):
    """The `with` form must reach the same 1 attempt / 0 credits on an exception."""
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)
    before_a = cg_budget.attempts(BUCKET_DISCOVERY)
    before_c = cg_budget.credits(BUCKET_DISCOVERY)

    with pytest.raises(RuntimeError):
        with governed_cg_call(BUCKET_DISCOVERY, s) as call:
            call.issued()
            raise RuntimeError("connection reset")

    assert cg_budget.attempts(BUCKET_DISCOVERY) == before_a + 1
    assert cg_budget.credits(BUCKET_DISCOVERY) == before_c


def test_a_200_upgrades_the_same_attempt_rather_than_adding_one(settings_factory):
    s = settings_factory(COINGECKO_DISCOVERY_ENABLED=True)
    before_a = cg_budget.attempts(BUCKET_DISCOVERY)
    before_c = cg_budget.credits(BUCKET_DISCOVERY)

    call = governed_cg_call(BUCKET_DISCOVERY, s)
    call.issued()
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
    call.issued()
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
_MAIN_BUNDLE_CALLS = 7  # top_movers 2 + trending 2 + by_volume 2 + deep_volume 1
_NARRATIVE_PASSES = 48  # NARRATIVE_POLL_INTERVAL=1800
_SECONDWAVE_PER_DAY = 48  # 1800s loop, 1 call


def _monthly(per_day: float) -> int:
    return round(per_day * _DAYS)


def _narrative_max_calls_per_pass(s) -> int:
    """STRUCTURAL maximum, from executable bounds -- not an estimate.

    The previous model hardcoded "48 x 1" for detail and "48 x 2" for the
    predictor. Both are LOOP BOUNDS: `fetch_laggards` runs once per heating
    category and `fetch_coin_detail` runs for every scored token AND again for
    every control token. Worse, the multipliers are LEARNER-TUNABLE --
    strategy_bounds permits max_heating_per_cycle and max_picks_per_category up
    to 10 each, so the true uncapped fan-out is 1 + 1 + 10 + 100 + 100 = 212
    calls/pass, ~315,000/month from one consumer against a 100,000 plan.

    Production activation today is ~1 call/pass because `/coins/categories`
    returns empty and the pass aborts (2026-08-19: narrative.observe_empty x44,
    zero heating/laggard/detail events). That is an artifact of a failing
    upstream, NOT a bound; modelling on it would repeat the mistake that
    produced the 62k headline.

    So the model uses the CAP, which now covers the entire fan-out.
    """
    return 1 + 1 + s.NARRATIVE_MAX_CG_CALLS_PER_PASS


def _narrative_uncapped_per_pass() -> int:
    """What the fan-out reaches with the learner at its permitted maximum."""
    from scout.narrative.strategy_bounds import STRATEGY_BOUNDS

    max_heating = STRATEGY_BOUNDS["max_heating_per_cycle"][1]
    max_picks = STRATEGY_BOUNDS["max_picks_per_category"][1]
    return int(1 + 1 + max_heating + 2 * (max_heating * max_picks))


_EVALS_PER_DAY = 4  # NARRATIVE_EVAL_INTERVAL=21600 (6h)


def _discovery_structural_max(s) -> dict:
    """Every DISCOVERY-class consumer at its executable maximum.

    "Complete" means every KNOWN consumer appears, including ones contributing
    zero today — a table that lists only positive contributors cannot be checked
    for omissions, which is how the 62k headline survived three rounds.
    """
    main = _monthly(
        (_CYCLES_PER_DAY / s.COINGECKO_DISCOVERY_INTERVAL_CYCLES) * _MAIN_BUNDLE_CALLS
    )
    narrative = _monthly(_narrative_max_calls_per_pass(s) * _NARRATIVE_PASSES)
    # Evaluator: a SHARED total budget across the 250-id batch loop and the
    # /simple/price fallback, so the executable maximum is exactly the cap.
    evaluator = _monthly(s.NARRATIVE_MAX_CG_EVAL_CALLS_PER_PASS * _EVALS_PER_DAY)
    secondwave = _monthly(_SECONDWAVE_PER_DAY)
    return {
        "main_bundle": main,
        "narrative_prediction_pass": narrative,
        "narrative_evaluator": evaluator,
        "secondwave": secondwave,
        # Dormant, flag-default OFF. Present as explicit zeros so "complete"
        # means every known consumer is represented, and so activation is
        # visibly a budget decision rather than a silent one.
        "instrumentation_dex_resolver_DISABLED": 0,
        "market_briefing_DISABLED": 0,
    }


def _operational_structural_max(s) -> dict:
    import scout.outcome_ledger as ledger

    return {
        "ledger_cg_enrollment_poll": _monthly(
            _CYCLES_PER_DAY / ledger._ENROLLMENT_CG_POLL_INTERVAL_CYCLES
        ),
        "key_reconciliation": _monthly(24),
        "tg_resolver_capped": s.COINGECKO_TG_RESOLVER_MONTHLY_CREDITS,
    }


def test_planned_discovery_workload_fits_its_envelope(settings_factory):
    """Structural maxima, every consumer, with margin that survives error."""
    s = settings_factory()
    parts = _discovery_structural_max(s)
    total = sum(parts.values())
    envelope = s.COINGECKO_MONTHLY_DISCOVERY_CREDITS

    assert total <= envelope, (
        f"discovery structural max {total:,}/month exceeds {envelope:,} at "
        f"interval={s.COINGECKO_DISCOVERY_INTERVAL_CYCLES}: {parts}"
    )
    margin = 1 - (total / envelope)
    assert margin >= 0.10, (
        f"only {margin:.1%} headroom; the ratification condition is >=10% "
        f"because these are bounds on loops, not measurements: {parts}"
    )


def test_operational_structural_max_fits_and_protects_its_floor(settings_factory):
    """Fixed duties must remain fundable after discretionary traffic."""
    s = settings_factory()
    parts = _operational_structural_max(s)
    total = sum(parts.values())
    envelope = s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS
    assert total <= envelope, f"operational {total:,} exceeds {envelope:,}: {parts}"

    fixed = parts["ledger_cg_enrollment_poll"] + parts["key_reconciliation"]
    assert fixed <= s.COINGECKO_OPERATIONAL_FIXED_FLOOR_CREDITS, (
        f"the reserved floor {s.COINGECKO_OPERATIONAL_FIXED_FLOOR_CREDITS:,} must "
        f"cover the fixed duties ({fixed:,}) or it protects nothing"
    )
    assert (
        s.COINGECKO_TG_RESOLVER_MONTHLY_CREDITS
        <= envelope - s.COINGECKO_OPERATIONAL_FIXED_FLOOR_CREDITS
    ), "the TG ceiling must fit in what is left above the reserved floor"


def test_the_evaluator_is_in_the_model_at_all(settings_factory):
    """Round-5 finding: the evaluator was missing from the table entirely.

    Its cap also existed only in config — `evaluate_pending` never read it — so
    the batch loop was bounded by the pending-prediction count, not by budget.
    """
    s = settings_factory()
    parts = _discovery_structural_max(s)
    assert parts["narrative_evaluator"] > 0
    assert "narrative_evaluator" in parts


def test_reserved_posture_is_reported_not_just_current_state(settings_factory):
    """TWO axes, because 59,768 was a current-state projection, not a design max.

    It assumed CRITICAL=0 on the basis of today's zero open positions. The
    posture we actually designed capacity for includes the full reserve.
    """
    s = settings_factory()
    discovery = sum(_discovery_structural_max(s).values())
    operational = sum(_operational_structural_max(s).values())

    current_state = discovery + operational  # 0 held positions today
    reserved_posture = discovery + operational + s.COINGECKO_MONTHLY_CRITICAL_CREDITS

    assert reserved_posture > current_state, "the two axes must differ"
    assert reserved_posture <= s.COINGECKO_MONTHLY_CREDIT_ALLOWANCE, (
        f"reserved posture {reserved_posture:,} exceeds the "
        f"{s.COINGECKO_MONTHLY_CREDIT_ALLOWANCE:,} plan"
    )


def test_narrative_is_bounded_and_would_not_fit_unbounded(settings_factory):
    """Pin WHY the cap exists, at the LEARNER-PERMITTED maximum.

    The knobs are tunable, so the default 5x5 is not the bound. At the permitted
    10x10 the uncapped fan-out is ~315,000/month against a 100,000 plan.
    """
    s = settings_factory()
    uncapped = _monthly(_narrative_uncapped_per_pass() * _NARRATIVE_PASSES)
    assert uncapped > s.COINGECKO_MONTHLY_CREDIT_ALLOWANCE * 3, (
        "the uncapped narrative fan-out must be demonstrably larger than the "
        "whole plan -- that is the round-4 finding"
    )
    capped = _monthly(_narrative_max_calls_per_pass(s) * _NARRATIVE_PASSES)
    assert capped < s.COINGECKO_MONTHLY_DISCOVERY_CREDITS / 2


def test_the_cap_is_independent_of_learner_tuning(settings_factory):
    """The model must not depend on strategy knobs the learner can move.

    A cap expressed in CALLS holds whatever max_heating_per_cycle and
    max_picks_per_category become; a model derived from those knobs does not.
    """
    s = settings_factory()
    assert _narrative_max_calls_per_pass(s) == 2 + s.NARRATIVE_MAX_CG_CALLS_PER_PASS


def test_interval_five_is_untenable_under_the_corrected_model(settings_factory):
    """The original C profile, re-checked against structural bounds."""
    s = settings_factory()
    main5 = _monthly((_CYCLES_PER_DAY / 5) * _MAIN_BUNDLE_CALLS)
    narrative = _monthly(_narrative_max_calls_per_pass(s) * _NARRATIVE_PASSES)
    secondwave = _monthly(_SECONDWAVE_PER_DAY)
    assert main5 + narrative + secondwave > s.COINGECKO_MONTHLY_DISCOVERY_CREDITS


def test_planned_operational_workload_fits_its_envelope(settings_factory):
    """Ledger enrollment poll + /key reconciliation + resolver lookups."""
    import scout.outcome_ledger as ledger

    s = settings_factory()
    ledger_poll = _monthly(_CYCLES_PER_DAY / ledger._ENROLLMENT_CG_POLL_INTERVAL_CYCLES)
    reconcile = _monthly(24)
    resolver = _monthly(20)
    total = ledger_poll + reconcile + resolver
    assert total <= s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS, (
        f"planned operational {total:,}/month exceeds "
        f"{s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS:,} "
        f"(ledger {ledger_poll:,} + reconcile {reconcile:,} + resolver {resolver:,})"
    )


def test_unthrottled_ledger_poll_would_not_have_fit_anything(settings_factory):
    """The round-3 defect, quantified: 1,440/day is ~43.2k/month."""
    s = settings_factory()
    unthrottled = _monthly(_CYCLES_PER_DAY)
    assert unthrottled > s.COINGECKO_MONTHLY_CRITICAL_CREDITS
    assert unthrottled > s.COINGECKO_MONTHLY_OPERATIONAL_CREDITS


# ---------------------------------------------------------------------------
# S1 · The evaluator cap must be REAL and TOTAL, and truncate FAIRLY
# ---------------------------------------------------------------------------


async def test_evaluator_batch_loop_honours_the_shared_budget(
    monkeypatch, settings_factory
):
    """The cap existed only in config; `evaluate_pending` never read it.

    `fetch_prices_batch` looped over ALL unique ids in 250-id batches, so with a
    large pending backlog it was bounded by the backlog, not by budget.
    """
    from scout.narrative import evaluator

    calls = []

    async def _fake_get(*a, **k):
        raise AssertionError("should not reach HTTP in this test")

    async def _counting_backoff(*a, **k):
        calls.append(1)
        return None

    # Drive fetch_prices_batch directly with a shared budget of 2 and 10 batches
    # worth of ids (2,500 ids / 250 per call).
    monkeypatch.setattr(evaluator, "governed_cg_call", lambda *a, **k: _AllowAll())
    monkeypatch.setattr(
        evaluator.coingecko_limiter, "acquire", AsyncMock(return_value=None)
    )

    class _Resp:
        status = 429

        async def __aenter__(self):
            calls.append(1)
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return []

    class _Session:
        def get(self, *a, **k):
            return _Resp()

    budget = [2]
    ids = [f"coin-{i}" for i in range(2500)]  # 10 batches if unbounded
    await evaluator.fetch_prices_batch(
        _Session(), ids, settings=settings_factory(), budget=budget
    )
    assert len(calls) == 2, f"budget of 2 must cap the batch loop; got {len(calls)}"
    assert budget[0] == 0


class _AllowAll:
    allowed = True
    reason = "ok"
    billable = False

    def issued(self):
        pass

    def finish(self, status):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_evaluator_orders_pending_oldest_due_first():
    """Truncation decides WHICH predictions mature, so the order is load-bearing.

    An arbitrary order would right-censor narrative outcome evidence by whatever
    the DB happened to return -- a measurement bias introduced by an API-budget
    repair. This repo already has one invisible maturity backlog; a second one
    created to fit an API quota would be worse than the quota problem.
    """
    import pathlib

    from scout.narrative import evaluator

    src = pathlib.Path(evaluator.__file__).read_text(encoding="utf-8")
    assert "ORDER BY datetime(predicted_at) ASC" in src, (
        "pending predictions must be selected oldest-due first so budget "
        "truncation defers the NEWEST, deterministically"
    )
    # And the id list must preserve that order rather than going through a set.
    assert "dict.fromkeys" in src, (
        "unique_ids must preserve the oldest-due ordering; list(set(...)) "
        "would randomise which predictions get funded"
    )


def test_evaluator_budget_is_TOTAL_across_batch_and_fallback():
    """One budget, not one each -- otherwise the real cap is 2x the number."""
    import pathlib

    from scout.narrative import evaluator

    src = pathlib.Path(evaluator.__file__).read_text(encoding="utf-8")
    assert "budget=eval_budget" in src, "the batch loop must share the budget"
    assert "eval_budget[0] <= 0" in src, "the fallback must check the SAME budget"


def test_evaluator_emits_budget_accounting():
    """A budget that silently drops work is indistinguishable from no data."""
    import pathlib

    from scout.narrative import evaluator

    src = pathlib.Path(evaluator.__file__).read_text(encoding="utf-8")
    for field in ("eligible", "attempted", "deferred_due_to_budget"):
        assert field in src, f"missing {field} in the budget accounting log"
