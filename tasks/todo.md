# Backlog — gecko-alpha

Last updated: 2026-05-18 (overnight closures: BL-NEW-AUDIT-SURFACE-ADDENDUM + BL-NEW-POLYMARKET-VERIFY → AUDITED 2026-05-18)

## Active Work: Overnight closures 2026-05-18 — items 1 + 2

- [x] **Item 1: BL-NEW-AUDIT-SURFACE-ADDENDUM**: 5-category mini-sweep clean (nginx/caddy not-found, /etc/systemd/system.conf only [Manager], /etc/apt/sources.list.d/ minimal, docker/containerd not-found, systemd inventory matches cycle-6 captures). Status PROPOSED → AUDITED 2026-05-18. Findings: `tasks/findings_audit_surface_addendum_2026_05_18.md`.
- [x] **Item 2: BL-NEW-POLYMARKET-VERIFY**: `/opt/polymarket-ml-signal/` does NOT exist; stale cron entry confirmed (outside gecko-alpha managed block, silently failing every 6h). Status PROPOSED → AUDITED 2026-05-18. Findings: `tasks/findings_polymarket_verify_2026_05_18.md`. Operator-pastable removal command embedded.

## Active Work: BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT

- [x] Isolated worktree: `.claude/worktrees/feat+social-mentions-denominator-audit`
- [x] Drift-check: `git fetch origin && git log -10 origin/master` confirms HEAD=`a20891f` (zero divergence, includes merged PR #150). 19 files match `social_mentions_24h|SOCIAL_MENTIONS`: scorer.py:121 (live consumer), models.py, db.py, dashboard surfaces, 4 test files, 4 doc files. No drift — field is wired as documented in originating backlog entry L228
- [x] Hermes-first: Hermes skill hub WebFetch (category-exhaustive: Social Media 7 skills) returns no per-token mention-aggregation skills. awesome-hermes 404 consistent. Bridge not eligible (Hermes X 0/126 resolved; TG 6 distinct tokens/24h)
- [x] Runtime-state verification (per CLAUDE.md §9a): `social_mentions_24h = 0 across all 1,671 candidates`, max=0; full `score_history` (6,096,576 rows) max=58; gte_60=0; gte_70=0; paper dispatch bypasses CONVICTION (`signals.py:325 quant_score > 0`)
- [x] Plan v2 (post-2-reviewer fold): `tasks/plan_social_mentions_denominator_audit.md`
- [x] 2 parallel plan reviewers: empirical-rigor (BLOCK on MIN_SCORE=60-not-25 CRITICAL + paper-dispatch-bypasses-CONVICTION CRITICAL) + strategy/deferral-risk (APPROVE-WITH-FIXES, multiple IMPORTANT); ALL CRITICAL + IMPORTANT folded into v2
- [x] Design v1: `tasks/design_social_mentions_denominator_audit.md`
- [x] 2 parallel design reviewers: operator-UX (3 CRITICAL: TL;DR overload, uncommitted queries, wrong PR number) + risk/deferral-discipline (1 CRITICAL: operator-response no SLA + multiple IMPORTANT); ALL CRITICAL + IMPORTANT folded into findings doc + design v2 by inline
- [x] `tasks/audit_v2_queries.sql` shipped for operator re-evaluation (per design-review folds)
- [x] Findings doc shipped: `tasks/findings_social_mentions_denominator_audit_2026_05_17.md` (recommendation: Option B; deferred to operator approval)
- [x] One-line `# DEAD SIGNAL` annotation on `scorer.py:121` (zero behavior change; 69/69 scorer tests pass on srilu)
- [x] backlog.md status flip PROPOSED → AUDITED 2026-05-17 + 5 follow-up entries filed (BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG, BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION, BL-NEW-SOCIAL-DENOMINATOR-OPERATOR-PREFERENCE, BL-NEW-SOCIAL-DENOMINATOR-VARIANT-B-IMPL, BL-NEW-SOCIAL-DENOMINATOR-VARIANT-C-IMPL — last 2 PENDING-OPERATOR-DECISION per PR-review fold R3 #4)
- [x] todo.md Active Work entry (this section)
- [x] PR #152 created + 3 parallel PR-stage reviewers dispatched (statistical-defensibility + structural + strategy-deferral-risk); 1 CRITICAL + 10 IMPORTANT folded into commit `5894352`
- [x] Reviewer 1 post-merge-review fold (commit pending): awesome-hermes-agent stale-404 claim corrected (x-twitter-scraper exists; doesn't cover per-token aggregation); 0-flip claim downgraded to "closed-form approximation"; todo checkboxes + counts corrected
- [x] Post-merge bookkeeping: PR #152 squash-merged to master at `e174a3d` (2026-05-17T23:39:11Z) per Reviewer 1 signoff; backlog status stamped with merge SHA + date
- [ ] Operator response to Open Question 1 (B vs C): file as PR comment or follow-up commit; trigger next-cycle implementation (BL-NEW-SOCIAL-DENOMINATOR-VARIANT-{B,C}-IMPL pre-filed)

Review:
- The originating concern (15-point dead phantom in SCORER_MAX_RAW=208) is empirically confirmed across 6,096,576 historical scoring rows (max=58, never reaches MIN_SCORE=60)
- Variant B (recommended) has 0-flip blast radius — gate recalibration from 60/70 to 65/75 preserves current friction
- Variant C unlocks 35 historical candidates at MIN_SCORE — operator preference question for funnel-widening
- Variant D (Hermes/TG bridge) deferred per data-readiness gate (Hermes 0/126 resolved; TG 6/24h distinct tokens)
- Plan-stage reviewer #1 caught CRITICAL: I had MIN_SCORE wrong (60 not 25); all backtest numbers re-computed against correct gates
- Per-trade dispatch path (`signals.py:325`) bypasses CONVICTION entirely — reframed blast-radius analysis to MiroFish-alert path
- Per CLAUDE.md §10 heuristic-invocation: full Plan→2-reviewers→Design→2-reviewers chain justified because findings-doc-only audit's deferral has highest rot risk; cycle-9 calendar discipline applied to all 3 follow-ups
- Per CLAUDE.md §11b: Wilson UB applied to 0/126 resolved claim (2.91% one-sided UB; negligible)

## Active Work: BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING

- [x] Isolated worktree: `.claude/worktrees/feat+lc-revival-criteria-tightening`
- [x] Drift-check: `git fetch origin && git log -10 origin/master` confirms HEAD=`5860d17` (zero divergence); 15 files match adjacent primitives (revival_cooloff, autosuspend_fix, first_signal_retirement — all SHIPPED via PRs #79/#81/#147); ZERO files match new diagnostic surface (`no_breakout_and_loss|exit_machinery_contribution|wilson_lb|bootstrap_lb_per_trade|keep_on_provisional`)
- [x] Hermes-first check: Hermes skill hub returns no trading-signal-revival skills; awesome-hermes-agent 404 consistent across cycles 7/8/9; custom build justified
- [x] Plan v3 drafted with `**New primitives introduced:**` header per CLAUDE.md gate: `tasks/plan_lc_revival_criteria_tightening.md`
- [x] 2 parallel plan reviewers dispatched (statistical/methodology + structural/integration vectors); 5 CRITICAL + 5 IMPORTANT folded into v2 → v3
- [x] Design v1 drafted as companion: `tasks/design_lc_revival_criteria_tightening.md`
- [x] 2 parallel design reviewers dispatched (integration-choreography + strategy-safety vectors); 4 CRITICAL + 9 IMPORTANT folded
- [x] Task 0 empirical baseline derivation against srilu prod: `tasks/baselines_revival_criteria_2026_05_17.md` (chain_completed n=12, volume_spike n=36, narrative_prediction n=185; healthy max nb_loss=0.368, healthy min exit_machinery=0.756)
- [x] TDD build: 49 unit tests on srilu Python 3.12.3 + pytest 8.4.2 (was 48 + 1 added at PR-fold for naive-ISO tz normalization)
- [x] Adjacent regression: 506 tests pass; 3 pre-existing env-coupled failures unrelated
- [x] Findings doc: `tasks/findings_lc_revival_criteria_tightening_2026_05_17.md` — 4 prod signals evaluated. LC=STRATIFICATION_INFEASIBLE (cutover today, correct); gainers_early=FAIL (contradicting 2026-05-13 audit-id=24); chain_completed + volume_spike=BELOW_MIN_TRADES (correct refusal at low n)
- [x] PR #150 created: https://github.com/Trivenidigital/gecko-alpha/pull/150
- [x] 3 parallel PR reviewers dispatched (statistical/safety, code-structural, strategy/UX); 0 CRITICAL + 5 IMPORTANT + 7 MINOR; all MUST/SHOULD folded into commit `3d8bf02`
- [x] PR description updated with full reviewer fold history table
- [x] backlog.md status flip PROPOSED → PR-OPEN / PENDING-MERGE + 4 follow-up items filed (BL-NEW-REVIVAL-VERDICT-WATCHDOG, BL-NEW-REVIVAL-CRITERIA-QUARTERLY-RECALIBRATION, BL-NEW-EVALUATION-HISTORY-PERSISTENCE, BL-NEW-REVIVAL-CRITERIA-PER-SIGNAL-TUNING)
- [x] **Post-merge:** PR #150 squash-merged to master at `a20891f` (2026-05-17T21:48:57Z). backlog.md status flipped PR-OPEN / PENDING-MERGE → SHIPPED 2026-05-17 with merge SHA.

Review:
- Read-only evaluator ships without any production-runtime side-effects; revive_signal_with_baseline / auto_suspend / main.py / calibrate.py all untouched
- Originating-failure prevention test (n=55 LC on 2026-05-13 under new criteria → BELOW_MIN_TRADES, refuses to emit PASS): structural prevention confirmed
- gainers_early FAIL verdict produced concrete contradiction evidence for 2026-05-13 audit-id=24; operator decision deferred per scope ("do not change gainers_early behavior unless evidence clearly supports it")
- §11b bootstrap CI + Wilson LB are first-class primary gates; secondary diagnostic gates (no_breakout_and_loss, exit_machinery_contribution) are derived from healthy-signal baselines, not fit-to-instance
- §9c lever-vs-data-path memory pattern is now instance #6 (the 5/13 verdict attributed soak success to the mechanism; mechanism didn't break; the input regime feeding the mechanism changed)
- No live config flips this PR. `keep_on_provisional_until_<iso>` (30d default) embeds structural revocability; active watchdog enforcement deferred to follow-up

## Active Work: BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX

- [x] Isolated worktree created: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-chain-anchor-pipeline-fix` on `codex/chain-anchor-pipeline-fix`
- [x] Drift/runtime check started from `BL-NEW-CHAIN-COMPLETED-SILENCE-AUDIT`; confirmed prod still has no `active_chains` writes after 2026-05-11 and no `chain_matches` after 2026-05-11 narrative / 2026-05-04 memecoin
- [x] Runtime lever correction: all three prod `chain_patterns` rows are currently `is_active=0`, so `load_active_patterns()` returns empty and the tracker exits before matching anchors
- [x] Hermes-first check started: installed VPS skills show no chain-pattern lifecycle primitive; public Hermes bundled/optional skills provide blockchain query tools but not gecko-alpha DB pattern retirement/revival semantics
- [x] Draft plan with drift + Hermes-first analysis: `tasks/plan_bl_new_chain_anchor_pipeline_fix.md`
- [x] Run two parallel plan reviews and fold findings: preserved learned `alert_priority`, added pattern provenance to avoid reversing operator disables, narrowed watchdog to active-chain writer health, and added Hermes URLs
- [x] Draft design with test matrix: `tasks/design_bl_new_chain_anchor_pipeline_fix.md`
- [x] Run two parallel design reviews and fold findings: snapshot-gated legacy recovery, lifecycle preservation of operator/code disables, migration tests, condition-aware watchdog anchors, deploy kill-switch check, rollback SQL
- [x] Build with TDD: provenance migration, safe built-in reconciliation, protected lifecycle guard, empty-pattern tracker log, chain-anchor health checker, shell wrapper, and systemd timer
- [x] Fresh focused verification: `tests/test_chains_patterns.py tests/test_chains_learn.py tests/test_chains_tracker.py tests/test_chain_pattern_provenance_migration.py tests/test_chain_anchor_health_watchdog.py` -> 49 passed
- [x] Fresh wider chain verification: `tests/test_chains_events.py tests/test_chains_db.py tests/test_chains_patterns.py tests/test_chains_tracker.py tests/test_chains_integration.py tests/test_chains_learn.py tests/test_chain_outcomes_hydration.py tests/test_narrative_chain_coherence.py` -> 79 passed, 1 skipped
- [x] Full-suite verification after rebase + parse-mode harness line-drift fix: `2316 passed, 39 skipped, 12 warnings in 330.87s`
- [x] PR created: https://github.com/Trivenidigital/gecko-alpha/pull/146
- [x] Three parallel PR reviews dispatched; first batch timed out, replacement reviewers returned structural/deploy/observability findings
- [x] Fold PR-review findings: per-pattern watchdog freshness, read-only DB check + schema-pending state, concrete timer enable docs, chain alert `parse_mode=None`, non-built-in operator/code disable preservation
- [x] Review-fold verification: watchdog/lifecycle/chain-alert/parse-mode targeted suite -> 24 passed; broader chain suite -> 94 passed, 1 skipped
- [x] Final full-suite verification after PR-review fold: `2321 passed, 39 skipped, 12 warnings in 347.90s`
- [x] Post-#147/#148/#149 rebase verification: no delete entries in `git diff --name-status origin/master..HEAD`; targeted chain/systemd suite `56 passed, 14 skipped`; full suite `2321 passed, 53 skipped, 12 warnings in 314.77s`
- [x] Runtime pre-deploy snapshot verified on srilu: prod `chain_patterns` still exactly match the migration recovery gate (`full_conviction` 52/2 inactive, `narrative_momentum` 58/2 inactive, `volume_breakout` 70/3 inactive, all `updated_at='2026-05-17 01:24:59'`)

Review:
- Fixed the actual runtime lever, not only the original `_check_active_chains` hypothesis: protected built-in `chain_patterns` can no longer be lifecycle-retired into complete anchor starvation, and exact known prod legacy retirement state is recoverable without reversing unknown/operator-disabled rows.
- Added recurrence coverage with `scripts/check_chain_anchor_health.py`, `scripts/chain-anchor-health-watchdog.sh`, and hourly systemd units that alert only when active protected patterns are missing or anchor-eligible upstream events are present while `active_chains` is stale.
- Pushed back on the partial-snapshot reactivation suggestion: all-or-nothing exact prod snapshot recovery is intentional per design because broadening inference can reverse unknown operator intent. The watchdog/logs surface non-matching inactive states for manual decision.

## Active Work: baseline test failures after PR #136 review

- [x] Reproduced current red subset: 17 failures in BL-064 reload, BL-076 metadata, calibration dry-run, mcap heartbeat, narrative token-id, parse-mode hygiene, and signal revival tests
- [x] Root-cause clustered failures into test-harness drift vs production hygiene fixes
- [x] Plan drafted: `tasks/plan_fix_baseline_test_failures_2026_05_16.md`
- [x] Implement plan task-by-task
- [x] Verify original 17-test subset is green: `17 passed in 8.06s`
- [x] Run adjacent suites and full suite with redirected output: adjacent `113 passed in 25.25s`; full `2159 passed, 39 skipped, 12 warnings in 463.12s`
- [x] Document final verification results here

Review:
- Fixed env-coupled tests by routing BL-076 through `settings_factory(_env_file=None)`.
- Updated stale test harnesses for long-lived BL-064 disabled heartbeat, calibration/feedback Telegram kwargs, CoinGecko query-param mocks, narrative resolution exception type, and signal-revival audit row selection.
- Production fix: pinned `parse_mode=None` at four `scout/main.py` Telegram dispatch sites flagged by the parse-mode hygiene audit.

## Active Work: X Alerts outcome columns

- [x] Isolated worktree created: `C:\projects\gecko-alpha-x-alert-outcome` on `codex/x-alert-outcome`
- [x] Drift check: existing X Alerts dashboard reads `narrative_alerts_inbound`; existing market tables include `price_cache`, `gainers_snapshots`, `volume_history_cg`, `volume_spikes`, and `momentum_7d`
- [x] Hermes-first check: existing Hermes `xurl` / `narrative_classifier` / `narrative_alert_dispatcher` path remains the source of X signals; this change adds dashboard-side valuation only, so no new Hermes/custom ingestion primitive is introduced
- [x] TDD: add endpoint coverage for $300 flat-investment outcome fields
- [x] Implement backend valuation with conservative unresolved/ambiguous fallback
- [x] Add X Alerts table columns for entry price, current price, % since alert, and $ P/L @ $300
- [x] Follow-up: make X Alert asset values clickable, using DexScreener for contract rows and CoinGecko for confidently resolved coin ids
- [x] Verify focused backend tests and frontend build: `tests/test_x_alerts_dashboard.py tests/test_dashboard_search.py` -> 34 passed; `npm run build` -> Vite production build passed
- [x] PR created: https://github.com/Trivenidigital/gecko-alpha/pull/133

## Active Work: BL-NEW-CG-RATE-LIMITER-BURST-PROFILE

- [x] Isolated worktree created: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-cg-burst-smoothing` on `codex/cg-burst-smoothing`
- [x] Runtime symptom verified: post-deploy CoinGecko 429 backoffs are slowing 60s cycles into ~101s average / ~263s max intervals
- [x] Drift check: existing `scout.ratelimit.RateLimiter` caps rolling request count, but has no inter-request spacing or jitter to smooth concurrent CoinGecko lanes
- [x] Hermes-first check: public Hermes skill hub / awesome-hermes-agent search found CoinGecko API reference and optional blockchain skills, but no installed/public Hermes runtime primitive for smoothing gecko-alpha's aiohttp CoinGecko calls
- [x] Baseline relevant tests: `tests/test_ratelimit.py tests/test_config.py` -> 35 passed
- [x] Design drafted: `tasks/design_bl_new_cg_rate_limiter_burst_profile.md`
- [x] TDD red: limiter tests prove consecutive calls are not currently spaced
- [x] Implementation: add configurable spacing/jitter to the shared CoinGecko limiter
- [x] Self-review fold: `configure_from_settings()` now mutates the limiter singleton in place so pre-imported CoinGecko modules receive the new burst profile
- [x] Verification: `tests/test_ratelimit.py tests/test_config.py tests/test_coingecko.py` -> 58 passed; wider CoinGecko-consumer suite -> 147 passed
- [x] Backlog closeout updated for PR-ready state
- [x] PR created: https://github.com/Trivenidigital/gecko-alpha/pull/129
- [x] Follow-up isolated worktree created: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-cg-throttle-fix` on `codex/cg-throttle-fix`
- [x] Runtime follow-up verified: throttles persisted after PR #129 spacing and conservative VPS tuning (`6/min`, `8s` min spacing, `2s` jitter)
- [x] Root cause pinned: `_get_with_backoff()` retried each 429 up to four times inside one cycle; Telegram social resolver also bypassed the shared CoinGecko limiter
- [x] TDD red: tests captured no-immediate-retry behavior, configurable default 429 cooldown, and resolver shared-limiter reporting
- [x] Implementation: CoinGecko 429 now trips global cooldown and fails soft without same-cycle retry; resolver and second-wave paths report 429s into the shared limiter
- [x] Verification: `tests/test_ratelimit.py tests/test_config.py tests/test_coingecko.py tests/test_tg_social_resolver.py::test_resolver_coingecko_429_uses_shared_limiter` -> 60 passed; adjacent suite -> 159 passed
- [x] Post-PR #130 deploy observation: retry ladder removed, but concurrent CoinGecko fan-out could still queue sibling requests before `report_429()` preempted them
- [x] Follow-up implementation: expose `RateLimiter.is_backing_off()` and make top-mover, volume-scan, and midcap CoinGecko lanes stop remaining same-cycle requests after a 429 cooldown is active
- [x] Follow-up verification: targeted throttle suite -> 63 passed; adjacent CoinGecko/social/second-wave suite -> 162 passed
- [x] Post-PR #131 deploy observation: `main.py` still launched separate CoinGecko lanes concurrently, so cross-lane fan-out persisted after a 429
- [x] Final fold: add `_fetch_coingecko_lanes()` in `main.py` to run CoinGecko lanes sequentially while DexScreener/GeckoTerminal remain parallel
- [x] Final fold verification: main/CoinGecko targeted suite -> 68 passed; adjacent suite -> 167 passed

## Active Work: 2026-05-14 gecko-alpha improvement run

- [x] Follow-up - BL-NEW-GT-ETH-ENDPOINT-404 on `codex/gt-eth-endpoint-404`: root cause pinned as GeckoTerminal provider id mismatch (`ethereum` project label vs `eth` GT network id). Design drafted in `tasks/design_bl_new_gt_eth_endpoint_404.md`; TDD red/green verified; focused GT/config tests 44 passed. Design reviewers timed out and were closed with no findings returned.
- [x] Follow-up - BL-NEW-INGEST-WATCHDOG implemented on `codex/ingest-watchdog`. Drift check found no existing per-source starvation state. Hermes-first found `webhook-subscriptions` notification-adjacent only; custom in-process detector justified while reusing `scout.alerter.send_telegram_message(parse_mode=None)`. Design captured in `tasks/design_ingest_watchdog.md`; focused suite 85 passed.
- [x] Item 1 - PR #119 merged: Hermes crypto-skill tracking + backlog rescope landed as `acf4b8e`. CI on PR #119 failed on unrelated baseline tests (8 failures across BL064 reload, calibration scheduler, heartbeat mcap, narrative token-id, signal-param revival); docs-only diff was merged with that caveat recorded in merge message.
- [x] Item 2 - BL-NEW-HERMES-FIRST-DEBT-AUDIT findings drafted in `tasks/findings_hermes_first_debt_audit_2026_05.md`.
- [x] Item 2 - backlog updated: BL-NEW-HERMES-FIRST-DEBT-AUDIT marked SHIPPED with priority follow-ups.
- [x] Item 3 - CoinGecko breadth + trending hydration fix implemented on `codex/coingecko-breadth-hydration`; PR-ready after 77 focused tests passed. Known unrelated heartbeat/aioresponses failures remain from PR #119 baseline.
- [x] Item 4 - BL-032 social signal audit drafted in `tasks/findings_bl032_social_signal_audit_2026_05_14.md`; backlog rescope closes custom Twitter/LunarCrush direction and adds scorer-denominator follow-up.
- [x] Item 5 - signal-quality gap report drafted in `tasks/findings_top_gainers_gap_2026_05_14.md`; backlog adds BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN for the exact miss class.
- [x] Follow-up - BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN implemented on `codex/coingecko-midcap-gainer-scan`; focused regression 83 passed.

## Completed: BL-NEW-GT-429-HANDLER

- [x] Isolated worktree created: `C:\projects\gecko-alpha-gt-429-handler` on `codex/gt-429-handler`
- [x] Drift check: GeckoTerminal lacks 429/5xx retry; DexScreener has the in-tree retry pattern to reuse
- [x] Hermes-first check: no installed VPS/public Hermes skill covers GeckoTerminal aiohttp ingestion retry
- [x] Baseline relevant tests: `tests/test_geckoterminal.py tests/test_dexscreener.py` -> 8 passed using pre-provisioned project venv
- [x] Plan drafted: `tasks/plan_bl_new_gt_429_handler.md`
- [x] Plan review by two parallel reviewers
- [x] Fold plan-review findings
- [x] Design drafted: `tasks/design_bl_new_gt_429_handler.md`
- [x] Design review by two parallel reviewers (one completed with findings; second timed out and was closed)
- [x] Fold design-review findings
- [x] TDD build
- [x] PR-review fix: convert legacy 500 test into explicit 5xx exhaustion coverage
- [x] PR-review fix: add multi-chain continuation after retry exhaustion
- [x] PR-review fix: assert structured fields on exhaustion telemetry
- [x] Targeted verification rerun: `tests/test_geckoterminal.py tests/test_geckoterminal_rank.py tests/test_dexscreener.py tests/test_coingecko.py` -> 28 passed
- [x] PR creation: https://github.com/Trivenidigital/gecko-alpha/pull/115
- [x] Three-reviewer PR pass (two completed; operational/Hermes reviewer timed out and was closed)
- [x] Merge: PR #115 squash-merged as `30b588a`
- [x] Deploy to VPS: `master` at `30b588a`, `gecko-pipeline` active, `geckoterminal_non_retryable_status` observed for known ethereum 404

## BL-NEW-QUOTE-PAIR soak (post-deploy)

- [ ] **D+3 mid-soak verification** — query `candidates` table for fraction satisfying `quote_symbol ∈ stables AND liquidity_usd >= 50K`. Threshold: < 40% to keep current bonus magnitude. Query in `docs/runbook_high_peak_fade.md`-adjacent runbook if needed.
- [ ] **D+7 soak end** — alert volume must not exceed +10% baseline. Revert via `STABLE_PAIRED_BONUS=0` env override if breached.

## Pending verifications (time-gated)

- [x] **2026-05-04 ~01:09Z+ — BL-071 guard verification (24h check).** **PASS (with caveat).** Verified 2026-05-04T15:35Z. `full_conviction` + `narrative_momentum` still `is_active=1` ✓. `volume_breakout` retired 2026-05-04T01:01:48Z via the `chain_pattern_retired` path (hit_rate=1.82%, 1 hit in 55 attempts) — legitimate individual underperformance, NOT a guard failure. The guard only short-circuits on `total_hits_across_all == 0`; with non-zero hits on at least one pattern, individual retirement is allowed (correct behavior). chain_completed paper_trades count: 7 → 10 in 24h (+3 new). Chain dispatch alive. No action needed.
- [x] **2026-05-04 13:58Z — BL-063 moonshot soak ends. DECISION: keep on permanently.** Verified 2026-05-04T15:35Z. Moonshot path: **19 closes / +$2,232.86 net / +$117.52/trade / 100% win**. Regular-trail comparison (peak ≥30, no moonshot armed): 13 closes / +$773.52 net / +$59.50/trade / 100% win. Moonshot delta = +$1,459.34 net — exceeds the +$1,420 sneak-peek prediction by ~3% and ~3× the regular-trail per-trade. Permanent.
- [ ] **2026-05-04 22:24Z — Paper-lifecycle widening soak ends.** Sneak-peek +$1,234 net / 91 closes. Decision: keep on.
- [ ] **2026-05-05 22:58Z — PR #59 strategy tuning soak ends.** Sneak-peek +$1,994 net / 135 closes / 67.4% win / 20% expired. Decision: keep on permanently.
- [ ] **2026-05-10 15:53Z — gainers_early reversal re-soak (7d).** Watch for performance vs the +$190/day sneak-peek that justified reversal. If actuals < +$100/day for 7d, re-evaluate.
- [x] **2026-05-13 02:13Z — losers_contrarian post-BL-NEW-AUTOSUSPEND-FIX revival 7d soak.** **KEEP ON (permanent).** Closed 2026-05-13T04:05Z. n=55, net +$826.68, per_trade +$15.03, win 69.1%. Both gate clauses cleared by ~4×. Zero auto-suspend fires during soak. Drivers: `peak_fade` n=26 +$1,688; `stop_loss` n=11 −$917 drag. Audit row id=23.
- [x] **2026-05-13 02:15Z — gainers_early post-BL-NEW-AUTOSUSPEND-FIX revival 7d soak.** **KEEP ON (permanent).** Closed 2026-05-13T04:05Z. n=128, net +$1,894.37, per_trade +$14.80, win 72.7%. Both gate clauses cleared. Zero auto-suspend fires during soak. `conviction_lock_enabled=1` stays armed. Drivers: `peak_fade` n=38 +$2,499 + `trailing_stop` n=54 +$888; `stop_loss` n=13 −$1,059 drag. Audit row id=24.
- [x] **2026-05-13 02:18Z — HPF dry-run 7d soak (BL-NEW-HPF Phase 1).** **KEEP DRY-RUN. Do NOT flip the flag.** Closed 2026-05-13T04:05Z. n=7 would-fires (6 gainers_early + 1 losers_contrarian). Aggregate counterfactual: HPF +$1,078.15 vs actual +$1,123.63 — **delta −$45.48 (negative)**. Subset reading (structural §9c): HPF beats `moonshot_trail` 3/3 (+$238) but loses to existing `peak_fade` 3/4 (−$285). Re-evaluate at n≥20 scoped to `moonshot_trail`-subset only (filed BL-NEW-HPF-RE-EVALUATION). Audit row id=25.
- [ ] **2026-05-13+ — Deploy PR #82 BL-NEW-MOONSHOT-OPT-OUT (held overnight 2026-05-06).** Migration adds `signal_params.moonshot_enabled INTEGER NOT NULL DEFAULT 1` — no behavior change on deploy (default opt-IN preserves existing floor). Per-signal opt-out via `UPDATE signal_params SET moonshot_enabled=0 WHERE signal_type='X'`. Backtest applicability caveat: `findings_high_peak_giveback.md` PnL projection used floored regime; opted-out signal must re-run backtest with floor removed before projecting impact.
- [x] **2026-05-17 — chain_complete fire-rate observation post-PR #80: CLOSED.** Lifetime: full_conviction=201, narrative_momentum=210, volume_breakout=301 chain_matches. Post-PR-#146 recent: active_chains=83 rows in 14d (oldest 2026-05-11T16:41Z), all 4 narrative anchor events fired 139× each in 7d. Paper-trades: 12 chain_completed in 14d, +$1,034 net, +$207/trade. Observability bump served purpose; PR #154 reverts `scout/chains/patterns.py` full_conviction + narrative_momentum from `medium` → `low` (also code-vs-prod-state alignment — PR #146 snapshot-restore already had prod at `low`). 14/14 chain_patterns tests pass including new closure-test `test_builtin_patterns_alert_priority_post_observability_revert`.

## Active soaks (don't disturb)

- [x] **Tier 1a flip — gainers_early kill REVERSED 2026-05-03T15:53Z** — original kill was based on pre-PR-#59 30d data. Sneak-peek of post-#59 data (4.7d window) showed gainers_early at +$508 / 59 closes / +$8.61/trade / 67.8% win — clearly profitable under the new adaptive trail. PR #59 fixed gainers_early; the kill was forfeiting ~$190/day. SQL reversal + restart verified: 5 new gainers_early trades opened at 15:58:29Z, zero `trade_skipped_signal_disabled` events. Tier 1a `SIGNAL_PARAMS_ENABLED=true` flag stays on for the other 7 signals (per-signal params still honored). Audit row in signal_params_audit. Backup: `scout.db.bak.gainers_revive_20260503_155322`.

- [ ] **2026-05-15 14:06Z — RE-SCOPED system health checkpoint (was: "Tier 1a kill 14d soak").** The original A/B (kill gainers_early, see net swing) was invalidated 2026-05-03 when we reversed the kill based on post-PR-#59 data. New scope: 2-week strategic checkpoint after a flurry of changes (Tier 1a flag on, per-signal params live, chain_completed dispatch wired + long-hold tuned, BL-071 guard live). Three concrete questions:
  1. **System P&L re-baseline.** Compute 14d rolling net (2026-05-01 → 2026-05-15) and compare to the −$506 baseline that motivated all the recent changes. Decision gate: ≥ +$1,000 net = strategy stack worked; +$0–$1,000 = mixed; < $0 = something else is bleeding, dig in.
  2. **Tier 1a infrastructure health.** Did Tier 1b auto-suspend fire on anything (shouldn't have, since all signals trended profitable in the 4.7d sneak-peek)? Did anyone run `calibrate.py`? Are signal_params_audit rows clean and traceable? Any latency regression from per-signal lookup vs Settings reads?
  3. **Next-best-next decision.** With 2 weeks of cleaner data and chain_completed actually producing trades, decide what's next: BL-067 (conviction-locked hold), BL-071a/b (outcome plumbing fixes), or "leave the system alone, monitor for another 30d, then revisit". Optionally also: do we re-evaluate BL-070 (entry stack gate) given the data actually shows we're net positive without it?
  - Verify queries (paste into VPS sqlite):
    ```
    -- (1) 14d rolling net since Tier 1a flip
    SELECT COUNT(*), ROUND(SUM(pnl_usd),2), ROUND(AVG(pnl_usd),2),
      ROUND(100.0*SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS win_pct
    FROM paper_trades WHERE status LIKE 'closed_%'
      AND datetime(closed_at) >= datetime('2026-05-01 14:06:00');
    -- (2) per-signal breakdown including chain_completed
    SELECT signal_type, COUNT(*) AS n, ROUND(SUM(pnl_usd),2) AS net,
      ROUND(AVG(pnl_usd),2) AS per_trade,
      ROUND(100.0*SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS win_pct
    FROM paper_trades WHERE status LIKE 'closed_%'
      AND datetime(closed_at) >= datetime('2026-05-01 14:06:00')
    GROUP BY signal_type ORDER BY net DESC;
    -- (3) auto-suspend events (Tier 1b should NOT have fired)
    SELECT * FROM signal_params_audit WHERE applied_by = 'auto_suspend';
    -- (4) all operator/calibration changes since Tier 1a went on
    SELECT * FROM signal_params_audit
    WHERE datetime(applied_at) >= datetime('2026-05-01 14:06:00')
    ORDER BY applied_at;
    ```
  - This is no longer an A/B test — just a 2-week strategic checkpoint. No automatic action; user-driven decision.
- [ ] **PR #58 BL-064 lenient-safety soak** — flag flipped 2026-04-28T15:17Z. Re-check window: 2026-05-12.
  - Decision gate: ≥40% win rate + avg pnl_pct >0 → keep on. As of 2026-04-29T12:25Z: 0 trades dispatched yet (curators haven't posted CA-bearing messages since flag flipped). Operational gap, not code.
- [ ] **PR #59 strategy tuning soak** — deployed 2026-04-28T22:58Z. Re-check window: 2026-05-05.
  - Early signal at 13.5h: 23 closes, +$650 net, ~70% win rate, 0 expired closes. 9× improvement in $/trade vs historical −$3.05. Letting it ride.
- [ ] **BL-063 moonshot soak** — flag flipped 2026-04-27T13:58Z. Soak ends 2026-05-04T13:58Z.
- [ ] **BL-064 14d TG social soak** — ends 2026-05-11T22:10Z.
- [ ] **Paper-lifecycle widening soak** — .env tweaks deployed 2026-04-27T22:24Z. Soak ends ~2026-05-04T22:24Z.

## Pending operator action (blocked on user)

- [x] **2026-05-06 02:40Z — Telegram credentials wired up.** Bot @Srini_gecko_bot (id 8427551586) DM'd to chat_id 6337722878 (operator's @LowCapHunt account). Test message via `alerter.send_telegram_message` confirmed end-to-end delivery. .env backup at `.env.bak.tg_<timestamp>`. Unblocks: BL-063 moonshot alerts, BL-064 social dispatches, channel-silence heartbeat, auto_suspend kill-switch (incl. new combined-gate paths), paper fills, calibrate weekly --dry-run alert (PR #76), future BL-NEW-HPF would-fire alerts.

## Next deliverables (in priority order)

### 1. Self-learning Tier 1a + 1b (proposed, awaiting user go-ahead)

The user asked "why isn't the agent self-learning". My response (deferred decision): scope a single PR for **per-signal parameter table** + **auto-suspension of dud signals**. Roughly:

- New `signal_params` DB table — per-signal-type LEG_1_PCT / TRAIL_PCT / SL_PCT / etc. Defaults seeded from current global Settings.
- Weekly calibration script that reads `combo_performance` rolling 30d, writes recalibrated params back to `signal_params`. Operator approves before write goes live (dry-run flag default).
- Evaluator reads per-signal params instead of global Settings.
- Auto-suspension: rolling 30d net P&L < threshold → set signal's `enabled=False` in DB + Telegram alert. One-way switch (manual re-enable).
- Tests + 1-2 day estimate.

This is NOT ML — just data-driven static rules with self-resetting parameters. Real ML (outcome model, RL exit timing) gated on ≥1000 trades/signal stable for 30d (not yet).

**~~User has not approved scope yet. Resume by asking.~~ CLOSED 2026-05-04 — already shipped.**

Drift research 2026-05-04 confirmed every component is in tree and operating in production:

- ✅ `signal_params` table + `signal_params_audit` (`scout/db.py:1578-1679`)
- ✅ `SignalParams` dataclass + `get_params` + cache (`scout/trading/params.py`)
- ✅ `SIGNAL_PARAMS_ENABLED=true` on prod
- ✅ **`scout/trading/calibrate.py`** (557 lines) — `--apply` / `--dry-run` / `--since-deploy` / `--force-no-alert`
- ✅ **`scout/trading/auto_suspend.py`** (268 lines) — hard_loss + pnl_threshold triggers
- ✅ Auto-suspend wired in `_run_feedback_schedulers` at `scout/main.py:163-170`
- ✅ Dashboard endpoint at `dashboard/api.py:953`
- ✅ Plan/design at `tasks/plan_tier_1a_1b.md` (544 lines, 5-reviewer signed off)

**Production evidence Tier 1b is firing daily** (3 audit rows by `applied_by='auto_suspend'`):
- 2026-05-02T01:00:18Z — first_signal + losers_contrarian (hard_loss)
- 2026-05-04T01:01:02Z — gainers_early (hard_loss)

**Real residual gaps (small, NOT blocking):**
- Calibrator never run in production (0 audit rows with `applied_by='calibration'`); operator-manual-by-design. Optional follow-up: weekly cron `--dry-run` + Telegram diff alert (no auto-apply).
- BL-067 opt-in 2026-05-04T15:31Z flipped `conviction_lock_enabled=1` for first_signal + gainers_early, both currently `enabled=0` (auto-suspended). Lock works on existing open trades only. Strategy decision pending: re-enable for new entries, or stay suspended-with-locked-existing.

### 2. Watchlist for next strategy-tuning re-check

When user asks "how is strategy tuning going" tomorrow:
- Re-run `.ssh_recheck.txt` queries (commands documented in conversation)
- Compare 36h post-deploy vs 13.5h baseline
- Look for: BL-064 first dispatched trade (depends on curator activity), trail/leg-1 fire rate stabilizing, gainers_early per-trade P&L sign

### 3. Open optional follow-ups (not urgent)

- [x] **2026-05-06 Channel-list reload task in BL-064 listener** — CLOSED-AS-SHIPPED. Drift-check finds: PR #73 (`a12603f`, 2026-05-04) shipped channel hot-reload via `_channel_reload_once` (`scout/social/telegram/listener.py:1252-1325`), heartbeat factory `_make_channel_reload_heartbeat` at line 1327, and structural-typed channels_holder TypedDict refactor in PR #75 (`8e54578`). Listener swaps handlers on reload without pipeline restart. todo.md item was stale.
- [ ] `narrative_prediction` token_id divergence fix — 32 of 56 stale-young open trades have empty/synthetic token_ids that don't appear in `price_cache`. Separate upstream fix.
- [x] **2026-05-06 @s1mple_s1mple verdict — DO-NOT-ADD (off-thesis).** Background investigation 2026-05-06: `@s1mple_s1mple` doesn't resolve via Bot API (likely user account, not channel — incompatible with Telethon listener). `@s1mplegod123` resolves as Russian-language esports diary "Дневник Симпла" (Counter-Strike pro s1mple of NaVi), 256K subscribers, ZERO crypto content across t.me sample + 1,220 cross-channel mention rows. No DB references in 5 tables. Operator can still add as `trade_eligible=0, cashtag_trade_eligible=0` watch-only with 30-day re-eligibility check if desired despite fit, but default action is no-add. See investigation notes inline; no separate findings file written.
- [ ] Audit fix #4 (24h hard-exit if peak<5%) deferred — accumulate more data first.
- [x] **BL-NEW-REVIVAL-COOLOFF — SHIPPED 2026-05-06** (PR #81 / `57192cb`). 7-day default cool-off on `revive_signal_with_baseline` with `force=True` bypass. Plan-stage MUST-FIX: positive `applied_by='operator'` filter. Design-stage MUST-FIX: settings DI. PR-stage CRITICAL: caplog→capture_logs. All applied. Smoke-tested on VPS: cool-off correctly blocks losers_contrarian re-revival.
- [x] **#3 Channel-list reload — CLOSED-AS-SHIPPED 2026-05-06.** Drift-check: PR #73 (`a12603f`, 2026-05-04) shipped channel hot-reload via `_channel_reload_once` + heartbeat factory + channels_holder TypedDict. todo.md item was stale.
- [x] **narrative_prediction token_id divergence — UPSTREAM FIX SHIPPED 2026-05-06** (PR #80 / `eaf3523`). Original symptom (32/56 stale-young opens) resolved by PR #72 + zombie cleanup. Real upstream cause was agent.py emitting `category_heating` with `token_id=accel.category_id`, breaking chain pattern matching. Pre-fix: 2,770 anchors → 2 chain_completes. Post-fix: per-laggard emission with `token.coin_id`.
- [x] **#5 @s1mple_s1mple verdict — DO-NOT-ADD 2026-05-06.** Esports diary, no crypto.
- [x] **moonshot floor nullification — UPSTREAM FIX MERGED 2026-05-06** (PR #82, deploy held until 2026-05-13). Per-signal `moonshot_enabled INTEGER NOT NULL DEFAULT 1` opt-out flag.
- [ ] **first_signal revival decision** — under combined-gate rule, first_signal would NOT auto-fire (-$132 30d net is borderline). Operator decision: revive for soak, or leave suspended. Note: revival now subject to 7-day cool-off (PR #81); first revival ever bypasses cool-off cleanly.

## What shipped this session (2026-04-28 → 2026-04-29)

| PR | Commit | Topic |
|---|---|---|
| #55 | 4c057e3 | BL-064 listener resilience (bad-handle / crash-state / txn-lock) — 3 fixes + 13 tests |
| #56 | 9127959 | Drop explicit BEGIN IMMEDIATE — match project _txn_lock pattern |
| #57 | adf1a32 | Dashboard reconcile open-trade PnL$ and PnL% on partial-fill ladders |
| #58 | 2061675 | BL-064 per-channel `safety_required` flag — unblocks fresh memecoins |
| #59 | 3c83fb7 | Strategy tuning — adaptive trail + per-signal kill switches |

Test count: 1354 → 1389 passing (+35 across the PRs).

Prod .env current state (relevant flags):
```
PAPER_MAX_DURATION_HOURS=168
PAPER_SL_PCT=25
PAPER_LADDER_TRAIL_PCT=20
PAPER_LADDER_LEG_1_PCT=10.0           # PR #59 — was 25 default
PAPER_LADDER_LEG_1_QTY_FRAC=0.50
PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=false
PAPER_SIGNAL_TRENDING_CATCH_ENABLED=false
TG_SOCIAL_ENABLED=True
TELEGRAM_BOT_TOKEN=placeholder        # ⚠️ not real
TELEGRAM_CHAT_ID=placeholder          # ⚠️ not real
```

Active TG channels (7):
- `@detecter_calls` (trade_eligible, safety_required=0)
- `@thanos_mind` (trade_eligible, safety_required=0)
- `@cryptoyeezuscalls` `@Alt_Crypto_Gems` `@nebukadnaza` `@alohcooks` `@CallerFiona1` (alert-only, strict)
- `@gem_detecter` (retired — typo, doesn't exist on Telegram)

## Resume hook

When the user comes back, the obvious next move is one of:
1. Approve the Tier 1a + 1b self-learning PR scope and start that work
2. Re-run the post-deploy strategy check-in (24-36h window now)
3. Set the real Telegram bot token + chat_id

Default suggestion if user opens with a generic "what's up": run the post-deploy check-in (option 2) — it's quick and gives them fresh data.
