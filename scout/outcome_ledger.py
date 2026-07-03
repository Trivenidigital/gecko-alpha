"""Signal outcome ledger — self-labeling forward returns (P0, edge-audit).

The 2026-07-02 edge audit (tasks/gecko-alpha-fable-review_2026_07.md Phase 3)
found historical forward-returns uncomputable: the alerts table has 33
lifetime rows with no usable price, so gate counterfactuals are impossible.
This module makes every FUTURE emission self-label:

- :func:`record_emission` writes one ``signal_outcome_ledger`` row at each
  emission site (candidate alert send, paper-trade dispatch, sampled
  gate-block) with the price + liquidity the emitting site had in hand.
- :func:`label_pending` (called from the hourly maintenance pass) resolves
  forward prices for due horizons from IN-DB sources ONLY — volume_history_cg
  history first, price_cache as a lateness-bounded fallback. No external API
  calls, zero rate-limit budget.

Labeling-granularity note (documented per the P0 spec): the labeler runs
hourly, so a 15m horizon is typically labeled 'late'. That is fine because a
horizon's price comes from HISTORICAL rows recorded at/after the horizon
deadline — never from the now-price — so late labeling does not skew returns.
The only fallback that could inject a late price (price_cache, which holds
only the current observation) is bounded by
``LEDGER_PRICE_CACHE_MAX_LATENESS_MINUTES``.

Terminal states (once ``emitted_at + 7d`` has passed):
- ``complete``    — at least one forward label resolved (unfilled horizons had
                    no in-DB source at their deadline and stay NULL).
- ``unlabelable`` — NO in-DB price source produced any label in 7 days. This
                    cohort is itself signal: tokens whose price telemetry
                    died (the liquidity-death cohort).

Everything here is observability, not control flow: failures are logged
(``ledger_record_failed`` / ``ledger_label_pass_failed``) and swallowed so
host paths (alert delivery, trade open) keep their exact prior behavior.

Trading interaction — MEASUREMENT-AFFECTING-TRADING (operator condition on
PR #406): the enrollment poller writes ``price_cache`` rows for
``dex:{chain}:{address}`` ids. The trading engine's GA-01
unpriceable-dispatch gate admits non-CG token_ids whenever a price_cache row
EXISTS, so poller-written rows can satisfy that row-exists branch while a
token is enrolled (7d TTL) — i.e. this measurement lane can open the trading
gate for dex tokens it polls. Gate tightening is a NAMED backlog slice:
**BL-NEW-LEDGER-GATE-TIGHTENING** (added to backlog.md via PR #409).
Acceptance of this module is conditional on NO tg_social / dex-producing
signal re-enable until that slice is merged AND deployed.

NOT related to scout/source_quality/ledger.py (source-call quality ledger).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from scout.db import Database
from scout.token_ids import is_cg_coin_id

log = structlog.get_logger(__name__)

# Forward-return horizons. These are SCHEMA-BOUND constants (each maps to a
# fixed column of signal_outcome_ledger), not tunables — changing one requires
# a schema migration, so they intentionally do not live in Settings.
_HORIZONS: tuple[tuple[str, timedelta], ...] = (
    ("r15m", timedelta(minutes=15)),
    ("r1h", timedelta(hours=1)),
    ("r4h", timedelta(hours=4)),
    ("r24h", timedelta(hours=24)),
    ("r7d", timedelta(days=7)),
)
# A row becomes terminal (complete / unlabelable) once the longest horizon has
# elapsed. Same value as the r7d horizon — schema-bound, see above.
_FINALIZE_AFTER = timedelta(days=7)


class GatedOutSampler:
    """Deterministic 1-in-N sampler for blocked trade-decision emissions.

    A counter (not RNG) so tests are exact and the sample is evenly spread
    across time. ``rate`` is read per call (from Settings) so an .env change
    picked up on restart — or a test override — applies immediately;
    ``rate <= 0`` disables sampling entirely.
    """

    def __init__(self) -> None:
        self._n = 0

    def should_record(self, rate: int) -> bool:
        if rate <= 0:
            return False
        self._n += 1
        return self._n % rate == 0


def _ledger_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "LEDGER_ENABLED", False))


def _namespace_for(token_id: str) -> str:
    """Price-source namespace for the enrollment poller's routing.

    - 'cg'    — CG-shaped coin id: pollable via one batched /simple/price.
    - 'dex'   — TG-social resolver's ``dex:{chain}:{address}`` namespace:
                pollable via the DexScreener tokens endpoint.
    - 'other' — bare contract addresses etc.: no poller lane yet; rows stay
                in the set until TTL and label only if another writer covers
                them (the residual unlabelable cohort).
    """
    if token_id.startswith("dex:"):
        return "dex"
    if is_cg_coin_id(token_id):
        return "cg"
    return "other"


async def _has_indb_price_coverage(db: Database, token_id: str) -> bool:
    """True iff *token_id* is already priceable from IN-DB sources WITHOUT
    enrollment — so the forward-polling set (the enrollment lane) should skip
    it.

    A token is in-DB-covered iff:
    - ``is_cg_coin_id(token_id)`` — a CG-slug id served by the CG ingestion
      lanes; its price history flows into volume_history_cg without any
      poller, OR
    - a ``price_cache`` row already exists for it — another writer already
      covers it.

    Rationale (coverage-gated enrollment, 2026-07-03 operator finding on
    PR #421): the enrollment set is capped at LEDGER_ENROLLMENT_MAX_ACTIVE
    (200, evict-oldest). Prod measured 153,113 dispatcher-suppressed blocks
    over 14d across 300 DISTINCT token_ids (~150 per 7d TTL window); most are
    CG-slug (chain_completed / gainers_early / losers_contrarian /
    volume_spike are CG-sourced) and ALREADY labelable from volume_history_cg.
    Enrolling them consumed ~75% of the cap and EVICTED the untracked
    micro-cap / dex cohort the recall lane exists to measure. Gating
    enrollment on "no in-DB coverage" targets the poller at exactly the
    untracked cohort, for ALL gated_out samples (not just PR #421's).

    Fail-soft: never raises. On any error returns False (= "no coverage" ->
    enroll), preserving the prior conservative behavior of not silently
    dropping a possibly-untracked token from the measurement lane.
    """
    if is_cg_coin_id(token_id):
        return True
    try:
        conn = db._conn
        if conn is None:
            return False
        cur = await conn.execute(
            "SELECT 1 FROM price_cache WHERE coin_id = ? LIMIT 1", (token_id,)
        )
        return (await cur.fetchone()) is not None
    except Exception as exc:
        log.warning("ledger_coverage_check_failed", token_id=token_id, error=str(exc))
        return False


async def _enroll_token_locked(
    conn: Any, token_id: str, settings: Any, now: datetime
) -> None:
    """Enroll *token_id* into the forward-polling set. Caller holds _txn_lock.

    UPSERT (never INSERT OR REPLACE — #325 lesson): re-emission refreshes
    expires_at but preserves the original enrolled_at. After the write, the
    active set is capped at LEDGER_ENROLLMENT_MAX_ACTIVE by evicting the
    rows closest to expiry (oldest-expire-first).

    Cap semantics (operator condition c2 on PR #406): the NEW enrollment
    always lands ('skipped_cap' is unreachable — eviction makes room), and
    every eviction is named in a ``ledger_enrollment_evicted`` structured
    log so the analyst can distinguish "unlabelable: liquidity death" from
    "unlabelable: coverage lost to cap eviction." Ledger rows of evicted
    tokens are NOT retroactively re-stamped; their enrollment_status stays
    'enrolled' and the eviction log is the censoring record.
    """
    ttl_days = int(getattr(settings, "LEDGER_ENROLLMENT_TTL_DAYS", 7))
    max_active = int(getattr(settings, "LEDGER_ENROLLMENT_MAX_ACTIVE", 200))
    expires = (now + timedelta(days=ttl_days)).isoformat()
    await conn.execute(
        """INSERT INTO ledger_enrollments
           (token_id, namespace, enrolled_at, expires_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(token_id) DO UPDATE SET
             expires_at = excluded.expires_at""",
        (token_id, _namespace_for(token_id), now.isoformat(), expires),
    )
    cur = await conn.execute("SELECT COUNT(*) FROM ledger_enrollments")
    count = int((await cur.fetchone())[0])
    if count > max_active:
        n_evict = count - max_active
        cur = await conn.execute(
            "SELECT token_id FROM ledger_enrollments "
            "ORDER BY expires_at ASC LIMIT ?",
            (n_evict,),
        )
        evicted_ids = [r[0] for r in await cur.fetchall()]
        placeholders = ",".join("?" * len(evicted_ids))
        await conn.execute(
            f"DELETE FROM ledger_enrollments WHERE token_id IN ({placeholders})",
            evicted_ids,
        )
        log.info(
            "ledger_enrollment_evicted",
            evicted_token_ids=evicted_ids,
            n_evicted=len(evicted_ids),
            max_active=max_active,
            evicted_for=token_id,
        )


async def price_and_age_from_cache(
    db: Database, token_id: str
) -> tuple[float | None, float | None]:
    """(price, age_seconds) for *token_id*'s price_cache row. Fail-soft.

    ``age_seconds`` = now - price_cache.updated_at — how stale the anchor was
    at the moment it was captured (operator condition c1 on PR #406: alerts
    resolve their anchor from the cache, so the age must ride along or the
    forward-return baseline silently degrades with cache staleness).
    Returns ``(None, None)`` when no usable row exists or on any error;
    ``(price, None)`` when the row exists but updated_at is unparseable.
    """
    try:
        conn = db._conn
        if conn is None:
            return None, None
        cur = await conn.execute(
            "SELECT current_price, updated_at FROM price_cache "
            "WHERE coin_id = ? AND current_price IS NOT NULL AND current_price > 0",
            (token_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None, None
        price = float(row[0])
        observed = _parse_ts(row[1])
        if observed is None:
            return price, None
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        return price, max(age, 0.0)
    except Exception as exc:
        log.warning("ledger_price_lookup_failed", token_id=token_id, error=str(exc))
        return None, None


async def price_from_cache(db: Database, token_id: str) -> float | None:
    """Best-effort current price for *token_id* from price_cache. Fail-soft."""
    price, _age = await price_and_age_from_cache(db, token_id)
    return price


async def record_emission(
    db: Database,
    settings: Any,
    *,
    kind: str,
    token_id: str,
    surface: str,
    price: float | None = None,
    anchor_cache_age_seconds: float | None = None,
    liquidity: float | None = None,
    liquidity_source: str = "none",
    gate_verdicts: dict[str, Any] | None = None,
    emitted_at: str | None = None,
) -> int | None:
    """Append one emission row to signal_outcome_ledger.

    Observability, not control flow: NEVER raises. Any failure logs
    ``ledger_record_failed`` and returns None so the host path (alert send /
    trade open / gate block) keeps its exact prior behavior. Respects the
    ``LEDGER_ENABLED`` kill switch.

    Args:
        kind: 'alert' | 'dispatch' | 'gated_out_sample' (CHECK-enforced).
        token_id: price-source key — CG coin id for CG-sourced emissions,
            contract address otherwise (the latter usually has no
            volume_history_cg rows and will resolve 'unlabelable', which is
            itself signal).
        surface: signal_type, or 'candidate_alert' for the alert pipeline.
        price: price at emission (forward-return anchor). NULL when the
            emitting site had none.
        anchor_cache_age_seconds: staleness of the anchor at emission
            (operator condition c1). Pass the cache age when *price* was
            resolved from price_cache (the alert site does, via
            :func:`price_and_age_from_cache`). When omitted, a non-NULL
            *price* is treated as LIVE at emission (0.0) — the
            dispatch/gated_out caller-supplied entry_price case. Stored NULL
            whenever *price* is NULL.
        liquidity: liquidity USD the emitting site had in hand, or None.
        liquidity_source: provenance label ('candidate', 'signal_data',
            'none', ...).
        gate_verdicts: JSON-serialized verdict context (block reason,
            conviction score vs threshold, ...). ``default=str`` so odd types
            never break the host path.
        emitted_at: ISO timestamp override; defaults to now (UTC).
    """
    if not _ledger_enabled(settings):
        return None
    try:
        conn = db._conn
        if conn is None or db._txn_lock is None:
            log.warning("ledger_record_skipped_db_closed", kind=kind, token_id=token_id)
            return None
        emitted = emitted_at or datetime.now(timezone.utc).isoformat()
        verdicts_json = (
            json.dumps(gate_verdicts, sort_keys=True, default=str)
            if gate_verdicts is not None
            else None
        )
        # c1: anchor age semantics — NULL without an anchor; explicit cache
        # age when the caller resolved from price_cache; else 0.0 (live).
        if price is None:
            anchor_age: float | None = None
        elif anchor_cache_age_seconds is not None:
            anchor_age = float(anchor_cache_age_seconds)
        else:
            anchor_age = 0.0
        # c2: enrollment outcome is stamped ON the ledger row so unrecorded
        # skips can never silently censor the missed-winner lane. Under the
        # evict-oldest-to-make-room semantics the new enrollment always
        # lands, so the stamp is binary here ('skipped_cap' stays reserved
        # in the CHECK for a future no-eviction policy).
        #
        # Coverage-gated enrollment (2026-07-03, PR #421 finding): enroll ONLY
        # tokens with NO in-DB price coverage. A CG-slug token (is_cg_coin_id)
        # or one that already has a price_cache row is labelable WITHOUT the
        # poller, so enrolling it just churns the LEDGER_ENROLLMENT_MAX_ACTIVE
        # cap and evicts the untracked cohort the recall lane measures. A
        # covered gated_out_sample therefore stamps 'not_needed' (per #406
        # semantics: "token has in-DB coverage"); only genuinely-untracked
        # ones stamp 'enrolled'. Analysis still separates covered-suppressed
        # (kind=gated_out_sample, status=not_needed) from untracked-suppressed
        # (status=enrolled) on the stamp, and can further split covered rows
        # via is_cg_coin_id at read time. Applies to ALL gated_out samples and
        # priceless emissions, not just PR #421's dispatcher-suppressed blocks.
        enroll_base = kind == "gated_out_sample" or price is None
        enroll_needed = enroll_base and not await _has_indb_price_coverage(db, token_id)
        enrollment_status = "enrolled" if enroll_needed else "not_needed"
        async with db._txn_lock:
            cur = await conn.execute(
                """INSERT INTO signal_outcome_ledger
                   (kind, token_id, surface, price_at_emission,
                    anchor_cache_age_seconds, liquidity_at_emission,
                    liquidity_source, gate_verdicts, enrollment_status,
                    emitted_at, label_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    kind,
                    token_id,
                    surface,
                    price,
                    anchor_age,
                    liquidity,
                    liquidity_source,
                    verdicts_json,
                    enrollment_status,
                    emitted,
                ),
            )
            # Enrollment-at-emission: in-DB-only labeling cannot price
            # tokens the tracked lanes never carry (below-min-mcap
            # micro-caps, dex:/bare-address ids in gated_out samples,
            # priceless alerts). Without forward polling those rows are
            # STRUCTURALLY unlabelable and the missed-winner recall lane is
            # unmeasurable. Enrollment is now coverage-gated (see the
            # enroll_needed derivation above): a gated_out sample or
            # priceless emission enrolls ONLY when the token has no in-DB
            # price coverage (not a CG-slug id and no existing price_cache
            # row); CG-covered tokens label from volume_history_cg without
            # the poller and must not churn the enrollment cap.
            if enroll_needed:
                await _enroll_token_locked(
                    conn, token_id, settings, datetime.now(timezone.utc)
                )
            await conn.commit()
        row_id = int(cur.lastrowid)
        log.debug(
            "ledger_emission_recorded",
            ledger_id=row_id,
            kind=kind,
            token_id=token_id,
            surface=surface,
        )
        return row_id
    except Exception as exc:
        log.warning(
            "ledger_record_failed",
            kind=kind,
            token_id=token_id,
            surface=surface,
            error=str(exc),
        )
        try:
            if db._conn is not None:
                await db._conn.rollback()
        except Exception as rb_exc:
            log.warning("ledger_record_rollback_failed", error=str(rb_exc))
        return None


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def _price_at_or_after(
    db: Database,
    token_id: str,
    deadline: datetime,
    max_cache_lateness: timedelta,
) -> float | None:
    """First in-DB price observed at/after *deadline* for *token_id*.

    Preference order (in-DB only — no external calls):
    1. volume_history_cg — true historical observations; the first row at or
       after the deadline is the horizon price, however late the labeler runs.
    2. price_cache — holds only the CURRENT observation, so it is accepted
       only when observed within *max_cache_lateness* after the deadline
       (otherwise it would inject a far-future price into an old horizon).
    """
    conn = db._conn
    cur = await conn.execute(
        "SELECT price, recorded_at FROM volume_history_cg "
        "WHERE coin_id = ? AND recorded_at >= ? "
        "AND price IS NOT NULL AND price > 0 "
        "ORDER BY recorded_at ASC LIMIT 1",
        (token_id, deadline.isoformat()),
    )
    row = await cur.fetchone()
    if row:
        return float(row[0])

    cur = await conn.execute(
        "SELECT current_price, updated_at FROM price_cache "
        "WHERE coin_id = ? AND updated_at >= ? "
        "AND current_price IS NOT NULL AND current_price > 0",
        (token_id, deadline.isoformat()),
    )
    row = await cur.fetchone()
    if row:
        observed = _parse_ts(row[1])
        if observed is not None and observed - deadline <= max_cache_lateness:
            return float(row[0])
    return None


async def _peak_price_in_window(
    db: Database, token_id: str, start: datetime, end: datetime
) -> float | None:
    """MAX(volume_history_cg.price) in [start, end] — raw price, not a return,
    so it labels even when price_at_emission is NULL."""
    conn = db._conn
    cur = await conn.execute(
        "SELECT MAX(price) FROM volume_history_cg "
        "WHERE coin_id = ? AND recorded_at >= ? AND recorded_at <= ? "
        "AND price IS NOT NULL AND price > 0",
        (token_id, start.isoformat(), end.isoformat()),
    )
    row = await cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


async def label_pending(db: Database, settings: Any) -> dict[str, Any]:
    """Per-cycle labeler: resolve forward returns for pending/partial rows.

    Called from the hourly maintenance pass. For each of the oldest
    ``LEDGER_LABEL_BATCH_MAX`` pending/partial rows, every horizon whose
    deadline (emitted_at + horizon) has passed is labeled with
    ``price_at_horizon / price_at_emission - 1`` using the first in-DB price
    observed at/after the deadline (see :func:`_price_at_or_after`). Once 7d
    have elapsed the row is finalized: ``peak7d`` = MAX(price) in the 7d
    window, status -> 'complete' (>=1 label) or 'unlabelable' (none).

    Always emits a ``ledger_label_pass`` structured log — that log line plus
    the table's registration in the dashboard system-health path is the §12a
    freshness surface for this pipeline table.

    Fail-soft: never raises; a failed pass logs ``ledger_label_pass_failed``
    and returns zeroed stats.
    """
    stats: dict[str, Any] = {
        "enabled": _ledger_enabled(settings),
        "n_examined": 0,
        "n_labeled": 0,
        "n_pending": 0,
        "n_unlabelable": 0,
        "n_complete": 0,
    }
    if not stats["enabled"]:
        log.info("ledger_label_pass", **stats)
        return stats

    try:
        conn = db._conn
        if conn is None or db._txn_lock is None:
            log.warning("ledger_label_skipped_db_closed")
            return stats

        batch_max = int(getattr(settings, "LEDGER_LABEL_BATCH_MAX", 500))
        cache_lateness = timedelta(
            minutes=int(
                getattr(settings, "LEDGER_PRICE_CACHE_MAX_LATENESS_MINUTES", 120)
            )
        )
        now = datetime.now(timezone.utc)

        cur = await conn.execute(
            "SELECT id, token_id, price_at_emission, emitted_at, "
            "r15m, r1h, r4h, r24h, r7d, peak7d, label_status "
            "FROM signal_outcome_ledger "
            "WHERE label_status IN ('pending', 'partial') "
            "ORDER BY emitted_at ASC LIMIT ?",
            (batch_max,),
        )
        rows = await cur.fetchall()
        stats["n_examined"] = len(rows)

        async with db._txn_lock:
            for row in rows:
                emitted = _parse_ts(row["emitted_at"])
                if emitted is None:
                    # Unparseable emitted_at can never label — terminal.
                    await conn.execute(
                        "UPDATE signal_outcome_ledger "
                        "SET label_status = 'unlabelable', labeled_at = ? "
                        "WHERE id = ?",
                        (now.isoformat(), row["id"]),
                    )
                    stats["n_unlabelable"] += 1
                    continue

                base = row["price_at_emission"]
                base = float(base) if base is not None and base > 0 else None
                updates: dict[str, float] = {}

                for col, horizon in _HORIZONS:
                    if row[col] is not None:
                        continue
                    deadline = emitted + horizon
                    if now < deadline:
                        continue
                    if base is None:
                        continue  # returns uncomputable without an anchor
                    horizon_price = await _price_at_or_after(
                        db, row["token_id"], deadline, cache_lateness
                    )
                    if horizon_price is not None:
                        updates[col] = horizon_price / base - 1.0

                final = now >= emitted + _FINALIZE_AFTER
                if final and row["peak7d"] is None:
                    peak = await _peak_price_in_window(
                        db, row["token_id"], emitted, emitted + _FINALIZE_AFTER
                    )
                    if peak is not None:
                        updates["peak7d"] = peak

                any_label = (
                    bool(updates)
                    or any(row[col] is not None for col, _ in _HORIZONS)
                    or row["peak7d"] is not None
                )

                set_parts = [f"{col} = ?" for col in updates]
                params: list[Any] = list(updates.values())
                if final:
                    status = "complete" if any_label else "unlabelable"
                    set_parts += ["label_status = ?", "labeled_at = ?"]
                    params += [status, now.isoformat()]
                    stats[
                        "n_complete" if status == "complete" else "n_unlabelable"
                    ] += 1
                elif any_label and row["label_status"] == "pending":
                    set_parts.append("label_status = 'partial'")

                if set_parts:
                    params.append(row["id"])
                    await conn.execute(
                        "UPDATE signal_outcome_ledger "
                        f"SET {', '.join(set_parts)} WHERE id = ?",
                        params,
                    )
                if updates:
                    stats["n_labeled"] += 1
            await conn.commit()

        cur = await conn.execute(
            "SELECT COUNT(*) FROM signal_outcome_ledger "
            "WHERE label_status IN ('pending', 'partial')"
        )
        stats["n_pending"] = int((await cur.fetchone())[0])
        log.info("ledger_label_pass", **stats)
        return stats
    except Exception as exc:
        log.warning("ledger_label_pass_failed", error=str(exc))
        try:
            if db._conn is not None:
                await db._conn.rollback()
        except Exception as rb_exc:
            log.warning("ledger_label_rollback_failed", error=str(rb_exc))
        return stats


# /simple/price accepts up to ~250 ids per call; the poller spends AT MOST
# one CG call per cycle, so the active-CG cohort is truncated to this size
# (the enrollment cap defaults to 200, comfortably inside one batch).
_ENROLLMENT_CG_BATCH_MAX = 250
# DexScreener tokens endpoint accepts up to 30 comma-joined addresses/call.
_ENROLLMENT_DEX_BATCH_MAX = 30


async def purge_expired_enrollments(db: Database) -> int:
    """Delete enrollments past TTL. Returns rows purged. Fail-soft."""
    try:
        conn = db._conn
        if conn is None or db._txn_lock is None:
            return 0
        now_iso = datetime.now(timezone.utc).isoformat()
        async with db._txn_lock:
            cur = await conn.execute(
                "DELETE FROM ledger_enrollments WHERE expires_at <= ?", (now_iso,)
            )
            await conn.commit()
        return int(cur.rowcount or 0)
    except Exception as exc:
        log.warning("ledger_enrollment_purge_failed", error=str(exc))
        return 0


async def active_enrollments(db: Database) -> list[tuple[str, str]]:
    """Return [(token_id, namespace), ...] for un-expired enrollments."""
    conn = db._conn
    if conn is None:
        return []
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = await conn.execute(
        "SELECT token_id, namespace FROM ledger_enrollments "
        "WHERE expires_at > ? ORDER BY enrolled_at ASC",
        (now_iso,),
    )
    rows = await cur.fetchall()
    return [(r[0], r[1]) for r in rows]


async def _poll_dex_enrollments(db: Database, session: Any, dex_ids: list[str]) -> int:
    """Price ``dex:{chain}:{address}`` enrollments via DexScreener.

    Batches up to 30 addresses per chain per call (DexScreener tokens
    endpoint contract; its rate budget is separate from CoinGecko's and
    generous). Writes through :meth:`Database.cache_prices` keyed by the
    FULL dex token_id, so the labeler's price_cache fallback resolves it.

    This is the dex: namespace's first price writer — MEASUREMENT-AFFECTING-
    TRADING: while a token is enrolled, its poller-written price_cache row
    can satisfy the GA-01 unpriceable-dispatch gate's row-exists branch.
    Gate tightening = backlog slice BL-NEW-LEDGER-GATE-TIGHTENING (PR #409);
    no tg_social / dex-producing signal re-enable until it is merged +
    deployed. See the module docstring and PR #406 body.
    """
    # Lazy import: aiohttp's import aborts on Windows dev boxes
    # (OPENSSL_Applink); this function only runs inside the Linux pipeline.
    import aiohttp

    parsed: dict[str, list[tuple[str, str]]] = {}
    for token_id in dex_ids:
        parts = token_id.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            continue
        parsed.setdefault(parts[1], []).append((parts[2], token_id))

    raw_coins: list[dict] = []
    timeout = aiohttp.ClientTimeout(total=15)
    for chain, addr_pairs in parsed.items():
        for i in range(0, len(addr_pairs), _ENROLLMENT_DEX_BATCH_MAX):
            batch = addr_pairs[i : i + _ENROLLMENT_DEX_BATCH_MAX]
            by_addr = {addr.lower(): tid for addr, tid in batch}
            url = (
                "https://api.dexscreener.com/tokens/v1/"
                f"{chain}/{','.join(addr for addr, _ in batch)}"
            )
            try:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        log.warning(
                            "ledger_dex_poll_http_error",
                            chain=chain,
                            status=resp.status,
                        )
                        continue
                    pairs = await resp.json()
            except Exception as exc:
                log.warning("ledger_dex_poll_fetch_failed", chain=chain, error=str(exc))
                continue
            if not isinstance(pairs, list):
                continue
            seen: set[str] = set()
            for pair in pairs:
                if not isinstance(pair, dict):
                    continue
                base = pair.get("baseToken") or {}
                addr = str(base.get("address") or "").lower()
                tid = by_addr.get(addr)
                if tid is None or tid in seen:
                    continue  # first pair per token wins (check_outcomes parity)
                try:
                    price = float(pair.get("priceUsd") or 0)
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                seen.add(tid)
                raw_coins.append(
                    {
                        "id": tid,
                        "current_price": price,
                        "market_cap": pair.get("marketCap") or pair.get("fdv"),
                    }
                )
    if raw_coins:
        await db.cache_prices(raw_coins)
    return len(raw_coins)


async def poll_enrollments(db: Database, session: Any, settings: Any) -> dict[str, Any]:
    """Per-cycle forward-poll of enrolled tokens so the labeler can price them.

    - CG-shaped ids: ONE batched /simple/price call per cycle (<=250 ids),
      reusing the held-position lane's fetch/shape helpers and writing
      through db.cache_prices — labeling stays fully in-DB. Budget impact:
      ~1 CG call/min ~= 3% of the 30/min Demo budget.
    - dex:{chain}:{address} ids: DexScreener tokens endpoint, up to 30
      addresses/call (separate, generous rate budget).
    - 'other' namespace (bare contract addresses): no poller lane; those
      rows label only if another writer covers them, else age out to
      'unlabelable' — the residual, now-measurable, cohort.

    Fail-soft: never raises; a failed pass logs and returns partial stats.
    Respects the LEDGER_ENABLED kill switch.
    """
    stats: dict[str, Any] = {
        "enabled": _ledger_enabled(settings),
        "n_active": 0,
        "n_cg": 0,
        "n_dex": 0,
        "n_other": 0,
        "n_priced": 0,
        "n_expired_purged": 0,
    }
    if not stats["enabled"]:
        return stats
    try:
        if db._conn is None:
            return stats
        stats["n_expired_purged"] = await purge_expired_enrollments(db)
        enrolled = await active_enrollments(db)
        stats["n_active"] = len(enrolled)
        if not enrolled:
            return stats

        cg_ids = [t for t, ns in enrolled if ns == "cg"]
        dex_ids = [t for t, ns in enrolled if ns == "dex"]
        stats["n_cg"] = len(cg_ids)
        stats["n_dex"] = len(dex_ids)
        stats["n_other"] = len(enrolled) - len(cg_ids) - len(dex_ids)

        if cg_ids:
            # Lazy import — held_position_prices imports aiohttp at module
            # load, which aborts on Windows dev boxes (OPENSSL_Applink).
            from scout.ingestion.held_position_prices import (
                _fetch_simple_price_batch,
                _shape_for_cache_prices,
            )

            batch = cg_ids[:_ENROLLMENT_CG_BATCH_MAX]  # ONE call per cycle
            resp = await _fetch_simple_price_batch(session, settings, batch)
            raw_coins = _shape_for_cache_prices(resp)
            if raw_coins:
                await db.cache_prices(raw_coins)
            stats["n_priced"] += len(raw_coins)

        if dex_ids:
            stats["n_priced"] += await _poll_dex_enrollments(db, session, dex_ids)

        log.info("ledger_enrollment_poll", **stats)
        return stats
    except Exception as exc:
        log.warning("ledger_enrollment_poll_failed", error=str(exc))
        return stats


def liquidity_from_signal_data(
    signal_data: dict[str, Any] | None,
) -> tuple[float | None, str]:
    """Extract (liquidity_usd, source_label) from a dispatcher's signal_data.

    Most signal_data payloads carry no liquidity (CG market surfaces don't
    have it); DEX-sourced dispatchers may pass ``liquidity_usd``. Returns
    ``(None, 'none')`` when absent/invalid — per the operator requirement:
    pass whatever the emitting site has, NULL otherwise.
    """
    if signal_data:
        for key in ("liquidity_usd", "liquidity"):
            value = signal_data.get(key)
            try:
                if value is not None and float(value) > 0:
                    return float(value), "signal_data"
            except (TypeError, ValueError):
                continue
    return None, "none"
