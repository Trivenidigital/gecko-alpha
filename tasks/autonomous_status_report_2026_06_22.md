# Gecko-Alpha autonomous status (local, read-only)

- Repo root: `C:/Users/srini/.codex/worktrees/e43e/gecko-alpha`
- Branch: `feat/overnight-closeout-20260622`
- HEAD: `b0df1720 2026-06-21T19:50:42+05:30 docs: add offshore live trading handoff (#373)`

## Key files present

- `backlog.md`: present
- `tasks/todo.md`: present

## Backlog anchors (best-effort)

- `BL-NEW-HERMES-CODEX-OPERATING-MODEL` @ backlog.md:182 - **Status:** PROPOSED 2026-05-22 - filed from operator strategy direction after Hermes+Codex architecture review. This is the working model for Gecko-Alpha going forward: Hermes is the orchestration/memory/scheduling layer; Codex is the coding/repo/runtime execution worker; the operator owns product and trading judgment.
- `BL-NEW-LIVE-DECISION-COCKPIT` @ backlog.md:2727 - **Status:** SHIPPED-PARTIAL / PARENT-ARCHIVED 2026-05-26 — parent cockpit work is no longer buildable as a single backlog item. Core trader surfaces now exist: `/api/live_candidates`, Now Tradable, `/api/trade_inbox`, tracker-to-cockpit promotion, trade decision events, Trade Inbox contract firewall, and aggregate dashboard contract smoke. Future work must target specific residual child gaps instead of rebuilding the parent.
- `BL-NEW-SIGNAL-TRUST-ROADMAP` @ backlog.md:2803 - **Status:** PARTIALLY-SHIPPED 2026-05-27 - registry and Signal Trust tab shipped in PR #239; per-signal scorecards shipped in replacement PR #289. PR #276 is closed/superseded. Remaining roadmap items below require fresh scope from current base.

## Template coverage

- `docs/superpowers/templates`: present
- All required templates present.

## Closeout work-loop runner (drift-check)

### Runner candidates

- No in-tree runner candidates found for `gecko-overnight-autonomous-closeout`.
- First-run behavior: manual/runbook-driven until a concrete scheduler or launcher artifact is designed, reviewed, and operator-approved.

### Reference-only mentions

- `scripts/report_autonomous_status.mjs` (matched: gecko-overnight-autonomous-closeout; reporter-self-reference)
- `tasks/autonomous_status_report_2026_05_23.md` (matched: gecko-overnight-autonomous-closeout; reference-only)
- `tasks/autonomous_status_report_2026_05_25.md` (matched: gecko-overnight-autonomous-closeout; reference-only)
- `tasks/closeout_report_overnight_autonomous_closeout_2026_05_23_prodpush.md` (matched: overnight autonomous closeout; reference-only)
- `tasks/closeout_report_overnight_autonomous_closeout_2026_05_23_run10_prodpush.md` (matched: overnight autonomous closeout; reference-only)
- `tasks/findings_autonomous_closeout_work_loop_state_2026_05_23.md` (matched: gecko-overnight-autonomous-closeout; reference-only)
- `tasks/plan_overnight_autonomous_closeout_2026_05_23_prodpush.md` (matched: overnight autonomous closeout; reference-only)
- `tasks/todo.md` (matched: overnight autonomous closeout; reference-only)

## Changes since `--since`

- Since: `2026-05-29T20:54:51.511Z`
- Commit before since (best-effort): `b1e1c752dc092ec48164b07714e3c0bf9e7798f5`

- Commits:
  - `b0df1720 2026-06-21T19:50:42+05:30 docs: add offshore live trading handoff (#373)`
  - `37511209 2026-06-19T22:34:48+05:30 feat(conviction): Prospective sub-$30M high-conviction watchlist (V1, observe-only) (#372)`
  - `db4c1c8d 2026-06-19T04:14:36+05:30 docs(first-signal): file extend-soak verdict (#371)`
  - `4f7e09ab 2026-06-19T01:14:12+05:30 feat(tg): retry_after pacing + bounded retry + source attribution (P1 #2) (#370)`
  - `b744cc18 2026-06-18T21:16:14+05:30 docs(slow_burn): failure finding + retire paper dispatch (P1 #3) (#369)`
  - `1d82b0f1 2026-06-18T18:50:07+05:30 feat(sqlite): durable WAL/freelist maintenance + stale-reader watchdog (P0 Part B) (#368)`
  - `0a1236fe 2026-06-14T00:27:00+05:30 fix(deploy): stop dist/index.html CRLF churn that blocks git pull on srilu (#367)`
  - `a3cb9f36 2026-06-14T00:04:05+05:30 feat(conviction-panel): per-column filtering + sortable headers (#366)`
  - `9df152c9 2026-06-13T06:39:28+05:30 feat(conviction): dashboard Conviction panel + recency sort (BL-NEW-CONVICTION-DASHBOARD-PANEL) (#365)`
  - `f8503055 2026-06-13T04:44:05+05:30 feat(conviction): cross-surface conviction score + shortlist (BL-NEW-CROSS-SURFACE-CONVICTION-SCORE) (#364)`
  - `84ad11e1 2026-06-13T01:11:22+05:30 docs(cohort): file BL-NEW-COHORT-DIGEST-EXTEND-4w (data-bound, 3 live-eligible signals) (#363)`
  - `c35caeb1 2026-06-13T01:06:39+05:30 feat(slow-burn): promote to flag-gated paper dispatch (BL-NEW-SLOW-BURN-DISPATCH-PROMOTION) (#362)`
  - `705365a7 2026-06-13T00:52:37+05:30 fix(minara): emit for native-Solana token ids (BL-NEW-MINARA-SOLANA-NATIVE-ID) (#361)`
  - `dbcb3ab3 2026-06-13T00:09:30+05:30 docs(soak): resolve 5 soak-completed backlog items (cohort/HPF/low-peak/minara/slow-burn) (#359)`
  - `55c46ad9 2026-06-13T00:03:30+05:30 fix(audit): anchor time-rot test to real now (unbreak master CI) (#360)`
  - `0acf0f5c 2026-06-05T02:47:26Z docs: close out trader decision cockpit deploy`
  - `34a3db3d 2026-06-05T02:41:04Z docs: record trader decision cockpit verification`
  - `8f320b71 2026-06-05T02:37:04Z build(dashboard): refresh trader decision bundle`
  - `bed2d04c 2026-06-05T02:35:14Z feat(dashboard): surface trader decision board`
  - `d95c2f1b 2026-06-05T02:32:17Z feat(dashboard): add trader decision board helper`
  - `2cb558fb 2026-06-05T02:29:40Z docs: add trader decision cockpit plan`
  - `c552297a 2026-06-05T02:29:07Z docs: plan trader decision cockpit`
  - `4f30fc3a 2026-06-02T12:55:57+05:30 feat(ingestion): proactive deep-volume page for $500K-$10M gap (Increment 2, #358)`
  - `32f3a2ca 2026-06-02T10:57:39+05:30 chore(ops): one-time gainers-comparison historical backfill (#357)`
  - `5a46e87f 2026-06-02T10:36:15+05:30 fix(gainers): tracker same-day timestamp bug + acceleration detector + $200M cap (#356)`
  - `6dfdc22e 2026-06-02T00:39:26+05:30 docs(todo): mark social denominator option b shipped`
  - `f2b85e49 2026-06-02T00:26:53+05:30 feat(scoring): remove dead social denominator`
  - `7ae28cce 2026-06-01T23:53:26+05:30 docs(todo): mark social denominator evidence shipped`
  - `b8d1ae1c 2026-06-01T23:45:52+05:30 chore(scoring): refresh social denominator evidence`
  - `a60f2817 2026-06-01T22:29:43+05:30 docs(phase-c): record live-trading path gaps (BL-055 close-before-go-live checklist) (#351)`
  - `6f4db89b 2026-06-01T22:26:45+05:30 fix(paper): fold realized ladder legs into closed-trade PnL (#350)`
  - `5cf68350 2026-06-01T21:55:04+05:30 feat(observability): per-cycle detection latency instrumentation (#349)`
  - `dee48ca8 2026-06-01T21:34:45+05:30 feat(reliability): §12 silent-failure hardening + kill-switch fail-safe fix (#348)`
  - `30821309 2026-05-31T23:21:52+05:30 docs(tg-alert): mark operator action telemetry shipped (#347)`
  - `3153fe3d 2026-05-31T19:50:16+05:30 fix(tg-alert): pace trade surface sends (#346)`
  - `463ce33f 2026-05-31T19:37:30+05:30 feat(tg-alert): add trade surface alerts (#345)`
  - `a6dcd13a 2026-05-31T18:47:52+05:30 feat(tg-alert): capture operator action telemetry (#344)`
  - `2bf9f4f1 2026-05-31T09:59:16+05:30 docs: close autonomous review task log (#343)`
  - `f453d5dd 2026-05-31T09:53:00+05:30 fix: harden autonomous review findings (#342)`
  - `f138c95c 2026-05-31T08:01:12+05:30 feat(dashboard): show entry snapshots in trade drawer (#341)`
  - `1361d939 2026-05-31T07:47:00+05:30 fix(actionability): preserve candidate first seen (#340)`
  - `a127a6ac 2026-05-31T07:32:50+05:30 feat(actionability): harden entry snapshot migration (#339)`
  - `aac53556 2026-05-31T06:55:55+05:30 feat(dashboard): show health changes in what changed (#338)`
  - `4ea6d97f 2026-05-31T06:17:48+05:30 feat(observability): per-subsystem health status enum on /api/system/health (BL-NEW-API-SYSTEM-HEALTH-STATUS-ENUM) (#337)`
  - `32bd1f6b 2026-05-30T23:05:34+05:30 feat(tg-alert): 24h per-token strict dedup + audit log (BL-NEW-TG-ALERT-NOISE-DEDUP) (#336)`
  - `686d0651 2026-05-30T20:41:32+05:30 docs(backlog): record Kraken audit verdict on autotrade-prep + file BL-NEW-API-SYSTEM-HEALTH-STATUS-ENUM follow-up (#335)`
  - `fe280968 2026-05-30T10:29:38+05:30 feat(dashboard): What Changed since last visit panel (BL-NEW-DASHBOARD-WHAT-CHANGED-SINCE-LAST-VISIT) (#334)`
  - `e8c21dc4 2026-05-30T08:41:09+05:30 docs: record Kraken MCP phantom-precondition runtime finding + reconciliation (2026-05-30) (#333)`
  - `74c485d9 2026-05-30T08:19:55+05:30 docs(backlog): flip missed-winner to SHELVED + add SHELVED banner to design (#332)`
  - `883c9f81 2026-05-30T08:16:06+05:30 docs(backlog): shelve missed-winner audit (structural data limit); preserve design (#331)`
  - `af8a15fd 2026-05-30T07:42:04+05:30 docs(backlog): mark audit primitives #326/#328/#329 shipped; missed-winner designed-not-built (#330)`
  - `3e3d5fcc 2026-05-30T07:07:27+05:30 feat(audit): focus freshness/tradability diagnostic (BL-NEW-FOCUS-FRESHNESS-TRADABILITY-GATES step 1) (#329)`
  - `c74a23eb 2026-05-30T05:38:49+05:30 feat(audit): signal early-usefulness scorecard (BL-NEW-SIGNAL-EARLY-USEFULNESS-SCORECARD) (#328)`
  - `e0172706 2026-05-30T05:16:15+05:30 feat(audit): clean price-path runner-attribution audit (BL-NEW-CLEAN-PRICE-PATH-AUDIT) (#326)`

- Changed files (best-effort diff):
  - `M	.env.example`
  - `M	.gitattributes`
  - `M	.gitignore`
  - `M	README.md`
  - `M	backlog.md`
  - `M	cron/README.md`
  - `M	cron/gecko-alpha.crontab`
  - `M	dashboard/api.py`
  - `M	dashboard/db.py`
  - `M	dashboard/frontend/App.jsx`
  - `A	dashboard/frontend/components/ConvictionTab.jsx`
  - `A	dashboard/frontend/components/ProspectiveWatchlistTab.jsx`
  - `M	dashboard/frontend/components/TGAlertsTab.jsx`
  - `M	dashboard/frontend/components/TradeDetailDrawer.jsx`
  - `M	dashboard/frontend/components/TradeInboxTab.jsx`
  - `A	dashboard/frontend/components/WhatChangedPanel.jsx`
  - `A	dashboard/frontend/components/tradeDecisionBoard.js`
  - `M	dashboard/frontend/components/useSort.jsx`
  - `A	dashboard/frontend/dist/assets/index-B9qlVYP5.js`
  - `A	dashboard/frontend/dist/assets/index-CPVH7gP9.css`
  - `D	dashboard/frontend/dist/assets/index-I-4RT-5X.css`
  - `D	dashboard/frontend/dist/assets/index-bLx0sGFr.js`
  - `M	dashboard/frontend/dist/index.html`
  - `M	dashboard/frontend/style.css`
  - `A	dashboard/frontend/whatChangedFacts.js`
  - `A	dashboard/frontend/whatChangedStorage.js`
  - `A	dashboard/health_status.py`
  - `M	docs/gecko-alpha-alignment.md`
  - `A	docs/offshore_handoff_live_trading_2026_06_21.md`
  - `A	docs/superpowers/plans/2026-06-05-trader-decision-cockpit.md`
  - `M	scout/alerter.py`
  - `M	scout/chains/alerts.py`
  - `M	scout/config.py`
  - `A	scout/conviction/__init__.py`
  - `A	scout/conviction/cross_surface.py`
  - `A	scout/conviction/prospective.py`
  - `A	scout/conviction/prospective_scorer.py`
  - `A	scout/conviction/watchlist_watchdog.py`
  - `M	scout/db.py`
  - `A	scout/gainers/acceleration.py`
  - `M	scout/gainers/tracker.py`
  - `M	scout/ingestion/coingecko.py`
  - `M	scout/live/engine.py`
  - `M	scout/live/kill_switch.py`
  - `M	scout/live/loops.py`
  - `M	scout/main.py`
  - `M	scout/narrative/agent.py`
  - `A	scout/observability/sqlite_holder_watchdog.py`
  - `A	scout/observability/sqlite_maintenance.py`
  - `A	scout/observability/tg_pacing.py`
  - `M	scout/scorer.py`
  - `M	scout/secondwave/detector.py`
  - `M	scout/social/lunarcrush/alerter.py`
  - `M	scout/trading/auto_suspend.py`
  - `M	scout/trading/calibrate.py`
  - `M	scout/trading/cohort_digest.py`
  - `M	scout/trading/entry_snapshot.py`
  - `M	scout/trading/minara_alert.py`
  - `M	scout/trading/paper.py`
  - `M	scout/trading/params.py`
  - `M	scout/trading/signals.py`
  - `M	scout/trading/suppression.py`
  - `M	scout/trading/tg_alert_dispatch.py`
  - `A	scout/trading/trade_surface_alerts.py`
  - `M	scout/trading/weekly_digest.py`
  - `M	scout/trending/tracker.py`
  - `M	scout/velocity/detector.py`
  - `A	scripts/acceleration-heartbeat-watchdog.sh`
  - `A	scripts/audit_clean_price_path.py`
  - `A	scripts/audit_focus_freshness_tradability.py`
  - `A	scripts/audit_missed_gainers.py`
  - `A	scripts/audit_signal_early_usefulness.py`
  - `A	scripts/audit_social_mentions_denominator.py`
  - `A	scripts/backfill_gainers_comparisons.py`
  - `M	scripts/gecko-backup-watchdog.sh`
  - `A	tasks/.hermes-check-receipts/bl-new-conviction-prospective-watchlist.json`
  - `A	tasks/.hermes-check-receipts/p0-part-b-durable-sqlite-maintenance.json`
  - `A	tasks/.hermes-check-receipts/p1-2-tg-pacing.json`
  - `A	tasks/design_api_system_health_status_enum_2026_05_30.md`
  - `A	tasks/design_clean_price_path_audit_2026_05_29.md`
  - `A	tasks/design_cross_surface_conviction_2026_06_12.md`
  - `A	tasks/design_focus_freshness_tradability_audit_2026_05_29.md`
  - `A	tasks/design_gainer_gap_fill_2026_06_02.md`
  - `A	tasks/design_gainer_gap_fill_inc2_2026_06_02.md`
  - `A	tasks/design_missed_winner_surfaced_junk_review_2026_05_30.md`
  - `A	tasks/design_prospective_conviction_watchlist_2026_06_19.md`
  - `A	tasks/design_signal_early_usefulness_scorecard_2026_05_29.md`
  - `A	tasks/design_tg_alert_24h_dedup_2026_05_30.md`
  - `A	tasks/findings_customer_improvement_audit_2026_06_01.md`
  - `A	tasks/findings_first_signal_extend_soak_2026_06_18.md`
  - `A	tasks/findings_live_path_gaps_2026_06_01.md`
  - `A	tasks/findings_missed_gainers_gap_2026_06_02.md`
  - `A	tasks/findings_slow_burn_dispatch_failure_2026_06_18.md`
  - `A	tasks/findings_social_mentions_denominator_evidence_2026_06_01.md`
  - `M	tasks/lessons.md`
  - `A	tasks/plan_dashboard_what_changed_panel_2026_05_30.md`
  - `A	tasks/plan_prospective_conviction_watchlist_2026_06_19.md`
  - `A	tasks/plan_sqlite_durable_maintenance_2026_06_18.md`
  - `A	tasks/plan_tg_alert_operator_action_telemetry_2026_05_31.md`
  - `A	tasks/plan_tg_pacing_2026_06_18.md`
  - `A	tasks/plan_trade_surface_tg_alerts_2026_05_31.md`
  - `M	tasks/todo.md`
  - `M	tests/live/test_kill_switch.py`
  - `M	tests/test_alerter.py`
  - `A	tests/test_alerter_pacing.py`
  - `A	tests/test_alerter_source_labels.py`
  - `M	tests/test_alerter_tg_burst_hook.py`
  - `A	tests/test_audit_clean_price_path.py`
  - `A	tests/test_audit_focus_freshness_tradability.py`
  - `A	tests/test_audit_signal_early_usefulness.py`
  - `A	tests/test_audit_social_mentions_denominator.py`
  - `M	tests/test_backup_rotate_script.py`
  - `M	tests/test_cohort_digest_part2.py`
  - `M	tests/test_coingecko.py`
  - `M	tests/test_config.py`
  - `A	tests/test_conviction_endpoint.py`
  - `A	tests/test_cross_surface_conviction.py`
  - `M	tests/test_dashboard_api.py`
  - `M	tests/test_dashboard_frontend_layout.py`
  - `A	tests/test_dashboard_tg_alert_operator_actions.py`
  - ... (42 more)

## Operator-only gates (reminder)

- Paid APIs/vendor calls, live execution/sizing, pruning/suppression/auto-disable, destructive DB writes/migrations, secrets/external account state require explicit operator approval.

