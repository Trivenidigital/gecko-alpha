# Acceptance report — versioned legacy-provenance recomputation

**Candidate:** `552ef6f6` (`fix/canonical-identity-semantics`, PR #559)
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

| | rows |
|---|---:|
| archived population | 2,891 |
| statuses, summed | **2,891** |
| overlay rows written | **2,891** |

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

## 5. What was actually fabricated

Only **4 rows** across the entire population are demonstrated prefix-only
collisions:

| coin | symbol | anchor | fabricated lead |
|---|---|---|---:|
| `openai-prestocks-2` | OPENAI | 2026-07-21 | 17,131 min (11.9 d) |
| `pepe-on-doge` | PODGE | 2026-08-10 | 18,798 min (13.1 d) |
| `blind-boxes` | BLES | 2026-08-11 | 18,739 min (13.0 d) |
| `floki-ceo` | FLOKICEO | 2026-08-11 | 18,739 min (13.0 d) |

All four carry 12–13 day "leads", the signature of a prefix match against an
unrelated token.

**This is the finding that changes what can be claimed.** The pre-ruling
framing was that the 45% `tier_high` drop is *mostly a provenance-metadata
artefact*. The replay does not support that, and does not support its opposite
either. What it supports:

- 1,543 rows are confirmed genuine,
- 4 are confirmed fabricated,
- 1,220 cannot be decided in either direction, because the history that would
  decide them predates every surviving source.

The honest statement is that the drop is **dominated by unverifiable history,
not by demonstrated fabrication** — and that no experiment available to us can
close the remaining 1,220. That gap is permanent.

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

The replay was run on three successive revisions of the code (`c1a50e33`,
`19dd27da`, `552ef6f6`), across a change of live history source
(`signal_events` → `signal_first_seen`), a rewrite of the coverage probe, and a
change from one transaction to per-surface commits.

**Every status count was identical on all three.** The result is insensitive to
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
