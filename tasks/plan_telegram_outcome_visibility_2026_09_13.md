# Telegram recorded-outcome visibility plan

**New primitives introduced:** one read-only aggregate endpoint and one panel in the existing Telegram tab. No alert sending, routing, dispatcher, schema or configuration changes.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Gecko Telegram audit outcome aggregation | None identified in inspected directory; Hub catalog did not load | Small repository-specific consumer of the existing ledger |
| Dashboard display / SQLite reads | Existing Gecko primitives | Reuse React panels, FastAPI and _ro_db |

Checked https://hermes-agent.nousresearch.com/docs/skills/ and https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13. The Hub returned a loading shell, so this is not an exhaustive catalog-negative claim. The ecosystem directory exposes orchestration and generic surfaces, not Gecko ledger semantics. Verdict: retain Hermes orchestration and implement only the missing descriptive consumer.

## Drift and runtime evidence

ALR-07's source backlog is the dated, untracked main-checkout tasks/backlog_fable_analysis_2026_07_10.md; its July runtime statistics are not current truth. Fresh base d5b26ff791ff64dac65d77d2a10a841df1c34007. Independent drift audit: tg_alert_dispatch.py:383-408 records eligibility and universe exclusions in tg_alert_log; detection_alert.py:830 records a second detection_lane:universe_filter: prefix. dashboard/api.py:295 exposes only sent rows. DispatchFunnelPanel.jsx:33 is a partial match: its data comes from trade_decision_events, not this ledger. Existing sent-alert endpoint must remain unchanged.

Readonly runtime assumptions checked 2026-09-13T23:10Z: tg_alert_log has outcome/detail/alerted_at. Exact prefix partition of blocked_eligibility is 1,654 detection-universe rows (436 in last 30 days), four paper-open universe rows (zero recent), 328 other/unspecified rows (zero recent). Recent ledger also contains 793 cooldown and 76 recorded sent rows. These are recorded events, not unique tokens, successful delivery receipts, opportunity counts or filter effectiveness. No causal attribution or enable/disable recommendation follows. Initial disposable query used a wrong timestamp name and was corrected to alerted_at; no product failure.

## Scope for plan review

GET /api/tg_alerts/outcomes?days=1, allowing 1..30 days. One read-only transaction; freeze an aware UTC as_of, query [as_of-days, as_of] using SQLite julianday, exclude unparseable and future timestamps. Return generated_at/as_of/window_start, read_only:true, historical_only:true, total recorded events in window, fixed outcome count buckets and a three-way blocked eligibility partition. Known outcome categories reflect current ledger: sent, blocked_eligibility, blocked_cooldown, blocked_dedup_24h, dispatch_failed, announcement_sent, m1_5c_announcement_sent; unfamiliar values aggregate under other_recorded_outcomes. No arbitrary labels or raw detail payloads.

Eligibility partition uses case-sensitive literal prefixes (GLOB) scoped strictly to outcome=blocked_eligibility: universe_filter:* => paper_open_universe; detection_lane:universe_filter:* => detection_universe; everything else => other_or_unspecified. All buckets present with zero counts. Partition sums exactly to blocked_eligibility; all outcomes sum to total. Separate table-wide invalid_timestamp_count and future_timestamp_count make exclusions visible without implying they fall inside the window. Empty table/window is successful zero with explicit copy; missing DB/schema or query error is sanitized503, not zero success. Cache-Control:no-store, unexpected failures logged. No ratios or ranks needed.

Add compact TelegramOutcomePanel within TGAlertsTab without changing its existing inbound/outbound lists or operator actions. Window buttons 1/7/30 days and manual Refresh, no automatic polling. Display Recorded Telegram outcomes, event denominator, exact window, outcome counts and blocked eligibility split. Explain that logged sent is a recorded outcome, not independent delivery confirmation; categories count events, not unique tokens; other/unspecified does not mean proven ineligible. Keep normal loading/empty/unavailable distinct; reject stale success/error/finally responses on window change/unmount with AbortController and request identity. No new packages, no token ranking, no external messages.

## Required sequence

- [x] Fresh branch from current master; independent drift audit and readonly runtime check.
- [x] Two parallel plan reviews and folds.
- [x] Design with two parallel reviews and folds before implementation.
- [x] Tests fail for absent endpoint; implement read-only aggregate/API and panel.
- [ ] Verify prefix partition, time boundaries/timezone equivalence, invalid/future timestamps, unknown outcomes, empty/error distinction, schema absence, readonly DB preservation, sums and no raw details; executable frontend window/error/stale-response behavior; existing Telegram/API/nav regressions and frontend build.
- [ ] PR, two independent PR reviews/folds, exact-head CI, normal merge if authorized checks pass.

## Ownership and release

Root owns feat/telegram-outcome-visibility-20260913 in the f39a worktree. Postmortem child owns a different worktree; shared db.py/api.py integration is sequential at merge and must retain both consumers. Add a separate db helper module if that reduces unnecessary conflicts, preserving _ro_db convention. No deployment while active capture owner controls the migration rollout; later rollback is a normal viewer revert/rebuild with no data restore. This closes ALR-07 visibility only, not engine filtering, alert delivery reliability, routing or operator dispatch gates.

## Review

Plan and design approved by two parallel reviewers each. Implementation and102focused/regression tests passed; see review_telegram_outcome_visibility_2026_09_13.md. Final PR reviews/CI pending.
