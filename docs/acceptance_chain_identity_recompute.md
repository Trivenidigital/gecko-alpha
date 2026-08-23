# Acceptance report — versioned legacy-provenance recomputation

**Candidate:** `599be775` (`fix/canonical-identity-semantics`, PR #559)
**Method:** full replay of the entire archived population against a trimmed
copy of production, taken in the pre-migration shape so `initialize()`
exercises the real upgrade-with-data path.

The ruling named 1,188 rows; that is the `gainers_comparisons` surface. All
three surfaces are replayed here — **2,891 rows** — and gainers is reported as
its own axis throughout.

---

## 1. Reconciliation

Every archived row lands in exactly one status, and the statuses sum to the
population. This is asserted, not observed: rows with no join key used to be
skipped silently, which made the totals fail to sum with nothing saying why.

Three numbers can each reasonably be called "the population" and they are not
the same. `reconciliation_report` emits all of them rather than leaving the
denominator implicit:

| | rows | what it means |
|---|---:|---|
| `population` | 2,891 | every archived row |
| `skipped_canonical` | 0 | already `canonical_v1`; never prefix-derived, nothing to replay |
| `replayed` | 2,891 | the denominator the status table below uses |
| statuses, summed | **2,891** | |
| `stored` | **2,891** | rows actually in the overlay |

They coincide here only because production has no `canonical_v1` archived rows
and no unjoinable rows. They will diverge on any future run, which is why the
report states which one it used.

| status | rows | earns credit |
|---|---:|:--:|
| `verified_canonical` | 1,543 | **yes** |
| `indeterminate_history` | 968 | no |
| `canonical_below_gate_indeterminate` | 252 | no |
| `no_legacy_credit` | 124 | n/a |
| `verified_prefix_only` | 4 | no |

## 2. The headline

Gainers `tier_high`, scored three ways over the same 1,188 rows:

| scenario | high | watch | low |
|---|---:|---:|---:|
| legacy (prefix credit trusted) | **341** | 415 | 432 |
| naive cutover (all legacy credit dropped) | **187** | 370 | 631 |
| with the overlay | **315** | 349 | 524 |

The naive cutover destroys 154 high-tier rows. The overlay recovers **128 of
them**. The residual 26 are rows whose provenance genuinely cannot be
reconstructed, plus the fabrications below.

`341` and `187` reproduce the ruling's own figures exactly, which is the
control: the harness reproduces both known endpoints before being trusted for
the third.

## 3. Per surface

| surface | credit-bearing | recovered | rate |
|---|---:|---:|---:|
| gainers | 1,144 | 564 | 49.3% |
| losers | 1,080 | 591 | 54.7% |
| trending | 543 | 388 | 71.5% |
| **total** | **2,767** | **1,543** | **55.8%** |

## 4. Row age — the axis that explains everything

| anchor month | recovered | held legacy credit | rate |
|---|---:|---:|---:|
| 2026-04 | 0 | 128 | 0.0% |
| 2026-05 | 0 | 390 | 0.0% |
| 2026-06 | 187 | 550 | 34.0% |
| 2026-07 | 551 | 795 | 69.3% |
| 2026-08 | 805 | 904 | 89.0% |

Recovery tracks history coverage, not row quality. The preserved sources span:

```
2026-06-19 .. 2026-07-03
2026-07-17 .. 2026-08-02
2026-08-09 .. now
```

April and May precede every source, so nothing there is reconstructible. June
starts mid-month. The July and August dips correspond to the two gaps.

**Verified, not assumed:** of the 968 `indeterminate_history` rows, **968 have
anchors outside the coverage intervals and 0 are inside-coverage-but-unmatched.**
The resolver is declining to assert negatives where history does not reach — it
is not silently failing to find matches. Had that split come back the other way
it would have meant the opposite.

## 5. What the prefix-only rows do and do not establish

Only **4 rows** across the entire population classify as `verified_prefix_only`:

| coin | symbol | anchor | fabricated lead |
|---|---|---|---:|
| `openai-prestocks-2` | OPENAI | 2026-07-21 | 17,131 min (11.9 d) |
| `pepe-on-doge` | PODGE | 2026-08-10 | 18,798 min (13.1 d) |
| `blind-boxes` | BLES | 2026-08-11 | 18,739 min (13.0 d) |
| `floki-ceo` | FLOKICEO | 2026-08-11 | 18,739 min (13.0 d) |

All four carry 12–13 day "leads", the signature of a prefix match against an
unrelated token.

**Read this status precisely.** It means: a prefix candidate explains the
legacy credit, and no canonical match for the coin exists in our substrate
within a covered window. It does **not** amount to proof of fabrication,
because the coverage predicate is a *global* span over all tokens' events — it
establishes that we were recording during the window, not that this specific
coin's canonical-id token would have been seen. The direction is safe (it
withholds credit rather than granting it), and the same objection is why the
`alias_unique` tier no longer produces verified positives at all. Making the
coverage predicate per-token is the residual work that would let these four be
called proven.

**This is the finding that changes what can be claimed.** The pre-ruling
framing was that the 45% `tier_high` drop is *mostly a provenance-metadata
artefact*. The replay does not support that, and does not support its opposite
either. What it supports:

- 1,543 rows are confirmed genuine — all at `contract` or `canonical_id` tier,
  where censoring cannot manufacture the win,
- 4 are best explained by a prefix collision, with the caveat above,
- 1,220 cannot be decided in either direction, because the history that would
  decide them predates every surviving source.

The honest statement is that the drop is **dominated by unverifiable history,
not by demonstrated fabrication** — and that no experiment available to us can
close the remaining 1,220. That gap is permanent.

### Tier discipline

Every one of the 1,543 verified rows resolved at `canonical_id` tier. **Zero**
were promoted through `alias_unique` (symbol equality), which is now recorded
as indeterminate unconditionally — a tier-3 win is exactly what censored
history fabricates, and the module's own worked example (`terra-luna-2 <-
luna`) produced a 7,200-minute lead that collapses to 60 once real history is
restored.

## 6. Left-censoring

Per the ruling, an observed canonical lead ≥ the gate qualifies, and a
reconstructed lead *below* the gate is **not** a negative. 252 rows are
`canonical_below_gate_indeterminate`; none has a negative lead, so no candidate
first-seen *after* its anchor slipped through the time bounds.

| reconstructed lead | rows |
|---|---:|
| < 1h | 49 |
| 1–12h | 101 |
| 12–24h | 102 |
| negative | **0** |

## 7. Sensitivity

The replay was run on four successive revisions of the code (`c1a50e33`,
`19dd27da`, `552ef6f6`, `599be775`), across a change of live history source
(`signal_events` → `signal_first_seen`), a rewrite of the coverage probe, and a
change from one transaction to per-surface commits, the removal of
`alias_unique` promotion, and a semantics filter on the tracker overlay.

**Every status count was identical on all four.** The result is insensitive to
those corrections, which is why they could be made late without re-opening the
measurement.

## 8. What this does not establish

- Recovery rates for anchors in coverage gaps. Unknowable, permanently.
- Behaviour once the `/root` snapshots are deleted. Recovery would fall toward
  the substrate-only rate (~18.8%). This is why the watchdog escalates on
  **recovered credit** rather than row count — see
  `docs/runbook_recompute_coverage.md`.
- Any claim about rows written after the archive snapshot was taken. They are
  stamped legacy but have no archived twin and cannot be covered.
