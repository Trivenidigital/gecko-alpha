"""CG discovery cadence is decoupled from the 60s main cycle.

2026-08-21 monthly-budget repair. The Basic plan allows 100,000 credits/month
(~3,226/day); the designed bundle was 8 calls/cycle x 1440 cycles/day = 11,520.
The old model reasoned only about "the 25/min limiter" — a RATE constraint —
and never about the MONTHLY credit ceiling, which is what actually ran out.

Two invariants matter here and they are easy to conflate:

1. WHICH lanes are throttled. held-position pricing must NOT be, because the
   live trailing-stop evaluator reads the price_cache rows it writes.
2. That a skipped lane leaves NO stale payload behind. main.py reads the
   module-level `last_raw_*` globals, not this function's return value, so a
   retained payload is republished as current data.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scout.ingestion import coingecko as cg_mod
import scout.main as main_mod


@pytest.fixture(autouse=True)
def _reset_counter():
    main_mod._cg_discovery_cycle_counter = 0
    cg_mod.reset_discovery_raw()
    yield
    main_mod._cg_discovery_cycle_counter = 0
    cg_mod.reset_discovery_raw()


@pytest.fixture
def settings(settings_factory):
    return settings_factory(
        COINGECKO_DISCOVERY_INTERVAL_CYCLES=5,
        HELD_POSITION_PRICE_REFRESH_ENABLED=True,
        # These tests are about CADENCE. The dark-until-reset activation gate
        # (default False, so a deploy before the September 1 credit reset cannot
        # resume discovery) is a separate concern with its own tests below.
        COINGECKO_DISCOVERY_ENABLED=True,
    )


def _coin(cid):
    return {
        "id": cid,
        "current_price": 1.0,
        "market_cap": 1000.0,
        "total_volume": 500.0,
        "symbol": "x",
        "name": "X",
    }


async def _run_cycle(settings, db, held_return=None):
    """Invoke _fetch_coingecko_lanes with every CG fetcher mocked."""
    calls: list[str] = []

    async def _mk(name, global_attr=None):
        async def _fn(*a, **k):
            calls.append(name)
            if global_attr:
                setattr(cg_mod, global_attr, [_coin(f"{name}-coin")])
            return [_coin(f"{name}-coin")]

        return _fn

    patches = {
        "cg_fetch_top_movers": await _mk("top_movers", "last_raw_markets"),
        "cg_fetch_trending": await _mk("trending", "last_raw_trending"),
        "cg_fetch_by_volume": await _mk("by_volume", "last_raw_by_volume"),
        "cg_fetch_deep_volume": await _mk("deep_volume", "last_raw_deep_volume"),
        "cg_fetch_midcap_gainers": await _mk("midcap", "last_raw_midcap_gainers"),
    }

    async def _held(*a, **k):
        calls.append("held_position_prices")
        return held_return if held_return is not None else []

    with (
        patch.multiple(main_mod, **patches),
        patch.object(main_mod, "fetch_held_position_prices", _held),
        patch.object(
            main_mod.coingecko_limiter, "is_backing_off", MagicMock(return_value=False)
        ),
    ):
        result = await main_mod._fetch_coingecko_lanes(AsyncMock(), settings, db)
    return calls, result


async def test_discovery_runs_only_every_nth_cycle(settings):
    """4 of 5 cycles must issue ZERO discovery calls."""
    db = MagicMock()
    discovery_rounds = 0
    for _ in range(10):
        calls, _ = await _run_cycle(settings, db)
        if "top_movers" in calls:
            discovery_rounds += 1
    assert (
        discovery_rounds == 2
    ), f"expected 2 discovery rounds in 10 cycles, got {discovery_rounds}"


async def test_held_position_runs_every_cycle_even_off_cadence(settings):
    """THE lane that must NOT be throttled — the exit evaluator depends on it."""
    db = MagicMock()
    for i in range(1, 6):
        calls, _ = await _run_cycle(settings, db)
        assert "held_position_prices" in calls, f"held lane skipped on cycle {i}"
        if i < 5:
            assert "top_movers" not in calls, f"discovery ran on off-cadence cycle {i}"


async def test_skipped_cycle_leaves_no_stale_discovery_payload(settings):
    """THE data-corruption case.

    main.py reads the module globals, not the return value. If a skipped cycle
    left the previous payload in place, main.py would (a) restamp price_cache
    `updated_at=now` over minutes-old prices, and (b) re-insert the SAME coins
    into volume_history_cg under a fresh recorded_at — fabricated observations
    in the series feeding detect_spikes' 7d average and the ledger's peak7d.
    """
    db = MagicMock()

    # Cycle 5 -> discovery runs, globals populated.
    for _ in range(5):
        calls, _ = await _run_cycle(settings, db)
    assert "top_movers" in calls
    assert (
        cg_mod.last_raw_markets
    ), "precondition: discovery cycle should populate globals"

    # Cycle 6 -> off cadence. Globals MUST be empty, not retained.
    calls, _ = await _run_cycle(settings, db)
    assert "top_movers" not in calls
    assert cg_mod.last_raw_markets == []
    assert cg_mod.last_raw_trending == []
    assert cg_mod.last_raw_by_volume == []
    assert cg_mod.last_raw_deep_volume == []
    assert cg_mod.last_raw_midcap_gainers == []


async def test_backoff_midsequence_leaves_no_stale_payload_for_unrun_lanes(settings):
    """Pre-existing defect, fixed by the same reset.

    Backoff aborts the lane sequence part-way. The lanes that never ran must not
    still be holding the previous round's payload.
    """
    db = MagicMock()
    await _run_cycle(settings, db)  # cycles 1..5 to reach a discovery round
    for _ in range(4):
        await _run_cycle(settings, db)
    assert cg_mod.last_raw_by_volume, "precondition: a full round populated by_volume"

    # Next discovery round, but backoff trips right after top_movers.
    calls: list[str] = []

    async def _movers(*a, **k):
        calls.append("top_movers")
        cg_mod.last_raw_markets = [_coin("m")]
        return [_coin("m")]

    async def _held(*a, **k):
        return []

    backoff = MagicMock(side_effect=[False, True, True, True, True, True])
    with (
        patch.object(main_mod, "cg_fetch_top_movers", _movers),
        patch.object(main_mod, "fetch_held_position_prices", _held),
        patch.object(main_mod.coingecko_limiter, "is_backing_off", backoff),
    ):
        for _ in range(5):
            main_mod._cg_discovery_cycle_counter += 1
        main_mod._cg_discovery_cycle_counter -= 1
        await main_mod._fetch_coingecko_lanes(AsyncMock(), settings, db)

    assert cg_mod.last_raw_by_volume == [], "unrun lane retained a stale payload"
    assert cg_mod.last_raw_deep_volume == []


def test_volume_scan_pages_reduced_to_two(settings_factory):
    """C initial profile: 3 -> 2, restored only on measured evidence."""
    assert settings_factory().COINGECKO_VOLUME_SCAN_PAGES == 2


def test_discovery_interval_is_settings_sourced_not_hardcoded(settings_factory):
    s = settings_factory(COINGECKO_DISCOVERY_INTERVAL_CYCLES=3)
    assert s.COINGECKO_DISCOVERY_INTERVAL_CYCLES == 3
    with pytest.raises(ValueError):
        settings_factory(COINGECKO_DISCOVERY_INTERVAL_CYCLES=0)


# ---------------------------------------------------------------------------
# Dark-until-reset activation gate
# ---------------------------------------------------------------------------


async def test_discovery_stays_dark_when_not_activated(settings_factory):
    """THE deployment-boundary guarantee.

    The ruling is "no CoinGecko discovery before the September 1 credit reset".
    Without a default-off flag that is only a promise about deployment TIMING: a
    deploy before the reset starts with an EMPTY local ledger, so every envelope
    looks available and discovery resumes on the 5th cycle. The provider would
    answer 429 because its real quota is gone -- but attempting and being
    refused is not "dark", it just moves the failure somewhere the local ledger
    cannot see.
    """
    settings = settings_factory(
        COINGECKO_DISCOVERY_INTERVAL_CYCLES=1,  # would fire EVERY cycle
        COINGECKO_DISCOVERY_ENABLED=False,
        HELD_POSITION_PRICE_REFRESH_ENABLED=True,
    )
    db = MagicMock()
    for _ in range(10):
        calls, _ = await _run_cycle(settings, db)
        assert "top_movers" not in calls
        assert "trending" not in calls
        assert "by_volume" not in calls
        # ...and the critical lane is UNAFFECTED: held positions must stay
        # re-priceable while discovery is dark.
        assert "held_position_prices" in calls


def test_discovery_activation_defaults_to_off():
    """Default-off, so the guarantee holds even if nobody sets anything.

    Constructs Settings DIRECTLY rather than via settings_factory: the factory
    deliberately enables discovery so unrelated fetch tests exercise real
    behaviour instead of a refused request. Asking the factory what the SHIPPED
    default is would only echo the factory's own override — a test that cannot
    observe the thing it names.
    """
    from scout.config import Settings

    shipped = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert shipped.COINGECKO_DISCOVERY_ENABLED is False
