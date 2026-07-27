# Pre-registration — Detection-lane gate-enrichment cohort (annex)

> **This document is a detection-receipt implementation and evidence annex.
> The governing Session 2 experimental protocol is
> `tasks/prereg_forward_cohort_v0_6.md`.
> Where the two documents differ, the Forward Cohort Pre-Registration controls.**

**New primitives introduced:** detection_decision_receipts table + DETECTION_GATE_VERSION constant + independent control-plane journal (design selection only in this revision — no implementation)

> Filename note: `prereg_*.md` matches none of the plan/design/spec gated
> patterns (`plan_*`, `design_*`, `spec_*`), so the new-primitives hook does not
> gate this file. The marker line above is included anyway for hygiene.

**Production fail-open guarantee (reviewer ruling C1).** Failure or
unavailability of the experiment control plane, measurement store, spool,
quoter, or evidence machinery prevents cohort admission only. Production
detection, gate evaluation, ranking, dispatch, and alert delivery continue
unchanged and are never delayed or suppressed by measurement failure.

**v0.6 (2026-07-27) — I1 finalization per reviewer ruling.** Changes from v0.5:
Session-1 markers resolved (§3: shakedown census frozen per the PR #475
dormant-deployment ruling; cohort-start marker re-scoped to the I4 authorization
gate); fenced admission defined (§3a) with its enforcement mechanism (§12b:
admission ledger + fencing token); segregated census (§3b); domain-separated
hashes replacing the single generic identity hash (§7a: idempotency / sampling /
archive-manifest, separately versioned pre-images); the 1,068-row shakedown
permanently labeled `invalid validation evidence` with an explicit allowed/
prohibited-use list (§3); per-component F2 persistence architecture selected and
recorded (§12), including the two-class control-plane writer model (§12a:
runtime supervisor for automatic transitions + operator scripts for
authorization-class events). Phase map: **I1 = this document**; **I2/I3 =
implementation** (incl. the observation-only live inflow census); **I4 = cohort
activation**. I2/I3/I4 are HELD until the standalone P0 corrective PR for the
shared-connection transaction defect (F2, `classified_but_not_remediated`) is
merged, deployed, and verified clean.

This document pre-registers the analysis that the **behavior-neutral**
`detection_decision_receipts` instrumentation (PR
`feat/detection-decision-receipts`) makes possible. Writing it BEFORE any data
accrues is the point — the endpoints, cohort definition, evaluability floor,
and censoring taxonomy are fixed here so the later analysis cannot be
retrofitted to the data.

The instrumentation itself changes NO send / gate / product behavior. It only
records, for every candidate the ALR-02 detection lane evaluates, a structured
decision receipt. See `scout/trading/detection_alert.py`,
`scout/db.py::_migrate_detection_decision_receipts_v1`.

---

## 1. Motivation — the previously-unrecoverable comparison cohort

The ALR-02 detection lane (`scout/trading/detection_alert.py`) drops candidates
that fail the quality gate with a bare, unlogged `continue`. The same is true of
the trigger ("not early vs CG trending") drop. Those dropped candidates are the
**gate-failer comparison cohort** — the population against which any
gate-enrichment estimate must be measured — and until this instrumentation they
were unrecoverable. Without them, "the gate improves precision" is an assertion
with no denominator.

The receipts make the full decision surface durable: every evaluated candidate
gets exactly one receipt carrying its machine-readable terminal outcome plus the
raw inputs the decision consumed.

---

## 2. What "cohort" means here (and what it does NOT)

The gate-failers are a **COMPARISON cohort for gate-enrichment estimation**.
They are **NOT** an "eligible-but-unsent control" and this analysis makes **no
causal claim about sending**. We are estimating a descriptive contrast — how
often gate-*passers* vs gate-*failers* go on to trend — to characterize what the
gate is selecting for. We are not running an experiment in which sending is the
treatment; nothing here randomizes or withholds a send.

Any statement of the form "sending caused X" is out of scope for this cohort.

---

## 3. Cohort start — deployment timestamp, no retroactive reconstruction (LOCK 5)

Verbatim definition:

> **Cohort start = the first healthy production process activation after BOTH
> the schema migration AND the two-identity fix code are deployed, recorded as
> an application-ready timestamp AFTER migrations complete.**

This is **one immutable timestamp** — not merge time, not migration time, not an
approximate window, and not deploy alone (see the replacement-cohort gate at the
end of this section). Pre-timestamp decisions are **outside the cohort and cannot
be reconstructed** (gainers/trending snapshot retention is short and the
dropped-candidate stream was never recorded).

**Shakedown exclusion (2026-07-26).** The first production activation
(2026-07-26T02:47:23Z) ran with two successive pre-fix classifiers:

1. the original evaluation-instance-independent key, under which cross-cycle
   re-polls of the same token collapsed to one row and were mis-classified as
   **conflicting duplicates** (715/cycle — see §7); and it also exposed
2. the **volume** consequence that motivated the two-identity model + capacity
   artifact (`tasks/capacity_detection_receipts_2026_07.md`).

All receipts written in the range **`[2026-07-26T02:47:23Z,
2026-07-26T15:32:08Z)`** are **pre-cohort shakedown data**: they are RETAINED and
labeled invalid **by this documented timestamp-range exclusion**, NOT deleted or
rewritten. No existing receipt row is mutated. (If a marker is ever added it must
be additive — e.g. a cohort-registry row — never an `UPDATE` of receipt rows.)

**Frozen shakedown census (Session-1 marker RESOLVED — PR #475 dormant-deployment
ruling, 2026-07-26).** The governing values, superseding the interim 733-row
snapshot previously cited here:

- Invalid-range end = the dormant-release application-ready timestamp
  **2026-07-26T15:32:08Z** (conservative post-migration readiness boundary: the
  first demonstrated healthy post-migration cycle; process activation 15:30:06Z
  and migration execution 15:30:08Z are recorded as such, NOT as readiness).
- Last invalid receipt: **2026-07-26T15:10:32.411166Z**.
- **Final invalid receipt count: 1,068** — frozen, retained, analytically
  excluded, and never reclassified into any future cohort.
- Receipt accrual has been DISABLED since that deployment
  (`DETECTION_RECEIPTS_ENABLED=false`, dormant per the #475 ruling); the count
  cannot grow.

**Evidentiary role — permanently labeled `invalid validation evidence` (reviewer
correction).** The 1,068-row population (which subsumes the earlier 733-row
02:49:25Z–03:12:41Z measurement) MAY support exactly three uses:

- proof that invalid rows remain segregated (§3b);
- hot/cold reconciliation exercises;
- validation of timestamp and archival conventions.

It MUST NOT be used as a substitute for: valid blocked-candidate inflow; a
production cohort capacity census; provider-accepted delivery latency; or valid
CP1/CP2 denominators. Where the capacity artifact cites these rows, it cites
them as a shakedown-era measurement instrument, not as production cohort
evidence — the **live, observation-only I3 census of passed/blocked inflow is
still required** and must be measured without creating production cohort rows.

> **Cohort start (UTC): NOT STARTED — intentionally unresolved (I4 gate).**
> The two-identity fix is deployed (dormant), so the original "TBD pending fix
> deploy" wording is obsolete; what remains is AUTHORIZATION. The value will be
> the application-ready timestamp (recorded AFTER migrations complete) of the
> first healthy process activation following an explicit reviewer cohort-start
> authorization (I4). It is written to the control-plane journal (§12) at
> activation, never retro-fitted.

Rows with `decided_at` in the shakedown range above (or otherwise before the
authorized cohort-start timestamp) are excluded.

### 3a. Fenced admission (v0.6 clarification)

Cohort admission is **fenced on both sides**, and the fence is definitional, not
procedural:

1. **Lower fence — the invalid shakedown range.** No receipt with `decided_at <
   2026-07-26T15:32:08Z` is ever admissible, regardless of its content, key
   shape, or later reclassification pressure. The 1,068 invalid rows are
   permanently outside the fence: retained for audit, excluded from analysis,
   never mutated, never reclassified.
2. **Admission fence — the authorized cohort start.** A receipt is admitted to
   the cohort **only** if `decided_at >= cohort_start`, where `cohort_start` is
   the I4-authorized application-ready timestamp recorded in the control-plane
   journal (§12). Receipts written while the lane is enabled but BEFORE an
   authorized cohort start (should that state ever occur) are fenced out exactly
   like shakedown rows: a new documented invalid range is opened for them.
3. **No third ANALYTICAL state (reconciled with measurement-gap records —
   reviewer ruling C6).** Every receipt is either inside the fence (admitted,
   in-cohort) or outside it (in a documented invalid range or linked to a
   control-journal failure episode). A candidate evaluated during a
   non-active control state (`inactive`, `validating`,
   `paused_measurement_failure`) receives NO evidentiary assignment; any
   operational receipt retained from such an interval is outside the cohort
   fence, linked to the invalid range or failure episode that covers it, and
   never becomes a hidden third analytical cohort. Genuinely unclassifiable
   receipts are a reconciliation failure and invalidate the affected window
   (§8).

**Measurement-gap observations (C6 — canonical record, mirrored from the
governing protocol §6.5).** Where a candidate flows during a non-active state
and it is safely recordable, an append-only record is written:

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

Rules: operational and availability reporting ONLY (uptime, admission rate,
gap counts); NEVER enters CP1/CP2 denominators; NEVER reconstructed into a
later assignment; linked to the authoritative control-journal failure
episode. If the system cannot write even the gap observation, the failure
episode records that observations were unavailable, and every evidentiary
report says so.

The fence is a DEFINITION; its runtime ENFORCEMENT is the fenced-admission
critical section of §12b (authoritative admission ledger + fencing-token
compare-and-commit inside `control_plane.db`). Every assignment admission
validates the current active fencing token; stale tokens are rejected across
process and crash boundaries.

### 3b. Segregated census (v0.6 clarification)

The census of every fenced-out population is **frozen and segregated**:

- The invalid shakedown census (n=1,068; range end 15:32:08Z; last row
  15:10:32.411166Z) is recorded once, in this document and in the control-plane
  journal (§12), and is never recomputed against live tables as if it could
  drift.
- Valid-cohort counts and invalid-population counts are NEVER merged, summed, or
  reported in a shared denominator. Any table or query that reports cohort size
  must report `in_cohort_n` and, separately labeled, `invalid_excluded_n`.
- The hot/cold archive tiering (§8) preserves segregation: archival moves rows
  between tiers but never across the fence, and per-tier census totals must
  reconcile to the frozen invalid census + the running valid census
  (`hot_n + cold_n = invalid_frozen_n + valid_running_n`, checked at every
  archive transaction and at manifest freeze).

**Replacement-cohort gate (the cohort does NOT start on deploy alone).** The
re-anchored cohort start is valid only once ALL hold: (a) a new reviewed head on
an exact green CI run; (b) a reviewer-APPROVED evidence-storage design (§8 +
capacity artifact); (c) migration + code deployed; (d) the application-ready
post-migration timestamp recorded; (e) one clean production reconciliation cycle
(§8 identity holds, `conflicting_duplicates_n == write_failures_n == 0`); and (f)
evidence storage demonstrably cannot threaten the service or silently truncate the
evidence lifecycle.

---

## 4. Primary INDEX decision and arm assignment (reviewer correction 1)

There is **ONE common index decision for BOTH arms**:

- **Index decision** = the token's **FIRST RECORDED EVALUATION after cohort
  start, of ANY outcome** — i.e. `MIN(decided_at)` across all of that token's
  receipts, computed at analysis time. Call its timestamp `index_decision_at`.
- **Arm assignment** = the machine-readable **gate outcome AT that index
  receipt** (`gate_fail_quality` and `too_old`/`not_early`/`universe_filter` on
  the failer/ineligible side; `sent` — or a gate-*passing* eligible outcome — on
  the passer side, per the analysis-plan mapping frozen below).
- **Outcome windows** (§5) are measured from `index_decision_at` for **BOTH
  arms**, identically.

This deliberately replaces the earlier "first *qualifying* decision" anchor,
which is **VOID**: gate-failers have no qualifying decision, so anchoring on it
introduced a timing asymmetry and post-index selection between the arms. The
common first-evaluation anchor removes both.

**Later evaluations, later qualification, or eventual sending of the same token
are reported SEPARATELY and NEVER reassign the primary cohort.** A token whose
index decision is `gate_fail_quality` but which later sends stays in the
gate-failer arm for the primary analysis; its later send is a secondary
observation.

**Two-identity model — analytical index identity (reviewer, 2026-07-26).** Under
the evaluation-identity write model (§7), every cycle writes a NEW receipt per
evaluated token, so a token has MANY receipts. The **analytical index identity**
is the token's FIRST VALID evaluation after cohort start; it permanently fixes
the arm assignment and the endpoint anchor `index_decision_at`. It is a
deterministic **query-time** definition (NOT a write-time key):

```sql
-- index_decisions: one row per token = its first VALID (post-shakedown,
-- in-cohort) evaluation. Deterministic: MIN(decided_at); ties broken by MIN(id).
WITH valid AS (
  SELECT * FROM detection_decision_receipts
  WHERE decided_at >= :cohort_start          -- excludes the shakedown range (§3)
),
firsts AS (
  SELECT token_id, MIN(decided_at) AS index_decision_at
  FROM valid GROUP BY token_id
)
SELECT v.token_id,
       v.decided_at AS index_decision_at,
       v.outcome    AS index_outcome,        -- fixes the arm (mapping below)
       v.id         AS index_receipt_id
FROM valid v
JOIN firsts f
  ON f.token_id = v.token_id AND f.index_decision_at = v.decided_at
GROUP BY v.token_id
HAVING v.id = MIN(v.id);                      -- deterministic tie-break
```

Post-index evaluations exist as receipts but carry **no arm/endpoint weight** —
they are "reported separately" (flow attrition, repeated-token accounting). The
endpoint windows (§5) are measured from `index_decision_at`.

Arm mapping frozen for the primary analysis (index-receipt outcome → arm):

| Index outcome | Arm |
|---|---|
| `gate_fail_quality` | gate-failer (comparison) |
| `sent` | gate-passer |
| `not_early`, `universe_filter`, `dedup_24h`, `rate_limit`, `dispatch_failed`, `too_old` | reported separately (ineligible / non-gate drops); NOT part of the pass-vs-fail primary contrast |

**`not_early` — verbatim semantics (reviewer amendment 1).**

> `not_early`: the token had already entered CG trending before
> `index_decision_at`; excluded from both primary arms and reported as flow
> attrition.

It **must never** count as a 24h/72h hit, a miss, a gate-passer, or a
gate-failer in the primary comparison. The receipt preserves the underlying
pre-index trending timestamp used for the classification in its `raw_inputs`
payload as **`pre_index_trending_at`** (= `MIN(snapshot_at)` from
`trending_snapshots`, the exact value the trigger read), so the "already
trending" determination is durable and auditable.

---

## 5. Endpoints

- **Primary endpoint:** the token appears on **CG trending within 24h** of
  `index_decision_at`.
- **Key secondary:** CG trending **within 72h** of `index_decision_at`.
- **Exploratory ONLY (NOT co-primary):** gainers-surface entry and price-return.
  These require independent validation before any promotion — reference-price
  rules, liquidity / data-quality checks, and outlier handling must be specified
  and reviewed first. They are explicitly not weighed in the primary conclusion.

Receipts must survive through **both** the 24h and 72h horizons **plus**
reconciliation, manifest freeze, and final analysis (see §8).

---

## 6. Evaluability floor (NOT a decision threshold)

`n >= 30` sends **AND** `n >= 30` gate-failers is the **evaluability floor** —
below it the contrast is simply not yet evaluable and no conclusion is drawn. It
is **not** a decision threshold; crossing it does not by itself license any
action. When evaluable, the analysis reports, for each arm:

- exact denominators (distinct tokens at index),
- effect size with confidence intervals (bootstrap CI, ≥10,000 resamples, on the
  per-token indicator),
- repeated-token reconciliation (see §7),
- regime segmentation (split on deploy / flag-flip / CG-regime boundaries; the
  contrast must be inspected within segments, not only pooled),
- missingness accounting (see §9).

No cap changes are proposed or implied by this analysis.

---

## 7. Two-identity model — evaluation identity vs analytical index identity

**Reviewer, 2026-07-26.** There are TWO distinct identities. Conflating them is
what produced the 715-conflicts/cycle defect.

### 7a. EVALUATION IDENTITY (the write-time idempotency key)

Each later poll of a candidate is a **NEW receipt**. The idempotency key
incorporates the **evaluation instance** (the cycle's `decided_at`):

```
idempotency_key = sha256_hex(
  "{token_id}|{outcome}|{source_observation_ts}|{gate_version}|{evaluation_instance}"
)
# evaluation_instance = the cycle decided_at (UTC isoformat)
```

**Domain-separated hashes (v0.6, reviewer correction).** Purposes share
identity FIELDS but never hash semantics: one generic identity hash reused for
idempotency, sampling, and archive integrity is rejected. Three separately
versioned pre-images:

1. **Idempotency** (write-time, deployed): the pipe-joined pre-image above —
   versioned as `receipt-idempotency-v1`, exactly the shape shipped in #475.
   Its canonicalization rules are frozen: null `source_observation_ts` renders
   as the **empty string**; the separator is a **literal `|`**;
   `evaluation_instance` is the cycle `decided_at` in **UTC isoformat**; digest
   **sha256 hex, lowercase**. Used ONLY for write-time idempotency/dedup of
   receipt rows. Any change is a breaking change requiring a `gate_version`
   bump AND a new cohort.
2. **Cohort sampling** (analysis-time; reviewer rulings C3 + C4): a canonical
   STRUCTURED pre-image with explicit domain separation, with the v0.6
   symmetric modulo rule applied to the digest:

   ```text
   sampling_digest = SHA256(canonical_json({
     "domain": "gecko-alpha-cohort-sampling-v1",
     "chain": canonical_chain,
     "canonical_contract": canonical_contract,
     "fixed_salt": fixed_salt
   }))

   sampling_bucket = unsigned_big_endian_integer(sampling_digest) mod 1000
   included = sampling_bucket < K_s
   ```

   `canonical_json` = UTF-8, sorted keys, no insignificant whitespace.
   **Freeze split (C3 — I3 controls the values):** I1 freezes the METHOD
   (this algorithm, the domain string, strata, budget rule, K_s-selection
   rule); I3 freezes the VALUES (`fixed_salt`, per-stratum `K_s`, inclusion
   probabilities and census-derived numbers) after the ≥72h non-cohort
   census and before I4, recorded in the I3 gate record; I4 loads the
   already-frozen I3 values and activates. The persisted record carries the
   hash-version identifier (`gecko-alpha-cohort-sampling-v1`), stratum,
   `K_s`, and inclusion probability. Any subsample of tokens is
   drawn/de-duplicated on `sampling_digest`, never on ad-hoc column subsets
   and never on the idempotency key.
3. **Archive-manifest integrity** (§8): the integrity hash covers the
   **manifest itself** — `SHA256("gecko-alpha-archive-manifest-v1" || canonical
   manifest bytes)`; the manifest lists member rows by their identity fields
   (including `idempotency_key` as a FIELD, which is fine — shared fields, not
   shared hash semantics). A restored partition is verified against the
   manifest hash, and its membership against the listed keys.

Because the cycle instant is IN the key, cycle-N and cycle-N+1
evaluations of the same token yield **different keys and DISTINCT rows**. Routine
re-polls are therefore never collapsed and never mis-classified as conflicts —
this eliminates the false-conflict class **by construction**.

`INSERT OR IGNORE` + the `UNIQUE` index now collapse only an **exact repeat of
the SAME evaluation instance** (an intra-cycle crash/retry, or a candidate
duplicated within one cycle's input list):

- **exact idempotent replay** — same evaluation-instance key AND byte-identical
  payload. Benign. Counted as `exact_replays`.
- **conflicting duplicate** — same evaluation-instance key but a DIFFERENT
  payload (the *same* evaluation produced two payloads — a genuine defect, or the
  gate config/code changed without a `gate_version` bump within one instant).
  Counted as `conflicting_duplicates`, a structured `detection_receipt_conflict`
  warning is emitted, and the count appears in the per-cycle
  `detection_receipt_summary` so it can never pass as healthy coverage.

Why the earlier fixes were rejected: the ORIGINAL key omitted the evaluation
instance, so cross-cycle re-polls hit the same key and (under full-payload
comparison) were counted as conflicts — 715/cycle on 2026-07-26 02:53Z
(`evaluated=724, newly_written=9, conflicting_duplicates=715`). A *reduced-field*
comparison was also rejected as insufficient; the correct fix is the
evaluation-identity key, which makes each poll its own row.

### 7b. ANALYTICAL INDEX IDENTITY (the query-time analytical unit)

The token's **first valid evaluation after cohort start** permanently fixes its
arm and endpoint anchor `index_decision_at`. This is the deterministic query in
§4 (`MIN(decided_at)` per token over in-cohort rows, `MIN(id)` tie-break).
Post-index evaluations remain as receipts but carry **no** arm/endpoint weight
(reported separately as flow attrition + repeated-token accounting).

**Consequence for volume:** because each poll is now a distinct row, the receipts
table grows per-evaluation, not per-decision-change. The measured/projected volume
and the resulting evidence-storage design constraints are documented in the
**capacity artifact** `tasks/capacity_detection_receipts_2026_07.md`; the
replacement cohort does not start until that storage design is reviewer-approved
(§3 gate).

---

## 8. Evaluation boundary, reconciliation, coverage & retention (LOCK 4 + corrections)

**Evaluation boundary (reviewer amendment 2).** In
`notify_early_detections` there is an explicit boundary: rejection BEFORE it is a
**filtered input** (no substantive decision logic — no score read, no gate
comparison, no arm-relevant branching — has executed), so **no receipt** is
emitted. The current pre-boundary filters are, by reason:

- `filtered_non_cg_source` — `chain != "coingecko"` (the lane only evaluates CG);
- `filtered_missing_id` — no `contract_address` (cannot key a receipt);
- `filtered_malformed` — the candidate's own attributes cannot even be read.

Once execution crosses the boundary (the first read of decision input — the
authoritative `first_seen`/observation time), a receipt (or a separately-keyed
equivalent) is **mandatory** for every terminal decision. Filtered inputs are
counted **separately by reason** in `detection_receipt_summary` so they never
disappear silently and never inflate `evaluated`.

**Per-cycle reconciliation identity (reviewer naming).** Every cycle the lane
emits `detection_receipt_summary` with
`evaluated_n / newly_written_n / exact_replays_n / write_failures_n /
conflicting_duplicates_n` plus the `filtered_*_n` reasons. The identity is:

```
evaluated = newly_written + exact_replays + write_failures + conflicting_duplicates
```

with the `filtered_*` counts reported BESIDE it and EXCLUDED from `evaluated`.
Under the two-identity model a **clean cycle following a prior cycle** shows
`newly_written == evaluated` (minus filtered) with
`exact_replays == conflicting_duplicates == 0` — this is the positive proof that
the false-conflict class is gone, and the **first-cycle reconciliation identity**
used to validate the replacement cohort at activation (§3 gate): the identity
must hold and `write_failures` / `conflicting_duplicates` must be 0.

**If receipts cannot be reconciled (the identity fails, or `write_failures` or
`conflicting_duplicates` is non-zero over the cohort window), the cohort is
INVALID for the affected window.** Coverage is reconciled per-cycle in-log; a
standalone cron watchdog is intentionally not added for this table (the
per-cycle reconciliation requirement in this §8 covers it — the per-cycle
summary, plus the fact that the lane itself is already watchdogged and the
writer only runs while `DETECTION_ALERT_LANE_ENABLED` is true; for
experiment-level health and pause semantics see the canonical protocol's
experiment-control section, `tasks/prereg_forward_cohort_v0_6.md` §11.3).

**Retention / prune guard (reviewer correction e).** Receipts MUST survive
through both outcome horizons, reconciliation, manifest freeze, and final
analysis. Two independent guards enforce this:

1. **Config floor** — `DETECTION_DECISION_RECEIPTS_RETENTION_DAYS` has a
   structural floor of **120 days** (`ge=120`), well beyond the 72h analysis
   horizon, so no operator value can prune inside the analysis lifecycle.
2. **Cohort-completeness guard** — `prune_detection_decision_receipts` prunes
   **nothing** until `DETECTION_RECEIPTS_COHORT_CLOSED_AT` (an ISO-8601 UTC
   marker) is set; when set, it prunes only rows older than **both** the
   retention floor **and** the close marker. Rows newer than the marker (a
   still-open cohort) are never pruned.

**Pruning any receipt before its cohort's analysis completes INVALIDATES the
cohort.** The close marker is set by the operator only after the final analysis
is frozen. A prune-guard test (`test_prune_blocked_until_cohort_closed`,
`test_prune_respects_cohort_close_marker`, `test_retention_floor_rejects_below_120`)
enforces both guards.

**120d is an EVIDENCE-retention requirement, not a single-file mandate.** Under
the two-identity model the per-evaluation row volume makes a single 120-day
SQLite file infeasible on the current box (see the capacity artifact
`tasks/capacity_detection_receipts_2026_07.md`). The evidence lifecycle
(reconciliation → both horizons → cohort closure → final analysis → audit buffer)
is preserved for ≥120 days across the approved + IMPLEMENTED **lifecycle-tiered
hot SQLite + time-partitioned compressed cold archive + queryable integrity
manifest** (`scout/trading/receipt_archive.py`). Ordinary post-index receipts
leave the hot table only via the fail-closed 7-step archival transaction (which
holds rows hot until an independent off-host durable copy is confirmed); the hot
prune guards above still protect index receipts + in-lifecycle rows. The
two-identity write model is unchanged by the storage tiering.

---

## 9. Censoring taxonomy

Every token that does not reach an endpoint is classified into exactly one:

- **not-yet-mature** — `now - index_decision_at < window`; the outcome is simply
  not observable yet. Excluded from the numerator AND denominator until mature.
- **missing** — mature, but the trending/gainers signal for the window is absent
  from the source tables (data gap, not a true negative). Reported as missingness
  (§6); NOT silently counted as "did not trend".
- **corrupt** — the receipt or the outcome data fails an integrity check
  (unparseable timestamp, conflicting duplicate, reconciliation failure).
  Excluded and reported.

A true negative ("evaluated, matured, and did not trend") is distinct from all
three and is the only non-event that counts against the numerator.

---

## 10. Receipt fields (what each row persists)

First-class columns (see the migration): `token_id`, `decided_at` (UTC),
`outcome`, `reason`, `source_observation_ts` (the candidate's `first_seen_at` the
decision was based on), `gate_version` (constant `DETECTION_GATE_VERSION`),
`code_version` (deployed git SHA read at startup, or NULL — in which case
`gate_version` is the resolver of record), `score_before` (raw model
`quant_score`, pre-clip; NULL if unscored), `score_after` (the `int(x or 0)`
operand actually compared), `comparator` (`>=`), `threshold_value`
(`DETECTION_ALERT_MIN_QUANT_SCORE` at decision time), `signals_fired` (JSON,
convenience only), `raw_inputs` (JSON — raw component values + per-input
missingness/default indicators + outcome-specific inputs), `idempotency_key`.

`signals_fired` is a **convenience** field and cannot substitute for the
underlying values; the gate arithmetic is reproducible from
`score_before` / `score_after` / `comparator` / `threshold_value` /
`gate_version` / `code_version` / `raw_inputs`.

---

## 11. Gate version discipline

`DETECTION_GATE_VERSION` (currently `"466.1"`) is bumped on any future change to
the detection-lane gate logic. Bumping it opens a fresh analytical unit per token
(it is part of the idempotency key) and makes pre- vs post-change receipts
distinguishable. A gate change that is NOT accompanied by a version bump is
surfaced as a `detection_receipt_conflict` (§7) rather than silently corrupting
the cohort.

---

## 12. Per-component persistence architecture (v0.6 — F2-informed, selected and recorded)

**Context (F2, status `classified_but_not_remediated`).** The standalone P0
investigation established that transactions on the process-shared aiosqlite
connection (`db._conn`) are only serialized for callers that take `db._txn_lock`;
the chain tracker and the DEX discovery writers bypass it, producing
nested-transaction collisions AND a foreign-rollback hazard (a colliding caller's
`ROLLBACK` can silently discard another writer's uncommitted work). Suppression
enforcement was verified fail-closed with zero escapes in the tested window, but
that finding is scoped to suppression enforcement only — the defect itself
remains active until the standalone P0 corrective PR (transaction-lock
discipline, foreign-rollback prevention, exception-detail retention,
deterministic concurrency tests) is merged, deployed and verified clean.

**Selection rule applied:** no load-bearing cohort evidence may depend for its
integrity on a write path that shares an unserialized connection with unrelated
writers; the control plane must additionally survive a pipeline-process defect
entirely.

| # | Component | Load-bearing? | Selected persistence | F2 rationale |
|---|---|---|---|---|
| 1 | `detection_decision_receipts` (hot tier) | **Subordinate detection-receipt audit/validation evidence** for THIS annex's cohort — NOT authoritative Forward Cohort gate_evaluations or assignments (those are authoritative in `measurement.db` / `control_plane.db` per the canonical §7.4 matrix) | scout.db table, ALL writes through the common disciplined transaction manager (P0 PR); `INSERT OR IGNORE` + UNIQUE(idempotency_key) preserved | Collisions eliminated at the mechanism level, not per-caller convention; a receipt write can neither be victim nor perpetrator of a foreign rollback |
| 2 | Cold archive partitions + integrity manifest (`scout/trading/receipt_archive.py`) | YES — evidence lifecycle ≥120d | Time-partitioned compressed files OUTSIDE SQLite + queryable manifest. The manifest enumerates member rows using their persisted identity and idempotency fields. Manifest integrity is protected by `gecko-alpha-archive-manifest-v1` over canonical manifest bytes. The sampling hash is not the archive integrity key. Fail-closed 7-step archival transaction; rows stay hot until the independent off-host durable copy is confirmed (off-host destination = standing operator dependency) | File-level artifacts are immune to connection-sharing defects; manifest hashes make silent truncation/corruption detectable at restore |
| 3 | **Independent control-plane journal** (cohort lifecycle: cohort-start authorization + app-ready timestamp, fencing-token epochs, census freezes, invalid-range declarations, close marker, invalidation/pause events, archive/restore proofs, admission ledger) | YES — the cohort's authority record | **NEW, SEPARATE SQLite database file** (`control_plane.db`), append-only event table, hash-chained rows (each row carries sha256 of predecessor), opened by DEDICATED connections; NEVER on `db._conn`. **Two writer classes** (§12a): a narrowly scoped runtime **control-plane supervisor** for automatic protocol transitions, and operator-invoked scripts for manual authorization/pause/recovery | Independence is the point: the journal must remain trustworthy even if scout.db's shared connection misbehaves (F2's exact failure class). Append-only + hash chain makes retro-editing evident |
| 4 | Per-cycle reconciliation census (`detection_receipt_summary`) | Supporting (validity gate §8) | Structured journal (journald) at cycle time — unchanged; but every census FREEZE and every validity verdict derived from it is durably recorded in the control-plane journal (#3) | journald retention is finite (the F9 lesson: journal-only evidence expires); freezes must outlive it |
| 5 | DEX outcome-ledger evidence (`signal_outcome_ledger`, `ledger_enrollments`) | YES — **the separate Session 1 DEX cohort (T0 clock); explicitly NOT Session 2 Forward Cohort evidence** | scout.db under the same disciplined transaction manager as #1 after the P0 PR | Same shared-connection exposure; same remedy; may remain in scout.db because it is not Forward Cohort evidence |

### 12a. Control-plane writer model (reviewer correction — NOT operator-only)

The v0.6 protocol requires AUTOMATIC transitions the moment the system detects:
a load-bearing evidence write failure; spool fsync failure; canonicalization
failure; quote-queue threshold breach; the quoter breaker remaining open; a
stale outcome worker; or unavailability of the control journal itself. An
operator-only writer cannot deliver those. Two writer classes are therefore
defined:

1. **Runtime control-plane supervisor** (narrowly scoped; I2 implementation:
   `scout/control_plane/supervisor.py`): the ONLY runtime writer. It holds a
   dedicated `control_plane.db` connection (never `db._conn`) and may write
   exactly the automatic-transition event types enumerated above (pause,
   breach, invalidation, degradation, journal-unavailability fallback). It may
   NOT write authorization-class events (cohort-start authorization, close
   markers, census freezes, activation).
2. **Operator-invoked scripts**: manual activation, pause, and recovery
   actions, and all authorization-class events (cohort start, close marker,
   census freeze, activation) — recorded with operator identity.

**Fencing-token semantics (reviewer ruling C5 — every transition increments).**
There is ONE monotonic `status_version` and it IS the fencing token. It
increments on EVERY control-state transition — `inactive → validating`,
`validating → active`, `active → paused_measurement_failure` (INCLUDING
automatic supervisor pauses), `paused → validating` (resume), and
`active → inactive/closed`. Authorization governs whether a transition may
occur; it does not govern whether the version increments. Supervisor-written
automatic transitions therefore increment the token exactly like
operator-written ones; admission validity is always judged against the
current `status_version`, never against event class.

If the control journal itself is unavailable, the supervisor fails CLOSED for
admission (no admission proceeds without a validated token — see §12b) and
emits the §12b degradation page; it never queues admissions for later
retro-write.

### 12b. Admission critical section + fencing token (the enforceable mechanism)

"Two-sided definitional fence" (§3a) states WHAT is admissible; this section
names the mechanism that ENFORCES it across process and crash boundaries. A
separate control DB plus a separate measurement DB is not automatically atomic,
so admission is made atomic in ONE store:

- **Authoritative admission ledger inside `control_plane.db`** (the selected
  mechanism, per the reviewer's option list). Every cohort admission is a
  single SQLite transaction ON `control_plane.db` (`BEGIN IMMEDIATE` on the
  dedicated connection — SQLite's file lock IS the OS-level cross-process
  mutex) that: (1) reads the current control record — requires
  `cohort_status = active` and captures the current **fencing token** (=
  `status_version`, the single monotonic counter of §12a that increments on
  EVERY transition); (2) validates the admitting writer's held token equals
  the current one (compare-and-commit against `status_version`); (3) inserts
  the admission-ledger row carrying that token.
  Stale-token admission fails the same transaction — compare-and-commit, not
  compare-then-commit. A crashed process that restarts with a retired token is
  rejected on its first admission attempt, because token state and admission
  live in the same transactional store.
- **Measurement-store mirror**: the assignment mirror row is written to the
  MEASUREMENT store (`measurement.db` for the Forward Cohort, per the
  canonical §7.4 matrix — NOT production scout.db), after the ledger commit,
  carrying the ledger row id + token. Mirrors are advisory until reconciled:
  any measurement-store cohort row without a matching admission-ledger row
  (or carrying a retired token) is excluded by the index query and surfaced
  as a reconciliation failure — it can never silently join the cohort.

**Explicitly rejected alternatives:** (a) control-plane rows as another scout.db
table — rejected: shares the failure domain the journal must referee; (b)
plain-JSONL control-plane file without hash chaining — rejected: silent
edit/truncation undetectable; (c) per-caller "remember to take the lock"
convention without a structural manager — rejected: that convention is exactly
what failed (chain tracker/DEX writers predate it and bypassed it); (d)
operator-only control-plane writes — rejected by reviewer ruling: automatic
transitions and fenced admission require a runtime writer (§12a); (e) advisory
cross-DB "atomicity" by ordering writes across control_plane.db and scout.db —
rejected: not crash-safe; replaced by the single-store admission ledger +
mirror-reconciliation above.

**Hold discipline.** This section records the SELECTION only (I1). Implementing
the control-plane journal and migrating writers onto the disciplined manager are
I2/I3 work, and cohort activation is I4 — all held until the P0 corrective PR is
merged, deployed, and verified clean.
