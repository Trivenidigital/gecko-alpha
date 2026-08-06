# Deterministic learner — evaluation report

**Date:** 2026-08-05
**Algorithm:** `deterministic-v1` (`scout/narrative/deterministic_learner.py`)
**Verdict:** `NO_CHANGE_WITHIN_VERIFIED_SEARCH_SPACE`
**Parameters changed by this evaluation:** none. **Lock states changed:** none.

This is a provenance record, not a proposal. The learner is proposal-only by
construction — it calls no `Strategy.set` — and this run produced no proposal to
consider.

---

## 1. Run identity

| Field | Value |
|---|---|
| Algorithm version | `deterministic-v1` |
| Qualifying population | **932** resolved predictions |
| Chronological split | **652** train / **280** validation (30% held out) |
| Snapshot SHA-256 | `509387d119bfd5d75238ebcc68d23138…` |
| Single-parameter candidates evaluated | **27** |
| Parameters searched | **3 of 14** |
| Parameters not identifiable from stored prediction columns | **11 of 14** |
| Provider calls | **0** — no network, no credential, no client constructed |

The split is chronological, not random: train is the earlier window, validation
the later one. A random split leaks future information into training through
overlapping market regimes, which is precisely how a parameter search convinces
itself it has found signal.

The snapshot hash is a content hash of the exact population the evaluation ran
against. It is meaningful only because the evaluation is deterministic — no RNG,
no wall-clock read, no set-iteration-order dependence, `Decimal` arithmetic
throughout. Run-varying metadata (`correlation_id`, `elapsed_ms`) is persisted in
a separate `runtime` subtree so the `evaluation` subtree stays byte-identical
across runs on one population.

---

## 2. What was searched — 3 of 14

`STRATEGY_BOUNDS` defines 14 tunable parameters. A parameter is searchable only
when **both** conditions hold: the learner can reconstruct its effect from stored
`predictions` columns, *and* it has a current value plus bounds.

| Parameter | Current value | Bounds | Reconstructed from |
|---|---|---|---|
| `counter_suppress_threshold` | 65 | (0, 100) | `counter_risk_score` |
| `laggard_max_mcap` | 200,000,000 | (5e7, 1e9) | `market_cap_at_prediction` |
| `min_trigger_count` | 1 | (1, 10) | `trigger_count` |

Each candidate grid is bounded twice: by `STRATEGY_BOUNDS`, and by
`MAX_CHANGE_FRACTION_PER_CYCLE = 0.25` of the bounded range. The clamp exists
because `laggard_min_volume` reaching its ceiling and staying pinned there for 72
days is the concrete failure a single noisy window can produce.

---

## 3. What was not searched — 11 of 14

These have no predicate over the stored prediction columns, so their effect
cannot be reconstructed offline. They are **excluded rather than guessed at**:

`category_accel_threshold` · `category_volume_growth_min` · `laggard_max_change` ·
`laggard_min_change` · `laggard_min_volume` · `hit_threshold_pct` ·
`miss_threshold_pct` · `max_picks_per_category` · `max_heating_per_cycle` ·
`signal_cooldown_hours` · `min_learn_sample`

Three distinct reasons, worth separating because they have different fixes:

- **Selection-time parameters** (`max_picks_per_category`, `max_heating_per_cycle`,
  `signal_cooldown_hours`) act on the *set* of predictions emitted, not on any
  individual row. Evaluating them offline needs a counterfactual over which rows
  would have existed — the stored table cannot answer that.
- **Label-defining parameters** (`hit_threshold_pct`, `miss_threshold_pct`) define
  `outcome_class` itself. Searching them against outcomes they define is circular.
- **Missing-column parameters** (`category_accel_threshold`,
  `category_volume_growth_min`, `laggard_*_change`, `laggard_min_volume`,
  `min_learn_sample`) would be searchable if the corresponding value were captured
  at prediction time. It is not.

**Named residual gap.** Two parameters have a working search predicate but no
`STRATEGY_BOUNDS` entry, so they were skipped as "no current value or bounds":
`narrative_fit_score_min` (current 60) and `laggard_min_mcap` (current 1,000,000).
These are searchable in principle and unsearchable in configuration. Adding bounds
would widen coverage from 3/14 to 5/16 with no algorithm change. Not done here —
it changes which parameters the learner may propose against, which is an owner
decision, not a side effect of an observability delivery.

Telemetry for the remaining 11 is explicitly **deferred**, not delivered.

**~~Named residual finding — two bounds tables have drifted.~~ CLOSED
2026-08-06.** `STRATEGY_BOUNDS` was defined twice —
`scout/narrative/strategy.py` (14 keys) and a hand-maintained copy inside
`dashboard/api.py` (13 keys). The API copy was missing
`counter_suppress_threshold`, so the operator-facing PUT endpoint applied **no
bounds validation** to that parameter, and the learner would then build its
candidate grid clamped around a possibly out-of-bounds current value.

Now one authoritative registry at `scout/narrative/strategy_bounds.py`, a
zero-dependency leaf module so the dashboard can consume it without importing
`scout.db`. Four consumers import it: `Strategy.set`, the deterministic
evaluator, the dashboard PUT endpoint, and a validation test that fails if a
second literal definition reappears anywhere under `scout/` or `dashboard/`.
Manual updates to `counter_suppress_threshold` are now bounds-checked to
`(0, 100)`.

---

## 4. Result

All 27 candidates were rejected. No candidate cleared the promotion gates:

| Gate | Threshold | Purpose |
|---|---|---|
| Held-out improvement | > 0.50 pp mean outcome | Noise-level differences cannot trigger a change |
| Train agreement | must also improve on train | Validation-only improvement is noise, not signal |
| Post-filter sample | train ≥ 30, validation ≥ 20 | A filter that keeps 4 rows proves nothing |
| Neighbourhood stability | surviving neighbour required, within tolerance | A single-point optimum is a spike, i.e. overfit |

The verdict is `NO_CHANGE_WITHIN_VERIFIED_SEARCH_SPACE` — since 2026-08-06 that
qualifier is the enum's actual value, not just prose in this report, and the
telemetry carries `searched_parameters: 3` and `unidentifiable_parameters: 11`
alongside it. The qualifier is load-bearing.
The learner searched 3 of 14 parameters. "No change" is a statement about those
three and about the 27 candidate values inside their clamped grids. It is **not**
evidence that the other 11 parameters are correctly set, and must not be cited as
such.

`NO_CHANGE` is a **success** outcome, not a skip: the learner ran, completed, and
concluded correctly. It maps to `DETERMINISTIC_NO_CHANGE` and moves
`last_daily_learn_success_at`. Treating a correct no-change as a non-success would
make a healthy learner look chronically broken — the inverse of the defect this
delivery exists to remove.

---

## 5. Outcome vocabulary

Daily and weekly cycles now report through one enum, `LearnCycleOutcome`. Every
outcome is reached by an explicit total mapping — never inferred from `None`, a
boolean, truthiness, or which log line appeared.

| Outcome | Meaning | Success clock |
|---|---|---|
| `DETERMINISTIC_NO_CHANGE` | evaluation completed, nothing should move | ✅ |
| `DETERMINISTIC_PROPOSAL` | a candidate survived every gate; owner review required | ✅ |
| `INSUFFICIENT_EVIDENCE` | not enough data, or no evaluable candidate grid | ❌ |
| `UNSTABLE_EVIDENCE` | a winner existed and the stability gates rejected it | ❌ |
| `FAILED` | the evaluation did not complete | ❌ |
| `OPTIONAL_COMMENTARY_DISABLED` | weekly commentary off by configuration | n/a |
| `OPTIONAL_COMMENTARY_SUCCESS` | weekly commentary ran (requires the flag on) | n/a |
| `OPTIONAL_COMMENTARY_FAILED` | weekly commentary failed; **not** a learner failure | n/a |

`INSUFFICIENT_EVIDENCE` and `UNSTABLE_EVIDENCE` were one conflated member. They
are opposite situations — "wait for more data" versus "the search found something
and the gates threw it out" — and conflated, an operator waiting for more data
would have waited forever. Worse, when candidates existed the stability-rejected
case fell through to `NO_CHANGE` with the note *"no candidate beat the current
configuration on held-out data"*, which was false.

`OPTIONAL_COMMENTARY_SUCCESS` is an addition beyond the seven specified members.
The weekly branch has a third reachable state — commentary enabled and
succeeding — and mapping it onto any of the seven would misreport it. It is
reachable only when `NARRATIVE_WEEKLY_COMMENTARY_ENABLED` is on, which is off by
default.

---

## 6. Failure telemetry

The runtime path returns a result on every path and never raises. A failure is a
`FAILED` proposal carrying: `outcome`, `exception_type`, `exception_message`,
`failing_step`, `algorithm_version`, `snapshot_hash`, `prediction_count`,
`elapsed_ms`, `correlation_id`, and a bounded traceback.

`failing_step` is assigned before each stage (`acquire_connection`,
`load_bounds`, `load_records`, `load_strategy`, `evaluate`, `persist_learn_log`)
rather than derived from the traceback, so the label survives redaction,
truncation, and any future logging change. A failed cycle still writes its
`learn_logs` row — otherwise the gap reads as a scheduler that never fired.

**Secret safety.** No credential, header, prompt, provider payload or wallet key
can enter: the deterministic learner handles none of them, the field set has no
key to put them in, and the two free-text fields are passed through `redact()` and
length-bounded regardless. `traceback.format_exception` renders frames and source
lines, never local variable values.

A real redaction defect was found and fixed while testing this. The header pattern
was `(authorization|x-api-key)\s*[:=]\s*\S+` — `\S+` stops at the first token, so
`Authorization: Bearer hunter2` redacted the word *Bearer* and logged the token.
That is the standard header shape and therefore the most likely one to appear. The
pattern now consumes to end-of-line, bounded so it cannot swallow the rest of a
multi-line traceback.

---

## 7. Anti-scope

Not done, and deliberately: no parameter value changed · no lock state changed ·
no observation-only revival infrastructure built · no paid model provider added ·
no real-money execution enabled · no order, swap, approval or transaction placed.

Deferred, explicitly not blockers: observation-only revival evidence collection ·
telemetry for the remaining 11 parameters · process-wide traceback redesign
(`structlog`'s JSONRenderer is configured without `format_exc_info`; this delivery
carries the traceback as an ordinary string field rather than reshaping every
module's output at once) · cross-module provider-health aggregation.
