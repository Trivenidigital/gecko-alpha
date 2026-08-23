# Operator rulings — retention (A–F) and chain-detection identity

**Why this file exists.** A reviewer of PR #558 could not verify the ledger
probe's closure predicate against ruling E, because no file in the tree
contained "cohort-closure", "Ruling E" or "DEFERRED_BY_ECONOMICS" — the ruling
lived only in operator messages. So whether `(surface, kind)` is the cohort key,
and whether `partial` counts as closed, rested entirely on the PR's own
assertion about a ruling nobody could read.

That is a real gap: a review cannot check an implementation against a
requirement it has no access to. The governing text is recorded here so it can
be cited, contradicted, and version-controlled like anything else.

Adjudication history for A–F lives in the measured packet referenced by
`docs/db-growth-retention-study`; this file records the binding text only.

---

## Ruling E — `signal_outcome_ledger`

> **GO for cohort-closure pruning; NO age-based pruning.** Mechanically derived
> closure + durable closure receipts. Delete only from CLOSED, non-protected
> cohorts. First prod prune authorized WITHOUT another approval if the
> classifier proves every candidate is closed+unprotected. Does NOT extend to
> `alert_events` / `alert_payloads`.

Receipt contents required before any deletion: cohort/generation identity,
closure reason + time, row count, evidence digest/summary. Then prove all
active/frozen-cohort outputs byte-identical before/after. Historical
incident/evidence cohorts are explicitly protectable.

### Status: `E = DEFERRED_BY_ECONOMICS` (2026-08-23)

> Do not implement E now. ~140 KB reclaim from a ~232 MB table is economically
> irrelevant to the current disk problem. Keep only lightweight observability:
> current table bytes; growth/day; closed-and-safely-prunable bytes; projected
> time to 1 GB. Reopen automatically when either safely reclaimable bytes reach
> roughly 100 MB, or projected table size exceeds 1 GB within 30 days. No
> operator approval is needed merely to reopen the engineering investigation at
> that threshold; any destructive pruning still follows the existing
> cohort-closure ruling.

**Implementation choices this leaves open, and how they were resolved** — these
are the PR's reading, not the ruling's words, and are the right things for a
reviewer to attack:

- **Cohort key = `(surface, kind)`.** The table has no cohort or generation
  column, so a key had to be chosen. `surface` × `kind` is the only pair that
  partitions emissions by their originating lane.

  **This is adequate for OBSERVABILITY and is explicitly NOT the key a pruner
  may delete on.** The ruling's vocabulary is richer than lane — it speaks of
  *cohort/generation identity*, *active/frozen-cohort outputs*, and
  *historical incident/evidence cohorts are explicitly protectable*. Generation,
  frozen and protected are lifecycle properties that `(surface, kind)` cannot
  express. For a probe that fails safe (ignoring protection OVERSTATES what is
  reclaimable, so the alarm fires early); for a pruner it is insufficient,
  because the ruling's receipts require cohort/generation identity and
  protection status that this key cannot carry.
- **`unlabelable` counts as CLOSED.** The labeler never re-selects it, so it is
  terminal in the same mechanical sense `complete` is. Named here because the
  two obvious states get discussed and this third one is easy to leave implicit.
- **`partial` counts as OPEN, not closed.** The labeler's own queue is
  `WHERE label_status IN ('pending','partial')`, so a partial row is work the
  system has not finished. Treating it as closed would let a prune race the
  labeler whenever the labeler is disabled or backlogged.
- **Closure requires dormancy past the label deadline AND zero open rows** —
  never age alone, which the ruling forbids.

---

## Ruling C — chain-detection identity (2026-08-23)

> **Detection truth requires stable asset identity. Prefix similarity is not
> identity.**
>
> Identity precedence: (1) exact chain + contract/address where available;
> (2) exact canonical CoinGecko/token identifier; (3) explicit
> provenance-backed alias → canonical asset mapping; (4) otherwise
> `identity_unresolved`.
>
> Prefix/symbol fuzzy matching may remain diagnostic only. It must not
> determine: `detected_by_chains`; `chains_detected_at`;
> `chains_lead_minutes`; early-detection win claims; persisted performance
> evidence.
>
> Do not overwrite historical lead-time evidence in place. Preserve old results
> as `legacy_prefix_semantics` or equivalent, and compute the new canonical
> metric separately/versioned. Historical recomputation is allowed only as a
> distinct derived dataset with explicit semantic version — not an UPDATE that
> destroys the previous evidence.

**Known deviation:** tier 3 is currently bare symbol equality, admitted only
when unique, because no provenance-backed alias table exists. Justified by
measurement — symbol equality independently decided zero winners in the impact
study — and should be re-sourced from a real alias table if one is built.

**Known over-breadth, unresolved:** the conviction gate refuses chains credit
from any row not stamped `canonical_v1`. Every pre-cutover row is
`legacy_prefix`, so 826 of 1188 prod gainers rows lose credit on deploy, though
~96% of them were decided by exact canonical identity and would still qualify.
The ruling forbids *prefix* determining win claims, not *legacy-era* rows. The
escape hatch is the recomputation clause above; it has not been taken.

---

## Rulings A, B, D, F — summary

| Option | Ruling |
|---|---|
| A `score_history` 21→14 | **FALSIFIED — HOLD at 21d.** 762/3347 tokens (22.8%) change their last-3 set; the binding reader has no date window. |
| B `volume_snapshots` 21→14 | **GO, 14d hard floor.** Executed 2026-08-23: span 21.04d → 14.002d, vol7d-eligible contracts 1668 → 1668. |
| D `signal_events` indexes | **GO.** No VACUUM bundled. |
| F `signal_events` 14d | **NO-GO on shortening; GO on first-seen substrate.** Substrate built, consumers migrated, parity proven (0 mismatches). Retention itself unchanged. |
