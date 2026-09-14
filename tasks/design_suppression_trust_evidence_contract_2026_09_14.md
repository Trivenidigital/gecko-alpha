**New primitives introduced:** NONE (documentation only)

# DESIGN — Suppression trust evidence acceptance contract (docs-only), 2026-09-14

**Context:** The plan at `tasks/plan_suppression_trust_evidence_contract_2026_09_14.md` has been through both reviews, with folds applied (lines 76-78). This design defines the final contract that `tasks/findings_suppression_provenance_2026_09_14.md:82` requires before any suppression cost or ranking conclusion. The contract only decides whether evidence is eligible. It changes no application code, proposes no schema, authorizes no capture and makes no runtime claim.

## Hermes-first analysis
The skill hub (https://hermes-agent.nousresearch.com/docs/skills) was fetched today, but its dynamic catalog didn't render. The parent inspected the ecosystem list (https://github.com/0xNyk/awesome-hermes-agent) and found nothing equivalent to a repo-specific receipts contract. Neither check claims a skill is absent everywhere.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Evidence acceptance criteria for Gecko suppression receipts | No (hub catalog unavailable; ecosystem list inspected) | Write repo-specific prose |
| Historical event/price lineage for Gecko SQLite stores | No equivalent found | Use cited source and schema evidence only |
| Statistical estimand and sensitivity review | No equivalent found | Reviewer-judged criteria in the contract; no dependency |
| Workflow/review orchestration | Not needed | Existing plan → review → fold process and `.reviewers/<PR>.toml` |

## Evidence basis and freshness
- **Dated counts:** every count comes from the 2026-09-14T13:03:47Z findings snapshot and keeps that date. Re-reading them never makes them fresh.
- **Fresh metadata:** the 19:39:58Z preflight (`tasks/evidence_suppression_contract_schema_2026_09_14.md:10`) proves two things only: the column lists of 4 tables, and that 3 source hashes (`signals.py`, `outcome_ledger.py`, `suppression_health.py`) match `e0fad17f`. It does not prove rows, flags, rates, coverage, JSON contents, or that no external retained evidence exists.
- **Schema facts used:** there are no dedicated shared receipt-key or per-label selected-observation identity/time columns. This is not an exhaustive search of JSON or external receipts. `volume_history_cg` has both `recorded_at` and `created_at`. `price_cache` keeps only `updated_at`. None of the four tables has a chain, asset-contract or quote-currency column.
- **Source references:** use the corrected lines in the plan (lines 16-27). The contract re-checks each one against the PR SHA.

## Verdict vocabulary
- `ACCEPTED`: the evidence meets every requirement for the scoped cohort and window.
- `NOT_MET`: evidence exists and fails a requirement.
- `UNKNOWN`: the default. Use it whenever the evidence inventory is incomplete. It does not mean evidence is permanently impossible.
- `UNVERIFIABLE_HISTORICAL`: only for a named window where an inventory has documented irrecoverable loss of the specific evidence. Forward observations never upgrade it.
- **All six ACCEPTED** means the evidence may be considered for a separately scoped decision. It does not authorize removing `coverage=unknown`/`provenance=unverified` (`dashboard/suppression_health.py:265-267`), policy or UI changes, capture, activation or ranking.

## Cohort and identity namespace (applies to every dimension)
A claim must declare:
- the cohort: signal type, `kind`, `reason`, `source_layer`, combo and suppression reason;
- a half-open UTC window, plus the timestamp field that defines membership;
- the token namespace. Ledger `token_id`, decision `token_id`, `volume_history_cg.coin_id` and `price_cache.coin_id` are treated as different namespaces until mapping evidence shows they are the same asset.

Mapping to chain, contract address and quote currency must be shown, not assumed. first_signal passes `token.contract_address` as `token_id` (`signals.py:865`), while losers uses `coin_id` (`signals.py:726`).

## Six dimensions

**D1 — Attempt denominator and receipt coverage**
- *Requirement:* the denominator counts every producer attempt in scope, including attempts dropped by `LEDGER_SAMPLE_SUPPRESSED` (`signals.py:66-67`), the global ledger switch, swallowed exceptions (`signals.py:86-93`), and decision-emit failures. Evidence of the attempt must be independent of the receipt store being measured.
- *Counterexamples:* receipt count ÷ receipt count; 13,793 = 13,793 treated as coverage; ledger rows treated as attempts; a quiet producer read as disabled or as idle without separate activity evidence.

**D2 — Event identity reconciliation**
- *Requirement:* an explicit one-to-one mapping from each attempt to its ledger receipt and, where the branch requests one, its decision receipt. Alternative mappings qualify only if uniqueness is shown independently, e.g. a retained key or a deterministic tuple proven collision-free for the cohort. The report must list unmatched, duplicate and many-to-one records.
- *Counterexamples:* joining on token plus nearest time; ignoring a JSON `combo_key` vs `signal_combo` mismatch; expecting decision rows for chain_completed or first_signal (`signals.py:861-874`, `1418-1431`).

**D3 — Emission price lineage**
- *Requirement:* identifies the actual price selected at emission: source store or provider, observation ID or equivalent, observed time and ingested time kept separate from `emitted_at`, and the asset, chain and quote currency.
- *Counterexamples:* `anchor_cache_age_seconds=0.0` produced by the default (`outcome_ledger.py:511-518`); a losers `price_at_snapshot` with no snapshot source time; a `price_cache` value that was later overwritten.

**D4 — Horizon price lineage and lateness**
- *Requirement:* identifies the price actually selected for each horizon (the store/row `_price_at_or_after` used at `outcome_ledger.py:639-677`), with observed and ingested times separate from `labeled_at`. Lateness limits are declared before any future study runs; this contract sets no values. Rows outside the limit are NOT_MET for that horizon; any exclusion remains visible in the denominator and passes D5 selection review before a claim.
- *Counterexamples:* inferring which source was used from today's preference order ("history first, so it must have been history") — this launders an unrecorded selection; a `volume_history_cg` row days late with no cap (`655-664`); a pruned or overwritten observation.

**D5 — Null and missing-price treatment**
- *Requirement:* a declared estimand, the missingness mechanism for each branch (`price=None` for first_signal and chain_completed), and a sensitivity analysis under stated assumptions. Separate immature outcomes (horizon not elapsed) from mature-but-missing outcomes, and report each denominator. Otherwise the dimension stays UNKNOWN or NOT_MET and the overall decision stays HOLD. Disclosing the excluded share is necessary but not sufficient.
- *Counterexamples:* NULL treated as a zero return; `complete` treated as a label with returns when only peak7d is set (`outcome_ledger.py:789-811`); priced-only returns reported as representative.

**D6 — Earliest-anchor selection bias**
- *Requirement:* declare the anchor rule, left censoring at the window edge, how the rule interacts with the priced/unpriced branch mix, and sensitivity to alternative anchors. Distinct-token counts are not summed across status groups.
- *Counterexamples:* the dated 51 of 1,530 earliest anchors with r7d reported as the cohort return; an anchor whose earlier emission fell before the window, treated as first.

## Future-evidence boundary
The contract may say what evidence would satisfy a dimension. That statement authorizes nothing. Any future implementation needs:
- its own plan and review cycle;
- fresh runtime verification;
- migration safety review if schema is involved;
- bounded resource and cadence gates (row/byte budget, timeout, cadence, retention cap, stop condition) set before any collection;
- a watchdog that can measurably tell apart "no demand", "disabled", "failed write" and "dead producer".

## Final artifact scope
1. `tasks/findings_suppression_trust_evidence_contract_2026_09_14.md`: vocabulary, namespace rule, D1-D6 requirements and counterexamples, freshness block, current verdict table (all `UNKNOWN`), reviewer dispositions, boundary.
2. One link line under "Evidence follow-through and remaining gates" in `tasks/current_closeout_queue_2026_09_14.md`, with no status change.
3. Todo tracking of the checklist state only.

## Validation (paper cases, no invented tests)
Walk the plan's cases A1-A9 through the contract text. Each must end in the stated verdict. Add three cases:
- **A10:** a namespace mismatch between contract address and `coin_id` → `NOT_MET`.
- **A11:** all six ACCEPTED used to justify removing the dashboard warning → rejected.
- **A12:** a fresh 19:39 hash match cited as row coverage → rejected.

Also confirm by hand that the diff is docs-only, every number carries its date, and every file:line reference resolves at the PR SHA.

## Checklist
- [x] Plan · [x] Plan reviews ×2 · [x] Plan folds
- [ ] Design · [ ] Design reviews ×2 · [ ] Design folds
- [ ] Contract build · [ ] PR · [ ] PR reviews ×2 (both finished) · [ ] PR folds and re-review on the new SHA

**Boundary:** no application changes, schema proposal, writers, activation, retention, soak, paid calls, UI or ranking. The verdict stays `UNKNOWN` until an evidence inventory shows otherwise.
