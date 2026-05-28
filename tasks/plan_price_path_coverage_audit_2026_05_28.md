**New primitives introduced:** `scripts/audit_price_path_coverage.py` read-only diagnostic; `tasks/findings_price_path_coverage_audit_2026_05_28.md` results doc (placeholder + follow-up snapshot commit).

# Price-Path Coverage Audit Plan

**Goal:** Read-only diagnostic. For the rows the trader sees in Today's Focus (last 36h window), measure intraday price-point density per row. Gates PR-C (sparkline inline visualization).

Pattern mirrors PR #310 (liquidity audit). Lessons applied without re-litigation:
- Consume live `/api/todays_focus` for the cohort (cohort matches trader view; no re-derivation from raw tables)
- Report joinable-vs-unjoinable to the price-path source as first-class fields (same key-space discipline)
- `PRAGMA table_info` at runtime for schema findings (no hard-coded booleans)
- Read-only DB connection via `sqlite3.connect(f'file:{db}?mode=ro', uri=True)`
- UTC `Z` timestamps; `coverage_rate = null` when denominator = 0
- No threshold comparisons in script (threshold logic lives in PR-C plan)
- No interpretive labels in output
- Findings doc commits with `<srilu_run_pending>` placeholder; live snapshot appended via follow-up commit on master

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Read-only DB diagnostic | No Hermes skill owns gecko-alpha's `volume_history_cg` schema or Today's Focus row contract. | Build in-repo (same pattern as PR #310). |

Awesome-hermes ecosystem check: no diagnostic plugin owns gecko-alpha's intraday data. Custom audit warranted.

## Drift / Runtime Findings (schema scan at `master @ 2ae0ef0a`)

### Source-of-truth scope (design review B1 fold)

This audit measures coverage of `volume_history_cg` ONLY, with the following named justification and explicit exclusions:

- **Why `volume_history_cg`**: this is the markets-watcher cadence source intended to feed PR-C's sparkline rendering. It holds `(coin_id, price, recorded_at)` triples appended by the CG `/coins/markets` poller (`scout/spikes/detector.py:18-58`). Keyed by CG slug, retention capped at 7 days by the writer's `DELETE FROM volume_history_cg WHERE datetime(recorded_at) < datetime('now', '-7 days')` prune (`scout/spikes/detector.py:55-57`).
- **Tables explicitly excluded from this audit's coverage count** (each holds price + a timestamp but PR-C is not scoped to consume them; if PR-C later decides to widen, a follow-up audit will measure them):
  - `gainers_snapshots.price_at_snapshot` + `snapshot_at`
  - `losers_snapshots.price_at_snapshot` + `snapshot_at`
  - `momentum_7d.current_price` + `detected_at`
  - `slow_burn_candidates.current_price` + `detected_at`
  - `volume_spikes.price` + `detected_at`
- **`price_cache`** (scout/db.py:854-861) holds only a single current snapshot per coin_id; not a history source. The `schema_findings` block reports `price_cache_has_history_table: false` as a measured fact via `PRAGMA table_info` (NOT hard-coded).

### Lookback-window retention ceiling (design review B-B1 fold)

`--lookback-hours` MUST be capped at `168` (7 days) to match the writer's prune. Beyond 7d the data does not exist and the audit would silently undercount. Default `24` is safely below the cap.

### Cutoff timestamp construction (design review B-B2 fold)

The writer at `scout/spikes/detector.py:26` uses `datetime.now(timezone.utc).isoformat()` which produces an ISO string with `+00:00` offset suffix. The audit MUST construct its cutoff the same way: `(now - timedelta(hours=lookback)).isoformat()` where `now = datetime.now(timezone.utc)` (the SAME `now` used for `audited_at`). String-compare against `recorded_at` then works correctly.

### Clock-source pinning (design review A-B2 fold)

`now` is captured ONCE at script start via `datetime.now(timezone.utc)`. Reused for `audited_at` AND the lookback cutoff predicate. NEVER use SQLite `datetime('now')` (server-local-tz, drift-prone).

### Structural predictions (to be confirmed by audit)

- Tracker-corpus rows (CG coin_ids) join directly to `volume_history_cg.coin_id` — coverage rate depends on watcher cadence.
- Paper-corpus rows may NOT join because `paper_trades.token_id` is sometimes a contract address (chain-side dispatch) — same key-space concern as PR #310. Audit reports `joinable_by_token_id` vs `unjoinable_or_zero_points` explicitly.

## Universe Pin

- **Cohort:** rows returned by `GET /api/todays_focus?window_hours=36` at audit run time.
- **Lookback window for price points:** `--lookback-hours` configurable; default 24; **MUST be ≤ 168** (7-day writer retention ceiling). Cutoff timestamp constructed once at script start via `datetime.now(timezone.utc) - timedelta(hours=lookback)`, then `.isoformat()` to match the writer's `+00:00`-suffixed ISO strings.
- **"Point counted":** a row in `volume_history_cg` with matching `coin_id` AND `recorded_at >= cutoff_iso` AND `price IS NOT NULL` AND `price > 0` AND `price < 1e308` (Infinity guard; defensive).
- **Boundary inclusivity:** `recorded_at >= cutoff_iso` is INCLUSIVE — a row at exactly the cutoff timestamp counts. Test fixture pins one row at the cutoff and asserts inclusion.
- **Join semantics:**
  - Tracker rows (`source_corpus == "tracker"`): direct `coin_id` match (CG slug to CG slug).
  - Paper rows (`source_corpus == "paper"`): try `volume_history_cg.coin_id = paper_row.token_id` (in case token_id is already a CG slug); if 0 points found, flag as `unjoinable_or_zero_points`. The audit does NOT pretend to do contract-address-to-coin_id resolution.
- **Density thresholds (FOR REPORTING, not for interpretation):** `points_per_row` is reported per-row, plus aggregate `points_distribution` (`min`, `p25`, `median`, `p75`, `max`) across cohort. The "≥N for ≥80% of rows" gate logic lives in PR-C's plan.
- **Distribution null-out for small N:** if `rows < 5`, emit `points_distribution: null` (statistics not meaningful at N<5). Per-row array still emitted so the operator can read raw points.
- **Empty cohort:** `rows == 0` → `points_distribution: null`, `join_rate: null`, `per_row: []`.
- **Timestamps:** all ISO 8601 with `Z` suffix.

## Scope

Build:

1. Add `scripts/audit_price_path_coverage.py` — same structural shape as `audit_liquidity_coverage.py`:
   - Args: `--url`, `--db`, `--window-hours` (Today's Focus), `--lookback-hours` (price points, default 24), `--timeout`, `--json`.
   - Fetches `/api/todays_focus`, classifies by `source_corpus`, queries `volume_history_cg` for point counts.
   - Emits JSON shape (pinned below) or human-readable table.
   - Exit codes: `0` audit ran; `2` fetch / DB error.
   - Read-only DB; no disk writes other than stdout.
2. Unit tests: synthetic `volume_history_cg` fixtures covering: empty cohort, tracker row with sufficient points, tracker row with zero points (unjoinable), paper row with zero points, multi-point row, NULL/0/negative price rows excluded from count, lookback window edge (one point just inside, one just outside, **one row exactly at cutoff timestamp — must be counted (inclusive boundary)**), tracker row with empty/null token_id (defensively classified as zero points without SQL crash), main() smoke, main() fetch-failure exit 2, `--lookback-hours 200` rejected with exit 2 (above 7d retention ceiling).
3. Findings doc `tasks/findings_price_path_coverage_audit_2026_05_28.md` with `<srilu_run_pending>` placeholder.
4. Backlog status flip: `BL-NEW-TODAYS-FOCUS-PRICE-PATH-COVERAGE-AUDIT` PROPOSED → SHIPPED with links.

Non-scope:
- No UI changes; no schema changes
- No backfill of missing intraday data
- No new tables; no migration
- No alert dispatch
- No threshold interpretation (`points_per_row: 4`, never `coverage: thin`)
- No reordering of `tasks/todo.md` beyond single status flip
- No mutations to scout.db (URI read-only enforces this)

## Output Shape (pinned)

```json
{
  "audited_at": "2026-05-28T22:30:00Z",
  "window_hours": 36,
  "lookback_hours": 24,
  "endpoint_url": "http://127.0.0.1:8000/api/todays_focus?window_hours=36",
  "total_rows": <int>,
  "paper_corpus": {
    "rows": <int>,
    "joinable_by_token_id": <int>,
    "unjoinable_or_zero_points": <int>,
    "join_rate": <float | null>,
    "points_distribution": null | {
      "min": <int>, "p25": <int>, "median": <int>, "p75": <int>, "max": <int>
    },
    "per_row": [
      { "token_id": "...", "symbol": "...", "points": <int> }
    ]
  },
  "tracker_corpus": {
    "rows": <int>,
    "rows_with_at_least_one_point": <int>,
    "rows_with_zero_points": <int>,
    "join_rate": <float | null>,
    "points_distribution": { ...same shape... },
    "per_row": [ ...same shape... ]
  },
  "schema_findings": {
    "volume_history_cg_has_price": <bool from PRAGMA>,
    "volume_history_cg_has_recorded_at": <bool from PRAGMA>,
    "price_cache_has_history_table": <bool from PRAGMA — check for separate price_cache_history table; false if absent>,
    "alternate_price_history_tables_present": {
      "gainers_snapshots": <bool>, "losers_snapshots": <bool>,
      "momentum_7d": <bool>, "slow_burn_candidates": <bool>,
      "volume_spikes": <bool>
    }
  }
}
```

Per-row arrays included so PR-C can scope against actual token coverage, not just aggregate stats.

## Verification

- Unit tests: 9 cases covering classification logic + edge cases (NULL/0/negative price excluded; lookback window boundary; tracker direct-match; paper joinable / unjoinable; multi-point; empty cohort; main smoke; fetch failure).
- Live run on srilu after deploy; snapshot appended to findings doc.

## Merge Gate

PR merges only when ALL three hold:
1. CI green.
2. Both PR reviewers (anti-scope, implementation/data-integrity) return zero findings OR only non-blocking findings.
3. Findings doc committed with `<srilu_run_pending>` placeholder.

Forward reference: threshold logic (e.g., "≥12 points per row for ≥80% of cohort" as PR-C green-light) lives in PR-C plan, not this audit.
