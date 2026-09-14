**New primitives introduced:** NONE (documentation only)

# PLAN — Suppression trust evidence contract (docs-only), 2026-09-14

**Context:** `tasks/findings_suppression_provenance_2026_09_14.md:82` says future cost or ranking conclusions stay on HOLD until an independently reviewed evidence contract exists. This plan turns that gate into acceptance criteria. It adds no writers, schema, activation, retention change, ranking or runtime work. Planning was read-only; no files were written.

## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Evidence contract | https://hermes-agent.nousresearch.com/docs/skills fetched; dynamic catalog unavailable | Repo-specific prose; no new dependency and no exhaustive absence claim |
| Ecosystem | https://github.com/0xNyk/awesome-hermes-agent inspected 2026-09-14 | General orchestration listings do not supply Gecko receipts; retain existing workflow |
| Runtime evidence | Existing in-tree audit and health primitives | Reuse; no package, service or collector added |

## Source references and drift (worktree vs. line numbers cited in findings)
Findings cite the `5c43526c` blobs. The current worktree shows these offsets, which the design must correct:
- `scout/trading/signals.py:60-93`: recorder docstring 60-65, then `LEDGER_SAMPLE_SUPPRESSED` early return at 66-67. It never raises (`except` at 86-93). Matches.
- `signals.py:714-735` → **713-735**: `_emit_dispatch_decision` starts at 713, ledger recorder at 722, `continue` at 735.
- `signals.py:850-872` → **850-874**: first_signal recorder with `price=None` at 861-873, `continue` at 874.
- `signals.py:1408-1431`: chain_completed recorder at 1418-1430, `continue` at 1431. Matches.
- `signals.py:103-132`: `_emit_dispatch_decision` writes `source_module="scout.trading.signals"` and passes no ledger row ID.
- `scout/outcome_ledger.py:511-518`: a supplied price with no explicit age gets `anchor_age = 0.0`. Matches.
- `outcome_ledger.py:639-678` → **639-677**: `_price_at_or_after` returns a bare float. No cap on history lateness (655-664); cache lateness bound at 675.
- `outcome_ledger.py:683-694` → **680-693**: `_peak_price_in_window` works without an anchor.
- `outcome_ledger.py:795-808` → **789-811**: finalize at 789, peak7d at 790-795, `any_label` at 797-801, `complete` at 805-808. Returns need an anchor (781-782).
- `outcome_ledger.py:747-818` → **746-819+**: the labeler's select/update. It persists no observation identity.
- `dashboard/suppression_health.py:262` → **262-267**: `retained_rows_only` at 262; `coverage="unknown"`, `provenance="unverified"` and `conclusions=insufficient…` at 265-267.
- `scout/db.py:2126-2139` (`trade_decision_events`) and `db.py:10001-10036` (`signal_outcome_ledger`): separate autoincrement IDs, no shared event key. Combo is `signal_combo` in one table and `gate_verdicts` JSON in the other. Timestamps are `created_at` vs `emitted_at`.

## Artifacts
1. `tasks/plan_suppression_trust_evidence_contract_2026_09_14.md`: this plan, with the primitives marker.
2. `tasks/design_suppression_trust_evidence_contract_2026_09_14.md`: dimensions, verdict vocabulary, adversarial cases, drift table.
3. `tasks/findings_suppression_trust_evidence_contract_2026_09_14.md`: the final acceptance contract, reviewer dispositions and boundary statement.
4. One line in `tasks/current_closeout_queue_2026_09_14.md` linking the contract under "remaining gates". It must not change any status.

## Contract design
- **Verdicts per dimension:** `ACCEPTED`, `NOT_MET`, `UNKNOWN`, `UNVERIFIABLE_HISTORICAL`. Any dimension that isn't `ACCEPTED` keeps the dashboard at `coverage=unknown` / `provenance=unverified`.
- **Dimensions:** D1 producer attempt denominator and receipt coverage; D2 event identity reconciliation; D3 emission price lineage; D4 horizon price lineage and lateness; D5 null/missing-price handling; D6 earliest-anchor bias.
- **Historical rule:** stored data has no attempt counter, shared event key or observation ID. D1-D4 cannot be established from these inspected stores alone. Use `UNKNOWN` until the required evidence inventory is complete; use `UNVERIFIABLE_HISTORICAL` only for a specifically scoped window with documented irrecoverable evidence loss. Independently retained receipts may be evaluated, but new forward observations never repair old lineage.
- **Forward rule:** the contract may list what future evidence would satisfy a dimension. That list does not authorize capture, writers, schema, flags or soak. Any later implementation needs its own plan and must set bounded resource and cadence gates before collecting data: row/byte budget, query timeout, sampling cadence, retention cap, stop condition.

## Adversarial acceptance examples (each must fail or be labeled correctly)
Numeric examples below are dated 2026-09-14T13:03:47Z audit observations, not current counts.
- **A1 attempt/receipt:** equal counts (13,793 = 13,793) → `NOT_MET`; counts are not a denominator. Seeing ledger rows while the recorder is fail-soft → `UNKNOWN`. first_signal's 10.1h gap → not proof a producer is disabled.
- **A2 wrong denominator:** a ledger/decision ratio for chain_completed or first_signal → rejected. Those branches never request decision writes (873/1430).
- **A3 reconciliation:** joining on token plus nearest timestamp → rejected. Duplicate token/combo inside one second is ambiguous; a combo mismatch between JSON and column counts as unreconciled. Require a demonstrably exact 1:1 mapping using a shared emission key or independently retained equivalent evidence; approximate matching cannot establish it.
- **A4 emission lineage:** `anchor_cache_age_seconds=0.0` → not freshness (511-518). Using the losers snapshot price without its source timestamp → `UNKNOWN`.
- **A5 horizon lineage:** an r7d from a `volume_history_cg` row days late → `NOT_MET` (no history lateness cap). An overwritten cache source or pruned observation stays `UNKNOWN` pending independent-receipt inventory; use `UNVERIFIABLE_HISTORICAL` only after scoped irrecoverable loss is documented.
- **A6 terminal labels:** 9,832 complete rows with 4,486 missing r7d; peak7d-only `complete` → not maturity.
- **A7 null/missingness:** dropping `price=None` rows before computing returns → rejected unless a declared estimand and reviewed missingness/selection treatment support the claim; reporting excluded share per signal is necessary but insufficient. Separate immature outcomes from mature-missing returns. Treating NULL as a zero return → rejected.
- **A8 earliest anchor:** the cohort picks each token's earliest in-window anchor (51 of 1,530 with r7d). The contract must report left censoring at the window edge, anchor-selection skew toward priced branches, and not sum counts across status groups.
- **A9 freshness laundering:** a runtime check today reproduces the dated counts → the counts keep their 13:03:47Z date.

## Assumptions the parent verifies before any runtime decision
- Current schema and the source lines above match production HEAD. The parent checks only schema and source, not counts.
- Active flags, retention overrides, prune receipts and heartbeats remain unknown until checked. The contract doesn't depend on them.
- There is no ledger pruner and no shared key. This comes from a source search and is not exhaustive.

## Checklist
- [ ] Plan (this)
- [ ] Plan review 1 (evidence/statistics) · [ ] Plan review 2 (scope/authorization boundary)
- [ ] Fold plan findings
- [ ] Design doc
- [ ] Design review 1 · [ ] Design review 2
- [ ] Fold design findings
- [ ] Contract build (findings doc plus queue link)
- [ ] PR (docs-only; `.reviewers/<PR>.toml` records the reviewer SHAs)
- [ ] PR review 1 · [ ] PR review 2 (both must finish before merge)
- [ ] Fold PR findings; re-run affected reviewers on the new SHA

## Verification
- Docs-only diff: no `.py`, `.jsx`, schema or config changes. The primitives marker is present in the plan and design.
- Every file:line reference is re-checked against the PR SHA; every number carries its observation date.
- A grep of the final doc finds no ranking or capture authorization and no retroactive `ACCEPTED`.

**Boundary:** no implementation, writers, schema, activation, retention, soak, paid calls or ranking. Dated counts stay dated. Historical gaps stay `UNKNOWN` / `UNVERIFIABLE_HISTORICAL`.

## Plan review folds

2026-09-14: contract_evidence (evidence/statistics) and contract_scope (authorization/ops) completed independent reviews of e477f64f. Folded UNKNOWN until scoped evidence loss is proven; disclosure alone cannot cure missingness bias. Even all dimensions ACCEPTED does not authorize warning removal, ranking, capture or activation; those require separately scoped authorization. No application implementation is included.
