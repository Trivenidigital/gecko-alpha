# Acceptance report — versioned legacy-provenance recomputation

**Candidate:** `89b070ef` (`fix/canonical-identity-semantics`, PR #559)
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
| `canonical_below_gate_indeterminate` | 249 | no |
| `alias_tier_not_verifiable` | 3 | no |
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

The collapse alarm's high-water ratchet, recorded on its first observation of
this replay, reproduces these independently:

```
gainers   rate 0.4930   best_rate 0.4930
losers    rate 0.5472   best_rate 0.5472
trending  rate 0.7145   best_rate 0.7145
```

Those are the same numbers to four decimal places, arrived at by a different
code path — the probe's per-surface correlation rather than the replay's status
tally. A disagreement between them would have meant the alarm was measuring
something other than what this table reports.

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

The replay was run on seven successive revisions of the code (`c1a50e33`,
`19dd27da`, `552ef6f6`, `599be775`, `47847f47`, `c8b5e009`, `4693c563`,
`f2847774`, `89b070ef`), across a change of live history source
(`signal_events` → `signal_first_seen`), a rewrite of the coverage probe, and a
change from one transaction to per-surface commits, the removal of
`alias_unique` promotion, a semantics filter on the tracker overlay, and a
gate re-check in the coverage probe.

**Every status count was identical on all nine**, with one deliberate
exception: the last revision split `alias_tier_not_verifiable` out of
`canonical_below_gate_indeterminate` (252 → 249 + 3), because that label
asserted a gate comparison never performed for the alias tier. The total,
`verified_canonical`, and the tier_high headline are unchanged. The result is
otherwise insensitive to these corrections, which is why they could be made
late without re-opening the measurement.

## 8. What this does not establish

- Recovery rates for anchors in coverage gaps. Unknowable, permanently.
- Behaviour once the `/root` snapshots are deleted. Recovery would fall toward
  the substrate-only rate (~18.8%). This is why the watchdog escalates on
  **recovered credit** rather than row count — see
  `docs/runbook_recompute_coverage.md`.
- Any claim about rows written after the archive snapshot was taken. They are
  stamped legacy but have no archived twin and cannot be covered. The
  documented remediation (re-run the backfill) cannot fix that state.
- Whether every token present in a source's window is actually recorded there.
  Coverage intervals are built from each source's `MIN..MAX` event span, and a
  span is not coverage — retention leaves a source sparse inside its own span.
  What *was* ruled out is the specific fabrication mode where one stale
  surviving row stretches a span backwards: all four sources span 14.0 days,
  exactly the prune window, and the backfill now prints these every run and
  warns above 21 days.

## 9. Known residuals

| residual | why it is not closed here |
|---|---:|
| Coverage is a global span, not per-token | Per-token coverage is a design change; the current predicate only ever produces conservative outcomes now that `alias_unique` cannot promote. |
| `coverage_intervals=None` falls back to a global substrate floor | Latent: there is no runtime caller of `recompute_legacy_provenance` outside the ops script, which always passes explicit intervals. Removing the default would be a silent behaviour change for a caller that does not exist yet. |
| Recovery falls to the substrate-only rate once the `/root` snapshots are deleted | Unavoidable. This is why the watchdog escalates on recovered credit rather than row count. |
| ~~The alarm cannot fire on gradual degradation~~ | **CLOSED.** A per-surface high-water rate ratchet now escalates on a fall below half the recorded rate, in both the probe and the watchdog. Recorded on first observation, raised on improvement, never lowered; reset by deleting the surface's row. See `docs/runbook_recompute_coverage.md`. |
| Probe and readers now share one correlation *and* one gate predicate | More correct than two independent proxies, but a wrong shared predicate makes them wrong **together and in agreement**, which reads as healthy. The remaining failure mode is deliberate. |
| A mark recorded at a population in [20, 40) can still be re-armed downward by growth | Residue of narrowing the guard, not a regression. A mark from 25 rows growing to 60 re-arms at the diluted rate, so a collapse that would page against the original is silent against the re-armed one. The guard's rationale — a handful of rows versus a thousand — does not cover 25 → 60, since 25 is above the judging floor and that mark was legitimately earned. |
| The ratchet has no downward re-calibration | The surviving population is not a random sample: rows that never re-appear skew toward old anchors that resolve indeterminate, so the rate over a shrinking remainder can fall benignly. One question with the row above — how a mark stays comparable as the population moves in either direction. `population` is recorded, so the hook exists; composition cannot be measured before a post-deploy population exists. Neither is live. |
