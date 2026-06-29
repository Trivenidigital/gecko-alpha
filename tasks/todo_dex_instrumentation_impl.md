# TODO — DEX-outcome instrumentation implementation (observe-only)

**Branch:** `feat/dex-outcome-instrumentation` (off origin/master)
**Spec:** `spec_dex_outcome_instrumentation_i1_i2_i3_2026_06_28.md` (PR #384, ACCEPTED)
**Verification:** local async suite is OpenSSL-blocked on Windows → **CI (`.github/workflows/test.yml`,
`uv run pytest`) is the runner.** Discipline: tests written first; nothing claimed "passing" until CI
is green. TDD red-green is observed via CI per pushed increment.

## Hard constraints (must hold at every commit)
- No gate recalibration / `MIN_SCORE` change · no scoring change · no threshold change.
- No paid Helius/Moralis. · No trading-alert behavior change · no new outbound trading alerts.
- Health/watchdog alerts → operator/health channel only. · Proxy data captured-not-scored.

## Acceptance bar (operator, 9 points) → component mapping
1. durable contract↔coin_id linkage → C2 (resolver) on C1 (`contract_coin_map`)
2. non-pruned earliest DEX-side entry mcap → C3 (`entry_mcap_snapshots`)
3. raw `txns_h1_buys` + ts + source → C4 (`txns_h1_buys_snapshots` + GT parse)
4. emit `dex_resolution_health` → C5
5. emit `dex_measurable_cohort_size` → C5
6. quality watchdogs (not just freshness) → C6
7. tests for fresh-but-empty failure modes → C6 tests
8. migration/backfill where safe → C1 migration + C2/C3 backfill seeds
9. prove no alert/scoring/gate change → C7 (guard tests + diff audit)

## Components (TDD-ordered; each = test-first → implement → CI-green → commit)

- [x] **C1 — schema + classifier.** DONE — 12 tests green locally (9e53f666). Add 3 tables to `_create_tables` (db.py:532) AND
  `_migrate_dex_instrumentation_v1` (BEGIN EXCLUSIVE template db.py:3510; schema_version row;
  post-commit assert); register in `initialize()` (db.py:81). Pure `classify_contract()` helper
  (CG-slug / evm / solana) in a no-aiohttp module so it unit-tests cleanly.
  Tests: tables exist, migration idempotent, classifier cases.
- [ ] **C2 — I1 resolver.** Reuse `fetch_coin_detail` (counter/detail.py:23) + `platforms`
  (minara_alert.py:185) → upsert `contract_coin_map`; ≤N/cycle budget (Settings); negative-result TTL;
  backfill seed from CG-native candidates. Tests (aioresponses): platforms parse, budget cap,
  best-effort never raises, backfill source tag.
- [x] **C3 — I2 writer.** DONE — `record_entry_mcap`, 5 tests green locally. `entry_mcap_snapshots` write-once earliest, DEX-mcap-preferred, hold-open on
  zero/placeholder; excluded from prune. Wire after `log_score` (main.py:1159). Tests: earliest wins,
  zero held open then filled, DEX preferred over CG-0, survives prune.
- [x] **C4 — I3 writer + GT parse.** DONE — `log_txns_snapshot` + GT h1 parse, 5 tests green locally (GT ingestion test = CI). `txns_h1_buys_snapshots` raw per-cycle capture + `source`; add GT
  `transactions.h1.buys/sellers` to `from_geckoterminal` (models.py:171); no-source → no row. Wire in
  the volume-snapshot loop (main.py:1093). Tests: raw capture + source, GT parse, no-row-when-missing.
- [x] **C5 — metrics.** DONE — `compute_dex_coverage_metrics`, 3 tests green locally. `dex_resolution_health` + `dex_measurable_cohort_size` query methods + rollup
  emit. Tests: health excludes never-listed; cohort-size counts fully-joinable only.
- [ ] **C6 — watchdogs.** Freshness (Tier-1) + data-quality (Tier-2: resolution-rate, non-zero mcap,
  non-null txns, coverage-trend, fresh-but-empty) in hourly maintenance (main.py:1357); add optional
  `TELEGRAM_HEALTH_CHAT_ID` routing (falls back to main chat; alerts `parse_mode=None` + dispatched/
  delivered logs). Tests: **fresh-but-empty fires**, freshness fires, routing uses health chat.
- [ ] **C7 — settings + no-regression proof.** New Settings (budget N, thresholds, retention, health
  chat). Guard test: scorer/gate output byte-identical pre/post for a fixture token; AST/grep guard
  that no new `send_telegram_message` callsite targets the trading path. Diff audit in PR body.
- [ ] **Final** — CI green on full suite; open **draft** PR with the 9-point acceptance mapping;
  collection-count guard (CI step) accounts for new tests.

## Review section (filled at end)
_(pending)_
