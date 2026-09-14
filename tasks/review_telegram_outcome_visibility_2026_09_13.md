# Telegram recorded-outcome visibility verification

## Result and scope

Implemented ALR-07's missing descriptive breakdown in the existing Telegram Dispatch Feedback section, without changing sent-alert lists, operator actions, writers, routing or eligibility. Counts come from tg_alert_log; the engine dispatch funnel remains a distinct data source. Fixed outcome buckets and two universe prefixes retain unknown/NULL cases under explicit residual categories. No ratios, token rankings or delivery-success claim.

## Required review sequence

Two parallel plan approvals at83f589b5: structural/time/partition and attribution/ops. Two parallel design approvals at7e048536, same distinct vectors. No required design folds; precision, naive-UTC timestamps, table-wide exclusions and stale finally/error handling were explicit before implementation. Final PR reviews and exact-head CI are pending.

## Verification evidence

- First endpoint test failed404 before implementation. Frontend lifecycle/placement tests failed for absent module/mount before implementation.
- New two-app regression failed because the first app read the second app's database (0 instead1). Root cause: legacy create_app declares global _db_path despite a stale closure comment. New route captures its target per app; regression now passes. Existing routes were not broadly refactored. The separate postmortem PR adopted its own same correction and renewed both reviews.
- 102 tests passed: new endpoint/lifecycle tests plus existing dashboard Telegram frontend guards, funnel/outcomes and dashboard API tests. Executable Node lifecycle checks exercise stale success/error/finally, HTTP200 unavailable,503,network failure,zero success and disposal.
- npm run build passed. Black with explicit py312 target and git diff --check passed. No dependency changes.
- Independent local browser rendering of the real component: table labels/count columns readable after applying existing tg-table styles;1d→30d selection changes counts; empty shows zero/no-recorded-events; unavailable shows retry with no count table. Fixture synthetic, removed and server stopped; this does not attest deployed API or full dashboard health.
- Actual candidate helper executed in memory against production mode-ro SQLite at2026-09-13T23:24:11Z: last30days total1,305 events =76 recorded sent +436 blocked eligibility +793 cooldown. Eligibility split0paper-open universe/436detection universe/0other. Table-wide invalid/future timestamps0/0. One probe51.19ms, not an SLO/performance guarantee. No remote source/config/DB writes or restart.

## Delivery boundary

Open PR, obtain two independent final reviews, fold issues and clearance metadata, await green exact-head CI, then merge normally. Integrate other ready viewer PRs serially and rebuild shared generated assets. No deployment while the separately owned RH/Pons capture migration rollout remains active. Later rollback is a normal viewer revert/rebuild; no database restore or migration.

ALR-07 visibility is the only backlog closure supported. Source/signal suppression, engine exclusions, alert routing and independent delivery verification remain distinct. Permanent automation prompt proposal and owner reconciliation stay in the automation report; no scheduler settings changed.

## Final independent reviews
PR578 reviewed code21886e79162f60da702b51fdfa226fd7bb1e142c: stop_gap_audit approved logic/concurrency after19independent tests; stale_pr_audit approved ops-safety/silent-failure/attribution after102independent tests. No required folds. Clearance anchors reference that actual reviewed code; final metadata-head CI remains pending.

## Resumed integration 2026-09-14
Prior automation owner stopped at a usage limit; root independently verified the failed terminal turn before assuming this PR. Original headad8cf590 CI34789749967 passed. Integration with current master and PR577 remains required, with renewed two-vector reviews and final-head CI.
Fresh focused verification:72tests passed for endpoint, executable frontend lifecycle, existing operator actions, funnel/outcomes and dashboard API. Actual unchanged helper executed in memory against production readonly SQLite at2026-09-14T00:24:18.640965Z:30days1,305 recorded events=76sent+436blocked eligibility+793cooldown. All436eligibility rows are detection-universe; invalid/futuretimestamps0. This is not independent delivery confirmation.
Production srilu HEADd2f0d61e, trackedclean and pipeline/dashboard/Hermes active at00:20:59Z. Full master includes separately scoped capture/schema changes, so this PR does not authorize an unreviewed full production refresh. No production file/DB/config writes or external messages.

Integrated PR577 merge7cbd82dd after exactCI7945passed12skipped and118dashboard checks. Only generated frontend assets conflicted; rebuilt combined source with Vite78modules. Combined Telegram/postmortem/API/navigation suite89passed. No unresolved merge paths. Both renewed reviews and exact final-head CI remain required.
