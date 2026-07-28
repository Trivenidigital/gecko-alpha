# Session 3 — Evidence Evaluation and Product-Ruling Pre-Registration (v0.4, final design round)

**New primitives introduced:** NONE (document only — defines analysis and ruling
procedure over Session 2 evidence; no implementation, no schema, no production
change).

**Status:** DRAFT v0.4 for final reviewer sign-off. Authorizes nothing.
Session 3 execution requires: an I4-activated, matured Session 2 cohort; this
document approved through its own gates (§15); and the separate product ruling
at the end. All v0.2 and v0.3 reviewer corrections are incorporated; **zero
unresolved reviewer-decision markers remain, and no new analytical choices
were introduced in v0.4** — every analytical choice traces to the Forward
Cohort Pre-Registration v0.6, reviewer decisions RD-1–RD-9, or the v0.2→v0.3
and v0.3→v0.4 correction rounds.

## Changelog v0.3 → v0.4 (five narrow corrections, nothing else)

```text
Y1  Freeze-boundary preservation: logical cut + frozen identity set are
    recorded atomically at the first eligible Monday; artifact
    materialization failure retries from the SAME logical cut; an
    unreconstructable cut is classified REPRODUCIBILITY_FAILED /
    NON_EVIDENTIARY and permanently disclosed — never silently replaced
    by a later Monday cohort.                                    → §1, §2
Y2  Causally atomic cross-store cut: brief writer barrier while the four
    cut markers are captured; frozen queries enforce all four cut
    boundaries; manifest proves boundary enforcement.            → §2
Y3  Multi-cell selection removed: >1 incompatible version cell ⇒ global
    Axis E = KEEP_GA3_GA6_PARKED; version-specific unpark requires a
    separately frozen single-version cohort or separately authorized
    protocol; evidentiary ADVERSE cell blocks later requests unless an
    owner ruling limits them to a newer, separately evaluated
    version.                                                     → §3, §11, §12
Y4  Axis A precedence defined (NON_EVIDENTIARY ≻ COVERAGE_SENSITIVE ≻
    EVIDENTIARY); CP1 evidence status = determinate count + ESS +
    snapshot integrity (v0.6 defines no numeric CoinGecko coverage
    floor — verified); cg_covered is mandatory reporting, not a gate. → §1, §8, §11
Y5  Observed-concern preservation: independent
    observed_extended_horizon_concern flag; >25% trigger numerator /
    denominator / zero-denominator treatment defined; any observed
    concern keeps Axis E parked.                                 → §14
```

**Governing source:** `tasks/prereg_forward_cohort_v0_6.md` (consolidated
v0.6) is authoritative for cohort identity and first-decision assignment,
primary and secondary contrasts, CP1 and executable CP2, the $100 primary
position and $500 entry-only sensitivity, crossover clocks, missing-data and
coverage rules, deterministic sampling and Hájek weighting, the 24-hour
report / 72-hour supplement split, versioning, measurement gaps, and change
control. **Session 3 redefines NONE of these.** Any conflict resolves to v0.6.

## Changelog v0.2 → v0.3 (reviewer corrections incorporated)

```text
X1   Gate 4 separated from freeze-integrity gates: gates 1,2,3,5,6,7 set
     the mandatory freeze boundary; gate 4 sets CP2 evidence status only;
     CP1 gets its own endpoint-level evidence status.            → §1
X2   Freeze is recorded by the automatic scheduled readiness controller;
     operator authorization happens once at S3-G1; snapshot failure =
     reproducibility failure, never an optional skip.            → §1, §2
X3   Consistent-cut snapshot procedure (high-water marks, online backup,
     WAL status, reconciliation proof, hash-after-cut).          → §2
X4   analysis_version_cell partitioning; no pooled governing
     classification; descriptive pooled appendix only; evidentiary
     ADVERSE cell blocks unpark.                                 → §3 (new), §11, §12
X5   Denominator language reconciled: full-cohort disposition/coverage
     table; raw sampled-in diagnostic rate; Hájek-weighted governing
     estimate; CP1 raw may use the full assignment cohort.       → §4, §6
RC-1 Leave-one-informative-stratum-out stability rule.           → §12
RC-2 Objective EXTENDED_HORIZON triggers.                        → §14
RC-3 Gate-4-only failure: freeze on schedule; CP2 published
     coverage-degraded/NON_EVIDENTIARY; CP2 gate-value and unpark
     claims prohibited.                                          → §1
RC-4 Endpoint-specific unresolved classes (incl. unresolved
     quote_gap); per-endpoint coverage labels; Axis A sensitivity
     rule.                                                       → §8
RC-5 Numeric latency-mismatch rule (n≥30, median outside
     [30s,120s]; strata at n≥20; else NOT_EVALUABLE).            → §9
XD1  Tail cohort_size defined per cohort × metric × version cell;
     NOT_EVALUABLE conditions; token_dead valuation rule.        → §10
XD2  Axis C frozen sign rules; no-positive-edge → INDETERMINATE. → §11
XD3  Axis E timing: 24h report always KEEP_GA3_GA6_PARKED +
     unpark_candidate_pending_72h; only the supplement may set
     REQUEST_SEPARATE_UNPARK_REVIEW.                             → §11, §14
XM   Minor: "every informative stratum" wording; stable subsection
     names replace numeric list references; delivery-state names
     bound to the deployed dispatch_lifecycle_version.           → §5, §8, §12
```

---

## 1. Entry gates, readiness cadence, and the mandatory freeze boundary

**Readiness check: every Monday at 00:00 UTC**, performed by the scheduled
readiness controller (automatic; no operator timing decision).

**Freeze-integrity gates (all must pass to set the freeze boundary):**

```text
G1  cohort validity      I4 activation completed with the v0.6 §11.4
                         (a)(b)(c) proofs attached
G2  reconciliation       admission-ledger ↔ measurement-store mirror joins
                         complete; failed windows excluded AND reported
                         invalid; the exclusion set frozen in the manifest
G3  maturity floor       ≥30 quality_passed AND ≥30 quality_blocked primary
                         assignments with closed, coverage-resolved 24h
                         windows (v0.6 §2.7)
G5  episode closure      no open measurement-failure episode overlapping the
                         analysis window without a closed, journaled
                         resolution
G6  version              all gate/pricing/quote-model/evaluator identifiers
    reconciliation       in the window enumerated and consistent with the
                         I3/I4 gate records
G7  reproducibility      the §2 consistent-cut machinery can produce a
                         verifiable snapshot
```

**Evidence-status test (does NOT defer the freeze):**

```text
G4  CP2 evidentiary floor   ≥30 determinate CP2 outcomes AND ≥80%
                            determinate coverage among quote-scheduled
                            entries, per primary cohort (v0.6 §4.7)
```

**Rules:**

- **The first Monday boundary at which ALL freeze-integrity gates pass is
  the mandatory freeze boundary.** The controller records the freeze
  automatically; an eligible boundary may NOT be skipped (no optional
  stopping). Operator authorization occurred once at S3-G1 and is not
  requested again at freeze time.
- **Gate-4-only failure (RC-3):** freeze on schedule; publish CP2 as
  coverage-degraded / NON_EVIDENTIARY; CP2 gate-value claims and unpark
  claims are prohibited for that report. **Integrity failure (any of
  G1–G3, G5–G7): do not freeze** — the failure is remediated and the next
  Monday boundary applies.
- **CP1 endpoint-level evidence status (Y4):** CP1 is assessed on its own
  determinate count (≥30 per cohort), its ESS rule (§7), and snapshot
  integrity — independent of G4. **v0.6 defines no numeric CoinGecko
  coverage floor (verified against the consolidated document), and Session
  3 does not invent one:** the `cg_covered` fraction is a MANDATORY
  REPORTED metric beside every CP1 rate, not a pass/fail gate;
  CP1-specific unresolved classes flow into the §8 sensitivity bounds
  instead.
- **Snapshot failure (Y1 — the boundary is preserved, not moved):** at the
  first eligible Monday, the LOGICAL freeze markers and the frozen identity
  set are recorded atomically (§2, step 0) BEFORE artifact
  materialization. If artifact materialization then fails, it is retried
  from that SAME logical cut — the cohort identities and observation
  window never change. If the original cut cannot be reconstructed, the
  initial Session 3 freeze is classified **REPRODUCIBILITY_FAILED /
  NON_EVIDENTIARY** and remains permanently disclosed; it is never
  silently replaced by a following Monday's cohort. Any later cohort is a
  separately versioned evaluation requiring its applicable authorization.
  A technical failure must not create a later, potentially more favorable
  dataset.
- The 24-hour report uses the frozen cohort; the 72-hour supplement uses
  the SAME frozen cohort identities with a later outcome-data cutoff
  (§2, "Supplement cut"); later weekly checks are operational monitoring
  only; any new evidentiary evaluation requires a separately versioned
  dataset and ruling; no rolling reclassification.

Earlier partial looks are pipeline-health checks only, labeled as such,
never evidentiary (v0.6 §2.7).

## 2. Dataset freeze: consistent cut and reproducibility manifest

**Freeze event** (recorded automatically by the readiness controller in the
control-plane journal): freeze timestamp (UTC), triggering boundary,
analysis-window boundaries, purpose ("Session 3 initial evidentiary
freeze").

**Consistent-cut procedure** (Y1 + Y2 — one causally atomic logical
instant, established FIRST; artifacts materialized after; hashes taken
over immutable copies only):

```text
0. atomic logical cut (Y1/Y2)         under a BRIEF cross-store writer
                                      barrier (admission writes, measurement
                                      writes, and spool appends quiesced
                                      together), capture IN ONE BARRIER
                                      WINDOW: the control-journal freeze
                                      sequence, the admission high-water
                                      mark, the measurement high-water
                                      mark, and the spool segment id + byte
                                      offset; then record the frozen
                                      identity set derivable at that cut;
                                      then release the barrier. The barrier
                                      is bounded (marker reads only — no
                                      snapshot work inside it) and is the
                                      ONLY step that blocks writers.
1. control-journal freeze sequence    (captured in step 0)
2. admission high-water mark          (captured in step 0)
3. measurement high-water mark        (captured in step 0)
4. spool cut                          (captured in step 0)
5. consistent snapshots               SQLite online-backup (or equivalent
                                      consistent-snapshot API) of
                                      measurement.db and control_plane.db —
                                      never direct hashing of a changing
                                      live file; spool copied to the
                                      recorded segment/offset; retried from
                                      the SAME step-0 cut on failure (Y1)
6. WAL/checkpoint status              recorded for both databases
7. reconciliation proof               every frozen assignment linked to its
                                      gate_evaluation and (where claimed)
                                      delivery evidence within the cut
8. immutable-copy hashes              SHA-256 over the post-cut immutable
                                      artifacts, recorded in the manifest
```

**Cut-boundary enforcement (Y2):** immutable backups may physically contain
records later than the cut; no later record may enter any report query.
Every frozen query carries its cut predicate explicitly:

```text
control rows       ≤ admission high-water mark
measurement rows   ≤ measurement high-water mark
journal records    ≤ freeze sequence
spool bytes        ≤ recorded segment/offset
```

The manifest lists each frozen query WITH its cut predicate, proving
boundary enforcement.

**Manifest contents:** the eight cut records above; row counts per table;
the frozen query set (verbatim, with ordering and null-handling); the
frozen cohort identity list (canonical identities + assignment ids); all
version identifiers present in the data with persisted pre-images; sampling
parameters from the I3 gate record (strata, K_s, salt reference, inclusion
probabilities); the invalid/excluded window list; tool versions.

**Reproducibility rule:** every number in the reports must be recomputable
from the frozen immutable artifacts by the frozen queries. Machine output
only; anything approximate is labeled APPROXIMATE and excluded from
decision inputs.

**Supplement cut:** the 72-hour supplement performs the SAME consistent-cut
procedure at a later outcome-data cutoff, restricted to the original frozen
identity list (no new identities). The 24-hour report's numbers are never
restated silently; the supplement cites the initial manifest and reports
deltas explicitly.

## 3. Analysis version cells

```text
analysis_version_cell = gate_version
                      × pricing_protocol_version
                      × quote_model_version
                      × outcome_evaluator_version
```

**Rules:**

- CP1/CP2 rates, coverage tables, sensitivity bounds, bootstrap intervals,
  ESS, and classification Axes A–D are computed SEPARATELY per version
  cell.
- **No governing classification pools incompatible version cells.** A
  pooled cross-version appendix may be published as DESCRIPTIVE ONLY,
  labeled as such, and feeds no classification.
- **Multi-cell rule (Y3 — no favorable-cell selection):** if the frozen
  cohort contains more than one incompatible analysis_version_cell, Axes
  A–D are reported separately per cell and **global Axis E remains
  KEEP_GA3_GA6_PARKED** — regardless of how favorable any individual cell
  is. A version-specific unpark request then requires either (a) a
  separately frozen, pre-registered single-version cohort, or (b) a
  separately authorized version-specific evaluation protocol.
- **Any version cell that is evidentiary AND classifies ADVERSE (Axis B)
  blocks an unpark request** — including later version-specific requests —
  unless a product-owner ruling explicitly limits the later request to a
  newer, separately evaluated version.
- Axis E remains KEEP_GA3_GA6_PARKED when no version cell independently
  meets the evidence floors.
- If the entire frozen cohort lies in one version cell (the expected
  initial case), the reports state that once and the cell-wise machinery
  reduces to the single-cell analysis.

## 4. Primary CP1/CP2 analysis (Analysis 1, v0.6 definitions verbatim)

- **Contrast:** quality_passed vs quality_blocked primary assignments
  (first-decision ITT assignment; crossovers stay in their assigned arm).
- **CP1:** CoinGecko trending within 24h of `decision_timestamp` (both
  arms, pricing-independent).
- **CP2:** executable +100%-before-−50% within 24h on the $100 position,
  anchored at `quote_time`; outcomes = first-crossed | neither |
  crossing_order_indeterminate; token_dead ⇒ −50% flagged, per v0.6.
- **Three denominator outputs (X5), per endpoint and version cell:**

```text
D1  full-cohort disposition & coverage table — EVERY assignment, including
    sampled-out and unresolved entries (v0.6 §6.3; no silent exclusion)
D2  raw sampled-in outcome rate — unweighted, explicitly labeled
    "conditional on sampling"; DIAGNOSTIC only
D3  Hájek-weighted full-cohort estimate — the GOVERNING population-rate
    estimate wherever sampling occurred
```

  For CP1 — whose outcome observation is not quote-sampled — the raw
  result may use the full assignment cohort, and D2/D3 coincide with it
  when K_s = 1000 everywhere. A sampled-in-only CP2 rate is never called
  a full-cohort rate.
- **Joint reporting (v0.6 §5.4):** CP1 and CP2 are co-primary; both are
  always reported together; no claim may cite one while omitting an
  unfavorable other.
- Secondary endpoints (MFE, drawdown, liquidity survival, gainers-surface,
  time-to-CP2) report per the v0.6 §5.2 list, split across the two reports
  per §14.

## 5. Secondary delivered-system and crossover analysis (Analysis 2)

- **Contrast:** provider_accepted_delivery vs quality_blocked, per v0.6:
  descriptive, non-independent, presented BOTH including and excluding
  `delivered_crossover` rows; no identity counted as two independent
  observations; affected counts disclosed.
- **Clocks:** delivered rows anchor at provider-acceptance time;
  `delivered_crossover` rows use the later actual provider-acceptance
  event (v0.6 §2.4).
- **Delivery truth (XM):** only durable provider acceptance counts as
  delivered. The concrete state names are bound to the DEPLOYED
  `dispatch_lifecycle_version` at freeze time (recorded in the manifest);
  the semantic rule — provider-confirmed acceptance only; every pending /
  attempted / failed / unknown / operator-resolution state is never
  delivered — governs regardless of the deployed enumeration. Delivery-
  classification-uncertainty rows (unknown-after-send and evidence-
  unavailable classes) are enumerated and reported as their own classes.
- **Disposition-cost analysis:** quality_passed_but_not_delivered breakdown
  by disposition (v0.6 §2.6 SUPPORTING).
- **Attribution frame:** Analysis 1 vs Analysis 2 differences attribute
  value/loss to post-gate machinery (v0.6 §1); descriptive only.

## 6. Raw, stratum-level, and Hájek-weighted reporting

Every rate appears in the three §4 denominator outputs, plus per-stratum
results for each pre-registered quant-score band (v0.6 §4.7). Every
denominator table publishes sampled-in and sampled-out
(quote_not_scheduled reason=sampled_out) counts per stratum. If K_s = 1000
everywhere, the forms coincide and the report says so once.

## 7. Uncertainty and effective-sample-size rules

**Bootstrap (weighted estimates and contrasts):** stratified
canonical-identity cluster bootstrap —

```text
replicates = 10,000
interval   = two-sided 95% percentile interval
cluster    = canonical token identity (all linked outcome, coverage,
             crossover and weight records move together)
strata     = cohort × pre-registered quant-score stratum
             (resampling with replacement WITHIN each cell)
weights    = frozen inclusion probabilities preserved per replicate
recompute  = raw rates, Hájek estimates, and cohort contrasts per replicate
scope      = per analysis_version_cell (§3)
```

**Raw unweighted binary rates:** Wilson score intervals (95%).

**Degenerate intervals:** reported as degenerate; the method is never
changed after seeing results.

**No p-values; no "statistically significant" language.**

**Effective sample size:** Kish ESS = (Σwᵢ)² / Σ(wᵢ²), computed separately
for each primary cohort × endpoint × analysis_version_cell, over the
determinate observations supporting that result. **An evidentiary weighted
claim requires, in each primary cohort: raw determinate n ≥ 30 AND Kish
ESS ≥ 30.** If unequal weights push ESS below 30, the weighted result is
labeled non-evidentiary even when the raw count exceeds 30.

## 8. Coverage and missingness sensitivity (overlay, per endpoint)

**The governing observed result remains the v0.6 presentation** (D1–D3 of
§4). Overlay bounds are then computed per co-primary and version cell:

```text
adverse-to-claim bound:
  unresolved quality_passed  → unfavorable
  unresolved quality_blocked → favorable
favorable-to-claim bound:    the reverse
```

**Endpoint-specific unresolved classes (RC-4):**

```text
CP1 unresolved:
  cg_indeterminate
  cg_source_outage
  cg_snapshot_gap where no positive trending appearance was observed

CP2 unresolved:
  quote_unavailable
  quote_failed
  venue_unreachable
  crossing_order_indeterminate
  quote_gap where threshold order cannot be determined
```

**Not unresolved:**

```text
token_dead                    determinate loss (protocol-defined)
neither                       determinate non-success
quote_not_scheduled           sampling; handled by Hájek weighting, never
                              additionally imputed
measurement_gap_observation   outside the evidentiary cohort; never imputed
```

**Analysis 2:** unknown-after-send and delivery-evidence-unavailable
classes are delivery-classification uncertainty — they never enter the
provider-accepted-delivery cohort and are never imputed as delivered.

**Labels, per endpoint:** `coverage_robust` (adverse bound remains
favorable), `coverage_sensitive` (observed favors passed but the adverse
bound reaches zero or reverses), `coverage_opposed` (observed does not
favor passed) — computed separately for CP1 and CP2. **Axis A becomes
COVERAGE_SENSITIVE when either co-primary is sensitive.**

Measurement-gap counts per failure episode are reported beside every
evidentiary denominator (v0.6 §6.5); the v0.6 §6.4 asymmetry disclosures
are reproduced verbatim in both reports.

## 9. Latency-tax and executable-quality analysis (no reanalysis)

Within v0.6 definitions only: latency_tax (delivered rows, median +
distribution by chain/venue); executable-quality diagnostics
(detection_reference_price vs executable entry price; quote-failure and
breaker-window rates per stratum; the $500-vs-$100 entry-only capacity
sensitivity).

**Latency calibration (context only):** empirical decision→provider-
acceptance intervals reported as median; p10, p25, p75, p90; fraction
above/below 60s; absolute and relative calibration error; chain/venue
breakdown for strata with ≥20 valid observations.

**Frozen mismatch rule (RC-5):**

```text
latency_calibration_mismatch = true when:
  valid provider-accepted observations ≥ 30
  AND median latency outside [30 seconds, 120 seconds]

fewer than 30 valid observations:
  latency_calibration_status = NOT_EVALUABLE
```

**Prohibitions:** no CP2 recalculation under realized latency; no clock
substitution; no alteration of the initial gate classification. A mismatch
produces a PROSPECTIVE amendment request for a future gate version only.

## 10. Tail-concentration analysis (continuous economic outcomes only)

Applied to continuous executable economic outcomes (executable net excess
value and continuous return metrics) — never to CP1/CP2 binary rates.

**cohort_size (XD1):** the number of determinate continuous-outcome
observations in the relevant cohort × metric × analysis_version_cell.

```text
k = max(3, ceil(0.05 × cohort_size))

tail_dominated = true if EITHER:
  (1) removing the three largest positive contributors from the cohort
      driving the favorable mean contrast reverses that contrast's sign; OR
  (2) the top k positive contributors account for >50% of aggregate
      positive executable excess value
```

**NOT_EVALUABLE (XD1):** if fewer than three positive contributors exist,
or the continuous outcome cannot be valued reproducibly, Axis D is
NOT_EVALUABLE — never BROAD_BASED by default.

**token_dead valuation (XD1):** only the persisted mechanically obtainable
net liquidation value is used; no invented continuous loss price.

Always reported: raw mean; median; 10% trimmed mean; top-1/3/5/10
contribution shares; leave-top-3-out result. The flag never changes CP1/
CP2 or the 24-hour endpoint result; it blocks an unpark request (§12).

## 11. Decision classification — five independent axes

Computed per analysis_version_cell from the frozen numbers; each
classification quotes the rules it fired.

**Axis A — Evidence quality (Y4 — explicit precedence):**

```text
Axis A = NON_EVIDENTIARY
  if either co-primary fails its applicable raw-n, ESS, integrity, or
  governing source-coverage requirement (CP2: the G4 floors; CP1: the
  §1 determinate-count/ESS/integrity requirements — no invented
  CP1 coverage gate)

else Axis A = COVERAGE_SENSITIVE
  if either co-primary is coverage_sensitive (§8)

else Axis A = EVIDENTIARY
```

**Axis B — Gate result:** `SUPPORTED` (both co-primaries' weighted
contrasts favor quality_passed; raw directions agree; adverse coverage
bounds remain non-negative) | `MIXED` (one co-primary favors passed, or
raw/weighted disagree, or coverage sensitivity overturns one endpoint) |
`NOT_SUPPORTED` (neither favors passed) | `ADVERSE` (CP2 favors blocked in
both raw and weighted — takes precedence over NOT_SUPPORTED).

**Axis C — Delivered-system result (XD2, frozen sign rules):**

```text
PRESERVES_GATE_EDGE   every co-primary endpoint with a positive primary
                      contrast remains positive in the secondary contrast
ERODES_GATE_EDGE      none reverses, but at least one becomes exactly
                      neutral
REVERSES_GATE_EDGE    at least one primary-positive endpoint becomes
                      negative
INDETERMINATE         delivery evidence/ESS insufficient, OR no positive
                      primary gate edge exists to preserve
```

Magnitude changes that remain positive are reported numerically but never
change the axis.

**Axis D — Tail status:** `BROAD_BASED` | `TAIL_DOMINATED` |
`NOT_EVALUABLE` (§10).

**Axis E — Product action (XD3):** at the 24-hour report, Axis E is ALWAYS
`KEEP_GA3_GA6_PARKED`, accompanied by:

```text
unpark_candidate_pending_72h = true | false
```

(true iff every §12 condition testable at 24h passes). **Only the 72-hour
supplement may set `REQUEST_SEPARATE_UNPARK_REVIEW`**, and only when every
§12 condition and the leave-one-stratum-out rule pass and no §14 veto
applies. No analytical result automatically unparks GA-3–GA-6.

Classifications are DESCRIPTIVE; they are not recommendations and trigger
no automatic action.

## 12. GA-3–GA-6 parking criteria

**Informative stratum** — a quant-score stratum containing:

```text
both quality_passed and quality_blocked candidates
≥10 determinate CP2 outcomes per cohort
ESS ≥10 per cohort where weighting applies
```

**Leave-one-informative-stratum-out rule (RC-1), per version cell:**

- at least TWO informative strata;
- CP2 favors quality_passed in EVERY informative stratum;
- after removing each informative stratum in turn, the remaining
  Hájek-weighted CP2 contrast stays positive;
- no corresponding raw leave-one-out contrast becomes adverse;
- with exactly two informative strata, each must independently favor
  quality_passed.

**An unpark request additionally requires ALL:** Axis A = EVIDENTIARY;
Axis B = SUPPORTED (or MIXED with CP2 favoring passed); Axis C ≠
REVERSES_GATE_EDGE; Axis D = BROAD_BASED; no evidentiary ADVERSE version
cell anywhere (§3); no open P0/P1 defect against the measurement substrate
at report time; the 72-hour supplement has not vetoed (§14); and the
request itself is a separate document ruled on separately (§15 S3-G5).

If fewer than two strata are informative, Session 3 may classify the
aggregate gate result but Axis E remains KEEP_GA3_GA6_PARKED.

## 13. Post-hoc analysis labeling and change control

- Any analysis not pre-registered here is labeled **POST-HOC** in every
  table/figure and is excluded from §11 classifications and §12 criteria.
- Amendments after approval follow the v0.6 §9 pattern: recorded
  product-owner ruling, version bump, post-hoc labeling, pre-amendment
  data reportable under original definitions.
- **Recommendation separation:** the evidence reports contain NO
  threshold, cap, ranking, or gate recommendations; recommendations go in
  the separate ruling-request document citing the frozen report.

## 14. The two reports

**24-hour evidentiary report** (at the §1 freeze): §4 primary contrast
with CP1/CP2 and full coverage breakdowns; §5 secondary contrast;
disposition-cost; matured 6h/24h secondary measures ONLY; §6–§10 machinery
as applicable to 24h endpoints; §11 Axis A–D classifications per version
cell; Axis E per XD3 (always parked + pending flag); §12 criteria
evaluation. It must not claim or imply any 72-hour endpoint is complete.
**Once published, the 24-hour CP1/CP2 classification is immutable.**

**72-hour completion supplement** (own consistent cut per §2, same frozen
identities): 72h MFE, max drawdown within 72h, liquidity survival at 72h,
tail-concentration outputs, >24h time-to-outcome; same coverage and
denominator disciplines. It adds exactly one label, chosen by frozen
triggers (RC-2):

```text
extended_horizon_status = CLEAR | CONCERN | NON_EVIDENTIARY
observed_extended_horizon_concern = true | false        (Y5 — independent)

CONCERN triggers (any one):
  1. the 72h weighted executable-return contrast reverses against
     quality_passed AND the raw direction agrees;
  2. the 72h liquidity-survival contrast is adverse in both raw and
     weighted reporting;
  3. the 72h continuous outcomes are TAIL_DOMINATED;
  4. the 24h-winner deterioration fraction exceeds 25% (definition below).

status assignment:
  NON_EVIDENTIARY   when the relevant 72h raw determinate count, ESS,
                    coverage, version reconciliation, or snapshot
                    integrity is insufficient — the GOVERNING label even
                    when a trigger also fires
  CONCERN           evidence sufficient AND any trigger fires
  CLEAR             evidence sufficient AND no trigger fires

observed_extended_horizon_concern = true whenever ANY trigger fires on the
observed data, INCLUDING under NON_EVIDENTIARY status — an observed
warning is never concealed by an insufficiency label. Any observed
concern keeps Axis E = KEEP_GA3_GA6_PARKED.
```

**Trigger-4 definition (Y5):**

```text
numerator    quality_passed assignments with a positive CP2 outcome at
             24h that, at the 72h cut, are token_dead OR lack an
             executable $100 liquidation route
denominator  quality_passed assignments with a positive CP2 outcome at
             24h
zero denominator → trigger_4 = NOT_EVALUABLE (never automatically clear;
             it neither fires CONCERN nor contributes to CLEAR — the
             remaining triggers govern, and the NOT_EVALUABLE state is
             printed)
reporting    numerator and denominator counts are always printed beside
             the percentage
```

The supplement MAY: veto or downgrade an unpark request (including
withdrawing `unpark_candidate_pending_72h`); flag liquidity survival,
delayed drawdown, or tail concentration; require additional observation.
It may NOT: upgrade a MIXED / NOT_SUPPORTED / ADVERSE 24-hour result;
relabel CP1 or CP2; retroactively change the frozen dataset.

## 15. Gates

```text
S3-G1  Document approval: reviewer sign-off + product-owner authorization
       of THIS document (v1.0). Drafting requires neither. Operator
       authorization for the automatic freeze is granted HERE, once.
S3-G2  Synthetic report validation: the report generator (built later,
       under its own authorized implementation task — NOT authorized by
       this document) runs end-to-end on SYNTHETIC validation-namespace
       data only; output checked against hand-computed values; zero
       production reads outside the frozen-snapshot mechanism.
S3-G3  Dataset freeze: §1 boundary reached; §2 consistent cut executed;
       manifest journaled.
S3-G4  Report publication: 24h report, then 72h supplement, each from its
       frozen snapshot; independent reconciliation pass (arithmetic,
       cohort boundaries, coverage math, secret absence) before release.
S3-G5  Product ruling: the separate recommendation/ruling-request document
       is submitted; the product owner rules; the ruling is recorded.
       Session 3 closes with the ruling record.
```

---

## Confirmation

No repository, PR, implementation, deployment, cohort, or Session 3
execution state changed in producing this draft. PR #477 and PR #476 are
untouched; I2/I3/I4, cohort accrual, and Session 3 execution remain held.
