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

## srilu prod snapshot — pending

<srilu_run_pending>

(To be appended via follow-up commit on master after deploy. Run:
`python scripts/audit_liquidity_coverage.py --db /root/gecko-alpha/scout.db --url http://127.0.0.1:8000 --window-hours 36 --json`)

## Branch Decision Logic (lives in PR-B plan, not here)

The thresholds (e.g., 80% coverage required to ship paper-side liquidity column with `unavailable` fallback for residual gap; 0% tracker-corpus liquidity triggering backfill-first vs paper-only-with-explicit-unavailable-on-tracker) are PR-B planning decisions. This audit's deliverable is the raw measurement; interpretation belongs to PR-B.
