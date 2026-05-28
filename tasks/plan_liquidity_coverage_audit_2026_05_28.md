**New primitives introduced:** `scripts/audit_liquidity_coverage.py` read-only diagnostic (consumes live `/api/todays_focus` + read-only DB lookups); `tasks/findings_liquidity_coverage_audit_2026_05_28.md` results doc (placeholder in this PR, snapshot appended via follow-up commit).

# Liquidity Coverage Audit Plan (v2 — design-review folded)

**Goal:** Read-only diagnostic. For the rows the trader actually sees in Today's Focus (last 36h window), measure liquidity_usd resolution rate per `source_corpus`. Output a findings doc that gates PR-B (`BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS`).

This PR ships NO UI / NO schema / NO build / NO operator-visible behavior. Pure measurement.

## Design-Review Folds (2 parallel plan reviewers)

Both reviewers found blockers that materially changed the plan:

- **Reviewer A B1 — findings-doc path ambiguity:** folded. Commit with `<srilu_run_pending>` placeholder; append snapshot via follow-up commit on master after deploy.
- **Reviewer A B2 — schema-findings hard-coded:** folded. Use `PRAGMA table_info(...)` at runtime; emit observed booleans.
- **Reviewer B B1 — key-space mismatch on paper_trades.token_id vs candidates.contract_address:** folded by pivoting to consume `/api/todays_focus` output directly. The audit no longer re-derives the cohort from raw tables; instead it consumes the live endpoint and per-row attempts liquidity lookup, reporting joinable/unjoinable counts as first-class fields.
- **Reviewer B B2 — cohort mismatch with `get_todays_focus`:** structurally avoided by the same pivot. Cohort is the live endpoint's output, by definition matching what the trader sees.
- Non-blocking refinements also folded: UTC `Z` timestamps, read-only `file:{db}?mode=ro` connection, `null` coverage_rate when denominator=0, expanded "verified-no-liquidity" list to include `volume_history_cg` and `trending_comparisons`, test fixtures enumerate NULL/0/negative/positive/multi-chain/empty, explicit no-writes statement, `coingecko_only_no_venue` documented as self-check.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Read-only DB coverage audit | No Hermes skill owns gecko-alpha's `candidates`/`gainers_comparisons` schema or the Today's Focus endpoint contract. | Build in-repo. |
| HTTP+DB cross-source diagnostic | No reusable primitive. | Small Python script using `urllib.request` + `sqlite3` in read-only mode. |

Awesome-hermes ecosystem check: no diagnostic plugin owns gecko-alpha's data sources or its Today's Focus row contract. Custom repo-local audit warranted.

## Drift / Runtime Findings

Schema state on `master @ f3d8d04`:

- **`candidates`** (scout/db.py:520-547) has `liquidity_usd REAL DEFAULT 0`. Primary key: `contract_address`.
- **`paper_trades.token_id`** is `TEXT` (no FK). Semantically it's whatever the dispatcher wrote — sometimes a contract address (chain detectors), sometimes a CoinGecko coin_id (CG gainers/trending). The audit therefore cannot assume `paper_trades.token_id == candidates.contract_address` and must report joinable-vs-unjoinable explicitly.
- **`gainers_comparisons`** (scout/db.py:925-947) has NO liquidity column. Verified.
- **`price_cache`** (scout/db.py:854-861) has NO liquidity column. Verified.
- **`volume_history_cg`** (scout/db.py:863-873) has NO liquidity column. Verified.
- **`trending_comparisons`**, **`losers_snapshots`** — to be verified at runtime by `PRAGMA table_info`.

Structural prediction (to be confirmed): tracker-corpus liquidity coverage is **0%** because no CG-keyed table has a liquidity column. Paper-corpus joinable rate depends on how the dispatcher populated `paper_trades.token_id`.

## Universe Pin (revised)

- **Cohort:** the rows returned by `GET /api/todays_focus?window_hours=36` at audit run time. No re-derivation from raw tables; the live endpoint IS the source of truth for what the trader sees.
- **Window:** `36` hours, matching the operator's default. Configurable via `--window-hours`.
- **"Valid liquidity":** `candidates.liquidity_usd IS NOT NULL` AND `candidates.liquidity_usd > 0`. NULL, 0, or negative count as missing.
- **Join attempt (paper rows):** try `candidates.contract_address = paper_row.token_id` first (exact match). If no row found, try `LOWER(candidates.contract_address) = LOWER(paper_row.token_id)` (case-insensitive). Either lookup succeeding counts as joinable.
- **Tracker rows:** no candidates-table lookup. Report `tracker_rows_with_liquidity_source: 0` as a structural constant.
- **Timestamps:** all ISO 8601 with explicit `Z` UTC suffix.

## Scope

Build:

1. Add `scripts/audit_liquidity_coverage.py` — read-only Python script that:
   - Accepts `--url` (default `http://127.0.0.1:8000`), `--db` (default `scout.db`), `--window-hours` (default 36), `--timeout` (default 10s), `--json` flag.
   - Opens DB via `sqlite3.connect(f'file:{db}?mode=ro', uri=True)` — read-only, structurally cannot mutate.
   - Fetches `/api/todays_focus?window_hours={N}` via `urllib.request.urlopen`.
   - For each row in payload: classify by `source_corpus`; if paper, attempt 2-step join; capture liquidity status.
   - Queries schema via `PRAGMA table_info(candidates)` etc. — `schema_findings` block reports observed columns, not hard-coded.
   - Emits a fixed-shape JSON object to stdout (when `--json`) OR a human-readable summary (default).
   - Exit codes: `0` = audit ran successfully (regardless of coverage rate — script does NOT interpret coverage). `2` = DB unreadable / HTTP fail / JSON parse error.
   - Writes nothing to disk except stdout. No file modifications. No DB writes (enforced by read-only URI).
2. Add unit tests in `tests/test_audit_liquidity_coverage.py` covering:
   - Empty payload (rows=0 → coverage_rate is `null`, not `0.0`).
   - Paper row with joinable contract_address, valid liquidity.
   - Paper row with joinable contract_address, NULL liquidity → counted as missing.
   - Paper row with joinable contract_address, 0 liquidity → counted as missing.
   - Paper row with joinable contract_address, negative liquidity → counted as missing (defensive).
   - Paper row with unjoinable token_id → counted as unjoinable, NOT as "missing liquidity."
   - Tracker row → counted in tracker bucket, no candidates lookup attempted.
   - Multi-chain paper rows → `by_chain` breakdown correct.
   - Chain field empty string → bucketed separately (not joined into a real chain).
3. Add `tasks/findings_liquidity_coverage_audit_2026_05_28.md` with a placeholder block:
   ```
   ## srilu prod snapshot — pending
   <srilu_run_pending>
   ```
   To be appended via follow-up commit on master after deploy. NOT a separate PR.
4. Backlog status update in `tasks/todo.md`: single-line flip of `BL-NEW-TODAYS-FOCUS-LIQUIDITY-COVERAGE-AUDIT` from `PROPOSED` to `SHIPPED` with link to findings doc. No reordering of other backlog items.

Non-scope:
- No UI changes; no `/api/todays_focus` schema changes.
- No backfill of missing liquidity (that's PR-B's decision based on findings).
- No new tables; no migration.
- No alert dispatch; no Telegram.
- No interpretive labels in output (`coverage_rate: 0.72`, never `coverage: poor`).
- No threshold comparisons in the script (the 80% threshold lives in PR-B's plan, not here).
- No mutations of any kind to scout.db or any other state.
- No reordering or content changes in `tasks/todo.md` other than the single status flip.

## Output Shape (pinned)

JSON output:
```json
{
  "audited_at": "2026-05-28T20:00:00Z",
  "window_hours": 36,
  "endpoint_url": "http://127.0.0.1:8000/api/todays_focus?window_hours=36",
  "total_rows": <int>,
  "paper_corpus": {
    "rows": <int>,
    "joinable_to_candidates": <int>,
    "unjoinable_to_candidates": <int>,
    "join_rate": <float 0..1 | null>,
    "rows_with_valid_liquidity": <int>,
    "coverage_rate": <float 0..1 | null>,
    "by_chain": {
      "<chain or '<empty>'>": {
        "rows": <int>,
        "joinable": <int>,
        "with_liquidity": <int>,
        "coverage_rate": <float | null>
      }
    }
  },
  "tracker_corpus": {
    "rows": <int>,
    "rows_with_liquidity_source": 0,
    "structural_note": "No CG-coin_id-keyed table has a liquidity column; tracker liquidity is a backfill gap."
  },
  "schema_findings": {
    "candidates_has_liquidity_usd": <bool from PRAGMA>,
    "gainers_comparisons_has_liquidity": <bool from PRAGMA>,
    "price_cache_has_liquidity": <bool from PRAGMA>,
    "volume_history_cg_has_liquidity": <bool from PRAGMA>,
    "trending_comparisons_has_liquidity": <bool from PRAGMA>
  }
}
```

`coverage_rate` is `null` (not 0.0) when the denominator is 0 — distinguishes "no rows to measure" from "all rows missing."

Human-readable output is a fixed-width summary table with the same numbers.

## Verification

- Unit tests: synthetic DB fixture + monkeypatched `urlopen` returning a controlled payload; exercise the 9 test cases enumerated above.
- Live run against srilu prod after deploy → append JSON to findings doc via follow-up commit.

## Merge Gate

PR merges only when ALL three hold:
1. CI green.
2. Both PR reviewers (anti-scope, implementation/data-integrity) return zero findings OR only non-blocking findings.
3. Findings doc committed with the `<srilu_run_pending>` placeholder (snapshot appends in follow-up commit on master after deploy).

Post-deploy smoke:
1. SSH to srilu, run `python scripts/audit_liquidity_coverage.py --db /root/gecko-alpha/scout.db --url http://127.0.0.1:8000 --window-hours 36 --json`.
2. Append the JSON output to `tasks/findings_liquidity_coverage_audit_2026_05_28.md` under the `<srilu_run_pending>` line via follow-up commit.
3. Aggregate dashboard contracts unchanged (audit is read-only, no behavior change).

## Forward Reference

Branch-decision logic (e.g., 80% coverage threshold for PR-B paper-side ship vs backfill-first) lives in the PR-B plan when that PR is scoped. This audit's deliverable is the measurement only; interpretation belongs to PR-B.
