"""Forward-only price-snapshot writer for CA-keyed source calls (design #392 C2).

Selects active contract-identity (``eligible_contract``) X source_calls within
the forward horizon, dedupes by priceable identity, fetches a *current* price
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
from typing import Any, Awaitable, Callable

import aiosqlite
import structlog

from scout.exceptions import PriceProviderError
from scout.source_quality.ledger import _priceable_identity, parse_utc

_log = structlog.get_logger()

# Widest forward window end: the 24h window closes at call+28h (ledger WINDOWS),
# so a call older than this can never gain a new in-window forward snapshot.
DEFAULT_HORIZON_HOURS = 28

# (*, chain, contract_address) -> pool-like | None ; pool-like has .network + .pool_address
PoolResolver = Callable[..., Awaitable[Any]]
# (*, network, pool_address) -> list of candle-like (ascending) ; candle has .close + .source
OhlcvFetcher = Callable[..., Awaitable[list[Any]]]


async def write_price_snapshots(
    conn: aiosqlite.Connection,
    *,
    now: datetime,
    resolve_pool: PoolResolver,
    fetch_ohlcv: OhlcvFetcher,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> dict[str, int]:
    """One forward-only snapshot cycle. Returns observability counters."""
    now_dt = now.astimezone(timezone.utc)
    cutoff = now_dt - timedelta(hours=horizon_hours)

    cur = await conn.execute(
        "SELECT id, token_id, contract_address, chain, call_ts "
        "FROM source_calls "
        "WHERE source_type='x' AND resolved_state='eligible_contract' "
        "AND contract_address IS NOT NULL "
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

    stats = {
        "identities_seen": len(seen),
        "snapshots_written": 0,
        "provider_errors": 0,
        "pools_unresolved": 0,
        "empty_ohlcv": 0,
    }
    snapshot_at = now_dt.isoformat()

    for identity_key, (contract_address, chain) in seen.items():
        try:
            pool = await resolve_pool(chain=chain, contract_address=contract_address)
            if pool is None:
                stats["pools_unresolved"] += 1
                _log.info(
                    "scps_pool_unresolved", identity_key=identity_key, chain=chain
                )
                continue
            candles = await fetch_ohlcv(
                network=pool.network, pool_address=pool.pool_address
            )
        except PriceProviderError as exc:
            stats["provider_errors"] += 1
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
    _log.info("scps_writer_cycle", **stats)
    return stats
