# Liquidity Coverage Audit — Findings

**Audit date:** 2026-05-28 (script merged in PR-A-followup batch)
**Audit script:** `scripts/audit_liquidity_coverage.py`
**Plan:** `tasks/plan_liquidity_coverage_audit_2026_05_28.md`

## Purpose

Gate the build PR for `BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS` (PR-B) with a measurement of:

1. Today's Focus cohort liquidity coverage (paper vs tracker corpus)
2. Joinable-vs-unjoinable rate for paper-corpus rows (since `paper_trades.token_id` vs `candidates.contract_address` is a non-trivial key-space alignment)
3. Schema-level confirmation of which tables in `scout.db` have a `liquidity_usd` column

## Audit Approach

The audit consumes the live `/api/todays_focus?window_hours=36` endpoint output — so the cohort matches exactly what the trader sees — and per-row attempts a `candidates` table lookup keyed on `contract_address` (exact match + case-insensitive fallback). Tracker-corpus rows skip the lookup entirely because no CG-coin_id-keyed table in `scout.db` has a liquidity column.

The script is read-only (DB opened via `file:{path}?mode=ro` URI), writes nothing to disk except stdout, and emits no interpretive labels — only factual counts and rates.

## srilu prod snapshot — 2026-05-28T22:10:53Z

Live audit executed on srilu at master `6f454936` (audit-script merge commit). Cohort: 5 rows from the live `/api/todays_focus?window_hours=36` endpoint.

```json
{
  "audited_at": "2026-05-28T22:10:53Z",
  "window_hours": 36,
  "endpoint_url": "http://127.0.0.1:8000/api/todays_focus?window_hours=36",
  "total_rows": 5,
  "paper_corpus": {
    "rows": 3,
    "joinable_to_candidates": 3,
    "unjoinable_to_candidates": 0,
    "join_rate": 1.0,
    "rows_with_valid_liquidity": 0,
    "coverage_rate": 0.0,
    "by_chain": {
      "coingecko": {
        "rows": 3,
        "joinable": 3,
        "with_liquidity": 0,
        "coverage_rate": 0.0
      }
    }
  },
  "tracker_corpus": {
    "rows": 2,
    "rows_with_liquidity_source": 0,
    "structural_note": "No CG-coin_id-keyed table has a liquidity column; tracker liquidity is a backfill gap."
  },
  "schema_findings": {
    "candidates_has_liquidity_usd": true,
    "gainers_comparisons_has_liquidity": false,
    "price_cache_has_liquidity": false,
    "volume_history_cg_has_liquidity": false,
    "trending_comparisons_has_liquidity": false
  }
}
```

## Factual observations from this snapshot

These are the raw measurements. Interpretation and threshold-driven decisions live in the PR-B plan.

1. **Paper-corpus join_rate = 1.0** — All 3 paper rows in the current cohort matched a `candidates.contract_address` row. The design-review concern about a `paper_trades.token_id` vs `candidates.contract_address` key-space mismatch did NOT manifest in this snapshot. The first-class joinable/unjoinable reporting remains valuable for future cohorts where the mismatch may surface.

2. **Paper-corpus coverage_rate = 0.0** — Of the 3 joinable paper rows, ZERO have `candidates.liquidity_usd > 0`. The `candidates.liquidity_usd` column is declared with `DEFAULT 0` (scout/db.py:528), and the current snapshot shows the field is empty/zero for the joined rows.

3. **`by_chain` bucket is `coingecko` for all 3 paper rows** — the `chain` field exposed by `/api/todays_focus` does not surface the underlying execution chain (Ethereum / Solana / Base) for these rows; it returns `coingecko` as a fallback. Per-chain breakdown of liquidity coverage is therefore not differentiable from the corpus aggregate in this snapshot.

4. **Tracker-corpus rows_with_liquidity_source = 0** (structural) — matches the schema prediction. No CG-coin_id-keyed table in `scout.db` has a liquidity column.

5. **schema_findings confirm**: only `candidates.liquidity_usd` exists. The other 4 tables checked (`gainers_comparisons`, `price_cache`, `volume_history_cg`, `trending_comparisons`) have no liquidity column.

## Forward-reference for PR-B scoping

The branch-decision logic lives in PR-B's plan, but the snapshot supplies these factual inputs:

- The headline blocker is NOT a sub-80% coverage threshold; it is a **structural 0% coverage** caused by the `candidates.liquidity_usd` column being empty/zero in the pipeline path that populates the current paper cohort.
- A simple "ship paper-side liquidity column with `unavailable` fallback" UI build would render `Liquidity: unavailable` for 100% of paper rows in this snapshot. That defeats the trader-feedback friction the field was meant to address.
- PR-B's plan should therefore decide between: (a) file a separate backfill PR to populate `candidates.liquidity_usd` from a data source before PR-B's UI work; (b) wire an alternative liquidity source (DexScreener / CoinGecko `/coins/markets`) keyed appropriately; (c) defer the liquidity-on-row column entirely until coverage is non-trivial.
- The `chain` field returning `coingecko` for all paper rows in this snapshot is a separate observation: if PR-B wants per-chain liquidity context (e.g., "Ethereum: $50K"), it will additionally need the chain-resolution path that's currently coarse.

Re-run the audit after any data-source change to verify the new coverage rate before scoping UI on top of it.

## Branch Decision Logic (lives in PR-B plan, not here)

The thresholds (e.g., 80% coverage required to ship paper-side liquidity column with `unavailable` fallback for residual gap; 0% tracker-corpus liquidity triggering backfill-first vs paper-only-with-explicit-unavailable-on-tracker) are PR-B planning decisions. This audit's deliverable is the raw measurement; interpretation belongs to PR-B.
