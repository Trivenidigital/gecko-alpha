"""Shared test fixtures for CoinPump Scout."""

import aiosqlite.core
import pytest
import sys

from scout.config import Settings
from scout.models import CandidateToken

# Fix for issue #31 — aiosqlite interpreter-shutdown hang.
#
# aiosqlite.Connection spawns a worker Thread that is NOT a daemon. On
# interpreter shutdown Python waits for all non-daemon threads to exit.
# In tests (pytest-asyncio auto mode) each test gets its own event loop;
# when a Connection is closed, the worker thread tries to post a result
# via call_soon_threadsafe to a loop that pytest-asyncio has since
# closed, raising RuntimeError('Event loop is closed') from inside the
# thread's try/except. That exception propagates out of the worker and
# the thread dies — but the underlying sqlite3 file is still unclosed
# on some paths, and other aiosqlite internals have left pending work
# that keeps the interpreter alive on shutdown for ~9 minutes on CI.
#
# Force the worker thread to be a daemon in tests only. Production code
# still uses the non-daemon default so a clean shutdown path for real
# data writes is preserved. This must run before any aiosqlite.Connection
# is instantiated; conftest.py module-level is early enough because
# scout.db only creates Connections lazily inside test bodies.
_OrigThread = aiosqlite.core.Thread


class _DaemonThread(_OrigThread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daemon = True


aiosqlite.core.Thread = _DaemonThread


@pytest.fixture(autouse=True)
def _reset_signal_sources_cache():
    """BL-067: per-test reset of `scout.trading.conviction` module cache.

    The `_signal_sources_missing` set is module-level — without a reset
    fixture, a missing-table cached during one test silently propagates
    to all subsequent tests in the same session, causing backtest tests
    to see stack=0 for tables that were missing in a different DB.

    Try/except for TDD-friendliness — works before the module exists.
    """
    try:
        from scout.trading.conviction import clear_missing_sources_cache_for_tests

        clear_missing_sources_cache_for_tests()
    except ImportError:
        pass
    yield


@pytest.fixture(autouse=True)
async def _reset_coingecko_limiter_state():
    """Keep shared CoinGecko cooldown state from leaking between tests."""

    async def _reset_known_limiters() -> None:
        from scout import ratelimit

        limiters = {ratelimit.coingecko_limiter}
        for module_name in (
            "scout.ingestion.coingecko",
            "scout.social.telegram.resolver",
            "scout.secondwave.detector",
            "scout.counter.detail",
            "scout.narrative.evaluator",
            "scout.narrative.observer",
            "scout.narrative.predictor",
        ):
            module = sys.modules.get(module_name)
            limiter = getattr(module, "coingecko_limiter", None) if module else None
            if limiter is not None:
                limiters.add(limiter)

        for limiter in limiters:
            reset = getattr(limiter, "reset", None)
            if reset is not None:
                await reset()

    await _reset_known_limiters()
    yield
    await _reset_known_limiters()


@pytest.fixture(autouse=True)
def solana_retry_backoff(monkeypatch):
    """Walk the Solana transport's retry ladder without waiting for it.

    ``transport._BACKOFFS`` is (0.5, 1.0, 2.0) and is slept for real, so every
    test that drove a full transient-retry ladder burned 3.5 seconds of wall
    clock doing nothing. Autouse because the cost is invisible at the call
    site: a test does not look slow, it just is, and the next one added would
    pay the same toll silently.

    The SLEEP is replaced, never the schedule. Attempt counts, ordering and
    exhaustion behaviour are decided by the ladder's length, which is
    untouched — so the retry semantics under test are exactly the production
    ones. Yields the list of delays the ladder asked for, so a test can assert
    the schedule was WALKED rather than merely skipped.
    """
    from scout.live.solana import transport

    requested: list[float] = []

    async def _instant(delay: float) -> None:
        requested.append(delay)

    monkeypatch.setattr(transport, "_retry_sleep", _instant)
    return requested


@pytest.fixture(autouse=True)
def _reset_tg_shadow_provider_registry():
    """Drop any registered `CallerFeatureProvider` between tests.

    The registry is module-level. A provider left registered by one test
    silently changes the next test's `gate_version` (its `module_source_hash`
    is in the fingerprint), which turns a fingerprint assertion into a
    test-order dependency.

    Try/except for TDD-friendliness — works before the module exists.
    """
    try:
        from scout.social.telegram.shadow import clear_registered_provider
    except ImportError:
        yield
        return
    clear_registered_provider()
    yield
    clear_registered_provider()


class FixtureCallerFeatureProvider:
    """TEST-ONLY deterministic `CallerFeatureProvider`.

    NEVER registered by production wiring — Stage A ships no real provider,
    and a fixture that leaked into production would arm a gate_version against
    a made-up feature set.

    Every returned value derives from `(channel_handle, decision_as_of,
    current_signal_id)` and the events handed to the constructor, filtered by
    `at <= decision_as_of`. That filter is what makes replay determinism real
    rather than asserted: evidence added after a decision's `as_of` cannot
    reach it, so re-running that decision later returns the same features.

    `history_*` values exclude `current_signal_id` per the §B provider
    contract — a signal may not improve its own caller's reputation. `event_*`
    values describe THIS call and may see it.
    """

    caller_feature_semantic_version = "tg-caller-fixture-v1"

    def __init__(
        self,
        events: list[dict] | None = None,
        *,
        module_source_hash: str = "0" * 64,
        semantic_version: str | None = None,
        schema: list[tuple[str, str]] | None = None,
    ) -> None:
        # `events`: dicts of {at: datetime, cluster: str, priceable: bool,
        # signal_id: int | None}.
        self._events = list(events or [])
        self.module_source_hash = module_source_hash
        if semantic_version is not None:
            self.caller_feature_semantic_version = semantic_version
        self._schema = schema or [
            ("history_eligible_distinct_clusters", "int"),
            ("history_priceable_coverage_rate", "float"),
            ("event_duplicate_rank_in_cluster", "int"),
        ]

    def feature_schema(self) -> list[tuple[str, str]]:
        return list(self._schema)

    def features(self, channel_handle, decision_as_of, current_signal_id):
        knowable = [e for e in self._events if e["at"] <= decision_as_of]
        history = [e for e in knowable if e.get("signal_id") != current_signal_id]
        clusters = {e["cluster"] for e in history}
        priceable = [e for e in history if e.get("priceable", True)]
        coverage = len(priceable) / len(history) if history else 0.0
        current = [e for e in knowable if e.get("signal_id") == current_signal_id]
        rank = 1
        if current:
            cluster = current[0]["cluster"]
            rank = 1 + sum(
                1
                for e in history
                if e["cluster"] == cluster and e["at"] <= current[0]["at"]
            )
        return {
            "history_eligible_distinct_clusters": len(clusters),
            "history_priceable_coverage_rate": coverage,
            "event_duplicate_rank_in_cluster": rank,
            "channel_handle": channel_handle,
        }


@pytest.fixture
def fixture_caller_feature_provider():
    """Factory for `FixtureCallerFeatureProvider`."""
    return FixtureCallerFeatureProvider


@pytest.fixture
def settings_factory():
    def _make(**overrides):
        defaults = dict(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            # Tests describe an OPERATING system. COINGECKO_DISCOVERY_ENABLED
            # ships False so a deploy before the September 1 credit reset cannot
            # resume discovery, but that is a deployment-time state: leaving it
            # off here would make every fetch test assert against a refused
            # request instead of the behaviour it means to cover. Tests for the
            # dark gate itself pass COINGECKO_DISCOVERY_ENABLED=False explicitly.
            COINGECKO_DISCOVERY_ENABLED=True,
        )
        defaults.update(overrides)
        return Settings(**defaults)

    return _make


@pytest.fixture
def patch_module_sleep(monkeypatch):
    """Return a helper that short-circuits ``asyncio.sleep`` in specific modules.

    Usage::

        def test_x(patch_module_sleep):
            patch_module_sleep("scout.ingestion.coingecko", "scout.ratelimit")
            ...

    Builds a ``types.SimpleNamespace`` clone of the real ``asyncio`` module with
    ``sleep`` replaced by an instant no-op, then monkey-patches the target
    modules' ``asyncio`` attribute to that clone. The real ``asyncio`` module is
    untouched — aiohttp, pytest-asyncio, and other libs keep working normally.
    """
    import asyncio as _asyncio_mod
    import importlib
    import types

    fake_asyncio = types.SimpleNamespace(
        **{
            n: getattr(_asyncio_mod, n)
            for n in dir(_asyncio_mod)
            if not n.startswith("_")
        }
    )

    async def _instant(_):
        return None

    fake_asyncio.sleep = _instant

    def _apply(*module_paths):
        for path in module_paths:
            mod = importlib.import_module(path)
            monkeypatch.setattr(mod, "asyncio", fake_asyncio)

    return _apply


@pytest.fixture
def token_factory():
    def _make(**overrides):
        defaults = dict(
            contract_address="0xtest",
            chain="solana",
            token_name="Test",
            ticker="TST",
            token_age_days=1.0,
            market_cap_usd=50000.0,
            liquidity_usd=10000.0,
            volume_24h_usd=80000.0,
            holder_count=100,
            holder_growth_1h=25,
        )
        defaults.update(overrides)
        return CandidateToken(**defaults)

    return _make


# Imported once at module load, not per test: this reset runs for every test in
# the suite (~7k), so anything done inside the fixture body is multiplied by 7000.
import datetime as _dt  # noqa: E402
import scout.main as _scout_main  # noqa: E402
from scout import coingecko_budget as _scout_cg_budget  # noqa: E402
from scout.ingestion import coingecko as _scout_cg  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cg_process_state():
    """Isolate the process-global CoinGecko cadence counter and credit ledger.

    Both are module-level singletons by design (consulted on every CG call, so a
    per-call DB round-trip would be its own budget problem). That makes them leak
    across tests: without this, whether a test lands on the discovery cadence
    depends on how many CG cycles earlier tests ran, and a test asserting on
    credits inherits another test's spend.

    Deliberately does the minimum work possible — plain assignments, no object
    construction, imports hoisted to module scope. An autouse fixture is paid by
    every test in the suite, and CI runs against a 25-minute wall.
    """
    _scout_main._cg_discovery_cycle_counter = 0
    _scout_cg.last_raw_markets = []
    _scout_cg.last_raw_trending = []
    _scout_cg.last_raw_by_volume = []
    _scout_cg.last_raw_midcap_gainers = []
    _scout_cg.last_raw_deep_volume = []
    _b = _scout_cg_budget.budget
    _b._month = _scout_cg_budget.billing_month()
    for _k in _b._counts:
        _b._counts[_k][0] = 0
        _b._counts[_k][1] = 0
    _b._dirty = False
    _b.provider_credits_used = None
    # Default to a HEALTHY CoinGecko provider, because that is the normal
    # production state and the open-boundary liveness gate reads this rather
    # than price_cache. Tests that exercise a DEAD provider set
    # `last_success_at = None` (or an old timestamp) explicitly -- opting into
    # the failure is clearer than every unrelated test having to opt out of it.
    _b.last_success_at = _dt.datetime.now(_dt.timezone.utc)
    _b._pace_alerted = False
    yield
