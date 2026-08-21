"""CoinGecko MONTHLY-CREDIT governor.

The 2026-08-21 incident was a resource-model defect, not a pricing problem: the
system modeled **calls/minute** (``coingecko_limiter``) while the hard
production constraint was **calls/month**. CoinGecko enforces those as two
independent limits, and only the second one has a wall you cannot back off
from — the Basic plan's 100,000 monthly credits hit 100.0% with 11 days to the
reset, and no amount of waiting released it.

This module owns the second axis. It does NOT touch rate limiting.

Two measures, because **attempts are not credits**
--------------------------------------------------
CoinGecko deducts a monthly credit on HTTP **200**. Unsuccessful 4xx/5xx calls
do **not** deduct a monthly credit, although they still count against the
per-minute rate limit. Counting every attempt as a credit would have massively
over-reported during the 429 storm (429s were ~40/hr while billing nothing), and
under a naive single counter the governor would have throttled the wrong axis.

  * ``attempts`` — every request issued. Rate/backoff observability.
  * ``credits``  — successful billable calls only. The budget axis.

Envelopes, not one shared pool
------------------------------
A single number lets discovery spend the allowance that keeps held positions
re-priceable — and an unpriceable open position is the GA-01 failure class. So
the allowance is partitioned, and **v1 deliberately does not let unused critical
reserve spill back into discovery**: that optimization needs a measured month
behind it, not an assumption.

Provider is the acceptance truth
--------------------------------
The local ledger is the *diagnostic/accounting* truth; CoinGecko's ``/key``
endpoint is the *acceptance* truth. They are reconciled rather than assumed
equal, because a divergence means our model of what bills is wrong — which is
itself the finding, and exactly the class of error that produced this incident.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)

# Buckets partition the monthly allowance. Keep this tuple as the single
# vocabulary — a bucket name that is not here is a programming error, not a new
# bucket, because an unrecognised name would silently accumulate against nothing.
BUCKET_DISCOVERY = "discovery"
BUCKET_CRITICAL = "critical"
BUCKET_OPERATIONAL = "operational"
BUCKETS: tuple[str, ...] = (BUCKET_DISCOVERY, BUCKET_CRITICAL, BUCKET_OPERATIONAL)


def billing_month(now: datetime | None = None) -> str:
    """Provider billing-period key, ``YYYY-MM`` in UTC.

    CoinGecko replenishes monthly credits on the 1st, so the calendar month in
    UTC is the period boundary. Derived from a passed-in ``now`` so tests can
    pin a month without patching the clock globally.
    """
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return f"{ts.year:04d}-{ts.month:02d}"


class CoinGeckoBudget:
    """In-memory credit ledger with durable write-through.

    Held in memory because it is consulted on every CoinGecko call and a DB
    round-trip per call would be its own budget problem; persisted because
    module counters reset on restart, and a budget that a service bounce zeroes
    is not a budget.
    """

    def __init__(self) -> None:
        self._month: str = billing_month()
        # (bucket) -> [attempts, credits]
        self._counts: dict[str, list[int]] = {b: [0, 0] for b in BUCKETS}
        self._dirty: bool = False
        # Last reconciliation against the provider, for divergence reporting.
        self.provider_credits_used: int | None = None
        self.provider_checked_at: datetime | None = None

    # -- lifecycle ---------------------------------------------------------

    def _roll_month_if_needed(self, now: datetime | None = None) -> bool:
        """Reset counters when the provider's billing period rolls over.

        Returns True if a roll happened. The reset is to ZERO rather than to a
        provider reading: at 00:00 on the 1st we have not yet spent anything,
        and seeding from a stale provider value would carry last month's
        consumption into the new envelope.
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
        self._dirty = True
        return True

    async def hydrate(self, db, now: datetime | None = None) -> None:
        """Load this month's counters from the ledger. Call once at startup."""
        self._roll_month_if_needed(now)
        conn = getattr(db, "_conn", None)
        if conn is None:
            return
        try:
            cur = await conn.execute(
                "SELECT bucket, attempts, credits FROM cg_credit_ledger "
                "WHERE month = ?",
                (self._month,),
            )
            rows = await cur.fetchall()
        except Exception:
            # A missing table (pre-migration) must not prevent the pipeline from
            # starting; it only means we begin the month at zero.
            log.exception("cg_budget_hydrate_failed", month=self._month)
            return
        for row in rows:
            bucket = row["bucket"] if not isinstance(row, tuple) else row[0]
            if bucket not in self._counts:
                # Unknown bucket in the table: retain it in the log rather than
                # dropping it silently, but do not invent a new envelope.
                log.warning("cg_budget_unknown_bucket_in_ledger", bucket=bucket)
                continue
            attempts = row["attempts"] if not isinstance(row, tuple) else row[1]
            credits = row["credits"] if not isinstance(row, tuple) else row[2]
            self._counts[bucket] = [int(attempts or 0), int(credits or 0)]
        log.info(
            "cg_budget_hydrated",
            month=self._month,
            credits=self.total_credits(),
            attempts=self.total_attempts(),
        )

    async def persist(self, db) -> None:
        """Write-through the in-memory counters. Cheap, idempotent, no-op when clean."""
        if not self._dirty:
            return
        conn = getattr(db, "_conn", None)
        if conn is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            for bucket, (attempts, credits) in self._counts.items():
                await conn.execute(
                    """INSERT INTO cg_credit_ledger
                         (month, bucket, attempts, credits, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(month, bucket) DO UPDATE SET
                         attempts = excluded.attempts,
                         credits = excluded.credits,
                         updated_at = excluded.updated_at""",
                    (self._month, bucket, attempts, credits, now_iso),
                )
            await conn.commit()
            self._dirty = False
        except Exception:
            # Never fatal to a pipeline cycle. The in-memory counters remain
            # authoritative for this process; the next persist retries.
            log.exception("cg_budget_persist_failed", month=self._month)

    # -- recording ---------------------------------------------------------

    def record(self, bucket: str, *, billable: bool, now: datetime | None = None) -> None:
        """Record one issued request.

        ``billable`` must be True only for a response that actually deducts a
        provider credit (HTTP 200). Passing ``resp.status < 400`` would be
        wrong for 3xx and is not what the provider bills on.
        """
        self._roll_month_if_needed(now)
        if bucket not in self._counts:
            # Fail loudly rather than accumulating into a phantom bucket.
            log.error("cg_budget_unknown_bucket", bucket=bucket, billable=billable)
            return
        self._counts[bucket][0] += 1
        if billable:
            self._counts[bucket][1] += 1
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

    def snapshot(self) -> dict:
        return {
            "month": self._month,
            "total_credits": self.total_credits(),
            "total_attempts": self.total_attempts(),
            **{f"{b}_credits": self.credits(b) for b in BUCKETS},
            **{f"{b}_attempts": self.attempts(b) for b in BUCKETS},
            "provider_credits_used": self.provider_credits_used,
        }

    # -- enforcement -------------------------------------------------------

    def discovery_exhausted(self, settings) -> bool:
        """True when discovery has spent its envelope.

        Only DISCOVERY is stopped. The critical reserve is preserved so held
        positions stay re-priceable — an open position that cannot be re-priced
        is the failure class this partition exists to prevent.
        """
        cap = int(getattr(settings, "COINGECKO_MONTHLY_DISCOVERY_CREDITS", 0) or 0)
        if cap <= 0:
            return False
        return self.credits(BUCKET_DISCOVERY) >= cap

    def projected_month_end_credits(self, now: datetime | None = None) -> float | None:
        """Linear projection of month-end consumption at the observed pace.

        Absolute 50/75/90% thresholds answer "how much have we spent", which is
        the wrong question mid-month: 60% on day 3 is an emergency and 60% on
        day 27 is fine. This answers "at this pace, where do we land".

        None when the elapsed window is too short to project from.
        """
        ts = now or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        month_start = ts.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        elapsed_days = (ts - month_start).total_seconds() / 86400.0
        if elapsed_days < 0.5:
            return None
        # Days in this calendar month.
        if ts.month == 12:
            next_start = month_start.replace(year=ts.year + 1, month=1)
        else:
            next_start = month_start.replace(month=ts.month + 1)
        days_in_month = (next_start - month_start).total_seconds() / 86400.0
        return self.total_credits() / elapsed_days * days_in_month


# Process-wide instance. Mirrors the existing `coingecko_limiter` singleton so
# every call site shares one view of the budget.
budget = CoinGeckoBudget()
