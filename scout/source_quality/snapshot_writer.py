"""Forward-only price-snapshot writer for CA-keyed source calls (design #392 C2).

Selects contract-identity source_calls of ANY source_type within the forward
horizon, dedupes by priceable identity, fetches a *current* price
from GeckoTerminal by contract address (via the injected C0 resolver/fetcher),
and records one source-tagged snapshot per identity per cycle into
``source_call_price_snapshots``. Over successive cycles these accumulate the
forward price series the (separate) C3 pricing hookup will read.

Design guarantees enforced here:

- **Provider failures are observable, never faked.** A ``PriceProviderError``
  from the resolver/fetcher is caught + counted (``provider_errors``); a missing
  pool (``resolve`` -> ``None``) and an empty OHLCV series (``fetch`` -> ``[]``)
  are counted *separately* (``pools_unresolved`` / ``empty_ohlcv``). In all three
  cases **no snapshot row is written** — no invented price.
- **GT-only, source-tagged.** Every snapshot stores its ``source`` (``gt`` from
  C0). DexScreener fallback is a later concern; this writer never mixes sources.
- **Table+writer only (C2).** This module NEVER writes ``source_calls``
  performance fields — the C3 pricing hookup owns that.

The C0 price functions are taken as injected callables so this module never
imports aiohttp and stays unit-testable without network; the cron script wires
the real ``resolve_pool_address`` / ``fetch_pool_ohlcv``.

Stage B Option 3 — CG coin_id lane
----------------------------------
A second identity kind is written here: genuine CoinGecko ``coin_id``
identities, stored under ``identity_kind = 'coin_id'`` (see the naming
reconciliation below). These are priced from the local ``price_cache`` surface
the pipeline already maintains, so the lane adds **zero** provider requests and
does not touch the contract lane's budget.

It exists because the identities it covers are the MAJORITY of priceable TG
calls, and the only other source for them — ``gainers_snapshots`` /
``losers_snapshots`` — carries a 7-day retention DELETE. A trailing-30d decision
surface cannot be reconstructed from a table that deletes its own history, which
is why those were rejected as Stage B substrate.

RETENTION / LIFECYCLE INVARIANT
-------------------------------
``source_call_price_snapshots`` is **append-only and unpruned**. No DELETE or
prune path may target it, and a source-text pin in the tests enforces that.

The constraint is not "keep everything forever" — it is that pruning is bounded
by the *active feature/generation evidence lifecycle*: **no raw observation
needed to reproduce an active generation's historical decision may be deleted
before that lifecycle closes.** A retention policy may be added later only once
it can demonstrate that the observations it removes cannot be required to
reconstruct any still-active generation's decision surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Awaitable, Callable

import aiosqlite
import structlog

from scout.exceptions import PriceProviderError
from scout.source_quality.ledger import _priceable_identity, parse_utc

_log = structlog.get_logger()

# Widest forward window end: the 24h window closes at call+28h (ledger WINDOWS),
# so a call older than this can never gain a new in-window forward snapshot.
DEFAULT_HORIZON_HOURS = 28

# Provider calls per identity: one pool resolve + one OHLCV fetch.
_REQUESTS_PER_IDENTITY = 2

# Activation ceilings. GeckoTerminal's keyless public tier is ~30 req/min per
# IP and is SHARED with the DEX-discovery lane on this host, so the per-run cap
# is set well inside that budget: 25 identities = 50 provider calls, spread over
# the run rather than bursted. Measured steady-state selection is 1-2 identities
# per run, so this is ~15x headroom over observed load, not a throttle.
DEFAULT_MAX_IDENTITIES_PER_RUN = 25
DEFAULT_MAX_REQUESTS_PER_DAY = 2000

# THE NAMING RECONCILIATION. Two different strings name the same concept:
#   * the SCHEMA CHECK on source_call_price_snapshots permits 'coin_id'
#   * `ledger._priceable_identity` emits 'token_id'
# Storing under one and reading under the other returns ZERO ROWS SILENTLY —
# no constraint fires, no error, the series is simply always empty. So the two
# are reconciled here, once, and pinned by a test that fails on a mismatch
# rather than returning nothing.
#
# 'coin_id' is canonical for STORAGE because the schema CHECK already permits
# exactly that and this PR alters no DDL. 'token_id' remains the ledger's
# in-memory kind. Anything writing or reading a CG identity in this table must
# go through these two names.
LEDGER_CG_KIND = "token_id"
STORAGE_CG_KIND = "coin_id"

# Kind translation, ledger-side -> storage-side. Exported because the constants
# ALONE are one-sided: a reader holding the ledger's 'token_id' and querying the
# table with it still gets zero rows, silently. Anything reading this table for
# an identity that came out of `_priceable_identity` should route through here.
_LEDGER_TO_STORAGE_KIND = {
    LEDGER_CG_KIND: STORAGE_CG_KIND,
    "contract": "contract",
}


def storage_kind_for(ledger_kind: str) -> str:
    """Storage ``identity_kind`` for a kind emitted by ``_priceable_identity``.

    Raises on an unknown kind rather than passing it through: a silently
    unmapped kind is exactly the zero-rows failure this indirection exists to
    prevent, and a new ledger kind should fail loudly here the day it appears.
    """
    try:
        return _LEDGER_TO_STORAGE_KIND[ledger_kind]
    except KeyError:
        raise ValueError(
            f"no storage identity_kind mapped for ledger kind {ledger_kind!r}; "
            f"known: {sorted(_LEDGER_TO_STORAGE_KIND)}"
        ) from None


# Per-run ceiling for the CG lane, mirroring the contract lane's. The CG lane
# spends no provider requests, so this is a WRITE-RATE bound, not a budget one:
# without it the row rate is bounded only by open-call coverage, which is 110
# calls x 96 runs/day = ~10.5k rows/day worst case. 32 sits above the measured
# steady state (14 distinct CG identities in the open window on 2026-08-13),
# so it defers nothing today while capping the pathological case at
# 32 x 96 = ~3.1k rows/day. Deferred identities are picked up next run — they
# stay inside the forward horizon, so nothing is lost.
DEFAULT_MAX_CG_IDENTITIES_PER_RUN = 32

# price_cache rows older than this are not treated as observations. The cache is
# a latest-value-per-coin surface refreshed by the pipeline, NOT a time series;
# measured on prod 2026-08-13, 10 of the 13 covered call identities were under
# an hour old (most ~1 minute) while one sat 3.3 days stale. Snapshotting the
# stale one would record a days-old price as a fresh observation.
DEFAULT_MAX_PRICE_CACHE_AGE_MIN = 90

# (*, chain, contract_address) -> pool-like | None ; pool-like has .network + .pool_address
PoolResolver = Callable[..., Awaitable[Any]]
# (*, network, pool_address) -> list of candle-like (ascending) ; candle has .close + .source
OhlcvFetcher = Callable[..., Awaitable[list[Any]]]


def _is_rate_limited(exc: PriceProviderError) -> bool:
    """True when a provider failure is a 429.

    ``scout.ingestion.gt_ohlcv`` raises with ``reason=f"http_{status}"`` after
    exhausting its own backoff, so a surfaced 429 means the retry budget was
    already spent — i.e. real, sustained pressure, not a single blip.
    """
    return "429" in str(getattr(exc, "reason", ""))


async def _requests_used_today(conn: aiosqlite.Connection, *, now: datetime) -> int:
    """GT requests already spent today, from the persisted run counters.

    Each processed identity costs at most ``_REQUESTS_PER_IDENTITY`` provider
    calls (pool resolve + OHLCV fetch), so ``identities_seen`` is the honest
    upper bound on spend. Deliberately an OVER-estimate: the daily ceiling
    should trip early rather than late.
    """
    day_start = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    cur = await conn.execute(
        "SELECT COALESCE(SUM(identities_seen), 0) FROM source_call_price_snapshot_runs "
        "WHERE ran_at >= ?",
        (day_start,),
    )
    row = await cur.fetchone()
    return int(row[0] or 0) * _REQUESTS_PER_IDENTITY


async def _last_snapshot_at(
    conn: aiosqlite.Connection, *, identity_kind: str, identity_key: str
) -> str | None:
    cur = await conn.execute(
        "SELECT MAX(snapshot_at) FROM source_call_price_snapshots "
        "WHERE identity_kind = ? AND identity_key = ?",
        (identity_kind, identity_key),
    )
    row = await cur.fetchone()
    return row[0] if row else None


# The as-of visibility predicate, in ONE place. Any reader that answers a
# question about the PAST must apply it; readers that report coverage must NOT.
#
# WHY THE SPLIT MATTERS, stated here because this is where the next reader
# author will look:
#   * DECISION / AS-OF readers (the ledger's price series, the Stage B caller-
#     feature provider) reconstruct what was knowable at an instant. Skipping
#     the gate lets them read a price the decision could not have seen —
#     future leakage, the defect this exists to close.
#   * COVERAGE / OBSERVABILITY readers (the snapshot watchdogs) answer "does
#     this identity have data at all". Applying the gate there would make
#     coverage UNDER-REPORT during the marker-lag window and page an operator
#     about a lane that is working correctly.
#
# CANONICAL TIMESTAMP-FORMAT RULE — read this before adding a bound.
# Four timestamps meet in this predicate and they have THREE producers:
#   snapshot_at   Python .isoformat()      ('2026-08-14T02:53:45.340209+00:00')
#   created_at    SQLite datetime('now')   ('2026-08-14 02:53:45')
#   visible_at    Python .isoformat()      (written by `_publish_batch`)
#   as_of         caller-supplied
# SQLite compares these as TEXT, and ' ' (0x20) sorts BEFORE 'T' (0x54). So a
# space-separated value is lexicographically EARLIER than a 'T'-separated one for
# the same instant — which silently inverts a bound (INF-04; see
# scout/timeutil.py). THE RULE: every comparison pair must share ONE producer.
#   b.visible_at <= :as_of        -> both Python isoformat; callers MUST pass
#                                    `.isoformat()`, never a SQLite-shaped string
#   s.created_at <= epoch_cutover -> both SQLite datetime('now'); the epoch row
#                                    is stamped by SQLite for exactly this reason
# Do NOT "fix" a future mismatch by wrapping a side in datetime(): that truncates
# sub-second precision and rounds marker comparisons the admitting way.
#
# `:as_of` is bound twice. The epoch subquery COALESCEs to '' — FAIL CLOSED: a DB
# missing the epoch row (migration not run, row deleted) admits NO null-batch row
# rather than all of them.
#
# THE TWO BOUNDS LEAN THE SAME WAY, which is why they differ in strictness:
#   visible_at <= as_of      INCLUSIVE — a marker committed exactly at as_of was
#                            knowable then; admitting it leaks nothing.
#   created_at <  epoch      EXCLUSIVE — `datetime('now')` has ONE-SECOND
#                            resolution, so a row written in the same second as
#                            the migration compares EQUAL to the cutover. Under
#                            `<=` that row would be grandfathered, i.e. treated
#                            as always-visible, which is the admitting direction.
#                            `<` costs at most one second of genuinely-old rows
#                            (they become invisible) and never admits a
#                            post-epoch row. Conservative both times.
VISIBLE_AS_OF_SQL = """
        (
            s.batch_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM source_call_snapshot_batches b
                WHERE b.batch_id = s.batch_id AND b.visible_at <= :as_of
            )
            OR (
                s.batch_id IS NULL
                AND s.created_at < COALESCE(
                    (SELECT epoch_cutover_ts
                     FROM source_call_snapshot_visibility_epoch WHERE id = 1),
                    ''
                )
            )
        )
"""


async def _allocate_batch_id(conn: aiosqlite.Connection) -> int:
    """Next batch id, WITHOUT publishing it.

    Allocated from the snapshots table (the highest id ever stamped onto data)
    rather than from the batches table, because a crashed cycle leaves a stamped
    id with no batch row — reusing that id would retroactively publish the
    crashed cycle's orphan rows under a later cycle's marker, which is precisely
    the early-visibility this mechanism exists to prevent.
    """
    cur = await conn.execute(
        "SELECT COALESCE(MAX(batch_id), 0) FROM source_call_price_snapshots"
    )
    highest_stamped = (await cur.fetchone())[0] or 0
    cur = await conn.execute(
        "SELECT COALESCE(MAX(batch_id), 0) FROM source_call_snapshot_batches"
    )
    highest_published = (await cur.fetchone())[0] or 0
    return max(highest_stamped, highest_published) + 1


async def _publish_batch(
    conn: aiosqlite.Connection, *, batch_id: int, visible_at: str, rows_written: int
) -> None:
    """Write the visibility marker in its OWN transaction, AFTER the data commit.

    The separation is the mechanism, not an implementation detail: until this
    row is committed, every snapshot carrying ``batch_id`` is unknowable to an
    as-of reader, however long ago it was inserted. Called only when the cycle
    actually wrote rows — an empty cycle publishes nothing and leaves the id
    free for the next one.
    """
    # PLAIN INSERT, not INSERT OR IGNORE. Two concurrent cycles can allocate
    # the same id; OR IGNORE would silently DISCARD the second marker, leaving
    # the later cycle's rows published under the earlier cycle's (earlier)
    # visible_at — backdated visibility, i.e. exactly the future leakage this
    # mechanism exists to prevent. A collision must be LOUD. The .sh wrapper
    # also takes an flock so the collision is prevented rather than merely
    # detected.
    await conn.execute(
        "INSERT INTO source_call_snapshot_batches "
        "(batch_id, visible_at, rows_written) VALUES (?, ?, ?)",
        (batch_id, visible_at, rows_written),
    )
    await conn.commit()


async def _eligible_cg_identities(
    conn: aiosqlite.Connection,
    coin_ids: list[str],
    *,
    now: datetime,
    max_cache_age_min: int,
    stats: dict[str, int],
) -> list[str]:
    """Drop identities that CANNOT produce an observation, before the cap.

    Rotation alone is not enough. A never-OBSERVABLE identity — no ``price_cache``
    row, an unparseable timestamp, or a row perpetually beyond the freshness
    bound — is also never-OBSERVED, so it sorts into the rank-0
    bootstrap bucket and sits at the head of the rotation forever. With as few as
    ``cap`` such identities the observable lane stalls COMPLETELY, and the only
    symptom is a rising ``cg_no_cache_row`` that reads as innocent.

    The accumulator makes this worse over time rather than better: a
    ``price_cache`` row freezes once its token drops out of the ranked universe,
    so perpetually-stale residents build up.

    Filtering here means the cap bounds only work that can actually yield rows.

    COUNTING AUTHORITY: this is the single counting site for the identities it
    excludes — an excluded identity never reaches ``_write_cg_snapshots``, and an
    admitted one is counted only there. So no identity is counted twice, and the
    ``cg_no_cache_row`` ("no row / no price") vs ``cg_stale_cache`` ("row present
    but too old") distinction is preserved at both sites.
    """
    if not coin_ids:
        return []
    placeholders = ",".join("?" for _ in coin_ids)
    cur = await conn.execute(
        "SELECT coin_id, current_price, updated_at FROM price_cache "
        f"WHERE coin_id IN ({placeholders})",
        tuple(coin_ids),
    )
    cached = {row["coin_id"]: row for row in await cur.fetchall()}

    eligible: list[str] = []
    for coin_id in coin_ids:
        row = cached.get(coin_id)
        if row is None or row["current_price"] is None:
            stats["cg_no_cache_row"] += 1
            continue
        try:
            observed_at = parse_utc(row["updated_at"])
        except ValueError:
            observed_at = None
        if observed_at is None:
            stats["cg_bad_timestamp"] += 1
            continue
        if (now - observed_at).total_seconds() / 60.0 > max_cache_age_min:
            stats["cg_stale_cache"] += 1
            continue
        eligible.append(coin_id)
    return eligible


async def _rotate_cg_identities(
    conn: aiosqlite.Connection, coin_ids: list[str]
) -> list[str]:
    """Order CG identities least-recently-observed first, never-observed FIRST.

    Without this the per-run cap STARVES the tail rather than deferring it:
    ``cg_seen`` comes out of a ``source_calls`` scan whose order is stable across
    runs, so a plain prefix slice picks the SAME identities every cycle and the
    ones past the cap never get a single observation. "Deferred, not dropped" is
    only true if the selection rotates.

    Never-observed identities sort first so a new identity bootstraps its series
    immediately; after that the oldest ``MAX(snapshot_at)`` wins. The identity
    key is the tiebreaker, so the order is deterministic.

    The bootstrap-first rank is safe ONLY because callers hand this an already
    filtered list (:func:`_eligible_cg_identities`): every identity here can
    produce an observation this run, so the rank-0 bucket drains by construction
    and no identity can hold a head slot permanently.
    """
    if not coin_ids:
        return []
    placeholders = ",".join("?" for _ in coin_ids)
    cur = await conn.execute(
        "SELECT identity_key, MAX(snapshot_at) AS last_at "
        "FROM source_call_price_snapshots "
        f"WHERE identity_kind = ? AND identity_key IN ({placeholders}) "
        "GROUP BY identity_key",
        (STORAGE_CG_KIND, *coin_ids),
    )
    last_by_key = {row["identity_key"]: row["last_at"] for row in await cur.fetchall()}

    def _rank(coin_id: str) -> tuple[int, str, str]:
        last = last_by_key.get(coin_id)
        # (0, "", key) for never-observed -> ahead of every observed identity.
        return (0, "", coin_id) if last is None else (1, last, coin_id)

    return sorted(coin_ids, key=_rank)


async def _write_cg_snapshots(
    conn: aiosqlite.Connection,
    coin_ids: list[str],
    *,
    now: datetime,
    stats: dict[str, int],
    max_cache_age_min: int,
    batch_id: int,
) -> None:
    """Append CG coin_id observations from the local `price_cache` surface.

    ZERO provider requests. `price_cache` is already maintained by the pipeline
    and is keyed by coin_id, so the cheapest correct source is a local read — no
    new CoinGecko call is added by this lane, and the provider budget that bounds
    the contract lane is deliberately untouched.

    `price_cache` is a latest-value cache, not a series, so the OBSERVATION TIME
    is its own ``updated_at`` — not ``now``. Using ``now`` would stamp a stale
    cached price as a fresh observation and silently corrupt the as-of reads the
    whole Stage B substrate exists to serve.

    Forward-only is enforced per identity: an observation is written only when it
    is strictly newer than that identity's latest stored snapshot. That also
    dedupes repeat runs inside one cache-refresh interval without needing a
    unique constraint.
    """
    for coin_id in coin_ids:
        cur = await conn.execute(
            "SELECT current_price, updated_at FROM price_cache WHERE coin_id = ?",
            (coin_id,),
        )
        row = await cur.fetchone()
        if row is None or row["current_price"] is None:
            stats["cg_no_cache_row"] += 1
            continue
        # A malformed `updated_at` is the plausible corruption mode here, and it
        # is NOT the same fact as "no cache row" — conflating them would report
        # a data-quality defect as absence. `parse_utc` returns None for some
        # shapes and raises ValueError for others, so both are handled.
        try:
            observed_at = parse_utc(row["updated_at"])
        except ValueError:
            observed_at = None
        if observed_at is None:
            stats["cg_bad_timestamp"] += 1
            _log.warning(
                "scps_cg_bad_timestamp",
                identity_key=coin_id,
                updated_at=str(row["updated_at"])[:64],
            )
            continue
        age_min = (now - observed_at).total_seconds() / 60.0
        if age_min > max_cache_age_min:
            stats["cg_stale_cache"] += 1
            _log.info(
                "scps_cg_cache_stale",
                identity_key=coin_id,
                age_min=round(age_min, 1),
                max_cache_age_min=max_cache_age_min,
            )
            continue
        observed_iso = observed_at.isoformat()
        last = await _last_snapshot_at(
            conn, identity_kind=STORAGE_CG_KIND, identity_key=coin_id
        )
        if last is not None and observed_iso <= last:
            # Already captured this observation (or a newer one). Forward-only.
            if observed_iso < last:
                # STRICTLY older than what we already hold: the upstream cache
                # moved BACKWARDS. Separated from the ordinary "nothing new"
                # case because it is upstream misbehaviour, not a quiet tick.
                _log.info(
                    "scps_cg_cache_regressed",
                    identity_key=coin_id,
                    observed_at=observed_iso,
                    last_snapshot_at=last,
                )
            stats["cg_not_newer"] += 1
            continue
        await conn.execute(
            "INSERT INTO source_call_price_snapshots "
            "(identity_key, identity_kind, chain, price, snapshot_at, source, "
            " batch_id) "
            "VALUES (?, ?, NULL, ?, ?, 'cg', ?)",
            (
                coin_id,
                STORAGE_CG_KIND,
                float(row["current_price"]),
                observed_iso,
                batch_id,
            ),
        )
        stats["cg_snapshots_written"] += 1
        stats["snapshots_written"] += 1


async def write_price_snapshots(
    conn: aiosqlite.Connection,
    *,
    now: datetime,
    resolve_pool: PoolResolver,
    fetch_ohlcv: OhlcvFetcher,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    max_identities_per_run: int = DEFAULT_MAX_IDENTITIES_PER_RUN,
    max_requests_per_day: int = DEFAULT_MAX_REQUESTS_PER_DAY,
    max_cg_identities_per_run: int = DEFAULT_MAX_CG_IDENTITIES_PER_RUN,
    max_price_cache_age_min: int = DEFAULT_MAX_PRICE_CACHE_AGE_MIN,
) -> dict[str, int]:
    """One forward-only snapshot cycle. Returns observability counters.

    Two hard ceilings bound provider spend, both fail-CLOSED (skip, never
    partial-write-then-blow-past):

    - ``max_identities_per_run`` caps requests in a single cycle. Excess
      identities are counted in ``skipped_over_cap`` and picked up next run —
      they stay inside the forward horizon, so nothing is lost.
    - ``max_requests_per_day`` caps the rolling UTC-day spend, measured from the
      persisted run counters, so a runaway cron cannot exhaust the provider
      budget between operator checks.
    """
    now_dt = now.astimezone(timezone.utc)
    cutoff = now_dt - timedelta(hours=horizon_hours)
    started_perf = perf_counter()

    # Select on PRICEABLE IDENTITY, not on source_type/resolved_state. The
    # original scope (source_type='x' AND resolved_state='eligible_contract')
    # matched zero rows in production: the X lane is dead, while the live TG
    # lane — the one the ranking gate actually spans — was excluded by design.
    # `resolved_state` is also mixed-case and inconsistent in prod
    # (RESOLVED/resolved/unresolved/UNRESOLVED_*), so it is not a safe filter.
    # What a row genuinely needs to be priced here is a contract address, a
    # chain, and a still-open outcome window; `_priceable_identity` below is the
    # single authority on the first two.
    #
    # Stage B Option 3 widens this to CG coin_id identities too. The old
    # predicate required a contract address, so a call carrying only a genuine
    # CoinGecko id was invisible here — on prod 2026-08-13 that was 110 of 127
    # open calls, i.e. the MAJORITY path. `_priceable_identity` still decides
    # which kind a row is; this predicate only stops excluding the coin_id ones.
    cur = await conn.execute(
        "SELECT id, token_id, contract_address, chain, call_ts "
        "FROM source_calls "
        "WHERE outcome_status IN ('pending','partial') "
        "AND ("
        "  (contract_address IS NOT NULL AND contract_address != '' "
        "   AND chain IS NOT NULL AND chain != '') "
        "  OR (token_id IS NOT NULL AND token_id != '')"
        ")"
    )
    rows = await cur.fetchall()

    # Dedupe by priceable identity (matches C1's _priceable_identity key exactly,
    # so the C3 lookup joins cleanly). Keep the ORIGINAL-case (contract, chain)
    # for the provider call — the identity_key is lowercased for grouping only,
    # and Solana contract addresses are case-sensitive.
    seen: dict[str, tuple[str, str | None]] = {}
    # CG identities are kept in a SEPARATE set: they are priced from an existing
    # local surface, cost zero provider requests, and therefore must not consume
    # the provider budget that bounds the contract lane.
    cg_seen: list[str] = []
    for row in rows:
        call_ts = parse_utc(row["call_ts"])
        if call_ts is None or call_ts < cutoff:
            continue
        identity = _priceable_identity(row)
        if identity is None:
            continue
        kind, key = identity
        if kind == LEDGER_CG_KIND:
            if key not in cg_seen:
                cg_seen.append(key)
            continue
        if kind != "contract":
            continue
        # The widened SELECT admits token_id-bearing rows, and one of those can
        # carry a contract with a NULL/empty chain — which `_priceable_identity`
        # still calls a contract identity. Without this guard such a row enters
        # the CONTRACT lane, calls resolve_pool(chain=None), and GT answers 404
        # on /networks/None/...; at the observed 1-2 identities per run a single
        # such row is enough to hold the provider_error_spike watchdog at its
        # threshold. Restores byte-equivalent contract-lane selection while
        # keeping the SQL widening.
        #
        # Measured on prod 2026-08-13: ZERO rows currently in this class, so
        # this is a LATENT guard, not a live defect — it protects the lane the
        # first time such a row appears.
        if not row["chain"]:
            continue
        if key not in seen:
            seen[key] = (row["contract_address"], row["chain"])

    # Daily ceiling is checked BEFORE any provider call so a breach costs zero
    # requests. Budget visibility is emitted whether or not the run proceeds.
    # Allocate the batch id BEFORE any row is written, but publish it only
    # after every data commit — see `_publish_batch`. Nothing is visible to an
    # as-of reader in between, which is the conservative direction.
    batch_id = await _allocate_batch_id(conn)

    used_today = await _requests_used_today(conn, now=now_dt)
    remaining_today = max(0, max_requests_per_day - used_today)
    affordable = remaining_today // _REQUESTS_PER_IDENTITY

    selected = list(seen.items())
    run_cap = min(max_identities_per_run, affordable)
    to_process = selected[:run_cap]

    stats = {
        # `identities_seen` keeps its original meaning — identities ACTUALLY
        # processed this run — because the persisted daily-spend estimate is
        # derived from it. Deferred work is reported separately.
        "identities_seen": len(to_process),
        "identities_selected": len(selected),
        "skipped_over_cap": len(selected) - len(to_process),
        "requests_attempted": 0,
        "snapshots_written": 0,
        "provider_errors": 0,
        "rate_limited": 0,
        "pools_unresolved": 0,
        "empty_ohlcv": 0,
        "requests_used_today_before": used_today,
        "requests_remaining_today_before": remaining_today,
        "daily_cap_reached": int(affordable <= 0),
        # CG lane counters. Reported separately from the contract lane so the
        # two are never conflated in a coverage read.
        "cg_identities_selected": len(cg_seen),
        "cg_skipped_over_cap": 0,
        "cg_snapshots_written": 0,
        "cg_no_cache_row": 0,
        "cg_stale_cache": 0,
        "cg_bad_timestamp": 0,
        "cg_not_newer": 0,
    }
    # FILTER, then ROTATE, then slice — the order matters and each step fixes a
    # distinct failure:
    #   filter  drops identities that cannot produce a row this run, so they
    #           cannot occupy the rotation's bootstrap head forever (H1)
    #   rotate  stops the stable source_calls scan order from handing the same
    #           prefix to every cycle, which starved the tail rather than
    #           deferring it (G2)
    #   slice   applies the cap to work that can actually yield rows
    cg_eligible = await _eligible_cg_identities(
        conn,
        cg_seen,
        now=now_dt,
        max_cache_age_min=max_price_cache_age_min,
        stats=stats,
    )
    cg_ordered = await _rotate_cg_identities(conn, cg_eligible)
    stats["cg_skipped_over_cap"] = max(0, len(cg_ordered) - max_cg_identities_per_run)
    if stats["cg_skipped_over_cap"]:
        # Never silently truncate — deferred work must be separable from absent
        # work, same contract as the contract lane's skipped_over_cap.
        _log.info(
            "scps_cg_run_cap_deferred",
            processing=max_cg_identities_per_run,
            deferred=stats["cg_skipped_over_cap"],
        )

    # The CG lane runs BEFORE the provider-budget gate and is not subject to it:
    # it issues no provider requests at all, so a contract-lane budget breach
    # must not also silence the majority path.
    await _write_cg_snapshots(
        conn,
        cg_ordered[:max_cg_identities_per_run],
        now=now_dt,
        stats=stats,
        max_cache_age_min=max_price_cache_age_min,
        batch_id=batch_id,
    )
    # Commit unconditionally, right here. The CG rows cost no provider requests,
    # so they must survive a contract-lane budget stop; committing at this single
    # point (rather than special-casing the early return) also bounds the
    # contract loop's own transaction, so an INSERT error down there cannot
    # strand these rows in a half-open transaction.
    await conn.commit()

    if affordable <= 0:
        # CG rows are already committed above; publish their marker before
        # returning or they stay invisible until a later cycle.
        if stats["cg_snapshots_written"]:
            await _publish_batch(
                conn,
                batch_id=batch_id,
                visible_at=datetime.now(timezone.utc).isoformat(),
                rows_written=stats["cg_snapshots_written"],
            )
        stats["batch_id"] = batch_id
        stats["duration_ms"] = int((perf_counter() - started_perf) * 1000)
        _log.warning(
            "scps_daily_request_ceiling_reached",
            used_today=used_today,
            max_requests_per_day=max_requests_per_day,
            identities_deferred=len(selected),
        )
        _log.info("scps_writer_cycle", **stats)
        return stats

    if stats["skipped_over_cap"]:
        # Never silently truncate: an operator reading "snapshots_written" must
        # be able to tell deferred work from absent work.
        _log.info(
            "scps_run_cap_deferred",
            processing=len(to_process),
            deferred=stats["skipped_over_cap"],
            run_cap=run_cap,
        )

    snapshot_at = now_dt.isoformat()

    for identity_key, (contract_address, chain) in to_process:
        try:
            stats["requests_attempted"] += 1
            pool = await resolve_pool(chain=chain, contract_address=contract_address)
            if pool is None:
                stats["pools_unresolved"] += 1
                _log.info(
                    "scps_pool_unresolved", identity_key=identity_key, chain=chain
                )
                continue
            stats["requests_attempted"] += 1
            candles = await fetch_ohlcv(
                network=pool.network, pool_address=pool.pool_address
            )
        except PriceProviderError as exc:
            stats["provider_errors"] += 1
            # 429 is the one failure mode that must be separable at a glance:
            # it is the documented stop condition for this lane, and it is
            # shared with the DEX-discovery lane on the same provider IP.
            if _is_rate_limited(exc):
                stats["rate_limited"] += 1
                _log.warning(
                    "scps_provider_rate_limited",
                    identity_key=identity_key,
                    chain=chain,
                    source=exc.source,
                    reason=exc.reason,
                )
            else:
                _log.warning(
                    "scps_provider_error",
                    identity_key=identity_key,
                    chain=chain,
                    source=exc.source,
                    reason=exc.reason,
                )
            continue

        if not candles:
            stats["empty_ohlcv"] += 1
            _log.info("scps_empty_ohlcv", identity_key=identity_key, chain=chain)
            continue

        latest = candles[-1]  # C0 returns ascending; last candle is most recent
        await conn.execute(
            "INSERT INTO source_call_price_snapshots "
            "(identity_key, identity_kind, chain, price, snapshot_at, source, "
            " batch_id) "
            "VALUES (?, 'contract', ?, ?, ?, ?, ?)",
            (
                identity_key,
                chain,
                float(latest.close),
                snapshot_at,
                getattr(latest, "source", "gt"),
                batch_id,
            ),
        )
        stats["snapshots_written"] += 1

    await conn.commit()
    # DATA IS NOW DURABLE. Only now does the batch become knowable: the marker
    # goes in its own transaction, so a crash here leaves the rows INVISIBLE
    # rather than early-visible.
    if stats["snapshots_written"]:
        await _publish_batch(
            conn,
            batch_id=batch_id,
            visible_at=datetime.now(timezone.utc).isoformat(),
            rows_written=stats["snapshots_written"],
        )
    stats["batch_id"] = batch_id
    stats["duration_ms"] = int((perf_counter() - started_perf) * 1000)
    stats["error_rate"] = (
        round(stats["provider_errors"] / stats["requests_attempted"], 4)
        if stats["requests_attempted"]
        else 0.0
    )
    stats["requests_remaining_today_after"] = max(
        0, remaining_today - stats["requests_attempted"]
    )
    _log.info("scps_writer_cycle", **stats)
    return stats


async def record_snapshot_run(
    conn: aiosqlite.Connection, *, ran_at: str, stats: dict[str, int]
) -> None:
    """Persist one writer cycle's counters (design #392 C4, §12a substrate).

    Read by the coverage watchdogs to distinguish "writer down" from "writer ran
    but nothing priceable", and to compute the provider-error rate over recent
    runs. Append-only; kept separate from ``write_price_snapshots`` so the pure
    pricing function stays side-effect-focused and the cron owns persistence.

    ``snapshots_written`` persisted here is CONTRACT-ONLY. The consumers of this
    table — notably the ``fresh_calls_no_snapshots`` coverage watchdog — read it
    as "did the CONTRACT lane produce anything", and the CG lane can write on a
    run where the contract lane produced nothing at all. Persisting the combined
    total would make a dead contract lane look healthy, defeating the watchdog.
    CG counters stay fully visible on the ``scps_writer_cycle`` log line.
    """
    contract_snapshots = int(stats.get("snapshots_written", 0)) - int(
        stats.get("cg_snapshots_written", 0)
    )
    await conn.execute(
        "INSERT INTO source_call_price_snapshot_runs "
        "(ran_at, identities_seen, snapshots_written, provider_errors, "
        " pools_unresolved, empty_ohlcv) VALUES (?, ?, ?, ?, ?, ?)",
        (
            ran_at,
            int(stats.get("identities_seen", 0)),
            max(0, contract_snapshots),
            int(stats.get("provider_errors", 0)),
            int(stats.get("pools_unresolved", 0)),
            int(stats.get("empty_ohlcv", 0)),
        ),
    )
    await conn.commit()
