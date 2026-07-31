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


async def write_price_snapshots(
    conn: aiosqlite.Connection,
    *,
    now: datetime,
    resolve_pool: PoolResolver,
    fetch_ohlcv: OhlcvFetcher,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    max_identities_per_run: int = DEFAULT_MAX_IDENTITIES_PER_RUN,
    max_requests_per_day: int = DEFAULT_MAX_REQUESTS_PER_DAY,
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
    cur = await conn.execute(
        "SELECT id, token_id, contract_address, chain, call_ts "
        "FROM source_calls "
        "WHERE contract_address IS NOT NULL AND contract_address != '' "
        "AND chain IS NOT NULL AND chain != '' "
        "AND outcome_status IN ('pending','partial')"
    )
    rows = await cur.fetchall()

    # Dedupe by priceable identity (matches C1's _priceable_identity key exactly,
    # so the C3 lookup joins cleanly). Keep the ORIGINAL-case (contract, chain)
    # for the provider call — the identity_key is lowercased for grouping only,
    # and Solana contract addresses are case-sensitive.
    seen: dict[str, tuple[str, str | None]] = {}
    for row in rows:
        call_ts = parse_utc(row["call_ts"])
        if call_ts is None or call_ts < cutoff:
            continue
        identity = _priceable_identity(row)
        if identity is None or identity[0] != "contract":
            continue
        key = identity[1]
        if key not in seen:
            seen[key] = (row["contract_address"], row["chain"])

    # Daily ceiling is checked BEFORE any provider call so a breach costs zero
    # requests. Budget visibility is emitted whether or not the run proceeds.
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
    }

    if affordable <= 0:
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
            "(identity_key, identity_kind, chain, price, snapshot_at, source) "
            "VALUES (?, 'contract', ?, ?, ?, ?)",
            (
                identity_key,
                chain,
                float(latest.close),
                snapshot_at,
                getattr(latest, "source", "gt"),
            ),
        )
        stats["snapshots_written"] += 1

    await conn.commit()
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
    """
    await conn.execute(
        "INSERT INTO source_call_price_snapshot_runs "
        "(ran_at, identities_seen, snapshots_written, provider_errors, "
        " pools_unresolved, empty_ohlcv) VALUES (?, ?, ?, ?, ?, ?)",
        (
            ran_at,
            int(stats.get("identities_seen", 0)),
            int(stats.get("snapshots_written", 0)),
            int(stats.get("provider_errors", 0)),
            int(stats.get("pools_unresolved", 0)),
            int(stats.get("empty_ohlcv", 0)),
        ),
    )
    await conn.commit()
