"""Resolver endpoint pool — several nodes, one consistent view per verdict.

*** NOTHING HERE CAN BROADCAST. ***
Every endpoint in this pool is a ``SolanaRpcClient``, which has no send method
at all (see ``rpc_client``), and ``PooledReadClient`` below re-exports only
read methods by name. Adding endpoints widens what the lane can READ; it does
not widen what it can SEND. Jito remains the sole submission path.

Why a pool at all
-----------------
The §7 resolver's dangerous verdict is ``definitively_not_submitted``: it is
the only one that clears the lane and licenses a rerun. It is reached by
combining two facts — the signature is absent, AND the block height has passed
``lastValidBlockHeight`` — and BOTH facts are read from an endpoint. A single
endpoint is therefore a single point of failure in the one direction that
costs money: if it is down the lane blocks on ``unresolved`` forever, and if it
is wrong the lane retires a row for a transaction that landed.

Four mechanisms, each answering a different way that goes wrong:

**Genesis validation** answers *is this endpoint even on our chain?* A devnet
or forked node answers "absent" to every mainnet signature, which is exactly
the input that manufactures a false definitive. A URL cannot prove which chain
it serves; ``getGenesisHash`` can, and an endpoint whose genesis is not
mainnet-beta is excluded rather than trusted.

**Health and latency** answer *is it caught up?* A lagging node is behind on
both facts, and being behind on block height while missing a signature from
its cache is the precise shape of a false definitive. ``getHealth`` reports lag
as a JSON-RPC error, and the elapsed time of that call is the latency sample.

**Deterministic selection** answers *did both sweeps look at the same node?*
The resolver takes two sweeps either side of a settle delay specifically so an
in-flight transaction has time to appear. Splitting those sweeps across two
nodes destroys that: sweep 1 asks node A, the transaction lands, sweep 2 asks
node B which has not caught up, and the pair reads as a clean double absence.
So one resolution pins to one endpoint, chosen by hashing a key that is stable
for that resolution (the signature). Failover is possible but never silent —
``PooledReadClient.single_view`` reports whether the verdict-bearing reads all
came from one node, and the caller downgrades the verdict when they did not.

**Cross-endpoint corroboration** answers *is one node's word enough?* Only for
the definitive absence. A ``landed`` verdict is positive evidence — a node that
HAS the signature is not inventing it, and a second opinion cannot make a
landed transaction un-land. An absence is the opposite: it is the absence of
evidence, and any single node's absence is compatible with the transaction
existing on the chain. So a definitive absence is put to a second endpoint
before it is acted on, and disagreement collapses it to ``unresolved``.

A single-endpoint deployment is fully supported and is what ships first: with
one endpoint corroboration reports ``attempted=False`` and the verdict stands
unchanged. Adding Helius or QuickNode later is a config edit, not a code
change — which is the whole point of building it now.

Secrets
-------
Provider endpoint URLs carry the API key IN THE URL (Alchemy and Helius put it
in the path, QuickNode in the host, some in the query string). No URL from this
module ever reaches a log, an evidence file, an exception message or a
traceback — every one of them is labelled through ``redact_endpoint`` first.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit

import aiohttp
import structlog

from scout.config import Settings
from scout.live.solana.constants import SOLANA_MAINNET_GENESIS_HASH
from scout.live.solana.resolver import ResolutionReport, resolve_submission
from scout.live.solana.rpc_client import SolanaRpcClient

log = structlog.get_logger(__name__)

# The closed set of methods ``PooledReadClient`` forwards. Every one is a read.
# Named here rather than left implicit in the class body so a test can assert
# the class exposes these and NOTHING else — the guarantee is that a submission
# method appearing on an endpoint client stays unreachable through the pool.
#
# The admission probes (``get_genesis_hash`` / ``get_health``) are deliberately
# absent: they are asked of ONE named endpoint and must not fail over, because
# a health answer that came from a different node is an answer to a different
# question.
PROXIED_READS = (
    "get_signature_statuses",
    "get_block_height",
    "get_balance",
    "get_token_balance",
    "get_transaction",
)

# Reads whose answers feed a VERDICT, as opposed to the supplementary balance
# reads. Only these have to come from one consistent view of the chain — a
# balance read from a second node is evidence for a human either way.
_VERDICT_READS = frozenset({"get_signature_statuses", "get_block_height"})


def redact_endpoint(url: str) -> str:
    """A stable, non-secret label for an endpoint URL.

    ``host[:port]#<8 hex>``. The host is safe to show and is what an operator
    recognises; the fingerprint distinguishes two endpoints on the same host
    without revealing what differs, since what differs is usually the API key.

    Everything after the host is dropped rather than masked. Alchemy and Helius
    carry the key in the PATH and some providers in the query string, so a
    "mask the query string" rule would leak exactly the deployment this lane
    runs against.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        host, port = "", ""
    fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{host or 'unparseable'}{port}#{fingerprint}"


def resolver_urls(settings: Settings) -> list[str]:
    """The resolver's endpoints, in preference order, de-duplicated.

    Three sources, most specific first:

    1. ``SOLANA_RESOLVER_RPC_URLS`` — the ordered pool.
    2. ``SOLANA_RESOLVER_RPC_URL`` — the singular key, read as a one-element
       pool. It is what is DEPLOYED, so it stays authoritative rather than
       becoming a legacy alias nobody remembers to migrate.
    3. ``SOLANA_RPC_URL`` — the general endpoint, accepted only when it is not
       itself a known round-robin (that check lives in ``solana_lane``, which
       owns the refusal).

    Order is preserved because it is a preference order for failover. Exact
    duplicates are dropped: two identical URLs are one node, and treating them
    as two would let "corroborated by a second endpoint" mean nothing.
    """
    configured = [
        u.strip() for u in (settings.SOLANA_RESOLVER_RPC_URLS or []) if u.strip()
    ]
    if not configured:
        singular = (settings.SOLANA_RESOLVER_RPC_URL or "").strip()
        configured = [singular] if singular else []
    if not configured:
        configured = [settings.SOLANA_RPC_URL]

    seen: set[str] = set()
    ordered: list[str] = []
    for url in configured:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


@dataclass(frozen=True)
class ResolverEndpoint:
    """One node in the pool. ``url`` is secret; ``label`` is what gets logged."""

    url: str
    label: str
    index: int
    client: Any

    @classmethod
    def build(cls, url: str, index: int, client: Any) -> "ResolverEndpoint":
        return cls(url=url, label=redact_endpoint(url), index=index, client=client)


@dataclass(frozen=True)
class EndpointHealth:
    """What one endpoint proved about itself. Fail-closed on every axis.

    ``usable`` is False unless the endpoint POSITIVELY proved it is on
    mainnet-beta and caught up. An endpoint we could not reach is not usable —
    "we could not check" and "it checked out" must never render the same.
    """

    label: str
    index: int
    usable: bool
    genesis_ok: bool
    genesis_hash: str | None = None
    latency_ms: float | None = None
    degraded: bool = False
    detail: str = ""

    def as_evidence(self) -> dict[str, Any]:
        return {
            "endpoint": self.label,
            "index": self.index,
            "usable": self.usable,
            "genesis_ok": self.genesis_ok,
            "genesis_hash": self.genesis_hash,
            "latency_ms": self.latency_ms,
            "degraded": self.degraded,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CorroborationReport:
    """A second endpoint's opinion on a definitive absence.

    ``attempted=False`` means there was no second endpoint to ask. That is a
    supported deployment, not a failure: the verdict stands and the evidence
    says it was uncorroborated, so nobody later reads single-node confidence
    into a package that never had it.
    """

    attempted: bool
    agrees: bool | None = None
    endpoint: str | None = None
    observed: str | None = None
    detail: str = ""

    def as_evidence(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "agrees": self.agrees,
            "endpoint": self.endpoint,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PoolResolution:
    """A resolution plus everything about HOW the pool reached it.

    ``report`` already carries the pool-adjusted verdict, so callers that only
    care about the verdict need no pool awareness at all. ``downgrade_reason``
    is non-None exactly when the pool overruled the raw resolver, and it MUST
    be passed on to the lane's age policy — otherwise the age rule can quietly
    re-derive the definitive verdict the pool just refused.
    """

    report: ResolutionReport
    raw_verdict: str
    endpoint: str | None
    endpoints_used: tuple[str, ...]
    single_view: bool
    corroboration: CorroborationReport
    downgrade_reason: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "resolver_endpoint": self.endpoint,
            "resolver_endpoints_used": list(self.endpoints_used),
            "resolver_single_view": self.single_view,
            "resolver_raw_verdict": self.raw_verdict,
            "resolver_pool_downgrade": self.downgrade_reason,
            "corroboration": self.corroboration.as_evidence(),
        }


class PooledReadClient:
    """Read-only proxy over the pool: deterministic start, sticky, failover.

    *** NO SUBMISSION METHOD, AND MUST NEVER GAIN ONE. ***
    It forwards a closed list of READ methods (``_PROXIED_READS``) and nothing
    else — there is no ``__getattr__`` passthrough, so a send method appearing
    on the underlying client would still be unreachable here.

    Sticky, not round-robin. The first endpoint that answers is pinned for the
    rest of this client's life, so the resolver's two sweeps look at one node
    unless that node stops answering. When it does, the next endpoint in the
    rotation is tried — reads move nothing, so failing over is safe — and the
    departure is RECORDED. ``single_view`` is how the caller learns the sweeps
    were split, which is grounds to refuse a definitive absence.
    """

    def __init__(self, endpoints: Sequence[ResolverEndpoint]) -> None:
        if not endpoints:
            raise ValueError("PooledReadClient needs at least one endpoint")
        self._order = tuple(endpoints)
        self._pinned: ResolverEndpoint = self._order[0]
        self._verdict_views: list[str] = []
        self._all_views: list[str] = []
        self._failures: list[dict[str, str]] = []

    @property
    def pinned(self) -> ResolverEndpoint:
        return self._pinned

    @property
    def endpoints_used(self) -> tuple[str, ...]:
        """Labels that answered, in first-use order."""
        seen: list[str] = []
        for label in self._all_views:
            if label not in seen:
                seen.append(label)
        return tuple(seen)

    @property
    def single_view(self) -> bool:
        """Did every VERDICT-bearing read come from one endpoint?

        False means the sweeps were split, and a definitive absence assembled
        from two nodes is not evidence — a transaction can be absent at node A
        and present at node B for no reason more sinister than propagation.
        """
        return len(set(self._verdict_views)) <= 1

    @property
    def failures(self) -> tuple[dict[str, str], ...]:
        return tuple(self._failures)

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Try the pinned endpoint, then the rest of the rotation.

        The LAST exception is re-raised when nothing answered, so the caller
        (the resolver) records a probe error and reaches ``unresolved`` — the
        fail-closed reading of "we could not look" — rather than an absence.
        """
        start = self._order.index(self._pinned)
        rotation = self._order[start:] + self._order[:start]
        last: Exception | None = None
        for endpoint in rotation:
            try:
                result = await getattr(endpoint.client, method)(*args, **kwargs)
            except Exception as exc:
                last = exc
                self._failures.append(
                    {
                        "endpoint": endpoint.label,
                        "method": method,
                        "error_type": type(exc).__name__,
                    }
                )
                log.warning(
                    "solana_resolver_endpoint_failed",
                    endpoint=endpoint.label,
                    method=method,
                    error_type=type(exc).__name__,
                )
                continue
            if endpoint.label != self._pinned.label:
                log.warning(
                    "solana_resolver_failover",
                    from_endpoint=self._pinned.label,
                    to_endpoint=endpoint.label,
                    method=method,
                )
                self._pinned = endpoint
            self._all_views.append(endpoint.label)
            if method in _VERDICT_READS:
                self._verdict_views.append(endpoint.label)
            return result
        assert last is not None  # the rotation is never empty
        raise last

    async def get_signature_statuses(self, signatures: list[str], **kwargs: Any) -> Any:
        return await self._call("get_signature_statuses", signatures, **kwargs)

    async def get_block_height(self, **kwargs: Any) -> int:
        return await self._call("get_block_height", **kwargs)

    async def get_balance(self, pubkey: str, **kwargs: Any) -> int:
        return await self._call("get_balance", pubkey, **kwargs)

    async def get_token_balance(self, owner: str, mint: str, **kwargs: Any) -> int:
        return await self._call("get_token_balance", owner, mint, **kwargs)

    async def get_transaction(self, signature: str, **kwargs: Any) -> Any:
        return await self._call("get_transaction", signature, **kwargs)


class ResolverPool:
    """The lane's resolver endpoints, validated and selected deterministically.

    Construct with ``from_settings`` in production; ``from_clients`` exists for
    tests and for the single-client wiring the runner falls back to.
    """

    def __init__(self, endpoints: Sequence[ResolverEndpoint], settings: Settings):
        if not endpoints:
            raise ValueError("a resolver pool needs at least one endpoint")
        self._endpoints = tuple(endpoints)
        self._settings = settings
        self._health: tuple[EndpointHealth, ...] | None = None

    # ---------------- construction ----------------
    @classmethod
    def from_settings(
        cls, settings: Settings, session: aiohttp.ClientSession
    ) -> "ResolverPool":
        """One ``SolanaRpcClient`` per configured URL, sharing one session.

        Each client is built from a settings copy whose ``SOLANA_RPC_URL`` is
        that endpoint — the same mechanism the runner already used for its
        single resolver client, so an endpoint client is a read-only client by
        construction and cannot become anything else here.
        """
        endpoints = [
            ResolverEndpoint.build(
                url,
                index,
                SolanaRpcClient(
                    settings.model_copy(update={"SOLANA_RPC_URL": url}), session
                ),
            )
            for index, url in enumerate(resolver_urls(settings))
        ]
        return cls(endpoints, settings)

    @classmethod
    def from_clients(
        cls, settings: Settings, clients: Sequence[tuple[str, Any]]
    ) -> "ResolverPool":
        """Build from ``(url, client)`` pairs already constructed."""
        return cls(
            [
                ResolverEndpoint.build(url, index, client)
                for index, (url, client) in enumerate(clients)
            ],
            settings,
        )

    # ---------------- inspection ----------------
    @property
    def endpoints(self) -> tuple[ResolverEndpoint, ...]:
        return self._endpoints

    @property
    def size(self) -> int:
        return len(self._endpoints)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(e.label for e in self._endpoints)

    @property
    def health(self) -> tuple[EndpointHealth, ...] | None:
        """The last ``check_health`` result, or None if it was never run."""
        return self._health

    def _usable_indices(self) -> set[int]:
        """Indices that may serve reads. Everything, until health says less.

        Before a health check has run, no endpoint has been DISPROVEN and the
        pool behaves exactly as it did with one unvalidated URL. After one has
        run, only endpoints that positively proved themselves are used.
        """
        if self._health is None:
            return {e.index for e in self._endpoints}
        return {h.index for h in self._health if h.usable}

    def _degraded_indices(self) -> set[int]:
        return {h.index for h in self._health if h.degraded} if self._health else set()

    def _preference_order(self) -> tuple[ResolverEndpoint, ...]:
        """Every usable endpoint, with latency-degraded ones moved to the back.

        Degraded means "slow, but on the right chain and caught up". It stays
        in this order because failover and corroboration would rather use a
        slow node than none — an unresolvable signature blocks the lane, and
        that is the worse outcome by a wide margin.
        """
        usable = self._usable_indices()
        degraded = self._degraded_indices()
        candidates = [e for e in self._endpoints if e.index in usable]
        return tuple(sorted(candidates, key=lambda e: (e.index in degraded, e.index)))

    def _selection_order(self) -> tuple[ResolverEndpoint, ...]:
        """The endpoints new resolutions are SPREAD across.

        Narrower than the failover order: while a fast endpoint exists, no new
        resolution is routed to a slow one. The slow endpoint is still reachable
        by failover and still eligible to corroborate — being demoted is not
        being excluded.
        """
        order = self._preference_order()
        degraded = self._degraded_indices()
        return tuple(e for e in order if e.index not in degraded) or order

    # ---------------- validation ----------------
    async def check_health(self) -> tuple[EndpointHealth, ...]:
        """Prove, per endpoint, that it is on mainnet-beta and caught up.

        Every endpoint is probed CONCURRENTLY — the pool's whole purpose is to
        survive one node being slow, and probing serially would make the slow
        node cost the same wall-clock time it was added to avoid.

        Genesis first: an endpoint on the wrong chain is excluded whatever its
        health says, because a healthy devnet node is precisely the worst case
        (fast, responsive, and wrong about every signature).
        """
        results = await asyncio.gather(
            *(self._probe(endpoint) for endpoint in self._endpoints)
        )
        self._health = tuple(results)
        for entry in results:
            log.info("solana_resolver_endpoint_health", **entry.as_evidence())
        if not any(entry.usable for entry in results):
            log.error(
                "solana_resolver_pool_unusable",
                endpoints=list(self.labels),
                reason="no endpoint proved it is on mainnet-beta and caught up",
            )
        return self._health

    async def _probe(self, endpoint: ResolverEndpoint) -> EndpointHealth:
        """One endpoint's genesis + health + latency. Never raises."""
        timeout = float(self._settings.SOLANA_RESOLVER_HEALTH_TIMEOUT_SEC)
        max_latency = float(self._settings.SOLANA_RESOLVER_MAX_LATENCY_MS)

        try:
            genesis = await asyncio.wait_for(
                endpoint.client.get_genesis_hash(), timeout=timeout
            )
        except Exception as exc:
            # The URL is deliberately absent from this detail: an endpoint that
            # fails is exactly the one whose error text an operator pastes into
            # a ticket, and the URL carries the API key.
            return EndpointHealth(
                label=endpoint.label,
                index=endpoint.index,
                usable=False,
                genesis_ok=False,
                detail=f"genesis probe failed ({type(exc).__name__})",
            )

        genesis_ok = str(genesis) == SOLANA_MAINNET_GENESIS_HASH
        if not genesis_ok:
            return EndpointHealth(
                label=endpoint.label,
                index=endpoint.index,
                usable=False,
                genesis_ok=False,
                genesis_hash=str(genesis),
                detail=(
                    "endpoint is NOT on Solana mainnet-beta; its genesis hash is "
                    f"{genesis}, expected {SOLANA_MAINNET_GENESIS_HASH}. A node on "
                    "another chain reports every mainnet signature as absent."
                ),
            )

        started = time.monotonic()
        try:
            await asyncio.wait_for(endpoint.client.get_health(), timeout=timeout)
        except Exception as exc:
            return EndpointHealth(
                label=endpoint.label,
                index=endpoint.index,
                usable=False,
                genesis_ok=True,
                genesis_hash=str(genesis),
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                detail=f"health probe failed ({type(exc).__name__})",
            )
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        degraded = latency_ms > max_latency
        return EndpointHealth(
            label=endpoint.label,
            index=endpoint.index,
            usable=True,
            genesis_ok=True,
            genesis_hash=str(genesis),
            latency_ms=latency_ms,
            degraded=degraded,
            detail=(
                f"slow: {latency_ms}ms over the {max_latency}ms budget — usable, "
                "but demoted behind the faster endpoints"
                if degraded
                else "ok"
            ),
        )

    # ---------------- selection ----------------
    def select(self, key: str) -> ResolverEndpoint:
        """The endpoint that serves ``key``. Same key, same endpoint, always.

        Consistent hashing over the usable endpoints in preference order. The
        determinism is the point: one resolution passes one key (the signature)
        and therefore keeps both of its sweeps on one node's view of the chain.

        Spreading by key rather than always taking the first endpoint is what
        makes a second endpoint carry real load — and therefore real evidence
        that it works — instead of being an untested standby that first gets
        exercised during an incident.
        """
        order = self._selection_order() or self._endpoints
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return order[int.from_bytes(digest[:8], "big") % len(order)]

    def rotation(self, key: str) -> tuple[ResolverEndpoint, ...]:
        """Failover order for ``key``: its endpoint first, then the rest."""
        order = self._preference_order() or self._endpoints
        chosen = self.select(key)
        position = next((i for i, e in enumerate(order) if e.label == chosen.label), 0)
        return order[position:] + order[:position]

    def read_client(self, key: str) -> PooledReadClient:
        """A read proxy pinned to ``key``'s endpoint, failing over in rotation."""
        return PooledReadClient(self.rotation(key))

    # ---------------- corroboration ----------------
    async def corroborate(
        self,
        *,
        signature: str,
        last_valid_block_height: int | None,
        exclude: str | None,
    ) -> CorroborationReport:
        """Ask a DIFFERENT endpoint whether the definitive absence holds.

        Only ever called for ``definitively_not_submitted``. A landed verdict
        does not come here: presence is positive evidence, and no second
        opinion can turn a transaction that is on the chain into one that is
        not.

        Agreement requires the second endpoint to independently see BOTH facts
        — signature absent AND blockhash expired. Anything else disagrees,
        including a probe error: an endpoint we could not reach has not
        corroborated anything, and treating silence as assent would make the
        corroboration step decorative.
        """
        others = [e for e in self._preference_order() if e.label != exclude]
        if not others:
            return CorroborationReport(
                attempted=False,
                endpoint=None,
                detail=(
                    "no second resolver endpoint is configured; the verdict rests "
                    "on one node. Add one to SOLANA_RESOLVER_RPC_URLS to corroborate."
                ),
            )

        endpoint = others[0]
        try:
            statuses = await endpoint.client.get_signature_statuses(
                [signature], search_transaction_history=True
            )
        except Exception as exc:
            return CorroborationReport(
                attempted=True,
                agrees=False,
                endpoint=endpoint.label,
                observed="error",
                detail=f"corroborating probe failed ({type(exc).__name__})",
            )

        status = statuses[0] if statuses else None
        if status is not None and getattr(status, "known", False):
            observed: Literal["landed", "failed_on_chain"] = (
                "landed" if status.err is None else "failed_on_chain"
            )
            return CorroborationReport(
                attempted=True,
                agrees=False,
                endpoint=endpoint.label,
                observed=observed,
                detail=(
                    "the second endpoint HAS this signature — the transaction "
                    "exists and the absence verdict was wrong"
                ),
            )

        if last_valid_block_height is None:
            return CorroborationReport(
                attempted=True,
                agrees=False,
                endpoint=endpoint.label,
                observed="absent_expiry_unknown",
                detail="no lastValidBlockHeight, so expiry cannot be re-derived",
            )

        try:
            height = await endpoint.client.get_block_height()
        except Exception as exc:
            return CorroborationReport(
                attempted=True,
                agrees=False,
                endpoint=endpoint.label,
                observed="error",
                detail=f"corroborating height probe failed ({type(exc).__name__})",
            )

        if height <= last_valid_block_height:
            return CorroborationReport(
                attempted=True,
                agrees=False,
                endpoint=endpoint.label,
                observed="absent_not_expired",
                detail=(
                    f"the second endpoint is at height {height}, still at or below "
                    f"lastValidBlockHeight {last_valid_block_height} — the "
                    "transaction can still land"
                ),
            )

        return CorroborationReport(
            attempted=True,
            agrees=True,
            endpoint=endpoint.label,
            observed="absent_and_expired",
            detail=f"independently absent and expired at height {height}",
        )


async def resolve_with_pool(
    *,
    pool: ResolverPool,
    expected_signature: str,
    last_valid_block_height: int | None,
    settings: Settings,
    owner_pubkey: str | None = None,
) -> PoolResolution:
    """Resolve one signature through the pool, and refuse to over-claim.

    The raw resolver is unchanged and does the deciding — this wraps it with
    the two things a multi-endpoint read has to answer for:

    1. **Split view.** If the verdict-bearing reads did not all come from one
       endpoint, a definitive absence is downgraded to ``unresolved``. Two
       nodes' absences are not one node's double absence.
    2. **Corroboration.** A surviving definitive absence is put to a second
       endpoint. Disagreement collapses it — to ``landed`` /
       ``failed_on_chain`` when the second endpoint actually HAS the signature
       (positive evidence outranks absence), otherwise to ``unresolved``.

    Downgrades only ever move a verdict AWAY from clearing the lane. There is
    no path here that turns ``unresolved`` into something actionable.
    """
    client = pool.read_client(expected_signature)
    report = await resolve_submission(
        expected_signature=expected_signature,
        last_valid_block_height=last_valid_block_height,
        rpc_client=client,
        settings=settings,
        owner_pubkey=owner_pubkey,
    )
    raw_verdict = report.verdict
    downgrade: str | None = None
    corroboration = CorroborationReport(
        attempted=False, detail="not applicable to this verdict"
    )

    if raw_verdict == "definitively_not_submitted":
        if not client.single_view:
            downgrade = "split_across_endpoints"
            report = _with_verdict(report, "unresolved")
        else:
            corroboration = await pool.corroborate(
                signature=expected_signature,
                last_valid_block_height=last_valid_block_height,
                exclude=client.pinned.label,
            )
            if corroboration.attempted and not corroboration.agrees:
                if corroboration.observed in ("landed", "failed_on_chain"):
                    downgrade = "corroborating_endpoint_has_the_signature"
                    report = _with_verdict(report, str(corroboration.observed))
                else:
                    downgrade = f"corroboration_failed:{corroboration.observed}"
                    report = _with_verdict(report, "unresolved")

    resolution = PoolResolution(
        report=report,
        raw_verdict=raw_verdict,
        endpoint=client.pinned.label,
        endpoints_used=client.endpoints_used,
        single_view=client.single_view,
        corroboration=corroboration,
        downgrade_reason=downgrade,
    )
    log.info(
        "solana_resolver_pool_resolution",
        signature=expected_signature,
        raw_verdict=raw_verdict,
        verdict=report.verdict,
        **resolution.as_evidence(),
    )
    return resolution


def _with_verdict(report: ResolutionReport, verdict: str) -> ResolutionReport:
    """A copy of ``report`` carrying the pool's verdict.

    Rebuilt rather than mutated because ``ResolutionReport`` is frozen — the
    probe trail must stay exactly what was observed, so the evidence still
    shows the raw reads that produced the verdict the pool then overruled.
    """
    return ResolutionReport(
        verdict=verdict,  # type: ignore[arg-type]
        signature=report.signature,
        last_valid_block_height=report.last_valid_block_height,
        probes=report.probes,
        checked_at=report.checked_at,
        slot=report.slot,
        confirmation_status=report.confirmation_status,
        on_chain_err=report.on_chain_err,
        balances=report.balances,
    )
