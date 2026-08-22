"""CoinGecko MONTHLY-CREDIT governor: accounting AND enforcement.

The 2026-08-21 incident was a resource-model defect, not a pricing problem: the
system modeled **calls/minute** (``coingecko_limiter``) while the hard
production constraint was **calls/month**. CoinGecko enforces those as two
independent limits, and only the second has a wall you cannot back off from --
the Basic plan's 100,000 monthly credits hit 100.0% with 11 days to the reset.

This module owns the second axis. It does NOT touch rate limiting.

Two measures, because **attempts are not credits**
--------------------------------------------------
CoinGecko deducts a monthly credit on HTTP **200**. Unsuccessful 4xx/5xx calls
do **not** deduct a monthly credit, although they still count against the
per-minute rate limit. Counting every attempt as a credit would have massively
over-reported during the 429 storm (429s ran ~40/hr while billing nothing).

Each issued request is counted **exactly once** as an attempt. The billable
outcome is recorded separately against that same attempt, so a response that
arrives and then fails to parse cannot inflate the attempt count.

Envelopes with DIFFERENT semantics per bucket
---------------------------------------------
A single pooled number lets discovery spend the allowance that keeps held
positions re-priceable, and an unpriceable open position is the GA-01 failure
class. But the buckets are not symmetric:

* ``discovery``   -- hard stop at its cap. Losing discovery loses opportunity.
* ``operational`` -- hard stop at its cap. Reconciliation must not eat the plan.
* ``critical``    -- **soft**. Re-pricing an ALREADY-OPEN position must not stop
  merely because the reserve is spent; refusing to re-price a live position is
  strictly worse than overspending a soft envelope, because it recreates the
  fabricated-$0 close. Exceeding the reserve instead blocks NEW opens and pages.

Enforcement lives at the HTTP choke point, not at any one caller, so a lane that
forgets to ask cannot spend anyway.

Provider is the acceptance truth
--------------------------------
The local ledger is the *diagnostic* truth; CoinGecko's ``/key`` endpoint is the
*acceptance* truth. Positive drift (provider says more used than we counted)
means spend we cannot attribute -- an un-instrumented call path, a
multi-credit endpoint, another consumer of the same key. That is not merely a
log line: it is capacity that is really gone, so it is subtracted from
non-critical capacity rather than reported and ignored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

log = structlog.get_logger(__name__)

BUCKET_DISCOVERY = "discovery"
BUCKET_CRITICAL = "critical"
BUCKET_OPERATIONAL = "operational"
BUCKETS: tuple[str, ...] = (BUCKET_DISCOVERY, BUCKET_CRITICAL, BUCKET_OPERATIONAL)

# Buckets whose envelope is a HARD stop. `critical` is deliberately absent --
# see the module docstring; stopping re-pricing of an open position is worse
# than overspending the reserve.
HARD_STOP_BUCKETS: frozenset[str] = frozenset({BUCKET_DISCOVERY, BUCKET_OPERATIONAL})


def billing_month(now: datetime | None = None) -> str:
    """Provider billing-period key, ``YYYY-MM`` in UTC.

    CoinGecko replenishes monthly credits on the 1st, so the UTC calendar month
    is the period boundary. Takes ``now`` so tests can pin a month without
    patching the clock globally.
    """
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return f"{ts.year:04d}-{ts.month:02d}"


class CoinGeckoBudget:
    """In-memory credit ledger with bounded write-through and enforcement.

    Held in memory because it is consulted on every CoinGecko call and a DB
    round-trip per call would be its own budget problem; persisted on a bounded
    cadence because module counters reset on restart, and a budget a service
    bounce clears is not a budget.
    """

    def __init__(self) -> None:
        self._month: str = billing_month()
        self._counts: dict[str, list[int]] = {b: [0, 0] for b in BUCKETS}
        self._dirty: bool = False
        self._unpersisted_credits: int = 0
        # Provider liveness: when a CoinGecko call last returned HTTP 200.
        # This is the CG-SPECIFIC heartbeat. It is NOT price_cache freshness --
        # price_cache is written by DexScreener too (outcome_ledger's dex
        # enrollment poller), so a fresh price_cache row proves nothing about
        # CoinGecko.
        self.last_success_at: datetime | None = None
        # Reconciliation against the provider.
        self.provider_credits_used: int | None = None
        self.provider_checked_at: datetime | None = None
        # Pace-alert rearm state: alert on the CROSSING, not on the condition.
        self._pace_alerted: bool = False

    # -- lifecycle ---------------------------------------------------------

    def _roll_month_if_needed(self, now: datetime | None = None) -> bool:
        """Reset counters when the provider billing period rolls over.

        Resets to ZERO rather than to a provider reading: at 00:00 on the 1st
        nothing has been spent, and seeding from a stale provider value would
        carry last month's consumption into the new envelope.
        """
        current = billing_month(now)
        if current == self._month:
            return False
        log.info(
            "cg_budget_month_rolled",
            previous_month=self._month,
            new_month=current,
            previous_credits=self.total_credits(),
        )
        self._month = current
        self._counts = {b: [0, 0] for b in BUCKETS}
        self.provider_credits_used = None
        self.provider_checked_at = None
        self._pace_alerted = False
        self._dirty = True
        self._unpersisted_credits = 0
        return True

    async def hydrate(self, db, now: datetime | None = None) -> None:
        """Load this month's counters from the ledger. Call once at startup."""
        self._roll_month_if_needed(now)
        conn = getattr(db, "_conn", None)
        if conn is None:
            return
        try:
            cur = await conn.execute(
                "SELECT bucket, attempts, credits, last_success_at "
                "FROM cg_credit_ledger WHERE month = ?",
                (self._month,),
            )
            rows = await cur.fetchall()
        except Exception:
            # A missing table (pre-migration) must not stop the pipeline; it
            # only means we begin the month at zero.
            log.exception("cg_budget_hydrate_failed", month=self._month)
            return
        for row in rows:
            bucket = row["bucket"]
            if bucket not in self._counts:
                log.warning("cg_budget_unknown_bucket_in_ledger", bucket=bucket)
                continue
            self._counts[bucket] = [
                int(row["attempts"] or 0),
                int(row["credits"] or 0),
            ]
            raw_success = row["last_success_at"]
            if raw_success:
                try:
                    parsed = datetime.fromisoformat(str(raw_success))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    if self.last_success_at is None or parsed > self.last_success_at:
                        self.last_success_at = parsed
                except ValueError:
                    log.warning("cg_budget_bad_last_success_at", raw=str(raw_success))
        log.info(
            "cg_budget_hydrated",
            month=self._month,
            credits=self.total_credits(),
            attempts=self.total_attempts(),
            last_success_at=(
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
        )

    async def persist(self, db, *, force: bool = False) -> None:
        """Write-through the counters. Cheap, idempotent, no-op when clean."""
        if not self._dirty and not force:
            return
        conn = getattr(db, "_conn", None)
        if conn is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        success_iso = (
            self.last_success_at.isoformat() if self.last_success_at else None
        )
        try:
            for bucket, (attempts, credits) in self._counts.items():
                await conn.execute(
                    """INSERT INTO cg_credit_ledger
                         (month, bucket, attempts, credits, last_success_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(month, bucket) DO UPDATE SET
                         attempts = excluded.attempts,
                         credits = excluded.credits,
                         last_success_at = COALESCE(
                           excluded.last_success_at, cg_credit_ledger.last_success_at),
                         updated_at = excluded.updated_at""",
                    (self._month, bucket, attempts, credits, success_iso, now_iso),
                )
            await conn.commit()
            self._dirty = False
            self._unpersisted_credits = 0
        except Exception:
            # Never fatal to a pipeline cycle. In-memory counters remain
            # authoritative for this process; the next flush retries.
            log.exception("cg_budget_persist_failed", month=self._month)

    async def maybe_persist(self, db, settings) -> None:
        """Flush when enough unpersisted spend has accumulated.

        A budget persisted only by the hourly maintenance pass loses up to an
        hour of spend to a crash or deploy, and the process comes back believing
        it has capacity it already burned. This bounds that window by CREDITS
        rather than by time, because the risk scales with spend, not with clock.
        """
        threshold = int(getattr(settings, "COINGECKO_BUDGET_FLUSH_EVERY_CREDITS", 0) or 0)
        if threshold > 0 and self._unpersisted_credits >= threshold:
            await self.persist(db)

    # -- recording ---------------------------------------------------------

    def record(
        self, bucket: str, *, billable: bool, now: datetime | None = None
    ) -> None:
        """Record one issued request and its billable outcome.

        Call EXACTLY ONCE per issued HTTP request. ``billable`` is True only for
        a response that actually deducts a provider credit (HTTP 200).
        """
        self._roll_month_if_needed(now)
        if bucket not in self._counts:
            # Fail loudly rather than accumulating into a phantom bucket.
            log.error("cg_budget_unknown_bucket", bucket=bucket, billable=billable)
            return
        self._counts[bucket][0] += 1
        if billable:
            self._counts[bucket][1] += 1
            self._unpersisted_credits += 1
            # A billable 200 IS the CoinGecko liveness heartbeat.
            self.last_success_at = now or datetime.now(timezone.utc)
        self._dirty = True

    # -- readings ----------------------------------------------------------

    @property
    def month(self) -> str:
        return self._month

    def credits(self, bucket: str) -> int:
        return self._counts.get(bucket, [0, 0])[1]

    def attempts(self, bucket: str) -> int:
        return self._counts.get(bucket, [0, 0])[0]

    def total_credits(self) -> int:
        return sum(c for _, c in self._counts.values())

    def total_attempts(self) -> int:
        return sum(a for a, _ in self._counts.values())

    def unattributed_provider_drift(self) -> int:
        """Provider-reported spend we could not attribute locally.

        Only POSITIVE drift matters. Provider-below-local means our accounting
        is conservative (harmless); provider-above-local means real capacity is
        gone that no local counter explains -- an un-instrumented path, a
        multi-credit endpoint, or another consumer of the same key.
        """
        if self.provider_credits_used is None:
            return 0
        return max(0, self.provider_credits_used - self.total_credits())

    def effective_used(self) -> int:
        """Credits genuinely consumed this month, provider-truth-corrected."""
        return self.total_credits() + self.unattributed_provider_drift()

    def cg_pricing_live(self, settings, now: datetime | None = None) -> bool:
        """Has CoinGecko itself served us recently?

        Deliberately NOT ``MAX(price_cache.updated_at)``: price_cache is written
        by DexScreener too (``outcome_ledger._poll_dex_enrollments`` writes
        ``dex:`` rows through ``Database.cache_prices``). A fresh Dex row would
        make a dead CoinGecko look alive, admitting a CG-only position that
        nothing can re-price -- and a stale-but-healthy CG token would be
        blocked by an unrelated provider's silence.
        """
        max_age = int(getattr(settings, "PAPER_OPEN_CG_PRICING_MAX_AGE_SEC", 0) or 0)
        if max_age <= 0:
            return True
        if self.last_success_at is None:
            # Unknown is NOT live. Conflating "never observed" with "fresh" is
            # how this class recurs.
            return False
        ts = now or datetime.now(timezone.utc)
        return (ts - self.last_success_at) <= timedelta(seconds=max_age)

    def snapshot(self) -> dict:
        return {
            "month": self._month,
            "total_credits": self.total_credits(),
            "total_attempts": self.total_attempts(),
            "effective_used": self.effective_used(),
            "unattributed_drift": self.unattributed_provider_drift(),
            **{f"{b}_credits": self.credits(b) for b in BUCKETS},
            **{f"{b}_attempts": self.attempts(b) for b in BUCKETS},
            "provider_credits_used": self.provider_credits_used,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
        }

    # -- enforcement -------------------------------------------------------

    def _cap_for(self, bucket: str, settings) -> int:
        return int(
            getattr(settings, f"COINGECKO_MONTHLY_{bucket.upper()}_CREDITS", 0) or 0
        )

    def allow(self, bucket: str, settings) -> tuple[bool, str]:
        """May a request in ``bucket`` be issued? Returns (allowed, reason).

        The single enforcement predicate, consulted at the HTTP choke point so a
        caller that forgets to ask still cannot spend.
        """
        if bucket not in self._counts:
            return False, "unknown_bucket"

        allowance = int(
            getattr(settings, "COINGECKO_MONTHLY_CREDIT_ALLOWANCE", 0) or 0
        )
        # Overall plan ceiling, corrected by provider truth. Unattributed spend
        # is real capacity gone; letting it be invisible is what allowed the
        # reserve to be eaten silently.
        if allowance > 0 and self.effective_used() >= allowance:
            if bucket == BUCKET_CRITICAL:
                # Still soft: re-pricing an open position outranks the ceiling.
                # The provider will refuse us anyway; that is its decision, not
                # ours to pre-empt into a fabricated close.
                return True, "critical_soft_over_allowance"
            return False, "monthly_allowance_exhausted"

        cap = self._cap_for(bucket, settings)
        used = self.credits(bucket)
        if bucket == BUCKET_DISCOVERY:
            # Unattributed provider drift is charged to DISCOVERY. It is real
            # capacity gone that no local counter explains, and it has to land
            # somewhere or it silently eats the reserve instead. Discovery is
            # the correct place: it is the largest non-critical envelope and the
            # one whose loss is least harmful. Charging it to `critical` would
            # invert the whole point of reserving re-pricing capacity.
            used += self.unattributed_provider_drift()
        if cap > 0 and used >= cap:
            if bucket in HARD_STOP_BUCKETS:
                return False, f"{bucket}_envelope_exhausted"
            return True, f"{bucket}_envelope_exceeded_soft"
        return True, "ok"

    def critical_reserve_exceeded(self, settings) -> bool:
        """True when the critical reserve is spent.

        Does NOT stop re-pricing (see ``allow``). It is the signal to stop
        opening NEW positions and to page: more open positions means more
        re-pricing demand against a reserve that is already gone.
        """
        cap = self._cap_for(BUCKET_CRITICAL, settings)
        return cap > 0 and self.credits(BUCKET_CRITICAL) >= cap

    def discovery_exhausted(self, settings) -> bool:
        allowed, _ = self.allow(BUCKET_DISCOVERY, settings)
        return not allowed

    def projected_month_end_credits(self, now: datetime | None = None) -> float | None:
        """Linear projection of month-end consumption at the observed pace.

        Absolute 50/75/90% marks answer "how much have we spent", which is the
        wrong question mid-month: 60% on day 3 is an emergency and 60% on day 27
        is fine. This answers "at this pace, where do we land".

        Uses ``effective_used`` so unattributed provider spend is projected too.
        None when too little of the month has elapsed to project from.
        """
        ts = now or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        month_start = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elapsed_days = (ts - month_start).total_seconds() / 86400.0
        if elapsed_days < 0.5:
            return None
        if ts.month == 12:
            next_start = month_start.replace(year=ts.year + 1, month=1)
        else:
            next_start = month_start.replace(month=ts.month + 1)
        days_in_month = (next_start - month_start).total_seconds() / 86400.0
        return self.effective_used() / elapsed_days * days_in_month

    def should_page_on_pace(self, settings, now: datetime | None = None) -> bool:
        """Page on the CROSSING, not on the condition.

        The hourly pass re-evaluates the projection every hour. Paging whenever
        it is over threshold would produce ~24 pages/day for one unchanged fact,
        and an operator who learns to ignore the alarm is worse off than one who
        never had it. Fires once per crossing and rearms only after the
        projection recovers below the threshold.
        """
        projected = self.projected_month_end_credits(now)
        allowance = int(
            getattr(settings, "COINGECKO_MONTHLY_CREDIT_ALLOWANCE", 0) or 0
        )
        if projected is None or allowance <= 0:
            return False
        ratio = projected / allowance
        threshold = float(
            getattr(settings, "COINGECKO_BUDGET_PACE_ALERT_RATIO", 1.10) or 1.10
        )
        if ratio >= threshold:
            if self._pace_alerted:
                return False
            self._pace_alerted = True
            return True
        # Hysteresis: rearm only once clearly back under, so a projection
        # hovering on the threshold cannot flap the pager.
        if ratio < threshold * 0.95:
            self._pace_alerted = False
        return False


class governed_cg_call:
    """Context manager making a hand-rolled CoinGecko request governed.

    Some CoinGecko callers cannot use ``_get_with_backoff`` because they own
    their own retry/backoff loops (the narrative lanes and the counter detail
    fetch). Rewriting those loops to fit one signature would be a large,
    risk-bearing refactor of code this repair has no other reason to touch --
    but leaving them ungoverned means the monthly model is a fiction, which is
    exactly the "62k/month is not a complete budget" failure.

    So they keep their loops and borrow the accounting/enforcement instead::

        with governed_cg_call(BUCKET_DISCOVERY, settings) as call:
            if not call.allowed:
                return []
            async with session.get(url) as resp:
                call.billable = resp.status == 200

    Records EXACTLY ONCE on exit, on every path including exceptions, so a
    request cannot be double-counted or lost. Every such exception is
    enumerated by tests/test_no_ungoverned_coingecko_paths.py -- a new direct
    CoinGecko call that adopts neither route fails that test.
    """

    __slots__ = ("bucket", "settings", "allowed", "reason", "billable", "_recorded")

    def __init__(self, bucket: str, settings=None) -> None:
        self.bucket = bucket
        self.settings = settings
        self.billable = False
        self._recorded = False
        if settings is None:
            self.allowed, self.reason = True, "unenforced_no_settings"
        else:
            self.allowed, self.reason = budget.allow(bucket, settings)

    def finish(self, status: int | None) -> None:
        """Record this issued request exactly once, given its HTTP status.

        The non-context-manager form, for callers whose retry loop shape makes
        a ``with`` block awkward. Idempotent: a second call is ignored, so a
        retry loop that creates one instance per attempt cannot double-count and
        one that reuses an instance cannot either.
        """
        if self._recorded or not self.allowed:
            return
        self._recorded = True
        budget.record(self.bucket, billable=(status == 200))

    def __enter__(self) -> "governed_cg_call":
        if not self.allowed:
            log.warning(
                "cg_request_refused_by_budget",
                bucket=self.bucket,
                reason=self.reason,
                month=budget.month,
                bucket_credits=budget.credits(self.bucket),
                effective_used=budget.effective_used(),
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # A refused call was never issued, so it is not an attempt either.
        if self.allowed and not self._recorded:
            self._recorded = True
            budget.record(self.bucket, billable=self.billable)
        return False


# Process-wide instance, mirroring the existing `coingecko_limiter` singleton so
# every call site shares one view of the budget.
budget = CoinGeckoBudget()
