# Gecko-Alpha — Forward Cohort Pre-Registration (v0.6, consolidated)

**New primitives introduced:** gate_evaluation + primary_assignment records; delivery-evidence spool; experiment-control journal (control_plane.db: cohort_status record, fencing token/status_version, admission ledger, failure episodes); measurement_gap_observation record; stratified sampling method (gecko-alpha-cohort-sampling-v1); isolated quote/outcome worker — all design-only at I1; no implementation in this document.

> **Canonical protocol.** This is the governing Session 2 experimental
> protocol (I1 document). `tasks/prereg_detection_gate_enrichment_cohort.md`
> is a subordinate detection-receipt implementation and evidence annex; where
> the two differ, THIS document controls.

**Status: DRAFT v0.6, fully consolidated** — complete §§1–12, self-contained;
no reliance on predecessor drafts or chat history. Consolidated from the
product-owner-supplied source chain v0.2 → v0.3 → v0.4 → v0.5 → v0.6 (later
draft controls earlier draft) with the issued S1 and C1–C6 reviewer rulings
of 2026-07-27 applied last (rulings control all drafts). Section-by-section
provenance: §13.

**I1 merge blockers:** Session 1 narrow reconciliation; F2 classification +
persistence selection (scope per §7.4, including the control-plane journal);
resolution of all `PENDING-S1:` / `PENDING-F2:` / `VERIFY:` markers (these
are the protocol's own gate-scoped markers, enumerated in §13.2 — each
resolves at its designated gate, per the §12 checklists); reviewer sign-off;
product-owner authorization.

## Changelog (cumulative)

```text
v0.2→v0.3  S1 data-model recoherence (immutable gate_decision, append-only
           events, derived reporting states); S2 secondary-crossover
           treatment; S3 quote-scheduler isolation + capacity census +
           sampling + 80% coverage floor; S4 two-phase delivery evidence;
           S5 version identifiers, decision→acceptance latency definition,
           on-chain token_dead path, direct gate_version format.
v0.3→v0.4  T1 gate_evaluation/primary_assignment split + canonical identity;
           T2 independent durable delivery spool w/ fail-open semantics;
           T3 evidentiary CP2 = ≥30 determinate AND ≥80% coverage; IPW
           aggregates; T4 latency ESTIMATOR pre-registered; T5 full-SHA
           gate_version, measurable token_dead, 72h refresh workload in
           census, four staged increments.
v0.4→v0.5  U1 four independent gates I1–I4; U2 synthetic-only before I4;
           U3 production-fails-open / experiment-fails-closed +
           cohort_status machine; U4 frozen values (60s latency, $1.00
           dust, Hájek estimator, exact workload formula).
v0.5→v0.6  V1 measurement_gap_observation + operational/evidentiary
           denominator separation + control-plane journal in F2 scope;
           V2 sampling freeze split (I1 method / I3 values); V3 fenced
           admission w/ fencing token + objective pause triggers + 3-part
           I4 proof; V4 $500 = entry-capacity sensitivity only.
Rulings    S1 topology (this canonical file + subordinate annex); C1
2026-07-27 production fail-open statement (§11.5); C2 stale-reference
           fixes (annex); C3 salt/K_s/probabilities freeze at I3; C4
           sampling digest = SHA256(canonical_json({domain, chain,
           canonical_contract, fixed_salt})), big-endian mod 1000; C5
           fencing token increments on EVERY transition; C6 gap-observation
           record carries control_status_version + failure-episode link.
```

---

## 1. Purpose and claims

The detection-lane quality gate leaves no durable record of gate failures,
so its value cannot be evaluated. This cohort restores identifiability.

**Primary claim (gate quality):** candidates the quality gate passes
outperform candidates it blocks on the co-primary endpoints, under identical
pricing clocks.

**Secondary claim (delivered-system value):** alerts that achieve
provider-accepted delivery outperform quality-blocked candidates — this
evaluates the whole alerting system (gate + ranking + cap + dedup +
dispatch), and differences between the primary and secondary results
attribute value/loss to the post-gate machinery.

Engineering-evidence design, not a confirmatory trial: endpoints are
pre-registered to prevent quiet redefinition; all results reported
descriptively with counts, distributions, and coverage.

## 2. Data model, identity, cohorts

### 2.1 Canonical token identity

```text
identity = (chain, canonical_contract)
chain               fixed enum (initially: SOLANA; extended only by amendment)
canonical_contract  Solana: canonical base58 mint address, preserved exactly
                    (case-sensitive; no re-encoding); other chains: checksummed
                    canonical form defined at chain-enum extension time
symbol              metadata only — never part of identity
pools               one contract across multiple pools is ONE identity; the
                    selected quote venue is recorded on each quote event,
                    not in identity
```

### 2.2 gate_evaluation (append-only)

Every quality-gate evaluation — first or subsequent — writes a complete,
immutable `gate_evaluation` row:

```text
gate_evaluation_id
identity (chain, canonical_contract)
decision = quality_passed | quality_blocked
decision_timestamp
gate_version
failed_gate_reason (blocked only, per-rule)
quant_score
signals_fired
detection_reference_price
feature_snapshot (full payload) + feature_snapshot_hash
pricing_protocol_version / quote_model_version / outcome_evaluator_version
```

Reevaluations are not demoted to generic events: their features, scores, and
decisions are evidence and are stored in full.

### 2.3 primary_assignment (exactly one per identity per gate_version)

```text
primary_assignment
identity
gate_version
assigned_cohort = quality_passed | quality_blocked
bound_gate_evaluation_id     # the FIRST terminal evaluation for this
                             # (identity, gate_version)
assignment_timestamp
```

Immutable once written. All primary-contrast membership, clocks, and quoting
derive from the bound evaluation. Later evaluations never alter an
assignment; a later opposite decision sets a `crossover` flag on the
assignment (analyzed per §2.5). Assignment admission at runtime is governed
by the fenced admission protocol of §11.3.

### 2.4 Downstream events and derived reporting states

Ranking, dedup, cap, dispatch, delivery-evidence, quote, and outcome events
each link to the specific `gate_evaluation_id` that produced them (not
merely to the identity):

```text
disposition events:   ranked | deduplicated | cap_blocked | dispatch_attempted |
                      dispatch_failed | reevaluation | crossover_marker
delivery evidence:    separate linked records per §7 (intent, provider response,
                      reconciliation results)
quote events:         scheduled | quoted | quote_failed | quote_not_scheduled (§4)
```

No stored field ever encodes "not delivered" as a terminal state, because
delivery evidence arrives after the decision and may arrive late
(reconciliation). Derived reporting states are computed at report time per
assignment from its bound evaluation's event stream — never stored as
stages — with the computation timestamp disclosed:

```text
provider_accepted_delivery        assigned_cohort=quality_passed AND a linked
                                  provider-acceptance evidence record exists
quality_passed_but_not_delivered  assigned_cohort=quality_passed AND no
                                  provider-acceptance evidence at report time
                                  (with disposition breakdown)
delivery_unknown_after_send       per §7
delivery_evidence_unavailable     per §7.3
```

### 2.5 Assignment rule and crossover treatment

Primary assignment is ITT-style: the **first terminal quality-gate
decision** for each identity under a gate_version determines its
primary-cohort membership, permanently. A token first blocked and later
passed/delivered **remains in the blocked primary cohort**; the later event
is recorded as a `crossover_marker` and analyzed separately. No cohort entry
is ever excluded from the primary contrast based on future behavior.
Re-evaluations under the same gate version after a terminal decision do not
create new primary rows; a gate_version change starts a new sub-cohort (§8).

**Secondary-analysis crossover treatment:** in the delivered-vs-blocked
contrast, crossover deliveries (identities whose first decision was blocked
but which were later delivered) are reported separately, and the contrast is
presented **both including and excluding** crossover deliveries. The
secondary contrast is descriptive and non-independent: the same identity is
never counted as two independent observations — where an identity appears on
both sides, this is disclosed and the affected counts shown. No
independence-based inference is drawn from the secondary analysis.

### 2.6 Contrasts

```text
PRIMARY    quality_passed        vs quality_blocked      (gate quality)
SECONDARY  provider_accepted_delivery vs quality_blocked (delivered-system value)
SUPPORTING quality_passed_but_not_delivered breakdown by disposition
           (what the cap/dedup/dispatch layer costs or saves)
```

### 2.7 Size and maturity

First evidentiary evaluation at **≥30 quality_passed and ≥30
quality_blocked primary assignments** with closed, coverage-resolved 24h
windows, plus the §4.7 evidentiary CP2 requirements. Earlier runs are
pipeline-health checks only, labeled as such.

## 3. Feature snapshots, canonicalization, and version identifiers

### 3.1 Payload

The **full snapshot payload is persisted**, not only its hash: every raw
feature value, derived score, signal state, and data-source availability
flag the gate evaluation saw. Append-only; reprocessing writes new rows,
never mutates.

### 3.2 Canonical serialization and hash

```text
schema_version        integer, bumped on any field addition/removal
encoding              UTF-8 canonical JSON
key order             lexicographically sorted at every object level
numbers               decimals serialized as strings with explicit scale;
                      floats forbidden in the canonical form (VERIFY: map
                      deployed float features to fixed-scale decimal strings)
timestamps            ISO-8601 UTC, millisecond precision, trailing 'Z'
null                  explicit JSON null; absent ≠ null (absence is a schema
                      violation for schema_version's field set)
arrays                order-significant; producers must emit deterministic order
                      (signals sorted by signal name)
hash                  SHA-256 over the canonical byte stream
```

### 3.3 gate_version and gate_code_hash

```text
gate_version = full SHA-256 (64 hex chars, untruncated) over the canonical
JSON pre-image {deployed_sha, gate_code_hash, runtime_gate_config,
schema_version}; pre-image persisted alongside every use.

gate_code_hash = SHA-256 over the canonical serialization (§3.2) of the manifest:
[
  {"path": "<normalized POSIX path, repo-root-relative>", "sha256": "<full file hash>"},
  ...
]
sorted lexicographically by path. PENDING-S1: the manifest file list
(quality-gate module set) from the drift-table reconciliation.
```

## 4. Prices, costs, quoting

### 4.1 detection_reference_price

Pipeline-observed price at decision time, from the gate's own source.
Recorded for all evaluations. Secondary/diagnostic only. **Never substitutes
into any executable endpoint** — if an executable quote cannot be obtained,
the entry is `quote_unavailable` (§6); a separate chart-price appendix may
be reported but carries no primary evidentiary weight.

### 4.2 Cost convention and notionals

```text
primary cash outlay  = exactly $100 all-in
capacity outlay      = exactly $500 all-in
all_in_entry_cost    = the fixed outlay ($100 or $500), by definition
acquired_quantity    = token quantity obtainable for that outlay AFTER
                       buy slippage, venue fees, and chain/priority fees
```

Costs reduce `acquired_quantity`; nothing is added on top of the fixed
outlay. The term `base_order_notional` is not used anywhere in this
protocol.

**Notionals — clarified (V4):**

```text
$100  primary position. ALL outcome evaluation — CP2, the 72-hour
      liquidation path, MFE/drawdown, token_dead determination — uses the
      $100 position exclusively.
$500  entry-capacity sensitivity measurement ONLY: one buy quote at
      quote_time (Analysis 1) / delivery time (Analysis 2). It has no
      outcome clock, no refresh quotes, and appears in no endpoint.
```

### 4.3 Analysis 1 clock — fixed latency, RESOLVED and FROZEN

Both cohorts receive an executable buy quote at:

```text
quote_time = decision_timestamp + fixed_pre_registered_latency
```

Session 1 found provider message IDs were not persisted; the pre-registered
estimator's eligibility rule (quality-gate `decision_timestamp` →
provider-acceptance intervals with confirmed provider evidence; median per
chain stratum; minimum n=10 per stratum; round up to whole seconds)
therefore yields no provable historical intervals. Accordingly:

```text
fixed_pre_registered_latency = 60 seconds (fallback INVOKED and FROZEN)
rationale: recorded operator estimate of the decision→dispatch→provider-
           acceptance path; chosen before any cohort accrual; Session 1
           evidence insufficient for the pre-registered estimator
scope:     all chains/venues until a gate_version change; bound per §8
```

Sole supersession path: if, **before I1 merge**, the Session 1 narrow
reconciliation produces ≥10 provably timestamped decision→acceptance
intervals per stratum (provider acceptance evidenced by
correlation-ID-matched structured logs whose acceptance timestamps the
reconciliation certifies as provider-side, not enqueue-side), the
pre-registered estimator computes and its result replaces 60s in the merged
document. After I1 merge, the frozen value changes only via §9 amendment.
Nothing about this remains pending at I4. Implementation requirement: the
quote scheduler must fire a live executable quote at `quote_time` for
**every** scheduled assignment, both cohorts — quotes cannot be
reconstructed retroactively.

### 4.4 Analysis 2 — delivered-system actionability (secondary)

`provider_accepted_delivery` candidates additionally receive an executable
quote at the actual provider-acceptance timestamp:

```text
delivery_executable_entry_price   (quote at provider acceptance time, $100/$500)
delivery_to_quote_latency         (recorded)
```

### 4.5 latency_tax (secondary, delivered candidates only)

```text
latency_tax = delivery_executable_entry_price / detection_reference_price − 1
```

Median and distribution, by chain and venue.

### 4.6 Executable liquidation value

At each evaluation tick: net proceeds of selling `acquired_quantity` —
executable sell quote minus sell slippage, venue fees, chain fees. Candle
data never determines an endpoint.

### 4.7 Quote worker: isolation, capacity, sampling, evidentiary thresholds

**Operational isolation — the quote worker is instrumentation and must be
incapable of harming production:**

```text
- asynchronous bounded queue between the gate path and the quoter; enqueue is
  non-blocking; queue overflow drops to a durable quote_not_scheduled record
  (reason=queue_full) rather than back-pressuring detection or dispatch
- independent HTTP client, timeout budget, and rate-limit budget from any
  production quote/price consumer; never shares provider quota with dispatch
- circuit breaker: on repeated quoter failures, open the breaker, write
  quote_failed (reason=breaker_open) durably, never retry into the gate path
- writes go through the measurement store but must not hold locks the
  detection/dispatch path contends on (VERIFY at I2/I3: table/connection
  isolation per the F2-selected architecture)
- a quoter outage can degrade coverage but can never delay a detection
  decision, an alert dispatch, or a delivery-evidence write
```

**Capacity census — exact workload formula:**

```text
per new assignment (both cohorts, scheduled):
  entry:    1 × $100 buy quote + 1 × $500 buy quote        (at quote_time)
  refresh:  1 × $100-quantity liquidation quote per tick
            for 72h  (= ticks_per_hour × 72)
$500 analysis is ENTRY-ONLY by pre-registration: no $500 refresh quotes.
retry multiplier: expected retries per quote from the worker's frozen
  timeout/retry constants (stated in the I3 gate record), plus breaker-open
  windows modeled as coverage loss, not extra calls
workload/hour = new_scheduled_inflow_per_hour × 2 entry quotes
              + rolling_active_inventory × ticks_per_hour × 1
              , all × retry multiplier
rolling_active_inventory = scheduled assignments whose 72h window is open
  (steady state ≈ scheduled inflow/hour × 72)
```

**Split freeze (V2; C3 — I3 controls the values).** Session 1 could not
durably reconstruct blocked-candidate inflow and therefore cannot source a
definitive census. The freeze is split:

**Frozen at I1 (the method):**

```text
strata            fixed quant-score bands: [0,20) [20,40) [40,60) [60,∞)
                  — replaceable before I1 merge only if the reconciliation
                  certifies score-distribution data supporting better cut
                  points; frozen at I1 either way
algorithm         symmetric hash inclusion via the domain-separated
                  sampling digest (ruling C4):

                  sampling_digest = SHA256(canonical_json({
                    "domain": "gecko-alpha-cohort-sampling-v1",
                    "chain": canonical_chain,
                    "canonical_contract": canonical_contract,
                    "fixed_salt": fixed_salt
                  }))
                  sampling_bucket =
                    unsigned_big_endian_integer(sampling_digest) mod 1000
                  included = sampling_bucket < K_s

                  canonical_json per §3.2 (UTF-8, sorted keys, no
                  insignificant whitespace). Identical strata definitions
                  and identical K_s applied to quality_passed and
                  quality_blocked within each stratum. Raw string
                  concatenation pre-images are rejected. The persisted
                  record carries the hash-version identifier, stratum, K_s,
                  and inclusion probability p_s = K_s / 1000.
call formula      the workload formula above (2 entry quotes + $100 refresh
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
estimator         normalized Hájek (below), unchanged
```

**Frozen at I3, before I4 (the values):**

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
- I4 loads the already-frozen I3 values and activates; no sampling value
  is frozen earlier than I3
```

No real cohort assignment may exist before I4 regardless of census state
(§11).

**Sampling estimator — named and defined:** full-cohort aggregate rates use
the **normalized Hájek estimator**:

```text
weighted_rate = Σ(wᵢ · outcomeᵢ) / Σ(wᵢ),   wᵢ = 1 / pᵢ
pᵢ = inclusion probability of assignment i's stratum (persisted per row)
```

Reported always alongside: unweighted sampled-only rates and raw per-stratum
results. Sampled-in/sampled-out counts per stratum in every denominator
table. Sampled-out entries → `quote_not_scheduled` (reason=sampled_out),
retained in denominators.

**Evidentiary CP2 requires BOTH, in each primary cohort:**

```text
≥ 30 determinate CP2 outcomes (covered or token_dead; not quote_unavailable,
     quote_failed, quote_not_scheduled, or any indeterminate class)
AND
≥ 80% determinate coverage among quote-scheduled entries
```

Failing either → CP2 section published, labeled **coverage-degraded,
non-evidentiary**; no gate-value conclusion may cite it.

## 5. Endpoints

Clock anchors: Analysis 1 endpoints anchor at `quote_time` (§4.3); Analysis
2 endpoints anchor at provider-acceptance time. CP1 anchors at
`decision_timestamp` for both cohorts (trending is pricing-independent).

### 5.1 Co-primary endpoints (evaluated jointly, both always reported)

```text
CP1  CoinGecko trending within 24h of decision_timestamp
CP2  +100% before −50% within 24h, executable definition:
       +100%: net executable liquidation value ≥ 2.0 × all_in_entry_cost
       −50%:  net executable liquidation value ≤ 0.5 × all_in_entry_cost
     outcome = first threshold crossed | neither | crossing_order_indeterminate (§5.3)
```

The −50% arm intentionally measures tradeability; chart-flat tokens with
poor round-trip liquidity may correctly register the loss outcome. Reports
must not "correct" this.

CP2 evaluation cadence: `VERIFY:` deployed price-refresh cadence; the merged
version states it explicitly ("evaluated each refresh tick, ≤N minutes
apart") as a known granularity bound.

### 5.2 Secondary endpoints

```text
MFE (executable) at 6h / 24h / 72h
maximum drawdown (executable) within 72h
liquidity survival at 24h / 72h (quotes at ≥ primary outlay)
gainers-surface appearance within 24h
time to CP2 outcome
latency_tax (delivered only)
coverage status (§6)
crossover_outcome analysis (§2.5)
disposition-cost analysis (what cap/dedup suppressed, §2.6 SUPPORTING)
```

### 5.3 crossing_order_indeterminate

If a quote gap spans observations such that **both** CP2 thresholds could
have been crossed within the gap (interval arithmetic on the bracketing
observed liquidation values admits both), the outcome is
`crossing_order_indeterminate` — never resolved by whichever threshold is
observed first after the gap. Reported as its own outcome class, in the
denominator.

### 5.4 Multiplicity

Two co-primaries, interpreted jointly and descriptively. No claim may cite
one co-primary while omitting an unfavorable result on the other.

## 6. Coverage — classified, never excluded

### 6.1 CP2 (quote) coverage taxonomy

```text
covered                       full quote availability through the window
quote_gap                     intermittent; endpoint assigned only if crossings
                              are unambiguous, else crossing_order_indeterminate
quote_unavailable             no executable entry quote at quote_time → CP2 not
                              evaluable; retained and reported; NO mid-price fallback
quote_not_scheduled           sampled out under the §4.7 sampling rule
                              (reason=sampled_out) or queue overflow
                              (reason=queue_full)
token_dead                    per the evidence rules below → CP2 = −50%, flagged
venue_unreachable             our-side infrastructure failure → indeterminate,
                              counted against coverage quality
```

**token_dead — precise criterion.** Definitive on-chain death requires
either:

```text
(a) the pool/pair account is closed, burned, or non-existent on chain,
    read directly from chain state (not via the quote provider); or
(b) no valid executable route exists for the sell, or the maximum
    mechanically obtainable net liquidation value for acquired_quantity —
    computed from independently read on-chain reserves and pool fee
    mechanics — is below the frozen dust threshold:

        dead_dust_threshold = $1.00 net, after all fees

    with reserve values and slot/block reference persisted.
```

Alternative path (either suffices): the two-tick four-leg rule — (a)
repeated zero-liquidity or non-executable liquidation observations across
≥2 refresh ticks; (b) pool removal or drained-liquidity state confirmed via
a source independent of the primary quote provider (direct chain state);
(c) no contemporaneous infrastructure outage recorded on our side; (d) a
documented dead-state timestamp. Failing all paths → `venue_unreachable`,
`quote_gap`, or indeterminate, not `token_dead`. The dust threshold freezes
at I1 merge; changes only via §9 amendment.

### 6.2 CP1 (trending) coverage taxonomy

```text
cg_covered            trending snapshots available across the full 24h window
cg_snapshot_gap       gaps; endpoint assigned only if a trending appearance is
                      positively observed; a negative with gaps = cg_indeterminate
cg_indeterminate      window not adequately observed; in denominator
cg_source_outage      CG API outage on record; in denominator, flagged
```

`VERIFY:` trending-snapshot capture cadence in deployed ingestion; state it
in the merged version.

### 6.3 Reporting rule

Denominators are always the full cohort (including `quote_not_scheduled`,
`quote_unavailable`, all indeterminates). Every rate is reported with its
coverage breakdown. Silent exclusion is a protocol violation.

### 6.4 Known asymmetries (disclosed, not adjusted)

- Analysis 1 places both cohorts on an identical synthetic clock; it
  measures gate selectivity, not realized user experience. Analysis 2
  measures realized delivery but has no symmetric control clock. Neither
  substitutes for the other; reports present both.
- CP1 trending is partially downstream of alerting ecosystems the delivered
  cohort participates in; CP2 is the endpoint robust to this.
- The secondary contrast is non-independent by construction; its
  including/excluding-crossover presentations bound the crossover effect
  rather than estimating it.

The v0.5 `measurement_unavailable` CP2 coverage class is **removed** —
superseded by §6.5. CP2 coverage classes apply only to admitted primary
assignments.

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

Terminology: **provider-accepted delivery** — the provider (e.g., Telegram)
accepted the message and returned an identifier. This does not assert human
viewing, and no field in this protocol claims it does.

### 7.1 Mechanism — independent durable spool

Delivery evidence persists to a **durable spool independent of the primary
database path** — no shared SQLite connection, transaction, or lock path
with suppression, cohort, or any production write. Candidate mechanisms:
separate SQLite database file, fsync-on-append JSONL, or another existing
durable seam. **F2's investigation selects the mechanism** (`PENDING-F2:`
recorded selection with rationale, per §7.4); this document constrains the
properties, not the implementation.

### 7.2 Sequence (two-phase, spool-first)

```text
1. Durably append pre-send intent to the spool (fsynced before step 3):
   internal alert ID, correlation ID, provider, destination identity,
   intent timestamp, gate-evaluation link
2. Best-effort mirror of the intent into the primary DB (failure tolerated)
3. Send once
4. Durably append provider response (provider message_id, acceptance
   timestamp, response status, persistence_status, intent link) to the spool
5. Reconcile the primary DB asynchronously from the spool
```

### 7.3 Behavior-neutral failure semantics

```text
intent DB mirror fails, spool succeeds      → send proceeds normally
provider accepted, DB persistence fails     → delivery remains provable from
                                              the spool; reconciliation
                                              restores DB state
spool unavailable                           → send behavior UNCHANGED (alerts
                                              are production; instrumentation
                                              never blocks them); the
                                              assignment is classified
                                              delivery_evidence_unavailable —
                                              excluded from the provider-
                                              accepted derived state, never
                                              fabricated into it; retained in
                                              all denominators
intent present, no response evidence,
send may have occurred                      → delivery_unknown_after_send;
                                              reconciliation MAY match
                                              provider-side history against
                                              intents but recovery is not
                                              assumed; unreconciled cases
                                              stay delivery_unknown_after_send,
                                              in denominators, reported
resend                                      → never automatic without proven
                                              provider idempotency (documented
                                              idempotency key or dedup
                                              guarantee for the exact send
                                              path); operator-initiated
                                              resends are new intents.
                                              Duplicate human-visible alerts
                                              are a production harm this
                                              instrumentation must not cause.
```

### 7.4 F2 persistence scope

F2 selects or approves the persistence path for **all load-bearing cohort
evidence**, not only delivery responses:

```text
gate evaluations · primary assignments · downstream dispositions ·
delivery evidence · quote events · outcome events ·
experiment-health transitions (§11.3)
```

plus (V1 extension):

```text
experiment-control journal (control plane): an independent persistence
seam capable of durably recording cohort_status transitions, fencing-token
changes, and failure episodes EVEN WHEN the primary measurement store is
the failed component. It must not share a write path, connection, or lock
with the measurement store.
```

The primary SQLite database remains acceptable for any measurement-store
component **only if** F2 proves its transaction and lock path is independent
of, and safe against, the production detection/dispatch path; otherwise the
measurement store is isolated.

**Control-plane seam — RESOLVED (F2 + reviewer rulings 2026-07-27):** a
separate SQLite database file `control_plane.db` — append-only hash-chained
event journal + authoritative admission ledger, dedicated connections (never
the pipeline's shared connection), two writer classes (narrowly scoped
runtime supervisor for automatic transitions; operator scripts for
authorization-class events). Architecture, writer model, and rejected
alternatives: annex §12/§12a/§12b
(`tasks/prereg_detection_gate_enrichment_cohort.md`).

`PENDING-F2:` the remaining per-component selections (measurement-store
components + spool mechanism) with rationale, recorded before I1 sign-off —
prepared analysis exists (Session-2 preparation pack) but the recorded
selection awaits the F2 corrective PR's merge/deploy/verification and
reviewer confirmation.

## 8. Versioning

Independently versioned, each recorded on every gate_evaluation row and
every quote/outcome event it governs:

```text
gate_version               §3.3 (gate code + runtime gate config)
pricing_protocol_version   this document's §4 rules (outlays, clocks, latency)
quote_model_version        quote source, venue routing, slippage/fee model
outcome_evaluator_version  CP1/CP2 evaluator code + cadence
```

A change to any one bumps only itself. gate_version changes open new
sub-cohorts; prior rows are never invalidated. Changes to the other three
during accrual go through §9 amendment, are labeled in reports, and results
are additionally broken out by the changed version where feasible.
`fixed_pre_registered_latency` is bound per gate_version.

## 9. Behavior-neutrality and change control

- The Session 2 increments add recording, quote scheduling for cohort
  pricing, delivery-evidence persistence, and experiment-control machinery
  only. Gate logic, thresholds, caps, ranking, dedup, alert content, and
  lane modes are unchanged. Any diff touching gate or dispatch behavior
  fails review by definition.
- After the first cohort row: no field of §2–§7 changes without (a) recorded
  product-owner ruling, (b) version bump, (c) post-hoc labeling in all
  subsequent reports, (d) pre-amendment data retained and reportable under
  original definitions.
- Any gate_version change (code or runtime config) opens a new sub-cohort,
  reported separately; prior rows are never invalidated.
- The frozen quantities under this change control include: the 60s latency
  value and its estimator parameters (§4.3), the $1.00 dust threshold
  (§6.1), the 30-determinate/80%-coverage evidentiary thresholds (§4.7),
  the entry-only $500 rule (§4.2), the Hájek estimator definition (§4.7),
  the sampling METHOD (§4.7, I1) and VALUES (§4.7, I3), the pause-trigger
  definitions and thresholds (§11.3), and the spool mechanism selection
  (§7.4) once recorded.

## 10. Evaluation and reporting

First evidentiary report at §2.7 maturity: primary contrast (Analysis 1)
with CP1/CP2 rates and full coverage breakdowns; secondary contrast
(Analysis 2) with the §2.5 crossover presentations; disposition-cost
analysis; crossover outcomes; latency_tax distribution;
gate_version/sub-cohort history; all §4.7 sampling-related reporting rules
(Hájek + unweighted + per-stratum; sampled-in/out counts per stratum in
every denominator table); resolution record of every former
`PENDING-S1`/`PENDING-F2`/`VERIFY` marker. Reports state the evaluation
timestamp at which derived states were computed. The report contains no
threshold/cap change recommendations; those go to a separate ruling request
referencing the report.

## 11. Implementation increments and experiment lifecycle

### 11.1 Increments

```text
I1  Pre-registration document (this document), merged under the I1 gate
I2  gate_evaluation / primary_assignment persistence + delivery spool +
    reconciliation + control-plane journal
I3  Isolated quote/outcome worker + capacity controls
I4  Cohort activation
```

No cohort row accrues until the complete measurement path is proven.

### 11.2 Synthetic-only before activation

Before I4, I2 and I3 process **only synthetic validation identities**:

```text
- synthetic identities use a reserved namespace: canonical_contract prefixed
  with the sentinel "VALIDATION-" (impossible as a base58 mint) AND stored
  with is_synthetic = true AND written to a segregated validation store/
  namespace (VERIFY at I2: separate DB file vs schema-enforced partition,
  per F2's architecture)
- every cohort query excludes synthetics BY CONSTRUCTION (query layer reads
  only the production namespace; is_synthetic is defense-in-depth, not the
  primary guard)
- NO live candidate receives a gate_evaluation or primary_assignment row in
  the production namespace before I4 — a feature flag does not make live
  writes "inert"; live writes are accrual and are prohibited pre-activation
```

### 11.3 Experiment control: fenced admission and objective triggers (V3, C5)

**Authoritative experiment-control record** (persisted in the control-plane
journal, §7.4):

```text
cohort_status = inactive | validating | active | paused_measurement_failure
activation_epoch
status_version / fencing_token     (ONE monotonic counter; increments on
                                    EVERY transition — ruling C5:
                                    inactive→validating, validating→active,
                                    active→paused (INCLUDING automatic
                                    supervisor pauses), paused→validating,
                                    active→inactive/closed. Authorization
                                    governs whether a transition may occur;
                                    it does not govern whether the version
                                    increments.)
transition_timestamp
transition_reason
failure_started_at / failure_ended_at
affected_components
first_complete_event_after_recovery
```

**Fenced admission protocol** — every production `primary_assignment`:

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

**Objective pause triggers** (subsystem-level; any one transitions
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

**Explicitly NOT pause triggers** (ordinary per-candidate coverage outcomes,
classified and retained): individual `quote_failed`, `quote_unavailable`,
`quote_not_scheduled`, single-candidate timeout/retry exhaustion,
`cg_snapshot_gap`, per-token `venue_unreachable`. These degrade coverage;
they do not transition `cohort_status`.

Recovery: passing end-to-end synthetic health check (a synthetic candidate
traverses the full path) before `cohort_status → active`;
`first_complete_event_after_recovery` recorded; fencing token incremented on
the resume transition.

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

### 11.5 Production fail-open guarantee (ruling C1)

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
[ ] All PENDING-S1 / PENDING-F2 / VERIFY markers resolved inline
[ ] Reviewer sign-off recorded
[ ] Product-owner authorization recorded
```

### I2 — persistence-merge gate

```text
[ ] Implements §2 model + §7 spool per F2's architecture
[ ] Crash-window tests: DB-mirror failure, post-send crash, spool-
    unavailable fail-open — all semantics of §7.3 demonstrated
[ ] Synthetic-namespace segregation demonstrated (§11.2); zero production-
    namespace writes under live traffic with activation off
[ ] Behavior-neutrality: no diff in gate/dispatch behavior (test-asserted)
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

---

## 13. Consolidation record

### 13.1 Section-by-section source provenance

| Section | Source(s), later-version-wins | Ruling overlays |
|---|---|---|
| §1 | v0.2 §1 (unchanged v0.3→v0.6) | — |
| §2.1 | v0.4 §2.1 (T1) | — |
| §2.2 | v0.4 §2.2 (T1; subsumes v0.2 §2.3 schema + v0.3 §2.5 version fields) | — |
| §2.3 | v0.4 §2.3 (T1) | §11.3 admission cross-ref (V3) |
| §2.4 | v0.3 §2.2–2.3 (S1) + v0.4 §2.4 (T1 evaluation-linking) + v0.4 §7.3 delivery_evidence_unavailable state | — |
| §2.5 | v0.2 §2.4 (R2) + v0.3 §2.4 (S2) + v0.4 §2.5 | — |
| §2.6 | v0.2 §2.2 | — |
| §2.7 | v0.4 §2.6 (assignments basis) + v0.2 §2.5 (health-check labeling) | — |
| §3.1–3.2 | v0.2 §3.1–3.2 (R7; unchanged thereafter) | — |
| §3.3 | v0.4 §3.3 (T5 full-SHA; supersedes v0.3 §3.3 truncated form) | — |
| §4.1 | v0.2 §4.1 | — |
| §4.2 | v0.2 §4.2 (R4) + v0.6 §4.2 (V4) | — |
| §4.3 | v0.2 §4.3 clock + v0.4 §4.3 estimator (T4, summarized as the invoked-fallback's basis) + v0.5 §4.3 (U4 resolution: 60s frozen + supersession path) | — |
| §4.4–4.6 | v0.2 §4.4–4.6 (unchanged thereafter) | — |
| §4.7 | v0.3 §4.7 isolation (S3) + v0.4 §4.7 thresholds/IPW (T3) + v0.5 §4.7 formula + Hájek (U4) + v0.6 §4.7 split freeze (V2) | C3 (I3 freezes values), C4 (digest formulation replaces `\|\|` concatenation of v0.3/v0.4/v0.6) |
| §5 | v0.2 §5 (R5 incl. crossing_order_indeterminate; unchanged thereafter) | — |
| §6.1 | v0.2 §6.1 taxonomy (R5) + v0.4 §6.1 (T5) + v0.5 §6.1 ($1.00 dust, U4) | — |
| §6.2–6.3 | v0.2 §6.2–6.3 | — |
| §6.4 | v0.2 §6.4 + v0.3 §6.4 (S2) + v0.6 §6.1–6.4 note (measurement_unavailable removed) | — |
| §6.5 | v0.6 §6.5 (V1) | C6 (control_status_version field + episode link) |
| §7 intro | v0.2 §7 (R6 terminology) | — |
| §7.1–7.3 | v0.3 §7 (S4 two-phase, unknown_after_send, no auto-resend) + v0.4 §7 (T2 spool, sequence, fail-open semantics) | — |
| §7.4 | v0.5 §7.4 (U3 broadened scope) + v0.6 §7.4 (V1 control-plane seam) | Control-plane selection RESOLVED per C-rulings; annex §12 referenced |
| §8 | v0.3 §8 (S5; unchanged thereafter) | — |
| §9 | v0.2 §8 + v0.3 §9 + v0.4 §9 extensions + v0.5/v0.6 frozen-value list | C5 noted via §11.3 |
| §10 | v0.2 §9 + v0.3 §2.3 (computation-timestamp disclosure) + v0.4 §10 (sampling reporting) | — |
| §11.1 | v0.4 §11 (T5) + v0.5 §11.1 (U1 naming) + control-plane journal added to I2 (V1) | — |
| §11.2 | v0.5 §11.2 (U2) | — |
| §11.3 | v0.5 §11.3 (U3 state machine) + v0.6 §11.3 (V3 fencing/admission/triggers) | C5 (every-transition increment) |
| §11.4 | v0.5 §11.4 + v0.6 §11.4 (V3 three-part proof) | — |
| §11.5 | — (new) | C1 verbatim |
| §12 | v0.5 §12 (U1 four gates) + v0.6 §12 (V2/V3 amendments); I2 gate = v0.5 list + v0.6 addition | — |

### 13.2 Remaining protocol markers (gate-scoped, NOT consolidation placeholders)

```text
PENDING-S1  §3.3  gate_code_hash manifest file list (drift-table reconciliation)
PENDING-F2  §7.4  remaining per-component persistence selections (measurement
                  store + spool mechanism); control-plane seam already resolved
VERIFY      §3.2  float→decimal mapping of deployed float features (I2)
VERIFY      §4.7  measurement-store lock/connection isolation (I2/I3, per F2
                  architecture)
VERIFY      §5.1  deployed CP2 price-refresh cadence (state at merge)
VERIFY      §6.2  deployed trending-snapshot capture cadence (state at merge)
VERIFY      §11.2 validation-namespace mechanism: separate DB file vs
                  schema-enforced partition (I2, per F2 architecture)
```

Each marker resolves at its designated gate per §12; the I1 gate requires
all of them resolved inline before merge. No source-chain consolidation
placeholders or inherited-section stubs remain — every section above is
fully stated.
