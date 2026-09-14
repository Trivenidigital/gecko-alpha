# Suppression trust evidence acceptance contract — 2026-09-14

This contract is documentation only. It sets what evidence would let suppression outcomes feed a future cost or ranking conclusion. It is the gate named in [the provenance findings' Next gate](findings_suppression_provenance_2026_09_14.md#next-gate). Following `tasks/design_suppression_trust_evidence_contract_2026_09_14.md`, it adds no code, schema, writers, capture, activation, deployment or policy. **Rollback:** revert the docs commit. Nothing is deployed.

## Evidence basis and freshness

- **Dated evidence:** retained-row observations come from the 2026-09-14T13:03:47Z read-only snapshot. They stay dated to that time. This contract repeats no counts, and re-reading them does not make them current. A new independently timestamped query is separate evidence, even if its values happen to match.
- **Fresh metadata:** the 2026-09-14T19:39:58Z preflight (`tasks/evidence_suppression_contract_schema_2026_09_14.md`) proves only the column lists of `signal_outcome_ledger`, `trade_decision_events`, `volume_history_cg` and `price_cache`. It also proves that the three source hashes (`scout/trading/signals.py`, `scout/outcome_ledger.py`, `dashboard/suppression_health.py`) match `e0fad17f`. It does not prove rows, flags, rates, activity, coverage, JSON payload contents, or the absence of external retained evidence.
- **Schema facts:** no dedicated shared receipt key column exists. No per-label selected-observation ID or source-time column exists. No chain, asset-contract or quote-currency column exists. `volume_history_cg` has `recorded_at` and `created_at`; `price_cache` keeps one overwritten `updated_at`. This is not an exhaustive search of JSON payloads or external stores.
- **Source facts, at the hash-matched revision:**
  - The suppressed-ledger recorder can skip on `LEDGER_SAMPLE_SUPPRESSED` and swallows exceptions (`signals.py:66-67`, `86-93`).
  - losers_contrarian writes a decision and then a ledger receipt (`signals.py:713-735`).
  - first_signal and chain_completed write only ledger receipts, with `price=None` (`signals.py:861-874`, `1418-1431`).
  - An emission price without an explicit age is stored as age `0.0` (`outcome_ledger.py:511-518`).
  - The horizon price selector returns a bare float (`outcome_ledger.py:639-677`).
  - A peak-only `complete` label is possible (`outcome_ledger.py:789-811`).

## Verdict vocabulary

| Verdict | Meaning |
|---|---|
| `ACCEPTED` | Evidence meets every requirement of the dimension for the declared cohort and window. |
| `NOT_MET` | Evidence exists and fails a requirement. |
| `UNKNOWN` | The default whenever the evidence inventory is incomplete. It does not mean evidence is permanently impossible. |
| `UNVERIFIABLE_HISTORICAL` | Only for a named window after an inventory documents irrecoverable loss of the specific evidence. New forward observations can never repair it. |

**Acceptance boundary:** if all six dimensions are `ACCEPTED`, the evidence becomes *eligible* for a separately scoped analysis. That does not authorize ranking, sizing, suppression policy or UI changes. It does not authorize removing `coverage="unknown"` / `provenance="unverified"` (`dashboard/suppression_health.py:265-267`), capture or activation. It also does not prove executable PnL: labels are observed-price returns, not fills, fees, slippage or liquidity-feasible exits.

## Cohort membership and identity (applies to every dimension)

Every claim must declare:
- the cohort predicate: signal type, ledger `kind`, `reason`, `source_layer`, combo and suppression reason;
- a half-open UTC window and the one timestamp field that defines membership;
- the identity namespace of each store.

Ledger `token_id`, decision `token_id`, `volume_history_cg.coin_id` and `price_cache.coin_id` are separate namespaces until mapping evidence shows they are the same asset. first_signal passes a contract address (`signals.py:865`); losers_contrarian passes a CoinGecko `coin_id` (`signals.py:726`). A price counts only when its chain, asset contract or listing ID, and quote currency are shown to match the emitted asset, not assumed.

**Timestamp validity (all dimensions):**
- Declare each timestamp's basis and timezone handling.
- Unparseable, non-UTC-normalizable or future timestamps, relative to the evidence snapshot time, are `NOT_MET` for the affected record.
- Excluded records stay visible in the denominators under D5.
- Aggregate SQLite checks do not validate every Python parser edge case.

## Six dimensions

**D1 — Attempt denominator and receipt coverage.**
- *Required:* an attempt record that can be reconstructed independently of the receipt store being measured. Declare logical event IDs separately from retry attempt IDs, and reconcile idempotent retries explicitly without inflating event counts. It must count successes, retries, and drops by lane flag, global ledger switch, swallowed exception or decision-emit failure. Logical-event coverage is distinct logical events with all branch-required receipts divided by distinct in-scope logical events expected to produce those receipts; never divide deduplicated receipts by retry attempts. Separately reconcile every attempt to success, idempotent retry, flag exclusion, drop or failure, with each class counted. Declare branch/flag inclusion before the study and retain excluded event counts; disabled periods cannot masquerade as fully observed coverage. A zero eligible-event denominator is undefined, not 100%.
- *Rejected:* receipt ÷ receipt ratios; equal ledger and decision aggregates treated as coverage; ledger rows treated as attempts; a quiet producer read as disabled or idle without separate activity evidence.

**D2 — Event identity reconciliation.**
- *Required:* an explicit one-to-one mapping from each successfully persisted logical event to its ledger receipt, with every attempt independently reconciled to success, idempotent retry, drop or failure and, where the branch requests one, its decision receipt. An alternative key, such as a retained external receipt or a deterministic tuple, qualifies only if it is shown independently to be collision-free for the cohort. Report unmatched, duplicate, retried and many-to-one records.
- *Rejected:* joining on token plus nearest timestamp; ignoring a mismatch between the JSON `combo_key` and `signal_combo`; expecting decision rows from ledger-only branches.

**D3 — Emission price lineage and age.**
- *Required:* the actual selected emission price, including:
  - source store or provider;
  - observation ID or equivalent;
  - observed time and ingested time, kept separate from `emitted_at`;
  - chain, asset and quote identity.
- Prove that the actual source version was available to the consuming path at emission selection, using trustworthy ingest/consumption/write chronology. An old observed timestamp ingested only after emission fails D3; it cannot repair the anchor retrospectively.
- A later study must predeclare a maximum emission observation age and its timestamp basis before looking at outcomes. Observations beyond that age are `NOT_MET`. An emission observation must not postdate emission; future/invalid observations fail even if they predate the evidence snapshot. This contract sets no numeric threshold and no live policy.
- *Rejected:* the default `anchor_cache_age_seconds=0.0`; a snapshot price with no source time; an overwritten `price_cache` value.

**D4 — Horizon price lineage and lateness.**
- *Required:* the actual observation selected for each horizon, with observed and ingested times separate from `labeled_at`, and asset identity as in D3. A later study must predeclare lateness limits per horizon before looking at outcomes. A late horizon is `NOT_MET` for that horizon and stays visible in D5 denominators. A selected horizon observation must be at or after the declared horizon deadline and no later than the evidence snapshot. This contract sets no values.
- Require trustworthy selection/consumption/write chronology proving that the source version was available when the labeler selected it. Ingest after the horizon deadline can be legitimate within the declared lateness policy, but ingest after the recorded selection cannot prove that selection; neither observed time nor presence today is sufficient.
- *Rejected:* inferring the selected source from today's preference order, which launders an unrecorded historical selection; a history row with no lateness bound; a pruned or overwritten observation.

**D5 — Maturity, missingness and selection.**
- *Required:*
  - a declared estimand;
  - separate denominators for all attempts, receipts, priced anchors, immature horizons (deadline not yet passed) and mature-but-missing horizons;
  - the missingness mechanism for each branch, including `price=None` branches;
  - a sensitivity analysis under stated assumptions.
- Disclosing the excluded share is necessary but not sufficient. Without estimand and sensitivity, the dimension stays `UNKNOWN` or `NOT_MET` and the decision stays HOLD.
- **Sample-size gate:** a later study must set its minimum usable sample from its estimand and precision target before it sees outcomes. If eligible mature records fall short, the result is HOLD. This contract sets no sample size or soak duration.
- *Rejected:* NULL treated as a zero return; peak-only `complete` treated as a return label; priced-only returns presented as representative.

**D6 — Earliest-anchor selection bias.**
- *Required:* a declared anchor rule, left-censoring treatment at the window edge, how the rule interacts with the priced/unpriced branch mix, and sensitivity to alternative anchor rules. Distinct-token counts are never summed across status groups.
- *Rejected:* earliest in-window anchors reported as the cohort return; an anchor treated as first when an earlier emission fell before the window.

## Current verdicts

| Dimension | Verdict | Concrete missing evidence | Smallest next engineering gate |
|---|---|---|---|
| D1 attempts/coverage | `UNKNOWN` | No attempt or drop record independent of receipts has been inventoried | Read-only receipt inventory: journals, logs, external stores |
| D2 identity | `UNKNOWN` | No dedicated shared key column; no alternative key shown to be collision-free | The same inventory, checking for a retained unique key |
| D3 emission lineage | `UNKNOWN` | No selected-observation ID, source time or quote identity per emission | Inventory of retained price observations and emission-side provider receipts |
| D4 horizon lineage | `UNKNOWN` | Selector persists only a float; pruning/overwrite history not inventoried | The same inventory, plus scoped irrecoverability findings |
| D5 missingness | `UNKNOWN` | No estimand, maturity split or sensitivity analysis | Analysis plan with predeclared estimand and sample gate |
| D6 anchor bias | `UNKNOWN` | Existing earliest-in-window rule is known; no study estimand, censoring treatment or sensitivity | The same analysis plan |

No dimension is `UNVERIFIABLE_HISTORICAL` yet, because no inventory has documented scoped irrecoverable loss.

## Paper validation (hypothetical counterexamples; not executed tests)

| Case | Hypothetical submission | Expected verdict |
|---|---|---|
| A1 | Equal ledger and decision aggregates cited as coverage | D1 `NOT_MET` |
| A2 | Ledger/decision ratio for first_signal or chain_completed | D1/D2 rejected; wrong denominator |
| A3 | Receipts joined on token plus nearest timestamp | D2 `NOT_MET` |
| A4 | Default zero anchor age cited as freshness | D3 `NOT_MET` |
| A5 | Horizon source inferred from current preference order | D4 `NOT_MET`; overwritten or pruned source stays `UNKNOWN` until inventoried |
| A6 | `complete` label status cited as return maturity | D5 `NOT_MET` |
| A7 | NULL-price rows dropped and share disclosed, with no estimand or sensitivity | D5 `UNKNOWN`; HOLD |
| A8 | Earliest in-window anchors reported as the cohort return | D6 `NOT_MET` |
| A9 | Dated 13:03:47Z observations copied and relabeled current | Rejected; the date stays |
| A10 | Contract address matched to a `coin_id` price with no mapping evidence | Identity `NOT_MET` |
| A11 | All six `ACCEPTED` used to remove the dashboard warning or rank | Rejected; eligibility only |
| A12 | 19:39:58Z hash match cited as row coverage | Rejected; metadata only |
| A13 | One event, success plus idempotent retry: receipt/attempt ratio is 1/2 | Logical-event coverage 1/1; two attempt outcomes separately reconciled |
| A14 | Old observation ingested after emission or after recorded horizon selection | D3/D4 NOT_MET for that asserted selection |

**Positive hypothetical, for illustration and not a claim about current data:** Suppose a future forward window for one signal has:
- an independent attempt log with drop classes, mapped by unique logical keys to all branch-required ledger and decision receipts, with retries reconciled and no unexplained unmatched records;
- emission and horizon observations carrying IDs, observed and ingested times, and chain, asset and quote identity, all within predeclared age and lateness limits;
- a declared estimand, maturity-split denominators and a sensitivity analysis;
- a met predeclared sample gate and a declared anchor rule with censoring treatment and passing alternative-anchor sensitivity.

That window could reach `ACCEPTED` on D1-D6, but only for that forward window. It would make the evidence eligible for a separately scoped analysis and nothing more.

## Next steps

1. **Operational minimum:** in a subsequent task within the standing read-only engineering authorization, write a bounded read-only plan to inventory independent receipts. It needs predeclared resource limits, no writes and no paid calls. It classifies each dimension as findable, `UNKNOWN` or scoped `UNVERIFIABLE_HISTORICAL`.
2. **If that inventory finds no usable receipts,** scope forward producer and price audit evidence separately. That work needs its own plan and design reviews, fresh runtime verification, and a resource budget (rows/bytes, timeout, cadence, retention cap, stop condition). It also needs a freshness SLO and a watchdog that measurably separates no demand, disabled, failed write and dead producer, plus migration-safety and activation gates before any collection.

Engineering planning for either step needs no operator approval. Activation, paid vendors and trading decisions keep their existing gates.

**Boundary:** no application, schema, writer, retention, activation, UI, policy, ranking or deployment change. All current verdicts are `UNKNOWN`.
