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

THE MONOTONIC SHORTCUT
----------------------
Missing older history can only make a true lead LARGER, never smaller. So an
observed canonical lead >= the conviction gate is SAFE under left-censoring: the
true lead is at least the observed one. The converse does not hold -- an
observed lead below the gate cannot be called a negative, because earlier
canonical history may simply be gone. That asymmetry is the only thing that lets
any row be verified at all under incomplete coverage.
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

    if resolution.tier in (TIER_CONTRACT, TIER_CANONICAL_ID, TIER_ALIAS_UNIQUE):
        # Canonical identity established. The monotonic argument makes a lead at
        # or above the gate safe even under left-censored history; below it, we
        # cannot distinguish "genuinely late" from "earlier history pruned".
        if canonical_lead is not None and canonical_lead >= gate_minutes:
            return STATUS_VERIFIED_CANONICAL
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
    if extra_history:
        extra_floor = min((v for v in extra_history.values() if v), default=None)
        if extra_floor and (floor is None or extra_floor < floor):
            # Coverage genuinely reaches further back now, so more anchors are
            # decidable. Leaving the floor at the substrate's would keep calling
            # rows indeterminate that the extra history can actually resolve.
            floor = extra_floor
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
                continue
            # Same +5min tolerance the consumers use, evaluated in SQL so the
            # bound is identical to the one that produced the legacy value.
            cur = await conn.execute(
                "SELECT datetime(?, '+5 minutes'), julianday(?)", (anchor, anchor)
            )
            cutoff, anchor_jd = await cur.fetchone()

            candidates = [
                (token_id, first_seen)
                for token_id, first_seen, nfs, _jd in substrate
                if cutoff is not None and nfs < cutoff
            ]
            jd_by_value = {first_seen: jd for _t, first_seen, _n, jd in substrate}

            res = resolve(candidates, coin_id, symbol or "")

            canonical_lead = None
            if res.first_seen_at is not None and anchor_jd is not None:
                seen_jd = jd_by_value.get(res.first_seen_at)
                if seen_jd is not None:
                    canonical_lead = round(_lead_minutes(anchor_jd, seen_jd), 1)

            anchor_covered = bool(floor) and anchor > floor
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

    await conn.commit()
    return counts
