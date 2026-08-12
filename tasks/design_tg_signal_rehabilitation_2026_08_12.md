**New primitives introduced:** (1) `tg_act_shadow` append-only decision table + writer (targeted `ON CONFLICT` idempotency, `_txn_lock`-guarded) + §12a lag-vs-eligible-input watchdog with generation cutover; (2) `tg_act_shadow_generations` gate-version activation registry (one row per gate_version); (3) `evaluate_tg_actionability_shadow()` versioned counterfactual evaluator with code+config fingerprint binding; (4) `tg_caller_features()` read-only, as-of-decision-time feature extractor reconstructing from immutable/timestamped ledger observations (NO new caller storage); (5) `scripts/qualify_tg_lifecycle_priceability.py` read-only historical-replay qualification harness with right-censoring; (6) widened `signal_data` pass-through fields in the TG dispatcher (field additions, not a new mechanism).

# TG Signal Rehabilitation — Design Proposal v1.2 (2026-08-12)

v1.2 addresses the operator's 10-point review of v1.1 (6 blockers, 3 important, 1 minor):
observed_at-bounded as-of reconstruction from immutable observations (§B), channel-
independent corroboration key (§B), A+B joint activation (§Gates), watchdog generation
cutover (§A-observability), targeted ON CONFLICT (§A-persistence), code-bound gate
version (§A-version), pre-ratified PQ thresholds (§C), `pending_maturity` right-censoring
(§C), `_txn_lock` ownership (§A-ownership), Solana-verify scope separation (§C).
v1.1 incorporated the five pre-approval requirements. The A/B/C/D architecture is
unchanged and operator-accepted.

Operator directive: rehabilitate `tg_social` as an evidence-producing participant in ACT
and learning **without** using paper-trade re-enablement as the discovery mechanism for
whether the lane is safe to paper trade. The four current controls (dispatch quarantine,
`signal_params.enabled=0`, ACT hardcode `v1_block_tg_social_low_n`, alert/live flags)
are **policy layers, not defects**, and remain unchanged by everything in this document.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram channel listening/parsing | none found (hub Telegram skill is bot-API messaging, not MTProto listening) | reuse in-tree `listener.py`/`parser.py` (0 new LOC) |
| Token resolution + enrichment | none found | reuse in-tree `resolver.py`; forward already-resolved fields |
| Caller reputation / noise scoring | none found | reuse in-tree source-quality ledger (`scout/source_quality/ledger.py`) |
| Solana identity verification | **yes — `gecko-solana-verify`** (adopted 2026-08-01, SECONDARY_READ_ONLY ruling) | separate guarded secondary identity check in Stage C (see §C scope note) |
| Shadow policy evaluation / paper gating | none found | build in-tree (deterministic policy code) |

awesome-hermes-agent ecosystem check: no crypto-caller-reputation or paper-lifecycle
skill exists. Verdict: `extends-Hermes` — one applicable skill (solana verify), consumed
in its ruled role; all else is in-tree reuse plus small net-new policy code. Receipt:
`tasks/.hermes-check-receipts/tg-signal-rehabilitation-….json`.

## Evidence base (audited 2026-08-12, srilu prod + tree)

30d funnel: 242 messages → 137 with extractions → 143 `tg_social_signals`
(114 RESOLVED / 17 UNRESOLVED_TERMINAL / 12 UNRESOLVED_TRANSIENT) → alerts delivered →
0 paper trades. Journal (5.5d horizon): 20 dispatch attempts = 10 `tg_social_admission_blocked`
+ 10 `trade_skipped_quarantined` (100% of quarantine events are tg_social; ~2/day reach
the engine). Historical 24 trades: 14 × `closed_expired`/`entry_fallback` fabricated flat
closes; 4 × `closed_sl` averaging −44.9%; all `price_source='legacy'`; every ACT stamp is
`v1_block_tg_social_low_n`. Self-sealing problem: ACT sits behind the quarantine, so TG can
never accumulate the evidence that would justify unblocking it. Journal funnel counts are
attribution evidence only — they do not justify reviving the lane. Raw capture:
`.dep523_tg_audit.txt` / `.dep523_tg_audit2.txt` (session scratch).

## Stage A — TG quality/ACT shadow (no trading mutation)

For every signal that reaches `resolution_state=RESOLVED`, evaluate a **counterfactual,
versioned** TG actionability decision and persist it, while the lane stays quarantined,
`enabled=0`, `live_eligible=0`, `tg_alert_eligible=0`.

### Field inventory — upstream-only today vs reaching the shadow decision

| Field | Exists today | Reaches decision surface today | Stage A |
|---|---|---|---|
| `mcap` | `ResolvedToken.mcap` | yes (`mcap_at_sighting`, only field ACT reads) | kept |
| `price_usd` | `ResolvedToken` | entry_price only | forwarded into `signal_data` |
| `volume_24h_usd` | `ResolvedToken` | **dropped at `dispatcher.py:219`** | forwarded |
| `age_days` | `ResolvedToken` | **dropped** | forwarded |
| safety trio (`safety_pass`/`completed`/`skipped_no_ca`) | `ResolvedToken` | **dropped** (gate-consumed only) | forwarded |
| `liquidity_usd` | NOT on `ResolvedToken` | — | added **only if** extractable from the existing resolution response; **zero new API calls** is a Stage A hard constraint |
| caller/channel features | source ledger (computable) | **absent** | computed via `tg_caller_features()` as-of decision time (Stage B) |
| duplicate/corroboration state | ledger cluster machinery | **absent** | within-channel: `duplicate_rank_in_cluster` recomputed as-of; cross-channel: channel-independent key (§B) |
| resolution/priceability confidence | partial (`resolution_state`) | **absent** | `resolution_state` + identity shape (cg-id vs `dex:{chain}:{addr}`) |

### Decision contract

`evaluate_tg_actionability_shadow(signal, features) -> (actionable: bool, reason: str, gate_version: str)`
— deterministic, thresholds from `Settings` (no hardcodes), reasons enumerated so we can
later answer "how many rejections were weak-caller vs missing-liquidity vs unpriceable-identity":
`shadow_pass`, `shadow_block_missing_mcap`, `shadow_block_liquidity_unknown`,
`shadow_block_caller_insufficient_n`, `shadow_block_caller_quality`,
`shadow_block_duplicate_call`, `shadow_block_safety_not_passed`,
`shadow_block_identity_unpriceable_class`, `shadow_block_mcap_band`.

### Version binding (v1.2: binds code, not just declared labels)

The effective version is `gate_version = "tg-shadow-v1+" + config_fingerprint[:16]`
(16 visible hex chars; the **full** SHA-256 digest is persisted inside `features_json`).
`config_fingerprint` is SHA-256 over canonical JSON (sorted keys, floats serialized via
`repr`, no Decimal normalization — per the 2026-07 hash-collision lesson) of:

1. the feature schema (ordered field names + types);
2. every threshold value the evaluator reads;
3. `evaluator_semantic_version` (declared);
4. `caller_feature_semantic_version` (declared, owned by Stage B);
5. SHA-256 of the **decision-producing module sources**: the shadow-evaluator module
   file and the caller-feature-extractor module file, hashed at import time.

(5) makes the binding mechanical rather than memory-dependent: changing extraction
semantics without renaming fields or touching thresholds still changes the fingerprint.
Deliberately NOT the deployed repo SHA — unrelated commits must not split cohorts; only
the decision-producing code is bound. A cosmetic-only edit to those two modules does
split a cohort; that is accepted as the cheap side of the trade.

### Persistence + idempotency invariant (v1.2: targeted conflict handling)

New append-only table:

```sql
tg_act_shadow(
  id INTEGER PRIMARY KEY,
  signal_id INTEGER NOT NULL REFERENCES tg_social_signals(id),
  gate_version TEXT NOT NULL,
  actionable INTEGER NOT NULL,
  reason TEXT NOT NULL,
  features_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(signal_id, gate_version)
)
```

**Invariant: exactly one immutable decision per `(signal_id, gate_version)`.** The writer
uses `INSERT INTO … ON CONFLICT(signal_id, gate_version) DO NOTHING` — NOT blanket
`INSERT OR IGNORE` — and inspects that statement's `rowcount` (never connection-wide
`total_changes`, per the #523 lesson): `rowcount == 0` ⇒ expected replay collision,
logged `tg_shadow_duplicate_skip`; **every other integrity failure (NOT NULL, CHECK, FK)
propagates loudly.** Only the one known-benign conflict is suppressed. Rows are never
UPDATEd or DELETEd. Multiple rows per signal exist **only** across distinct
`gate_version` values — the intended mechanism for comparing rule iterations. Tests must
include the restart-replay case (process the same RESOLVED signal twice, assert rowcount
1 — assert existence AND count) and the loud-failure case (malformed row raises, is not
silently absorbed).

### DB transaction ownership (v1.2)

The shadow writer writes through the shared pipeline `Database._conn`; therefore **its
mutation and commit MUST occur while holding the existing `db._txn_lock`** — same
invariant as every other shared-connection writer, re-affirmed after the #523 marker
repair. Feature evaluation, feature extraction, and JSON serialization all complete
BEFORE lock acquisition; nothing long-running (and zero network — see below) happens
under the lock. A feature flag does not make a malformed writer harmless; the lock
discipline is a merge requirement, not an activation-time concern.

### Generation registry + observability (v1.2: cutover boundary)

New single-purpose registry:

```sql
tg_act_shadow_generations(
  gate_version TEXT PRIMARY KEY,
  activated_at TEXT NOT NULL
)
```

Written exactly once per gate_version: at writer startup, if the computed `gate_version`
has no row, insert `(gate_version, now)` (same `ON CONFLICT DO NOTHING` + rowcount
discipline). This is the **prospective-mode cutover**: the watchdog and the writer's
eligibility scan consider only signals whose RESOLVED `created_at` ≥ the current
gate_version's `activated_at`. First activation therefore starts from zero eligible rows,
and a threshold change (new gate_version) starts a new generation prospectively — **no
implicit obligation to backfill history, and no page storm at cutover.** Historical
backfill, if ever wanted, is a separate **named operation** with a finite frozen
signal-id cohort, run under an operator instruction, and the watchdog stays disarmed for
that generation until the backfill reconciles. A LEFT JOIN never decides this policy.

The §12a watchdog condition (activation-aware, generation-aware):

> alarm iff `TG_SHADOW_ENABLED` is true AND ≥1 `tg_social_signals` row exists with
> `resolution_state='RESOLVED'` AND `created_at ≥ activated_at(current gate_version)`
> AND `created_at` older than the lag threshold (proposed 60 min) AND no `tg_act_shadow`
> row for the current `gate_version`.

This distinguishes "no work arrived" (quiet Telegram day — no page) from "work arrived
and the shadow writer failed" (page, with the eligible-but-unshadowed count in the body).

### Ownership and failure behavior

Deterministic ownership: the shadow evaluator is plain thresholded code — no ML, no LLM.
Failure containment: shadow evaluation runs after alerting/persistence on the listener
side; any exception is caught, logged (`tg_shadow_eval_failed`, structured, with
signal_id), and **must not** affect alert delivery, signal persistence, or the (blocked)
dispatch path. A shadow failure is an evidence gap, not a pipeline fault — and the
watchdog above converts a persistent gap into a page.

Network/API cost: **zero** — every Stage A input is already resolved in-process or read
from local SQLite.

## Stage B — Caller quality + noise features (reuse the ledger; no new storage)

`tg_caller_features(channel_handle, as_of: datetime) -> features` computes, read-only,
the ledger's quality axes: eligible distinct clusters (min-N denominator — NOT raw
message count), duplicate rate/rank, both coverage denominators (`coverage_rate` AND
`priceable_coverage_rate`, per #482), forward-return distribution
(`forward_{30m,1h,6h,24h}_pct`, `max_favorable/adverse_pct_24h`), structurally-
unpriceable proportion, cross-channel corroboration, and a trailing-30d vs lifetime
recency split.

### Reconstruction source (v1.2 — immutable observations, not current aggregates)

`compute_source_quality_summary()` reads **current** derived fields, and the ledger is
mutable by design: outcome fields refresh later, `resolved_state` updates,
`duplicate_rank_in_cluster` is recomputed. Therefore Stage B **reuses the ledger's axes
but NOT its current aggregate output**: every feature is reconstructed from
immutable/timestamped observations — `source_calls` insert-time facts (`call_ts`,
`observed_at`, identity, channel) and timestamped `source_call_price_snapshots` rows —
restricted to what was provably recorded by `as_of`. Mutable derived columns
(`resolved_state`, `duplicate_rank_in_cluster`, refreshed outcome fields) are never
consumed as if they existed historically; where a derived value is needed (duplicate
rank, cluster membership, forward returns) it is **re-derived as-of** from the immutable
rows.

### As-of knowability (v1.2 — double time bound)

A historical input row contributes to features at decision time `as_of` only if:

> `call_ts ≤ as_of` **AND** `observed_at ≤ as_of`

— a call posted before `T` but ingested after `T` was not knowable at `T` and is
excluded. On top of knowability, **outcome maturity** applies per feature, using the
ledger's own window bounds (`forward_24h_pct` closes at call_ts + 28h, `forward_6h_pct`
at +7h, `forward_1h_pct` at +90m, `forward_30m_pct` at +45m):

> a prior call contributes a forward-return field **only if**
> `prior_call_ts + window_end ≤ as_of` AND the snapshot rows that price that window have
> `snapshot_at ≤ as_of`. A window still open at `as_of` is excluded from that field's
> numerator AND denominator — neither a win, a loss, nor a sample.

"Exclude pending/partial as of today" is explicitly NOT the rule — that leaks future
outcomes into historical decisions. **Determinism test (required):** recomputing
`tg_caller_features(channel, T)` at any later wall-clock time must produce byte-identical
`features_json` — guaranteed precisely because inputs are immutable timestamped rows;
this also legitimizes backfill (a backfilled decision sees exactly what a live decision
at `T` would have seen).

### Cross-channel corroboration key (v1.2 — channel-independent)

The existing `duplicate_cluster_key` embeds `source_id` (the channel) + day, so the same
token called by two channels gets two different keys **by construction** — correct for
within-channel duplicate rate, structurally unusable for corroboration. Corroboration is
therefore derived (read-only, no new table) from the **channel-independent canonical
`cluster_identity`** plus a frozen time-bucket rule (same UTC-day bucket as the ledger's
cluster convention), counting **distinct `source_id`s** whose calls satisfy the §B
knowability bound: `corroborating_channels(identity, bucket, as_of) = COUNT(DISTINCT
source_id WHERE call_ts ≤ as_of AND observed_at ≤ as_of)`.

**Min-N gate:** a channel below `TG_CALLER_MIN_ELIGIBLE_CLUSTERS` (proposed 10, counted
as-of decision time) yields `shadow_block_caller_insufficient_n`, mirroring the ledger's
`insufficient_sample` rank_status; the as-of analogue of `biased_low_coverage` maps to
`shadow_block_caller_quality`. New storage is explicitly NOT proposed; if the ledger
proves insufficient the gap goes back to the operator before any schema change.

## Stage C — Lifecycle-priceability gate ("PQ-GATE"; no trades required)

Snapshot-priceability (source-call outcome measurement via `source_call_price_snapshots`)
does NOT prove paper-lifecycle priceability. PQ-GATE requires evidence that the exact
identity shapes now emitted — `dex:solana:{addr}`, `dex:robinhood:{addr}`, CG slugs —
support the full chain:

`entry-price provenance → ongoing price refresh → exit evaluation → durable exit price/provenance`

### Historical observations only

The harness evaluates hypothetical entry/exit timestamps **exclusively against price
observations that were actually recorded at those historical times** — the
`source_call_price_snapshots` series (cron `*/15` since #480/#482) and any timestamped
price-history rows the trade evaluator's own paths persist. "The token can be priced
today" is NOT evidence that a trade opened 30 days ago was priceable at hour 47. Where
the historical record is insufficient to evaluate a required lifecycle tick, the result
for that lifecycle is **`indeterminate_unmeasurable`** — a first-class outcome, never
converted into simulated success and never dropped from the report.

### Outcome classes incl. right-censoring (v1.2)

Per-lifecycle outcomes:

- `measurable_market_exit` — full 168h lifecycle evaluable, legitimate market-provenance exit;
- `coverage_failed` — observations existed but ran dry before a legitimate exit (the
  honest analogue of the historical entry-fallback scenario);
- `indeterminate_unmeasurable` — matured, but record too sparse to judge;
- **`pending_maturity`** — the call's 168h lifecycle horizon has not yet closed
  (`call_ts + 168h > now`). NOT coverage_failed, NOT indeterminate — simply not mature.
  Displayed in every report against the frozen denominator, **excluded from the verdict
  computation entirely.**

The **frozen denominator** is the full trailing-30d RESOLVED cohort (n=114 at audit
time; frozen by signal-id list + count + hash at first harness run). **Verdict timing:**
the PQ verdict is computed only when every cohort member has matured — i.e. at
`freeze_time + 168h` at the latest (the youngest member is at most 0d old at freeze) —
so a recent-heavy cohort can never fail the indeterminate ceiling merely because time
has not elapsed. Interim reports before that date show all four classes and carry no
verdict authority. Results are stratified by identity class — `dex:solana:*`,
`dex:robinhood:*`, true CG ids — **reported separately, never aggregated**, so Solana
coverage cannot mask an unsupported Robinhood path.

### Structural invariant vs coverage threshold

Two different things, never merged:

1. **Structural provenance invariant (non-negotiable correctness, not a gate number):**
   neither the harness nor any future paper path may manufacture a flat exit. If no
   valid market-provenance price exists at a hypothetical exit, the lifecycle result
   stays `coverage_failed`/unresolved. `entry_fallback` is not an acceptable evidence
   outcome anywhere in this program. Enforced in harness code and asserted in its tests
   independent of any threshold.
2. **Statistical coverage gate — RATIFIED 2026-08-12, frozen before any harness run
   (v1.2):** per identity stratum, over matured determinate lifecycles:
   `measurable_market_exit / (measurable_market_exit + coverage_failed) ≥ 95%`, AND
   `indeterminate_unmeasurable / (matured cohort members in stratum) ≤ 20%` — above the
   ceiling the stratum is `unratable`, not passed. These numbers are **fixed now**, per
   the operator's pre-ratification (conditional on the `pending_maturity` correction,
   which this section implements); the first harness run is evidentiary under them.
   There is no post-hoc threshold selection: no threshold may change after any outcome
   report exists, except by a new operator ruling that also requires a fresh
   non-overlapping cohort. A stratum that fails or is unratable is named and excluded
   from any future pilot cohort rather than averaged away.

### Solana secondary verification — scope (v1.2)

Two separated concerns:

- **PQ outcome calculation** = historical local evidence ONLY (snapshots + persisted
  price history). Zero external calls; no live lookup ever contributes to an outcome
  class.
- **Identity verification** = `gecko-solana-verify` (SECONDARY_READ_ONLY per the
  2026-08-01 ruling) runs as a separate, guarded, read-only check that `dex:solana:*`
  addresses are real mints matching their claimed metadata. It **does perform network
  RPC reads** (Solana public read-only endpoints) — disclosed here as the one network
  activity in Stage C — is rate-limited, never substitutes for historical price
  evidence, and its results are reported in a separate section of the PQ report
  (identity-verified / identity-mismatch / identity-unverifiable), never merged into
  the outcome classes.

Method notes: read-only; no paper trades; no new API quota for outcome calculation;
simulates the tg_social ladder geometry over a 168h lifecycle at the evaluator's own
tick cadence.

## Stage D — Bounded paper pilot (FUTURE; not in this proposal's build scope)

Only after A+B have accumulated shadow decisions and C has passed: a separate
pre-registered proposal (cohort definition from shadow_pass rows, stake cap, concurrent-
trade cap, N-cap, auto-halt on first `entry_fallback` close, kill switch, promotion and
rollback criteria) — with quarantine removal and `signal_params.enabled=1` as the
operator-approved flip, and `live_eligible`/`tg_alert_eligible` still 0. Nothing in
Stages A–C touches any of the four controls.

## Promotion / rollback gates (v1.2: A+B joint activation)

| Gate | Trigger | Action |
|---|---|---|
| A→merge | spec approval + PR review | shadow writer merged, deploy-dark (`TG_SHADOW_ENABLED=false`) |
| B→merge | PR review | feature extractor merged, still dark |
| **A+B→activate** | **both** PRs merged AND deployed AND deploy-verified; operator flips `.env` | shadow live with the full intended caller-feature set from decision #1 — no throwaway gate_version, no mid-cohort semantic change |
| Shadow health | eligible-but-unshadowed rows (current generation) older than 60 min, flag on | §12a watchdog page with count |
| Feature freeze | ≥30 shadow decisions under one gate_version | freeze that gate_version's thresholds for measurement |
| C→PQ-GATE verdict | all frozen-cohort members matured (≤ freeze+168h); report reviewed per-stratum | pass → Stage D proposal permitted; fail/unratable → upstream price-coverage work, lane stays closed |
| Rollback (any stage) | shadow eval errors >5% of RESOLVED, or watchdog page unresolved 7d | `TG_SHADOW_ENABLED=false`; table retained |

Stage A activation does NOT precede Stage B: the caller-dependent reasons
(`shadow_block_caller_*`, `shadow_block_duplicate_call`) require `tg_caller_features()`,
so activating A alone would ship a semantically different rule under a version that
changes days later. Dark-merge order between the two PRs is free; **activation is joint.**

## Cost, sequencing, and approvals

Build sequence (each its own PR, tests-first): (1) Stage A pass-through + shadow
evaluator + tables (`tg_act_shadow`, `tg_act_shadow_generations`) + watchdog (~280 LOC +
tests incl. restart-replay idempotency, loud-failure, lock-discipline, generation-cutover
cases); (2) Stage B as-of feature extractor consumed by the evaluator (~200 LOC + tests
incl. the determinism/no-leakage test with late-`observed_at` fixtures and the
cross-channel corroboration key test); (3) Stage C harness + stratified report (~200 LOC
+ fixture tests incl. the structural-invariant and pending_maturity assertions). API
cost: zero new external calls in A/B; C's outcome calculation reads existing historical
snapshots only (the disclosed exception: `gecko-solana-verify` identity RPC reads,
reported separately). Approvals required: operator spec approval (this document), per-PR
merge approvals, and a separate future approval cycle for Stage D. PQ thresholds are
already ratified (§C) and require no further ratification step.
