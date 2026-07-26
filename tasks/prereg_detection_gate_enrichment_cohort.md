# Pre-registration — Detection-lane gate-enrichment cohort

**New primitives introduced:** detection_decision_receipts table + DETECTION_GATE_VERSION constant

> Filename note: `prereg_*.md` matches none of the plan/design/spec gated
> patterns (`plan_*`, `design_*`, `spec_*`), so the new-primitives hook does not
> gate this file. The marker line above is included anyway for hygiene.

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

All receipts written in the range **`[2026-07-26T02:47:23Z, <fix-activation-ts>)`**
are **pre-cohort shakedown data**: they are RETAINED and labeled invalid **by this
documented timestamp-range exclusion**, NOT deleted or rewritten. No existing
receipt row is mutated. (If a marker is ever added it must be additive — e.g. a
cohort-registry row — never an `UPDATE` of receipt rows.) The 733 shakedown rows
present on srilu scout.db (02:49:25Z–03:12:41Z) are the measured basis for the
capacity artifact.

> **Cohort start (UTC): __________________ (TBD — the application-ready
> timestamp, recorded AFTER migrations complete, of the first healthy process
> activation following the two-identity fix deploy)**

Rows with `decided_at` in the shakedown range above (or otherwise before this
re-anchored timestamp) are excluded.

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

A null `source_observation_ts` renders as the empty string; the separator is a
literal `|`. Because the cycle instant is IN the key, cycle-N and cycle-N+1
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
standalone cron watchdog is intentionally not added for this table (§12a is
satisfied by the per-cycle summary + the fact that the lane itself is already
watchdogged and the writer only runs while `DETECTION_ALERT_LANE_ENABLED` is
true).

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
