# Gecko-Alpha — Forward Cohort Pre-Registration (v0.6)

**New primitives introduced:** experiment-control journal (control_plane.db: cohort_status record, fencing token/status_version, admission ledger, failure episodes) + measurement_gap_observation record + stratified sampling method (gecko-alpha-cohort-sampling-v1) + quote worker (I3) — all design-only at I1; no implementation in this document.

> **Canonical protocol.** This is the governing Session 2 experimental
> protocol (I1 document). `tasks/prereg_detection_gate_enrichment_cohort.md`
> is a subordinate detection-receipt implementation and evidence annex; where
> the two differ, THIS document controls.

**Status: DRAFT v0.6 — consolidation in progress.** This revision applies the
three v0.5 logical corrections plus one clarification (changelog below) and
the reviewer's C1–C6 rulings of 2026-07-27. **Consolidation caveat
(PENDING-V05-SOURCE):** the reviewer requires this file to carry the complete
§§1–12 with no "unchanged from v0.x" placeholders. The v0.6 change text is
fully incorporated below; the sections inherited unchanged from v0.5 (marked
`PENDING-V05-SOURCE`) require the v0.5 source text, which exists only in the
product-owner's records — it is not in this repository, any branch, or any
session archive accessible to the dev session. Supplying that text is the
single external input needed to finish consolidation. No content below is
invented: every populated section comes verbatim-or-restructured from the
v0.6 draft text or an explicit reviewer ruling.

I1 merge blockers (unchanged): Session 1 narrow reconciliation; F2
classification + persistence selection (scope per §7.4, including the
control-plane journal); marker resolution; reviewer sign-off; product-owner
authorization. Additional consolidation blocker: PENDING-V05-SOURCE sections
below.

## Changelog v0.5 → v0.6

```text
V1  Gap-candidate contradiction resolved: measurement_gap_observation
    record; operational vs evidentiary denominator separation; independent
    control-plane journal added to F2 scope.                → §6.5, §7.4, §11.3
V2  Sampling freeze split: I1 freezes the METHOD (strata, algorithm,
    formula, budget rule, selection rule, minimums, estimator); I3 runs a
    bounded non-cohort census through the inert worker and freezes the
    VALUES (K_s, salt, probabilities) before I4.            → §4.7, §12
V3  Fenced admission: authoritative experiment-control record with
    fencing token; commit protocol; three-part I4 proof; objective
    subsystem-level pause triggers with frozen thresholds, distinguished
    from per-candidate coverage outcomes.                   → §11.3–11.4
V4  Clarification: $500 is entry-capacity sensitivity only; CP2 and the
    72h liquidation path are evaluated on the $100 primary position. → §4.2
```

Plus reviewer rulings 2026-07-27 applied in this consolidation: C1
(production fail-open statement, §11.5), C3 (salt/K_s freeze at I3, §4.7),
C4 (sampling digest formulation, §4.7), C5 (fencing token increments on
every transition, §11.3), C6 (measurement_gap_observation canonical record,
§6.5).

---

## 1. Motivation and cohort definition

`PENDING-V05-SOURCE — §1 inherited unchanged from v0.5; full text required
from the product-owner's v0.5 record.`

## 2. Arms, assignment and endpoints (CP1 / CP2 definitions)

`PENDING-V05-SOURCE — §2 inherited unchanged from v0.5; full text required
(exact CP1 and CP2 endpoint definitions, arm definitions, assignment rules).`

## 3. Cohort boundaries and admission preconditions

`PENDING-V05-SOURCE — §3 inherited unchanged from v0.5; full text required.`
(Runtime admission enforcement is governed by §11.3 of this document.)

## 4. Prices, costs, quoting

### 4.1 `PENDING-V05-SOURCE — inherited unchanged from v0.5.`

### 4.2 Notionals — clarified (V4)

```text
$100  primary position. ALL outcome evaluation — CP2, the 72-hour
      liquidation path, MFE/drawdown, token_dead determination — uses the
      $100 position exclusively.
$500  entry-capacity sensitivity measurement ONLY: one buy quote at
      quote_time (Analysis 1) / delivery time (Analysis 2). It has no
      outcome clock, no refresh quotes, and appears in no endpoint.
```

### 4.3–4.6 `PENDING-V05-SOURCE — inherited unchanged from v0.5 (includes the frozen 60s latency fallback; the $1.00 dust threshold and 30/80% thresholds are I1-frozen per §12 and must appear here verbatim from v0.5).`

### 4.7 Quote worker — split freeze (V2, with C3/C4 rulings applied)

Isolation requirements, workload formula structure, Hájek estimator, and
evidentiary thresholds unchanged from v0.5. The freeze is split, because
Session 1 could not durably reconstruct blocked-candidate inflow and
therefore cannot source a definitive census:

**Frozen at I1 (the method):**

```text
strata            fixed quant-score bands: [0,20) [20,40) [40,60) [60,∞)
                  — replaceable before I1 merge only if the reconciliation
                  certifies score-distribution data supporting better cut
                  points; frozen at I1 either way
algorithm         symmetric hash inclusion via the domain-separated
                  sampling digest (reviewer ruling C4):

                  sampling_digest = SHA256(canonical_json({
                    "domain": "gecko-alpha-cohort-sampling-v1",
                    "chain": canonical_chain,
                    "canonical_contract": canonical_contract,
                    "fixed_salt": fixed_salt
                  }))
                  sampling_bucket =
                    unsigned_big_endian_integer(sampling_digest) mod 1000
                  included = sampling_bucket < K_s

                  canonical_json = UTF-8, sorted keys, no insignificant
                  whitespace. Identical strata and K_s across cohorts. The
                  persisted record carries the hash-version identifier,
                  stratum, K_s, and inclusion probability. Raw string
                  concatenation pre-images are rejected.
call formula      §4.7 workload formula (2 entry quotes + $100 refresh
                  ticks × 72h rolling inventory, × retry multiplier)
budget rule       max worker request budget = 50% of the quote provider's
                  documented rate limit on the worker's DEDICATED
                  credential (measured/confirmed at I3); headroom margin:
                  projected workload must fit within 80% of that budget
retry/breaker     assumptions per the worker's frozen timeout/retry
                  constants (stated in the I3 gate record)
K_s selection     for each stratum, the LARGEST K_s (step 1/1000) such
                  that projected total workload fits the budget rule,
                  subject to the minimum below; if unconstrained,
                  K_s = 1000 (no sampling)
minimum           expected determinate outcomes per primary cohort within
                  a 30-day accrual horizon ≥ 30 at the selected K_s; if
                  the budget cannot satisfy this, the conflict is escalated
                  to a product-owner ruling rather than silently resolved
estimator         normalized Hájek (§ v0.5), unchanged
```

**Frozen at I3, before I4 (the values — reviewer ruling C3: I3 controls):**

```text
- bounded, NON-COHORT capacity census through the deployed inert worker:
  observed real inflow rates (passed and blocked) and rolling 72h
  inventory projection, using observation-only counters — no
  gate_evaluation, no primary_assignment, no production-namespace write
  of any kind during the census
- census duration: minimum 72 hours of continuous observation
- K_s selected mechanically by the I1-frozen rule from census numbers
- fixed_salt, strata (as frozen at I1), K_s, and inclusion probabilities
  recorded in the I3 gate record and frozen before I4 activation
- I4 loads the already-frozen I3 values and activates; nothing is frozen
  "at cohort registration"
```

No real cohort assignment may exist before I4 regardless of census state
(§11).

## 5. `PENDING-V05-SOURCE — inherited unchanged from v0.5.`

## 6. Coverage

### 6.1–6.4 `PENDING-V05-SOURCE — inherited unchanged from v0.5, EXCEPT:` the v0.5 `measurement_unavailable` CP2 coverage class is removed — superseded by §6.5. CP2 coverage classes apply only to admitted primary assignments.

### 6.5 Measurement gaps — operational vs evidentiary separation (V1, C6)

During `paused_measurement_failure` (and during `inactive`/`validating` if
live candidates flow), no primary assignment is admitted; instead, where
safely recordable, an append-only record is written:

```text
measurement_gap_observation
    identity, if safely available
    observed_at
    gate_decision, if safely available
    active_gate_version
    failure_episode_id
    control_status_version
    missing_components
```

Reporting rules:

```text
- gap observations appear in OPERATIONAL funnel and measurement-
  availability denominators (uptime, admission rate, gap counts)
- they appear in NO CP1/CP2 cohort rate: without an admitted
  primary_assignment there is no cohort membership and no valid
  outcome clock
- they are NEVER reconstructed into assignments after recovery
- each is linked to the authoritative control-journal failure episode
- every evidentiary report states the count of candidates excluded
  from accrual due to measurement unavailability, per failure episode
```

If the failed component prevents even gap-observation writes, the episode's
control-plane journal entry (§11.3) records that observation itself was
unavailable, and the report says so.

## 7. Delivery evidence

### 7.1–7.3 `PENDING-V05-SOURCE — inherited unchanged from v0.5.`

### 7.4 F2 persistence scope (V1 extension)

F2's selection covers, in addition to the seven v0.5 components
(`PENDING-V05-SOURCE` — the seven-component list itself must be restored
from v0.5):

```text
experiment-control journal (control plane): an independent persistence
seam capable of durably recording cohort_status transitions, fencing-token
changes, and failure episodes EVEN WHEN the primary measurement store is
the failed component. It must not share a write path, connection, or lock
with the measurement store.
```

**F2 selection (resolves `PENDING-F2` for the control-plane seam):** a
separate SQLite database file `control_plane.db` — append-only, hash-chained
event journal + authoritative admission ledger, dedicated connections (never
the pipeline's shared `db._conn`), two writer classes (runtime supervisor
for automatic transitions; operator scripts for authorization-class events).
Full architecture, writer model, and rejected alternatives: annex §12/§12a/
§12b (`tasks/prereg_detection_gate_enrichment_cohort.md`). Remaining
per-component selections for the seven v0.5 components: `PENDING-F2` until
the v0.5 component list is restored.

## 8–10. `PENDING-V05-SOURCE — inherited unchanged from v0.5.`

## 11. Implementation increments and experiment lifecycle

### 11.1–11.2 `PENDING-V05-SOURCE — inherited unchanged from v0.5 (increments; synthetic-only before I4).`

### 11.3 Experiment control: fenced admission and objective triggers (V3, C5)

Authoritative experiment-control record (persisted in the control-plane
journal, §7.4):

```text
cohort_status = inactive | validating | active | paused_measurement_failure
activation_epoch
status_version / fencing_token     (ONE monotonic counter; increments on
                                    EVERY transition — reviewer ruling C5:
                                    inactive→validating, validating→active,
                                    active→paused (INCLUDING automatic
                                    supervisor pauses), paused→validating,
                                    active→inactive/closed. Authorization
                                    governs whether a transition may occur;
                                    it does not govern whether the version
                                    increments.)
transition_timestamp
transition_reason
```

Fenced admission protocol — every production `primary_assignment`:

```text
1. read the control record: require cohort_status = active; capture the
   current fencing_token
2. write the assignment carrying that fencing_token
3. commit fails if status or token changed between read and commit
   (compare-and-commit against status_version); a failed admission is
   re-evaluated against the new control state — if no longer admissible,
   it becomes a measurement_gap_observation, never a retried assignment
   under a stale token
```

Objective pause triggers (subsystem-level; any one transitions
`cohort_status → paused_measurement_failure`):

```text
- control/measurement store write failure (any load-bearing evidence write)
- delivery-spool fsync failure
- snapshot canonicalization failure (serialization error or hash mismatch)
- bounded-queue overflow: > 100 dropped enqueues within any 10-minute window
- quoter circuit breaker open continuously > 10 minutes
- outcome-worker freshness breach: no completed evaluation cycle for
  > 3 × the stated refresh cadence
- health-transition journal unavailable (control plane itself unwritable →
  pause is enforced by the admission protocol failing closed: no readable
  active status = no admission)
```

Explicitly NOT pause triggers (ordinary per-candidate coverage outcomes,
classified and retained): individual `quote_failed`, `quote_unavailable`,
`quote_not_scheduled`, single-candidate timeout/retry exhaustion,
`cg_snapshot_gap`, per-token `venue_unreachable`. These degrade coverage;
they do not transition `cohort_status`.

Recovery unchanged: passing end-to-end synthetic health check before
`active`; `first_complete_event_after_recovery` recorded; fencing token
incremented on the resume transition.

### 11.4 I4 activation — proof obligations (V3)

At I4:

```text
- activation_epoch and initial fencing_token recorded in the control journal
- production admission enabled solely via the control record (no separate
  flags); cohort-start event = first admitted primary_assignment
- proof, attached to the I4 gate record:
    (a) zero production assignments with timestamp < activation_epoch
    (b) every production assignment carries a fencing_token valid for an
        ACTIVE interval per the control journal
    (c) join of assignments × control-journal intervals shows no admission
        during any non-active interval
```

### 11.5 Production fail-open guarantee (reviewer ruling C1)

Failure or unavailability of the experiment control plane, measurement
store, spool, quoter, or evidence machinery prevents cohort admission only.
Production detection, gate evaluation, ranking, dispatch, and alert delivery
continue unchanged and are never delayed or suppressed by measurement
failure.

## 12. Gates

### I1 — document-merge gate (amended per V2)

```text
[ ] Session 1 narrow reconciliation complete (incl. §4.3 supersession
    resolution and any strata-refinement evidence)
[ ] F2 classified; persistence selected for all §7.4 components INCLUDING
    the control-plane journal
[ ] Frozen at I1: 60s latency (or superseded value), $1.00 dust threshold,
    30/80% thresholds, entry-only $500 rule, Hájek estimator, sampling
    METHOD (strata, algorithm, formula, budget rule, selection rule,
    minimum constraint), pause-trigger definitions and thresholds
[ ] All PENDING-S1 / PENDING-F2 / PENDING-V05-SOURCE / VERIFY markers
    resolved inline
[ ] Reviewer sign-off recorded
[ ] Product-owner authorization recorded
```

### I2 — persistence-merge gate (unchanged, plus:)

```text
[ ] Control-plane journal implemented per F2 selection; fenced admission
    protocol implemented; admission provably fails closed when the control
    journal is unreadable
```

### I3 — quote-worker-merge gate (amended per V2)

```text
[ ] Isolation demonstrated (queue bound, quota separation, breaker,
    lock isolation); retry/timeout constants recorded
[ ] Dedicated-credential rate limit confirmed; worker budget instantiated
    per the I1-frozen budget rule
[ ] Bounded non-cohort capacity census executed (≥72h, observation-only,
    zero production-namespace writes); inflow and inventory recorded
[ ] K_s selected mechanically by the I1-frozen rule; fixed_salt, strata,
    K_s, inclusion probabilities recorded and frozen; minimum-sample
    constraint verified or escalated
[ ] Synthetic end-to-end pass in the validation namespace
```

### I4 — cohort-activation gate (amended per V3)

```text
[ ] I2 and I3 deployed and green, synthetic-only
[ ] Full-path synthetic health check passing
[ ] Sampling values frozen (from I3) and loaded
[ ] Control-journal transitions inactive → validating → active recorded
    with fencing tokens
[ ] §11.4 proof obligations (a)(b)(c) attached
[ ] Product-owner activation ruling recorded
```
