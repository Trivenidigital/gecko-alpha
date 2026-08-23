"""Versioned recomputation of legacy chain-detection provenance (ruling C).

WHY THIS EXISTS
---------------
The canonical-identity rule removes prefix-derived evidence. Applied naively it
also removes every PRE-CUTOVER row's chains credit, because those rows predate
the `chains_identity_tier` column and so cannot prove their provenance either
way. Measured on production that is 826 of 1188 gainers rows and `tier_high`
341 -> 187 -- a 45% drop that is mostly a metadata artefact, not a change in
detection quality.

So `legacy_prefix` does not mean "bad". It means **"historical semantics need
provenance resolution"**, and this module resolves it.

WHAT IT PRODUCES
----------------
A SEPARATE versioned derived result. The archived `*_legacy_prefix_v1` rows are
never updated: pretending a legacy row always had canonical provenance would
destroy the evidence the archive exists to preserve.

THE COVERAGE PROBLEM, WHICH IS THE HARD PART
--------------------------------------------
`signal_first_seen` was backfilled from retention-bounded history and only ever
moves minima EARLIER prospectively. Measured on production 2026-08-23:

    substrate floor            2026-08-09T00:40:48
    legacy anchors span        2026-04-16 .. 2026-08-18
    anchors after the floor    237 of 1188
    anchors before it          951 of 1188

So for most legacy rows a missing canonical match is NOT evidence that none
existed -- it is left-censoring. Treating absence as proof would silently
manufacture the same false negatives this whole workstream exists to remove,
just in the other direction.

Hence three outcomes, never two: **verified canonical**, **verified prefix-only**,
and **indeterminate**.

THE MONOTONIC SHORTCUT, AND ITS LIMIT
-------------------------------------
Missing older history can only make a true lead LARGER **within a tier**. So an
observed lead >= the conviction gate is safe under left-censoring, and the
converse does not hold: a lead below the gate cannot be called a negative,
because earlier history may simply be gone.

**That argument does NOT hold ACROSS tiers, and an earlier version of this module
claimed it did.** `identity.resolve` picks tier first, then earliest within tier.
Censoring can hide the canonical-id token entirely, letting a lower-tier
alias -- bare symbol equality, possibly a DIFFERENT ASSET -- win with an earlier
first_seen. Restoring history reinstates the correct, LATER, canonical match and
the lead COLLAPSES. Executed against the real resolver:

    censored  [("luna", 08-15)]                    -> alias_unique,  lead 7200
    restored  + ("terra-luna-2", 08-19T23:00)      -> canonical_id,  lead   60

`terra-luna-2 <- luna` is not hypothetical: it is one of the eight named
production identity errors this workstream exists to remove.

So a verified positive requires a tier that censoring cannot mask:
`contract` or `canonical_id`, where the winner IS the coin and no higher tier
can appear later. An `alias_unique` win is recorded as indeterminate instead.

That also disposes of a justification that does not transfer: symbol equality
was admitted as tier 3 because the impact study measured it deciding ZERO
winners -- but that study ran on the live, uncensored substrate. The recompute
replays over deliberately censored history, where canonical ids are exactly what
is missing and short symbol-tokens are what survive. This is the one context
where symbol equality decides winners, which is precisely where it must not.
"""

from __future__ import annotations

from dataclasses import dataclass

from scout.identity import (
    TIER_ALIAS_UNIQUE,
    TIER_CANONICAL_ID,
    TIER_CONTRACT,
    TIER_UNRESOLVED,
    Resolution,
)

RECOMPUTE_SEMANTICS = "chain_identity_recompute_v1"

#: Canonical identity established AND the observed lead clears the gate. Safe
#: under left-censoring by the monotonic argument above.
STATUS_VERIFIED_CANONICAL = "verified_canonical"
#: Canonical identity established, but the observed lead is below the gate.
#: NOT a negative: earlier canonical history may be pruned, so the true lead
#: could clear it. Indeterminate for gate purposes.
STATUS_CANONICAL_SUB_GATE = "canonical_below_gate_indeterminate"
#: Coverage is good enough to have found a canonical match, none exists, and a
#: prefix candidate explains the legacy credit. The old credit was fuzzy.
STATUS_PREFIX_ONLY = "verified_prefix_only"
#: Two or more distinct tokens claim the identity; not an identity assertion.
STATUS_AMBIGUOUS = "ambiguous_identity"
#: History does not reach far enough back to decide either way.
STATUS_INDETERMINATE = "indeterminate_history"
#: The legacy row never had chains credit; there is nothing to recover.
STATUS_NO_LEGACY_CREDIT = "no_legacy_credit"
#: Archived row carrying no usable join key (no coin_id, or no anchor). Not a
#: history problem -- the row cannot be correlated to anything at all.
STATUS_UNJOINABLE_ROW = "unjoinable_row"

#: Statuses that earn chains conviction credit. Exactly one.
CREDIT_BEARING = frozenset({STATUS_VERIFIED_CANONICAL})


@dataclass(frozen=True)
class RecomputeRow:
    """One legacy comparison row's resolved provenance."""

    source_table: str
    source_row_id: int
    coin_id: str
    symbol: str
    historical_anchor: str
    legacy_detected: bool
    legacy_lead: float | None
    canonical_detected: bool
    canonical_lead: float | None
    identity_tier: str
    evidence_status: str
    semantics_version: str = RECOMPUTE_SEMANTICS

    @property
    def earns_chain_credit(self) -> bool:
        return self.evidence_status in CREDIT_BEARING


def _lead_minutes(anchor_jd: float, seen_jd: float) -> float:
    return max(0.0, (anchor_jd - seen_jd) * 1440.0)


def classify(
    *,
    resolution: Resolution,
    legacy_detected: bool,
    legacy_lead: float | None,
    canonical_lead: float | None,
    anchor_covered: bool,
    gate_minutes: float,
) -> str:
    """Decide a legacy row's evidence status.

    ``anchor_covered`` is the coverage predicate: True when history reaches far
    enough back that a canonical match, had one existed, would have been found.
    It is what separates "we looked and it is not there" from "we cannot see
    that far".
    """
    if not legacy_detected:
        return STATUS_NO_LEGACY_CREDIT

    if resolution.tier == TIER_UNRESOLVED and resolution.alias_candidates:
        return STATUS_AMBIGUOUS

    if resolution.tier in (TIER_CONTRACT, TIER_CANONICAL_ID):
        # The winner IS the coin, so no higher tier can appear later to displace
        # it and the monotonic argument holds: restoring history can only move
        # this first_seen earlier. A lead at or above the gate is therefore safe
        # under left-censoring; below it we cannot separate "genuinely late"
        # from "earlier history pruned".
        if canonical_lead is not None and canonical_lead >= gate_minutes:
            return STATUS_VERIFIED_CANONICAL
        return STATUS_CANONICAL_SUB_GATE

    if resolution.tier == TIER_ALIAS_UNIQUE:
        # NEVER a verified positive under possibly-censored history. A hidden
        # canonical-id token would outrank this on tier and could carry a LATER
        # first_seen, collapsing the lead -- so an alias win can be an artefact
        # of what is missing rather than evidence of what happened.
        #
        # This used to promote on `anchor_covered`, described as "safe once
        # coverage establishes that no canonical-id token was hidden".
        # `anchor_covered` cannot establish that. Coverage intervals are built
        # from MIN/MAX over ALL tokens' events -- a GLOBAL span. They say "we
        # were recording during this window", never "THIS coin's canonical-id
        # token would have been seen". Review reproduced the failure on this
        # module's own worked example: `terra-luna-2 <- luna` stamped
        # verified_canonical at a 7,200-minute lead on censored history, which
        # collapses to 60 once the real history is restored -- exactly the
        # error the module docstring says the design forecloses.
        #
        # So honour the stated rule: an alias win is indeterminate. The
        # asymmetry is the point -- this branch manufactured a false POSITIVE,
        # granting unearned credit, where the same predicate's other uses
        # produce negatives and fail safe.
        return STATUS_CANONICAL_SUB_GATE

    # No canonical identity found.
    if not anchor_covered:
        # Absence proves nothing this far back.
        return STATUS_INDETERMINATE
    if resolution.prefix_token_id is not None:
        # Coverage is adequate, nothing canonical matches, and a prefix
        # candidate explains the old credit.
        return STATUS_PREFIX_ONLY
    # Covered, and nothing matches at all -- the legacy credit cannot be
    # reproduced from any identity basis. Not provably prefix, so not a
    # positive claim either.
    return STATUS_INDETERMINATE


#: The three comparison surfaces and the column carrying each one's anchor.
_SURFACES = {
    "gainers_comparisons": "appeared_on_gainers_at",
    "losers_comparisons": "appeared_on_losers_at",
    "trending_comparisons": "appeared_on_trending_at",
}


def _anchor_is_covered(anchor: str, intervals) -> bool:
    """Is there history spanning the moment this anchor needs?

    An earlier version used a single global floor -- `anchor > min(all history)`
    -- which licenses PER-TOKEN evidence from a GLOBAL scalar and fails three
    ways:

      * the snapshot union has GAPS (2026-07-03..07-17, 2026-08-02..08-08).
        An anchor inside a gap sits far above the global floor, so absence there
        was read as proof;
      * a token whose only events fell in a gap is in no source at all, yet its
        coin's anchor was "covered" by a floor derived from a DIFFERENT token;
      * worst, ONE ancient token dragged the floor back far enough to mark
        essentially every anchor covered -- collapsing the three-outcome design
        to two, so `indeterminate` would almost never fire and every unmatched
        row became an asserted `verified_prefix_only`.

    Intervals answer the question actually being asked. Outside them the answer
    is "we cannot see", which is not the same as "it is not there".
    """
    if not anchor or not intervals:
        return False
    return any(start <= anchor <= end for start, end in intervals)


async def substrate_floor(conn) -> str | None:
    """Earliest first-seen the substrate knows about, i.e. the coverage edge.

    Everything before this is invisible: the substrate was backfilled from
    retention-bounded history, so no evidence exists on the far side of it.
    """
    cur = await conn.execute("SELECT MIN(first_seen_at) FROM signal_first_seen")
    row = await cur.fetchone()
    return row[0] if row else None


async def recompute_legacy_provenance(
    conn,
    *,
    gate_minutes: float = 1440.0,
    semantics_column: str = "chains_identity_semantics",
    extra_history: dict[str, str] | None = None,
    coverage_intervals: list[tuple[str, str]] | None = None,
) -> dict:
    """Resolve every archived legacy row's provenance into the versioned table.

    Reads the IMMUTABLE `*_legacy_prefix_v1` archives, never the live tables, so
    a tracker recompute cannot race it and the archived evidence is untouched.

    Idempotent: the result is keyed on (source_table, source_row_id) and
    REPLACEd, so a rerun over unchanged inputs produces an unchanged table.

    ``extra_history`` is an optional ``{token_id: earliest_iso}`` map of history
    from OUTSIDE the live substrate. It exists because the live substrate alone
    recovers only 155 of 826 production rows (18.8%) -- its floor is
    2026-08-09 while legacy anchors reach back to April -- whereas the preserved
    /root snapshots take that to 538 (65.1%).

    Production passes nothing here: the pipeline must not depend on ad-hoc
    operator files under /root. The offline backfill script supplies them, and
    the result lands in this same versioned table, so the runtime reads one
    source of truth either way.
    """
    from scout.identity import resolve

    floor = await substrate_floor(conn)
    if coverage_intervals is None:
        # UNDECLARED -> default to the substrate's own window. An EMPTY list is
        # a different statement: it declares that nothing is covered, and must
        # not silently widen to the default. `or []` conflated the two, which is
        # the fail-open shape -- a caller asserting "no coverage" would have got
        # full coverage.
        intervals = [(floor, "9999-12-31T23:59:59+00:00")] if floor else []
    else:
        intervals = list(coverage_intervals)
    counts: dict[str, int] = {}

    # Normalise ONCE. Doing datetime()/julianday() per substrate row per legacy
    # row is 2,617 x 1,188 = 3.1M SQL round-trips; precomputing makes the whole
    # replay a single pass.
    cur = await conn.execute(
        "SELECT token_id, first_seen_at, datetime(first_seen_at), "
        "julianday(first_seen_at) FROM signal_first_seen "
        "WHERE datetime(first_seen_at) IS NOT NULL"
    )
    substrate = [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]

    # Fold in out-of-substrate history, keeping the EARLIER value per token. It
    # can only move a first-seen earlier, which is the safe direction: a lead
    # can grow, never shrink, so nothing that already qualified stops
    # qualifying.
    if extra_history:
        by_token = {t: (t, fs, nfs, jd) for t, fs, nfs, jd in substrate}
        for token_id, earliest in extra_history.items():
            if not token_id or not earliest:
                continue
            known = by_token.get(token_id)
            if known is not None and known[1] <= earliest:
                continue
            cur = await conn.execute(
                "SELECT datetime(?), julianday(?)", (earliest, earliest)
            )
            nfs, jd = await cur.fetchone()
            if nfs is None:
                continue
            by_token[token_id] = (token_id, earliest, nfs, jd)
        substrate = list(by_token.values())

    # Built ONCE. This is loop-invariant -- the substrate does not change
    # while the replay runs -- and every millisecond it wasted was spent
    # holding the write transaction open against a live pipeline.
    jd_by_value = {first_seen: jd for _t, first_seen, _n, jd in substrate}

    for source_table, anchor_col in _SURFACES.items():
        archive = f"{source_table}_legacy_prefix_v1"
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (archive,)
        )
        if not await cur.fetchone():
            continue

        cur = await conn.execute(f"PRAGMA table_info({archive})")
        cols = {r[1] for r in await cur.fetchall()}
        if not {"coin_id", "symbol", anchor_col}.issubset(cols):
            continue
        sem = semantics_column if semantics_column in cols else None

        cur = await conn.execute(
            f"""SELECT id, coin_id, symbol, {anchor_col},
                       COALESCE(detected_by_chains, 0), chains_lead_minutes
                  FROM {archive}
                 {"WHERE COALESCE(" + sem + ", 'legacy_prefix') != 'canonical_v1'" if sem else ""}"""
        )
        rows = await cur.fetchall()

        for row_id, coin_id, symbol, anchor, legacy_detected, legacy_lead in rows:
            if not coin_id or not anchor:
                # Counted, not silently dropped. The acceptance report claims a
                # FULL replay of the archived population, and a status
                # breakdown that quietly omits a row cannot be reconciled
                # against the row count -- the totals would simply fail to sum,
                # with nothing saying why. A row with no coin_id or no anchor
                # is unjoinable, so it gets a terminal status of its own rather
                # than being folded into `indeterminate_history`, which means
                # something different: history we could not reach.
                counts[STATUS_UNJOINABLE_ROW] = counts.get(STATUS_UNJOINABLE_ROW, 0) + 1
                continue
            # PER-SURFACE bound. gainers/trending use the +5min tolerance with
            # both sides normalised; losers uses a bare `first_seen_at < ?`.
            # scout/identity.py documents that the two are NOT interchangeable,
            # and an earlier version of this loop applied +5min uniformly while
            # asserting in a comment that the bound was "identical to the one
            # that produced the legacy value" -- so losers rows could be
            # verified on candidates the canonical losers path would refuse.
            #
            # ONE decision, used for both halves. The cutoff EXPRESSION and the
            # comparison it feeds have to move together: written as two
            # independent `source_table ==` checks, flipping only the first
            # left a `datetime()` cutoff (space-separated) being compared
            # against raw isoformat-`T` values, and `'T'`(0x54) > `' '`(0x20).
            # Review showed that mutant surviving the whole suite -- the two
            # halves silently disagreeing is exactly the failure the
            # per-surface split was introduced to prevent.
            raw_bound = source_table == "losers_comparisons"
            if raw_bound:
                cur = await conn.execute("SELECT ?, julianday(?)", (anchor, anchor))
            else:
                cur = await conn.execute(
                    "SELECT datetime(?, '+5 minutes'), julianday(?)", (anchor, anchor)
                )
            cutoff, anchor_jd = await cur.fetchone()

            candidates = [
                (token_id, first_seen)
                for token_id, first_seen, nfs, _jd in substrate
                if cutoff is not None
                and ((first_seen < cutoff) if raw_bound else (nfs < cutoff))
            ]

            res = resolve(candidates, coin_id, symbol or "")

            canonical_lead = None
            if res.first_seen_at is not None and anchor_jd is not None:
                seen_jd = jd_by_value.get(res.first_seen_at)
                if seen_jd is not None:
                    canonical_lead = round(_lead_minutes(anchor_jd, seen_jd), 1)

            anchor_covered = _anchor_is_covered(anchor, intervals)
            status = classify(
                resolution=res,
                legacy_detected=bool(legacy_detected),
                legacy_lead=legacy_lead,
                canonical_lead=canonical_lead,
                anchor_covered=anchor_covered,
                gate_minutes=gate_minutes,
            )
            counts[status] = counts.get(status, 0) + 1

            await conn.execute(
                """INSERT OR REPLACE INTO chain_identity_recompute_v1
                   (source_table, source_row_id, coin_id, symbol, historical_anchor,
                    legacy_detected, legacy_lead, canonical_detected, canonical_lead,
                    identity_tier, evidence_status, semantics_version, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    source_table,
                    row_id,
                    coin_id,
                    symbol,
                    anchor,
                    1 if legacy_detected else 0,
                    legacy_lead,
                    1 if res.detected else 0,
                    canonical_lead,
                    res.tier,
                    status,
                    RECOMPUTE_SEMANTICS,
                ),
            )

        # Commit PER SURFACE, not once at the end. A single transaction
        # spanning all three held the write lock for the entire replay --
        # measured at 6.6s against a live pipeline, which blocked for 5.7s
        # behind it. Three shorter transactions bound how long anything
        # else waits, and let completed surfaces survive a later failure.
        await conn.commit()

    return counts


async def reconciliation_report(conn) -> dict[str, int]:
    """Denominators for the acceptance table, queried rather than assumed.

    `sum(counts)` from a replay cannot be checked against anything on its own.
    It equals the NON-CANONICAL archived rows -- not every archived row -- and
    rows classified `unjoinable_row` are counted but never written. Three
    different numbers can all reasonably be called "the population", and an
    acceptance table that does not say which one it used cannot be reconciled
    against the table it describes.

    Deliberately NOT folded into `recompute_legacy_provenance`'s return value:
    callers sum that dict, so adding non-status keys would silently corrupt
    every `sum(counts.values())`.

    `stored` is read back from the overlay rather than tallied during the
    write, because `INSERT OR REPLACE` on (source_table, source_row_id)
    collapses a duplicate pair silently -- two archived rows in, one row out,
    no error. "Rows we wrote" and "rows that exist" are separate claims.
    """
    out = {"population": 0, "skipped_canonical": 0, "stored": 0}
    for source_table in _SURFACES:
        archive = f"{source_table}_legacy_prefix_v1"
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (archive,)
        )
        if not await cur.fetchone():
            continue
        cur = await conn.execute(f"SELECT COUNT(*) FROM {archive}")
        out["population"] += (await cur.fetchone())[0]

        cur = await conn.execute(f"PRAGMA table_info({archive})")
        if "chains_identity_semantics" in {r[1] for r in await cur.fetchall()}:
            cur = await conn.execute(
                f"SELECT COUNT(*) FROM {archive} WHERE "
                "COALESCE(chains_identity_semantics, 'legacy_prefix') = 'canonical_v1'"
            )
            out["skipped_canonical"] += (await cur.fetchone())[0]

    cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='chain_identity_recompute_v1'"
    )
    if await cur.fetchone():
        cur = await conn.execute("SELECT COUNT(*) FROM chain_identity_recompute_v1")
        out["stored"] = (await cur.fetchone())[0]
    out["replayed"] = out["population"] - out["skipped_canonical"]
    return out
