"""PR-S3: the resolver endpoint pool.

The pool exists to make one verdict trustworthy — ``definitively_not_submitted``,
the only one that clears the lane and licenses a rerun. Every test here is
ultimately about that verdict being reached ONLY when it is earned:

* an endpoint that cannot prove it is on mainnet-beta never serves a verdict;
* a resolution's two sweeps come from ONE node, or the verdict is withdrawn;
* a definitive absence is corroborated by a second node, or it is flagged as
  uncorroborated — and a corroborator that disagrees collapses it;
* the pool can only READ. Adding endpoints widens what the lane can see, never
  what it can send.

Assertions are on real objects — resolved verdicts, endpoint state, evidence
files, ledger rows — rather than on mock call logs, because a mock call log
proves a function was called and not that the lane reached a safe state.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from scout.live.solana.constants import SOLANA_MAINNET_GENESIS_HASH, USDC_MINT
from scout.live.solana.resolver_pool import (
    PooledReadClient,
    ResolverPool,
    redact_endpoint,
    resolve_with_pool,
    resolver_urls,
)
from scout.live.solana.rpc_client import SignatureStatus

_SIGNATURE = "4Rf81Nd6uXrp39hFFr1WMFnGjzxyGXrvwb8VSRUnotH1uTbzLEsatgthdk1mR2yUq2P1Bz5HXoccfQX7YTUgffwy"
_LAST_VALID = 283_000_500
_DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"

# An API key in the path, exactly as Alchemy and Helius serve it. Asserted
# ABSENT from every label, log line and evidence field.
_KEYED_URL = "https://solana-mainnet.g.alchemy.com/v2/SECRET-KEY-DO-NOT-LEAK"


class FakeEndpoint:
    """One scripted node. Every read is queued, and each call pops the next.

    Deliberately not a MagicMock: the tests below assert on which node served
    which read, and a mock that answers everything cannot express "this node
    is down" or "this node is behind" — the two states the pool exists for.
    """

    def __init__(
        self,
        *,
        genesis: str = SOLANA_MAINNET_GENESIS_HASH,
        health: str | Exception = "ok",
        statuses: list | None = None,
        heights: list | None = None,
        fail_reads: Exception | None = None,
        genesis_error: Exception | None = None,
    ):
        self._genesis = genesis
        self._genesis_error = genesis_error
        self._health = health
        self._statuses = list(statuses or [])
        self._heights = list(heights or [])
        self._fail_reads = fail_reads
        self.status_calls = 0
        self.height_calls = 0

    async def get_genesis_hash(self):
        if self._genesis_error is not None:
            raise self._genesis_error
        return self._genesis

    async def get_health(self):
        if isinstance(self._health, Exception):
            raise self._health
        return self._health

    async def get_signature_statuses(self, _signatures, **_kw):
        self.status_calls += 1
        if self._fail_reads is not None:
            raise self._fail_reads
        return [self._statuses.pop(0)]

    async def get_block_height(self, **_kw):
        self.height_calls += 1
        if self._fail_reads is not None:
            raise self._fail_reads
        return self._heights.pop(0)

    async def get_balance(self, _owner, **_kw):
        return 0

    async def get_token_balance(self, _owner, _mint, **_kw):
        return 0


def _pool(settings, *endpoints):
    return ResolverPool.from_clients(
        settings,
        [(f"https://node-{i}.example/rpc", e) for i, e in enumerate(endpoints)],
    )


def _absent_and_expired(sweeps: int = 2) -> dict:
    return {
        "statuses": [SignatureStatus(known=False) for _ in range(sweeps)],
        "heights": [_LAST_VALID + 10 + i for i in range(sweeps)],
    }


async def _resolve(pool, settings, **kw):
    return await resolve_with_pool(
        pool=pool,
        expected_signature=_SIGNATURE,
        last_valid_block_height=kw.pop("last_valid_block_height", _LAST_VALID),
        settings=settings,
        **kw,
    )


@pytest.fixture
def pool_settings(settings_factory):
    return settings_factory(SOLANA_SUBMISSION_SETTLE_SEC=0.0)


# ======================================================================
# Configuration: the ordered list, and back-compat with the deployed key
# ======================================================================
def test_the_deployed_singular_key_is_read_as_a_one_element_pool(settings_factory):
    """What is in .env today keeps working, unchanged and authoritative."""
    settings = settings_factory(SOLANA_RESOLVER_RPC_URL="https://dedicated.example/rpc")
    assert resolver_urls(settings) == ["https://dedicated.example/rpc"]


def test_the_url_list_supersedes_the_singular_key(settings_factory):
    settings = settings_factory(
        SOLANA_RESOLVER_RPC_URL="https://old.example/rpc",
        SOLANA_RESOLVER_RPC_URLS="https://a.example/rpc,https://b.example/rpc",
    )
    assert resolver_urls(settings) == [
        "https://a.example/rpc",
        "https://b.example/rpc",
    ]


def test_the_url_list_accepts_a_json_array_and_a_native_list(settings_factory):
    as_json = settings_factory(
        SOLANA_RESOLVER_RPC_URLS='["https://a.example/rpc", "https://b.example/rpc"]'
    )
    as_list = settings_factory(
        SOLANA_RESOLVER_RPC_URLS=["https://a.example/rpc", "https://b.example/rpc"]
    )
    assert resolver_urls(as_json) == resolver_urls(as_list)


def test_neither_key_set_falls_back_to_the_general_rpc(settings_factory):
    settings = settings_factory(SOLANA_RPC_URL="https://general.example/rpc")
    assert resolver_urls(settings) == ["https://general.example/rpc"]


def test_duplicate_urls_collapse_to_one_endpoint(settings_factory):
    """Two identical URLs are ONE node.

    Left alone they would let a 'corroborated by a second endpoint' verdict be
    one node agreeing with itself, which is the opposite of corroboration.
    """
    settings = settings_factory(
        SOLANA_RESOLVER_RPC_URLS="https://a.example/rpc,https://a.example/rpc"
    )
    assert resolver_urls(settings) == ["https://a.example/rpc"]


def test_a_zero_health_timeout_is_refused(settings_factory):
    """Zero is not 'no limit' — it admits no endpoint at all."""
    with pytest.raises(ValueError, match="SOLANA_RESOLVER_HEALTH_TIMEOUT_SEC"):
        settings_factory(SOLANA_RESOLVER_HEALTH_TIMEOUT_SEC=0)


# ======================================================================
# Secrets: the URL carries the API key
# ======================================================================
def test_the_endpoint_label_never_carries_the_api_key():
    label = redact_endpoint(_KEYED_URL)
    assert "SECRET-KEY-DO-NOT-LEAK" not in label
    assert "/v2/" not in label
    assert label.startswith("solana-mainnet.g.alchemy.com#")


def test_two_keys_on_one_host_get_distinguishable_labels():
    """Otherwise an operator cannot tell which endpoint an alert is about."""
    first = redact_endpoint("https://host.example/v2/KEY-ONE")
    second = redact_endpoint("https://host.example/v2/KEY-TWO")
    assert first != second
    assert redact_endpoint("https://host.example/v2/KEY-ONE") == first  # stable


async def test_a_failing_endpoints_detail_does_not_quote_its_url(pool_settings):
    """The endpoint most likely to be pasted into a ticket is the failing one."""
    pool = ResolverPool.from_clients(
        pool_settings,
        [(_KEYED_URL, FakeEndpoint(genesis_error=ConnectionError("unreachable")))],
    )
    (entry,) = await pool.check_health()
    assert entry.usable is False
    blob = repr(entry.as_evidence())
    assert "SECRET-KEY-DO-NOT-LEAK" not in blob
    assert "ConnectionError" in blob  # still says WHAT failed


# ======================================================================
# Chain identity: the endpoint has to prove which chain it serves
# ======================================================================
async def test_a_devnet_endpoint_is_excluded_from_the_pool(pool_settings):
    """*** The verdict-poisoning case. ***

    A devnet node reports every mainnet signature as absent. Absent is half of
    `definitively_not_submitted`, so an unchecked devnet endpoint would retire
    rows for transactions that landed.
    """
    good = FakeEndpoint()
    devnet = FakeEndpoint(genesis=_DEVNET_GENESIS)
    pool = _pool(pool_settings, devnet, good)

    health = await pool.check_health()
    assert [h.usable for h in health] == [False, True]
    assert health[0].genesis_ok is False
    assert _DEVNET_GENESIS in health[0].detail

    # And it never serves a read, whatever key is asked for.
    served = {pool.select(f"sig-{i}").index for i in range(50)}
    assert served == {1}


async def test_an_unreachable_endpoint_is_not_usable(pool_settings):
    """'We could not check' and 'it checked out' must not render the same."""
    pool = _pool(pool_settings, FakeEndpoint(genesis_error=TimeoutError("no answer")))
    (entry,) = await pool.check_health()
    assert entry.usable is False
    assert entry.genesis_ok is False


async def test_a_lagging_endpoint_is_not_usable(pool_settings):
    """getHealth reports lag as an error; a node behind the cluster is behind
    on BOTH facts the definitive verdict is assembled from."""
    from scout.live.solana.exceptions import SolanaAPIError

    pool = _pool(
        pool_settings, FakeEndpoint(health=SolanaAPIError("behind by 4021 slots"))
    )
    (entry,) = await pool.check_health()
    assert entry.usable is False
    assert entry.genesis_ok is True  # right chain, wrong state
    assert "health probe failed" in entry.detail


async def test_a_slow_endpoint_is_demoted_but_kept(settings_factory):
    """Slow beats absent: an unresolvable signature blocks the lane.

    Real elapsed time rather than a patched clock — the two probes run
    concurrently, so a scripted clock would be asserting on gather's
    interleaving rather than on the latency measurement.
    """
    import asyncio

    class Slow(FakeEndpoint):
        async def get_health(self):
            await asyncio.sleep(0.05)
            return "ok"

    pool_settings = settings_factory(
        SOLANA_SUBMISSION_SETTLE_SEC=0.0, SOLANA_RESOLVER_MAX_LATENCY_MS=25.0
    )
    slow, fast = Slow(), FakeEndpoint()
    pool = _pool(pool_settings, slow, fast)
    health = await pool.check_health()

    assert [h.usable for h in health] == [True, True]
    assert health[0].degraded is True and health[1].degraded is False
    # Demoted, not excluded: the fast node is preferred for every key, but the
    # slow one is still in the rotation behind it.
    assert {pool.select(f"sig-{i}").index for i in range(50)} == {1}
    assert [e.index for e in pool.rotation("sig-0")] == [1, 0]


# ======================================================================
# Deterministic selection: both sweeps see one chain
# ======================================================================
def test_selection_is_deterministic_for_the_same_key(pool_settings):
    endpoints = [FakeEndpoint() for _ in range(4)]
    pool = _pool(pool_settings, *endpoints)
    chosen = pool.select(_SIGNATURE).label
    assert all(pool.select(_SIGNATURE).label == chosen for _ in range(20))
    # And across a freshly-built pool over the same URLs — determinism has to
    # survive a process restart, or `resolve` picks a different node than the
    # `place` that preceded it.
    rebuilt = _pool(pool_settings, *[FakeEndpoint() for _ in range(4)])
    assert rebuilt.select(_SIGNATURE).label == chosen


def test_different_keys_spread_across_the_pool(pool_settings):
    """A standby endpoint that never serves traffic is untested at the moment
    it is needed."""
    pool = _pool(pool_settings, *[FakeEndpoint() for _ in range(4)])
    used = {pool.select(f"signature-{i}").index for i in range(200)}
    assert used == {0, 1, 2, 3}


async def test_both_sweeps_of_one_resolution_read_from_one_endpoint(pool_settings):
    """*** The reason selection is keyed rather than round-robin. ***

    Split sweeps turn "absent at A, then absent at B" into a false double
    absence. Asserted on the endpoints' own call counters, not on the pool's
    bookkeeping.
    """
    endpoints = [FakeEndpoint(**_absent_and_expired()) for _ in range(3)]
    pool = _pool(pool_settings, *endpoints)
    resolution = await _resolve(pool, pool_settings)

    assert resolution.report.verdict == "definitively_not_submitted"
    # Exactly one endpoint served BOTH sweeps. A second endpoint may have been
    # asked once — that is the corroborating probe, which is a different
    # question and deliberately goes to a different node.
    swept = [e for e in endpoints if e.status_calls >= 2]
    assert len(swept) == 1, "the two sweeps straddled two endpoints"
    assert swept[0].status_calls == 2
    assert resolution.single_view is True
    assert len(resolution.endpoints_used) == 1


# ======================================================================
# Read failover
# ======================================================================
async def test_a_dead_endpoint_fails_over_instead_of_blocking_the_lane(pool_settings):
    """An unreachable resolver means `unresolved`, and `unresolved` BLOCKS.

    Single-endpoint, that makes an RPC outage an outage of the recovery path.
    Failover is what a second endpoint buys.
    """
    dead = FakeEndpoint(fail_reads=ConnectionError("endpoint down"))
    alive = FakeEndpoint(**_absent_and_expired())
    pool = _pool(pool_settings, dead, alive)
    # Force the dead node to be picked first for this signature.
    client = PooledReadClient([pool.endpoints[0], pool.endpoints[1]])
    statuses = await client.get_signature_statuses([_SIGNATURE])

    assert statuses == [SignatureStatus(known=False)]
    assert (
        client.pinned.index == 1
    ), "should have re-pinned to the endpoint that answered"
    assert alive.status_calls == 1
    assert client.failures[0]["error_type"] == "ConnectionError"


async def test_failover_never_invents_an_absence_when_every_endpoint_is_down(
    pool_settings,
):
    """All endpoints down is 'we could not look', which is `unresolved`."""
    pool = _pool(
        pool_settings,
        FakeEndpoint(fail_reads=ConnectionError("down")),
        FakeEndpoint(fail_reads=TimeoutError("down too")),
    )
    resolution = await _resolve(pool, pool_settings)

    assert resolution.report.verdict == "unresolved"
    assert resolution.report.rebuild_is_safe is False
    assert resolution.report.probes[0].outcome == "error"


async def test_a_split_view_withdraws_the_definitive_verdict(pool_settings):
    """*** The failover safety property. ***

    Failing over BETWEEN sweeps is safe for a read and fatal for a verdict:
    node A's absence plus node B's absence is not one node's double absence.
    The raw resolver says definitive; the pool refuses it.
    """

    class DiesAfterOneSweep(FakeEndpoint):
        async def get_signature_statuses(self, signatures, **kw):
            if self.status_calls >= 1:
                raise ConnectionError("endpoint dropped between sweeps")
            return await super().get_signature_statuses(signatures, **kw)

    first = DiesAfterOneSweep(
        statuses=[SignatureStatus(known=False)], heights=[_LAST_VALID + 10]
    )
    second = FakeEndpoint(**_absent_and_expired())
    pool = ResolverPool.from_clients(
        pool_settings,
        [("https://a.example/rpc", first), ("https://b.example/rpc", second)],
    )
    client = PooledReadClient([pool.endpoints[0], pool.endpoints[1]])
    monkeypatched = ResolverPool.read_client
    try:
        ResolverPool.read_client = lambda self, key: client  # type: ignore[assignment]
        resolution = await _resolve(pool, pool_settings)
    finally:
        ResolverPool.read_client = monkeypatched  # type: ignore[assignment]

    assert resolution.raw_verdict == "definitively_not_submitted"
    assert resolution.single_view is False
    assert resolution.downgrade_reason == "split_across_endpoints"
    assert resolution.report.verdict == "unresolved"
    assert resolution.report.rebuild_is_safe is False


# ======================================================================
# Cross-endpoint corroboration of the definitive verdict
# ======================================================================
async def test_one_endpoint_still_reaches_a_definitive_verdict(pool_settings):
    """*** Absence of a secondary must NOT block activation. ***

    The lane ships on one endpoint. The verdict stands; the evidence records
    that nobody corroborated it.
    """
    pool = _pool(pool_settings, FakeEndpoint(**_absent_and_expired()))
    resolution = await _resolve(pool, pool_settings)

    assert resolution.report.verdict == "definitively_not_submitted"
    assert resolution.downgrade_reason is None
    assert resolution.corroboration.attempted is False
    assert "no second resolver endpoint" in resolution.corroboration.detail


async def test_a_second_endpoint_that_agrees_leaves_the_verdict_standing(pool_settings):
    resolver = FakeEndpoint(**_absent_and_expired())
    corroborator = FakeEndpoint(
        statuses=[SignatureStatus(known=False)], heights=[_LAST_VALID + 30]
    )
    pool = _pool(pool_settings, resolver, corroborator)
    # Pin the resolution to endpoint 0 so endpoint 1 is the corroborator.
    resolution = await resolve_with_pool(
        pool=pool,
        expected_signature=_key_selecting(pool, 0),
        last_valid_block_height=_LAST_VALID,
        settings=pool_settings,
    )

    assert resolution.report.verdict == "definitively_not_submitted"
    assert resolution.corroboration.attempted is True
    assert resolution.corroboration.agrees is True
    assert resolution.corroboration.observed == "absent_and_expired"
    assert corroborator.status_calls == 1


async def test_a_second_endpoint_holding_the_signature_overturns_the_verdict(
    pool_settings,
):
    """*** The failure the whole pool exists to catch. ***

    One node's absence is compatible with the transaction existing. When the
    second node HAS it, the transaction landed — and the lane must not clear.
    """
    resolver = FakeEndpoint(**_absent_and_expired())
    corroborator = FakeEndpoint(
        statuses=[SignatureStatus(known=True, slot=9, err=None)]
    )
    pool = _pool(pool_settings, resolver, corroborator)
    resolution = await resolve_with_pool(
        pool=pool,
        expected_signature=_key_selecting(pool, 0),
        last_valid_block_height=_LAST_VALID,
        settings=pool_settings,
    )

    assert resolution.raw_verdict == "definitively_not_submitted"
    assert resolution.report.verdict == "landed"
    assert resolution.report.rebuild_is_safe is False
    assert resolution.downgrade_reason == "corroborating_endpoint_has_the_signature"


async def test_a_second_endpoint_that_is_behind_collapses_the_verdict(pool_settings):
    """The corroborator says the blockhash has NOT expired — it can still land."""
    resolver = FakeEndpoint(**_absent_and_expired())
    corroborator = FakeEndpoint(
        statuses=[SignatureStatus(known=False)], heights=[_LAST_VALID - 5]
    )
    pool = _pool(pool_settings, resolver, corroborator)
    resolution = await resolve_with_pool(
        pool=pool,
        expected_signature=_key_selecting(pool, 0),
        last_valid_block_height=_LAST_VALID,
        settings=pool_settings,
    )

    assert resolution.report.verdict == "unresolved"
    assert resolution.corroboration.observed == "absent_not_expired"
    assert resolution.downgrade_reason == "corroboration_failed:absent_not_expired"


async def test_an_unreachable_corroborator_is_not_assent(pool_settings):
    """Silence is not agreement, or the corroboration step is decorative."""
    resolver = FakeEndpoint(**_absent_and_expired())
    corroborator = FakeEndpoint(fail_reads=ConnectionError("down"))
    pool = _pool(pool_settings, resolver, corroborator)
    resolution = await resolve_with_pool(
        pool=pool,
        expected_signature=_key_selecting(pool, 0),
        last_valid_block_height=_LAST_VALID,
        settings=pool_settings,
    )

    assert resolution.report.verdict == "unresolved"
    assert resolution.corroboration.agrees is False
    assert resolution.corroboration.observed == "error"


async def test_a_landed_verdict_is_not_put_to_a_second_endpoint(pool_settings):
    """Presence is positive evidence; a second opinion cannot un-land a swap."""
    resolver = FakeEndpoint(statuses=[SignatureStatus(known=True, slot=7, err=None)])
    corroborator = FakeEndpoint()
    pool = _pool(pool_settings, resolver, corroborator)
    resolution = await resolve_with_pool(
        pool=pool,
        expected_signature=_key_selecting(pool, 0),
        last_valid_block_height=_LAST_VALID,
        settings=pool_settings,
    )

    assert resolution.report.verdict == "landed"
    assert resolution.corroboration.attempted is False
    assert corroborator.status_calls == 0


def _key_selecting(pool, index: int) -> str:
    """A signature-shaped key that deterministically selects ``index``.

    Searched rather than hardcoded: the selection function may change, and a
    test that pinned a magic string would then silently exercise the wrong
    endpoint instead of failing.
    """
    for i in range(10_000):
        key = f"{_SIGNATURE}{i}"
        if pool.select(key).index == index:
            return key
    raise AssertionError(f"no key selects endpoint {index}")


# ======================================================================
# The pool cannot broadcast
# ======================================================================
@pytest.mark.parametrize(
    "forbidden",
    [
        "send_transaction",
        "sendTransaction",
        "send_raw_transaction",
        "broadcast",
        "submit_transaction",
    ],
)
def test_the_pooled_client_has_no_broadcast_method(forbidden):
    """Adding endpoints widens what the lane READS, never what it sends."""
    assert not hasattr(PooledReadClient, forbidden)
    assert not hasattr(ResolverPool, forbidden)


def test_the_pooled_client_exposes_exactly_the_declared_reads():
    """The closed set is the guarantee — assert the class matches it."""
    from scout.live.solana.resolver_pool import PROXIED_READS

    public = {
        name
        for name in vars(PooledReadClient)
        if not name.startswith("_") and callable(getattr(PooledReadClient, name, None))
    }
    assert public == set(PROXIED_READS)


async def test_the_pooled_client_forwards_only_reads(pool_settings):
    """No ``__getattr__`` passthrough: a send method on the underlying client
    is still unreachable through the pool."""

    class WithASendMethod(FakeEndpoint):
        async def send_transaction(self, _tx):  # pragma: no cover - never callable
            raise AssertionError("the pool reached a submission method")

    pool = _pool(pool_settings, WithASendMethod(**_absent_and_expired()))
    client = pool.read_client(_SIGNATURE)
    assert not hasattr(client, "send_transaction")
    with pytest.raises(AttributeError):
        client.send_transaction  # noqa: B018


def test_the_pool_module_invokes_no_broadcast_rpc_method():
    """AST walk over every method the module calls on an endpoint client.

    A grep cannot tell the module's own prose (which names the broadcast
    methods in order to explain their absence) from a call site.
    """
    import scout.live.solana.resolver_pool as module

    tree = ast.parse(inspect.getsource(module))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)

    banned = {
        "send" + "_transaction",
        "send" + "Transaction",
        "submit" + "_transaction",
        "send" + "_raw_transaction",
    }
    assert not (
        called & banned
    ), f"broadcast reachable from the pool: {called & banned}"
    assert "get_signature_statuses" in called  # sanity: the walk found real calls


# ======================================================================
# The pool's refusal is final — the age policy cannot re-derive it
# ======================================================================
def test_the_age_policy_cannot_re_derive_a_verdict_the_pool_refused(settings_factory):
    """*** The bypass this guard exists to close. ***

    The age rule reads absence off the probe trail and can upgrade
    `unresolved` to definitive on its own. The pool's objection is about what
    those probes are WORTH, so without the guard the pool's refusal would be
    silently undone one function later.
    """
    from scout.live.solana.resolver import ResolutionProbe, ResolutionReport
    from scout.live.solana_lane import apply_age_policy

    settings = settings_factory()
    report = ResolutionReport(
        verdict="unresolved",
        signature=_SIGNATURE,
        last_valid_block_height=None,
        probes=(
            ResolutionProbe(sweep=0, outcome="absent", blockhash_expired=None),
            ResolutionProbe(sweep=1, outcome="absent", blockhash_expired=None),
        ),
        checked_at="2026-08-01T00:00:00+00:00",
    )
    # Old enough, cleanly absent, no evidence height: exactly the shape the age
    # rule upgrades.
    without_guard = apply_age_policy(
        report, age_seconds=7200, settings=settings, had_evidence_height=False
    )
    assert without_guard.verdict == "definitively_not_submitted"

    with_guard = apply_age_policy(
        report,
        age_seconds=7200,
        settings=settings,
        had_evidence_height=False,
        pool_downgrade="split_across_endpoints",
    )
    assert with_guard.verdict == "unresolved"
    assert with_guard.reason == "split_across_endpoints"
    assert with_guard.clears_the_lane is False


def test_a_landed_verdict_still_passes_the_pool_guard(settings_factory):
    """The guard must not turn positive evidence into an escalation."""
    from scout.live.solana.resolver import ResolutionReport
    from scout.live.solana_lane import apply_age_policy

    report = ResolutionReport(
        verdict="landed",
        signature=_SIGNATURE,
        last_valid_block_height=_LAST_VALID,
        probes=(),
        checked_at="2026-08-01T00:00:00+00:00",
    )
    verdict = apply_age_policy(
        report,
        age_seconds=10,
        settings=settings_factory(),
        had_evidence_height=True,
        pool_downgrade="split_across_endpoints",
    )
    assert verdict.verdict == "landed"


# ======================================================================
# Balances stay supplementary
# ======================================================================
async def test_balances_read_through_the_pool_do_not_decide_the_verdict(pool_settings):
    class WithBalances(FakeEndpoint):
        async def get_token_balance(self, _owner, mint, **_kw):
            return 9_000_000 if mint == USDC_MINT else 0

    pool = _pool(
        pool_settings,
        WithBalances(
            statuses=[SignatureStatus(known=False)], heights=[_LAST_VALID - 1]
        ),
    )
    resolution = await _resolve(
        pool, pool_settings, owner_pubkey="7v54NWdBtkjuAFJrLGsS2SXnuk8nKam81mZJeeYxVFi9"
    )

    # A big USDC balance looks like a landed swap. The verdict ignores it.
    assert resolution.report.balances["output_token"] == 9_000_000
    assert resolution.report.verdict == "unresolved"
