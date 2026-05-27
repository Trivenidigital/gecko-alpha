# gecko-alpha — Backlog

## Close-Development Park List 2026-05-22

Operator close-development block 2026-05-22 explicitly parks the following items. **All entries listed below are PARKED — NOT FORGOTTEN.** They remain individually tracked in their existing locations; this section is the consolidated index so future sessions don't accidentally re-scope them.

**Parked until price-coverage / probe decision:**
- `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO` — gated on operator picking Path 2 (CG Pro paid)
- `BL-NEW-SOURCE-CALL-FORWARD-ONLY-COVERAGE` — gated on operator picking Path 3 (GT-free forward-only)
- `BL-NEW-DASHBOARD-SOURCE-CALL-QUALITY-SURFACE` — gated on source_calls having priced outcomes
- `BL-NEW-X-KOL-COST-GOVERNOR` — gated on source_calls X-handle cohort rankability
- Any KOL / TG / X ranking or pruning surface — gated on source_calls coverage

**Actionability data gate status (`n_actionable >= 20 AND n_exploratory >= 5`):**
- CLEARED 2026-05-26: actionable=55, exploratory=16, zero malformed stamped rows. See `tasks/findings_actionability_gate_revalidation_2026_05_26.md`.
- This clears the stale row-count wait, not the policy evidence bar. Actionability v2, suppression, source-quality gate consumption, and classifier changes still require their own fresh drift/runtime re-scope and separate plan/design before implementation.

**Parked until operator decision (no engineering work pending):**
- Operator-alert activation (`BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE` ENDPOINT-SHIPPED / HERMES-SKILL-PENDING — see runbook)
- Social denominator B / C variants
- Cron / revival watchdog scheduling
- First-signal soak until 2026-05-31 gate
- CG Pro paid sample

**Low-priority hygiene only (file when work resumes, do not pre-scope):**
- Scanner exception bounding
- `datetime.utcnow` deprecation cleanup
- `print`/log consistency
- Deploy CRLF / filemode hygiene (BL-NEW-DEPLOY-FILEMODE-CRLF-HYGIENE re-evaluated at next deploy per its own trigger gate)

---

## Open PRs Held for Design Review

### BL-NEW-PR-33-DESIGN-REVIEW-REQUIRED: paper-trade edge detection PR pending design review
**Status:** CLOSED-SUPERSEDED 2026-05-27 - GitHub PR #33 is closed unmerged (`closedAt=2026-05-22T18:13:02Z`). The prior OPEN / DESIGN-REVIEW-REQUIRED text was stale. Revisit only as a fresh first_signal design after the 2026-05-31 first_signal decision gate, not by reviving the stale PR branch.
**Why closed:** implementation predated later cockpit/actionability/autosuspend work and was already marked stale/superseded in `tasks/todo.md`. Current backlog should not route agents to an open-PR design review that no longer exists.
**Action when authorized:** if first_signal survives the 2026-05-31 gate, start a fresh current-base plan/design and re-run drift/runtime checks.
**Hermes-first:** N/A — internal trading logic, no Hermes surface.

---

## Priority Legend
- **P0** — Blocking: must complete before first live run
- **P1** — High: significantly improves signal quality or production readiness
- **P2** — Medium: valuable enhancement, not blocking
- **P3** — Low: nice-to-have, future phase

---

## Current Final Backlog Snapshot (audited 2026-05-26)

This section is the operator-facing backlog after the 2026-05-22 trader-lens review, refreshed against shipped PRs through PR #294. It compresses older dashboard/source-quality items into four tracks so future sessions do not build stale one-off surfaces.

### Track 0 - Hermes + Codex Operating Model (direction of travel)
- `BL-NEW-HERMES-CODEX-OPERATING-MODEL` - make Hermes the durable orchestration/memory/scheduling layer and Codex the repo-grounded execution worker.

**Rule:** Hermes remembers, routes, schedules, and stores gates. Codex reads the repo/runtime, plans, implements, tests, reviews, and opens PRs. Runtime evidence beats both Hermes memory and Codex assumptions.

### Track 1 - Trader Decision Surface (mostly shipped; child work only)
- `BL-NEW-LIVE-DECISION-COCKPIT` - SHIPPED-PARTIAL / PARENT-ARCHIVED after PR #228/#229/#232/#239/#270/#273/#279/#281/#282/#284. Do not rebuild the parent cockpit; use child follow-ups only.
- `BL-NEW-SIGNAL-TRUST-ROADMAP` - PARTIALLY-SHIPPED. Registry/tab shipped in PR #239; scorecards shipped in PR #289. Remaining child work must be scoped from the roadmap below, not from stale PR #276.
- `BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER` - AUDITED-PHANTOM after 2026-05-26 runtime baseline. Do not build until the re-audit trigger fires and paper/tracker overlap proves operator-visible noise.
- `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN` - still gated. Prod soak on 2026-05-27 returned `2026-05-25=50`, `2026-05-26=17`; volume is high but the `>= 3` mature UTC-day gate has not cleared. Recheck on 2026-05-28 UTC.
- `BL-NEW-DECISION-EVENT-WATCHDOG-MULTI-SIGNAL` - SHIPPED-DEPLOYED 2026-05-27 via PR #299 (`876ae5e`). Extends the PR #279 `trade_decision_events` watchdog beyond `gainers_early` to enabled snapshot-backed paper dispatchers, and adds pre-engine decision events for `losers_contrarian` / `trending_catch` filter outcomes.
- `PR #278 Now Tradable counter-risk badges` - CLOSED-SUPERSEDED by PR #290. Trade Inbox is now the primary trader surface and exposes display-only counter-risk context there.
- `PR #280 TG alert parking docs` - CLOSED-SUPERSEDED 2026-05-26. Parking state is represented in this backlog and `tasks/lessons.md`.

**Rule:** V1 is read-only. No live execution, no sizing, no KOL ranking, no source pruning, no automatic signal disable.

### BL-NEW-CROSS-IDENTIFIER-RESOLVER-TRACKER-PAPER: collapse paper/tracker duplicates across identifier forms
**Status:** AUDITED-PHANTOM 2026-05-26 - runtime baseline found `0` current true cross-id candidate pairs; do not implement until the trigger below fires.
**Tag:** `trader-surface` `identity-resolution` `trade-inbox` `top-gainers`
**Why:** PR #281 intentionally dedupes tracker-promoted rows only when `gainers_comparisons.coin_id` matches `paper_trades.token_id`. In prod, many paper rows use contract addresses while tracker rows use CoinGecko ids, so the same asset can appear twice: one `source_corpus=paper`, one `source_corpus=tracker`. Source labels make this decodable but still visually noisy.

**Audit evidence:** 2026-05-26 pre-work runtime baseline found `paper_open=144`, `tracker_36h=70`, same-id overlap `10`, strict symbol+name overlap `10`, and same-symbol different-identifier raw pairs `0`. No current true cross-id candidate cohort exists.

**Re-audit trigger:** reopen only if same-symbol different-identifier candidate rate exceeds `3` per UTC day for two consecutive UTC days, or the operator captures at least `3` visible duplicate rows in one Trade Inbox review window with paper/tracker identity evidence.

**Conditional guardrail:** do not implement a resolver until the trigger fires and the re-audit proves operator-visible noise. If it fires, prefer deterministic provider/contract mapping; do not build a symbol-only merge.

**Non-scope:** no alerting, ranking, source pruning, execution changes, or hidden suppression of unmatched rows.

### BL-NEW-TG-ALERT-QUALIFICATION-DESIGN: qualify Telegram alerts over the complete trader surface
**Status:** GATED / SOAK-METRIC-NOT-YET-AUDITABLE 2026-05-27 - deferred until tracker-to-cockpit promotion soak clears and the unlock metric is measured by a widened/fixed query or recorded daily artifacts.
**Tag:** `telegram` `alerts` `trade-inbox` `decision-support` `anti-scope`
**Why:** Telegram alert qualification should run over the complete decision-support universe. A gate built before tracker-promoted candidates are measured would miss the TOES/BILL/UB-style tracker wins that motivated the trader-surface work.

**Hard dependency:** PR #281 tracker-to-cockpit promotion must remain deployed and the request-independent soak metric must clear: `>= 5` unique tracker-promoted `coin_id`s/day for `>= 3` mature UTC days, measured with `scripts/trade_inbox_tracker_promotion_soak.sql`, or the 14-day calendar backstop must close with an explicit low-volume decision.

**Current runtime gate check:** 2026-05-27 prod run of `scripts/trade_inbox_tracker_promotion_soak.sql` returned `2026-05-25|50` and `2026-05-26|17`. This confirms sufficient daily volume is likely, but the design remains locked. The current SQL scans only `datetime('now', '-36 hours')`, so a single run cannot prove three mature UTC days; it also suppresses tracker rows against current open paper rows, so historical counts can drift. Unlock requires either a widened/fixed SQL query covering at least four UTC days with point-in-time paper state, or three recorded daily artifacts with `run_at`, SQL hash, rows, and this caveat. The 14-day backstop remains 2026-06-09.

**Additional unlock criteria:** the volume floor is necessary but not sufficient. Before opening the alert-qualification design PR, collect and pin: (1) 14-day current Telegram alert volume by surface/gate, (2) an operator-action/noise baseline for existing alerts using available evidence such as acted/ignored/manual notes, with unknowns labeled rather than inferred, (3) per-corpus candidate volume split for paper-backed, tracker-promoted, and other alertable surfaces, and (4) target scarcity budget, initially framed as a maximum daily alert range that compresses the measured candidate baseline before any TG send is added. If any input is unavailable, the design PR must state the missing measurement and keep launch blocked or dashboard-only.

**Design checklist when unlocked:** drift-check all current Telegram alert surfaces; quantify 14-day alert volume and operator-action baseline; pin "qualified" without future-runner lookahead; decide corpus scope; include parse-mode hygiene and dispatched/delivered logs; add an auditable alert-decision event surface if a new writer ships; prove scarcity can compress the observed tracker-promotion baseline before any TG send.

**Anti-scope:** urgency tiers, TRADE_NOW/WATCH_BREAKOUT labels, and alert intent stay out of `/api/trade_inbox`. Default future shape is a separate `/api/trade_alert_intent`-style endpoint. Relax the Trade Inbox firewall only via a deliberate contract PR with new invariants.

### BL-NEW-DECISION-EVENT-WATCHDOG-MULTI-SIGNAL: cover all enabled snapshot-backed paper dispatchers in the decision-event watchdog
**Status:** SHIPPED-DEPLOYED 2026-05-27 - PR #299 merged as `876ae5e` and deployed to srilu. The existing PR #279 watchdog was `gainers_early`-only; this follow-up extends coverage to enabled `losers_contrarian` and `trending_catch` dispatchers without changing trading behavior.
**Tag:** `observability` `trade-decision-events` `silent-failure-prevention` `trader-surface`
**Why:** `trade_decision_events` is now the structured substrate for explaining why a signal reached or missed trade dispatch. If `losers_contrarian` or `trending_catch` dispatcher instrumentation breaks, a gainers-only watchdog would stay green while those surfaces silently lose decision attribution.
**Action:** Extend `scripts/check_trade_decision_events.py` with an enablement-aware static source mapping for `gainers_snapshots`, `losers_snapshots`, and `trending_snapshots`, excluding already-open paper rows to mirror dispatcher eligibility; add pre-engine fail-soft decision events for `losers_contrarian` / `trending_catch` filter and suppression branches; keep existing cron line and table schema unchanged.
**Guardrails:** skip checks when `TRADING_ENABLED=False` or per-signal paper dispatch flags are disabled; do not emit misleading `missing_price` dispatcher events; no new alerting, ranking, urgency tiers, signal policy, DB table, or cron line.
**Deploy smoke:** `scripts/check_trade_decision_events.py --db /root/gecko-alpha/scout.db --lookback-minutes 15` returned `ok=true`, `status=ok`, with `trending_catch` source rows and decisions both `15`; stale pre-restart loser rows aged out cleanly.

### Track 2 - Source/KOL Measurement Enablers (gated)
- `BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE` - next authorized probe. Determines whether GT free can recover old source-call OHLCV if pool-at-call selection is fixed.
- `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` - implementation remains gated until sample/probe evidence proves temporal integrity, trust-tier labels, and chain identity.
- `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` - needed because most TG/X calls do not yet carry vendor-resolvable identity.
- `BL-NEW-GT-CHAIN-MAP-EXTENSION` - small follow-up after the solana/ethereum/base path proves useful.

**Rule:** TG/X and KOLs stay context-only until this track makes source-call outcomes rankable.

### Track 3 - Data-Gated Strategy Evidence (actionability gate cleared; re-scope before build)
- `BL-NEW-X-OUTCOME-LINKAGE`, `BL-NEW-TG-OUTCOME-LINKAGE`, and `BL-NEW-NO-PEAK-RISK-HANDLING` - no longer blocked by the `20/5` actionability row-count gate as of `tasks/findings_actionability_gate_revalidation_2026_05_26.md`, but each still needs a stale-PR/current-base triage pass before action: fetch current master, check for shipped overlap, rebase/re-scope as needed, and re-verify runtime assumptions before implementation.
- Re-scope gates: X linkage requires current-base drift plus unresolved/priced X counts; TG linkage requires current direct-FK/linkage-state counts and source-call overlap; no-peak risk requires current-regime replay, peak/giveback coverage, and an explicit `pre_entry_giveback_ratio IS NOT NULL` guard. The actionability row-count finding authorizes triage, not implementation.
- `BL-NEW-COHORT-DIGEST-DECISION`, first_signal 2026-05-31 revival decision, and revival criteria follow-ups - data-bound decisions, not calendar-driven build work.
- Social-denominator Option B/C - operator-choice item; now should be evaluated through the signal-trust roadmap rather than as an isolated scorer tweak.

### Track 4 - Ops Hygiene (do opportunistically)
- Scanner exception bounding, `datetime.utcnow` cleanup, print/log consistency, MiroFish DEBUG noise suppression.
- Watchdog meta-watchdog / cron drift scheduling / stale reminder items.
- Held-position fallback and stale-count alerts if price freshness again blocks trader-facing decisions.

### Folded / Parked By This Review
- Source-call quality dashboards, TG/X leaderboards, KOL cost governor, and source pruning are parked behind price coverage + signal trust.
- Token confluence, peak-giveback badges, and health-to-trader-impact are folded into the live decision cockpit and entry-quality layer.
- X-alerts timeout/index backlog is stale after PR #213/#215 unless fresh runtime evidence shows a regression.
- Duplicate source-call cron-tick watchdog entry is superseded by PR #211/#216/#217/#218 deployment.

### BL-NEW-HERMES-CODEX-OPERATING-MODEL: durable orchestration with Codex execution workers
**Status:** PROPOSED 2026-05-22 - filed from operator strategy direction after Hermes+Codex architecture review. This is the working model for Gecko-Alpha going forward: Hermes is the orchestration/memory/scheduling layer; Codex is the coding/repo/runtime execution worker; the operator owns product and trading judgment.
**Tag:** `operating-model` `hermes-first` `codex-worker` `workflow` `memory`
**Why:** Gecko-Alpha has enough code velocity. The failure modes to solve are continuity, stale backlog, operator-gated decisions, evidence-bound soaks, runtime-state drift, and multi-session handoff. Hermes is the right layer for durable memory, scheduling, routing, and cross-session state. Codex is the right layer for repo-grounded plans, implementation, tests, runtime probes, PRs, and code review.

**Role split:**

| Layer | Owner | Notes |
|---|---|---|
| Durable goals / backlog memory | Hermes | Stores project state, parked items, gates, and next prompts |
| Task routing / overnight orchestration | Hermes | Launches bounded Codex jobs; stores final artifacts |
| Runtime reminders / soak wakeups | Hermes | Schedules data-bound rechecks and operator prompts |
| Repo-grounded plan/design | Codex | Reads code, tests, backlog, runtime evidence; writes PR artifacts |
| Implementation / refactor / debug | Codex | Edits files, runs tests, opens PRs |
| PR review | Codex + reviewer agents | 2-vector review minimum for important work |
| Product/trading judgment | Operator | Approves vendor samples, prod writes, live-trading changes, sizing, pruning |
| Truth source | Runtime evidence | DB/journal/CI/prod state beats remembered state |

**Canonical task workflow:**
1. Operator sets direction.
2. Hermes creates or updates durable task state.
3. Hermes dispatches Codex with the exact bounded prompt.
4. Codex runs: Plan -> 2 parallel reviewers -> folds -> Design -> 2 parallel reviewers -> folds -> Build -> PR -> 2 parallel PR reviewers -> folds -> verification.
5. Codex returns PR/findings/no-build decision.
6. Hermes stores the outcome, next gate, and next prompt.

**Guardrails:**
- Hermes-first does not mean Hermes-builds-everything. It means drift-check -> Hermes ecosystem check -> custom code only for residual gaps.
- Hermes memory is not truth. Every build prompt must verify runtime assumptions before acting when DB rows, env vars, feature flags, approvals, or prod state matter.
- No autonomous prod DB writes, vendor paid calls, live execution, sizing, source pruning, or signal retirement without explicit operator gate unless already covered by a shipped watchdog/runbook.
- Every overnight job must end in one of: PR, findings doc, or explicit no-build decision with evidence.
- Codex work should stay branch/PR/test based; Hermes work should stay durable-state/scheduler based.

**Immediate application:** Use this model for `BL-NEW-LIVE-DECISION-COCKPIT` and `BL-NEW-SIGNAL-TRUST-ROADMAP`. Hermes tracks gates and memory; Codex builds the endpoint/panel/registry through PRs; the operator approves any move toward live execution, pruning, or paid vendor samples.

## Active Work: 2026-05-20 source-call outcome ledger

### BL-NEW-SOURCE-CALL-OUTCOME-LEDGER: durable TG/X per-call outcome substrate
**Status:** SHIPPED 2026-05-20 - PR #206 merged (`aaffa6b0`) and PR #207 deployed/wired live writer + lag watchdog (`df76d851`).
**Tag:** `source-quality` `tg_social` `x_alerts` `measurement-substrate` `hermes-first`
**Why:** Operator wants quality over quantity from TG/X sources: repeated noisy TG calls and expensive X/KOL calls need evidence at the source level. Existing tables store source events (`tg_social_signals`, `narrative_alerts_inbound`) and paper trades, but no durable per-call ledger ties each call to forward returns, duplicate clustering, missing-data reasons, and paper-trade linkage.
**Action:** Ship `source_calls` sidecar table, idempotent TG/X backfill helper, bounded forward-window outcome computation from existing CG snapshot tables, low-n/coverage-aware summary helper, and §12a lag watchdog. No trading behavior changes, no source suppression, no dashboard endpoint in this PR.
**Hermes-first summary:** use Hermes for X/KOL collection/classification; keep custom durable attribution and summary because no Hermes skill owns gecko-alpha source-call outcomes. Full table in `tasks/plan_source_call_outcome_ledger_2026_05_20.md` and `tasks/design_source_call_outcome_ledger_2026_05_20.md`.

| State | Current evidence |
|---|---|
| MERGED | PR #206 merge commit `aaffa6b0`; PR #207 merge commit `df76d851` on `origin/master` |
| DEPLOYED | srilu `/root/gecko-alpha` HEAD `df76d85` |
| BACKFILLED | prod `source_calls` has 1,254 rows as of 2026-05-21 check |
| WRITER-LIVE | prod crontab has `*/5 * * * * /root/gecko-alpha/scripts/source-calls-live-writer.sh` |
| WATCHDOG-LIVE | prod crontab has `*/10 * * * * /root/gecko-alpha/scripts/source-calls-lag-watchdog.sh` |
| PRICE-COVERAGE-LIMITED | prod `source_calls`: 14 `price_at_call`, 0 rows with 1h/6h/24h forward pct; handled by `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` |

### BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG: detect writer cron outages independently of upstream traffic
**Status:** SHIPPED-DEPLOYED 2026-05-21 — PR #211 merged + activated on srilu-vps. Heartbeat advancing every 5min via cron; lag-watchdog quiet (writer-heartbeat branch active, status=ok). 3 hotfix PRs co-shipped: #216 (`Settings` field declaration, crash-loop fix), #217 (wrapper sources `.env` for cron's sparse env), #218 (env-source ordering so `DB_PATH=scout.db` from `.env` doesn't override the absolute fallback).
**Tag:** `observability` `silent-failure-prevention` `cron` `section-12a`
**Why:** `source-calls-lag-watchdog` reads `MAX(upstream_ts) - MAX(source_calls_ts)` — Class-1 silent-failure (CLAUDE.md §12a) when writer cron stops AND upstream is also quiet (both timestamps stale → lag delta stays small → no alert). Writer is idempotent and fires on empty upstream too, so writer-staleness IS independently detectable via heartbeat.
**Action:** Add `--heartbeat-file` to `scripts/source_calls_live_writer.py` (touch on exit 0). Add `--writer-heartbeat-file` / `--writer-threshold-minutes` to `scripts/check_source_calls_lag.py` with three statuses: `writer_stale` (mtime > threshold), `writer_heartbeat_missing` (file absent + ledger has rows), `writer_heartbeat_pending` (file absent + empty ledger; alert-suppressed first-run with 6h escalation to `writer_never_fired`). Wire through `source-calls-lag-watchdog.sh` with differentiated plain-prose alert text + §12b `*_alert_dispatched/delivered/failed` log triplet.
**Hermes-first:** KEEP_CUSTOM — checked Hermes skill hub (`hermes-agent.nousresearch.com/docs/skills`) and `awesome-hermes-agent` topic; no cron/heartbeat/cadence-monitoring skill exists. webhook-subscriptions is event-driven push, not pull-cadence; weights-and-biases is MLOps experiment logging. Project-internal observability of a project-internal Python writer.
**Cost:** ~120 LOC additive (no new bash script, no new cron line, no new state dir beyond writer-heartbeat path). 16 new tests across 3 test files; 5 pre-existing lag-watchdog tests preserved.
**Activation:** operator sets `WRITER_HEARTBEAT_FILE=/var/lib/gecko-alpha/source-calls/writer-heartbeat` in `/root/gecko-alpha/.env` + `mkdir -p /var/lib/gecko-alpha/source-calls && chmod 0755 /var/lib/gecko-alpha/source-calls`. No `crontab` reload needed (no managed-block change).
**Kill criterion:** 2026-08-21 (90 days post-deploy) — revert via `unset WRITER_HEARTBEAT_FILE` if zero real `writer_stale`/`writer_heartbeat_missing`/`writer_never_fired` fires by that date; treat as infra-debt per CLAUDE.md §10. Operator may extend if observed reliability data warrants.
**Files:** `scripts/source_calls_live_writer.py`, `scripts/source-calls-live-writer.sh`, `scripts/check_source_calls_lag.py`, `scripts/source-calls-lag-watchdog.sh`, `tests/test_source_calls_live_writer.py`, `tests/test_check_source_calls_lag.py` (new), `tests/test_source_calls_lag_watchdog.py`, `tasks/plan_source_call_cron_tick_watchdog.md`, `tasks/design_source_call_cron_tick_watchdog.md`.

### BL-NEW-DASHBOARD-X-ALERTS-TIMEOUT-FIX: batched entry-price preload for /api/x_alerts
**Status:** SHIPPED-DEPLOYED 2026-05-21 — PR #213 merged; combined with PR #215 (`UPPER(symbol)` functional indexes on `volume_history_cg` + `gainers_snapshots` to fix the dominant resolver SCAN that masked the entry-price batching win in post-deploy smoke). **Measured prod timing: limit=80 9.30s → 0.18s p50 (~50× faster).** No SQLite SCAN of the 2.5M-row volume_history_cg per resolved cashtag.
**Tag:** `dashboard` `performance` `read-only` `visibility-only`
**Why:** Prod 2026-05-21 timing: limit=10 → 1.47s, limit=40 → 5.73s, limit=80 → 9.30s. Pre-fix code ran O(N rows x 5 queries) entry-price lookups; aiosqlite serializes per-connection, so latency scaled linearly with limit and tripped the frontend 5s abort timeout (added in PR #190). Indexes `(coin_id, time_col)` already exist on all 5 source tables — the bottleneck was query *count*, not query cost.
**Action:** Restructure `get_x_alerts` into two passes: (1) per-row coin_id resolution (existing `_resolve_coin_id_for_outcome` cached path), (2) one-shot `_preload_entry_price_data(coin_ids, window_lo, window_hi)` issuing 5 queries with `coin_id IN (?,...)` + global window, then per-row outcomes use the prebuilt `dict[coin_id, list[(ts, price)]]`. Preserves exact closest-prior-or-earliest-after semantics. No new index. No schema change. No new endpoint. No behavior change.
**Tests:** 7 pre-existing tests preserved. 2 new regression guards: `test_x_alerts_entry_price_uses_batched_query_per_source` (asserts exactly 1 SELECT per source table for 5 rows referencing same coin_id), `test_x_alerts_entry_price_skips_preload_when_no_resolved_coins` (defensive: `IN ()` would be a syntax error).
**Expected impact:** At limit=80, query count drops from ~400 entry-price queries to 5 entry-price queries. Per-row remaining cost is 4 coin_id-resolver queries × unique-symbol count (already cached) + 1 current_price query × unique coin_ids (already cached). Target <2s at limit=80 post-deploy.
**Validation runbook:** post-deploy on srilu-vps: `time curl -s -o /dev/null -w "http=%{http_code} time=%{time_total}s\n" "http://localhost:8000/api/x_alerts?limit=80"`. Pre-fix baseline 9.3s. Acceptance: <2s p50.
**Hermes-first:** N/A — pure local query optimization on existing read-only endpoint.
**Files:** `dashboard/db.py` (refactor get_x_alerts), `tests/test_x_alerts_dashboard.py` (2 new regression tests).

### BL-NEW-DASHBOARD-SOURCE-CALL-HEALTH: read-only aggregate health endpoint for source_calls
**Status:** SHIPPED-DEPLOYED 2026-05-21 — PR #214 backend (`GET /api/source_calls/health`, ~0.03s p50) + PR #219 frontend cockpit panel (`SourceCallsHealthPanel.jsx` in `HealthTab`, 30s auto-refresh). Operator gate intact: no per-source identifiers in API response; rankability rollup only.
**Tag:** `dashboard` `read-only` `visibility-only` `operator-cockpit`
**Why:** The cockpit needs a single endpoint where the operator can see "what's going on with source_calls" — row count, unresolvable rate, duplicate rate, outcome status distribution, price coverage by horizon, writer freshness, rankability rollup. Pre-fix the operator had to read backlog entries + run sqlite3 ad-hoc on prod. Now: one curl call.
**Operator gates respected:** NO per-source ranking exposed. `rankability` is a rollup (`source_count`, `rankable`, `insufficient_sample`, `biased_low_coverage`) plus a `not_rankable_label` prose field. Regression test `test_health_endpoint_does_not_expose_per_source_ranking` asserts no source id leaks into the response.
**Hermes-first:** N/A — pure local read-only summary of the existing source_calls table; uses existing `scout.source_quality.ledger.compute_source_quality_summary` helper. No external service.
**Action:** New helper `dashboard.db.get_source_calls_health(db_path)` builds the aggregate dict. Wired into `dashboard/api.py` as `GET /api/source_calls/health`. Defensive on `schema_missing` (fresh DB / pre-PR-#206 rollback). 5 tests cover empty-state, aggregate stats, no-per-source-leak, "not rankable yet" gate label, writer freshness.
**Follow-up:** `BL-NEW-DASHBOARD-SOURCE-CALL-QUALITY-SURFACE` covers eventual frontend panel; this endpoint provides the backend.
**Files:** `dashboard/db.py` (+~180 LOC), `dashboard/api.py` (+18 LOC endpoint stub), `tests/test_source_calls_health_endpoint.py` (new, 5 tests).

### BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-INDEX: functional `UPPER(symbol)` indexes (PR #215)
**Status:** SHIPPED-DEPLOYED 2026-05-21 — PR #215 merged + migration auto-applied at next gecko-pipeline restart. Two new functional indexes: `idx_vol_hist_cg_symbol_upper`, `idx_gainers_snap_symbol_upper`. Both via the `_migrate_*` pattern + `paper_migrations` idempotency.
**Why surfaced separately from PR #213:** post-deploy smoke of #213 revealed the batched entry-price preload worked as designed but x_alerts at limit=80 still ran 9-16s. EXPLAIN QUERY PLAN confirmed `_resolve_coin_id_for_outcome` SCANned the 2.5M-row `volume_history_cg` per cashtag (~360ms × ~30 cashtags ≈ 10s of the budget). `UPPER(symbol) = ?` is non-sargable against the `(coin_id, time_col)` indexes. Filed + shipped same session.
**Tag:** `dashboard` `performance` `migration` `visibility-only`
**Net post-fix:** 0.20s p50 (vs 9.30s pre-fix; ~45× speedup).

### BL-NEW-DASHBOARD-SOURCE-CALL-HEALTH-PANEL: V1 frontend cockpit panel (PR #219)
**Status:** SHIPPED-DEPLOYED 2026-05-21 — PR #219. `dashboard/frontend/components/SourceCallsHealthPanel.jsx` slotted into `HealthTab` above the existing stat bar. Consumes `/api/source_calls/health`. 30s auto-refresh.
**Surfaces:** writer freshness badge (fresh/lagging/STALE), row counts + tg/x split, unresolvable_rate + duplicate_rate (color-coded), outcome status distribution, price coverage by horizon (5 proportional bars), rankability rollup banner with `not_rankable_label` rendered verbatim from backend.
**Operator gate respected:** zero per-source identifiers in component output; the API itself guards via `test_health_endpoint_does_not_expose_per_source_ranking`. Caption explicitly states "per-source ranking deliberately not shown".
**Tag:** `dashboard` `frontend` `visibility-only` `operator-cockpit`
**Vite build artifacts committed:** `dist/index.html` + `dist/assets/index-nifTXfDA.js` + `dist/assets/index-C5u4mHYq.css` (per CLAUDE.md memory `feedback_vite_dist_index_html_commit_discipline`).
**Follow-up:** `BL-NEW-DASHBOARD-SOURCE-CALL-QUALITY-SURFACE` remains for any future per-source ranking surface (data-gated on source-call coverage; not this iteration).

### BL-NEW-DASHBOARD-SOURCE-CALL-QUALITY-SURFACE: dashboard surface over `source_calls`
**Status:** FOLDED / PRICE-COVERAGE-GATED 2026-05-22 - original per-source ranking surface is now parked behind `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` and folded into `BL-NEW-SIGNAL-TRUST-ROADMAP`. The shipped `/api/source_calls/health` + Health panel cover aggregate visibility; any future per-source quality surface must wait until source-call outcomes are rankable.
**Tag:** `dashboard` `source-quality` `read-only` `trader-cockpit`
**Why:** The ledger is a substrate, not a trader-facing cockpit. The trader still needs a view that answers: which TG channels/X handles are rankable, which are noisy repeaters, which have low coverage, and which are linkage-pending versus actually bad.
**Action:** Add read-only endpoint(s) and dashboard panel(s) backed by `compute_source_quality_summary`, with explicit low-n, biased-low-coverage, unresolvable, duplicate-rate, and linkage-confidence labels. Extend rather than duplicate existing `TGAlertsTab` / `XAlertsTab`.
**Decision-by:** after price coverage materially improves and at least one source reaches min_sample=10 with coverage >=0.50.

### BL-NEW-X-KOL-COST-GOVERNOR: evidence-backed X/KOL pruning and budget guardrails
**Status:** PARKED-PENDING-SOURCE-RANKABILITY 2026-05-22 - keep as a strategy concern, but do not build as a standalone governor. It is downstream of source-call price coverage, signal trust, and operator-approved source measurement.
**Tag:** `x_alerts` `cost-governance` `kol-list` `evidence-gated`
**Why:** X is a paid input stream. Underperforming handles should not remain indefinitely, but pruning before the source-call ledger has enough rankable coverage risks cutting useful discovery sources based on noise.
**Action:** After `source_calls` has rankable X-handle cohorts, design a review workflow for prune/keep/watch decisions. Require sample/coverage gates and operator approval; no automatic handle removal in the first pass.
**Decision-by:** evidence-gated on source-call ledger coverage, signal-trust registry maturity, and operator cost tolerance.

## Active Findings

### BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT: vendor sample track for CG/GT
**Status:** SAMPLE-RUN-FAILED-WITH-CORRECTION 2026-05-22 — sample 2/7 criteria failed AND follow-up lookback-cap probe REFUTED the original "GT free has short lookback cap" interpretation. GT free supports ≥180d historical OHLCV on at least one pool. Real blocker is historical-pool-selection (V1 `current_reserve_proxy_v1` picks today's top-reserve, which may not have existed at `call_ts`). See `tasks/findings_source_call_gt_sample_2026_05_22.md` §11 correction. **Pool-selection probe 2026-05-22 returned NEGATIVE within 8-call budget — see `tasks/findings_historical_pool_selection_probe_2026_05_22.md`.** Implementation gate STAYS CLOSED. Rationale now: "even with smarter pool selection, GT free does not cover the old corpus." Operator path decision required: Path 2 (CG Pro, paid) vs Path 3 (forward-only, no cost).
**Why:** PR #220 found GoldRush `historical_by_addresses_v2` is daily-only. Operator leans path C (CG/GT). This BL files the CoinGecko MCP / GeckoTerminal evaluation through Hermes-first lens + produces operator-facing decision packet.
**Action:** Docs-only PR. Adds plan (`tasks/plan_source_call_price_coverage_sample_cg_gt_2026_05_21.md`), design (`tasks/design_source_call_price_coverage_sample_cg_gt_2026_05_21.md`), decision packet (`tasks/vendor_sample_decision_packet_cg_gt_2026_05_21.md`), `.gitignore` entry for `tasks/vendor_samples/`. No code. No vendor calls. No prod DB writes.
**Hermes-first:** KEEP_CUSTOM — Hermes skill hub + awesome-hermes-agent ecosystem + srilu-installed skills (20+) all checked, none own OHLCV/historical-price. CG MCP server is an MCP protocol surface, not a Hermes-managed skill. GT public API (free, no key, 30 req/min) recommended as first sample.
**Critical findings surfaced:**
- 30m is NOT a native candle interval; design pre-registers 30m as a *return* between two 5m candles, NEVER an OHLCV composite.
- GT's `reserve_in_usd` is current-state, not historical at call_ts → pool selection is honestly a "current_reserve_proxy_v1" rule with explicit drift caveat.
- Only ~15% of source_calls (202 dex:chain:contract rows) are EVER eligible regardless of vendor; recommendation: ACCEPT 202-row V1 ceiling + file `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` upstream.
- 7mo source-call corpus; sample probes GT lookback boundary via oldest-token-day; failure → narrowed eligibility, operator-decided.
**Files:** `tasks/plan_*.md`, `tasks/design_*.md`, `tasks/vendor_sample_decision_packet_cg_gt_2026_05_21.md`, `.gitignore`.

### BL-NEW-SOURCE-CALL-GT-LOOKBACK-CAP-PROBE: empirically establish GT free's historical cap
**Status:** PROBE-RUN-REFUTED 2026-05-22 — operator ran the 3-call probe; GT free returned 200 with 137 / 110 / 239 candles at 180d / 120d / 60d back on the same CIPHER pool that originally 401'd. The "lookback cap" hypothesis is **REFUTED**. GT free supports ≥180d depth. See `findings_source_call_gt_sample_2026_05_22.md` §11.
**Closed-with-result:** the 401 on the original 2025-10-20 sample wasn't a global free-tier cap. The real blocker is historical-pool-selection — today's top-reserve pool ≠ call_ts's primary pool for old `source_calls`. Continues as `BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE`.

### BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE: pool-at-call identity for old source_calls
**Status:** PROBE-RUN-NEGATIVE 2026-05-22 — operator authorized + ran 8/10-call probe; **0 of 5 informative pool-probes cover call_ts** within budget. Token A (CIPHER, 7mo): 3 oldest pools (all pre-call_ts created) returned HTTP 401; Token B (4mo): 2 oldest pools have data but only from ~12-15d *before* call_ts; Token C (3mo): rate-limited (429) on `/pools`. Recommendation: PARK historical GT backfill. See `tasks/findings_historical_pool_selection_probe_2026_05_22.md`.
**Why:** GT free works for ≥180d OHLCV (lookback-cap probe). The V1 `current_reserve_proxy_v1` pool-selection rule blindly picks today's top-reserve pool, which may not have existed at `call_ts` for old source_calls — that's the real cause of the original 401. For old source-calls, ANY pool with OHLCV coverage at `call_ts` may exist among the 20 pools returned by `/tokens/{address}/pools`, but the V1 rule discards them in favor of today's primary.
**Findings (2026-05-22, this PR):**
- Token A pattern is structurally informative: 3 oldest pools (all created in 2025-09, weeks BEFORE call_ts) returned HTTP 401. Consistent with "GT free does not index OHLCV for old/abandoned pools at all," not "we picked the wrong pools." Probing the other 17 unlikely to improve.
- Token B pattern is structurally informative: 2 oldest pools have candles, but only from ~14d before call_ts (pools went inactive before the call). `before_timestamp` was honored — GT returned the most-recent 12 candles, which happened to be 12d pre-call.
- Token C inconclusive (rate-limited); re-probe would consume new budget but unlikely to overturn the directional read.
**Cost:** Spent 8 of 10 authorized GT public API calls. Free, no API key.
**Closes-vs-keeps-open:**
- BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO becomes MORE compelling (Path 2 paid → only remaining historical-rescue path).
- BL-NEW-SOURCE-CALL-FORWARD-ONLY-COVERAGE remains the no-cost option (Path 3).
- This BL is **closed-with-result** by `findings_historical_pool_selection_probe_2026_05_22.md`. Operator picks Path 2 or Path 3.

### BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO: evaluate CG Pro's lookback vs GT free
**Status:** PROPOSED 2026-05-22 — only file detailed packet if operator picks Path 2 of `findings_source_call_gt_sample_2026_05_22.md`.
**Why:** GT free failed criterion 7 (oldest lookback). If CG Pro has deeper history, the older `source_calls` corpus becomes recoverable. Open question: is CG Pro's data the same GT data behind a Pro paywall (no lookback advantage), or does CG Pro maintain longer history independently?
**Action:** Future packet (mirror of PR #222 shape) targeting CG Pro `/onchain/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}`. Includes cost commitment (~$129/mo Analyst tier; verify current pricing). Sample call requires paid Pro API key.
**Cost:** ~$129/mo subscription + 1 sample call. Operator pre-budget.
**Trigger condition:** evidence-gated on operator picking Path 2.

### BL-NEW-SOURCE-CALL-FORWARD-ONLY-COVERAGE: GT-free backfill from now onward, accept zero historical
**Status:** PROPOSED 2026-05-22 — only plan if operator picks Path 3 of `findings_source_call_gt_sample_2026_05_22.md`.
**Why:** GT free works for recent tokens. Treating coverage as forward-only (new `source_calls` rows get full coverage; older rows accept "no forward coverage" permanently) avoids vendor cost AND avoids waiting on identity-resolution upstream BL.
**Action:** Plan + design + impl PR for `source_call_price_observations` write path that only fires on new rows. Schema records `coverage_eligibility_anchor_ts` so downstream consumers know "rows older than X have no coverage by design."
**Cost:** Implementation effort (~design from parent PR #208 + GT-specific code). No vendor cost.
**Trade-off:** Dashboard's `not_rankable_label` takes months to flip — coverage grows organically only on new rows. Acceptable IF operator prioritizes speed-of-shipping over historical-backfill.
**Trigger condition:** evidence-gated on operator picking Path 3.

### BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION: resolve NULL / "(unresolved)" / non-dex source_calls token_ids
**Status:** PROPOSED 2026-05-21 — surfaced by CG/GT sample track. Blocks dashboard's `not_rankable_label` from flipping to "rankable."
**Why:** Of 1323 `source_calls` rows, only 202 (~15%) carry `dex:chain:contract` IDs that any vendor can backfill. 460 are NULL token_id, 578 are literal "(unresolved)", 83 are misc coin_ids. The vendor-side coverage problem cannot be solved by a better vendor — the upstream identity-resolution problem is the real blocker.
**Action:** Future plan/design. Investigate why TG/X scrapers don't resolve contract addresses for ~85% of mentions. Probably needs (a) regex extraction improvement, (b) coin_resolver Hermes skill integration, (c) DexScreener / token-page lookup fallback. Out of scope for the CG/GT sample track.
**Trigger condition:** evidence-gated. Plan starts after the CG/GT sample track produces ≥1 successful row in `source_call_price_observations` — i.e., when the vendor side is unblocked AND the identity side becomes the binding constraint.
**Hermes-first:** likely use existing `coin_resolver` skill (already installed on srilu). KEEP_CUSTOM only for the gecko-alpha-side glue.

### BL-NEW-GT-CHAIN-MAP-EXTENSION: extend `_geckoterminal_network_for_chain` to cover bsc / monad / hyperevm
**Status:** PROPOSED 2026-05-21 — surfaced by CG/GT sample track Reviewer A.C2.
**Why:** Source_calls `dex:*` rows include bsc=19, monad=2, hyperevm=1 which the in-tree GT chain mapper doesn't cover. Sample script will raise explicitly on these; production backfill would silently skip them.
**Action:** Add network mappings (`bsc → "bsc"`, `monad → "monad"` if GT supports, etc.) to `scout/ingestion/geckoterminal.py:_geckoterminal_network_for_chain`. Verify each via a one-off GT `/networks` discovery call (free, ~2 calls).
**Trigger condition:** evidence-gated. File after the CG/GT sample passes for solana/ethereum/base; the 22 affected rows are small enough that V1 backfill can proceed without them.

### BL-NEW-DEPLOY-FILEMODE-CRLF-HYGIENE: stop chmod/line-ending churn from blocking pulls
**Status:** PROPOSED 2026-05-21 — surfaced during PR #220 deploy. Build-decision gate: re-evaluate if the same friction occurs on the next deploy; if it does, this is a real recurring blocker worth fixing. Otherwise, leave PROPOSED.
**Why:** Two chronic friction sources during every deploy to srilu-vps:
1. **Exec bits.** `scripts/source-calls-live-writer.sh`, `scripts/source-calls-lag-watchdog.sh`, `scripts/source_calls_live_writer.py` are stored in the repo at mode `100644`. Cron requires `0755`. Prod has them at `0755` from previous deploys, so `git pull` reports them as locally modified (mode-only diff) and the pull is blocked until `git stash` or `git checkout --`. After every deploy I have to `chmod +x` them again.
2. **CRLF on dist/index.html.** `core.autocrlf` rewrites the file on checkout; `git status` then reports it as modified. Pull blocked. Resolution required `rm` of the file so git restores it cleanly from the incoming master.
**Action (when authorized):**
- `git update-index --chmod=+x scripts/source-calls-live-writer.sh scripts/source-calls-lag-watchdog.sh scripts/source_calls_live_writer.py` (one-time normalize).
- Add `.gitattributes` entry: `dashboard/frontend/dist/*.html text eol=lf` (or `binary` if line endings are irrelevant).
- Smoke: clean deploy without `stash` / `rm` workarounds.
**Hermes-first:** N/A — repo hygiene, no external service.
**Drift-check:** No existing `.gitattributes` (or it doesn't cover these patterns). One-time normalize is the canonical fix; this isn't a per-deploy task.
**Trigger condition:** re-evaluate at next deploy. If pull conflicts on the SAME files for the SAME reasons, the cost-of-fix (~10 LOC) is justified. If not (e.g., next deploy is clean because no script edits), defer indefinitely.

### BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX
**Status:** PARTIAL-SHIPPED 2026-05-20 — Step 1 instrumentation deployed on srilu-vps (commit `3af48d9`); the actual Step 4 timeout/runtime fix is filed as separate follow-up `BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY` pending operator decision on which path (parallelize vs extend) based on the empirical evidence.
**Why:** Hermes narrative scanner cron (`gecko-x-narrative-scanner`) hits the 120s `_get_script_timeout()` budget on busy cycles. Pre-instrumentation evidence: ~40% of recent cycles exceeded 120s. The pre-existing log only emitted `Duration: 136.0s` without per-stage attribution — making any fix shape speculative.
**Original target name:** `BL-NEW-HERMES-NARRATIVE-CRON-PROMPT-INJECTION-FIX` was stale (last actual prompt-injection block was 2026-05-15 14:00 UTC; resolved by the May 15 refactor to `no_agent: true` shell-script mode). Renamed per `memory/feedback_jobs_json_canonical_for_cron_diagnosis.md` — jobs.json `last_error` is the canonical current state.
**Evidence (Step 1 first instrumented cycle, 2026-05-20T04:00:53Z, full final state):** 4 `SCANNER-STAGE-START` + 4 `SCANNER-STAGE-TIMING` + 1 `SCANNER-CYCLE-SUMMARY`. Total cycle duration 234.28s. Per-stage timing: kol-watcher 12.11s, narrative-classifier **222.14s (95% of total, BOTTLENECK CONFIRMED)**, coin-resolver 0.00s, narrative-alert-dispatcher 0.02s. 2 alerts dispatched. Non-trivial operational insight: `jobs.json` records `last_status=error` ("timed out after 120s") but the Python sub-subprocess survives the wrapper SIGTERM (orphaned to PID 1), continues writing to its open file descriptor, and completes the work — the cron timeout flag is currently misleading. (Initial pre-cycle-completion read showed 2/1/0 due to log capture mid-write; corrected post-cycle.)
**Scope (this PR — docs-only):**
- VPS instrumentation deployed to `/home/gecko-agent/run-scanner-cycle.py` + wrapper `gecko_x_narrative_scanner.sh`
- Per-stage JSON-encoded structured emits (`SCANNER-STAGE-START` / `SCANNER-STAGE-TIMING` / `SCANNER-CYCLE-SUMMARY`)
- OpenRouter 4xx/5xx counters + `classification_other_error` for invariant/parse/connection failures
- Wrapper hardened: `umask 0027` + explicit `chmod 0640` on log file
- One-shot `chmod 0640` on existing world-readable cycle reports
- Backups: `*.bak.3af48d9-1779249120` at mode 0600
**Plan:** `tasks/plan_hermes_narrative_cron_fix_2026_05_20.md`
**Design:** `tasks/design_hermes_narrative_cron_fix_2026_05_20.md`
**Runbook:** `tasks/runbook_hermes_narrative_cron_instrumentation_2026_05_20.md`

### BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY
**Status:** SHIPPED 2026-05-20 — parallelized classifier deployed at concurrency=3 + fcntl.flock overlapping-cycle guard + per-worker 429 backoff. First post-fix cycle (05:00:54Z) completed in **79.27s** (was 234.28s) — 3.0× speedup, well under 120s. `jobs.json` `last_status=ok`. See `tasks/runbook_hermes_narrative_cron_timeout_apply_2026_05_20.md`.
**Plan:** `tasks/plan_hermes_narrative_cron_timeout_apply_2026_05_20.md` (v2, post-fold)
**Design:** `tasks/design_hermes_narrative_cron_timeout_apply_2026_05_20.md` (v2, post-fold)
**Runbook:** `tasks/runbook_hermes_narrative_cron_timeout_apply_2026_05_20.md`
**Concurrency tuning:** start at 3; promote to 5 only after ≥5 consecutive clean cycles + zero `openrouter-429-burst-count` + confirmed OpenRouter tier.

### BL-NEW-HERMES-CRON-NO-AGENT-FLAG-WATCHDOG
**Status:** SHIPPED 2026-05-20 — `scripts/hermes-no-agent-flag-check.sh` landed via this PR. Operator-runnable on-demand or schedulable via cron. No alerting wired (operator pipes stderr to their preferred channel).
**Why:** `jobs.json` `no_agent: true` flag is what keeps the historical 2026-05-15 prompt-injection failure mode resolved. If a future PR or operator accidentally flips it, the May 15 issue returns silently. Programmatic guardrail needed.
**Implementation:** Standalone bash script in `scripts/` validating 5 invariants from `/home/gecko-agent/.hermes/cron/jobs.json`: (1) file readable, (2) job `gecko-x-narrative-scanner` (id `c849fffec986`) present, (3) `no_agent == true`, (4) `enabled == true`, (5) `script` path non-empty. Distinct exit codes (0/1/2/3/4/5/6) for each failure class. Structured JSON event on stderr. Tests under `tests/test_hermes_no_agent_flag_check.py` (skipped on Windows per `test_cron_drift_watchdog.py` precedent).
**Smoke-tested on prod 2026-05-20T04:53Z:** happy path emits `HERMES-NO-AGENT-CHECK-OK ... no_agent=true ...` exit 0; missing jobs.json emits structured JSON failure exit 1.
**Operator action (optional):** schedule via cron if desired:
`0 */2 * * * /root/gecko-alpha/scripts/hermes-no-agent-flag-check.sh --quiet || /path/to/alerter`

### BL-NEW-SCANNER-EXISTING-EXCEPTION-BOUNDING
**Status:** SHIPPED 2026-05-27 via `tasks/findings_scanner_hygiene_2026_05_27.md`; deployed to `/home/gecko-agent/run-scanner-cycle.py` with backup `/home/gecko-agent/backups/run-scanner-cycle.py.20260527T010403Z`.
**Why:** Vector B F1 finding during design review: existing `log(f"... {e}")` callsites in `/home/gecko-agent/run-scanner-cycle.py` (lines 65, 112, 285, 476, 650, 716) interpolate exception messages without bounding. New instrumentation hunks added `str(e)[:120]` truncation per Invariant 2 — but the existing sites are asymmetric. Not regressive; consistency cleanup.
**Scope:** wrap each unbounded `{e}` site with `type(e).__name__` + `str(e)[:120]` pattern. ~6 line edits in run-scanner-cycle.py. VPS-only.
**Staged evidence:** candidate artifact `artifacts/scanner_hygiene_2026_05_27/run-scanner-cycle.after.py` replaces the six targeted scanner exception logs; `rg -n "log\(f.*\{e\}"` returns no staged matches.

### BL-NEW-HERMES-CRON-SUBPROCESS-LIFECYCLE-AUDIT
**Status:** AUDITED-DEFERRED 2026-05-20 (post-PR-#204 audit completed).
**Why:** Step 1 instrumentation revealed that Hermes cron's `subprocess.run(timeout=120)` does NOT actually kill the Python sub-subprocess when the wrapper SIGTERM fires at 120s — the Python orphans to PID 1 and continues running.
**Audit findings (P3 audit, 6h-block 2026-05-20):**

1. **Wrapper structure analysis:** `/home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh` spawns Python synchronously and does post-processing (grep + printf) AFTER Python exits. The post-processing produces the cron job's stdout (which becomes Telegram message body). `exec` is NOT viable without restructuring the wrapper's grep step.

2. **Overlap risk post-PR-#204:** The fcntl.flock guard at script entry (Python-level) blocks duplicate-Python execution. Even if Hermes cron starts two wrappers within the same hour, only ONE Python runs; the second emits `SCANNER-CYCLE-SKIP-OVERLAP` + exits 0. Effective overlap-stacking risk eliminated.

3. **Orphan completes work + cron records error:** This is COSMETIC, not functional. The Python orphan keeps writing to its open file descriptor and the cycle-report log captures everything. `jobs.json.last_status=error` is misleading but the work IS happening. Operators can verify via the cycle-report log.

4. **Concurrency=3 cuts typical cycles to ~79s** — orphan behavior only manifests on cycles that exceed 120s (now rare). The high-impact path is closed.

**Verdict:** Current behavior acceptable. No tiny safe fix exists (`exec` requires wrapper restructure; `trap SIGTERM + killpg` adds complexity without clear win). Restructure deferred unless:
- Sustained cycles > 120s become common despite parallelization
- Operator-facing `jobs.json.last_status` reliability becomes a hard requirement
- Resource pressure manifests from accumulated orphans (currently no observed orphan processes)

**Recommendation:** keep wrapper as-is. Revisit if any of the above conditions develop. File deferred design as separate effort if triggered.
**Surfaced by:** Vector A M1 finding during PR #201 review. **Audited by:** P3 audit in 6h-block 2026-05-20T05:13Z.

### BL-NEW-HERMES-NARRATIVE-DEFERRED-RESOLUTION-SWEEP
**Status:** BLOCKED-CANONICAL-ID / SOURCE-CALL-IDENTITY-RESOLUTION 2026-05-27 - runtime re-check found unresolved CA rows still exist, but the naive sweep has no safe canonical `resolved_coin_id` target.
**Runtime re-check 2026-05-27:** prod has 24 unresolved CA rows in the last 7d and 39 all-time; zero rows have `resolved_coin_id`. Only 3 recent rows match `candidates`, all for one Solana CA. `candidates` has no `coingecko_id`; `/api/coin/lookup` returns `coin_id=None` for `candidates` hits. Table audit found `coingecko_id` only on `second_wave_candidates`. Hermes cron `gecko-x-narrative-scanner` is enabled and last `ok`.
**Constraint:** do not write `resolved_coin_id` with contract address, ticker, or any other surrogate; that would overstate source-call rankability and still not unlock price coverage. Reopen implementation only after a canonical CA-to-CoinGecko-id resolver or the broader `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` design exists.
**Why:** Step 1 resolver-health re-check found 15 historical `narrative_alerts_inbound` rows with `extracted_ca IS NOT NULL` but `resolved_coin_id IS NULL`. Most likely cause: the gecko-alpha `/api/coin/lookup` endpoint returned `found=False` because those tokens hadn't been ingested by gecko-alpha at scan time (pre-CG-listing case — exactly the V1 structural limitation per design doc §3). The resolver only tries ONCE per CA; there's no re-resolution sweep when gecko-alpha later ingests the token.

### BL-NEW-SCANNER-DATETIME-UTCNOW-DEPRECATION
**Status:** SHIPPED 2026-05-27 via `tasks/findings_scanner_hygiene_2026_05_27.md`; deployed to `/home/gecko-agent/run-scanner-cycle.py` with backup `/home/gecko-agent/backups/run-scanner-cycle.py.20260527T010403Z`.
**Why:** `/home/gecko-agent/run-scanner-cycle.py` uses `datetime.utcnow()` which emits a DeprecationWarning under Python 3.12+. The warning text leaks into the cycle-report log via the `2>&1` redirect, occasionally interleaving mid-line with stdout content (per Vector A M2 + Vector B M1 at PR #204 review). Not a security issue (warning text is constant); cosmetic log hygiene.
**Scope:** swap `datetime.utcnow()` → `datetime.now(timezone.utc)`. `timezone` already imported. Touchpoints: state.start_time (CycleState.__init__), `cutoff_time = datetime.now(timezone.utc) - timedelta(...)` already uses correct form; only `state.start_time` needs the swap.
**Staged evidence:** runtime drift found three same-class `datetime.utcnow()` sites (`state.start_time`, `log(...)` timestamp, final-report duration). Candidate artifact stages all three so duration math stays aware/aware; `rg -n "datetime\.utcnow"` returns no staged matches.

### BL-NEW-SCANNER-PRINT-TO-LOG-CONSISTENCY
**Status:** SHIPPED 2026-05-27 via `tasks/findings_scanner_hygiene_2026_05_27.md`; deployed to `/home/gecko-agent/run-scanner-cycle.py` with backup `/home/gecko-agent/backups/run-scanner-cycle.py.20260527T010403Z`.
**Why:** Vector B F10 finding during design review: `/home/gecko-agent/run-scanner-cycle.py:691-713` (FINAL REPORT section) uses raw `print()` calls; the rest of the script uses the wrapped `log()` helper. Inconsistency is not a leak (current interpolations are counters/lengths) but is a future-maintenance trap.
**Scope:** convert FINAL REPORT print() calls to log() (or vice versa for the new JSON-encoded summary emit). Trivial. VPS-only.
**Staged evidence:** candidate artifact converts human-readable final-report print calls to `log(...)` while preserving structured JSON `print(json.dumps(...))` emits and the `SCANNER_CYCLE:` summary content.

### BL-NEW-ACTIONABILITY-GATE
**Status:** SHIPPED 2026-05-19 — implemented by PR #181 (`7506adc`) and deployed to srilu; visibility follow-up shipped by PR #182 (`32df89d`).
**Why:** Current paper trades mix decision-bearing and exploratory cohorts. The 2026-05-19 profit-pattern analysis found sharp separation between profitable current-regime patterns (`narrative_prediction`, `chain_completed`, `volume_spike`) and junk/exploratory patterns (`losers_contrarian`, weak `gainers_early`, low-n `trending_catch`).
**Evidence:** `tasks/findings_profit_patterns_2026_05_19.md`
**Decision:** Complete. `paper_trades.actionable`, `actionability_reason`, and `actionability_version` now mark decision-bearing vs exploratory cohorts without suppressing raw signal collection. 24h validation remains a separate evidence gate in `tasks/runbook_actionability_validation_2026_05_19.md`.
**Data-gate revalidation 2026-05-26:** CLEARED / no immediate implementation authorized. v1 closed rows now actionable=55 (+$335.53 / 43 wins / 12 losses) and exploratory=16 (-$385.63 / 7 wins / 9 losses), with zero malformed stamped rows and no mixed-version/pre-cutover anomaly. The row-count wait is closed, but exploratory n remains below 20 and exit-shape dominates PnL, so no v2 / suppression / source-quality consumption action is taken in this PR. See `tasks/findings_actionability_gate_revalidation_2026_05_26.md`.

### BL-NEW-ACTIONABILITY-GATE-V1-IMPLEMENT
**Status:** SHIPPED 2026-05-19 — PR #181 merged `7506adc`; deployed to srilu and verified with fresh stamped rows 2206-2208. PR #182 merged/deployed `32df89d` for dashboard/API visibility.
**Why:** Paper/live-readiness decisions need a cohort marker separate from `would_be_live`, which answers live-slot eligibility rather than audit-derived actionability.
**Scope:** Complete. Nullable `paper_trades.actionable`, `actionability_reason`, and `actionability_version`; pure classifier at open time after DB-side market-cap enrichment; exploratory paper rows retained. No suppression or capital allocation change shipped.
**Plan:** `tasks/plan_actionability_gate_v1.md`

### BL-NEW-X-OUTCOME-LINKAGE
**Status:** DESIGN-MERGED / RE-SCOPE-ELIGIBLE-AFTER-ACTIONABILITY-REVALIDATION 2026-05-26 - design doc merged via PR #184 as `2e2f506c` (squash 2026-05-21). The actionability `20/5` row-count gate cleared in `tasks/findings_actionability_gate_revalidation_2026_05_26.md`; implementation still must start with a fresh drift/runtime re-scope and must not treat the actionability finding as automatic approval.
**Why:** X handle ranking is blocked: 215 X alerts had 0 priced outcomes because `resolved_coin_id`/pricing linkage is missing.
**Scope:** Persist `resolved_coin_id`, `x_handle`, outcome status, entry/current price, and $300 notional P&L.

### BL-NEW-TG-OUTCOME-LINKAGE
**Status:** DESIGN-MERGED / RE-SCOPE-ELIGIBLE-AFTER-ACTIONABILITY-REVALIDATION 2026-05-26 - same merge as BL-NEW-X-OUTCOME-LINKAGE; design doc in PR #184 (`2e2f506c`). The actionability `20/5` row-count gate cleared in `tasks/findings_actionability_gate_revalidation_2026_05_26.md`; implementation still needs fresh drift/runtime re-scope. TG side is already structurally FK-linked via `tg_social_signals.paper_trade_id`; the design covers the `linkage_state` disambiguator + backfill + unified view.
**Why:** TG channel ranking is blocked: only 2 current-regime closed linked trades, both low-n losses.
**Scope:** Persist and dashboard `tg_channel`, `resolution_state`, `posted_at`, `paper_trade_id`, and `mcap_at_sighting`.

### BL-NEW-NO-PEAK-RISK-HANDLING
**Status:** AUDIT-MERGED / RE-SCOPE-ELIGIBLE-AFTER-ACTIONABILITY-REVALIDATION 2026-05-26 - peak-giveback / freshness historical audit merged via PR #183 as `4e672fe6` (squash 2026-05-21). V2 candidate identified: `pre_entry_peak_gain_pct >= 40%` AND `pre_entry_giveback_ratio >= 0.50` (current-regime -$962/52 trades; all-history -$1,034/66). The actionability `20/5` row-count gate cleared in `tasks/findings_actionability_gate_revalidation_2026_05_26.md`; implementation still needs fresh drift/runtime re-scope and the V2 gate must require `pre_entry_giveback_ratio IS NOT NULL` per the audit coverage appendix.
**Why:** `no_peak_<5` current-regime bucket is deeply negative (-$6,090.86 / n=99), but `peak_pct` is not available at trade-open time.
**Scope:** Design a peak<5 early-exit or hard-risk policy separately; do not mix exit/risk handling into Actionability Gate v1.

---

## Design Decisions (Locked In)

These decisions were reviewed and approved. Reference them when implementing P1 items.

**D1 — Scoring normalization (UPDATED):** MIN_SCORE lowered to 25 and CONVICTION_THRESHOLD to 22 after observing that the CoinGecko micro-cap universe produces lower raw scores than originally modelled. The 178-point normalization base compresses scores: typical top tokens score 25-35 quant. The original target of MIN_SCORE=60 would require 4+ signals firing simultaneously which rarely happens in current market conditions. Thresholds will be raised as vol_acceleration signal accumulates history (requires 3+ scan cycles) and more data sources come online.

**D2 — Buy pressure fields:** Add `txns_h1_buys: int | None = None` and `txns_h1_sells: int | None = None` to CandidateToken as Optional fields. Parser populates where available (DexScreener `txns.h1.buys` / `txns.h1.sells`). Scorer treats `None` as 0 points for buy pressure — graceful degradation if the field is missing from the API response.

**D3 — Score velocity parameter injection:** Pass `historical_scores: list[float] | None = None` into the scorer as a parameter. Keeps scorer a pure function — no DB access, fully testable. The caller (main.py) does the DB read and passes historical scores in. This is the correct pattern: I/O at the edges, pure logic in the core.

**D4 — Qwen migration order (SUPERSEDED):** BL-001 (Qwen migration) was cancelled — Claude haiku-4-5 via Anthropic SDK retained as fallback scorer. Rationale: user has $200/month Anthropic plan, no need for additional DashScope account. The narrative scoring prompt has been calibrated with a detailed rubric and quantitative context for Claude haiku-4-5 specifically.

**D5 — Implementation order for enhanced scorer:** Execute P1 items in this sequence:
1. BL-011 (buy pressure) — new CandidateToken fields + parser + signal
2. BL-012 (age bell curve) — replace existing signal, no new fields
3. BL-010 (hard disqualifiers) — liquidity floor pre-filter
4. BL-014 (co-occurrence multiplier) — structural scoring change
5. BL-013 (score velocity) — DB table + parameter injection
6. BL-016 (normalization) — adjust scale after all signals added
7. BL-015 (confidence tag) — enriches MiroFish seed last

---

## P0 — Blocking

### BL-001: Migrate fallback scorer from Anthropic to Qwen (OpenAI-compatible)
**Status:** CANCELLED — see D4 (SUPERSEDED)
**Files:** scout/mirofish/fallback.py, scout/config.py, .env.example, tests/test_fallback.py, pyproject.toml
**Why:** User wants Qwen (qwen-plus via DashScope) instead of Claude haiku for the narrative fallback scorer. DashScope uses OpenAI-compatible API.
**Changes needed:**
- Replace `anthropic` SDK with `openai` SDK (async client) in fallback.py
- Add config fields: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_NAME` (replace `ANTHROPIC_API_KEY`)
- Update seed prompt for Qwen's response style
- Update tests to mock OpenAI client instead of Anthropic
- Remove `anthropic` from pyproject.toml dependencies, add `openai`
**Acceptance:** `uv run pytest tests/test_fallback.py -v` passes, dry-run produces narrative scores

### BL-002: Create .env with real API keys and run first live dry-run
**Status:** DONE — live dry-run completed, real API keys configured on VPS
**Files:** .env
**Why:** Pipeline has never been tested against real APIs. Need to verify DexScreener/GeckoTerminal response parsing, Telegram delivery, and end-to-end flow.
**Keys needed:**
- TELEGRAM_BOT_TOKEN: (stored in .env)
- TELEGRAM_CHAT_ID: (stored in .env)
- ANTHROPIC_API_KEY: (stored in .env)
**Acceptance:** `uv run python -m scout.main --dry-run --cycles 1` completes with real tokens fetched, scored, and logged

---

## P1 — Enhanced Scorer (Phase 1: DexScreener-only data)

### BL-010: Add hard disqualifiers (Tier 1 pre-filter)
**Status:** DONE — implemented by offshore devs (liquidity floor in scorer.py:55-57)
**Files:** scout/scorer.py, scout/models.py, tests/test_scorer.py
**Why:** Current scorer has no fraud filter. Wash-traded tokens pass easily.
**Changes:**
- Liquidity floor: auto-discard if `liquidity_usd < $15K` (configurable via Settings)
- Run before any scoring — fail fast, return score=0
- Deployer wallet check deferred to Phase 2 (needs Helius/Moralis)
- Wash trade top-3-wallet check deferred to Phase 2 (needs on-chain data)
**Acceptance:** Tokens with < $15K liquidity get score 0 and never reach MiroFish

### BL-011: Add buy pressure ratio signal (Tier 3)
**Status:** DONE — implemented by offshore devs (buy pressure in scorer.py:104-114)
**Files:** scout/models.py, scout/ingestion/dexscreener.py, scout/scorer.py, tests/
**Why:** Best wash-trade discriminator available from existing API data. DexScreener returns `txns.h1.buys` and `txns.h1.sells` — currently unused.
**Changes:**
- Add `txns_h1_buys: int | None = None` and `txns_h1_sells: int | None = None` to CandidateToken (see Decision D2)
- Parse from DexScreener response in `from_dexscreener()`
- Score: buy_ratio > 65% → +15 points
**Acceptance:** Tokens with skewed buy pressure score higher than balanced volume tokens

### BL-012: Replace binary token age with bell curve (Tier 4)
**Status:** DONE — implemented by offshore devs (age bell curve in scorer.py:83-97)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Current binary `< 7 days = 10 pts` misses the optimal 1-3 day window.
**Changes:**
- 0 pts for < 12h (too early, no liquidity)
- 5 pts for 12-24h
- 10 pts for 1-3 days (peak window)
- 5 pts for 3-5 days
- 0 pts for > 5 days (likely dead)
**Acceptance:** Scoring curve matches spec, existing tests updated

### BL-013: Add score velocity bonus (Tier 2)
**Status:** DONE — implemented by offshore devs (score velocity in scorer.py:149-154)
**Files:** scout/scorer.py, scout/db.py, scout/main.py, tests/
**Why:** A token whose score is rising across consecutive scans indicates active accumulation in progress — the velocity itself is a signal.
**Changes:**
- Add `score_history` table in db.py (contract_address, score, scanned_at)
- Log each score in main.py after scoring
- In scorer: accept `historical_scores: list[float] | None = None` param, award +10 if strictly increasing over last 3 scans (see Decision D3)
- Scorer remains pure (no I/O) — main.py does the DB read and passes historical scores in
**Acceptance:** Tokens with rising scores get bonus, flat/declining scores get nothing

### BL-014: Add co-occurrence multiplier
**Status:** DONE — implemented by offshore devs (co-occurrence multiplier in scorer.py:159-161)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Vol/liq ratio alone is the most commonly gamed signal. Penalize isolated vol/liq without holder growth; bonus when both fire together.
**Changes:**
- After summing all signal points:
  - If `vol_liq_ratio` fired AND `holder_growth` fired → multiply by 1.2×
  - If `vol_liq_ratio` fired WITHOUT `holder_growth` → multiply by 0.8×
- Apply multiplier before returning final score
- Cap final score at 100
**Acceptance:** Wash-traded tokens (high vol, no holder growth) score 20% lower

### BL-015: Add signal confidence tag to MiroFish seed
**Status:** DONE — implemented by offshore devs (signal_confidence function in scorer.py:167-177)
**Files:** scout/mirofish/seed_builder.py, scout/scorer.py, tests/
**Why:** Enriching the MiroFish seed with signal context improves narrative simulation quality.
**Changes:**
- scorer.py returns additional `confidence: str` (HIGH if 3+ tiers firing, MEDIUM if 2, LOW if 1)
- seed_builder.py includes `signal_confidence` and `signals_fired` list in the seed payload
- Update MiroFish prompt to reference the confidence level
**Acceptance:** MiroFish seed contains signal context, tests verify format

### BL-016: Normalize scoring to 125 base → 100 scale
**Status:** DONE — implemented by offshore devs (normalization in scorer.py:156-157, SCORER_MAX_RAW=183)
**Files:** scout/scorer.py, scout/config.py, tests/test_scorer.py
**Why:** New signals (buy pressure +15, velocity +10, revised age curve) push max above 100. Need normalization.
**Changes:**
- Calculate raw sum from all signals (max 125 base)
- Normalize: `final = min(100, int(raw_sum * 100 / 125))`
- Apply co-occurrence multiplier after normalization
- Update MIN_SCORE semantics if needed
**Acceptance:** All scores remain 0-100, tests verify edge cases

### BL-NEW-QUOTE-PAIR: Stable-pair liquidity-quality signal
**Status:** SHIPPED 2026-05-09 — PR #85 (`3774591`) squash-merged + deployed VPS 2026-05-09T16:40:34Z. Migration `bl_quote_pair_v1` (schema_version 20260513) applied; columns `quote_symbol` + `dex_id` added to candidates table; forward-ingestion populating both fields for DexScreener-sourced rows.
**Tag:** `scoring` `dexscreener` `co-occurrence`
**Files:** scout/models.py (2 fields + parser), scout/config.py (3 settings), scout/scorer.py (inlined signal), scout/db.py (migration + columns), scout/aggregator.py (`_PRESERVE_FIELDS`), tests/test_models_quote_pair.py, tests/test_scorer_quote_pair.py, tests/test_db_migration_bl_quote_pair.py, tests/test_aggregator.py.
**Why:** DexScreener returns `quoteToken.symbol` + `dexId` per pair but the parser was discarding both. Tokens paired with USDC/USDT have materially different exit dynamics than WETH/SOL-paired tokens (no secondary stable-leg slippage). Industry precedent: Birdeye/GMGN use stable-pair as a standard liquidity-quality discriminator.
**Effect:** +5 raw / +2 normalized when `quote_symbol ∈ {USDC, USDT, DAI, FDUSD, USDe, PYUSD, RLUSD, sUSDe} AND liquidity_usd >= $50K`. Counts toward co-occurrence multiplier (intended — stable-pair is real evidence).
**Magnitude analysis:** Direct +2 normalized is a tiebreaker; dominant mechanical effect is when `stable_paired_liq` pushes a 2-signal token to 3-signal, triggering the 1.15× co-occurrence multiplier (~+15 normalized uplift).
**Test count:** 32 new + 11 added during PR review = 43 net-new (160-test subset baseline went 149→160).
**Pipeline executed:** Industry research → drift+Hermes-first → plan + 2 reviewers (R1 statistical, R2 code-structural) → fixes → design + 2 reviewers (R3 test-discipline, R4 operational) → fixes → build (TDD) → PR #85 + 3 reviewers (R5 code-quality, R6 silent-failure, R7 type/integration) → 1 CRITICAL + 5 MUST-FIX + 1 NIT folded → squash-merge → deploy.
**Soak:** D+0 = 2026-05-09T16:40Z; D+3 mid-soak verification 2026-05-12; D+7 ends 2026-05-16. Revert via `STABLE_PAIRED_BONUS=0` env override (no code rollback). Acceptance: alert volume must not exceed +10% baseline.
**Skipped reviewer NITs (deferred):** sub-threshold debug log; INSERT OR IGNORE log; lock-contention test; Literal type for quote_symbol; frozenset vs tuple; `dex_id` consumer (planned); GT-only token coverage (defer to soak data).
**See:** `tasks/plan_quote_pair_signal.md`, `tasks/design_quote_pair_signal.md`, memory `project_bl_quote_pair_2026_05_09.md`.

---

## P2 — Phase 2 Enhancements (Helius/Moralis required)

### BL-020: Populate holder_growth_1h from enricher
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/ingestion/holder_enricher.py
**Why:** Code review found holder_growth_1h is never populated. The 25-point holder growth signal is dead in production without this.
**Changes:**
- Store previous holder_count in DB per contract_address
- On next scan, compute delta as holder_growth_1h
- Requires at least 2 scan cycles to produce data
~~**Blocked by:** Helius/Moralis API key~~

### BL-021: Add unique buyer wallet count signal (Tier 3)
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/ingestion/holder_enricher.py, scout/models.py, scout/scorer.py
**Why:** Distinguishes organic community buying from bot accumulation.
**Changes:**
- Add `unique_buyers_1h: int = 0` to CandidateToken
- Fetch from Helius (Solana) / Moralis (EVM) transfer history
- Score: high unique_buyers relative to total_txns → +15 pts
~~**Blocked by:** Helius/Moralis API key~~

### BL-022: Add wash trade detection (top-3 wallet volume concentration)
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/scorer.py, scout/ingestion/holder_enricher.py
**Why:** Hard disqualifier — if top 3 wallets account for > 40% of volume, it's almost certainly wash trading.
**Changes:**
- Fetch top wallet transaction data from Helius/Moralis
- Compute concentration ratio
- Disqualify (score 0) if > 40%
~~**Blocked by:** Helius/Moralis API key~~

### BL-023: Add deployer wallet supply concentration check
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/safety.py or scout/scorer.py
**Why:** Classic rug setup — deployer holds > 20% of supply.
**Note:** Partially covered by GoPlus already. Evaluate overlap before implementing.
~~**Blocked by:** Helius/Moralis API key~~

### BL-024: Add transaction size distribution signal
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/ingestion/holder_enricher.py, scout/scorer.py
**Why:** Organic pre-pump = many small txns ($50-$500). Bot wash = fewer large uniform txns.
~~**Blocked by:** Helius/Moralis API key~~

---

## P2 — Infrastructure & Reliability

### BL-030: Add Solana chain bonus to scorer (Tier 4)
**Status:** DONE — implemented by offshore devs (Solana bonus in scorer.py)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Diagram shows +5 pts for Solana chain (meme premium). Solana has disproportionate meme coin activity.
**Changes:** Simple conditional: `if chain == "solana": +5 pts`

### BL-031: Add market cap tier curve (Tier 4)
**Status:** DONE — implemented by offshore devs (mcap tier curve in scorer.py)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Current binary $10K-$500K gate misses the sweet spot. Diagram shows $10K-$100K as peak score, tapering to $500K.
**Changes:** Graduated scoring: 8 pts for $10K-$100K, 5 pts for $100K-$250K, 2 pts for $250K-$500K

### BL-032: Social signal source decision (consolidates old BL-032 + BL-041)
**Status:** AUDITED 2026-05-14 - see `tasks/findings_bl032_social_signal_audit_2026_05_14.md`.
**Tag:** `audited` `dead-signal` `consolidates-BL-041` `hermes-first-rescope`
**Files (eventual):** `scout/ingestion/`, `scout/models.py`, `scout/scorer.py`
**Why:** `social_mentions_24h` is a 15-point signal that has never fired in production — code review found it's never populated. The old 2026-05-03 conclusion ("Hermes route is closed") is stale after narrative-scanner activation work: installed VPS Hermes now has `social-media/xurl`, `kol_watcher`, `narrative_classifier`, and `narrative_alert_dispatcher`. That path does NOT automatically produce generic social-volume counts, but it does cover the highest-value X/KOL narrative stream. Do not build custom Twitter/LunarCrush code until this existing Hermes path is evaluated against the dead scorer field.

**Audit result:** Do NOT build custom Twitter/LunarCrush now. Prod verification found `candidates.social_mentions_24h` is zero for all 1,543 candidates, `social_signals` / `social_baselines` / `social_credit_ledger` are empty, TG social is active (421 messages and 164 signals in 7d), and Hermes X is active but immature (6 `narrative_alerts_inbound` rows, 0 resolved). Telegram rows are curated-call signals, not market-wide social volume; X rows are narrative alerts, not generic social counts. Keep both as first-class surfaces rather than stuffing them into `social_mentions_24h`.

**Decision:** Close the "build social API" direction. The remaining residual gap is scorer calibration: `social_mentions_24h` is an unwired 15-point feature inside `SCORER_MAX_RAW`, so removing/replacing it requires backtest rather than casual cleanup.

**Follow-up:** BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT below.
**Note on consolidation:** This entry replaces the prior separate BL-032 + BL-041 (X/Twitter monitoring). They were two pending entries for the same dead-signal problem; merged 2026-05-03.

### BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT: backtest dead social signal removal/replacement
**Status:** AUDITED 2026-05-17 (PR #152 squash-merged to master at `e174a3d` 2026-05-17T23:39:11Z) — see `tasks/findings_social_mentions_denominator_audit_2026_05_17.md`. Full Plan→2-reviewers→Design→2-reviewers→PR→3-reviewers cycle completed (plus 1 post-merge re-review fold per Reviewer 1). **Recommendation: Option B (remove + recalibrate gates 60→65 / 70→75); closed-form approximate 0-flip blast radius across 6,096,576 score_history rows** (closed-form because `score_history` stores only post-multiplier final score; exact re-score from raw points + signal list not feasible). Code change DEFERRED to explicit operator approval per scope constraint. PR ships findings doc + audit_v2_queries.sql for re-evaluation + one-line `# DEAD SIGNAL` annotation on scorer.py:121 (zero behavior change; 69/69 scorer tests pass). Hermes X bridge (0/131 resolved 7d) and TG bridge (6 distinct contracts/24h) both data-not-ready; both deferred. 5 follow-up items filed below (3 audit + 2 VARIANT-{B,C}-IMPL PENDING-OPERATOR-DECISION).
**Tag:** `scoring` `dead-signal` `calibration` `hermes-first` `audited`

**Original status (now historical):** PROPOSED 2026-05-14. Originating scope: Compare current scoring denominator against (1) remove `social_mentions_24h` from scoring/max raw, (2) replace with evidence-gated `narrative_mentions_24h` / `kol_mentions_24h` from Hermes X + TG, (3) leave as-is until X narrative rows mature.

**Hermes-first verdict (post-audit, corrected 2026-05-17 PR-stage):** Hermes skill hub Social Media category (7 skills) + awesome-hermes-agent ecosystem BOTH checked. awesome-hermes-agent IS reachable (prior cycle-7/8/9 "404 consistent" claim was stale — corrected). `x-twitter-scraper` exists at `https://github.com/Xquik-dev/x-twitter-scraper` (typed X/Twitter API: search, timelines, mentions, trends, monitors, webhooks) but does NOT cover per-token 24h aggregation. Audit verdict unchanged (no drop-in primitive); diligence framing corrected.

**Re-evaluation triggers** (operator runs `tasks/audit_v2_queries.sql` when ANY fires):
1. `narrative_alerts_inbound.resolved_coin_id` ≥ 20 in any 30d window (currently 0/126)
2. `tg_social_messages` distinct-contract 24h rollup ≥ 50 (currently 6)
3. `scorer.py` signal weight change OR `SCORER_MAX_RAW` change (invalidates Variant B's 0-flip math)
4. 2026-08-17 (90d calendar backstop per cycle-9 `keep_on_provisional_until_<iso>` convention)
5. Operator explicit request

### BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG: daily cron for re-eval triggers 1+2
**Status:** PROPOSED 2026-05-17 — filed concurrent with BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT findings.
**Tag:** `observability` `audit-watchdog` `silent-failure-prevention`
**Why:** Re-eval triggers 1+2 (`narrative_alerts_inbound.resolved_coin_id` ≥ 20 in 30d; `tg_social_messages` distinct-contract 24h ≥ 50) are operator-memory-dependent. Per CLAUDE.md §12a (freshness SLO + watchdog rule), decision-bearing thresholds need automated detection or they silently never fire — same shape as `BL-NEW-REVIVAL-VERDICT-WATCHDOG` from cycle-9. The 2c (TG rollup) and 2a (resolution rate) follow-ups merged into this single watchdog per design-review fold R1 #5 (both query the same `narrative_alerts_inbound` + `tg_social_messages` surface).
**Action:** Daily cron query against `narrative_alerts_inbound` resolution-count (30d window) AND `tg_social_messages` distinct-contract 24h rollup; TG alert ("revival_criteria-style") when either threshold crossed. **Owner:** operator or next-cycle infrastructure work assignee — to be claimed on PR merge.
**Decision-by:** 4 weeks from PR merge. **If not implemented by that date, audit must be re-run manually on 2026-08-17** (90d backstop is the load-bearing safety net; watchdog is the convenience layer). Per PR-review fold R3 #2 + R2 #5.

### BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION: codify the `# DEAD SIGNAL` annotation pattern
**Status:** SHIPPED 2026-05-19 — style-guide convention codified in `docs/gecko-alpha-alignment.md` on branch `codex/scorer-dead-signal-comment-convention`.
**Tag:** `scoring` `code-convention` `intellectual-debt-prevention`
**Why:** Per design-review fold R2 §2: Signal 13 (CryptoPanic) at `scorer.py:184-198` has a documented gated-comment convention; Signal 5 (Social Mentions) lacked one until this audit added it. Future scorer audits will repeat this work unless the convention is codified.
**Action:** Complete. Alignment doc now requires dormant scorer signals to carry `# DEAD SIGNAL — pending <BL ticket>` or `# GATED — pending <BL ticket or config>` immediately above the threshold check, with ticket and re-eval trigger where known.
**Evidence:** `docs/gecko-alpha-alignment.md` § "Scorer dormant-signal comments"; existing examples remain `scout/scorer.py` Signal 5 social mentions and Signal 13 CryptoPanic.

### BL-NEW-SOCIAL-DENOMINATOR-OPERATOR-PREFERENCE: B vs C decision for next-cycle code change
**Status:** PROPOSED 2026-05-17 — surfaced as Open Question 1 in audit findings.
**Tag:** `operator-decision` `awaiting-response`
**Why:** Audit recommends Variant B (remove + recalibrate gates 60→65 / 70→75) with closed-form approximate 0-flip blast radius (caveat: `score_history` stores only post-multiplier final score; closed-form approximation not exact re-score). Variant C (remove WITHOUT recalibrating) widens MiroFish funnel by 35 historical candidates — operator may value this if recall > precision.
**Action:** Operator responds via PR comment OR `tasks/findings_social_mentions_denominator_operator_decision.md` follow-up commit. On response: close this entry; promote pre-filed `BL-NEW-SOCIAL-DENOMINATOR-VARIANT-B-IMPL` or `-VARIANT-C-IMPL` below from PENDING-OPERATOR-DECISION to PROPOSED for next-cycle code change.
**Decision-by:** 4 weeks from PR merge. **If no response by then, entry remains PROPOSED until 2026-08-17 backstop run resurfaces for human triage.** (Per PR-review fold R3 #1: the prior "default action = stamp interim Option A" had no actor and was itself a silent-non-trigger — the exact failure mode this audit exists to prevent. Removed.)

### BL-NEW-SOCIAL-DENOMINATOR-VARIANT-B-IMPL: implement Option B (remove + recalibrate gates)
**Status:** PENDING-OPERATOR-DECISION 2026-05-17 — pre-filed per PR-review fold R3 #4 so operator's "approve B" comment lands on a real backlog row.
**Tag:** `scoring` `code-change` `gate-recalibration` `pending-decision`
**Why:** Audit-recommended path. Removes Signal 5 from scorer.py + drops SCORER_MAX_RAW 208→193 + raises MIN_SCORE 60→65 + raises CONVICTION_THRESHOLD 70→75. Closed-form approximate 0-flip blast radius across 6M+ historical rows (audit Q3; closed-form because `score_history` stores only post-multiplier final score). Pre-implementation step: re-score against fresh srilu DB to confirm approximation still holds.
**Action:** ~2h. Edit scorer.py:120-127 (remove Signal 5 block) + scorer.py:37 (208→193) + config.py:27-28 (60→65, 70→75). Update test_scorer.py for new max-raw. Smoke-test on srilu.
**Promotion trigger:** Operator approves Option B in BL-NEW-SOCIAL-DENOMINATOR-OPERATOR-PREFERENCE → promote this to PROPOSED; close `-VARIANT-C-IMPL` below.
**Decision-by:** Conditional on operator decision; not calendar-bound on its own.

### BL-NEW-SOCIAL-DENOMINATOR-VARIANT-C-IMPL: implement Option C (remove without recalibrating)
**Status:** PENDING-OPERATOR-DECISION 2026-05-17 — pre-filed per PR-review fold R3 #4.
**Tag:** `scoring` `code-change` `funnel-widening` `pending-decision`
**Why:** Variant C path. Removes Signal 5 from scorer.py + drops SCORER_MAX_RAW 208→193 but KEEPS gates at 60/70. Widens MiroFish funnel by 35 historical candidates (audit Q4) — operator preference if recall > precision.
**Action:** ~1h. Edit scorer.py:120-127 (remove Signal 5 block) + scorer.py:37 (208→193). Update test_scorer.py for new max-raw. NO config.py change. Smoke-test on srilu.
**Promotion trigger:** Operator approves Option C in BL-NEW-SOCIAL-DENOMINATOR-OPERATOR-PREFERENCE → promote this to PROPOSED; close `-VARIANT-B-IMPL` above.
**Decision-by:** Conditional on operator decision; not calendar-bound on its own.

### BL-033: Add heartbeat logging every 5 minutes
**Status:** DONE — heartbeat logging implemented (PR #7)
**Files:** scout/main.py
**Why:** PRD requires heartbeat log showing: tokens scanned, candidates promoted, alerts fired, MiroFish jobs today.
**Changes:** Track cumulative stats, log summary every 5 min (or every N cycles)

### BL-NEW-INGEST-WATCHDOG: Per-source ingestion starvation alert
**Status:** SHIPPED 2026-05-15 — commit `479e6c7 feat(observability): add ingestion starvation watchdog`. Status updated per `tasks/findings_backlog_drift_audit_2026_05_16.md`. Design in `tasks/design_ingest_watchdog.md`; implementation uses raw-source health samples rather than post-filter candidate counts.
**Tag:** `observability` `silent-failure` `tg-alert`
**Files:** scout/heartbeat.py (per-source consecutive-empty counter), scout/main.py (cycle-loop instrumentation + TG dispatch), scout/ingestion/{coingecko,dexscreener,geckoterminal}.py (raw-source samples), tests/test_{heartbeat,ingest_watchdog,coingecko,dexscreener,geckoterminal,config}.py.
**Why:** When a single ingestion source (CoinGecko / DexScreener / GeckoTerminal chain) stops returning raw upstream data, the pipeline silently keeps running on remaining sources. Memory `feedback_clear_pycache_on_deploy.md` and the BL-066' incident showed the operator only learns about silent ingestion failures via downstream symptoms (e.g., paper-trade volume drop). Industry-standard ops pattern.
**Drift verdict:** NET-NEW. Heartbeat module (`scout/heartbeat.py`) currently tracks aggregate cycle stats only — no per-source counters. Existing failure-streak precedent: `_combo_refresh_failure_streak` (`scout/main.py:92`), `_social_consecutive_restarts` (`scout/main.py:89`). The proposed implementation follows that pattern.
**Hermes verdict:** Installed VPS `devops/webhook-subscriptions` is notification-adjacent but gateway/webhook based, not an in-process ingestion monitor. Public Hermes docs/catalog do not provide a gecko-alpha source-starvation primitive. Build custom detector; reuse existing project Telegram alerter with `parse_mode=None`.
**Effect:** New per-source counter (`_ingest_watchdog_state[source]`); increments when an expected raw-source sample has `raw_count=0`; resets on first positive raw count. When counter ≥ `INGEST_STARVATION_THRESHOLD_CYCLES` (default 5), emit TG alert + structlog warning with last-success timestamp. One alert per source per starvation episode; recovery emits one recovery alert. Midcap off-cadence samples are `expected=False` and ignored.
**Risks:** First deployment may surface known `geckoterminal:ethereum` 404 starvation as a one-time alert. This is intentional visibility for an existing source gap, not alert spam. False positives from quiet market/filter regimes are mitigated by raw-count health instead of candidate-count health.
**Soak isolation rationale:** Item 2 sends Telegram alerts. Deploying it during BL-NEW-QUOTE-PAIR's 7d soak would mix new alert noise with existing alert-volume measurements, making the +10% revert threshold harder to attribute. Defer until D+3 mid-soak verification of BL-NEW-QUOTE-PAIR (2026-05-12) at minimum.
**Verification:** Focused local suite 85 passed using pre-provisioned project venv after reviewer folds for DexScreener token-detail health and GeckoTerminal error context; `uv` bootstrap in fresh worktree hit local PyPI certificate `UnknownIssuer` before project code ran.

### BL-NEW-PARSE-MODE-AUDIT: Project-wide `send_telegram_message` parse_mode hygiene
**Status:** SHIPPED 2026-05-13 — per-site fixes landed via PR #111 (commit `325369d`). 7 HIGH ACTUAL sites closed (6 from audit + 1 plan-review discovery: `scout/alerter.py:189 send_alert` was missed because audit grepped only `send_telegram_message` calls). AST coverage test (`tests/test_parse_mode_hygiene.py::test_all_dispatch_sites_pin_parse_mode`) with resolver-aware second arm mechanically enforces the audit-methodology lesson going forward. 3 HIGH POTENTIAL sites in `scout/main.py:351,434,1537` deferred per audit policy (need 7-day production log review before promotion). 8 currently-allowlisted sites listed inline in test file with rationale.
**Surfaced 2026-05-11 during §2.9 fix (PR #106).** The auto_suspend bug was one instance of a systemic class. Per-site fixes shipped as a single PR (#111) covering all 7 HIGH ACTUAL sites with 4-layer test coverage.
**Tag:** `silent-failure` `tg-alert` `parse_mode` `class-2-residual`
**Why:** `alerter.send_telegram_message` defaults to `parse_mode="Markdown"`. Telegram MarkdownV1 parses unbalanced `_ * [ ] \`` as formatting markers — when a message body contains a signal name (`gainers_early`, `hard_loss`, `trending_catch`) or token symbol (e.g., `AS_ROID`) with stray markdown chars, Telegram returns HTTP 200 with the body silently mangled (markers consumed, weird italics applied). The §2.9 trending_catch incident on 2026-05-11T01:00:26Z is the worked example: operator received the alert but didn't recognize it as auto-suspend. PR #106 fixes the two auto_suspend sites; the remaining call sites need site-by-site audit.
**Drift verdict:** NET-NEW. No existing backlog entry tracks this class. PR #106 closes the §2.9 *instance*; this entry tracks the *class*. CLAUDE.md §12b (global) now encodes the rule; existing call sites pre-date the rule and need retroactive verification.
**Hermes verdict:** No Hermes skill covers Telegram-payload-parse-mode hygiene. Project-internal.

**Inventory (24 total `send_telegram_message` call sites in `scout/`):**

*Already pass `parse_mode=None` (7 — verified 2026-05-11):*
- `scout/main.py:250` (calibration dry-run alert — PR #76 silent-failure C1 fix)
- `scout/main.py:991` (heartbeat/health summary)
- `scout/main.py:1051` (heartbeat/health summary)
- `scout/main.py:1189` (per PR-stage adv-S2 fix)
- `scout/trading/auto_suspend.py:272` (hard_loss — PR #106)
- `scout/trading/auto_suspend.py:327` (pnl_threshold — PR #106)
- `scout/trading/tg_alert_dispatch.py:312` (BL-NEW-TG-ALERT-ALLOWLIST R1-C1 fold)

*Default to Markdown — needs audit (15 sites):*
- `scout/chains/alerts.py:59` — chain pattern alerts (likely contains signal names)
- `scout/live/loops.py:251` — live trading alerts (token symbols + signal names)
- `scout/main.py:165` — combo_refresh failure (generic body, low risk)
- `scout/main.py:350` — chunked summary (body unclear)
- `scout/main.py:433` — generic summary (body unclear)
- `scout/main.py:1521` — daily summary (formatted text with signal names)
- `scout/narrative/agent.py:557` — narrative LEARN reflection
- `scout/narrative/agent.py:715` — narrative LEARN reflection
- `scout/secondwave/detector.py:285` — secondwave alerts (token symbols)
- `scout/social/lunarcrush/alerter.py:144` — LunarCrush social alerts
- `scout/trading/calibrate.py:354` — calibration applied (HIGH RISK — body iterates over signal_type)
- `scout/trading/suppression.py:186` — suppression alerts (signal-info body)
- `scout/trading/weekly_digest.py:335` — weekly digest chunks
- `scout/trading/weekly_digest.py:340` — weekly digest tail
- `scout/velocity/detector.py:193` — velocity alerts (token symbols)

**Effect:** Per-site decision — `parse_mode=None` for system-health/diagnostic alerts where formatting adds no value; `_escape_md(value)` for user-data fields inside intentionally-formatted operator-visible messages (chains/alerts, daily summary). Each site reviewed for whether body could realistically contain `_ * [ ] \``.

**Triage hint:** HIGH RISK = body iterates over signal_type or token symbols (calibrate.py:354, chains/alerts.py:59, secondwave/detector.py:285, suppression.py:186, weekly_digest.py:335/340, velocity/detector.py:193, narrative/agent.py:557/715, live/loops.py:251); LOW RISK = body is static or controlled (main.py:165 combo_refresh, main.py:350/433 chunked).

**Risks of NOT fixing:** Each high-risk site can produce a silent-rendering alert that operator doesn't recognize, exactly matching the §2.9 pattern. The class-2 silent-failure surface stays open until each site is audited.
**Risks of fixing all at once:** A single sprawling PR conflates 15 review surfaces. Better to group by module area (e.g., one PR for `scout/trading/`, one for `scout/narrative/`, one for `scout/main.py` daily-summary sites) and ship sequentially.

**Discovery:** PR #106 grep audit 2026-05-11. Inventory preserved here before head-state decays.
**Estimate:** ~1-2 hours per area group (read each call's body context + decide `parse_mode=None` vs `_escape_md` + test). Total 3-5 hours across all 15 sites. Sequenceable independently — no shared state, no soak risk.

**Cross-references:**
- PR #106 (instance fix at auto_suspend) — closure pattern for each site
- Global CLAUDE.md §12b — encodes the rule the audit enforces
- Project CLAUDE.md "What NOT To Do" — pointers to global §12b + worked example
- `tasks/findings_silent_failure_audit_2026_05_11.md` §2.9 — original finding

### BL-053: CryptoPanic news feed (shipped 2026-04-20, deactivated by default — operator activation pending)
**Status:** SHIPPED-BUT-DEACTIVATED — diagnosed 2026-05-11 during silent-failure audit §2.2 closure. Code intact in tree; flags default-off per original design intent ("research-only, no scoring signal activation in this increment" per BL-053 design doc §1). The 22-day "silent failure" surfaced in the audit was actually a 22-day deploy-without-activate — a **(b'-new)** failure class distinct from (a) auth failure, (b) listener-not-scheduled, and (c) gate-swallow. See `tasks/findings_silent_failure_audit_2026_05_11.md` §2.2 diagnosis block.
**Tag:** `shipped-but-deactivated` `news-feed` `bl-053` `deploy-without-activate`

**Deactivation reasoning:**
- Original design (BL-053 design doc §1) explicitly shipped flag-gated as research-only.
- No automated SQL consumer of `cryptopanic_posts` table — scoring path uses in-memory enrichment (`model_copy` on candidates), not DB reads. The table is archive-for-future-analysis only. `fetch_all_cryptopanic_posts` (`scout/db.py:4314`) exists as a SELECT helper but has zero callers in `scout/`.
- Activating without a validated research need produces archival data nobody uses (the "data nobody uses" antipattern).
- §12a discipline (global CLAUDE.md): shipping a monitored pipeline table without a consumer is the exact failure shape the silent-failure audit was created to surface; should not repeat it.

**Activation conditions (when operator chooses Path C — must ship as ONE coherent PR, not piecewise):**
1. **Both flags + token** in prod `.env`:
   - `CRYPTOPANIC_ENABLED=True` — enables fetch + persist
   - `CRYPTOPANIC_API_TOKEN=<free-tier-token-from-cryptopanic.com>` — fetch short-circuits to `[]` without it
   - `CRYPTOPANIC_SCORING_ENABLED=True` — gates the `cryptopanic_bullish` Signal 13 in `scout/scorer.py:197`. Flipping `_ENABLED` alone does NOT activate the scoring path.
2. **Scorer recalibration** — bump `SCORER_MAX_RAW` from 198 to ~208 (or whatever the new total is after Signal 13's +10) per memory `project_session_2026_04_20_bl052_bl053.md`. Requires a recalibration PR with weight verification, NOT just an `.env` change.
3. **Rate-limit decoupling** — listener currently fires once per main pipeline cycle. Current prod `SCAN_INTERVAL_SECONDS=60` → 60 req/hr → borderline of free-tier (50-200 req/hr per BL-053 design doc §3). Design assumed 300s (12 req/hr). At least one of:
   - revert pipeline cadence to 300s (broad blast radius across all modules — undesirable)
   - introduce a decoupled `CRYPTOPANIC_FETCH_INTERVAL_SECONDS` (cleanest; +5 LoC + tests; should be ≥120s for safety margin)
   - empirically verify the actual free-tier limit (request + sustain at 60/hr for 24h with monitoring) before deciding
4. **§12a freshness SLO** — add `cryptopanic_posts` to the audit-snapshot CLI's monitored-tables list (the CLI lands via PR #105 post-M1.5c gate). Pre-registered SLO suggestion: "writes within 1h of pipeline restart; alert if no writes for 4h."
5. **One coherent PR** — flags + recalibration + decoupled interval + SLO in the same change. Splitting reintroduces the deploy-without-activate trap.

**Cross-references:**
- BL-053 original design: `docs/superpowers/specs/2026-04-20-bl053-cryptopanic-news-feed-design.md`
- BL-053 plan: `docs/superpowers/plans/2026-04-20-bl053-cryptopanic-news-feed-plan.md`
- Deploy session memory: `project_session_2026_04_20_bl052_bl053.md`
- Activation gate: BL-NEW-CYCLE-CHANGE-AUDIT (next entry) feeds the decoupling decision in (3)
- Roadmap context: this backlog's "Virality Detection Roadmap" §2 ranks CryptoPanic as Source #2

### BL-NEW-CYCLE-CHANGE-AUDIT: audit design-time assumptions against current `SCAN_INTERVAL_SECONDS`
**Status:** SHIPPED 2026-05-13 — findings doc at `tasks/findings_cycle_change_audit_2026_05_13.md`. **Audit reframed mid-execution**: plan-review verified via `git log --all -S "SCAN_INTERVAL_SECONDS" -- scout/config.py` that gecko-alpha has had `SCAN_INTERVAL_SECONDS = 60` since the initial scaffold commit `bbf6810` (2026-03-20). The "300s era" cited in this filing was coinpump-scout heritage; gecko-alpha was scaffolded from coinpump-scout and inherited design docs assuming the upstream cycle. Per-module reframing on "does each design-doc cycle-math assumption hold at gecko-alpha's 60s cycle?" Five-bucket classification (Phantom / Phantom-fragile / Watch / Borderline / Broken) + Unfalsifiable meta-bucket. 13 per-finding carry-forward BL-NEW-* entries filed for follow-up (Helius / Moralis / CG burst-profile / GeckoTerminal 429-handler + ethereum 404 / Anthropic spend target / Tier C2 SLOs / Tier F documentation pass / BL-060 cycle-verify / SQLite WAL profile). Next-audit trigger: SCAN_INTERVAL change OR new external API OR new *_CYCLES setting OR write-rate ±2× OR 2026-11-13.
**Surfaced 2026-05-11 during BL-053 §2.2 closure analysis.** The default `SCAN_INTERVAL_SECONDS` decreased from **300s to 60s** at some point between BL-053's design (which assumed 300s → 12 req/hr CryptoPanic, "well under any free-tier cap") and the current deployed state (60s → 60 req/hr, **at the low end** of the 50-200/hr CryptoPanic free-tier band). BL-053 is one concrete instance; **other modules with design-time rate-limit / throttle / polling / cache-TTL / backoff-window assumptions may have silently become broken or borderline by the cycle change.**
**Tag:** `audit` `structural-attribute-verification` `silent-degradation` `§9b`

**Why:** This is a structurally different audit class than the silent-failure audit (`findings_silent_failure_audit_2026_05_11.md`). That audit was **table-freshness-based** — does the writer still produce rows? This audit would be **assumption-validity-based** — does the code's design-time math still hold given a known config change? §9b (structural-attribute verification) territory.

**Investigation scope:**
1. **Find all time-based design-time math** — grep for `SCAN_INTERVAL`, `req/hr`, `req/min`, `requests per`, `rate_limit`, `backoff`, `interval`, `TTL`, `cache_seconds` across `scout/`. List every place where a design-time computation assumed a specific cycle frequency.
2. **For each finding, classify:**
   - **Phantom drift** — design-time computation still holds at 60s (the assumption had wide margin)
   - **Borderline** — at 60s, math just barely fits; one bad cycle would tip it (BL-053 is this case)
   - **Broken** — at 60s, the assumption is violated; module is silently rate-limited or throttling itself
3. **Cross-reference each module's external rate limit** (CoinGecko, GeckoTerminal, DexScreener, GoPlus, Helius, Moralis, etc.) against the current cycle math.
4. **Report:** list of `(module, design-assumption, current-validity, severity, fix-shape)`.

**Drift verdict:** NET-NEW. No existing backlog entry tracks assumption-validity audit. Sibling to `BL-NEW-CI-MASTER-BROKEN` (test-validity audit) and the silent-failure audit (table-freshness audit).

**Hermes verdict:** No skill covers config-change-impact analysis on time-based assumptions. Project-internal.

**Estimate:** ~2-3 hours focused investigation + ~30 min report write-up. Per-finding fix scope varies (most likely 1-line interval-decoupling additions; in rare cases, full module reworks).

**When to run:** Not urgent — system is running, no acute breakage. Schedule as a dedicated session with clean head, not during a wait window. Could naturally bundle with BL-053 reactivation (which needs investigation finding #3's resolution anyway).

**Cross-references:**
- BL-053 deactivation (immediately above) — first concrete instance of cycle-change-drift
- `tasks/findings_silent_failure_audit_2026_05_11.md` §2.2 closure — discovery context
- `feedback_section_9_promotion_due.md` — methodological framing (§9b structural-attribute verification)

### BL-034: Set up MiroFish Docker integration
**Status:** DROPPED — Claude Haiku fallback is sufficient, gate lowered to MIN_SCORE=25
**Files:** docker-compose.yml, scout/mirofish/client.py
**Why:** MiroFish is the key differentiator but hasn't been tested locally yet. Currently all narrative scoring goes through the fallback.
**Changes:** Clone MiroFish repo, configure LLM keys, test /simulate endpoint, verify seed format compatibility

---

## P2 — Operational hygiene + agent-framework integrations

### BL-072: Operational alignment doc + new-primitives convention + pre-write hook
**Status:** SHIPPED 2026-05-03 (this PR)
**Tag:** `convention` `tooling` `enforcement`
**What shipped:**
- `docs/gecko-alpha-alignment.md` — 4-part operational hygiene reference (deployed patterns / drift checklist / working agreement / explicit limits)
- `.claude/hooks/check-new-primitives.py` — PreToolUse hook gating `tasks/(plan|design|spec)_*.md` on the `**New primitives introduced:** [list or NONE]` line
- `.claude/settings.json` — hook registered as 4th PreToolUse entry (matcher `Write|Edit|MultiEdit|NotebookEdit`); preserves existing 3 PreToolUse hooks + 2 PostToolUse blocks + Stop hook
- `CLAUDE.md` — "Plan/Design Document Conventions" sub-heading under existing "Coding Conventions" (no new top-level)
**Why:** chain_patterns auto-retired silently for 17d (2026-04-14 → 2026-05-01) because no convention surfaced the silent-failure surface at proposal time. This PR codifies the surface so future plans declare what infrastructure they add, and the hook prevents drift mechanically (not by discipline alone).
**No production code modified.** No DB migration. Existing tests still pass.
**Honest limitation:** the hook checks the marker EXISTS — does NOT validate that the listed primitives are truthful. Human PR review verifies accuracy. Documented in `docs/gecko-alpha-alignment.md` Part 4 and `CLAUDE.md`.

### BL-073: Hermes Agent integration roadmap
**Status:** RESEARCH-GATED — Phase 0 DONE 2026-05-03; Phase 1 unfunded
**Tag:** `research-gated` `hermes` `cost-gated` `90-day-cancellation`
**Realistic outlook:** Phase 1 (GEPA on `narrative_prediction` LLM prompt) is the one Hermes capability with concrete projected value for gecko-alpha. Cost gate revised down to ~$10 + ~1 day work after Phase 0 identified `NousResearch/hermes-agent-self-evolution` as a near-complete starting framework. Trigger to start: operator commits funding + bandwidth.

**Phases:**

| # | What | Cost | Starting framework | Trigger | Status |
|---|---|---|---|---|---|
| 0 | Browse Hermes skills hub + ecosystem for relevant skills | 1h | — | operator-driven | DONE 2026-05-03 — 671 skills hub identified, frameworks chosen for Phases 1+2, ≥3 honest rejects logged. See `tasks/notes_agentskills_browse_2026_05_03.md` |
| 1 | GEPA evolve `narrative_prediction` LLM prompt against the 1,274-row `predictions` table eval set (42 HIT / 40 MISS / 566 NEUTRAL / 561 UNRESOLVED) | $10 + ~1d (was 2d before Phase 0) | `NousResearch/hermes-agent-self-evolution` (MIT, 2.7k stars, DSPy+GEPA pipeline). Hermes built-ins: `dspy`, `evaluating-llms-harness`, `weights-and-biases` from the 671-skill hub | operator commits | unfunded |
| 2 | Hermes ops agent on VPS — Telegram NL access to gecko-alpha state, scheduled cron checks, cross-platform messaging gateway | ~0.5–1d (was 1-2d before Phase 0) + $5/mo | `JackTheGit/hermes-ai-infrastructure-monitoring-toolkit` (near-drop-in: Telegram bot + cron + monitoring). Optional fleet view: `builderz-labs/mission-control` (3.7k stars) | operator approves new VPS service | unfunded |
| 3 | Model routing for narrative LLM via OpenRouter (200+ models, ensemble, A/B against the Phase 1 eval harness) | 2-3d + variable per-model cost | reuse Phase 1 eval harness | Phase 1 eval harness exists | gated on Phase 1 |
| 4 | BL-064 cross-platform expansion via Hermes gateway (Discord/Slack curator channels in addition to Telegram) | 2-3d | reuse Phase 2 gateway | BL-064 14d soak (2026-05-11) shows curator-side trade dispatch works on Telegram first | gated on BL-064 soak |
| 5 | Atropos RL infrastructure for tool-calling model training | n/a now | — | ≥1000 trades/signal stable for 30d (per memory `feedback_ml_not_yet.md`) | gated on data volume — months out |

**Honest cancellation criterion (REVISED post-Phase 0):** Original criterion was "close as won't-fix by 2026-08-03 if Phase 1 hasn't started". With Phase 1 work halved by `hermes-agent-self-evolution`, the activation barrier is mostly operator attention rather than engineering risk. Re-evaluate this criterion at the +30d check (2026-06-03) — if it still looks like the right call, keep it; if Phase 1 looks like a no-brainer, drop the cancellation criterion. Status checks: +30d (2026-06-03), +60d (2026-07-03), +90d (2026-08-03).

**Realistic outcome 4 weeks from now:** Phase 0 done (it is), Phase 1 may now be cheap enough to attempt opportunistically.
**Realistic outcome 90 days from now:** Phase 1 + Phase 2 shipped (positive case, more plausible after Phase 0) OR still unfunded (worst case — re-evaluate cancellation).

**Honest reject reasons logged in Phase 0:** `chainlink-agent-skills` (wrong oracle model), `hxsteric/mercury` (wrong problem — execution routing, not signals), `ripley-xmr-gateway` (wrong chain), no paper-trade skill exists, no CoinGecko/DexScreener-specific skill, no SQLite-audit-log skill.

**Adapted from `Trivenidigital/shift-agent` analysis:** the inspiration for BL-072 + BL-073 was `shift-agent`'s `docs/hermes-alignment.md` + `CLAUDE.md` "Hermes-first" rules. shift-agent **runs on Hermes** as its production runtime; gecko-alpha does NOT (vanilla async Python pipeline). The adaptation is structural — we kept the 4-part doc shape and the read-deployed-code rule, dropped the Hermes-specific drift-tag vocabulary as cargo-cult, and replaced it with the more answerable single-line `**New primitives introduced:** [list or NONE]` declaration.

### BL-NEW-HERMES-CRYPTO-SKILLS-TRACKING: track crypto-relevant Hermes ecosystem capabilities
**Status:** SHIPPED-RESEARCH 2026-05-14 — commit `acf4b8e docs(hermes): track crypto skill ecosystem and debt audit (#119)`. Research note at `tasks/research_hermes_crypto_skills_2026_05_14.md`. Ongoing-tracking entry; not "closed" but no further build action queued. Status updated per `tasks/findings_backlog_drift_audit_2026_05_16.md`.
**Tag:** `hermes-first` `crypto-skills` `research-gated` `agent-framework-integrations`
**Why:** The Top Gainers gap investigation found new crypto-relevant skill surfaces outside the original May 3 Hermes pass: CoinGecko's first-party Agent SKILL, GoldRush/Covalent agent skills + Hermes MCP path, HermesHub as an early registry, and updated awesome-hermes-agent entries. These are not runtime replacements for gecko-alpha ingestion today, but they must be tracked so future custom-code proposals do not skip cheaper skill/API-reference paths.

**Tracked findings:**
- CoinGecko Agent SKILL (`coingecko/skills`) - first-party SKILL-compatible API knowledge for CoinGecko endpoints and workflows. Use as API-reference input for CoinGecko ingestion designs.
- GoldRush Agent Skills (`covalenthq/goldrush-agent-skills`) + GoldRush Hermes MCP guide - candidate future path for wallet/holder/transfer/DEX-pair intelligence, not a CoinGecko breadth replacement.
- HermesHub (`amanning3390/hermeshub`) - early curated Hermes skill registry; add to future Hermes-first search surface.
- Existing VPS Hermes install still has project-owned X/KOL skills (`kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, `xurl`) but no installed CoinGecko/GeckoTerminal market-breadth runtime skill.

**Decision:** For the current Top Gainers miss, do not hand off runtime ingestion to Hermes. Instead, cite the CoinGecko SKILL/API docs in the forthcoming CoinGecko breadth/hydration design and keep gecko-alpha responsible for persistence, dedupe, signal tables, watchdogs, and dashboards.

**Backlog replacement / re-scope matrix (2026-05-14):**

| Existing backlog area | Skill discovery impact | Decision |
|---|---|---|
| BL-032 social signal source | Hermes X/KOL path is now installed and live-adjacent (`xurl`, `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`). | Re-scope away from new custom Twitter/LunarCrush code. First evaluate existing Hermes/Telegram rows as the source for a social-confirmation feature. |
| BL-043 Prometheus/Grafana | HermesHub communication skills and BL-073 Phase 2 ops-agent path may cover some operator-facing monitoring. | Keep deferred; do not build full custom metrics stack until Hermes ops path is accepted/rejected. DB freshness/watchdog primitives remain gecko-alpha-owned. |
| BL-075 slow-burn watcher | CoinGecko SKILL improves API correctness; GoldRush can later validate on-chain accumulation. | Keep gecko-alpha-owned signal persistence, but require CoinGecko SKILL citation in the design and defer on-chain enrichment to a provider audit. |
| BL-NEW-HELIUS-PLAN-AUDIT / BL-NEW-MORALIS-PLAN-AUDIT | GoldRush MCP/skills overlap with wallet, holder, transfer, price, DEX-pair, and security-data use cases. | Compare against GoldRush before spending time on provider-specific throttles/upgrades or new custom enrichment code. |
| Virality / Early Detection roadmaps | Old roadmap overweighted custom LunarCrush, Twitter, Dune, Nansen, and pump.fun builds. | Add Hermes-first overlay: Hermes X first for social, CoinGecko SKILL first for market-data design, GoldRush first for on-chain, custom only for residual runtime gaps. |

**Trigger to revisit:** Any future proposal that adds paid market-data APIs, on-chain holder/wallet analysis, x402/AgentCash spend, or new Hermes-installed skills/plugins for crypto data. Re-run installed-VPS inventory + public ecosystem check before coding.

**Kill criterion:** If no crypto-relevant Hermes ecosystem change is adopted by 2026-08-14 and no new custom market-data primitive is proposed before then, close this tracking entry as superseded by the standing AGENTS.md Hermes-first rule.

### BL-NEW-HERMES-FIRST-DEBT-AUDIT: classify existing custom-code debt against current Hermes ecosystem
**Status:** SHIPPED 2026-05-14 - findings doc at `tasks/findings_hermes_first_debt_audit_2026_05.md`.
**Tag:** `hermes-first` `custom-code-debt` `audit` `debt-reduction`
**Why:** The project already carries substantial custom code written before the Hermes-first discipline was consistently enforced. Future-only Hermes checks are not enough; the existing backlog and shipped modules need a one-time classification so the project stops adding custom surfaces where a skill/plugin can now own the workflow.

**Scope:** Audit backlog + shipped modules across five domains:
- Market data: CoinGecko, GeckoTerminal, DexScreener, CoinMarketCap-like fallbacks.
- Social/narrative: Telegram, X/KOL, `social_mentions_24h`, narrative classifier/dispatcher.
- On-chain enrichment: Helius, Moralis, holder/wallet/transfer/security checks, Dune/Nansen-style analytics.
- Ops/monitoring: watchdogs, dashboards, Prometheus/Grafana, operator notifications.
- Execution: Minara, live adapters, approval/dispatch boundaries.

**Output:** `tasks/findings_hermes_first_debt_audit_2026_05.md` with each item classified as:
- `KEEP_CUSTOM` - durable runtime/persistence/scoring primitive that gecko-alpha should own.
- `USE_SKILL_AS_REFERENCE` - skill improves API correctness/review, but runtime remains gecko-alpha.
- `REPLACE_WITH_HERMES` - existing/future custom code should be retired in favor of installed/upstream skill.
- `BRIDGE_TO_HERMES` - gecko-alpha emits/consumes a narrow interface while Hermes owns the workflow.
- `DELETE_OR_DEFER` - backlog item is stale or no longer worth building.

**Result:** Classified market ingestion, CoinGecko hydration, X/KOL social, Telegram social, narrative scanner, Helius/Moralis, Dune/Nansen/pump.fun roadmap, Prometheus/Grafana, watchdogs, operator alerts, Minara/live execution, and GEPA/eval. Highest-priority follow-ups:
1. CoinGecko breadth + trending hydration fix stays custom but must cite CoinGecko SKILL/API docs.
2. BL-032 must audit existing Hermes X + Telegram rows before any LunarCrush/custom Twitter work.
3. Helius/Moralis audits become provider-consolidation comparisons that include GoldRush.
4. Old LunarCrush/Santiment/Nansen/Dune/pump.fun roadmap entries are historical unless a new residual-gap design revives them.

### BL-NEW-COINGECKO-BREADTH-HYDRATION: widen CoinGecko signal discovery without new provider debt
**Status:** SHIPPED 2026-05-14 — commits `2487ad7` (impl) + `5e3417b feat(coingecko): hydrate trending and widen breadth (#121)` (PR merge). Status updated per `tasks/findings_backlog_drift_audit_2026_05_16.md`.
**Tag:** `signals` `coingecko` `breadth` `hydration` `hermes-first`
**Why:** Top Gainers audit showed the system can catch winners when they reach existing tables, but some CoinGecko-listed movers never enter `gainers_snapshots`, `volume_history_cg`, `momentum_7d`, `slow_burn_candidates`, or `velocity_alerts`. Two concrete gaps were found: `/search/trending` rows carried rank but not true market data, and the volume scan only covered the top 500 by volume.

**Drift/Hermes-first result:** Existing `scout/ingestion/coingecko.py` and `scout/main.py` already own CoinGecko ingestion and raw-market fan-in. Installed VPS Hermes skills/plugins do not include CoinGecko, GoldRush, market-breadth, top-gainer, or trending-hydration runtime skills. Public CoinGecko Agent SKILL is used as API-reference only; gecko-alpha keeps DB writes, scoring, watchdogs, and dashboards.

**What changed:** `fetch_trending` now hydrates trending IDs through one `/coins/markets?ids=...` request, preserving `cg_trending_rank` while populating true market cap, volume, price, and change fields. `fetch_by_volume` honors new `COINGECKO_VOLUME_SCAN_PAGES` (default 3; raises volume breadth from 500 to 750 while keeping main-cycle scheduled CoinGecko calls well under the 25/min limiter). `run_cycle` now feeds hydrated trending raw rows into the existing gainers/spikes/momentum/slow-burn/velocity raw-market surfaces.

**Verification:** TDD red/green for trending hydration, hydration-failure fallback, configurable volume page count, and raw-row combiner. Focused regression: `tests/test_coingecko.py tests/test_main.py tests/test_main_cryptopanic_integration.py tests/test_gainers_tracker.py tests/test_spikes_detector.py tests/test_slow_burn_detector.py` -> 77 passed. Broader run including `tests/test_heartbeat_mcap_missing.py` still shows the known baseline aioresponses URL-matching failures from PR #119, not a regression in this branch.

### BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN: free-tier market-rank scan for low-volume gainers
**Status:** SHIPPED 2026-05-14 — commit `4860692 feat(coingecko): scan midcap gainers (#124)` + docs follow-up `0ce1540`. Design at `tasks/design_coingecko_midcap_gainer_scan.md`. Status updated per `tasks/findings_backlog_drift_audit_2026_05_16.md`.
**Tag:** `signals` `coingecko` `gainers` `quality-over-quantity` `hermes-first`
**Why:** Exact-ID Top Gainers audit found the remaining misses (Playnance `playnance`, Bityuan `bityuan`, SAFEbit `safecoin`) are mid-cap CoinGecko tokens around rank 470-680 with low absolute volume. They were not top-1000 by volume in the audit sample and were not trending, so neither `fetch_by_volume` nor PR #121's trending hydration can catch them reliably.

**Hermes-first result:** No installed Hermes runtime skill replaces this. CoinGecko Agent SKILL/API docs can guide endpoint use; gecko-alpha should keep persistence, dedupe, signal tables, and dashboards.

**What changed:** Added `fetch_midcap_gainers()` as a cadence-gated CoinGecko `market_cap_desc` rank-band lane. Defaults scan pages 2-4 every 3 cycles, require rank 251-1000, 24h change >= 25%, volume >= $250K, market cap $10M-$500M, cap output to 20 rows/cycle, and clear `last_raw_midcap_gainers` on every disabled/off-cadence/outage path to prevent stale replay. Returned `CandidateToken`s join the normal aggregate/enrich/score path, and gated raw rows join price cache plus gainers/spikes/momentum/slow-burn/velocity raw-market surfaces.

**Verification:** TDD red/green for rank/quality filtering, page-failure preservation, outage stale-cache clearing, disabled/off-cadence clearing, and `run_cycle()` aggregate/raw-cache integration. Focused regression: `tests/test_coingecko.py tests/test_main.py tests/test_main_cryptopanic_integration.py tests/test_gainers_tracker.py tests/test_spikes_detector.py tests/test_slow_burn_detector.py` -> 83 passed.

### BL-074: Minara as live-execution layer (post-BL-055 unlock)
**Status:** PHASE 0 Option A SHIPPED 2026-05-11 — see BL-NEW-M1.5C below. Subsequent phases (Option B execution-on-VPS + adapter shape decision) remain gated on BL-055 unlock. Captured 2026-05-03.
**Tag:** `phase-0-shipped` `gated-on-BL-055` `live-execution` `minara` `hermes-ecosystem`
**Vision:** gecko-alpha alerts in → Minara executes out. gecko-alpha continues to own signal generation, conviction gating, and observability; Minara owns wallet custody, venue routing (EVM + Solana + Hyperliquid perps), order placement, and on-ramp. Two-layer architecture, clean separation.

**Why this is BL-074, not Phase N of BL-073:** BL-073 is about Hermes building blocks for gecko-alpha's *narrative LLM and ops agent*. Minara is a *live-execution skill pack* — different problem class. Lumping them would muddle the dependency graph (BL-073 phases are independent of BL-055; this work absolutely is not).

**Hard prerequisites (all from BL-055 unlock criteria, copied here so they don't drift):**
1. BL-055 shadow soak passes 7d clean (per memory `project_bl055_deployed_2026_04_23.md`).
2. `scout/live/balance_gate.py` implemented (currently the live path raises `NotImplementedError`; verified 2026-05-03 — file does not exist).
3. `would_be_live` paper-trade subset has been validated against actual outcomes (per `feedback_paper_mirrors_live.md` — capital-constrained FCFS-20-slots subset must show positive PnL before risking real capital).
4. Operator writes a live-execution policy: capital allocation rules, per-trade size limits, daily loss limits, kill-switch escalation, custody approach (hot wallet vs. external signer), regulatory posture.
5. Operator explicit go-ahead.

**Architectural choices to revisit when the gate opens (do NOT pre-decide now):**
- **Adapter shape.** Minara is an *agent skill* (NL commands like "Buy 100 USDC of ETH"), not a CCXT-style REST client. Existing `scout/live/adapter_base.py` + `binance_adapter.py` pattern assumes the latter. Either: (a) write `MinaraAdapter` that shells out to `minara` CLI (npm package `minara@latest`) translating intents to NL commands — bypasses Hermes entirely, treats Minara as a thin executor; (b) gecko-alpha publishes structured trade intents to a queue (Redis/SQLite outbox), separate Hermes+Minara process subscribes — preserves agent UX, adds infra; (c) keep alerts → Telegram → operator → Hermes+Minara as today, no integration. Decision belongs in a future spec, not in this entry.
- **Custody.** Minara wallet is a hot wallet on the same host as gecko-alpha → blast radius if VPS compromised. Mitigations to evaluate: per-trade size limit, separate signing host, hardware key, withdraw-only kill switch.
- **Failure semantics.** What does gecko-alpha do if Minara is down at the moment of a high-conviction alert? Queue and retry, or fail-closed and alert operator? (Default fail-closed — execution layer outage should not silently drop signals.)
- **Reconciliation.** Minara executions need to flow back into `paper_trades`/`live_trades` tables for the existing PnL/audit/dashboard surfaces, otherwise we lose end-to-end traceability.

**Reference:** `Minara-AI/skills` (MIT, 263⭐ as of 2026-05-03, last push 2026-04-21). 88/100 self-reported on `Minara-AI/crypto-skill-benchmark` (Sonnet 4.6, 76 scenarios — note self-reported). Multi-chain: Ethereum, Base, Arbitrum, Optimism, Polygon, Avalanche, Solana, BSC, Berachain, Blast, Manta, Mode, Sonic, Conflux, Merlin, Monad, Polymarket, XLayer, Hyperliquid (perps).

**Honest reality check:** Until items 1–4 of the prerequisites above are real, this entry is a vision artifact, not an actionable backlog item. Re-evaluate when BL-055 reaches the unlock checkpoint. Don't let it accrete into a spec prematurely — premature spec for a system whose upstream gate hasn't opened is exactly the BL-073-style theatre we just argued against.

**Operator-side evaluation worth doing now (zero gecko-alpha code change):** install Hermes + Minara on a terminal you control, manually execute a small number of trades on alerts gecko-alpha currently surfaces to Telegram. This is the cheapest way to assess Minara's execution quality on signals you already trust. Outcome of that trial directly informs adapter-shape choice (a) vs. (b) above.

### BL-NEW-LOW-PEAK-LOCK: apply conviction-lock widening to trail_pct_low_peak (P2)
**Status:** SHIPPED 2026-05-11 — PR #100 (`e960d68`) squash-merged + deployed VPS 2026-05-11T14:03Z. Fixes silent BL-067 contract violation at `scout/trading/evaluator.py:168` where conviction-lock widening was explicitly bypassed for low_peak trades. See `tasks/findings_sustain_winners_cut_losers_2026_05_11.md` §5 + memory `project_p2_low_peak_lock_shipped_2026_05_11.md`.
**Tag:** `osmo-fix` `bl-067-completion` `surgical-fix` `proof-of-mechanism-for-p1`
**Trigger:** OSMO #1838 (paper, 2026-05-10) — stack=3 conviction-locked, peaked +13.3%, trail-exited at 8.6% drawdown for +$11/+3.67%, then token ran +87% post-exit. Bug: the 8% `trail_pct_low_peak` fired despite stack=3 supposedly adding +10pp via BL-067.
**What shipped:** `_CONVICTION_LOCK_DELTAS` extended with `trail_pct_low_peak` field per stack tier (stack=2 +5pp, stack=3 +10pp, stack=4 +15pp; all cap at 25%). `conviction_locked_params()` returns the widened value when base supplies it (backwards-compat for `scripts/backtest_conviction_lock.py` which uses 3-field shape). Evaluator passes `sp.trail_pct_low_peak` in base + applies locked value via `dataclasses.replace`. Backwards-compatible — paper trades stay open at same rate; only trail width inside the low-peak branch widens for conviction-locked trades.

**Empirical justification:** n=75 trail-stop-winners-with-peak<20% show uniform 10pp giveback across all signal types AND all mcap tiers ($5M-$250M+). Mcap-tier hypothesis explicitly **TESTED AND REJECTED** — findings §4.5.

**Blast radius (verified 2026-05-11):** 10 currently-open stack=3 gainers_early trades with peak<20%, $3,000 capital. Realistic 14d sample: 3-7 closes.

**Pre-registered evaluation criteria (locked, see findings §5):**
- **Success:** ≥50% qualifying closes giveback ≤5pp + mean ≤6pp + none >15pp
- **Failure:** (≥2 SL paths at -25% loss) OR (≥3 expiry worse than baseline 8% trail would have realized = peak × 0.92) OR (mean PnL across qualifying closes <0)
- **n<5 at D+14:** positive → extend soak 14d (do NOT proceed to P1); negative → revert; neutral → extend

**Dependency:** P1-uniform width-lock backtest is GATED on P2 success. If P2 fails, P1 does NOT auto-ship — re-scope based on revealed failure mode.

**What this does NOT close:**
- 91% of n=75 finding surface (99 non-locked trades with peak<20%) — pending P1-uniform after width-lock backtest (scoped findings §6.5, infrastructure verified in `scripts/backtest_conviction_lock.py`)
- Moonshot floor neutralization at peak≥40% — tracked separately in `tasks/findings_moonshot_floor_nullification.md`
- Conviction-lock now operates in two of three peak regimes (low_peak ✅ + middle band ✅), still neutralized at moonshot regime ❌

**Revert:** `UPDATE signal_params SET conviction_lock_enabled=0` (disables BL-067 entirely incl. the widening). For narrower revert, `PAPER_CONVICTION_LOCK_ENABLED=False` in .env + restart.

**D+14 evaluation:** 2026-05-25T14:03Z. Query template in memory file.

### BL-NEW-LIVE-ELIGIBLE: would_be_live writer with tier-based eligibility (BL-060 revival)
**Status:** SHIPPED 2026-05-11 — PR #98 (`8a07662`) squash-merged + deployed VPS 2026-05-11T13:22Z. Closes the ~3-week-old BL-060 writer gap (column existed since 2026-04-23 but all 752 closed trades had NULL/0). See data analysis `tasks/findings_live_eligibility_winners_vs_losers_2026_05_11.md` + memory `project_live_eligible_writer_shipped_2026_05_11.md`.
**Tag:** `observability` `bl060-revival` `data-driven-thresholds` `pre-execution-routing`
**What shipped:** Tier-based `would_be_live` stamping on every paper-trade open:
- **Tier 1 (mandatory):** `chain_completed` (any) OR `conviction_locked_stack >= 3` — historical n=27, 77.8% WR, $47/trade
- **Tier 2 (high-quality):** `volume_spike` (any spike_ratio) OR `gainers_early` AND `mcap >= $10M` AND `price_change_24h >= 25%` — historical n=95, 55.8% WR
- **FCFS cap** `PAPER_LIVE_ELIGIBLE_SLOTS=20`: stamps 1 only if Tier 1/2 AND under cap. Closed trades don't occupy slots.
- 3 new tunable Settings (`PAPER_LIVE_ELIGIBLE_SLOTS`, `PAPER_TIER2_GAINERS_MIN_MCAP_USD`, `PAPER_TIER2_GAINERS_MIN_24H_PCT`)
- Pure observability — **NO production behavior change**. Paper trades open at same rate; column just records membership.

**PR-stage V1 reviewer folds (5b8e4e6):**
- IMPORTANT: docstring tightened to acknowledge SELECT-then-INSERT race (1-2 over-stamp possible under burst opens; acceptable for observation, must wrap in `db._txn_lock` when live trading routes through)
- NIT: skip `compute_stack` DB call for `chain_completed`/`volume_spike` (unconditionally Tier 1a/2a, stack value unused)
- NIT: annotate evaluator long_hold partial-TP reopen with explicit "settings omitted by design" intent comment

**Why this BL number:** revives BL-060 (paper-mirrors-live) with the data-derived gate that the original quant-score-based plan would not have caught. Original BL-060 design preserved in `docs/superpowers/plans/2026-04-23-bl060-paper-mirrors-live.md` for historical reference.

**Verification queries:**
```bash
ssh root@89.167.116.187 "sqlite3 /root/gecko-alpha/scout.db \"SELECT signal_type, would_be_live, COUNT(*) FROM paper_trades WHERE opened_at > datetime('now','-24 hours') GROUP BY signal_type, would_be_live\""
# expect post-deploy rows to have would_be_live = 0 or 1 (not NULL)
ssh root@89.167.116.187 "sqlite3 /root/gecko-alpha/scout.db \"SELECT MIN(opened_at), MAX(opened_at), COUNT(*) FROM paper_trades WHERE would_be_live=1\""
# expect monotonic accumulation post-13:22Z
```

**Revert:** Set `PAPER_LIVE_ELIGIBLE_SLOTS=0` in `.env` + restart (all stamps become 0). No DB cleanup. Existing rows untouched.

**Follow-up items (NOT in this PR):**
- Dashboard surface for `would_be_live=1` cohort PnL (separate small UI change)
- Weekly digest A/B comparing live-eligible cohort vs unfiltered firehose
- Make race-strict (wrap SELECT+INSERT under `db._txn_lock`) once live trading routes through this filter

### BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT: structural live-evaluability per signal_type
**Status:** SHIPPED 2026-05-17 — branch `feat/live-evaluable-signal-audit` (cycle 7, analysis-only; V36 fold). Findings in `tasks/findings_live_evaluable_signal_audit_2026_05_17.md`. Key conclusions: 3 of 9 observed signal_types are structurally live-eligible (`chain_completed` Tier 1a, `volume_spike` Tier 2a, `gainers_early` Tier 2b). Post-cutover (≥ 2026-05-11T13:52Z) empirical confirmation: `gainers_early` 28.2% eligible, `volume_spike` 100%, `losers_contrarian` + `narrative_prediction` 0% (structurally non-eligible — ~48% (118/248) of post-cutover paper volume from types that can never go live). `first_signal` Tier 1b reachable in principle (1 stack-3 trade pre-cutover); separate follow-up to drill cause of 16-day silence. 2 surface findings filed as follow-ups: BL-NEW-CHAIN-COMPLETED-SILENCE-AUDIT + BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION.

**Original status (now historical):** PROPOSED — surfaced 2026-05-12 during Step 1 verification of "(2) would auto-suspend-against-=1-cohort have spared trending_catch / first_signal." Implementation deferred to next live-trading roadmap revisit.
**Tag:** `observability` `live-roadmap-input` `structural-evaluability` `tier-rule-coverage`
**Why:** Both trending_catch and first_signal are **structurally non-eligible** under current Tier 1/2 rules — their signal_data shape caps the stack count below the Tier-1b threshold of 3. This is the load-bearing argument; the empirical data corroborates it but cannot prove it on its own:

- `trending_catch` — signal_data is `{"source": "trending_snapshot", "mcap_rank": N}` only; fires alone from the trending-snapshot ingestion path; **max stack = 1 by design**.
- `first_signal` — admission rule (`scout/config.py:369`, `FIRST_SIGNAL_MIN_SIGNAL_COUNT=2`) requires ≥2 stacking signals; observed signal_data carries exactly 2 (momentum_ratio + cg_trending_rank); **max stack = 2 by design**.

Corroborating empirical data (Vector B T-TIGHT-2 fold — demoted to corroboration, not load-bearing): Step 1 saw 0/108 trending_catch and 0/253 first_signal trades with `conviction_locked_stack >= 3` in their pre-kill cohorts. The first_signal "0/253" figure is partly an artifact — BL-067 conviction-lock didn't deploy until 2026-05-04, so the column was uniformly NULL during the cohort window. The structural cap is what makes the claim hold even where the empirical record can't reach.

The auto-suspends weren't wrong (paper losses were real), but they also weren't *answering* the question "would live trading on this signal lose money," because live trading on this signal was structurally impossible under current Tier 1/2 rules.

**Drift verdict:** NET-NEW. No existing entry audits the structural live-eligibility surface per signal_type. BL-NEW-LIVE-ELIGIBLE shipped the writer; this entry asks what the writer can never stamp `=1` for and why.
**Hermes verdict:** No Hermes skill covers signal-type × eligibility-rule coverage analysis. Project-internal.

**Effect (proposed):** For each signal_type currently producing paper trades, compute:
1. **Structural max conviction_stack** — the maximum number of co-occurring signals possible at open time given the signal's source (e.g., trending_catch fires alone from `trending_snapshot` → max stack = 1; first_signal stacks on momentum+trending → max stack = 2; gainers_early can carry multiple co-firing signals → max stack ≥ 3 possible).
2. **Empirical eligible-subset rate** — historical % of trades where `compute_would_be_live` would have returned 1 (post-2026-05-11 writer for forward; backfill via `matches_tier_1_or_2()` against historical signal_data for prior rows).
3. **Tier rule path coverage** — which Tier 1a/1b/2a/2b path admits the signal_type (or none).

**Interpretation:** signal_types with structural max stack < 3 AND signal_type ∉ {chain_completed, volume_spike, gainers_early-with-gate} have *structurally empty* eligible subsets — they are not live-trading candidates regardless of paper performance. Their continued resource consumption (paper slots, alert noise, calibration cycles, MiroFish jobs) should be evaluated against that constraint at the next live-trading roadmap revisit.

**Known instances from Step 1:**
- `trending_catch` — max stack = 1 (single-source from trending_snapshot); not in Tier 1a/2a/2b; **structurally non-eligible**
- `first_signal` — max stack = 2 (momentum_ratio + cg_trending_rank pair); not in Tier 1a/2a/2b; **structurally non-eligible**

**Other candidate signal_types to audit when this runs:** `losers_contrarian`, `narrative_prediction`, `tg_social` (each may or may not be structurally stackable to ≥3 — empirical question).

**Not in this PR:** dashboard surface for the audit results (could fold into BL-NEW-LIVE-ELIGIBLE's dashboard view), or a settings-driven "signal_types in scope for live evaluation" allowlist that excludes structurally-empty types from auto-suspend / calibration / alerting calculations.

**Estimate:** ~2 hours analysis + ~1 hour write-up. No code change for the audit itself.

### BL-NEW-Q2-SIMULATOR: paired counterfactual for the live-eligibility evaluation
**Status:** FOLDED-INTO-SIGNAL-TRUST-ROADMAP 2026-05-22 - still conceptually useful, but should be scoped inside signal-family scorecards / live-readiness evidence rather than as a standalone simulator first.
**Tag:** `evaluation-framework` `q2-simulator` `live-roadmap-gate` `paired-counterfactual`
**Why:** The dashboard cohort view (BL-NEW-LIVE-ELIGIBLE follow-up) measures whether the eligible cohort diverges from the full cohort. That answers Q1 (cohort identification). But the strategic question — Q2: *"is eligible-cohort evaluation worth the statistical cost of smaller n?"* — requires a different artifact entirely: a paired simulator that, for each historical operational decision made on the full cohort (auto-suspend fires, calibration parameter changes, alert routing thresholds), shows what the same decision would have been if made on the eligible subset.

Without Q2's answer, the 4-week dashboard verdict still leaves the operator with: *"yes the cohorts diverge — but would acting on the divergence have led to better operational outcomes, or just noisier ones at small n?"* That's the actual gate on whether (2)/(3)/(4) are worth pursuing.

**Drift verdict:** NET-NEW. The dashboard view is observational; no existing artifact does the counterfactual decision-replay. `scripts/backtest_*.py` family is closest precedent but each is single-purpose.

**Hermes verdict:** No Hermes skill covers paired-counterfactual decision-replay for cohort comparisons. Project-internal.

**Effect (proposed):** A `scripts/q2_simulator.py` that, for a window of historical operational events (auto-suspends, calibration changes, threshold flips), replays each event against both cohorts and reports:
- Decisions that would have been *different* under eligible-cohort gating (fire fewer / fire later / fire never)
- Operational outcome delta (PnL, win-rate, drawdown) under each branch
- Per-decision sample size at decision time (gates the confidence interval on each comparison)

**Sequence:** scoped after the 4-week dashboard verdict produces evidence — only worth building if Q1's answer is non-trivial. Filing now so Q2 doesn't get implicitly "answered" by sunk-cost reasoning at the 4-week mark.

**Estimate:** ~6-8 hours simulator + ~2 hours findings doc.

### BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST: scheduled-summary shape for the 4-week evidence window
**Status:** SHIPPED 2026-05-17 — branch `feat/live-eligible-weekly-digest` (5 commits + plan + design + V27/V28/V29/V30 folds). `scout/trading/cohort_digest.py` (`build_cohort_digest` + `send_cohort_digest` + `_compute_signal_cohort_stats` + `_compute_all_cohorts_stats` + `_classify_verdict` + `_detect_verdict_flip` + `_build_final_block`). Verdict-classification logic mirrors `dashboard/frontend/components/TradingTab.jsx:389-425` verbatim (`STRONG_WR_GAP_PP=15.0` STRICT >, `STRONG_PNL_FLOOR_USD=200.0` both cohorts, sign-flip required for strong-pattern, symmetric `|wrDelta|>5` moderate band, near-identical/INSUFFICIENT_DATA labels). Singleton state via `cohort_digest_state` (V30 INSERT OR REPLACE + sub-SELECT preserves other field). `paper_trades.closed_at` partial index added (V30 MUST-FIX). 8 Settings: `COHORT_DIGEST_ENABLED/N_GATE/DAY_OF_WEEK/HOUR/FINAL_DATE/STRONG_WR_GAP_PP/STRONG_PNL_FLOOR_USD/MODERATE_WR_GAP_PP`. `_run_feedback_schedulers` extended to `tuple[str,str,str]`; `last_cohort_digest_date` initialized from `cohort_digest_state.last_digest_date` on startup so same-day restart does NOT re-fire (V29 MUST-FIX). Pre-registered decision criteria in `tasks/plan_live_eligible_weekly_digest.md` § Decision criteria; filed follow-up `BL-NEW-COHORT-DIGEST-DECISION`. Memory checkpoint: `project_cohort_digest_decision_2026_06_08.md`.

**Original status (now historical):** PROPOSED — surfaced 2026-05-12 during Vector C strategy/framing review of the dashboard cohort view PR. Filed as a UX-shape improvement; doesn't block the dashboard.
**Tag:** `evaluation-framework` `attention-budget` `scheduled-summary` `digest-shape`
**Why:** The dashboard cohort view requires the operator to glance at it ~3× per day for 4 weeks looking for a low-probability divergence event across ~7 signal_types. That's a high vigilance cost for a small expected output. A scheduled weekly summary alert ("Week 2 of 4: gainers_early eligible n=14, wrΔ=+4pp, no sign-flip — tracking") followed by a single end-of-window verdict alert produces the same evidence at &lt;10% of the attention cost, with the dashboard available for ad-hoc drill-in when the operator chooses.

**Drift verdict:** NET-NEW. No existing weekly-digest covers the cohort comparison surface. Existing `scout/trading/weekly_digest.py` is signal-PnL-focused (not cohort-comparison-focused) but is the architectural neighbor.

**Hermes verdict:** No Hermes skill covers scheduled cohort-summary digests. Project-internal.

**Effect (proposed):** New weekly cron + `scout/trading/cohort_digest.py` writing a TG message with per-signal-type cohort comparison + verdict classification (matching dashboard's logic). At the 4-week mark, fire a final summary message with the decision-point recommendation.

**Sequence:** can ship anytime after the dashboard view. Independent of Q1 outcome.

**Estimate:** ~3-4 hours weekly digest + cron + tests.

### BL-NEW-COHORT-DIGEST-DECISION: act on cohort-digest 4-week evidence
**Status:** PROPOSED 2026-05-17 — filed concurrent with BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST shipping. Evidence-gated on the 4-week measurement window.
**Trigger:** 2026-06-08 (anchor — first eligible Monday at or after). Per V28 SHOULD-FIX fallback: digest fires on first eligible run with `end_date >= COHORT_DIGEST_FINAL_DATE AND last_final_block_fired_at IS NULL`.
**Pre-registered criteria** (per `tasks/plan_live_eligible_weekly_digest.md` § Decision criteria):
- EXTEND if per-signal flip events ≥ 2 within window (instability) — file `BL-NEW-COHORT-DIGEST-DECISION-EXTENDED`
- RECOMMEND-LIVE-REVIEW (exploratory) if 4 stable weekly digests AND any enumerated signal classifies "strong-pattern (exploratory)" — BL-055 gates auto-promote
- TRACK-WIDER if 4 stable weekly digests AND every signal is "moderate"/"tracking" — file `BL-NEW-COHORT-DIGEST-EXTEND-4w`
- INCONCLUSIVE if all signals stuck at INSUFFICIENT_DATA at 2026-06-08 — file `BL-NEW-COHORT-DIGEST-INCONCLUSIVE`
**Decision artifact:** findings doc + backlog flip + memory checkpoint update.
**decision-by:** 2026-06-08

### BL-NEW-HPF-RE-EVALUATION: re-evaluate `PAPER_HIGH_PEAK_FADE_DRY_RUN` flip decision at n≥20
**Status:** ACTIVE — D+7 review closed 2026-05-13T04:05Z (audit row id=25, `signal_params_audit.field_name='soak_verdict'`, value `dry_run_continued`). HPF dry-run produced n=7 would-fires by 2026-05-13; pre-registered criterion was ambiguous and aggregate counter-factual was −$45 vs actuals, so the flip is deferred rather than acted on. Continue accumulating toward n≥20.

**2026-05-13 closure — subset finding (structural, §9c lever-vs-data-path):**

Per-trade pattern is sharper than the aggregate:
- HPF beats `moonshot_trail` 3/3 (1699 +$81, 1765 +$81, 1815 +$76 → **+$238 total**) — moonshot floor (`PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30`) lets trades give back more than HPF's 60% retrace.
- HPF loses to existing `peak_fade` 3/4 (1811 −$185, 1638 −$87, 1836 −$44; 1791 +$31 → **−$285 net**) — existing `peak_fade` exits later and captures more upside.

HPF's 60% peak threshold fires *rarely* (7 over 7d vs ~64 actual `peak_fade` exits in the same window). The lever HPF appears to be ("fade high peaks earlier") is only meaningful for the **moonshot_trail subset** — overlapping with the parked high-peak-giveback finding (`project_session_2026_05_05_high_peak_park.md`). Turning HPF on globally would clip the profitable `peak_fade` exits short.

**Refined criterion-scope (added 2026-05-13):** the next n≥20 eval should be **stratified by actual exit reason**, not aggregate. Specifically: compute the counterfactual delta separately for the `moonshot_trail`-actual subset vs the `peak_fade`-actual subset. If the moonshot_trail subset is consistently positive at n≥10 within it, consider a *targeted* flip — e.g., only arm HPF when peak ≥ moonshot threshold — rather than the binary global flip the locked criteria below currently model.
**Tag:** `paper-trading` `high-peak-fade` `dry-run-extension` `heavy-tail-truncation`
**Why:** HPF dry-run was activated 2026-05-06T02:18Z on `gainers_early` + `losers_contrarian` per parent BL-NEW-AUTOSUSPEND-FIX. The pre-registered flip criterion ("If gate would have fired earlier AND counter-factual PnL is positive, flip `PAPER_HIGH_PEAK_FADE_DRY_RUN=False`") was ambiguous in practice — per-trade PnL positive on all 7 fires (would say flip), but aggregate USD vs actual exits was -$45/-4.0% (would say don't flip). The 3 trades where HPF capped heavy-tail winners (1811 -$185, 1638 -$87, 1836 -$44) are exactly the asymmetric-truncation risk that n=7 cannot resolve. Deferring to n≥20 (or +14d) reduces the sampling-noise interpretation.

**Drift verdict:** NET-NEW. BL-NEW-AUTOSUSPEND-FIX is in memory only (`project_bl_autosuspend_fix_2026_05_06.md`), not in backlog; this is the natural follow-up entry.
**Hermes verdict:** No Hermes skill covers heavy-tail-truncation evaluation. Project-internal.

**Counter-factual evidence at 2026-05-13 (n=7):**
| Trade | Signal | HPF exit% | Actual% | HPF $ delta |
|---|---|---:|---:|---:|
| 1836 | losers_contrarian | 95.6 | 110.2 | -$44 |
| 1811 | gainers_early | 60.5 | 122.1 | -$185 |
| 1815 | gainers_early | 45.1 | 19.6 | +$76 |
| 1791 | gainers_early | 42.2 | 31.8 | +$31 |
| 1765 | gainers_early | 36.9 | 9.9 | +$81 |
| 1699 | gainers_early | 34.4 | 7.3 | +$81 |
| 1638 | gainers_early | 44.7 | 73.6 | -$87 |

Aggregate: HPF $1,078 vs Actual $1,124. HPF improves 4/7 but the 3 heavy-tail caps dominate the $-delta.

**Refined criteria for next review (locked):**
- **Trigger:** earliest of (a) `SELECT COUNT(*) FROM high_peak_fade_audit WHERE dry_run=1` ≥ 20, OR (b) 2026-05-20T00:00Z.
- **Flip to live (`PAPER_HIGH_PEAK_FADE_DRY_RUN=False`):** aggregate HPF counter-factual $ ≥ actual exits $ by ≥ +5% across the full audit window AND no single trade shows HPF $ delta ≤ -$200.
- **Keep dry-run, extend +14d:** aggregate within ±5% (noise band) OR n still <20.
- **Disable HPF entirely (`PAPER_HIGH_PEAK_FADE_ENABLED=False`):** aggregate HPF counter-factual $ < actual exits $ by ≥ -10% AND ≥3 trades show HPF $ delta ≤ -$100 (heavy-tail-cap pattern confirmed).

**Verification query template (run at trigger):**
```sql
SELECT a.trade_id, p.signal_type,
       ROUND(a.peak_pct,2) AS hpf_peak,
       ROUND(a.retrace_pct,2) AS hpf_retrace_at_fire,
       ROUND((1 + a.peak_pct/100.0) * (1 - a.retrace_pct/100.0) * 100 - 100, 2) AS hpf_exit_pnl_pct,
       p.exit_reason, ROUND(p.pnl_pct,2) AS actual_pnl_pct, ROUND(p.pnl_usd,2) AS actual_pnl_usd
FROM high_peak_fade_audit a JOIN paper_trades p ON p.id = a.trade_id
WHERE a.dry_run = 1 ORDER BY a.fired_at DESC;
```

**Where to act:** `.env` PAPER_HIGH_PEAK_FADE_DRY_RUN / PAPER_HIGH_PEAK_FADE_ENABLED + restart pipeline. No code change.

**Parent context:** see memory `project_bl_autosuspend_fix_2026_05_06.md` § "Soak outcomes (2026-05-13 actuals)" for full per-trade table + reasoning.

**Estimate:** ~30 min query + decision + .env edit + restart.

### BL-NEW-M1.5C: Minara DEX-eligibility alert extension (Phase 0 Option A under BL-074)
**Status:** SHIPPED 2026-05-11 — PR #96 (`ef68c6c`) squash-merged + deployed VPS 2026-05-11T01:54Z. Schema 20260517 migration `bl_tg_alert_log_m1_5c_outcome` applied; M1.5b sentinel preserved across rebuild (verified `m1_5b_sentinel_preserved=true`). Onboarding TG announcement delivered. See memory `project_m1_5c_deployed_2026_05_11.md`.
**Tag:** `decision-support` `minara` `solana-first` `phase-0-option-a` `pre-execution-layer`
**What shipped:** TG paper-trade-open alerts now include a copy-pasteable line `Run: minara swap --from USDC --to <SPL_addr> --amount-usd 10` for Solana-listed tokens. Operator copy-pastes into their local terminal where Minara CLI is logged in. **gecko-alpha does NOT execute** — pure decision-support. Settings-sourced `MINARA_ALERT_AMOUNT_USD=10` default; caller's $300 paper-trade size cannot leak (R2-C1 discipline). 4-layer failure isolation in `maybe_minara_command` + base58 SPL shape validation (32-44 chars; rejects EVM-hex under solana key) + asyncio.CancelledError-safe sentinel demotion (clears 6h cooldown trap on dispatch cancel).
**Why this BL number:** Phase 0 Option A is the cheapest valuable step toward BL-074's vision. Adds gecko-alpha → Minara decision-support BEFORE the BL-055 unlock gates on full execution. Operator behavior during soak informs whether Option B (TG approval gateway + VPS-side execution) is worth scoping or whether Option A is sufficient.
**Forward kill criterion (per V3 strategy reviewer fold):** 14d post-deploy, count `minara_alert_command_emitted` log events vs. operator self-reported manual paste count. Decision tree:
- High emission + high paste rate → proceed to M1.5d Option B scoping.
- High emission + low paste rate → Option A was wrong product shape; defer Option B, revisit operator workflow.
- Low emission rate → re-examine Solana coverage rate; consider EVM expansion (M1.5d EVM, 17 chains supported by Minara).

**Verification queries (24h soak ends 2026-05-12T01:54Z):**
```bash
ssh root@89.167.116.187 "journalctl -u gecko-pipeline --since '24 hours ago' | grep -c minara_alert_command_emitted"
ssh root@89.167.116.187 "sqlite3 /root/gecko-alpha/scout.db \"SELECT COUNT(*) FROM tg_alert_log WHERE outcome='m1_5c_announcement_sent'\""  # expect 1
```

**Revert:** `MINARA_ALERT_ENABLED=False` + restart. No code rollback, no DB cleanup. Migration is forward-only but idempotent.

**Post-merge folds (deferred from 3-vector PR review):**
- Retrofit `**Hermes-first analysis:**` + `**Drift-check:**` sections into `tasks/plan_m1_5c_minara_alert.md` per CLAUDE.md §7 convention (V3-I1)
- Revisit `$10` default sizing after 7d soak (V3-I2)
- Document alternatives (bash function, dashboard column, skip-to-Option-B) in plan (V3-I4)
- Better migration test exercising rebuild path with pre-existing rows (V1-I2 — empirically validated on prod deploy, defer test-quality improvement)
- Operator runbook note: do not `DELETE FROM tg_alert_log WHERE outcome != 'sent'` or M1.5b + M1.5c announcement sentinels re-spam (V2-I4)

**3-vector PR review caught 3 CRITICAL pre-merge** (folded in commit `fff3658` pre-rebase): base58 SPL shape validation (V1-I1 + V2-I2 convergence), CancelledError sentinel-stuck (V2-I1), isinstance(dict) guard for CG schema drift (V1-I1).

### BL-NEW-MINARA-DB-PERSISTENCE: persist `minara_alert_command_emitted` events to DB for D+14 kill-criterion eval
**Status:** SHIPPED 2026-05-13/14 — commits `6e65e2e feat(minara): persist alert emissions` + `e628097 Merge pull request #112 from Trivenidigital/codex/minara-db-persistence`. Status updated per `tasks/findings_backlog_drift_audit_2026_05_16.md`. Original PROPOSED note retained below for context.

**Original status (now historical):** PROPOSED 2026-05-13 — surfaced during D+2 Minara verification on srilu-vps. M1.5c is operationally healthy (10 emissions in 48h covering 9 unique Solana tokens including `goblincoin`, `chill-guy`, `troll-2`, `useless-3`), but the V3-strategy kill-criterion at D+14 (2026-05-25) depends on counting `minara_alert_command_emitted` events vs operator self-reported manual paste count — and that event currently has **no DB-side persistence**, only structured logs in journalctl. journalctl retention defaults to ~30 days on systemd but can rotate earlier under disk pressure. The kill-criterion eval is one journalctl rotation away from being unverifiable.

**Tag:** `silent-failure-class-1` `minara` `m1_5c` `kill-criterion-substrate` `observability`

**Why:** Class 1 silent-failure shape per global CLAUDE.md §12a-style discipline — decision-bearing telemetry stored only in logs creates an availability dependency that's invisible until the dependency lapses. The kill-criterion at D+14 is the load-bearing eval for whether to scope M1.5d Option B (VPS-side execution); losing the data because journalctl rotated before the eval is run is a structural failure mode.

**Drift verdict:** NET-NEW. No existing entry covers Minara-emission persistence. BL-NEW-M1.5C (PR #96) shipped the emit logic but did NOT include DB-side row writes. The migration `bl_tg_alert_log_m1_5c_outcome` added `m1_5c_announcement_sent` to the `tg_alert_log.outcome` enum but no per-emit row schema.
**Hermes verdict:** No Hermes skill covers Minara-specific telemetry persistence. Project-internal.

**Effect (proposed):** Add a `tg_alert_log` write (or new sibling table `minara_alert_emissions`) inside `scout/trading/minara_alert.py:maybe_minara_command` immediately before/after the `minara_alert_command_emitted` log call. Columns: `id`, `coin_id`, `chain`, `amount_usd`, `command_text` (or hash of it), `emitted_at`, `paper_trade_id` (FK if applicable), `signal_type`. Plus an `operator_paste_acknowledged_at` column (NULL by default; future operator-facing UI lets them mark "yes I executed this").

**Pre-registered kill-criterion query (D+14 = 2026-05-25):**
```sql
SELECT DATE(emitted_at) AS day, COUNT(*) AS emitted,
       SUM(CASE WHEN operator_paste_acknowledged_at IS NOT NULL THEN 1 ELSE 0 END) AS pasted,
       ROUND(100.0 * SUM(CASE WHEN operator_paste_acknowledged_at IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS paste_rate_pct
FROM minara_alert_emissions
WHERE emitted_at >= '2026-05-11T01:54:00Z'
GROUP BY day ORDER BY day;
```
- High emit + high paste → Option B scoping per BL-NEW-M1.5C kill tree
- High emit + low paste → wrong product shape; revisit operator workflow
- Low emit → re-examine Solana coverage; consider EVM expansion (M1.5d EVM, 17 chains)

**Where to act:** `scout/trading/minara_alert.py` (add DB write); `scout/db.py` (new migration for `minara_alert_emissions` table OR new columns on `tg_alert_log`); `scout/trading/tg_alert_dispatch.py` (pass paper_trade_id through to maybe_minara_command).

**Backfill consideration:** the 10+ events already emitted since 2026-05-11 are in journalctl only. A one-time backfill script can parse the journalctl JSON lines into the new table — captures the soak window's history. Bounded by journalctl retention (~30 days max).

**Estimate:** ~2-3 hours for migration + write logic + backfill script + tests + PR review + deploy. Should ship before 2026-05-22 (D+11) to leave 3-day buffer for the D+14 query to have clean data.

### BL-NEW-MINARA-COOLDOWN-REVERIFY: re-verify Minara per-coin cooldown after parallel-session soak merges
**Status:** AUDITED-NO-VIOLATION 2026-05-19 — runtime verification on srilu-vps (`journalctl -u gecko-pipeline --since 2026-05-11 | grep minara_alert_command_emitted`) returned 54 total emits across 10 distinct multi-emit coins; **0 intra-coin gaps under 6h**. Shortest observed gap was `goblincoin` at ~17.5h (2026-05-11T22:26 → 2026-05-12T15:57), well above the 6h cooldown documented in BL-NEW-M1.5C. Empirical observation of `goblincoin` double-emit referenced in the original filing was already legitimate under the current cooldown. The conditional re-verify trigger ("parallel-session cooldown PR lands on gecko-alpha master") has not fired — no Minara cooldown PR is visible in `git log -- scout/trading/minara_alert.py scout/trading/tg_alert_dispatch.py` since 2026-05-13. Closing as AUDITED-NO-VIOLATION; reopen only if a parallel-session cooldown PR later lands on master AND a new audit returns gaps under the new threshold.

**Original status (now historical):** PROPOSED 2026-05-13 — filed defensively during D+2 Minara verification. Observation flagged + clarified, but the parallel-session PR is not yet visible from gecko-alpha master, so re-verify is appropriate once it lands.

**Tag:** `defensive-filing` `minara` `m1_5c` `cooldown` `parallel-session-coordination`

**Empirical observation (2026-05-13 verification, srilu-vps):** `goblincoin` (solana) emitted `minara_alert_command_emitted` twice — 2026-05-11T22:26:10Z and 2026-05-12T15:57:45Z, **17h apart**. Per BL-NEW-M1.5C line 599, the documented Minara cooldown is **6h** ("asyncio.CancelledError-safe sentinel demotion (clears 6h cooldown trap on dispatch cancel)"). 17h > 6h, so under the *currently-deployed* design the two emits are legitimate (cooldown expired correctly between firings).

**Why this entry exists (despite the above):** operator reports a newer cooldown PR is in soak on the parallel-session (shift-agent) side — not yet visible in gecko-alpha master commits as of 2026-05-13. If that PR changes the cooldown duration, behavior, or per-coin/per-signal scoping, the goblincoin double-emit may become non-legitimate or the design intent may shift. This entry is a checkpoint to re-verify *after* the parallel PR lands on master, not a claim of any current bug.

**Drift verdict:** NET-NEW filing, but the underlying mechanism is already covered by BL-NEW-M1.5C. This is observability of a soak window, not a new feature.
**Hermes verdict:** Not Hermes-relevant. Pure project-internal cooldown logic.

**Coordination note (2026-05-13):** the parallel Claude session owns shift-agent + may also own the cooldown work referenced. This entry's check should be deferred until: (a) the parallel session's cooldown PR is merged to gecko-alpha master, OR (b) the parallel session explicitly confirms the cooldown work is shift-agent-scoped and not coming to gecko-alpha.

**Pre-registered re-verification (run when triggered):**
```bash
# 1. Confirm cooldown PR landed on master (look for minara_alert.py or tg_alert_dispatch.py touch)
git log --since="2026-05-13" -- scout/trading/minara_alert.py scout/trading/tg_alert_dispatch.py

# 2. Sample double-emit cases on prod since the new cooldown took effect
ssh root@89.167.116.187 "journalctl -u gecko-pipeline --since '<post-merge timestamp>' \
  | grep minara_alert_command_emitted \
  | python3 -c 'import sys, json, collections; \
    rows=[json.loads(l.split(\":\",4)[-1].strip()) for l in sys.stdin if l.strip().startswith(\"{\")]; \
    by_coin=collections.defaultdict(list); \
    [by_coin[r[\"coin_id\"]].append(r[\"timestamp\"]) for r in rows if r.get(\"event\")==\"minara_alert_command_emitted\"]; \
    [print(c, ts) for c,ts in by_coin.items() if len(ts)>1]'"

# 3. Assert: all intra-coin intervals respect the new cooldown
# If new cooldown is e.g. 12h, any pair within 12h is a violation
```

**Action if violation found:** open a bug PR against the parallel session's cooldown logic with the violating coin + timestamps as evidence. Do NOT silently fix in-place — parallel-session ownership boundary applies.

**Estimate:** ~15 min check + ~30 min triage if violations found. Skip entirely if the parallel cooldown PR turns out to be shift-agent-scoped only.

### BL-NEW-DEX-PRICE-COVERAGE: DexScreener/GeckoTerminal price_cache coverage gap (follow-up to held-position refresh)
**Status:** DEFERRED-WITH-UPDATED-EVIDENCE 2026-05-18 — PR #157 merged `be36bfb`; see `tasks/findings_dex_price_coverage_audit_2026_05_18.md`. **148 open paper_trades, 100% cg-coin-id shape, 0% contract-addr.** Same framing as 2026-05-12 (0/150 then; 0/148 now). Coverage gap empirically dormant. The 21 stale-cache opens are a DIFFERENT bug (held-position refresh rate gap; cg-coin-id tokens — NOT DEX-coverage); that separate follow-up has since shipped via PR #158. Re-eval triggers updated.

**Original status (now historical):** PROPOSED 2026-05-12 — filed as follow-up during Alt A design pass for held-position price refresh.
**Why:** Structural finding surfaced by 2026-05-12 Phase 1 Explore agent on price_cache write path: **`scout/ingestion/dexscreener.py` and `scout/ingestion/geckoterminal.py` do not write to `price_cache` at all.** Their tokens get cache rows only as a side effect of also appearing in a CoinGecko ingestion lane (markets/trending). Pure-DEX-discovered tokens (no CG listing) get no cache row — same shape as the AALIEN case but for a different reason. Currently latent because the open-trades cohort is 0% contract-addr-shaped (all current held tokens have CoinGecko coin_ids), but this is a known landmine.
**Scope:**
- Add a price-source fallback for tokens whose `token_id` is a contract address (starts with `0x`, base58 Solana mint shape, or otherwise non-CG-format)
- Most natural shape: extend `scout/ingestion/held_position_prices.py` (shipped via BL-NEW-HELD-POSITION-REFRESH) with a per-address DexScreener fallback for held positions that fall outside the CG-id filter
- Alternative shape: have DexScreener / GeckoTerminal ingestion lanes write to `price_cache` directly when they discover tokens
**Coverage gap reference:** `tasks/findings_open_position_price_freshness_2026_05_12.md` triage data — 0 of 150 currently-held tokens were contract-addr-shaped, so this fix's deferred status is empirically validated for now. 2026-05-18 re-audit (`tasks/findings_dex_price_coverage_audit_2026_05_18.md`): 0 of 148 — same shape, deferral still valid.
**Acceptance:** With the fallback shipped, every open paper_trade has a `price_cache` row that's < N minutes old regardless of whether the underlying token has a CG listing.
**Estimate:** 2-4 hours including DexScreener client wiring + tests.
**Re-evaluation triggers:** (1) any 30d window shows contract-addr-shaped tokens accumulating in `paper_trades.status='open'`, OR (2) a pure-DEX signal source added to the scorer, OR (3) 2026-08-18 (90d calendar backstop).

### BL-NEW-HELD-POSITION-REFRESH-RATE-GAP: 14% of open paper_trades have stale_gt_24h price_cache rows (separate from DEX-coverage)
**Status:** SHIPPED 2026-05-18 — PR #158 merged `a649032`. Visibility-first fix: stale_open_count gauge + per-token persistent-stale WARN (paper_trade_id + symbol + consequence per R3 I2 fold; 24h dedup; 7d prune) + `_get_cached_price_ages` + `_get_held_trade_metadata` helpers + `simple_price_missing_ids` log diagnostic. Root cause SUSPECTED stale-source behavior (softened per R1 C1 — empirical validation deferred to post-deploy via the new `simple_price_missing_ids` log field). 33/33 tests pass on srilu Python 3.12.3. Task 4 (`/coins/{id}` fallback) descoped pending CG-rate-limit-clear + manual-curl verification. Filed `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` + `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` as evidence-gated/baseline-first follow-ups. See `tasks/findings_held_position_refresh_rate_gap_2026_05_18.md`.

**Originating context:** 2026-05-18 cycle-12 PR #157 audit (`tasks/findings_dex_price_coverage_audit_2026_05_18.md`) surfaced 21/148 open paper_trades with `price_cache > 24h` stale. All 21 are cg-coin-id shape (NOT DEX-coverage class). Trailing-stop/peak-fade evaluators can't fire correctly on stale prices; 14% silent miss-rate is material.

**Post-merge validation action:** after deployment and at least one pipeline cycle, run `tasks/validation_pr158_held_position_refresh_rate_gap.md`; do NOT mark 24h validation complete until journal evidence exists. 2026-05-18 follow-up: operator enabled `HELD_POSITION_PRICE_REFRESH_ENABLED=True` and `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES=1`; first post-flip cycle refreshed 150/150 with `simple_price_missing_ids=[]`, but later cycles under active CG 429/backoff had `refreshed_count=0`, `not_found_count=145-147`, and 25-26 recurring `simple_price_missing_ids`. Current interpretation: deployed-active, not blocked by config, but interval-per-cycle refresh appears to saturate CoinGecko budget. Keep 24h validation OPEN; evaluate misses outside 429 windows before changing fallback status.

### BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT: `/coins/{id}` second-pass for tokens missed by `/simple/price`
**Status:** DESIGN-READY / RATE-LIMIT-BLOCKED 2026-05-18 — PR #163 merged `2f8f187`, evidence-gated follow-up to BL-NEW-HELD-POSITION-REFRESH-RATE-GAP. Manual VPS probe recovered `pythia` and `iagon` via `/coins/{id}` with HTTP 200 + USD price; third probe (`superwalk`) hit HTTP 429, so rate-limit risk remains material. Post-flip PR #158 validation now shows `simple_price_missing_ids` during active CG 429/backoff, but CoinGecko budget is already saturated (`cg_429_backoff` 21 and `rate_limiter_429_reported` 24 in the sampled window). See `tasks/design_held_position_fallback_coins_endpoint.md` and `tasks/findings_pr158_postdeploy_2026_05_18.md`. Do not implement until misses recur outside 429 windows and a bounded `/coins/{id}` probe passes without worsening backoff.
**Tag:** `held-position-refresh` `fallback` `evidence-gated` `cg-rate-limit-sensitive`
**Why:** PR #158 confirms 21 of 148 open paper_trades have stale `price_cache` because CG `/simple/price` returns no data for them. `/coins/{id}` is plausibly more complete (returns per-token detail) but unverified.
**Verification gate:** Initial manual probe passed for 2/3 attempted stale ids (`pythia`, `iagon` returned USD prices; `superwalk` hit HTTP 429). This supports design work but NOT implementation by itself. Implement only after live `simple_price_missing_ids` recur outside active 429/backoff windows and the rate-limit budget can tolerate bounded `/coins/{id}` calls.
**Action (if ship):** ~2h. Cap `MAX_FALLBACK_PER_CYCLE=5` (CG free-tier 30/min budget; avoid burning quota); `coingecko_limiter.is_backing_off()` precheck before each call; reuse `_shape_for_cache_prices`-compatible dict shape per existing pattern.
**Decision-by:** evidence-gated (CG-rate-limit-clearance window required); if no operator verification by 2026-06-30, close as inconclusive.

### BL-NEW-HELD-POSITION-STALE-COUNT-ALERT: threshold-driven TG alert on stale_open_count
**Status update 2026-05-27:** BASELINE-MEASURED / BELOW-SUGGESTED-THRESHOLD / OPERATOR-THRESHOLD-PENDING. The baseline-first follow-up remains open, but current data does not justify alert implementation under the suggested threshold.
**Status:** PROPOSED 2026-05-18 — baseline-first follow-up to BL-NEW-HELD-POSITION-REFRESH-RATE-GAP. Closes the §12a residual (operator-grep-required gauge → automated alert).
**Tag:** `held-position-refresh` `observability` `silent-failure-prevention` `baseline-first`
**Why:** PR #158 ships `stale_open_count` gauge in structured log + per-token WARN. Gauge is operator-grep-dependent. Per CLAUDE.md §12a, threshold-driven alert closes the silent-failure surface.
**Threshold suggestion:** `stale_open_count > max(5, 0.05 * held_total)` for ≥3 consecutive cycles (hysteresis matching cycle-9 patterns). Specific threshold TBD after 7d post-deploy baseline measurement.
**Action:** ~1.5h. Add curl-direct TG alert path inside `fetch_held_position_prices` after the gauge computation; reuse cycle-12 `parse_mode=None` pattern; 24h dedup matching `_warned_today`.
**Baseline 2026-05-27:** 3,878 `held_position_refresh_summary` rows across 2026-05-20T01:42:52Z through 2026-05-27T01:39:12Z. `stale_open_count` min/p50/max = `2/4/5`; `held_total` min/p50/max = `125/139/150`; zero cycles exceeded the suggested threshold (`>` not `>=`). Keep decision-by 2026-06-15 unless operator chooses a lower threshold.
**Decision-by:** 2026-06-15 (4 weeks from PR #158 merge; baseline window must close first).

### BL-NEW-CG-LANE-ORDER-HELD-POSITION-FIRST: reorder _fetch_coingecko_lanes so held_position runs first
**Status:** SHIPPED 2026-05-18 — PR #170 merged `47f0835` at 2026-05-18T18:38:58Z. Deployed to srilu-vps at 18:39:46Z (HEAD `147cba4` → `47f0835`, pycache cleared, restart). Findings: `tasks/findings_cg_budget_attribution_2026_05_18.md`.

**Post-deploy evidence (30min window, 11 cycles):** Cycles 1-3 (18:41:21-18:47:10Z) `refreshed=148/147/147` ✓ (3-consecutive-clean gate met). Cycles 4-8 (18:50:14-18:59:46Z) `refreshed=0/not_found=147` ✗ — wholesale failure recurs during a sustained CG IP-rate-limit cooldown window. Cycles 9-11 (19:01:56-19:07:56Z) `refreshed=147/147/147` ✓ recovery. Rate-limit signals (two distinct counters): 12 `cg_429_backoff` events (one per HTTP 429 received from CG inside `_get_with_backoff`) and 13 `coingecko_lanes_stopped_for_backoff` events (one per cycle-lane boundary where `limiter.is_backing_off()` is true). Lane-stop distribution by `after=` field: held_position=5, top_movers=4, by_volume=2, midcap_gainers=1, trending=1 (sum=13). The two counters are independent because a single 429 carries forward a 120s cooldown that can be detected at multiple lane boundaries within the same cycle.

**Effectiveness:** held_position success rate improved from ~10% pre-fix (cycle 1 fortuitous, all subsequent failed) to ~55% post-fix (6/11 cycles green). **Material improvement; not a complete fix.** Deep 429 cooldown windows still starve even the first lane because CG's IP-rate-limit ceiling is independent of local limiter ordering. See `BL-NEW-CG-FREE-TIER-DEMO-API-KEY` below for the next investigation step.

**#158 24h validation:** STILL OPEN. Extended journal evidence outside sustained 429 windows required before flip.
**Tag:** `coingecko-budget` `lane-ordering` `held-position-refresh` `silent-failure-prevention` `small-fix`
**Hermes-first:** fresh check 2026-05-18 (3 surfaces: installed VPS skills under `/home/gecko-agent/.hermes/skills/`, Hermes optional-skills catalog, awesome-hermes-agent) — 0 hits on async lane orchestration / rate-limiter priority / CG lane scheduling.
**Drift verdict:** existing primitive `_fetch_coingecko_lanes` modified in place; no new primitives introduced.

### BL-NEW-CG-FREE-TIER-DEMO-API-KEY: register and configure CoinGecko Demo API key to lift IP-rate-limit ceiling
**Status:** RUNBOOK-READY 2026-05-18 — runbook at `tasks/runbook_cg_demo_api_key_2026_05_18.md` covers pre-flight baseline / register / `.env` edit / restart / rollback / 2h validation. Direct follow-up to BL-NEW-CG-LANE-ORDER-HELD-POSITION-FIRST partial-effectiveness.
**Tag:** `coingecko-budget` `rate-limit` `evidence-gated` `small-fix` `config-only` `operator-gated`
**Why:** Post-#170 deploy evidence shows lane reorder lifts held_position success rate from ~10% to ~55%, but 5/11 cycles in the 30min sample still hit `coingecko_lanes_stopped_for_backoff after="held_position_prices"`. The binding constraint is CG's IP-rate-limit ceiling, not local lane ordering. PR #129's deploy notes already flagged Demo API key as the next escalation after conservative tuning if throttles persist.
**Action:** ~30min, operator-only. See `tasks/runbook_cg_demo_api_key_2026_05_18.md` for exact steps. Code already threads the key via `params["x_cg_demo_api_key"]` in 5 ingestion sites (coingecko.py + held_position_prices.py) plus the HTTP-header form `x-cg-demo-api-key` in secondwave/detector.py and indirect threading in narrative/agent.py, briefing/collector.py, minara_alert.py, trending/tracker.py — 16 call sites across 8 modules. No in-tree change needed.
**Validation gate:** post-deploy 2h window must show `cg_429_backoff` count drops materially (target: ≥50% reduction) AND `held_position_refresh_summary.refreshed_count > 0` for ≥10 consecutive cycles without intermittent failure-windows.
**Hermes-first:** N/A — operator credential registration. The optional Hermes blockchain skills reference CoinGecko for pricing but don't supply a key.
**Drift-check:** API-key param path already exists in code (`scout/ingestion/coingecko.py:99,191,228,319,441` + `scout/ingestion/held_position_prices.py:182-183`). Config-only enablement.
**Decision-by:** 2 weeks (mirrors BL-NEW-CG-RATE-LIMITER-BURST-PROFILE / BL-NEW-CG-LANE-ORDER-HELD-POSITION-FIRST cadence).

### BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE: wire push-notification for narrative_alert_dispatcher 503 misconfig (Path C1)
**Close-dev 2026-05-22 status anchor:** OPERATOR-GATED — no operator authorization received in the 2026-05-22 close-development block. Cost-of-activation already reduced to "copy-paste 4 SKILL sections + set 2 env secrets" per `tasks/runbook_operator_alert_skill_patch_2026_05_21.md`. **Three concrete operator inputs blocking activation:** (1) `OPERATOR_ALERT_HMAC_SECRET` (generate via `python3 -c "import secrets; print(secrets.token_hex(32))"`), (2) set same value on srilu `/root/gecko-alpha/.env` AND `/home/gecko-agent/.hermes/.env`, (3) apply SKILL.md patch + restart relevant services + run smoke test (501→401 without HMAC, signed POST works, temporary narrative-secret failure triggers operator-alert delivery + Telegram landing, narrative secret restored). When authorized: flip to full SHIPPED after smoke confirms operator_alert_dispatched + operator_alert_delivered log triplet fires.

**Status:** ENDPOINT-SHIPPED / HERMES-SKILL-PENDING 2026-05-18 — PR #176 merged `012e67c` at 2026-05-18T23:52:24Z. Gate fired at activation time (204 rows in `narrative_alerts_inbound` vs ≥10 threshold). `scout/api/internal_alert.py` adds POST `/api/internal/operator-alert`, HMAC-authed via the parameterized `_verify_hmac` from `scout/api/narrative.py` against the new independent `OPERATOR_ALERT_HMAC_SECRET` (Reviewer 1 P1 fold — breaks the circular gating that would have made the documented smoke-test impossible). Calls `scout.alerter.send_telegram_message(parse_mode=None, raise_on_failure=True)` with §12b dispatched / delivered / failed log triplet. Router wired into `dashboard/api.py` with the same stub-503 pattern as the narrative router. 17 tests in `tests/test_internal_alert_api.py` cover: feature-gate 503, missing-headers 401, bad-sig 403, replay 409, invalid-payload 400, delivery success 200, delivery failure 502, log-triplet ordering, four secret-leakage scans, and four gate-independence cases (Reviewer 1 P1 regression coverage). Original PROPOSED 2026-05-13.

**Why ENDPOINT-SHIPPED instead of full SHIPPED (Reviewer 1 P2 phased discipline):** the gecko-alpha endpoint is live but the Hermes-side dispatcher still uses Path B log-only until both (a) `OPERATOR_ALERT_HMAC_SECRET` is set on srilu `.env`, (b) `/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md` is updated to POST to `/api/internal/operator-alert` with that secret, AND (c) a smoke test confirms the dispatcher's HMAC POST reaches the endpoint and `operator_alert_dispatched` fires on gecko-alpha. Smoke-test shape: unset `NARRATIVE_SCANNER_HMAC_SECRET` on gecko-alpha + restart + the dispatcher should detect the narrative-side 503 and POST to `/api/internal/operator-alert` — which works because it authenticates with the independent `OPERATOR_ALERT_HMAC_SECRET`. A Telegram alert should land. Flip to full `SHIPPED` only after that.

**Activation runbook:** `tasks/runbook_operator_alert_activation_2026_05_19.md` covers all out-of-repo steps — secret generation, srilu `.env` edit (secret-hygiene via `ssh -t` + `read -rsp`), service restart, endpoint-401-vs-503 verification, Hermes SKILL.md change shape (canonical-string scheme + headers), smoke-test sequence with mandatory restore of `NARRATIVE_SCANNER_HMAC_SECRET`, rollback path. Operator-gated; do not execute without authorization.

**Concrete SKILL.md patch (added 2026-05-21):** `tasks/runbook_operator_alert_skill_patch_2026_05_21.md` provides drop-in code blocks for Steps 5a-5d — operator copies inserted sections directly into `/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md` instead of designing the dispatcher changes from the shape-level description in the original runbook. Includes Step 5e (set the secret on the Hermes-side env) and a fixed-vector signature pinning verification snippet.

**Deploy state 2026-05-21 (verified):** PR #176 endpoint code is on srilu prod via master `df76d85`. `OPERATOR_ALERT_HMAC_SECRET` is NOT in `/root/gecko-alpha/.env` (grep -c → 0 on both set and empty patterns). Endpoint live response: 503. SKILL.md unchanged since 2026-05-13 15:07Z (still Path B log-only; status=DRAFT in frontmatter). No `operator_alert_dispatched` or `narrative_dispatcher_misconfig` log activity in last 24h. **Fully operator-blocked** — no progress since 2026-05-19 runbook landed. Cost-of-activation now reduced from "shape-level design + apply" to "copy-paste 4 sections + set 2 env secrets".

**Deploy state 2026-05-19:** PR #176 (endpoint code) is on srilu prod via master `ec4f35c` (deployed 2026-05-19T00:20:36Z). The endpoint currently returns 503 because `OPERATOR_ALERT_HMAC_SECRET` is empty — feature-gated off by default. The runbook activates it.

**Independent secret (Reviewer 1 P1 fold):** the internal-alert endpoint authenticates against a NEW Settings field `OPERATOR_ALERT_HMAC_SECRET`, NOT `NARRATIVE_SCANNER_HMAC_SECRET`. The earlier shape would have failed in the very scenario this endpoint exists to surface: if `NARRATIVE_SCANNER_HMAC_SECRET` is missing/broken, the narrative endpoint 503s — and so would have the operator-alert endpoint, leaving the dispatcher unable to raise an alert about the broken narrative ingestion. Independent secrets break that circular dependency. Same shape rules as `NARRATIVE_SCANNER_HMAC_SECRET` (empty or ≥32 chars). The shared `_verify_hmac` in `scout/api/narrative.py` is now parameterized via `secret_field` + `feature_label` kwargs; default arguments preserve existing narrative behavior bit-for-bit.

**Phased post-merge status (Reviewer 1 P2 fold):** the PR ships the gecko-alpha endpoint only. The Hermes-side dispatcher continues to use Path B (log-only `narrative_dispatcher_misconfig`) until `/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md` on srilu is updated out-of-repo to POST to `/api/internal/operator-alert`. Status progression:
- On PR merge → flip to `ENDPOINT-SHIPPED / HERMES-SKILL-PENDING` (NOT full SHIPPED). Endpoint is live but the dispatcher is not yet calling it.
- Operator must set `OPERATOR_ALERT_HMAC_SECRET` on srilu `.env` (generate via `python3 -c "import secrets; print(secrets.token_hex(32))"`) AND configure the Hermes dispatcher's SKILL.md with the same value.
- After operator updates the SKILL.md AND a smoke test confirms the dispatcher's HMAC POST reaches `/api/internal/operator-alert` and the `operator_alert_dispatched` log event fires on gecko-alpha → flip to `SHIPPED`.
- Smoke-test shape: trigger a deliberate `NARRATIVE_SCANNER_HMAC_SECRET=""` on the gecko-alpha side, restart, then restore. The dispatcher should detect the narrative-side 503 and POST to `/api/internal/operator-alert` — which now works because it authenticates with the independent `OPERATOR_ALERT_HMAC_SECRET`. A Telegram alert should land. Alternative: dispatcher emits a one-off `dispatcher_self_test` POST with a known marker source.
**Tag:** `narrative-scanner` `path-c1` `operator-alert` `post-activation` `evidence-gated`

**Why deferred:** V1.1 dispatcher emits a structured `narrative_dispatcher_misconfig` journalctl log on 503 (Path B). Operator-side discovery via `journalctl -g 'narrative_dispatcher_misconfig'`. This is sufficient for V1 because the 503 path only fires on a one-shot misconfig (operator forgot to set `NARRATIVE_SCANNER_HMAC_SECRET`) — discovery latency = "operator next runs journalctl" ≈ minutes to hours, acceptable for a should-not-recur condition.

**Hermes-first basis for the deferred decision (2026-05-13):** Focused check across installed VPS skills under `/home/gecko-agent/.hermes/skills/` + public Hermes docs hub found no Telegram / Slack / Discord / outbound-webhook / operator-alert primitives. `webhook-subscriptions` confirmed INBOUND-only. gecko-agent's `~/.hermes/.env` lacks TG credentials. Path A (use existing primitive) is closed. Path C1 below is the next-best option but requires real new work, not 5-minute wire-up.

**Scope (Path C1 wire-up):**
- New `scout/api/internal_alert.py` with `POST /api/internal/operator-alert` endpoint on gecko-alpha
- Reuse existing `NARRATIVE_SCANNER_HMAC_SECRET` for auth (no new credential setup)
- HMAC scheme identical to `narrative.py` (same canonical-string format, same replay LRU)
- Endpoint calls `scout.alerter.send_telegram_message(parse_mode=None, ...)` — per §2.9 parse-mode hygiene
- Update dispatcher SKILL.md on srilu: replace `narrative_dispatcher_misconfig` log-only with the triplet pattern (`alert_dispatched` + `alert_delivered` + `alert_failed`) per CLAUDE.md §12b
- Tests: HMAC fixed-vector + parse_mode integration test + delivery-failure path

**Trigger condition (evidence-gated, NOT calendar-gated):** Path C1 wire-up fires only when narrative_scanner has produced **≥10 narrative_alerts_inbound rows** since activation. The 10-row threshold is the floor at which "the system is actually running" stops being conjectural and becomes empirical. If activation produces zero rows for 30 days due to unrelated bugs, Path C1 does NOT fire — that's correct because operator-alert work isn't load-bearing if the system isn't generating events to alert about.

**Verification query (run periodically post-activation):**
```sql
SELECT COUNT(*) FROM narrative_alerts_inbound;
-- Trigger Path C1 when this returns ≥ 10
```

**Kill criterion:** If narrative_scanner is deprecated or replaced before reaching 10 rows in `narrative_alerts_inbound`, this entry closes as obsolete without action. Prevents indefinite-open backlog drift if V1 doesn't pan out.

**References:**
- Deployed Path B SKILL.md: `/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md` on srilu-vps
- Design doc 503 behavior: `tasks/design_crypto_narrative_scanner.md:89` (V1.1 update + BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE reference inline)
- CLAUDE.md §12b: automated state-reversal alerts must emit `*_dispatched` + `*_delivered` log triplet
- §2.9 parse-mode hygiene: signal-name strings to `send_telegram_message` require `parse_mode=None`

**Drift verdict:** NET-NEW. No existing primitive covers cross-host operator-alert delivery with HMAC auth.
**Hermes verdict:** ✅ Hermes-first check done 2026-05-13 — none of 687 skill-hub entries cover Telegram/Slack/Discord/email/webhook-out from a Hermes skill. Wiring into gecko-alpha's existing `scout.alerter` is the cheapest correct path.

**Estimate:** ~30-60 min code (new endpoint mirroring narrative.py pattern) + ~30 min tests + review cycle.

---

## BL-NEW carry-forwards filed 2026-05-13 from BL-NEW-CYCLE-CHANGE-AUDIT (PR #114)

All 13 entries below were surfaced by the cycle-change audit findings doc and stubbed here per the actionability discipline (PR-review fold). Each carries a `decision-by` field; the audit's next-audit trigger (2026-11-13) measures shipped vs drifted ratio.

### BL-NEW-CG-RATE-LIMITER-BURST-PROFILE
**Status:** SHIPPED 2026-05-15 — commit chain `7f1a174 fix(coingecko): smooth shared rate limiter bursts (#129)` + `a08d9ef fix(coingecko): stop 429 retry amplification` + `f45e598 fix(coingecko): stop same-cycle 429 fanout` + `d1cf96b fix(coingecko): serialize main CoinGecko lane`. Design at `tasks/design_bl_new_cg_rate_limiter_burst_profile.md`. **Residual:** deploy-verification only — compare post-restart `cg_429_backoff` / `rate_limiter_global_backoff` / `resolver_transient` / cycle intervals against 30-min pre-fix baseline. Status updated per `tasks/findings_backlog_drift_audit_2026_05_16.md`.

**Original status (now historical):** THIRD FOLLOW-UP PR-READY 2026-05-15 on `codex/cg-throttle-serialize` - PR #129 spacing/jitter shipped, PR #130 removed in-call retry amplification, and PR #131 stopped same-function page/strategy fan-out. Post-PR #131 logs still showed cross-lane fan-out because `main.py` launched separate CoinGecko lanes concurrently. Root causes now covered: (1) `_get_with_backoff()` no longer retries each 429 up to four times; (2) Telegram social resolver and second-wave paths report 429s into the shared limiter; (3) `RateLimiter.is_backing_off()` lets same-function CoinGecko lanes stop remaining strategy/page requests; (4) `main.py::_fetch_coingecko_lanes()` now runs top movers, CoinGecko trending, volume, midcap, and held-position price refresh sequentially, stopping lower-priority CoinGecko lanes when backoff is active while preserving DexScreener/GeckoTerminal parallelism. Design updated: `tasks/design_bl_new_cg_rate_limiter_burst_profile.md`. Verification: main/CoinGecko targeted suite -> 68 passed; adjacent CoinGecko/social/second-wave suite -> 167 passed.
**Action:** merge/deploy orchestrator serialization follow-up, then compare post-restart `cg_429_backoff`, `rate_limiter_global_backoff`, `resolver_transient`, and cycle intervals against the 30-minute pre-fix window. If throttles persist with cross-lane fan-out stopped, next investigation is provider identity/keying: configure a CoinGecko Demo API key/header or reduce optional CoinGecko lanes before reducing scanner breadth.
**decision-by:** 2 weeks (per design v2 §4 Borderline urgency mapping).

### BL-NEW-GT-429-HANDLER
**Status:** SHIPPED 2026-05-13 — PR #115 (`30b588a`) adds bounded GeckoTerminal HTTP 429/5xx retry with stable structured logs and is deployed on srilu. Transport errors remain single-attempt fail-soft to avoid broadening cycle-latency blast radius. Tests cover 429 recovery, 503 recovery, 429 exhaustion, 5xx exhaustion, multi-chain continuation after exhaustion, 404 no-retry, and transport-error no-retry.
**Action:** add 429/5xx handler matching DexScreener's HTTP-status retry pattern.
**decision-by:** 2 weeks.

### BL-NEW-GT-ETH-ENDPOINT-404
**Status:** SHIPPED 2026-05-15 — commit `e0e51c8 fix(geckoterminal): map ethereum chain to eth network id`. Status updated per `tasks/findings_backlog_drift_audit_2026_05_16.md`. Design at `tasks/design_bl_new_gt_eth_endpoint_404.md`. **Residual:** post-deploy verify no fresh `networks/ethereum/trending_pools` 404s + `geckoterminal:ethereum` samples still exist.

**Original status (now historical):** PR-READY 2026-05-14 on `codex/gt-eth-endpoint-404` — root cause confirmed: gecko-alpha's canonical chain label is `ethereum`, but GeckoTerminal's provider network id is `eth`. Live cheap fetch: `/networks/ethereum/trending_pools` -> 404; `/networks/eth/trending_pools` -> 200; official GT `/networks` metadata lists `id="eth"` with `coingecko_asset_platform_id="ethereum"`.
**Action:** merge and deploy the alias fix; post-deploy verify no fresh `networks/ethereum/trending_pools` 404s and that `geckoterminal:ethereum` samples still exist.
**decision-by:** 4 weeks.

### BL-NEW-HELIUS-PLAN-AUDIT
**Status:** AUDITED-PHANTOM 2026-05-18 — findings at `tasks/findings_helius_plan_audit_2026_05_18.md`. Same shape as Moralis (PR #173): three independent surfaces confirm the path is dead under current configuration: (1) `HELIUS_API_KEY=` empty on srilu .env; (2) 0 Helius log hits in 24h; (3) `holder_snapshots` table = 0 rows total. Early-return guard at `scout/ingestion/holder_enricher.py:33-34` prevents any Helius HTTP call when the key is empty. No code change. Conditional guardrail filed below as `BL-NEW-HELIUS-ENABLEMENT-GUARDRAIL`.

**Notable nuance vs Moralis (Reviewer 1 P1 correction — stale daily cap):** Helius Free plan = **1M monthly credits** per current docs (1 credit per Standard RPC call; 10 req/s rate limit which is not binding at any plausible gecko-alpha cohort × cycle rate). The audit's ~100k/day reference was stale. Recalibrated at today's measured 12 cycles/hr: ~35k/day × 30 ≈ **~1.05M/month — at or marginally above the 1M Free cap before any ambient Helius usage outside gecko-alpha**. At ~30 cycles/hr (e.g., post-Demo-API-key partial relief): ~2.61M/month (~2.6× over). At audit's 60 cycles/hr: ~5.22M/month (~5.2× over). **Helius risk is rate-dependent (monthly)** in a way Moralis's was not; binding constraint is monthly credits, not daily calls.

**Hermes-first 2026-05-18 (fresh, 4 surfaces):** (1) installed VPS skills (28 dirs, 0 helius/getTokenAccounts/Solana-holder hits); (2) Hermes optional-skills catalog `blockchain/solana` covers wallet/balance/top-5-holders + transactions but NOT `getTokenAccounts` full-count (and not installed); (3) awesome-hermes-agent has no Helius/Solana RPC entry; (4) GoldRush/Covalent agent-skills cover balances/transactions/NFTs/prices but no SPL holder enumeration. No installed or external primitive replaces the in-tree path.

### BL-NEW-HELIUS-ENABLEMENT-GUARDRAIL: gate operator enablement of Helius behind plan-tier + cycle-rate check
**Status:** PROPOSED 2026-05-18 — conditional follow-up to AUDITED-PHANTOM closure of BL-NEW-HELIUS-PLAN-AUDIT. Only fires if/when operator decides to set `HELIUS_API_KEY` on prod.
**Tag:** `holder-enrichment` `evidence-gated` `operator-gated` `pre-enablement-guardrail` `conditional` `rate-dependent`
**Why:** Audit (2026-05-18) confirmed the path is dead under current config; if-enabled at today's 12 cycles/hr projects ~1.05M/month against the Helius Free 1M monthly credit allocation (at-or-marginally-above the cap before ambient usage). If cycle rate climbs to ≥30 cycles/hr (e.g., post-Demo-API-key partial relief on CG ceiling), projection scales to ~2.6M/month (~2.6× over). Enablement decision must include a monthly-credit projection step before setting the key.
**Trigger condition (NOT calendar-gated):** operator intent to enable Helius. If no intent in 6mo, close as inert.
**Action checklist (operator-only when triggered):**
1. Confirm plan tier via Helius dashboard. Free plan = 1M monthly credits; paid tiers higher.
2. **Project monthly credit consumption** at enablement time: count `secondwave_cycle_complete` events over a recent 1h window, multiply by 24 × 30 × ~121 solana/cycle (or re-measured cohort). Compare against the Free 1M cap with explicit headroom for ambient Helius usage. **If projection > 1M/month without paid uplift:** add per-token `holder_count` cache (24h TTL suggested) AND/OR throttle the `enrich_holders` fan-out before enabling. Today's ~12 cycles/hr already projects ~1.05M/month — borderline-not-safe.
3. Capture pre-enablement baseline + post-enablement 2h validation window (mirrors `runbook_cg_demo_api_key_2026_05_18.md` structure): verify `holder_snapshots` row-rate, check for `Helius holder lookup failed` entries, confirm credit usage at Helius dashboard.
4. Re-check Hermes-first / GoldRush at enablement time — a Solana-specific full-holder-enumeration skill may have shipped by then.
**Hermes-first 2026-05-18:** carry-forward of the audit's negative result across 4 surfaces; re-check at enablement time.
**Drift verdict:** NET-NEW guardrail; conditional follow-up to AUDITED-PHANTOM closure. Mirrors `BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL` shape but with rate-dependent cap evaluation step.
**Decision-by:** evidence-gated (6mo backstop) — close as inert if no operator intent to enable Helius.

### BL-NEW-MORALIS-PLAN-AUDIT
**Status:** AUDITED-PHANTOM 2026-05-18 — findings at `tasks/findings_moralis_plan_audit_2026_05_18.md`. Three independent surfaces confirm the path is dead under current configuration: (1) `MORALIS_API_KEY=` empty on srilu .env; (2) 0 Moralis log hits across 24h + 7d windows; (3) `holder_snapshots` table = 0 rows total. Early-return guard at `scout/ingestion/holder_enricher.py:37-38` prevents any Moralis HTTP call when the key is empty. The cycle-change audit's 25× over-cap projection was contingent on the key being set; with the key empty the risk is phantom. No code change. Conditional guardrail filed below as `BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL`.

**Hermes-first 2026-05-18 (fresh, 4 surfaces):** (1) installed VPS skills (28 dirs, 0 hits on holder/Moralis/EVM patterns); (2) Hermes optional-skills catalog (closest is `blockchain/evm` — wallet/tokens/gas, not holders; not installed); (3) awesome-hermes-agent (no holder skill); (4) GoldRush/Covalent agent-skills (4 skills: foundational REST, streaming, CLI, x402 — none cover ERC20 holder enumeration). Verdict: no installed or external primitive replaces the current in-tree Moralis use case.

**Cohort calibration (24h, srilu DB):** EVM-mappable candidates = 30 (12 ethereum + 18 base + 0 polygon). If enabled at current cohort × observed 12 cycles/hr: ~200-260k calls/month — exceeds Moralis legacy-free 40k/mo by 5-7×, consistent with the original audit's direction even with updated rate.

### BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL: gate operator enablement of Moralis behind plan-tier + throttle decision
**Status:** PROPOSED 2026-05-18 — conditional follow-up to AUDITED-PHANTOM closure of BL-NEW-MORALIS-PLAN-AUDIT. Only fires if/when operator decides to set `MORALIS_API_KEY` on prod.
**Tag:** `holder-enrichment` `evidence-gated` `operator-gated` `pre-enablement-guardrail` `conditional`
**Why:** Audit (2026-05-18) confirmed the path is dead under current config but would project to 5-7× over Moralis legacy-free cap if enabled at current cohort. Without a plan-tier check + throttle/cache decision BEFORE the key is set, enablement would silently exceed budget on legacy-free or rack up CU-based bills.
**Trigger condition (NOT calendar-gated):** operator intent to enable Moralis. If no intent in 6mo, close as inert.
**Action checklist (operator-only when triggered):**
1. Confirm plan tier via Moralis dashboard (legacy-free vs CU-based vs paid).
2. If legacy-free: add per-token `holder_count` cache (24h TTL suggested) + hard daily call cap before enabling.
3. If CU-based paid: set budget alert; project worst-case monthly spend at observed EVM cohort × cycle rate.
4. Re-check Hermes-first / GoldRush. By enablement time, holder coverage may have shifted; prefer consolidated provider if available.
5. Capture pre-enablement baseline + post-enablement 2h validation window (mirrors BL-NEW-CG-FREE-TIER-DEMO-API-KEY runbook shape).
**Hermes-first 2026-05-18:** carry-forward of the audit's negative result across 4 surfaces; re-check at enablement time.
**Drift verdict:** NET-NEW guardrail; conditional follow-up to AUDITED-PHANTOM closure.
**Decision-by:** evidence-gated (6mo backstop) — close as inert if no operator intent to enable Moralis.

### BL-NEW-ANTHROPIC-SPEND-TARGET
**Status:** PROPOSED 2026-05-13 — surfaced by cycle-change audit B11 (Unfalsifiable-by-policy; current baseline ~$0.06/day).
**Action:** operator decision — accept proposed skeleton `$5/day soft cap, $20/day alert` OR modify; record decision by adding `ANTHROPIC_DAILY_SPEND_SOFT_CAP_USD` setting to `.env` + `config.py`.
**decision-by:** 2 weeks.

### BL-NEW-SCORE-HISTORY-WATCHDOG-SLO
**Status:** PROPOSED 2026-05-13 — surfaced by cycle-change audit C2-score (no SLO documented; 17,325 rows/hr).
**Action:** add `score_history` to §12a watchdog daemon's monitored-tables list with **relative-to-baseline SLO**: alert if row-rate drops below 10% of trailing-1h p50.
**Dependency:** §12a daemon implementation (unbuilt; see `findings_silent_failure_audit_2026_05_11.md` closing notes).
**decision-by:** 2 weeks filing; implementation gated on daemon.

### BL-NEW-SCORE-HISTORY-PRUNING
**Status:** SHIPPED 2026-05-16 — PR #136 merged `00abaa7`. All 4 residual DRIFT-PARTIAL gaps closed:
- Parameterized via `Settings.SCORE_HISTORY_RETENTION_DAYS` (default 21d) — `scout/config.py:262`.
- Decoupled from the narrative daily-learn loop; pruning now lives in `scout/main.py:1280-1290` inside `_run_hourly_maintenance` (independent of `scout/narrative/agent.py`).
- Silent `except: pass` replaced with `logger.exception("score_history_prune_failed")` at `scout/main.py:1289-1290`.
- Row-count telemetry: `logger.info("score_history_pruned", rows_deleted=..., keep_days=...)` at `scout/main.py:1283-1288` (cryptopanic pattern — info-when-rows>0, silent-when-zero).
- Test coverage: `tests/test_hourly_maintenance.py` + `tests/test_db.py` + `tests/test_config.py` + `tests/test_narrative_agent_prune.py`.

**Original status (now historical):** DRIFT-PARTIAL 2026-05-16 (per `tasks/findings_backlog_drift_audit_2026_05_16.md`) — 14-day time-based pruning EXISTED at `scout/narrative/agent.py:687-689` at filing time; 4 residual gaps documented. PROPOSED 2026-05-13 — surfaced by cycle-change audit C2-score.

### BL-NEW-VOLUME-SNAPSHOTS-WATCHDOG-SLO
**Status:** PROPOSED 2026-05-13 — surfaced by cycle-change audit C2-volume (same shape as C2-score).
**Action:** add `volume_snapshots` to §12a watchdog daemon's monitored-tables list (relative-to-baseline SLO).
**decision-by:** 2 weeks filing; implementation gated on daemon.

### BL-NEW-VOLUME-SNAPSHOTS-PRUNING
**Status:** SHIPPED 2026-05-16 — PR #136 merged `00abaa7` (combined with BL-NEW-SCORE-HISTORY-PRUNING). Same residual-gap closures:
- Parameterized via `Settings.VOLUME_SNAPSHOTS_RETENTION_DAYS` (default 21d) — `scout/config.py:263`.
- Decoupled from narrative loop into `scout/main.py:1292-1303` inside `_run_hourly_maintenance`.
- Silent `except: pass` replaced with `logger.exception("volume_snapshots_prune_failed")` at `scout/main.py:1302-1303`.
- Row-count telemetry: `logger.info("volume_snapshots_pruned", rows_deleted=..., keep_days=...)` at `scout/main.py:1296-1301`.
- Test coverage: `tests/test_hourly_maintenance.py` + `tests/test_db.py` + `tests/test_config.py`.

**Original status (now historical):** DRIFT-PARTIAL 2026-05-16 — same shape as BL-NEW-SCORE-HISTORY-PRUNING. PROPOSED 2026-05-13 — surfaced by cycle-change audit C2-volume.

### BL-NEW-CALIBRATION-ERA-DOC
**Status:** SHIPPED-WITH-AUDIT 2026-05-13 — surfaced by cycle-change audit Tier F; 1-line code comments documenting cycle-era assumption shipped as part of PR #114. Comment text: `# calibration era: undocumented — see BL-NEW-CALIBRATION-ERA-DOC`. 7 settings tagged: VELOCITY_DEDUP_HOURS, LUNARCRUSH_DEDUP_HOURS, SLOW_BURN_DEDUP_DAYS, SECONDWAVE_DEDUP_DAYS, FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN, PAPER_STARTUP_WARMUP_SECONDS, CACHE_TTL_SECONDS.

### BL-NEW-BL060-CYCLE-VERIFY
**Status:** AUDITED-CYCLE-INDEPENDENT 2026-05-19 — overnight drift-cleanup audit confirms BL-060 pacing is event-driven (per-trade-open) not time-driven. Evidence: `scout/trading/paper.py:110` calls `compute_would_be_live()` once per trade-open event; `scout/trading/live_eligibility.py:104` capacity check reads `SELECT COUNT(*) ... WHERE would_be_live = 1 AND status = 'open'` against `PAPER_LIVE_ELIGIBLE_SLOTS` (default 20). The FCFS-20 cap operates on currently-open count, not on rate — whether the pipeline runs at 60s / 90s / 15-min cadence, the cap behaves identically. The design doc's `bl060-paper-mirrors-live-design.md:197` "15-min cycle" reference is descriptive of an old assumed deploy cadence, NOT a load-bearing dependency in the implementation. Comment at `scout/trading/live_eligibility.py:31-32` confirms shape: "quality subset, not a FCFS-20 cap on the firehose." No code change needed.

**Original status (now historical):** PROPOSED 2026-05-13 — surfaced by cycle-change audit Tier E.
**Original action:** verify BL-060 implementation (paper-mirrors-live) paces independently of 60s cycle, not the 15-min cycle assumed in design doc `bl060-paper-mirrors-live-design.md:197`.
**Original decision-by:** 4 weeks.

### BL-NEW-SQLITE-WAL-PROFILE
**Status:** SHIPPED 2026-05-17 — branch `feat/sqlite-wal-profile` (5 commits + plan/design folds). `Database.probe_wal_state()` in `scout/db.py` (5 PRAGMA reads + 2 `os.path.getsize` syscalls, using `pragma_wal_autocheckpoint` table-valued form for pure-read semantics with Windows-stdlib fallback); 13th SQL hop in `_run_hourly_maintenance` AFTER all 12 prunes so `wal_size_bytes` captures DELETE-driven peak. Log levels: `sqlite_wal_probe` DEBUG / `sqlite_wal_bloat_observed` WARNING / `sqlite_wal_probe_failed` exception. Settings: `SQLITE_WAL_PROFILE_ENABLED: bool = True`, `SQLITE_WAL_BLOAT_BYTES: int = 50_000_000`. Operator scripts: `scripts/wal_summary.sh` (consecutive-bloat-run aggregator + runaway-WAL/freelist single-event checks + Week-1 baseline calibration with V23 M2 restart-bracket drop) + `scripts/wal_archive.sh` (weekly cron, filename-date rotation, 8w retention, same-day .N suffix, 2-week overlap). 4 probe tests + 3 hourly-hook integration tests + 3 config tests pass locally; full regression validated on srilu (Windows OPENSSL workaround per memory `reference_windows_openssl_workaround.md`). Pre-registered decision criteria documented in `tasks/plan_sqlite_wal_profile.md` § Decision criteria; filed follow-up `BL-NEW-SQLITE-WAL-TUNING-DECISION`. Memory checkpoint: `project_sqlite_wal_tuning_checkpoint_2026_06_14.md`.

**Original status (now historical):** PROPOSED 2026-05-13 — surfaced by cycle-change audit non-external-constraint sub-scan. Action: measure SQLite WAL bloat at 17k+ writes/hr (combined score_history + volume_snapshots + upsert_candidate); add WAL checkpoint cadence tuning if bloat is observed.

### BL-NEW-SQLITE-WAL-TUNING-DECISION: act on SQLite-WAL-profile data after 4-week soak
**Status:** PROPOSED 2026-05-17 — filed concurrent with BL-NEW-SQLITE-WAL-PROFILE shipping. Evidence-gated on 4-week measurement window.
**Trigger:** 2026-06-14 (4 weeks post-deploy). Week-1 calibration at 2026-05-24.
**Pre-registered criteria** (per `tasks/plan_sqlite_wal_profile.md` § Decision criteria):
- TUNE if ≥12 STRICTLY consecutive hourly probes `wal_size_bytes > SQLITE_WAL_BLOAT_BYTES` (`wal_summary.sh` aggregator prints "TUNE criterion MET")
- TUNE-IMMEDIATELY if any single probe `wal_size_bytes > 500MB`
- VACUUM-schedule (file `BL-NEW-SQLITE-VACUUM-SCHEDULE`) if any single probe `freelist_count > 0.10 × page_count`
- ACCEPT if zero of the above for 4 weeks (close as no-action)
**Week-1 calibration procedure:** `./scripts/wal_summary.sh 168` on srilu reads suggested `SQLITE_WAL_BLOAT_BYTES` (~1.5×p95 rounded to 5MB, floor 50MB); operator sets `.env` override + restarts. Restart-bracket samples are dropped automatically (gap >90min heuristic).
**Decision artifact:** findings doc + backlog flip to SHIPPED/ACCEPT
**decision-by:** 2026-06-14

### BL-NEW-TG-BURST-PROFILE
**Status:** SHIPPED 2026-05-17 — branch `feat/tg-burst-profile` (5 commits + plan/design folds). `TGDispatchCounter` in `scout/observability/tg_dispatch_counter.py`; `record_dispatch()` + `record_429()` hooks wired into `scout.alerter.send_telegram_message`; `source:` kwarg added for callsite attribution (V14 fold); group-vs-DM 20/min threshold guard (V13 fold — current prod chat is DM); `scripts/tg_burst_summary.sh` + `scripts/tg_burst_archive.sh` operator helpers (weekly cron, 8-week retention, filename-date rotation). 12 counter+integration tests pass locally; alerter integration tests verified on srilu (Windows OPENSSL workaround). Pre-registered decision criteria documented in `tasks/plan_tg_burst_profile.md` § Decision criteria; filed follow-up `BL-NEW-TG-PACING-DECISION`. Memory checkpoint: `project_tg_burst_pacing_checkpoint_2026_06_14.md`.

**Original status (now historical):** PROPOSED 2026-05-13 — surfaced by cycle-change audit Telegram-burst reclassification (Phantom-fragile; 13+ dispatch sites point at one chat; coincident-burst probability unmeasured). Action: instrument per-cycle Telegram dispatch volume; measure burst frequency vs 1/sec same-chat and 20/min same-group limits.

### BL-NEW-TG-PACING-DECISION: act on TG-burst-profile data after 4-week soak
**Status:** PROPOSED 2026-05-17 — filed concurrent with BL-NEW-TG-BURST-PROFILE shipping. Evidence-gated on the 4-week measurement window.
**Trigger:** 2026-06-14 (4 weeks post-deploy).
**Pre-registered criteria** (per `tasks/plan_tg_burst_profile.md` § Decision criteria):
- PACE if any `tg_dispatch_rejected_429` event observed in the 4-week window
- PACE if `tg_burst_observed` (group-chat callsite) >50/week sustained
- ACCEPT if zero burst OR 429 events in 4 weeks
- DM-only bursts with zero 429 → ACCEPT (1-on-1 DMs tolerate ~30/sec)
- weekly_digest/daily_summary multi-chunk `breached_1s` events are EXPECTED noise; exclude via `grep -v 'source.*weekly-digest'`
**Decision artifact:** findings doc + backlog flip to SHIPPED/ACCEPT
**decision-by:** 2026-06-14

---

## P2 — BL-064 follow-ups (TG social signals deployed 2026-04-27)

### BL-065: Dispatch paper trades from cashtag-only resolutions
**Status:** SHIPPED 2026-05-04 — PR #65 squash-merged as `835ce7f`, deployed VPS 2026-05-04T05:08:30Z. Default fail-closed (`cashtag_trade_eligible=0` on all 8 channels). Operator must `UPDATE tg_social_channels SET cashtag_trade_eligible=1 WHERE channel_handle='@<curator>'` to enable. 6 new BlockedGate values + 4 Settings + 6 log events. 18 active tests + 6 cleanly-skipped placeholders. Closes BL-064 zero-trade gap. See memory `project_bl065_deployed_2026_05_04.md`.

**Original spec — flagged 2026-04-29, now historical:**
**Files:** `scout/social/telegram/listener.py` (cashtag-only branch ~L249-276), `scout/social/telegram/dispatcher.py`, `scout/social/telegram/resolver.py` (search-top-3 path), schema (`tg_social_channels` add column), tests
**Why:** Today, when a curator posts only `$EITHER` (cashtag) without a contract address, BL-064 sends a Telegram alert with top-3 CoinGecko candidates but **never** dispatches a paper trade — `listener.py:249` returns before `dispatch_to_engine`. With the active trade-eligible curators (`@thanos_mind`, `@detecter_calls`) currently posting cashtag-only hype, this means BL-064 has dispatched zero trades despite the listener being healthy. Extending dispatch to cashtags would unlock the bulk of curator activity.
**Design decisions to make:**
- **Candidate selection** — top-1 by mcap? Top-1 with minimum mcap floor (e.g. $1M to skip dead tickers)? Top-1 with confidence-margin gap over #2? Reject if top-3 is ambiguous (small mcap spread)?
- **Safety** — current path skips GoPlus on cashtags (no CA to query). Either: (a) fetch CA from CoinGecko candidate before safety check, (b) allow cashtag dispatches per-channel via new `cashtag_trade_eligible` flag (mirrors `safety_required`), (c) require both `trade_eligible=1 AND safety_required=0` to opt in.
- **Per-channel opt-in** — separate column `cashtag_trade_eligible` on `tg_social_channels` so we can enable for the trusted-curator subset without auto-enabling the alert-only ones.
- **Trade size** — same `PAPER_TG_SOCIAL_TRADE_AMOUNT_USD=300` as CA path, or smaller given lower confidence (e.g. $150)?
- **Dedup with CA path** — if the same curator later posts the CA for the same token (cashtag→CA upgrade path), do we open a second trade? Probably no — same `_has_open_tg_social_exposure` check already covers it once we resolve the cashtag to a coin_id.
**Acceptance:** Post a `$<CASHTAG>` message in a channel marked `cashtag_trade_eligible=1`, verify a paper trade opens with `signal_type=tg_social`, `signal_data` carries `{"resolution": "cashtag", "cashtag": "$X", "candidate_rank": 1, "candidates_total": 3}`, and the existing alert-only channels remain alert-only.
**Estimate:** 0.5-1 day with tests.

### BL-066: Dashboard view for BL-064 activity (channels, messages, alerts)
**Status:** SHIPPED-VARIANT 2026-05-04 — original 5-endpoint scope reduced after drift check found `/api/tg_social/alerts` (composite) + `TGAlertsTab.jsx` already deployed. BL-066' (gap-fill) PR #66 squash-merged as `6b95c2f`, deployed VPS 2026-05-04T06:09:04Z: added `/api/tg_social/dlq` + extended composite endpoint with `cashtag_dispatched_24h` + per-channel cashtag fields (`cashtag_trade_eligible`, `cashtag_dispatched_today`, `cashtag_cap_per_day`) + new `TGDLQPanel.jsx`. 12 active tests + 3 cleanly-skipped. **Lesson learned:** `find . -name __pycache__ -exec rm -rf {} +` mandatory on VPS after any `git pull` touching `dashboard/` Python (stale .pyc caused 14 startup 500s). See memory `project_bl066_deployed_2026_05_04.md` + `feedback_clear_pycache_on_deploy.md`. **Remaining gap (low priority):** original spec proposed 5 separate endpoints; composite covers 95% of need, defer split unless operator finds it limiting.

**Original spec — flagged 2026-04-29, now historical:**
**Files:** `dashboard/api.py` (new `/api/tg_social/*` endpoints), `dashboard/db.py`, `dashboard/frontend/components/` (new TGSocial section), `dashboard/frontend/main.jsx` (add tab or section)
**Why:** BL-064 has been live since 2026-04-27 with 1,019 messages ingested, 487 signals parsed, 395 in DLQ — and there is currently **zero dashboard visibility** into any of it. Operators have to SSH to the VPS and run sqlite queries to see channel activity. The Telegram alert channel that was supposed to be the primary visibility surface is non-functional because the bot token is a placeholder. Until the token is fixed, the dashboard is the only realistic visibility surface.
**Endpoints to add:**
- `GET /api/tg_social/channels` — list configured channels with `trade_eligible`, `safety_required`, last_seen_msg_id, last_message_at, listener_state
- `GET /api/tg_social/messages?limit=20` — recent messages with cashtags/contracts extracted, has_ca flag
- `GET /api/tg_social/signals?limit=20` — recent resolved signals + which message they came from, dispatch outcome
- `GET /api/tg_social/dlq?limit=20` — recent DLQ entries with error, channel, message preview
- `GET /api/tg_social/stats` — totals: messages last 24h by channel, resolution success rate, dispatch rate, DLQ rate
**Frontend:** new "Social" tab (or section in Health tab) showing channel health, recent messages, recent signals, DLQ count, link to full DLQ detail.
**Acceptance:** Operator can open dashboard, see at a glance: are listeners running? are messages flowing? what's in DLQ? did a trade dispatch?
**Estimate:** 0.5-1 day backend + 0.5 day frontend.

### BL-071a': Wire chain_match writers + DexScreener fetch for memecoin outcome hydration
**Status:** SHIPPED 2026-05-04 — PR #64 squash-merged as `cbb1e7f`, deployed VPS. Closes the silent-skip surface for DexScreener-resolved memecoin chain outcomes. See memory `project_chain_revival_2026_05_03.md` (related context).

**Original spec (post-Bundle A 2026-05-03), now historical:**
**Tag:** `chain-pipeline` `outcome-telemetry` `unblocks-BL-071a-fully`
**Files:** `scout/chains/tracker.py` (`_record_chain_complete`, `_record_expired_chain` — accept and store mcap; hydrator's populated-branch — replace silent `continue` with DexScreener FDV fetch + outcome computation), `scout/chains/events.py` or chain-completion caller chain (pass current FDV through to writers), tests
**Why:** Bundle A added `chain_matches.mcap_at_completion REAL` column + hydrator branch that skips silently when populated. Writers still pass NULL because adding the caller-wiring would have grown Bundle A scope. Once writers populate the column AND the hydrator inlines the DexScreener fetch, hit/miss outcomes flow for memecoin chain_matches. Closes the BL-071a death-spiral structurally.
**Acceptance:**
- New memecoin chain_matches have non-NULL `mcap_at_completion`.
- LEARN cycle emits `chain_outcomes_hydrated count>0` for memecoin pipeline (instead of `chain_outcomes_unhydrateable_memecoin total_unhydrateable=N` aggregate warning).
- Pattern hit-rate becomes meaningful for memecoin patterns.
- Remove the `chain_outcomes_unhydrateable_memecoin` warning OR downgrade to INFO (per Bundle A design-doc §6 Q1).
- **Coupling guard (per Bundle A PR-review R2 S2):** writer-wiring + DexScreener fetch MUST land in the same PR. Splitting them would re-introduce the silent-skip path on populated rows (hydrator skips silently when `mcap_at_completion` is set; if writers wire the column without the fetch landing, every populated row is silently dropped from outcome resolution). Add a test that fails if `chain_matches` has any row with non-NULL `mcap_at_completion` AND `outcome_class IS NULL` AND `completed_at < now-48h` after a LEARN cycle — that's the canary.
- **Re-introduce per-cause counters in the aggregate warning** when the failure modes are actually distinguishable (today they aren't; Bundle A intentionally collapsed `mcap_at_completion_null_count` + `outcomes_table_empty_count` into just `total_unhydrateable` to avoid misleading log fields).
**Estimate:** 0.5d (small caller-chain edit + DexScreener fetch in hydrator + tests + coupling-guard test).

### BL-071a: Investigate why memecoin `outcomes` table is empty
**Status:** Not started — flagged 2026-05-03 during BL-071 investigation
**Tag:** `research-gated` `chain-pipeline` `outcome-telemetry`
**Files:** likely `scout/chains/tracker.py`, `scout/memecoin/`, wherever outcomes are supposed to be written for pump.fun / dexscreener tokens
**Why:** `chain_matches.update_chain_outcomes` queries `outcomes WHERE contract_address = ? AND price_change_pct IS NOT NULL` for `pipeline='memecoin'` rows. The `outcomes` table has **0 rows** in prod. So memecoin chain_matches can NEVER get hydrated — they all stay `outcome_class=NULL` or get marked `EXPIRED` by the miss-recorder. That's half the cause of the BL-071 auto-retirement death spiral.
**Investigation:** trace which writer is supposed to insert into `outcomes` for memecoin tokens. Possibilities: (a) writer never existed (intentional — memecoin pipeline never had outcome tracking), (b) writer exists but is gated behind a disabled config flag, (c) writer exists but is silently failing.
**Acceptance:** Either (a) confirm `outcomes` is dead by design and route memecoin chain_matches to a different outcome source (e.g. `paper_trades` outcomes), OR (b) re-enable the writer + verify rows start appearing.
**Estimate:** 0.5–1 day investigation + fix.

### BL-071b: narrative `chain_matches` start at `outcome_class='EXPIRED'`, hydrator skips them
**Status:** Not started — flagged 2026-05-03 during BL-071 investigation
**Tag:** `research-gated` `chain-pipeline` `outcome-telemetry`
**Files:** `scout/chains/tracker.py:518` (`_record_chain_miss` writer), `scout/chains/tracker.py:550` (`update_chain_outcomes` hydrator)
**Why:** All 154 narrative `chain_matches` in prod have `outcome_class='EXPIRED'` with NO `evaluated_at` timestamp — meaning they were marked EXPIRED at write-time by `_record_chain_miss`, not by the hydrator. The hydrator's `WHERE outcome_class IS NULL` clause then skips them entirely, even though the `predictions` table has 42 actual `'HIT'` outcomes that should propagate. Net effect: narrative pattern hit-rate is permanently 0% even when patterns succeed. Other half of the BL-071 death spiral.
**Two design choices to evaluate:**
- (a) Change `_record_chain_miss` to write `outcome_class=NULL` (let the hydrator decide later), OR
- (b) Widen the hydrator's WHERE clause to include `outcome_class='EXPIRED'` (re-evaluate marked-expired matches against the predictions table).
- Option (a) is cleaner semantically — EXPIRED should mean "we waited and nothing happened", not "we wrote it as EXPIRED on first encounter". But may break other consumers expecting EXPIRED-at-write-time semantics.
**Acceptance:** narrative chain_matches start producing `outcome_class='hit'` for tokens whose predictions resolved as HIT. Pattern hit-rate becomes meaningful (non-zero for real winners).
**Estimate:** 0.5 day investigation + 0.5 day fix + tests.

### BL-070: Entry stack gate — refuse trades with insufficient signal confirmation
**Status:** **SHELVED — re-evaluate when system net is clearly negative again, OR if 30d data still shows large stack=1 bleed after 2026-05-15 checkpoint.**
**Tag:** `research-gated` `strategy` `entry-filter` `requires-backtest`
**Plan:** `tasks/plan_bl070_entry_stack_gate.md`
**Why shelved (history):** v1 backtest (`scripts/backtest_v1_signal_stacking.py`) showed stack≥2 trades net +$722 vs stack=1 trades net −$1,243 over 30d. Plan proposed entry-time gate filtering stack=1 trades. Adversarial reviewer's Q10 prompted a baseline check that showed Tier 1a `enabled=0` for `gainers_early` + `trending_catch` would capture $933 of the $1,243 swing with zero new code, so we executed the Tier 1a kill 2026-05-01 instead of building BL-070. **However:** the kill of `gainers_early` was reversed 2026-05-03 when the post-PR-#59 data showed it had become profitable (+$8.61/trade across 59 closes). PR #59 + chain dispatch revival + Tier 1a infrastructure together appear to be enough to swing the system net positive without BL-070.
**Resume protocol:** Only revisit if (a) the 2026-05-15 checkpoint shows 14d net materially negative, OR (b) a future targeted backtest (point-in-time entry replay, paper_trades source removed, index audit, lookback sensitivity sweep) shows entry-time stack-gate lift > $200/30d on top of the current state. If neither — BL-070 is structurally unneeded; close as won't-fix.

### BL-067: Conviction-locked hold — extend exit gates when independent signals stack on the same token
**Status:** **RESEARCH-GATED — DO NOT IMPLEMENT YET.** Requires backtest + design decisions documented below before any production code lands.
**Tag:** `research-gated` `strategy` `multi-signal` `requires-backtest`
**Files (when implementation starts):** `scout/trading/evaluator.py`, `scout/trading/conviction.py` (new), `scout/db.py` (signal-stack lookup), `scout/trading/params.py` (per-signal opt-in column), tests.
**Why — the BIO case study (2026-04-30):** BIO (`bio-protocol`) was caught across **5 independent signal surfaces over 7 days** (`first_signal` → `gainers_snapshots` → `trending_snapshots` → `losers_contrarian` on dip → `narrative_prediction` → `trending_catch` → `gainers_early` + DEX-side wrapper). Each fired a *separate* paper trade that exited within 2.5h–25h on trailing-stop / peak-fade / expiry. Net captured: **+$63 across 5 trades**. If the FIRST trade (`#869 first_signal`, opened 2026-04-23 01:10 at $0.0349) had been held continuously, the single position would now sit at **+16.3% / ~$49 unrealized** with a 7-day peak near +37.8%. **One position held through the multi-signal confirmation beats five positions churned in 12-hour windows.** The system correctly identified high conviction; the exit logic ignored that context. This is a structural, not BIO-specific, gap.
**Concept:** When a paper trade is open AND `N >= 2` *distinct* independent signals fire on the same `token_id` AFTER `opened_at`, the trade enters "conviction-locked" mode with extended exit gates:

| Stacked signals | max_duration_hours | trail_pct | sl_pct |
|---|---|---|---|
| 1 (default) | from `signal_params` | from `signal_params` | from `signal_params` |
| 2 | +72h | +5pp (cap 35%) | +5pp (cap 35%) |
| 3 | +168h | +10pp (cap 35%) | +10pp (cap 40%) |
| ≥4 | +336h | +15pp (cap 35%) | +15pp (cap 40%) |

**Definition of "distinct independent signal":**
- Different `signal_type` from the trade's own `signal_type`
- Fired *after* the trade's `opened_at`
- Not a duplicate of a `signal_type` already counted in the stack
- Sources to query: `gainers_snapshots`, `losers_snapshots`, `trending_snapshots`, `velocity_alerts`, `volume_spikes`, `narrative_predictions`, `chain_matches`, `tg_social_signals` — all already populated in scout.db.

**Open design questions (must resolve BEFORE coding):**
1. **Lookback window** — count only signals fired in last 7d, or full open-life?
2. **Per-signal-type opt-in** — does `narrative_prediction` (slow, multi-day window) benefit, or does conviction-lock only apply to fast signals (`gainers_early`, `first_signal`, `volume_spike`)?
3. **Interaction with PR #59 adaptive trail (low-peak tightening)** — does conviction-lock OVERRIDE the low-peak threshold (peak<20% → trail to 8%)? Or compose? They pull opposite directions.
4. **Interaction with BL-063 moonshot trail (peak ≥ 40% → 30% trail)** — moonshot is peak-driven, conviction is signal-count-driven. Likely compose (whichever is wider wins), but verify.
5. **Cap on stack count** — count up to 4? 6? Diminishing returns past N=3?
6. **Storage** — compute stack on-the-fly each evaluator pass (cheap, ~10 row DB hit), or persist `conviction_stack_count` column on `paper_trades`?
7. **Per-signal `conviction_lock_enabled` boolean on `signal_params`** — same calibration table controls which signal-types respect the multiplier.
8. **TG social interaction** — should `tg_social_signals` count as a stacked signal? It's a separate detection surface but the same trade would already be open under that signal_type if dispatched.
9. **Conviction stack downgrade** — once locked, does the lock stay regardless of subsequent activity? Or expire if no new signals fire for X days?

**Required research before implementing:**
1. **Backtest script** (`scripts/backtest_conviction_lock.py`) replaying last 30-90d of paper trades:
   - For each open trade, compute the stack count at every evaluator tick
   - Simulate exits under conviction-locked params vs the actual exits
   - Output: number of trades that would have been locked, simulated PnL delta vs actual, win-rate change, max-hold change, expired-pct change
2. **Survey of "BIO-like" plays in the existing data** — count how many tokens hit `N≥3` stacked signals over a 7d window in 2026-04 paper trades. If the answer is "BIO is unique," the feature is a poor ROI investment.
3. **Document edge cases:** what happens to a conviction-locked trade when the original `signal_type` gets auto-suspended (Tier 1b)? When an additional signal of an excluded-from-calibration type (`narrative_prediction`) fires?
4. **Compare to existing PR #6 (Multi-Signal Conviction Chains)** — that ships an *alert-time* convicition concept; this is *exit-time*. Verify they're orthogonal, not duplicating logic.

**Acceptance (when implementation eventually lands):**
- `backtest_conviction_lock.py` shows ≥10% PnL lift on simulated 30d window vs actual
- BIO replay demonstrates the trade staying open ≥5 days vs current ≤26h exits
- All 5 design questions above resolved in PR description
- Per-signal opt-in via `signal_params.conviction_lock_enabled` column (default OFF — deploy as no-op, flip per signal after observation)
- Tests: stack counts correctly, conviction-lock + adaptive-trail compose correctly, conviction-lock + moonshot compose correctly, suspended source signal does NOT block conviction lock from staying active
- Dashboard surfaces: badge on open positions showing current stack count + "conviction-locked" status

**Estimate (post-research):** 1.5–2 days code + tests + dashboard surface. Backtest script is a separate ~0.5 day deliverable that gates everything else.

**Resume protocol:** When user says "let's work on conviction-locked hold" or "BL-067", FIRST step is the backtest script. Do not write `scout/trading/conviction.py` until the backtest output justifies it.

### BL-075: Slow-burn miss diagnostic + watcher (RIV-shape blind spot)
**Status:** PHASE A + PHASE B SHIPPED 2026-05-10. **Phase A** (mcap-missing telemetry) shipped 2026-05-03; 6d telemetry showed 53.5% mcap-null rate (>5% gate → Phase B unblocked). **Phase B** PR #91 (`395feab`) `detect_slow_burn_7d` + schema 20260515; PR #93 (`975c45b`) silent-skip telemetry follow-up (heartbeat counter + all-skipped WARNING + always-emit summary log). 21 first-cycle detections; 47.6% momentum overlap (under 70% gate). **14d shadow soak ends 2026-05-24** — kill criterion + promotion-to-paper-dispatch decision at that point. See memory `project_bl075_phase_b_2026_05_10.md`.
**Tag:** `phase-a-shipped` `phase-b-shipped` `shadow-soak-active` `detection-blind-spot`
**Motivating evidence (2026-05-03):** RIV (`riv-coin`) ran $2M → $200M mcap over 30 days — exactly the asymmetric move the system exists to surface. SSH audit against prod `scout.db` returned **zero rows** for RIV across `gainers_snapshots`, `trending_snapshots`, `velocity_alerts`, `volume_spikes`, `momentum_7d`, `second_wave_candidates`, `predictions`, `narrative_signals`, `chain_matches`, `tg_social_signals`, `candidates`, `paper_trades`, `alerts`. Only trace: one row in `price_cache` from 2026-05-01T00:08Z with `market_cap=0.0` (CoinGecko returned null mcap, our parser writes 0). For context: gainers polling captured 90,002 rows in last 30d; trending captured 5,655. Polling is healthy. RIV simply never appeared in either.
**Best-fit hypothesis (three compounding causes):**
1. **CoinGecko `/coins/markets` 1h-change top-50 cut.** A 100x distributed over 30 days averages ~16%/day; individual 1h windows may rarely hit the top-50 cut. We catch concentrated short pumps (BLESS 16h, GENIUS 10.5h, MEZO 8h) — slow-burn marathons fall through.
2. **`market_cap=0.0` silent rejection.** BL-010 hard-rejects `liquidity_usd < $15K` and the predictions agent floors `market_cap_at_prediction`. CoinGecko returning null mcap → our parser writes 0 → multiple downstream gates auto-drop without logging a rejection. No "rescue" path for tokens with strong price action but missing mcap data.
3. **Trending-list miss.** Our trending poller catches 91.8% of CoinGecko Highlights tokens — but RIV apparently never made that tab during a poll cycle (or pumped between polls). We can't distinguish from the data.
**Honest scope caveat:** n=1. RIV alone doesn't justify a major detection rebuild. The point of Phase A is to find out **how often this is actually happening** before building anything heavyweight.

**Phase A — Cheap diagnostic (1h):**
- Add a `mcap_missing_count` counter to ingestion telemetry (`scout/ingestion/coingecko.py` + heartbeat log).
- Increment when CoinGecko returns a token with `market_cap` null/0 but `current_price > 0`.
- Log to existing heartbeat output every 5min (matches BL-033 pattern).
- **Acceptance:** After 7d of telemetry, we know the rate of mcap-missing silent rejections. Decision tree:
  - If < 1% of unique tokens scanned → silent-rejection is a corner case; close BL-075 as won't-fix on this axis.
  - If 1–5% → worth a fallback (estimate mcap from `volume_24h × ratio` or pull from DexScreener); tractable scope expansion.
  - If > 5% → significant blind spot; Phase B is justified.

**Phase B — Slow-burn watcher (shadow-only, ~0.5d after Phase A):**
- New module `scout/early/slow_burn.py` — separate from existing detection layer.
- Filter: `price_change_7d > 50%` AND `price_change_1h < 5%` (the inverse of velocity_alerter — slow accumulation, not concentrated pump).
- Write to new `slow_burn_candidates` table with snapshot history.
- **No paper trade dispatch.** Research-only, like the original PR #27 velocity_alerter pattern. Shadow soak ≥ 14d before any signal-routing decision.
- **Acceptance:** After 14d shadow soak, count tokens that flagged → became 5x+ runners. If hit-rate matches or beats existing velocity_alerter (~zero false-negative cost; the test is whether the new signal catches misses the existing layer doesn't), promote to a real signal type with paper trade dispatch behind a flag (`SLOW_BURN_DISPATCH_ENABLED=False` default).

**Cross-references (do NOT pre-couple, but worth knowing):**
- BL-073 Phase 1 GEPA on `narrative_prediction` could plausibly evolve a slow-burn classifier as a downstream consumer of the same eval set. Worth re-checking after Phase 1 ships (if it ships).
- BL-032 social signal source decision — slow-burn tokens often have organic social mentions before the price move; a working `social_mentions_24h` signal could complement the slow-burn watcher.
- BL-067 conviction-locked hold — slow-burn signal would be one more independent surface that could stack into conviction-lock once both ship.

**Estimate:** Phase A: 1h. Phase B: 0.5d code + 14d shadow soak before any acceptance read.

**Resume protocol:** Operator says "BL-075" or "RIV miss" or "slow-burn watcher" → start with Phase A. Do not skip to Phase B; the diagnostic data informs whether B is worth building.

---

## P3 — Future / Nice-to-have

### BL-040: Add backtesting framework
**Status:** DONE — backtest CLI implemented (PR #8, `python -m scout.backtest`)
**Why:** PRD Phase 4 (weeks 4-6). Need 30 days of outcome data first. /backtest slash command exists but needs real data.

### BL-041: Add X/Twitter social monitoring
**Status:** MERGED INTO BL-032 (2026-05-03) — see "Social signal source decision". X/Twitter is one of several possible sources; the actual question is "which source fills the dead `social_mentions_24h` signal", not "must we use Twitter specifically". Resolved at the decision level.

### BL-042: Refactor test helpers to use conftest.py fixtures
**Status:** DONE — 17 test files migrated to shared conftest.py fixtures
**Why:** Code review M5 — shared fixtures added to conftest.py but existing tests still use local helpers. Low priority cleanup.

### BL-043: Add Prometheus/Grafana monitoring
**Status:** DEFER UNTIL BL-073 PHASE 2 DECIDED (tagged 2026-05-03)
**Tag:** `defer-until-BL-073-Phase-2` `observability` `parallel-work-risk`
**Why:** Production observability — export scan rates, alert rates, MiroFish latency as metrics.
**Why deferred:** BL-073 Phase 2 (`JackTheGit/hermes-ai-infrastructure-monitoring-toolkit`, 0.5–1d) provides Telegram bot + cron + monitoring as a near-drop-in. If that ships, much of this work is redundant. Decision tree:
- If BL-073 Phase 2 ships → re-scope BL-043 to "Prometheus exporters for what the Hermes monitoring toolkit doesn't cover" (likely much smaller).
- If BL-073 Phase 2 is rejected (operator declines new VPS service) → BL-043 returns to its original full scope.
- If BL-073 Phase 2 is still unfunded at 2026-06-03 (+30d check) → re-evaluate independently.
**Do not parallel-work** with BL-073 Phase 2 — risk of building Prometheus scaffolding that the Hermes toolkit replaces.

### BL-044: VPS deployment with systemd service
**Status:** DONE — deployed to Srilu VPS (89.167.116.187)
**Why:** Production deployment. Run scanner as a persistent service with auto-restart.
**Services:** `gecko-pipeline.service`, `gecko-dashboard.service` (systemd, enabled on boot)
**Dashboard:** http://89.167.116.187:8000

---

## Virality Detection Roadmap — Multi-Source (Apr 2026)

**2026-05-14 Hermes-first overlay:** This roadmap predates the live Hermes X/KOL narrative path and the May 14 crypto-skill discovery pass. Treat the ranked rollout below as historical context, not execution order. Updated discipline:
- Social/influencer work: use installed Hermes `xurl` + `kol_watcher` + `narrative_classifier` + `narrative_alert_dispatcher` first; do not start custom Twitter/LunarCrush code until residual gaps are proven.
- Market-data work: use CoinGecko's first-party Agent SKILL/API docs as review input, but keep gecko-alpha responsible for durable ingestion, signal persistence, and dashboards.
- On-chain work: compare Dune/Nansen/Helius/Moralis/pump.fun proposals against GoldRush MCP/agent skills before building provider-specific custom code.
- Meta-classification work: if BL-073 Phase 1 ships, prefer the Hermes self-evolution/eval harness over a fresh custom classifier.

**Context:** ASTEROID (+114775% / +50036.5%) exposed the limit of CoinGecko-only detection. Price/volume is the *symptom* of virality, not the cause. No amount of ML on price history predicts a Musk tweet. Detection scales with **data sources**, not with model training. Each new source unlocks a distinct virality trigger class.

**Trigger taxonomy → data source → lead time:**

| Trigger class | Example | Source | Lead time |
|---|---|---|---|
| Celebrity/influencer endorsement | Musk reply | Twitter/X API + LunarCrush influencer list | seconds–minutes |
| Exchange listing / rumor | Binance, Coinbase | Twitter CEX accounts + announcement bots | minutes (rumor) / same-second (official) |
| News / macro event | ETF approval, SEC ruling | CryptoPanic / CoinDesk / Bloomberg | minutes |
| Cultural moment | Polaris Dawn, elections, viral TikTok | Twitter trending + Google Trends + Reddit | hours–days |
| Coordinated degen campaigns | Telegram pumps, CT thread waves | Telegram/Discord scraping + X reply-velocity | minutes |
| Copycat mania | ASTEROID → instant SHIBA-2 / ORBIT | pump.fun new-deploy watcher + fuzzy-match | seconds |
| Whale / smart money | Labeled wallet accumulation | Nansen / Arkham / Dune | minutes |
| Narrative rotation | AI / RWA / DePIN sector pumps | Our category_snapshots + LunarCrush topics | tens of minutes |
| Perp / funding anomaly | Funding flip, OI spike on perps | Binance / Bybit / OKX WebSockets | seconds |
| Developer / project news | GitHub teasers, team posts | GitHub webhook + project Twitter | hours |

**Ranked rollout (ROI = coverage × lead-time ÷ effort × cost):**

| # | Source | Classes covered | Lead time | Effort | Cost |
|---|---|---|---|---|---|
| 1 | DexScreener `/token-boosts/top` + GeckoTerminal per-chain trending | paid-promo, copycat, rotation | seconds–min | 1–2 d | free |
| 2 | CryptoPanic news feed | news/macro | min (free) / sec (paid) | 2 d | free basic |
| 3 | Binance/Bybit perp WebSocket (funding + OI anomaly) | perp/funding | seconds | 2–3 d | free |
| 4 | LunarCrush Discover | influencer, cultural, rotation | minutes | 4–5 d | $24/mo |
| 5 | pump.fun new-deploy watcher (Solana) | copycat | seconds | 5–6 d | $0–49/mo (Helius) |
| 6 | Dune Analytics smart-money queries | whale accumulation | minutes | 3–4 d | free–$390/mo |
| 7 | Nansen Smart Money API (upgrade from #6) | whale | sec–min | 4–5 d | $150–1,500/mo |
| 8 | Twitter/X API direct (only if LunarCrush insufficient) | influencer | seconds | 5–7 d | $200–5,000/mo |

**Skip list (negative ROI):** Telegram/Discord scraping (legal gray, noisy), GitHub webhooks (too niche), Reddit velocity (hours of lag), Arkham scraping (TOS risk).

**Sprint plan:**

- **Sprint 1 — free sources, quick wins (1 week):**
  - PR #28 — DexScreener boosts + GeckoTerminal trending → `velocity_boost` tier
  - PR #29 — CryptoPanic news-tag watcher → `news_watch` tier
  - PR #30 — Binance/Bybit perp WebSocket anomaly detector → `perp_anomaly` tier
- **Sprint 2 — paid social, Musk-class catch:**
  - PR #31 — LunarCrush Discover integration → `social_velocity` tier ($24/mo)
- **Sprint 3 — on-chain upstream signal:**
  - PR #32 — Dune smart-money queries, cron-scheduled → `smart_money` tier
  - PR #33 — pump.fun new-deploy watcher with fuzzy-match → `copycat_launch` tier
- **Sprint 4 — meta-layer:**
  - PR #34 — Ensemble virality classifier. Requires ≥3 tiers live + ~2 weeks of labeled data. Tags each alert: `influencer-driven | whale-accumulation | rotation | copycat | news | perp-driven`. Telegram messages gain virality-class badges; exit logic diverges by class (influencer dies in hours, whale runs for days). **Cross-ref (2026-05-03):** the BL-073 Phase 1 framework (`NousResearch/hermes-agent-self-evolution`, DSPy + GEPA) is structurally compatible with this classification problem — if Phase 1 ships and works on `narrative_prediction`, PR #34 becomes a downstream consumer of the same pipeline rather than a separate build. Worth checking before building from scratch.

**First action:** PR #28 (DexScreener boosts + GeckoTerminal trending) — free, 2 days, proves the paid-promo hypothesis before committing to LunarCrush subscription. Execute after PR #27 velocity alerter stabilizes with ~48h of live Telegram traffic.

**What learning CAN do on existing data (no new sources):**
- Retrospective virality classifier: label past alerts (virality vs organic) using already-collected features — wallet concentration, holder_growth_1h curve, vol/mcap slope across 3+ cycles. Virality has narrow wallet sets + vertical-then-vertical curves; organic has broader accumulation.
- Ensemble on existing signals: velocity alert + extreme holder growth + rising vol/mcap for 3 cycles → "suspected virality" tag even before Sprint 4.

**Realistic expectation:** LunarCrush + DexScreener boosts gets us 5–15 minutes faster on narrative-driven pumps. We will never beat Musk-timed institutional trades (they have co-located Twitter feeds). Target: beat retail discovery by a meaningful window.

---

## Early Detection Roadmap — Phased Approach

**Goal:** Detect tokens that will appear on [CoinGecko Highlights](https://www.coingecko.com/en/highlights) (Trending Coins + Top Gainers) 1-2 hours before they appear, for manual research and informed buy decisions.

**Architecture:** Parallel early detection layer running alongside existing pipeline in shadow mode. Logs predictions with timestamps, compares against CoinGecko trending snapshots. Existing pipeline unchanged.

**Success metrics:** Hit rate (% of flagged tokens that appeared on Highlights), average lead time (minutes before CoinGecko), misses (tokens that trended without our flag).

> **NOTE (Apr 2026):** The CoinGecko Trending Tracker (PR #12) + Volume Spike Detector (PR #15) now serve as the primary early detection layer, using FREE CoinGecko data. LunarCrush/Santiment/Nansen phases are DEFERRED — the free approach achieved 56/61 (91.8%) trending hit rate with 62.4h avg lead time.

> **NOTE (2026-05-14):** The "Phase 1 LunarCrush CURRENT" label below is stale. The current first-line path is: fix CoinGecko breadth/hydration using CoinGecko SKILL/API docs as reference, reuse Hermes X/KOL narrative signals for social confirmation, and compare any on-chain provider work against GoldRush before building custom integrations.

### Phase 1: LunarCrush Social Velocity ($24/mo) — CURRENT
**Status:** STALE / DO NOT START WITHOUT NEW HERMES-FIRST REVIEW
**Rationale:** Social mention velocity is the #1 input to CoinGecko's trending algorithm. LunarCrush aggregates Twitter + Reddit + Telegram into a single API. Cheapest way to validate the thesis.
**Modules:**
- `scout/early/lunarcrush.py` — API client, fetch Galaxy Score + social volume
- `scout/early/tracker.py` — Spike detection, comparison vs CoinGecko trending
- `scout/early/models.py` — EarlySignal, TrendingSnapshot models
- DB tables: `early_signals`, `trending_snapshots`
- Dashboard: "Early Detection" tab with live signals, hit rate, lead time
**Config:** `LUNARCRUSH_API_KEY`, `LUNARCRUSH_POLL_INTERVAL=300`, `SOCIAL_VOLUME_SPIKE_RATIO=2.0`, `GALAXY_SCORE_JUMP_THRESHOLD=10`
**Validation:** After 2-4 weeks of shadow data, measure hit rate + lead time. If >50% hit rate with >30 min avg lead time, thesis is validated.

### Phase 2: Santiment Cross-Validation ($49/mo)
**Status:** Future — contingent on Phase 1 validation
**Rationale:** Second independent social signal source. Santiment's "emerging trends" and social volume divergence metric provides cross-validation against LunarCrush. Reduces false positives.
**Integration:** GraphQL API via `sanpy` Python client. Add as second signal source in `scout/early/`. Boost confidence when both LunarCrush AND Santiment flag the same token.
**Trigger:** Proceed if Phase 1 hit rate is promising but false positive rate is >40%.

### Phase 3: Nansen Smart Money ($49/mo + API credits)
**Status:** Future — strongest signal but most expensive
**Rationale:** Smart money (whale/fund wallets) accumulating a token typically precedes social buzz by hours. This catches a different phase of the pump lifecycle — accumulation before attention.
**Integration:** REST API. Track labeled wallet inflows for tokens in our candidate pool. When smart money + social spike align, highest confidence signal.
**Trigger:** Proceed if Phases 1-2 show social signals alone miss tokens that pump from whale accumulation without initial social buzz.

### Alternative Sources (if LunarCrush doesn't validate)
- **Dune Analytics** — Custom SQL queries on on-chain social/volume data
- **Defined.fi** — Real-time new pair discovery across 40+ chains (free tier)
- **Birdeye** — Solana-specific trending (free-$49/mo)
- **CoinGecko unused endpoints** — `watchlist_portfolio_users` spikes, category momentum, `is_anomaly` flags (free, but may be circular)
- **CoinMarketCap** — Cross-reference trending from different algorithm (free 333 req/day)

### Reviewer Notes (preserved for context)
> The lead time numbers from social APIs mean "before CoinGecko page updates", not "before price moves." For automated trading this is insufficient edge. However, for manual research (our use case), even minutes of lead time is valuable for investigating WHY a token is gaining attention before the retail crowd sees it on Highlights.
>
> If pivoting to automated trading in the future, the architecture changes significantly: need execution engine, risk management, MEV awareness, and sub-second latency. That is a separate project.

---

## Completed Features (April 2026 Session)

### Narrative Rotation Agent (PR #1)
**Status:** DONE — live on VPS
Autonomous 5-phase agent: OBSERVE → PREDICT → ALERT → EVALUATE → LEARN
Self-improving via agent_strategy table. 26+ predictions, LEARN phase active.

### Counter-Narrative Scoring (PR #3)
**Status:** DONE — live on VPS
Adversarial risk analysis for both pipelines. Deterministic flags + LLM synthesis.

### Shared CoinGecko Rate Limiter (PR #4)
**Status:** DONE — live on VPS
Token bucket limiter (25/min) shared across all CoinGecko callers. Closes issue #2.

### Second-Wave Detection (PR #5)
**Status:** DONE — live on VPS
Detects tokens that pumped 3-14 days ago and are re-accumulating.

### Multi-Signal Conviction Chains (PR #6)
**Status:** DONE — live on VPS
Event store + temporal pattern matching. 3 built-in patterns with LEARN lifecycle.

### Heartbeat + LEARN Counter Integration (PR #7)
**Status:** DONE — live on VPS

### CoinGecko Watchlist Signal + Backtest CLI (PR #8)
**Status:** DONE — live on VPS

### Dashboard Expansion (PRs #9-11 + fixes)
**Status:** DONE — live on VPS
5 tabs: Pipeline, Narrative Rotation, Chains, Second Wave, Health
TokenLink component with CoinGecko/DexScreener routing.

### CoinGecko Trending Snapshot Tracker (PR #12)
**Status:** DONE — live on VPS. 15/15 trending tokens caught (100% hit rate), avg 25.6h lead time.
Validates core goal — snapshots trending page, measures if we caught tokens before they trended.

### Personalized Narrative Matching (PR #13)
**Status:** DONE — live on VPS. 3 alert modes: all/whitelist/blacklist.
Category + mcap preferences for alert filtering. 3 modes: all/whitelist/blacklist.

### Test Fixture Refactor + Backlog Cleanup (PR #14)
**Status:** DONE
Test fixture refactor (BL-042) + backlog cleanup.

### Volume Spike Detector + Top Gainers Tracker (PR #15)
**Status:** DONE — live on VPS. Detects individual token breakouts via 5x+ volume surges. Top gainers validation same pattern as trending tracker.

### Top Losers Tracker + Volume-Sorted Scan (PR #17)
**Status:** DONE — live on VPS

### Comprehensive Code Review — 26 Fixes (PR #18)
**Status:** DONE

### Peak Gain Tracking (PR #19)
**Status:** DONE — live on VPS

### main.py Refactoring + UNRESOLVED Fix (PR #20)
**Status:** DONE — 1513 to 668 lines

### Market Briefing Agent (PR #21)
**Status:** DONE — live on VPS

### Dashboard Improvements
**Status:** DONE — sortable columns, missed gainers section, heating lead time

### 7d Momentum Scanner
**Status:** DONE — live on VPS

### Volume Spike Detector Broadened
**Status:** DONE — expanded to 250 tokens

### Dashboard Redesign
**Status:** DONE
3-tab layout (Signals/Pipeline/Health), Early Catches validation, quality signals, price cache, Narrative vs Meme separation.

### Price Cache System
**Status:** DONE
Stores prices from pipeline fetches, dashboard reads from DB (zero extra CoinGecko calls).

### SQLite datetime string-comparison fix (PR #24)
**Status:** DONE — live on VPS
38 queries across 10 modules wrap stored columns with `datetime()` to force parsing on both sides. `datetime.isoformat()` writes `T`-separator; SQLite `datetime('now')` returns space-separator; `'T' > ' '` produced false-stale comparisons. Max price-divergence dropped from 16.99% to 4.07%, avg to 0.71%. VANA 1.75 stale-peak entries cleared.

### Momentum_ratio 24h floor (PR #25)
**Status:** DONE — live on VPS
`momentum_ratio` signal now requires 24h change ≥ `MOMENTUM_MIN_24H_CHANGE_PCT` (default 3.0%). Previously stablecoin peg wobble (0.05% / 0.08% = ratio 0.625 > 0.6) was triggering the +20-point signal, polluting paper trades with USDC/DAI/PYUSD showing uniform -0.5% losses. Zero stablecoins in paper book post-deploy.

### Paper trade hard cap + startup warmup (PR #26)
**Status:** DONE — live on VPS
Two gates on `scout/trading/engine.py`: Step 0 warmup (`PAPER_STARTUP_WARMUP_SECONDS=180`, `time.monotonic()`-based, immune to wall-clock jumps) refuses new trades for 3 min after startup. Step 2c cap (`PAPER_MAX_OPEN_TRADES=10`) caps concurrent opens. Fixes restart-burst behavior: every process restart was replaying every currently-qualifying token as a fresh signal, filling the book with 45+ positions. Verified: exactly 10 open post-restart, warmup skip logs fire at elapsed=138.6s, max-open skip logs fire at overflow.

### CoinGecko velocity alerter (PR #27)
**Status:** DONE — live on VPS (`VELOCITY_ALERTS_ENABLED=true`)
New `scout/velocity/detector.py` tier for catching asteroid-class pumps (ASTEROID +60087%) earlier than gainers / 7d-momentum trackers. Filters: 1h ≥ 30%, mcap $500K–$50M, vol/mcap ≥ 0.2, top-10 by 1h change, dedup 4h per coin-id via new `velocity_alerts` table. **Research-only — no paper trade dispatch.** Zero extra CoinGecko API calls (reuses `_raw_markets_combined` cache). 616 tests passing. Planned: meta-tier in Sprint 4 of Virality Roadmap.

### Open follow-ups noted during session
- **Edge detection for paper trades:** only open on *transition* into qualifier set, not current-state membership (prevents restart-bursts at root). Requires persisting previous cycle's qualifier set per signal type. Noted in PR #26 body.
- **DexScreener boosts + GeckoTerminal per-chain trending** as additional velocity sources. See Virality Roadmap PR #28.

---

## Trading Engine Roadmap

**Goal:** Autonomous DEX trading — detect signals, execute trades on-chain, manage positions.

**Approach:** Paper trading first (2 weeks) to prove edge with PnL data, then graduate to live trading with small positions ($50-100/trade).

### Architecture Decisions (Locked In)

**D1 — Pluggable engine:** The trading engine is an independent common component (`scout/trading/`) that any signal source can call. Interface: `engine.buy(token_id, chain, amount_usd)` / `engine.sell(...)`. Mode switchable: paper or live.

**D2 — Signal triggering:** Paper mode trades ALL signals (Option C) — volume spikes, narrative picks, trending catches, chain completions. Maximizes data collection. Live mode will use multi-signal confirmation (multi-layer agreement before executing).

**D3 — Chain support:** Chain-agnostic paper trading with chain metadata stored. Live execution targets BSC (PancakeSwap), Solana (Raydium/Jupiter), Ethereum/Base (Uniswap).

**D4 — Exit strategy:** Multi-checkpoint tracking (1h, 6h, 24h, 48h) for analysis + simulated take-profit (+20%) and stop-loss (-10%) for realistic PnL. Both run in parallel per trade.

**D5 — Libraries chosen:**
- EVM chains: `web3-ethereum-defi` (MIT, pip install, pure Python, 800+ stars)
- Solana: `raydium_py` or solana-py based library
- Paper trading shim: custom (~50 lines)
- NOT using Hummingbot (too heavy) or Freqtrade (no DEX support)

### Phase A: Paper Trading Engine (Current)
**Status:** LIVE — running on VPS since Apr 15. Currently on Iteration 4 (first_signal + narrative_prediction). Collecting data for 48h undisturbed.
**Module:** `scout/trading/`
```
scout/trading/
  engine.py        # Pluggable interface — buy/sell/get_positions/get_pnl
  paper.py         # Paper trading — simulate fills at current price, log to DB
  models.py        # PaperTrade, Position, PnL models
```
**DB tables:** `paper_trades`, `paper_positions`
**Dashboard:** Paper PnL section on Signals tab — per-signal-type performance
**Config:** `TRADING_ENABLED=true`, `TRADING_MODE=paper`, `PAPER_TRADE_AMOUNT_USD=50`
**Success criteria:** 2 weeks of paper trades with positive PnL after simulated fees → graduate to Phase B

#### Paper Trading Iterations

| Iteration | Signals Used | Result |
|-----------|-------------|--------|
| 1 | All 7 signals | All losing — bought at the top every time |
| 2 | Removed lagging signals | Still micro-cap junk |
| 3 | Added $5M mcap filter | momentum_7d still producing late entries |
| **4 (current)** | **first_signal + narrative_prediction only** | **Collecting data — 48h undisturbed run** |

### Phase B: Live Execution Engine (Future — after paper validates)
**Status:** Not started — blocked by Phase A validation
**Module extensions:**
```
scout/trading/
  live_evm.py      # web3-ethereum-defi — PancakeSwap, Uniswap swaps
  live_solana.py   # raydium_py — Raydium, Jupiter swaps
  risk.py          # Position sizing, max exposure, stop-loss enforcement
  wallet.py        # Encrypted private key management (NEVER in .env)
```
**Requirements before going live:**
- [ ] 2+ weeks of paper trading data showing positive PnL
- [ ] Risk management: max $50/trade, max $500 total exposure, automatic stop-loss
- [ ] Kill switch: `TRADING_KILL_SWITCH=true` instantly stops all trading
- [ ] Private key encryption (keyring or encrypted file, never plaintext)
- [ ] Manual approval for first 10 live trades (dashboard queue)
- [ ] Gas estimation + slippage protection per chain
- [ ] MEV protection (private RPC for Ethereum, Jito for Solana)

### Phase C: Advanced Trading (Future)
- Partial position scaling (enter 50%, add if signal strengthens)
- Dynamic position sizing based on signal quality score
- Cross-chain arbitrage (same token on multiple DEXes)
- Portfolio rebalancing
- PnL-based signal weighting (auto-increase position size for profitable signal types)

### RCA Results (Apr 19, 2026) — Validates the Trading Thesis
- 12/14 CoinGecko Highlights tokens caught (86%)
- 15/15 CoinGecko Trending tokens caught (100% hit rate)
- BLESS: caught 16h early, currently +216% 7d
- GENIUS: caught 10.5h early
- MEZO: caught 8h early
- RaveDAO: caught 33h early, +5333% 7d
- Gap: 2 tokens missed (aPriori, Bedrock) — individual breakouts without category momentum. Volume Spike Detector (PR #15) designed to catch these going forward.

---

## Actionability measurement substrate 2026-05-20

Two entries surfaced during the 2026-05-19 trader/strategist brainstorm synthesis: the next quality jump for actionability is durable point-in-time entry facts plus an operator feedback loop — NOT a smarter composite score. Both filed PROPOSED; implementation of `FOUNDATION` happens in its own design-then-implementation PR. `OPERATOR-FEEDBACK-MARKS` is a separate sequel and **must not be bundled** with the foundation implementation.

### BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION: stamp point-in-time entry facts for future V2
**Status:** SHIPPED 2026-05-20 — PR #200 merged at `b9dda34`. `paper_trade_entry_snapshots` sidecar table + writer + tests deployed. As of 2026-05-20T13:17Z: 10 fresh paper_trades have 1:1 sidecar coverage; 0 `entry_snapshot_stamp_failed`; 0 `SCHEMA_DRIFT_DETECTED`. I-B2 enrichment fold empirically verified — `mcap_usd_at_entry` correctly populated from enriched signal_data on chain_completed trades. 7-invariant stamp-verification probe on fresh trade 2223 (2026-05-20T05:07:46Z) passed.
**Original filed status:** PROPOSED 2026-05-20.
**Tag:** `actionability` `measurement-substrate` `paper-trading` `schema` `analytics`
**Why:** Actionability V2, source quality, and profit-pattern analysis should not reconstruct entry context from mutable/current tables. Reconstructed features can leak future state, vary by coverage, and smear stamped rows with historical rows. The next serious quality improvement is a measurement substrate, not a smarter-looking score.

**Core principle:** persist point-in-time entry facts at paper-trade open. This is metadata only: no classifier changes, no suppression, no capital allocation, and no paper-trade open/exit behavior changes beyond durable stamping.

**Must-have today-computable fields:**
- `entry_snapshot_version`
- `entry_snapshot_complete`
- `entry_snapshot_missing_fields`
- `signal_type`
- `mcap_usd_at_entry`
- `mcap_bucket_at_entry`
- `liquidity_usd_at_entry` when present in signal data
- `token_age_days_at_entry` or `first_seen_at_entry` when available
- `detected_by_combo_at_entry` when available
- `source_confluence_count_at_entry` when available
- `tg_channel_at_entry` for `tg_social` when available
- `actionability_version`
- `actionability_reason`
- active exit params at entry (`tp_pct_at_entry`, `sl_pct_at_entry`, and trail / peak-fade params if present in current signal params)

**Explicitly deferred fields:** `x_handle_at_entry` (pending PR #184 / X linkage design), `price_freshness_seconds_at_entry` (pending price-cache writer/hot-path instrumentation), richer resolver confidence/linkage state (partial today; should not block foundation).

**Coverage contract:** If all required today-computable fields are present, mark `entry_snapshot_complete=true`. Missing optional/deferred fields must not fail paper-trade open; record them in `entry_snapshot_missing_fields`. Dashboard/backtests must distinguish fully stamped rows, partially stamped rows, and pre-cutover/reconstructed historical rows.

**Design requirement:** Compare wide `paper_trades` columns vs a `paper_trade_entry_snapshots` sidecar table keyed by `paper_trade_id`. Prefer the shape that minimizes hot-path risk while keeping snapshot growth manageable. If unsure, stop at design PR rather than forcing a late-night migration.

**Acceptance:** New paper trades have durable entry snapshots with explicit coverage state. Historical/reconstructed rows cannot be silently mixed with complete stamped rows in actionability/V2 analysis.

### BL-NEW-ACTIONABILITY-OPERATOR-FEEDBACK-MARKS: dashboard learning-loop annotations
**Status:** PROPOSED 2026-05-20.
**Tag:** `actionability` `operator-feedback` `dashboard` `learning-loop`
**Why:** The false-negative explorer can surface exploratory winners, but without an operator mark there is no feedback loop from "this was real" back into future V2 design. Human review should be captured as durable metadata before it becomes tuning input.

**Scope:** From the trade detail drawer, allow the operator to mark a trade/signal as `real_winner`, `false_positive`, `interesting_but_late`, `bad_source_noisy`, or `ignore`, with an optional note. This should be a separate primitive from entry snapshots.

**Constraints:** No classifier changes, no suppression, no live/capital behavior, and no automated V2 learning from marks until separately designed. Implement after `BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION` or as a separate reviewed PR.

**Acceptance:** Operator feedback is persisted, visible in dashboard/read models, and exportable for later V2 review without changing trading behavior.

---

## Follow-ups filed 2026-05-20 from entry-snapshot impl-review pass (PR #200)

Seven entries surfaced during the two-vector reviewer pass against PR #200's
implementation. The two Vector B Important findings (I-B1 + I-B2) were folded
into PR #200 (`affafec`); these are the residual Minor / out-of-scope items.

### BL-NEW-ACTIONABILITY-CANDIDATES-FIRST-SEEN-PRESERVE: stop overwriting first_seen_at on re-ingest
**Status:** PROPOSED 2026-05-20.
**Tag:** `data-integrity` `candidates` `ingestion` `actionability`
**Why:** `_upsert_candidate` at `scout/db.py:4495` uses `INSERT OR REPLACE`,
and `CandidateToken.first_seen_at` defaults to `now()` on construction
(`scout/models.py:75`). When a token is re-ingested in a later cycle, the row
is replaced including `first_seen_at` — overwriting the earliest sighting with
the most-recent. Downstream consumers reading `first_seen_at` (now including
`paper_trade_entry_snapshots.first_seen_at_at_entry` via this PR) see a
lower-bound near-zero value instead of true age, especially for micro-cap
re-rotations. Surfaced as Vector B I-B3 against PR #200.

**Scope:** Either (a) change `upsert_candidate` to use a SQL `COALESCE`
pattern that preserves the earlier `first_seen_at` value, OR (b) split into
explicit `insert_candidate_if_new` + `update_candidate` paths so the writer
intent is explicit. Add a regression test that exercises the re-ingest path
and asserts `first_seen_at` does NOT change.

**Constraints:** Must NOT break existing test fixtures that rely on the
upsert semantics. Audit the data-on-disk for tokens with implausibly recent
`first_seen_at` (suspected re-ingestion drift) and decide whether to backfill
from `score_history.scanned_at` MIN.

**Acceptance:** `first_seen_at` is monotonic per `(contract_address, chain)`.
Regression test asserts. Documentation in `docs/gecko-alpha-alignment.md`
captures the contract.

### BL-NEW-ACTIONABILITY-MIGRATION-SCHEMA-DRIFT-DETECTED-LOG: parity with minara migration helper
**Status:** PROPOSED 2026-05-20.
**Tag:** `migration` `observability` `actionability`
**Why:** `_migrate_actionability_entry_snapshot_v1` ROLLBACK except-block omits
the `SCHEMA_DRIFT_DETECTED` operational log that the sibling
`_migrate_minara_alert_emissions_v1` emits at `scout/db.py:3554`. Operator
monitoring that greps `SCHEMA_DRIFT_DETECTED` won't catch a failure of the
new migration. Surfaced as Vector A Minor #1 against PR #200.

**Scope:** Add `_log.error("SCHEMA_DRIFT_DETECTED", migration=migration_name)`
to the except block at `scout/db.py:3650-3661`. Two-line change.

**Acceptance:** Sibling-migration parity; greppable from monitoring.

### BL-NEW-ACTIONABILITY-MIGRATION-ASSERT-SCHEMA: post-migration shape assert
**Status:** PROPOSED 2026-05-20.
**Tag:** `migration` `schema-drift` `actionability`
**Why:** The sibling minara migration has `_assert_minara_alert_emissions_schema`
(2-pass: before-index and after-index) that catches CHECK/column drift at
migration time. The new actionability migration has no equivalent. If a
future operator runs an `ALTER TABLE` on prod and the helper migrates a
partially-correct shape, drift surfaces at INSERT time (loud) instead of
migration time (loudest). Surfaced as Vector A Minor #2 against PR #200.

**Scope:** Add `_assert_paper_trade_entry_snapshots_schema` matching the
sibling shape. Wire into the migration helper. Add a test that mutates the
table (drops the CHECK constraint, removes a column) + re-invokes initialize()
+ asserts the assert raises with a clear error.

**Acceptance:** Drift surfaces at migration time, not INSERT time.

### BL-NEW-ACTIONABILITY-CANDIDATES-CASE-FIDELITY: test fixture vs prod data mismatch
**Status:** PROPOSED 2026-05-20.
**Tag:** `test-fidelity` `actionability`
**Why:** `tests/test_entry_snapshot.py::_ensure_candidate_row` lowercases the
contract_address before inserting (`contract_address.lower()`). Real-world
prod data carries checksummed mixed-case addresses (ETH/BSC). The
`_read_first_seen_at` query at `entry_snapshot.py:128` uses
`LOWER(contract_address)=LOWER(?)` to handle this, but the test fixture
data is degenerate for the case-fold path. Surfaced as Vector A Minor #4
against PR #200.

**Scope:** Add a single test that seeds a mixed-case `contract_address` (e.g.,
`0xAbCdEf...`) and asserts the LOWER() lookup resolves. Optional: audit
existing fixtures for similar degeneracy.

**Acceptance:** Case-fold path is structurally exercised in tests.

### BL-NEW-ACTIONABILITY-TG-SIGNAL-TYPES-EXPANSION: applicable-fields semantic re-check
**Status:** PROPOSED 2026-05-20 (evidence-gated).
**Tag:** `actionability` `entry-snapshot` `signal-types`
**Why:** `_applicable_optional_fields` at `entry_snapshot.py:45-47` hardcodes
the `tg_social` branch as the only signal_type for which `tg_channel_at_entry`
is required. Future TG-relay variants (`tg_kol`, `tg_dao_calls`, etc.) would
silently be marked `complete=1` with NULL channel, breaking source-quality
analyses that filter on tg_channel. Surfaced as Vector B Minor #2 against
PR #200.

**Scope:** Trigger condition — at the time a NEW tg_* signal_type is added
to `DEFAULT_SIGNAL_TYPES`. Audit + extend `_applicable_optional_fields` to
include the new variant. File this entry so the cross-reference exists.

**Constraints:** Evidence-gated; do NOT pre-emptively widen the branch
beyond actual signal_types.

**Acceptance:** When a new tg_* signal_type lands, the applicable-fields
function is updated in the SAME PR.

### BL-NEW-ACTIONABILITY-CANDIDATES-CROSS-CHAIN-FIRST-SEEN: cross-chain rediscovery
**Status:** PROPOSED 2026-05-20.
**Tag:** `data-integrity` `candidates` `actionability`
**Why:** `_read_first_seen_at` at `entry_snapshot.py:128` filters by
`(contract_address, chain)`. For tokens later re-indexed under a different
chain label (e.g., a Solana token rediscovered as `chain="coingecko"`), the
query returns None even when an earlier candidates row exists under the
original chain. Surfaced as Vector B Minor #1 against PR #200.

**Scope:** Decide policy — match-by-contract-only (collapse chain
distinctions for `first_seen_at`) vs match-by-(contract, chain) (current).
Coordinate with BL-NEW-ACTIONABILITY-CANDIDATES-FIRST-SEEN-PRESERVE — both
modify the same read pattern.

**Acceptance:** Documented policy + matching read query in
`entry_snapshot.py`.

### BL-NEW-DASHBOARD-ENTRY-SNAPSHOT-DRAWER: surface *_at_entry in TradeDetailDrawer
**Status:** PROPOSED 2026-05-20.
**Tag:** `dashboard` `actionability` `entry-snapshot`
**Why:** PR #200 ships the substrate (`paper_trade_entry_snapshots` sidecar)
but no dashboard surface consumes it yet. The TradeDetailDrawer (PR #195)
already has a "Source / confluence" group — extending it to show
`*_at_entry` fields when present, and "pre-cutover (no snapshot)" when
absent, is the minimum-viable read.

**Scope:**
- `dashboard/db.py:get_open_positions` adds
  `LEFT JOIN paper_trade_entry_snapshots s ON s.paper_trade_id = pt.id`
  (NOT `USING (paper_trade_id)` — `paper_trades` column is `id`, not
  `paper_trade_id`).
- Drawer rendering of new fields + pre-cutover label.
- Dist rebuild + commit per
  `feedback_vite_dist_index_html_commit_discipline.md`.

**Constraints:** Visibility-only; no behavior changes.

**Acceptance:** Drawer shows snapshot values for post-cutover trades and a
clear "pre-cutover" subtitle for older trades. Playwright smoke verifies
no console errors + correct labeling.

---

## Follow-ups filed 2026-05-19 from trader-cockpit overnight assignment (#194 / #195)

Six entries surfaced during the dashboard cockpit overnight assignment. **All file-only, no implementation.** Operator scope: file for visibility / future scheduling; implementation requires separate approval. Pair with PR #194 (Trader Action Queue) + #195 (Trade Detail Drawer) which already covered the cheap drilldown surface.

### BL-NEW-DASHBOARD-TG-SOURCE-QUALITY: per-TG-channel leaderboard
**Status:** PARKED-PENDING-SOURCE-CALL-PRICE-COVERAGE 2026-05-22 - original leaderboard framing is premature. Fold future work into `BL-NEW-SIGNAL-TRUST-ROADMAP` / source-call rankability after price coverage improves.
**Tag:** `dashboard` `tg_social` `leaderboard` `read-only` `evidence-gated`
**Why:** Operator wants to answer "is this TG channel trustworthy?" Existing data spans `tg_social_channels`, `tg_social_messages`, `tg_social_signals` (which already has `paper_trade_id` FK), and `paper_trades`. A per-channel leaderboard reading from these tables would surface: messages, resolved cashtag/CA, dispatched trades, closed PnL, win rate, unresolved/spam rate. **Read-only aggregation** — no new schema or trading behavior.
**Action:** ~3-5h. Add `GET /api/tg_social/channel_leaderboard?since=...` returning per-channel rollup. Render in TG tab or a new TG Source Quality panel. No new schema.
**Decision-by:** only after source-call coverage becomes rankable; until then TG remains context-only.

### BL-NEW-DASHBOARD-X-SOURCE-QUALITY: per-X-KOL leaderboard
**Status:** PARKED-PENDING-SOURCE-CALL-PRICE-COVERAGE 2026-05-22 - original leaderboard framing is premature. Fold future work into `BL-NEW-SIGNAL-TRUST-ROADMAP` / source-call rankability after price coverage improves.
**Tag:** `dashboard` `x_alerts` `leaderboard` `read-only` `evidence-gated`
**Why:** Same shape for X handles. Data: `narrative_alerts_inbound` already has `tweet_author`. The outcome side is `paper_trades` but linkage is missing until `BL-NEW-X-OUTCOME-LINKAGE` / PR #184 ships. **Without linkage, the leaderboard can only show ingestion-side stats**: alerts emitted per handle, priced-vs-unresolved rate, duplicate-shill count. PnL per handle requires the linkage; show "linkage-pending" honestly.
**Action:** ~3-5h. Add `GET /api/x_alerts/handle_leaderboard?since=...` returning per-`tweet_author` rollup. Render in X Alerts tab as a separate panel. **Linkage-dependent fields rendered as honest placeholders until PR #184 lands.**
**Decision-by:** only after source-call coverage becomes rankable; until then X remains context-only.

### BL-NEW-DASHBOARD-TOKEN-DEDUPE-CONFLUENCE-VIEW: unified token drawer/page
**Status:** FOLDED-INTO-LIVE-DECISION-COCKPIT 2026-05-22 - keep the need, but build it as `BL-NEW-PER-TOKEN-EVIDENCE-BUNDLE` inside `BL-NEW-LIVE-DECISION-COCKPIT`, not as a separate token page first.
**Tag:** `dashboard` `token-drawer` `read-only` `confluence` `evidence-gated`
**Why:** Clicking a token symbol should land on a page showing everything known about it: signals fired, trading state, TG mentions, X mentions, pipeline appearances, price-freshness, and any cohort/actionability stamps. Today the trader has to bounce between Signals / Trading / TG / X tabs to assemble this picture. A unified token drawer/page would compose existing endpoints client-side (or a single new aggregating endpoint).
**Action:** ~6-10h. Two paths:
  - (a) Frontend-only: `/token/<coin_id>` route that fans out to existing endpoints (`/api/candidates`, `/api/signals/quality`, `/api/trading/positions` filtered, `/api/x_alerts` filtered, `/api/tg_social/alerts` filtered, `/api/gainers/comparisons` filtered) and composes a unified panel.
  - (b) Backend-aggregating: single `GET /api/token/<coin_id>/everything` returning one composed payload. Faster page-load but locks the shape.
Recommend (a) first; (b) only if (a)'s 6-fetch fan-out is too slow.
**Decision-by:** live decision cockpit plan/design.

### BL-NEW-DASHBOARD-PEAK-GIVEBACK-RISK-BADGE: visual badge for stale-momentum tokens
**Status:** FOLDED-INTO-ENTRY-QUALITY 2026-05-22 - keep the risk, but surface it through `BL-NEW-ENTRY-QUALITY-STATE` / live candidate risk badges rather than as a separate dashboard badge project.
**Tag:** `dashboard` `risk-badge` `peak-giveback` `read-only` `evidence-gated`
**Why:** Audit findings from #183 (still draft, actionability-runbook-gated) identify `pre_entry_peak_gain_pct >= 40%` AND `pre_entry_giveback_ratio >= 0.50` as a candidate stale-entry V2 gate. Even as visibility-only (NOT as a suppression), surfacing this as a badge on Signals / Top Gainers / Pipeline rows would let the trader avoid stale-momentum tokens manually. The labels should be **risk indicators only** — not behavior-altering filters.
**Action:** ~3-4h. Read-only: derive `pre_entry_peak_gain_pct` from existing snapshot history (gainers_snapshots, volume_history_cg, etc.) at row-fetch time; attach as field. Render as badge: "FRESH" (no giveback signal) / "STALE PEAK ⚠" (≥40% / 50% giveback) / "MID-CYCLE" (between). NO behavior change; pure presentation. **Gated on PR #183's audit findings landing or being accepted as a risk-labeling source.**
**Decision-by:** live decision cockpit entry-quality design plus #183 actionability evidence.

### BL-NEW-DASHBOARD-WHAT-CHANGED-SINCE-LAST-VISIT: session-aware "what's new" panel
**Status:** PROPOSED 2026-05-19 — surfaced from the operator's "What changed since I last looked?" cockpit question.
**Tag:** `dashboard` `delta-view` `session-state` `read-only` `evidence-gated`
**Why:** Trader returns to the dashboard hours later. The cockpit should surface: new actionable trades since last visit, newly closed trades, biggest PnL swings, new TG/X mentions on currently-open positions, health regressions that affect trading. Without this, the trader has to scan everything to find what's new.
**Action:** ~4-6h. Frontend-only initially:
  1. Local `lastVisitTs` stored in `localStorage` on page unload.
  2. On page load, render a "Since X ago" panel summarizing diffs computed client-side against the existing endpoints.
  3. Server-side variant possible later if client-side reconstruction is too slow.
No schema or backend change for the MVP.
**Decision-by:** evidence-gated.

### BL-NEW-DASHBOARD-HEALTH-TRADER-IMPACT: convert health signals into trading consequences
**Status:** FOLDED-INTO-LIVE-DECISION-COCKPIT 2026-05-22 - remaining value should become candidate refusal/risk reasons and dashboard health captions, not a standalone health project unless cockpit design proves it needs a dedicated surface.
**Tag:** `dashboard` `health` `trader-impact` `read-only` `evidence-gated`
**Why:** Today's Health tab shows freshness rows for ~15 tables — but the trader has to translate "category_snapshots last 12h ago" into "category heating is stale, narrative_prediction trades opened in the last 12h may have used pre-stale categorization." A second view should translate raw freshness rows into trading-impact statements:
  - price_cache stale → trailing stops cannot trigger on stale-priced positions
  - X resolver stale → X-derived outcome links may regress
  - TG listener stale → TG-social signal_type intake may regress
  - MiroFish cap exhausted → narrative ranking may be partially uncached
  - CG rate-limit elevated → price/actionability validation latency rises
**Action:** ~4-6h. Add a "Trading Impact" panel to the Health tab. Read existing `/api/system/health` + add small per-table impact-mapping table on the frontend. **No new endpoint required for the MVP.**
**Decision-by:** live decision cockpit plan/design.

---

## Follow-ups filed 2026-05-19 from dashboard work (#189 / #190 + overnight triage)

Four entries surfaced during PR #190's X Alerts perf investigation and the overnight dashboard triage pass. **All file-only, no implementation.** Operator scope: file for visibility / future scheduling; do not start implementation until separately approved.

### BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-SCHEMA-ALIGN: reconcile `_resolve_coin_id_for_outcome` against the actual `candidates` schema
**Status:** STALE-PENDING-DRIFT-CHECK 2026-05-22 - `/api/x_alerts?limit=80` is healthy after PR #213/#215 (~0.18-0.20s). Do not implement from the old diagnosis without first verifying current journal/runtime evidence still shows this schema path failing.
**Tag:** `dashboard` `x_alerts` `schema-drift` `silent-degradation`
**Why:** `dashboard/db.py:get_x_alerts._resolve_coin_id_for_outcome` runs `SELECT DISTINCT coingecko_id AS coin_id FROM candidates WHERE LOWER(contract_address) = LOWER(?) AND chain = ? AND COALESCE(coingecko_id, '') != ''` for every row whose contract address was extracted. Verified prod schema (`PRAGMA table_info(candidates)`): no `coingecko_id` column exists; the column-list ends at `quote_symbol`. The query raises `OperationalError`, caught silently by `_safe_fetchall` and returned as `[]`. **Every X alert with a contract falls through to the slower symbol-table scan path.** The original intent of the contract-match path is bypassed in 100% of cases.
**Action:** ~2-4h. Decide between (a) DROP the `coingecko_id` reference from the resolver query (acknowledge that `candidates` no longer carries a CoinGecko ID mapping; rely on symbol-fallback only — affects per-request perf if symbol path is also slow) OR (b) ADD a `coingecko_id` column to `candidates` schema + backfill from existing ingest paths (schema migration). Recommend (a) for scope conservatism; (b) only if the contract → coingecko_id mapping is needed elsewhere.
**Decision-by:** close if next drift-check finds no live journal failures; reopen only on fresh runtime evidence.

### BL-NEW-DASHBOARD-X-ALERTS-SYMBOL-INDEX: add `(symbol)` indexes to the 4 symbol-fallback source tables
**Status:** SUPERSEDED 2026-05-22 - functional `UPPER(symbol)` indexes shipped via PR #215 and prod timing improved to ~0.18-0.20s for `/api/x_alerts?limit=80`. Keep this historical row only as context.
**Tag:** `dashboard` `x_alerts` `performance` `schema-migration`
**Why:** When the contract-match path falls through (per the RESOLVER-SCHEMA-ALIGN entry's observation: 100% of the time today), `_resolve_coin_id_for_outcome` queries 4 source tables (`gainers_snapshots`, `volume_history_cg`, `volume_spikes`, `momentum_7d`) with `WHERE UPPER(symbol) = ? AND COALESCE(coin_id, '') != '' ORDER BY time_col DESC LIMIT 25`. None of these tables have an index on `symbol` — every query is a full-table scan. With PR #190's per-symbol cache the cost is unique-symbols × 4 scans, not per-row × 4. But under concurrent dashboard load, 4 full-table scans × 15-30 unique symbols still pushes the endpoint past the 12s frontend timeout. ASGI smoke in isolation: limit=30 = ~9s (acceptable); under live dashboard load: 13-30s+ (timeout).
**Action:** ~1-2h schema migration. Add `idx_<table>_symbol(UPPER(symbol))` to all 4 tables (or just `(symbol)` if SQLite's expression-index requirement is lifted by the writer normalizing case). Verify each writer site upcases consistently. Indexed scan should drop the symbol-path cost ~10x. **Schema migration — gated on operator approval.** Pairs naturally with RESOLVER-SCHEMA-ALIGN; ship together or in sequence.
**Decision-by:** none; superseded by PR #215 unless a fresh regression appears.

### BL-NEW-DASHBOARD-TG-CONVERSION-FUNNEL-ENDPOINT: read-only TG conversion-funnel aggregation endpoint
**Status:** PROPOSED 2026-05-19 — surfaced from overnight dashboard triage; assignment item 7. Filed as a backend instrumentation task, NOT a UI fix.
**Tag:** `dashboard` `tg_social` `funnel` `observability`
**Why:** Operator wants a TG conversion funnel visible in the dashboard: `messages → CA/cashtag → resolved → trade dispatched → linked outcome`. Existing source data is spread across `tg_social_messages` (raw ingest), `tg_social_signals` (resolved cashtag/CA + `paper_trade_id` FK), and `paper_trades` (outcome). A simple JOIN can produce per-window counts, but no endpoint surfaces it today. The TG/X linkage design in PR #184 covers schema-shape changes; this entry is the **read-only aggregation endpoint that consumes existing schema and feeds a UI panel**, distinct from #184 scope.
**Action:** ~4-6h. Add `GET /api/tg_social/funnel?since=...&channel=...` to `dashboard/api.py` returning `{window_start, window_end, messages_total, with_cashtag, with_ca, resolved, dispatched, linked_outcome_open, linked_outcome_closed, closed_actionable, closed_exploratory}`. Single composite query against the 3 tables. Read-only. Add a Signals or TG tab panel rendering the funnel stages with row counts + drop-off percentages. No new schema.
**Decision-by:** evidence-gated on operator wanting TG funnel visibility prioritized over other dashboard work. Pairs with PR #184 outcome — if TG linkage gets implementation approval, this should ship in the same cycle.

### BL-NEW-DASHBOARD-PIPELINE-GATE-BLOCKER-COUNTS: per-gate blocker telemetry + pipeline tab explanations
**Status:** PROPOSED 2026-05-19 — surfaced from overnight dashboard triage; assignment item 9. Filed as a backend instrumentation task, NOT a UI fix.
**Tag:** `dashboard` `pipeline` `observability` `gate-blockers`
**Why:** Operator wants the Pipeline tab to show blocker/explanation counts: below score, safety blocked, MiroFish cap, alert gate, rate-limit/cooldown, etc. Existing `/api/funnel/latest` returns per-stage row counts but no per-gate blocker attribution. The actual gate-blocking sites in `scout/main.py`, `scout/gate.py`, `scout/safety.py`, `scout/mirofish/`, etc. emit structlog events on block but do NOT persist a counter that the dashboard can read. Without backend instrumentation, the dashboard can only show "blocker counts not available" honestly — which is the right empty-state per assignment scope.
**Action:** ~6-10h. Two-part work:
  1. **Backend instrumentation:** add a `pipeline_gate_blocks` table (or extend `signal_events`) capturing `(gate_name, candidate_id, blocked_at, reason)` at each block site. Persist per-cycle aggregates to a `pipeline_gate_block_counters` rollup table so the dashboard reads a single small table.
  2. **Dashboard endpoint:** `GET /api/pipeline/gate_blockers?since=...` reading the rollup. Pipeline tab renders per-gate bar with block count + percentage of candidates blocked.
Schema migration — gated on operator approval. Honest fallback today: explicit "Per-gate blocker counts not available — pending BL-NEW-DASHBOARD-PIPELINE-GATE-BLOCKER-COUNTS" empty-state in the Pipeline tab.
**Decision-by:** evidence-gated on operator wanting pipeline-attribution visibility prioritized. Schema migration scope; ship in a dedicated cycle.

## Follow-ups filed 2026-05-16 from BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING PR

These four entries were surfaced during the score/volume pruning PR's plan/design review cycle (V1+V2 plan, V3+V4 design). Filed per actionability discipline.

### BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION: parameterize + decouple remaining 6 narrative-owned prunes
**Status:** SHIPPED 2026-05-16 — PR #138 merged `c4d0859`. All 6 tables (`volume_spikes`, `momentum_7d`, `trending_snapshots`, `learn_logs`, `chain_matches`, `holder_snapshots`) parameterized via Settings + hourly-pruned via `scout.main._run_hourly_maintenance`. Narrative loop helper `_run_extra_table_prune` deleted; daily-learn block no longer prunes tables directly. 5 new indexes (`idx_*_detected_at|snapshot_at|created_at|scanned_at`) added via cycle 1's extended `_migrate_scanned_at_index` helper (`column` kwarg + dynamic log events). `_validate_backtest_cli_retention_floor` model_validator added enforcing 30d floor on trending/chain/volume (V8 fold). chain_matches index deferred (V9 NICE-TO-HAVE — slow growth, EXPLAIN-gate at PR-stage).

**Conditional follow-up identified, NOT filed as a separate backlog entry:** `BL-NEW-PRUNE-PACING-FOLLOWUP` was surfaced during the D9 design fold (11 prunes/hour WAL pressure check post-deploy). It is evidence-gated: only worth filing as a real entry if post-deploy observation surfaces actual WAL contention. No standalone `BL-NEW-PRUNE-PACING-FOLLOWUP` entry exists below; treat the placeholder reference here as a conditional follow-up that has not been filed.

**Original status (now historical):** PROPOSED 2026-05-16 — residual from `feat/score-volume-pruning-harden` PR's §7a partial-match reframe.

### BL-NEW-CRON-DRIFT-WATCHDOG-ENV-WHITESPACE-TOLERANCE: backport .env leading-whitespace tolerance into cron-drift-watchdog.sh
**Status:** SHIPPED 2026-05-18 — PR #161 merged `01efcbd`; sibling-symmetry follow-up surfaced by PR #159 R1 IMPORTANT review. See detailed shipped row below for verification notes.
**Tag:** `cron-drift-watchdog` `env-parsing` `backport` `sibling-symmetry` `silent-failure-prevention`
**Why:** PR #159 (cycle 14) added `^[[:space:]]*` tolerance + sed-strip to `scripts/systemd-drift-watchdog.sh` so an indented `TELEGRAM_BOT_TOKEN=` in `.env` doesn't trigger silent exit-5 false negative. The sibling `scripts/cron-drift-watchdog.sh:197-198` (shipped in PR #156) still uses strict `^TELEGRAM_BOT_TOKEN=` regex. Inverts source-of-truth assumption (cron-drift was supposed to be the canonical pattern; systemd PR adopted a SUPERSET).
**Action:** ~1h. Edit `scripts/cron-drift-watchdog.sh:197-198` to use same regex+sed pattern as systemd PR #159. Mirror the `test_leading_whitespace_in_env_parsed_correctly` test from PR #159 into `tests/test_cron_drift_watchdog.py`. Single-commit PR.
**Decision-by:** within 2 weeks of PR #156 merge (operator can also choose to delay until next cycle's watchdog work).

### BL-NEW-PARSE-MODE-AUDIT-EXTEND-URLLIB-DISPATCH: extend AST sweep to urllib.request dispatch sites
**Status:** SHIPPED 2026-05-18 — PR #162 merged `54da462`; surfaced by PR #160 R2 MINOR-1 review.
**Tag:** `parse-mode-hygiene` `silent-failure-prevention` `class-3` `ast-sweep` `coverage`
**Hermes-first analysis:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram/messaging delivery | yes — Hermes messaging gateway docs (`https://hermes-agent.nousresearch.com/docs/user-guide/messaging`) | not a replacement; this is an in-repo AST regression harness for gecko-alpha source |
| Scheduled/script alerting | yes — Hermes Cron/no-agent jobs (`https://hermes-agent.nousresearch.com/docs/guides/cron-script-only`) | not applicable; no scheduling primitive replaces Python source auditing |
| Python Telegram parse-mode static audit | none found | extend existing `tests/test_parse_mode_hygiene.py` scanner |

awesome-hermes-agent ecosystem check: reviewed `https://github.com/0xNyk/awesome-hermes-agent`; no listed skill/plugin provides project-local AST enforcement for Telegram `parse_mode`. Verdict: custom test-harness extension is justified.
**Why:** `tests/test_parse_mode_hygiene.py:213` walks `scout/` for direct `.post(.../sendMessage)` calls and resolves payload dicts to assert `parse_mode` is absent or escape-coverage is present. PR #160's new `scout/config_alert.py` uses `urllib.request.urlopen(req)` — NOT `.post()` — so the sweep does NOT cover it. The unit-test assertion at `test_config_alert.py:78` is sufficient for current scope, but future urllib-direct dispatch sites would be uncovered.
**Action:** ~2h. Extend the AST sweep to also walk `urllib.request.urlopen(Request(...sendMessage...))` patterns. Resolve the Request data argument back to a json.dumps dict literal where possible; assert `parse_mode` absent or escape-coverage present, same as the existing `.post` arm.
**Decision-by:** within 4 weeks (low urgency — current sweep covers 95% of dispatch sites).

### BL-NEW-SETTINGS-VALIDATION-ALERT: curl-direct Telegram on settings_validation_failed
**Status:** SHIPPED 2026-05-18 — PR #160 merged `788059a`. New `scout/config_alert.py` module wired into `scout/config.py:load_settings()`. urllib.request (stdlib only) curl-direct alert; SHA256 content-hash dedup via state file `/var/lib/gecko-alpha/settings-validation-watchdog/last_alerted_hash`; 3s timeout (avoids doubling systemd crash-loop period); plain text / no parse_mode (§12b). 18/18 tests pass on srilu Python 3.12.3.
**Original status (now historical):** PROPOSED 2026-05-16 — V4#1 review deferred from `feat/score-volume-pruning-harden` PR.
**Why:** `load_settings()` in `scout/config.py` emits structured `logger.error("settings_validation_failed", ...)` before re-raising ValidationError. systemd `Restart=always`+`RestartSec=10` (verified on srilu) means a bad `.env` triggers a 10s crash-loop visible in journalctl but with no active Telegram push. Curl-direct push (mirroring `gecko-backup-watchdog` from memory `project_vps_backup_rotation_2026_05_09.md`) requires content-hash dedup to avoid 360 msg/hr storm.
**Post-merge action:** completed 2026-05-18 via PR #160 squash merge `788059a`.

### BL-NEW-SCORE-VOLUME-PRUNE-ALERT: §12b active alert on score/volume prune failure
**Status:** PROPOSED 2026-05-16 — V4#6 review deferred from `feat/score-volume-pruning-harden` PR.
**Why:** `_run_hourly_maintenance` logs `logger.exception("score_history_prune_failed")` (and volume) but does NOT actively push an alert. If pruning silently fails for 7+ days, the table grows unbounded and operator only sees it via `du -sh scout.db` spot check. Evidence-gated.
**Action:** §12b active-alert path (TG curl-direct OR `scout.alerter` with `parse_mode=None`) in the exception branches. Likely shipped together with `BL-NEW-SETTINGS-VALIDATION-ALERT` (same alert-infrastructure shape).
**decision-by:** evidence-gated on first prod failure (no calendar trigger).

### BL-NEW-SETTINGS-IMMUTABILITY: prevent post-construction Settings mutation bypassing validators
**Status:** AUDITED 2026-05-18 — PR #157 merged `be36bfb`; see `tasks/findings_settings_immutability_audit_2026_05_18.md`. **Recommendation: do NOT implement frozen=True at this time** — protection is for a hypothetical bug (no current code mutates the validator-relevant fields). Audit found 1 production mutation (`scout/main.py:1534` legitimate CLI override of MIN_SCORE; no validator interaction) + ~10 test mutations (`tests/test_main.py` + `tests/test_trading_engine.py` direct mutation; `tests/test_bl076_junk_filter_and_symbol_name.py` uses preferred `monkeypatch.setattr`). Refactor cost (10+ test sites + 1 production site + model_copy plumbing) > benefit (defense against hypothetical future bug). Follow-up `BL-NEW-SETTINGS-FROZEN-WHEN-CALL-FOR-IT` filed evidence-gated.

**Original status (now historical):** PROPOSED 2026-05-16 — V6 PR-review NOTE finding from `feat/score-volume-pruning-harden`. Pydantic v2 `Settings` is NOT frozen by default; `s.SCORE_HISTORY_RETENTION_DAYS = 5` post-construction silently bypasses validators. Audit confirmed no current mutation of that specific field.

### BL-NEW-SETTINGS-FROZEN-WHEN-CALL-FOR-IT: evidence-gated frozen=True re-evaluation
**Status:** PROPOSED 2026-05-18 — evidence-gated follow-up to BL-NEW-SETTINGS-IMMUTABILITY audit (see `tasks/findings_settings_immutability_audit_2026_05_18.md`).
**Tag:** `pydantic` `settings` `immutability` `evidence-gated`
**Why:** Audit found no current mutation path bypasses a validator. Re-evaluate when (a) a new Pydantic validator is added with a load-bearing invariant (live-caps / money flows / soak windows / schema migration prerequisites), OR (b) any production code adds `settings.X = value` post-construction mutation outside the existing `scout/main.py:1534` CLI-override precedent, OR (c) 2026-08-18 (90d calendar backstop).
**Action:** ~3-5h. Re-run audit; if a new validator-relevant mutation is found, implement `frozen=True` + refactor 1 production + 10 test sites via `monkeypatch.setattr` (tests) + `settings.model_copy(update=...)` (production overrides). Update `scout/config.py` module docstring with the convention.
**Decision-by:** 2026-08-18 (90d calendar backstop) OR earlier on trigger.

### BL-NEW-SYSTEMD-UNIT-IN-REPO: systemd units must be repo-tracked
**Status:** SHIPPED 2026-05-17 — branch `feat/systemd-units-in-repo` (cycle 6). Captured `gecko-pipeline.service` + `gecko-dashboard.service` verbatim from srilu `/etc/systemd/system/` into `systemd/`. `systemd/README.md` documents deploy workflow + drift-audit one-liner. No drop-in directories on srilu (`/etc/systemd/system/<unit>.service.d/` absent for both units; full capture is the 2 files). V34/V35 PR-review folds: wildcard `cp systemd/*.{service,timer}` (scales to future units), drop-in enumeration in audit one-liner, `systemctl edit` warning, restart blast-radius callout, reload-semantics clarification (long-running needs explicit restart; timers don't unless schedule changed).

**Original status (now historical):** PROPOSED 2026-05-16 — V4 NOTE finding from `feat/score-volume-pruning-harden` PR design review. `/etc/systemd/system/gecko-pipeline.service` and `gecko-dashboard.service` exist only on srilu-vps, not in `systemd/` directory of this repo. Substrate-finding shape — config-not-in-git is the same class that drove the 2026-05-16 backlog drift audit.

### BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK: prevent recurrence via automated drift detection
**Status:** SHIPPED 2026-05-17 — branch `feat/systemd-drift-precommit-hook` (cycle 10). Daily TG-alert watchdog via `scripts/systemd-drift-watchdog.sh` + `systemd/systemd-drift-watchdog.{service,timer}` (09:30 UTC fire, staggered after 09:00 gecko-backup-watchdog). Option (a) chosen over (b) pre-commit per backlog rationale: drift is operator-introduced on deploy host, not at commit time. Implementation per V47/V48-folded design: bi-directional enumeration (repo→prod drift + drop-ins; prod→repo UNTRACKED PROD UNIT for `gecko-*` / `minara-*` / `systemd-drift-watchdog.*`); sha256 ack-tombstone with pre-hash sort for filesystem-order independence; HTTP-failure path leaves ACK_FILE unwritten (intentional re-alert); flock + heartbeat-file on CLEAN. 13 tests with module-level `skipif win32`. Two follow-ups filed: BL-NEW-DRIFT-STALE-REMINDER (suppress-counter elevation if 180-day silent suppress matters in practice) + BL-NEW-WATCHDOG-META-WATCHDOG (the §12a daemon should monitor this watchdog's own freshness).

**Original status (now historical):** PROPOSED 2026-05-17 — V35 PR-review FOLLOW-UP from BL-NEW-SYSTEMD-UNIT-IN-REPO (PR #142). Action: Either (a) daily cron + Telegram alert on DRIFT, OR (b) pre-commit hook. (a) chosen because drift is operator-introduced on deploy host.

### BL-NEW-DRIFT-STALE-REMINDER: elevate long-running systemd-drift silent_suppress to operator-visible
**Status:** PROPOSED 2026-05-17 — V48 SHOULD-FIX FOLLOW-UP from BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK (cycle 10).
**Why:** When drift state is stable (e.g., one-time `systemctl edit` experiment the operator forgot to revert), cycle 10's ack-tombstone suppresses re-alerts. Designed to prevent alert fatigue. But: a 180-day silent_suppress is operator-invisible — DEBUG-level journalctl event only. Operator who set `--priority info` filter misses it entirely. Conditional on whether prolonged silent_suppress observed in practice.
**Action:** ~30min. Persist a counter alongside the hash in `last_alerted_hash` (2-line format: `hash\ncount`); at counter milestones (7d, 30d, 90d) emit a single soft TG reminder. Alternative: elevate `systemd_drift_silent_suppress_same_drift_set` event to INFO with `suppressed_count` field.
**Decision-by:** 8 weeks (evidence-gated — file if cycle-10 watchdog ever silent-suppresses for >14 consecutive days in practice).

### BL-NEW-WATCHDOG-META-WATCHDOG: monitor the watchdogs themselves
**Status:** PROPOSED 2026-05-17 — V46 SHOULD-FIX FOLLOW-UP from BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK (cycle 10).
**Why:** Cycle 10's drift watchdog exits 4-7 silently to journalctl when env-file / token / Telegram-API fails. No alert reaches the operator. Same applies to gecko-backup-watchdog, held-position-price-watchdog, minara-emission-persistence-watchdog. The §12a daemon proposal is the structural fix.
**Action:** Implement the §12a freshness-SLO daemon to monitor each watchdog's heartbeat file with an SLO (e.g., "systemd-drift-watchdog heartbeat updated within last 26h" — 24h cadence + 2h slack). Alert via separate TG dispatch on SLO breach. Out-of-scope for cycle 10.
**Pattern:** §12a generic daemon from `findings_silent_failure_audit_2026_05_11.md` closing notes.
**Decision-by:** 8 weeks (depends on §12a daemon implementation; can be deferred until the daemon exists).

### BL-NEW-OTHER-PROD-CONFIG-AUDIT: sweep srilu for other repo-untracked prod config
**Status:** SHIPPED-WITH-FINDINGS 2026-05-17 — branch `feat/other-prod-config-audit` (cycle 11). Findings doc: `tasks/findings_other_prod_config_audit_2026_05_17.md`. Of 17 categories swept, only 1 gap (gecko cron entries repo-untracked at schedule level) — closed via new `cron/` directory (sentinel-bracketed managed block + idempotent `cron/deploy.sh` per V54 fold). Apache "Possible Gap" withdrawn after drill (not installed). VPS multi-tenant inventory documented (gecko-alpha + polymarket-ml-signal + btc15minutebot + shift-agent). 4 follow-ups filed (cron drift watchdog, cron-to-timer decision, drift-watchdog archive, firewall decision 2026-06-14, polymarket-verify); 1 withdrawn (Apache). Memory checkpoint: `project_prod_config_audit_2026_05_17.md`.

### BL-NEW-CRON-DRIFT-WATCHDOG: bash watchdog for crontab drift (cycle 11 follow-up)
**Status:** SCRIPT-SHIPPED / SCHEDULING-PENDING-OPERATOR 2026-05-18 — PR #156 merged `7f9aee6`. **`scripts/cron-drift-watchdog.sh` script is on master; the cron schedule entry is NOT added to `cron/gecko-alpha.crontab` in this PR** (per "do not change live config without explicit operator approval" — adding to the managed-block fragment would auto-fire daily after the next `bash cron/deploy.sh`). Operator chooses when to schedule per `cron/README.md` §"Setup (one-time, opt-in to scheduled firing)". Runtime protection is GATED on operator scheduling action. Script verified via prod-crontab dry-run CLEAN + 20/20 tests on srilu. 2-reviewer plan-fold + 3-reviewer PR-fold + 1-reviewer post-review fold complete. Filed 3 follow-ups (HEARTBEAT-MONITOR + WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT scoped to systemd-watchdog only, since cron-watchdog already has the mktemp + max-time + ACK_DIR-exit-9 fixes + ENV-WHITESPACE-TOLERANCE parity with PR #159's systemd-watchdog follow-up).

**Original status (now historical):** PROPOSED 2026-05-17 — cycle 11 follow-up to BL-NEW-OTHER-PROD-CONFIG-AUDIT.

**Remaining operator action:** after the operator's separate scheduling-opt-in action (adding the cron line to `cron/gecko-alpha.crontab` and running `bash cron/deploy.sh`), flip once more to `SHIPPED / SCHEDULED <date>`. Three-stage convention reflects the two independent gates: PR merge AND operator scheduling.

### BL-NEW-CRON-DRIFT-WATCHDOG-HEARTBEAT-MONITOR: wire stale-heartbeat detector for cron-drift-watchdog
**Status:** PROPOSED 2026-05-18 — PR-stage R2 #13 fold from BL-NEW-CRON-DRIFT-WATCHDOG. CLAUDE.md §12a compliance: shipping a new heartbeat-writing watchdog without a stale-detector is the silent-failure surface §12a exists to prevent.
**Tag:** `observability` `watchdog` `silent-failure-prevention`
**Why:** `scripts/cron-drift-watchdog.sh` writes `/var/lib/gecko-alpha/cron-drift-watchdog/heartbeat` on CLEAN runs but no separate monitor checks the heartbeat's freshness. If the watchdog itself stops running (cron line removed, script broken, etc.), the operator has no signal.
**Action:** ~1h. Extend existing `scripts/gecko-backup-watchdog.sh` (or create `scripts/cron-drift-stale-heartbeat-watchdog.sh` modeled on it) to alert when the cron-drift-watchdog heartbeat is older than N hours (default 25h to cover a daily cron firing 1-hour-late). Add to cron managed block in `cron/gecko-alpha.crontab`. Until shipped, operator runs the one-liner in `cron/README.md` §"Heartbeat freshness check".
**Decision-by:** 2026-06-15 (4 weeks from PR #156 merge).

### BL-NEW-CRON-DRIFT-WATCHDOG-ENV-WHITESPACE-TOLERANCE: tolerate indented Telegram keys in cron-drift-watchdog `.env`
**Status:** SHIPPED 2026-05-18 — PR #161 merged `01efcbd`; stacked follow-up to PR #156.
**Tag:** `watchdog` `env-parsing` `parity-hardening`
**Drift-check:** no existing BL entry or implementation matched this exact parity gap. PR #159's `scripts/systemd-drift-watchdog.sh` tolerates leading whitespace in `.env` credential lines via `^[[:space:]]*TELEGRAM_*=`; PR #156's `scripts/cron-drift-watchdog.sh` still uses strict `^TELEGRAM_*=` and can fail before alert delivery if an operator indents the key.
**Hermes-first analysis:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Scheduled watchdog execution | yes — Hermes Cron / no-agent script jobs (`https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/`) | not a replacement; scheduling primitive does not patch gecko-alpha's in-repo watchdog parser |
| Polling/watchers | yes — devops/watchers (`https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/devops/devops-watchers`) | not applicable; covers RSS/JSON/GitHub watermark polling, not local crontab drift or `.env` credential extraction |
| Crontab drift + Telegram credential parsing | none found | build the 2-line in-tree parity fix with regression coverage |

awesome-hermes-agent ecosystem check: reviewed `https://github.com/0xNyk/awesome-hermes-agent`; no listed skill/plugin replaces gecko-alpha's local crontab diff watchdog or its `.env` parsing. Verdict: custom fix is justified and minimal.
**Why:** This is the cron-side sibling of PR #159's systemd-watchdog false-negative fix. A stray indent before `TELEGRAM_BOT_TOKEN=` or `TELEGRAM_CHAT_ID=` should not suppress alert delivery.
**Action:** replace strict `grep -E '^TELEGRAM_*=' ... cut -d= -f2-` parsing with a small `sed` extractor that tolerates leading whitespace and does not trip `set -euo pipefail` before the documented exit-5 branch. Add prod-path tests with a curl stub proving indented keys deliver and missing keys exit 5.

### BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT: backport mktemp + curl --max-time + ACK_DIR-exit fixes to systemd-watchdog
**Status:** SHIPPED 2026-05-18 — PR #159 merged `63aeef0`. Backported PR #156 hardening to `scripts/systemd-drift-watchdog.sh`: `mktemp` response file, `curl --max-time 30`, ACK_DIR mkdir failure exits 9, and leading-whitespace `.env` parsing tolerance.
**Tag:** `security-hardening` `watchdog` `tech-debt`
**Why:** PR #156 (cron-drift-watchdog) fixed three latent issues that ALSO exist in `scripts/systemd-drift-watchdog.sh`: (a) `/tmp/.gecko-drift-resp.$$` is a predictable PID-based tmp path — symlink-attack surface; (b) `curl` without `--max-time` can hold the flock indefinitely on hung network; (c) `mkdir -p $ACK_DIR` failure only warns then `exec 9>$LOCK_FILE` fails abruptly under set -e. Same fixes apply.
**Action:** ~45min. Apply to `scripts/systemd-drift-watchdog.sh`: (1) `mktemp -t gecko-systemd-drift-resp.XXXXXX`, (2) `curl --max-time 30`, (3) `exit 9` on ACK_DIR mkdir failure (vs current "warn-then-die-cryptically"). Add 3 regression-style tests. Optionally: (d) `.env` leading-whitespace token-grep tolerance.
**Decision-by:** 2026-06-15 (4 weeks from PR #156 merge).

### BL-NEW-CRON-TO-SYSTEMD-TIMER: convert 2 weekly cron entries to systemd timers (cycle 11 follow-up)
**Status:** PROPOSED 2026-05-17 — cycle 11 design-tension follow-up (V53 fold).
**Why:** Cycle 10 canonicalized the systemd-timer pattern (drift-watchdog runs as timer). Cycle 11 ships 2 weekly cron entries (`tg_burst_archive.sh` Sun 03:30, `wal_archive.sh` Sun 03:45). Inconsistency: should these be systemd timers too?
**Expected disposition (V55 SHOULD-FIX):** likely close as no-op. Cron is simpler for weekly schedules; cycle-10's systemd-timer canon applies more naturally to high-cadence triggers. Filing only to document the tension was considered.
**Action:** ~30min decision + (if convert) ~2h build. Decision criteria: does any operational need require systemd-timer features (RandomizedDelaySec, Conditions, OnUnitActiveSec)? If yes, convert. If no, close.
**decision-by:** 2026-06-14.

### BL-NEW-DRIFT-WATCHDOG-ARCHIVE: extend wal_archive.sh shape for systemd-drift-watchdog events (cycle 11 follow-up)
**Status:** PROPOSED 2026-05-17 — cycle 11 follow-up to BL-NEW-OTHER-PROD-CONFIG-AUDIT (V53 fold).
**Why:** Cycle 10's `systemd-drift-watchdog.sh` emits plain-text `echo` lines (`OK: 0 drifts...`, `ALERTED: HTTP 200; hash=...`), not structured `"event":` fields. Cycles 3+4 archive scripts (`tg_burst_archive.sh`, `wal_archive.sh`) grep for `"event":` JSON, so they DON'T capture drift-watchdog output. Default journald retention may drop it. Operationally needed only if drift-watchdog history matters for post-incident review.
**Action:** ~1h. Mirror `wal_archive.sh` shape: weekly cron, filename-date rotation, 8-week retention. Grep filter is `systemctl status systemd-drift-watchdog.service` OR `grep "systemd-drift-watchdog.sh"` (the echo output is unstructured plain text).
**decision-by:** evidence-gated — file only if drift-watchdog history becomes operationally needed (e.g., investigating a past drift event that's been pruned).

### BL-NEW-FIREWALL-DECISION: operator review of srilu ACCEPT-policy at 2026-06-14 (cycle 11 follow-up)
**Status:** PROPOSED 2026-05-17 — cycle 11 follow-up; pre-registered review.
**Why:** Cycle 11 audit found UFW inactive + iptables INPUT policy ACCEPT (no rules). Security-posture choice; documented unchanged. Pre-registered review at 2026-06-14 per CLAUDE.md §11 data-bound-not-calendar-bound discipline.
**Kill-criterion (locked):** if srilu remains single-tenant-by-app AND no inbound attack surface change observed, accept ACCEPT-policy and close. Otherwise: file `BL-NEW-FIREWALL-ENABLE` to switch on UFW with allowlist (ssh + port 8000 for dashboard).
**Verification command:**
```bash
ssh root@srilu-vps 'crontab -l | grep -v "/root/gecko-alpha"; ls /opt/ 2>&1; ls /etc/logrotate.d/; ss -tlnp'
```
Expected: known 4-app tenant set unchanged; no unexpected listening ports.
**decision-by:** 2026-06-14.

### BL-NEW-AUDIT-SURFACE-ADDENDUM: 5-category mini-sweep next cycle (cycle 11 follow-up)
**Status:** AUDITED 2026-05-18 — PR #155 merged `00db6ee`; see `tasks/findings_audit_surface_addendum_2026_05_18.md`. All 5 categories clean: nginx/caddy not-found, /etc/systemd/system.conf only `[Manager]`, /etc/apt/sources.list.d/ minimal (nodesource + ubuntu), docker/containerd not-found, systemd inventory matches captured cycle-6 units. No follow-ups filed.

**Original status (now historical):** PROPOSED 2026-05-17 — cycle 11 V58 PR-review FOLLOW-UP from BL-NEW-OTHER-PROD-CONFIG-AUDIT. V58 surfaced 5 additional surfaces (nginx/caddy explicit probe, `/etc/systemd/system.conf`, `/etc/apt/sources.list.d/`, docker/containerd, complete systemd unit inventory) that are operationally meaningful but not in the original backlog scope.

**Re-run** (when srilu infrastructure shifts):
```bash
ssh root@srilu-vps '
systemctl is-enabled nginx caddy 2>&1
grep -v "^#\|^$" /etc/systemd/system.conf
ls /etc/apt/sources.list.d/
systemctl is-enabled docker containerd 2>&1
systemctl list-units --type=service --all | grep -v "@\.service$" | head -40
'
```

### BL-NEW-POLYMARKET-VERIFY: operator confirms polymarket-ml-signal path validity (cycle 11 follow-up)
**Status:** AUDITED 2026-05-18 — PR #155 merged `00db6ee`; see `tasks/findings_polymarket_verify_2026_05_18.md`. Path `/opt/polymarket-ml-signal/` does NOT exist; `/opt/` empty. Stale cron line confirmed (outside gecko-alpha managed block, silently failing every 6h). Findings doc emits operator-pastable removal command + safety bounds. Cycle-11 V52+V53 hypothesis (b) "sweep redirect collapsed output" eliminated; (a) "polymarket dir was deleted" or never-installed confirmed.

**Original status (now historical):** PROPOSED 2026-05-17 — cycle 11 follow-up (V52 + V53 fold). Cycle 11 sweep showed crontab references `/opt/polymarket-ml-signal/scripts/extract_data.sh` every 6h, but `ls /opt/` returned empty.

**Operator action (one-line, scoped, reversible):**
```bash
ssh srilu-vps "(crontab -l | grep -v '/opt/polymarket-ml-signal/') | crontab -"
```

### BL-NEW-CHAIN-COMPLETED-SILENCE-AUDIT: investigate 10+ day chain_completed silence
**Status:** SHIPPED-WITH-FINDING 2026-05-17 — branch `feat/chain-completed-silence-audit` (cycle 8, audit-only). Findings doc: `tasks/findings_chain_completed_silence_2026_05_17.md`. **Confirmed regression:** chain_matches narrative pipeline silent 5.5d (last 2026-05-11T16:43Z); memecoin pipeline silent 13d (last 2026-05-04T00:51Z). active_chains MAX(anchor_time)=2026-05-11T16:42Z (no new anchors); signal_params enabled=1 (NOT auto-suspended); code path unchanged in May. Mechanism per §9c: visible levers (enabled=1, code unchanged) ≠ controlling lever (something stopped creating new active_chains rows). Surfaced as HIGH-priority fix item `BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX`. Second chain-pipeline silence in ~6 weeks (prior: 2026-04-14→2026-05-01, 17d, fixed PR #60/#61); same substrate class.

**Original status (now historical):** PROPOSED 2026-05-17 — Finding 1 from BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT (cycle 7).

### BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX: restore chain-anchor matching (regression inside `_check_active_chains`)
**Status:** SHIPPED 2026-05-17 — PR #146 (`5860d17`, merged 2026-05-17T16:50:40Z) restored the chain-anchor pipeline; post-deploy verification 2026-05-18 ~21:10Z (28h+ after merge) confirms recovery: `active_chains` rows/day = 11 → 104 → 117 across 2026-05-16/17/18; both `memecoin` (142 rows last 3d) and `narrative` (90 rows last 3d) pipelines firing; `chain_complete` events present today. See `tasks/findings_chain_anchor_resolved_2026_05_18.md` for status-correction record + §9c near-miss note (drift-check that almost filed a duplicate watchdog despite PR #146 already shipping `chain-anchor-health-watchdog.{sh,service,timer}`). **No follow-up filed** — recurrence-prevention watchdog already shipped as part of PR #146.

**Mechanism (per PR #146):** all three built-in `chain_patterns` rows were inactive on prod, so `load_active_patterns()` returned empty and `check_chains()` exited before anchor matching/writes. Fix adds protected built-in provenance (`is_protected_builtin`, `disabled_reason`, `disabled_at`), exact prod-snapshot legacy recovery, safe built-in reconciliation that preserves operator/code disables and learned `alert_priority`, lifecycle blocked-retirement for protected built-ins, explicit `chain_no_active_patterns` logging, and hourly `chain-anchor-health-watchdog` systemd coverage for active-pattern starvation / stale `active_chains` under recent anchor-eligible events. Fresh verification: focused chain-anchor suite 49 passed; wider chain suite 79 passed, 1 skipped. **V37 audit-review fold:** mechanism updated.
**Tag:** `regression` `silent-failure` `tier-1a-down` `chain-detection` `recurrence`
**Why:** Tier 1a's strongest signal (`chain_completed`) has been STRUCTURALLY DEAD for 5.5+ days. `active_chains` MAX(anchor_time)=2026-05-11T16:42Z; no new anchors since.
**Mechanism per §9c (V37-corrected):** narrative pipeline anchor event `category_heating` IS firing today (1,805 lifetime events; last 2026-05-17T07:04Z, 7min before this audit). ALL upstream step events for narrative are LIVE. So the break is NOT upstream — it's INSIDE the chain-step-matching logic in `chains.tracker._check_active_chains` OR the `active_chains` writer. Possible mechanisms: (a) anchor match logic recently rejects what used to match (payload schema drift), (b) `INSERT INTO active_chains` silently fails (cooldown dedup, conviction_boost gate), (c) anchors are created but immediately marked complete with 0 steps and pruned. **Truncate/prune ruled out** by per-day rowcount (`SELECT substr(anchor_time,1,10), COUNT(*) FROM active_chains GROUP BY 1` shows zero new rows post-2026-05-11, not declining-over-time).
**Action:** ~3-5h diagnostic + fix.
(i) **Memecoin pipeline diagnostic** (dead 13+d, since 2026-05-04T00:26Z): disjoint anchor event set from narrative. Two different last-fire dates from two different upstream emitter sets → likely TWO separate failures. Drill each separately:
   - For memecoin: confirm memecoin pattern's step-1 anchor event_type by reading `scout/chains/patterns.py`; verify those event_types fire in signal_events post-2026-05-04.
   - For narrative: anchor `category_heating` fires today; instrument `chains.tracker._check_active_chains` to log step-1 match attempts + reasons-for-non-match.
(ii) Trace `_record_anchor` (or equivalent): is the INSERT executing? Cooldown dedup query result?
(iii) Compare pattern step-1 payload at 2026-05-11T16:42Z (last working) vs current — schema drift?
(iv) Add `active_chains_write_rate` watchdog SLO as part of fix.
**Cheap interim check (V37 SHOULD-FIX):** operator runs `SELECT event_type, pipeline, MAX(created_at) FROM signal_events GROUP BY 1,2` in morning checks — surfaces continued silence at signal-event granularity.
**Recurrence prior art:** `project_chain_revival_2026_05_03.md` — April 14 → May 1 (17 days dead). Same substrate class. PR #60/#61 fix history may guide diagnosis.
**decision-by:** 1 week (HIGH priority — Tier 1a outage; cohort digest (cycle 5) renders `chain_completed` as `near-identical` so the digest doesn't even surface the outage at the cohort-comparison layer).

### BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION: decide whether first_signal stays or retires
**Status:** SHIPPED-WITH-DECISION 2026-05-17 — branch `feat/first-signal-retirement-decision` (cycle 9, analysis-only). Findings doc: `tasks/findings_first_signal_retirement_decision_2026_05_17.md`. **Decision: Option A REVIVE-AND-SOAK** with 14d window ending 2026-05-31. Root cause was AUTO-SUSPEND on 2026-05-02T01:00:18Z under PRE-PR-#79 logic; under current combined-gate logic the suspension would NOT fire (`net_pnl = -$132 > -$200` per V38/V39 verified). Operator runs revival via `Database.revive_signal_with_baseline` helper (`scout/db.py:4056`) with `operator='operator'` (V40 MUST-FIX — cool-off filter compatibility) and `systemctl stop`/`start` around the python invocation (V41 SHOULD-FIX — avoid BEGIN EXCLUSIVE race). Memory checkpoint: `project_first_signal_revival_decision_2026_05_31.md`. Pre-registered verdict criteria + n≥10 trip-wire + 28d auto-extend + early-halt at n≥20 per CLAUDE.md §11. 1 follow-up STAGED conditionally (to be filed at 2026-05-31 decision time): BL-NEW-FIRST-SIGNAL-RETIRE-CODE (if revival fires hard_loss or regresses).

**Original status (now historical):** PROPOSED 2026-05-17 — Finding 3 from BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT (cycle 7, post-V36 fold). Cycle 9 drilled the root cause beyond the 3 hypotheses (a/b/c) in the original action list.

### BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING: regime-stratified revival gate for losers_contrarian (and revival-criteria template for all signals)
**Status:** SHIPPED 2026-05-17 — PR #150 squash-merged to master at `a20891f` (2026-05-17T21:48:57Z). New module `scout/trading/revival_criteria.py` (~650 LOC) enforces n≥100 floor + cutover-stratified two-window + Wilson-LB + bootstrap-LB + 2 secondary diagnostic gates. Verdict renamed `keep_on_permanent` → `keep_on_provisional_until_<iso>` (30d expiry default) to embed revocability. 49 unit tests pass on srilu Python 3.12.3; 506 adjacent regression tests pass. Full Plan→Design→Build→PR reviewer cycle: 2 plan reviewers + 2 design reviewers + 3 PR reviewers; all CRITICAL + IMPORTANT folded. Empirical evaluation against srilu prod scout.db (4 signals): losers_contrarian=STRATIFICATION_INFEASIBLE (cutover 0d ago, correct), gainers_early=FAIL contradicting 2026-05-13 audit-id=24, chain_completed + volume_spike = BELOW_MIN_TRADES (correct refusal at low n). Memory checkpoint: `project_lc_revival_criteria_shipped_2026_05_17.md`.

**Original status (now historical):** PROPOSED 2026-05-17 — surfaced by drill on losers_contrarian post-soak bleed (5/13 → 5/17). Filed concurrent with auto-suspend firing 2026-05-17T01:02:46Z (audit ids 26/27). Backlog scope item 5 ("post-verdict monitoring") implemented as `keep_on_provisional_until_<iso>` verdict rename (revocable by structural expiry); active watchdog enforcement deferred to `BL-NEW-REVIVAL-VERDICT-WATCHDOG` follow-up below.

**Post-merge action (operator):** ✅ COMPLETED 2026-05-17. PR #150 merged at `a20891f` (2026-05-17T21:48:57Z) per Reviewer 1 signoff. Status above flipped from `PR-OPEN / PENDING-MERGE` → `SHIPPED <date>` with merge SHA.

### BL-NEW-REVIVAL-VERDICT-WATCHDOG: active enforcement of keep_on_provisional_until_<iso> expiry
**Status:** SCRIPT-SHIPPED / SCHEDULING-PENDING-OPERATOR 2026-05-19 — design PR #185 reviewed and folded; implementation PR opened against branch `codex/revival-verdict-watchdog-impl`. 18/18 tests passing. Cron entry NOT installed; activation requires explicit operator approval (see `cron/README.md` "Revival-verdict-watchdog" section). Matches BL-NEW-CRON-DRIFT-WATCHDOG precedent.
**Tag:** `revival` `watchdog` `silent-failure-prevention`
**Why:** PR #150 ships the verdict-stamp machinery (`keep_on_provisional_until_<iso>`) but does NOT actively enforce the expiry. Operator runs the evaluator manually; if the operator forgets to re-run at expiry, the audit row sits as a stale "valid" verdict. Per CLAUDE.md §12-style silent-non-failure rule: if it looks like a primitive but doesn't fire, the operator's mental model is wrong about the system.
**Action:** ~6-8h implementation. Daily cron job that scans `signal_params_audit` for the most-recent `field_name='soak_verdict'` row per signal_type whose `new_value LIKE 'keep_on_provisional_until_%'`; if the parsed expiry timestamp is in the past, either (a) emit operator alert "verdict expired, re-run evaluator" or (b) auto-write a revoke row. **Implemented as (a)** — operator decision-point preserved.
**Approach implemented:** (a) alert-only. Auto-revoke (b) explicitly rejected as a §12b "automated state reversal of operator-applied state" case.
**Post-merge action (operator):** smoke-test the script on srilu (`bash scripts/revival-verdict-watchdog.sh` — expected exit 0 on prod today since 0 provisional rows exist). When ready, follow the activation runbook in `cron/README.md` to install the daily cron entry.
**Decision-by:** 4 weeks from PR #150 merge (2026-06-14).

### BL-NEW-REVIVAL-CRITERIA-QUARTERLY-RECALIBRATION: periodic re-derivation of healthy-signal baselines
**Status:** PROPOSED 2026-05-17 — PR-stage reviewer #3 finding #10 follow-up.
**Tag:** `revival` `recalibration` `defer-flag`
**Why:** `Settings.REVIVAL_CRITERIA_*` thresholds derive from 2026-05-17 healthy-signal baselines (chain_completed n=12, volume_spike n=36, narrative_prediction n=185). Signal regimes shift over time; baselines drift. The `EXIT_MACHINERY_MIN=0.70` anchor in particular is small-sample-dependent.
**Trigger conditions** (per `tasks/baselines_revival_criteria_2026_05_17.md` defer-flag): re-derive whenever (a) 90d elapsed since last derivation, OR (b) any signal-set change, OR (c) any FAIL verdict whose failure_reasons list contains ONLY `exit_machinery_contribution` (suggests the threshold is biting on a healthy signal).
**Action:** ~30min — re-run the baseline SQL queries from `tasks/baselines_revival_criteria_2026_05_17.md` against current scout.db; if any baseline shifts >10%, update `Settings` defaults via .env override.
**Decision-by:** Calendar-bound at 2026-08-17 (90d) OR data-bound on trigger.

### BL-NEW-EVALUATION-HISTORY-PERSISTENCE: persist evaluator runs to DB beyond structlog
**Status:** PROPOSED 2026-05-17 — PR-stage reviewer #2 finding #16 + reviewer #3 finding #11 follow-up.
**Tag:** `revival` `observability` `forensics`
**Why:** PR #150's evaluator emits `revival_criteria_evaluated` structlog events only. journalctl on srilu retains them, but cross-evaluation analysis ("what verdicts has gainers_early received over time?") requires reading structured logs which is inconvenient. A dedicated `revival_criteria_runs` table would persist verdicts + diagnostics + cutover info per evaluation.
**Action:** ~3-4h. Add table to db.py schema with columns (id, signal_type, evaluated_at, verdict, n_trades, cutover_at, cutover_source, cutover_age_days, window_a_*, window_b_*, failure_reasons_json). Wire write in `evaluate_revival_criteria`. Dashboard surface optional.
**Decision-by:** 8 weeks from PR #150 merge.

### BL-NEW-REVIVAL-CRITERIA-PER-SIGNAL-TUNING: per-signal Settings overrides for revival criteria
**Status:** PROPOSED 2026-05-17 — PR-stage reviewer #1 + #3 finding #7 follow-up.
**Tag:** `revival` `per-signal-tuning`
**Why:** Global thresholds (n≥100, Wilson LB ≥55%, etc.) may be too strict/lenient for specific signal classes. chain_completed at n=12 with +$108/trade × 83% win is a strong signal but the n=100 floor blocks evaluation for ~205 days at current fire rate. If operator concludes the floor is structurally too high for low-fire-rate / high-EV signal classes, a per-signal override mechanism unblocks the evaluator without weakening the floor for noisy signals.
**Action:** ~4-6h. Add `signal_revival_criteria_overrides` table (signal_type, key, value); evaluator consults overrides first, falls back to Settings defaults. CLI flag `--show-overrides` for transparency.
**Decision-by:** Conditional — file only when operator has observed a specific case where global thresholds are demonstrably inappropriate. May never fire.

### BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION: extend forward-price substrate so TG/X source-call outcomes can resolve beyond top-board membership
**Status:** DESIGN-SHIPPED / IMPLEMENTATION-GATED 2026-05-21 - PR #208 merged (`d57f6d59`). Plan at `tasks/plan_source_call_price_coverage_expansion_2026_05_21.md`, design at `tasks/design_source_call_price_coverage_expansion_2026_05_21.md`. Prod check after PR #207: 1,254 `source_calls`, 14 `price_at_call`, 0 rows with 1h/6h/24h forward pct. Root cause is structural, not a bug: `scout/source_quality/ledger.py:142` (`_fetch_snapshot_rows`) queries only `gainers_snapshots` + `losers_snapshots` keyed by CoinGecko `coin_id`. TG/X-called tokens overwhelmingly fall outside top-gainers/losers boards by selection design - that is the value prop of KOL early-detection, not a price-coverage failure.
**Tag:** `source-quality` `price-coverage` `tg-social` `x-alerts` `attribution`
**Why:** With the current substrate the ledger functions as a **source-call coverage/duplication ledger** (duplicate_rate, cluster diversity, per-source raw volume) but cannot rank sources by forward PnL. The 99.2% unresolvable rate is honest truth, not failure — but it caps near-term ledger value to volume/duplication signals only.
**Current scope:** Do NOT extend `_fetch_snapshot_rows` in the first PR. Design a sidecar `source_call_price_observations` substrate with explicit trust tier, source family, aggregation mode, chain identity, candle availability semantics, liquidity evidence, and prod-copy preview workflow.
**Hermes-first basis:** GoldRush/Covalent and CoinGecko MCP are both valid MCP-aligned historical-price candidates. Evaluate both against the gecko-alpha residual gap before building bespoke clients. DexScreener latest-spot API is rejected for historical coverage unless implemented later as a prospective cache.
**Non-goals:** No source ranking / pruning built off this. No dashboard "best source" surface. No actionability consumption. No live config. No vendor call without explicit operator approval. Ledger remains a coverage/duplication artifact until price coverage materially improves AND a separate BL promotes it to PnL ranking.
**Acceptance:** A future implementation must report total-population unresolved rate separately from an identity-eligible denominator; identity-eligible unresolvable rate must be <=80%; at least one source must reach `min_sample=10` and primary-horizon coverage >=0.50 with temporal integrity, trust-tier labeling, chain identity, and liquidity/pool context enforced. A coverage win bought by relaxing these invariants does not count.
**Implementation gate:** Do not start implementation until the operator explicitly authorizes the vendor sample/timestamp-semantics check. GoldRush/Covalent and CoinGecko MCP both require approved sample evidence for candle availability semantics, chain identity fields, liquidity evidence, and cost/rate-limit behavior before any code path or schema migration is scoped.

### BL-NEW-SOURCE-CALL-LIVE-WRITER-WIRE: keep `source_calls` fresh as new TG/X rows land
**Status:** SHIPPED-IN-PR 2026-05-20 — co-shipped with lag watchdog activation in branch `feat/source-calls-lag-watchdog-activate`. Path (i) cron entry: `*/5 * * * * scripts/source-calls-live-writer.sh` invoking `scripts/source_calls_live_writer.py`. Writer is idempotent, no Telegram dispatch (single alerter surface is the lag watchdog per §12a).
**Tag:** `source-quality` `pipeline-freshness` `silent-failure-prevention`
**Why:** Class-1 silent-failure shape per global CLAUDE.md §12a — substrate table shipped + freshness watchdog shipped, but the writer that should keep the watchdog happy was never wired. Empirically validated 2026-05-20T17:54Z when the first manual watchdog run against prod (61 min after backfill ceiling) detected `unledgered_tg=10` and dispatched a real Telegram alert. Co-shipping the writer prevents that becoming a permanent alert-fatigue surface once cron is activated.
**Implementation:** `scripts/source_calls_live_writer.py` (Python CLI, exit 0/1) + `scripts/source-calls-live-writer.sh` (bash wrapper, no Telegram). Calls `backfill_source_calls(conn)` + `refresh_source_call_outcomes(conn)` per invocation. UPSERT semantics by (source_type, source_event_id) → idempotent. Cron cadence 5min, watchdog SLO 30min → 6x writer cycles inside the SLO.
**Non-goals:** No change to ledger schema. No new alerting layer (lag watchdog owns operator-visible alerts). No source ranking / pruning built off this — writer keeps the ledger current; it does NOT make the ledger rankable. Price-coverage expansion is BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION.
**Acceptance:** After deploy + 1 writer cycle:
1. New upstream rows get `source_calls` rows (`MAX(call_ts)` advances toward `MAX(posted_at)` / `MAX(tweet_ts)`).
2. Lag watchdog returns `ok=true` on the next cron tick.
3. No repeated Telegram alerts in journalctl for `source-calls-lag-watchdog`.

### BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG: writer-rate parity check independent of upstream-lag watchdog
**Status:** SUPERSEDED / SHIPPED-DEPLOYED 2026-05-22 - duplicate row. The active shipped record is the top-level `BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG: detect writer cron outages independently of upstream traffic`, shipped via PR #211 and activated with hotfixes #216/#217/#218.
**Tag:** `source-quality` `silent-failure-prevention` `cron-health`
**Why:** Per global CLAUDE.md §12a — every pipeline table needs both upstream-lag detection AND writer-rate detection. The lag watchdog is upstream-anchored; this BL adds a writer-rate-anchored watchdog. Without this, a sustained writer-cron daemon stall during a TG/X burst could lag pipelines silently until the upstream catches up (which would then trigger the lag watchdog — but by then the burst is already partly missed).
**Scope:** Tiny — add a second `check_source_calls_writer_rate.py` (or extend the existing `scripts/check_source_calls_lag.py` lag-watchdog check) that asserts writer ticked ≥N times in the last M minutes (e.g., ≥2 ticks in 15min for `*/5` cron). Wire as a second cron entry every 10 min. Telegram-alert on rate-floor breach.
**Non-goals:** No change to writer or watchdog wrapper. No source ranking. No price-coverage work.
**Acceptance:** Watchdog fires Telegram alert when writer ticks fall below threshold for ≥2 consecutive 10-min cycles, independent of upstream activity. 7-day soak with zero false positives during normal operation.
**Re-eval trigger:** use the shipped top-level row for any future 90-day kill/review decision.

### BL-NEW-MIROFISH-DEBUG-NOISE-SUPPRESS: stop journal pollution from fallback_raw_response DEBUG events
**Status:** SHIPPED 2026-05-26 - PR #286 merged/deployed at `8e799ea`; successful fallback raw-response logging removed in `scout/mirofish/fallback.py`. Pre-work prod baseline on srilu at SHA `a455365`: `fallback_raw_response_24h=50`, `fallback_raw_response_7d=350`, broad health grep saw `4` hits involving this event in 24h. Deploy-window smoke after pipeline restart: `fallback_attempts=0`, `fallback_failures=0`, `raw_response_events=0`, `broad_health_hits=0`; recorded as clean no-live-fallback-observed smoke.
**Tag:** `observability` `journal-hygiene` `low-priority`
**Why:** Operator-visible health checks (`journalctl --since X -iE error|exception|traceback`) currently must filter out MiroFish DEBUG noise manually. Suppressing the DEBUG-level emission OR promoting it to a separate counter would clean the operator-grep surface.
**Scope:** Remove `fallback_raw_response` from successful Anthropic fallback responses. Parse failures still surface truncated raw text through `FallbackScoringError` and gate error logging.
**Non-goals:** No fallback behavior change. No MiroFish client change.
**Acceptance:** post-deploy journal window distinguishes "no live fallback observed" from "fallback fired"; when fallback fires, `fallback_raw_response=0`, no Anthropic fallback failures attributable to this change, and healthy broad grep no longer contains this event.

### BL-NEW-LIVE-DECISION-COCKPIT: turn signal substrate into a trader-facing "trade / watch / reject" surface
**Status:** SHIPPED-PARTIAL / PARENT-ARCHIVED 2026-05-26 — parent cockpit work is no longer buildable as a single backlog item. Core trader surfaces now exist: `/api/live_candidates`, Now Tradable, `/api/trade_inbox`, tracker-to-cockpit promotion, trade decision events, Trade Inbox contract firewall, and aggregate dashboard contract smoke. Future work must target specific residual child gaps instead of rebuilding the parent.
**Tag:** `dashboard` `live-decision` `trader-cockpit` `actionability` `hermes-first`
**Why:** The next leverage point is not another raw signal. It is a decision cockpit that collapses existing evidence into one per-token verdict with explicit reasons and refusal states. The useful current filters are `paper_trades` + current `price_cache`, `actionable=1`, `would_be_live=1`, fresh open-trade PnL vs entry, and source-call health warning when TG/X is not rankable. The friction is absence of a single "what can I trade now?" endpoint and UI.

**2026-05-26 audit evidence:** shipped PR chain covers the parent surface: PR #228/#229/#232 (`/api/live_candidates` + counter_flags fix + contract smoke), PR #239 (Now Tradable and Signal Trust V1 tabs), PR #270 (deterministic live_candidates contract delta), PR #273 (Trade Inbox), PR #279 (trade decision events), PR #281 (tracker wins promoted to Trade Inbox), PR #282/#283 (Trade Inbox contract firewall/folds), PR #284/#285 (aggregate dashboard contract smoke + deploy record). Keep remaining work as child items such as signal trust scorecards, TG alert qualification after soak, or explicitly scoped entry/risk display deltas.

**Operator-trader diagnosis captured:**
- Trustable today: pipeline health, actionability stamps, `would_be_live`, chain_completed + volume_spike, and explicit "not rankable yet" source-call health.
- Not trustable today: TG/X direct trading input, KOL ranking, narrative predictions as standalone entries, broad open-paper-trade list as a cockpit, or automatic live allocation.
- Product target: "Show me 3-5 candidates worth a tiny live experiment, with reasons and caveats."
- Product non-target: blind live trading, auto-sizing, KOL/source pruning, actionability-v2 consumption, or autonomous execution.

**Hermes-first posture:** Hermes should enrich and explain, not become the substrate. Before the plan/design PR, re-run the mandatory Hermes-first section and check in-tree drift first. Expected split:

| Domain | Hermes role | Decision boundary |
|---|---|---|
| Trader-readable candidate explanation | summarize why a token is interesting/dangerous; convert raw evidence into "why trade / why avoid" | BRIDGE_TO_HERMES if an installed/public skill fits; otherwise keep prompt/output contract local |
| Counter-risk interpretation | detect already_peaked, weak community, dead_project, fake catalyst, copycat meta from existing prediction/counter-risk fields | USE_AS_ENRICHMENT; never override price/actionability truth without structured evidence |
| TG/X/KOL context | normalize call context and source narrative | CONTEXT_ONLY until source-call price coverage becomes rankable |
| Price truth / PnL / identity / execution | none | KEEP_CUSTOM; Hermes must not be load-bearing for price, PnL attribution, chain identity, or order execution |

**Child backlog sequence:**

1. **BL-NEW-LIVE-CANDIDATES-ENDPOINT** — Add read-only `/api/live_candidates`.
   - **Status:** SHIPPED-MERGED / CONTRACT-HARDENED 2026-05-26 — implemented in `f81b63ed` (PR #228) with counter_flags hotfix `db19e79a` (PR #229), operator contract+smoke validator `0727e218` (PR #232), and deterministic ordering/unique-token hardening via PR #270.
    - Return 10-20 per-token rows, not raw trades.
    - Inputs: open/recent `paper_trades`, `price_cache`, actionability metadata, `would_be_live`, `chain_matches`, latest prediction/counter-risk fields, source-call health.
    - Include token, symbol, name, current price, mcap, 24h change, open paper trade ids, signal surfaces, `actionable`, `would_be_live`, current-vs-entry %, inclusion reasons, exclusion/risk reasons, and `trade/watch/reject/data_insufficient`.
    - No writes, no live execution, no suppression.

2. **BL-NEW-TRADER-READINESS-SCORE** — Add a score separate from conviction.
   - **Status:** PARTIALLY-SHIPPED 2026-05-26 — Trade Inbox now emits `trade_score`, `action_label`, `window_state`, reasons, and refusal diagnostics. Do not re-open as a generic scoring system; any next work must be a measured refinement against Trade Inbox behavior.
   - Positive factors: actionability pass, `would_be_live` pass, multiple independent surfaces, fresh signal age, current price still near entry, sane mcap/liquidity, no counter-risk flags, resolved identity.
   - Negative factors: high counter-risk, already faded beyond threshold, already ran too far beyond entry, source not rankable, unresolved identity, TG/X-only context, stale price.
   - Must emit factor breakdown; no opaque single number.

3. **BL-NEW-PER-TOKEN-EVIDENCE-BUNDLE** — Collapse duplicate evidence by token.
   - **Status:** PARTIALLY-SHIPPED 2026-05-26 — Trade Inbox groups paper rows and tracker-promoted rows with provenance (`source_corpus`, `open_trade_ids`, `recent_trade_ids`, `surfaces`). A deeper token drawer remains optional, not parent-blocking.
   - Example target: ALLO row shows `volume_spike` yesterday + `chain_completed` today + `actionable=1` + `would_be_live=1` instead of two disconnected trade rows.
   - Evidence windows pre-registered (e.g. 36h primary, 7d historical support).
   - Distinguish active evidence from historical color.

4. **BL-NEW-ENTRY-QUALITY-STATE** — Separate "system says yes" from "entry is still good".
   - **Status:** SHIPPED-V1 2026-05-26 — `/api/live_candidates` and `/api/trade_inbox` expose entry/window state and already-ran/blocked routing. Future refinements should be scoped as explicit label semantics changes.
   - Labels: `fresh_entry`, `acceptable_pullback`, `already_faded`, `already_ran`, `too_stale`, `already_stopped_out`, `data_insufficient`.
   - Uses current price vs paper entry, signal age, checkpoint/peak context, and price freshness.
   - Prevents `actionable=1` tokens that are already -10% or +25% from being silently treated as equally tradable.

5. **BL-NEW-NARRATIVE-COUNTER-RISK-INTO-TRADE-VIEW** — Promote prediction warnings into the candidate row.
   - **Status:** SHIPPED-V1 2026-05-27 - backend exposes `counter_risk_score` and `counter_flags` on live candidates, and PR #290 exposes display-only counter-risk context in Trade Inbox (`counter_risk_score`, `counter_flags`, `counter_risk_predicted_at`). PR #278 is closed/superseded. Future refinements should target Trade Inbox label semantics, not the stale Now Tradable PR.
   - Surface `dead_project`, `weak_community`, `already_peaked`, `narrative_mismatch`, low fit score, and counter-risk score as red/yellow badges.
   - Narrative predictions can enrich or downgrade a candidate; they are not standalone live entries unless the scoring layer also passes entry-quality and actionability gates.

6. **BL-NEW-DASHBOARD-NOW-TRADABLE-PANEL** — Build a dashboard panel backed by `/api/live_candidates`.
   - **Status:** MERGED-IN-TREE 2026-05-24 — PR #239 merged (merge commit `050fe12b`): adds read-only “Now Tradable (V1)” + “Signal Trust (V1)” tabs; no execution/pruning affordances.
    - Shows only assets a human might consider now.
    - Top-level buckets: `trade small now`, `watch only`, `reject`, `data insufficient`.
    - Each row links to paper trade detail and evidence bundle.
    - Explicit caption: "TG/X context is excluded from ranking until source-call price coverage is rankable."

7. **BL-NEW-TGX-CONTEXT-ONLY-GUARDRAIL** — Prevent unrankable TG/X from influencing live candidate labels.
   - **Status:** ENFORCED-BY-CONTRACT 2026-05-26 — Trade Inbox contract firewall and aggregate dashboard contract smoke guard against urgency/alert/ranking leakage. Future alert intent should use a separate endpoint unless a deliberate contract PR relaxes the firewall.
   - If `source_calls.rankability.rankable=0` or price coverage is below threshold, TG/X may appear as context badges only.
   - It must not boost `trade` labels, KOL ranking, pruning, or actionability consumption.
   - Unlock condition: source-call price coverage expansion ships and at least one source reaches `min_sample=10`, coverage >=0.50 with temporal integrity, trust-tier labeling, and chain identity enforced.

**Plan/design gates before implementation:**
1. Drift-check current dashboard/API first; close any child item that already exists.
2. Hermes-first analysis documented near the top of the plan/design.
3. Runtime-state verification: query current closed/open actionability cohorts, source-call rankability, and price freshness before scoring design.
4. No live execution, order sizing, KOL pruning, or source-ranking consumption in V1.
5. Treat disagreements between `actionable` and `would_be_live` explicitly; do not hide them behind the score.

**Acceptance for V1:** MET for the parent cockpit as of 2026-05-26 except for optional refinements noted above. Operator has Now Tradable and Trade Inbox panels/endpoints, candidate labels and refusal reasons, tracker-promoted rows, read-only contract tests, and CI contract smoke. Keep TG/X and KOL context excluded from ranking until source-call coverage becomes rankable.

### BL-NEW-SIGNAL-TRUST-ROADMAP: convert Gecko-Alpha from signal collector to trustable signal system
**Status:** PARTIALLY-SHIPPED 2026-05-27 - registry and Signal Trust tab shipped in PR #239; per-signal scorecards shipped in replacement PR #289. PR #276 is closed/superseded. Remaining roadmap items below require fresh scope from current base.
**Tag:** `signals` `trust` `actionability` `hermes-first` `live-readiness`
**Why:** The signal layer has uneven maturity. `chain_completed` and `volume_spike` currently feel more useful than raw TG/X noise; `actionable=1` and `would_be_live=1` are strong filters; source-call health correctly warns when KOL/TG/X is not rankable. But narrative predictions can still create paper trades while their own reasoning says "weak fit"; TG/X remains unresolved/noisy; and open paper trades are too broad to be treated as a trustable signal surface.

**Trust boundary captured:**
- Trust today: pipeline is observable, actionability stamps exist, `would_be_live=1` is a strong extra filter, chain_completed + volume_spike are useful, and the system can say "not rankable yet."
- Do not trust today: TG/X as direct entries, KOL ranking, narrative predictions as standalone entries, broad open-paper-trade lists, automatic live allocation, or source pruning.
- Target state: every signal family has an explicit maturity state and can be used only at its allowed maturity level.

**Hermes-first posture:** Hermes should help classify, explain, compare, and summarize signal quality. Hermes must not be load-bearing for price truth, execution, PnL attribution, chain identity, or KOL ranking before price coverage is rankable.

| Signal trust domain | Hermes role | Decision |
|---|---|---|
| Narrative quality and counter-risk | classify "weak fit", dead_project, weak_community, already_peaked, fake catalyst, copycat meta | BRIDGE_TO_HERMES / USE_AS_ENRICHMENT |
| Trader-readable signal explanation | explain why a signal family fired and why it may be dangerous | BRIDGE_TO_HERMES if available; otherwise local prompt contract |
| Similar historical winners/losers | summarize comparable prior cases using already-trusted DB facts | USE_AS_ENRICHMENT only |
| Signal maturity, PnL cohorts, source ranking | none for truth computation | KEEP_CUSTOM |
| Price, identity, execution, PnL | none | KEEP_CUSTOM |

**Child backlog sequence:**

**V1 visibility artifacts (already in-tree):**
- Registry: `docs/superpowers/registries/signal_trust_registry.v1.json`
- Validator: `scripts/validate_signal_trust_registry.mjs`
- Runbook: `docs/runbooks/signal-trust-roadmap-v1.md`
- **Status:** MERGED-IN-TREE 2026-05-24 — PR #239 merged (merge commit `050fe12b`): adds read-only `GET /api/signal_trust_registry` export + dashboard tab (still visibility-only; not-for-pruning/not-for-auto-disable).

1. **BL-NEW-SIGNAL-MATURITY-TAXONOMY** - Give every signal family an explicit maturity state.
    - Example states: `trusted_experimental`, `context_only`, `data_insufficient`, `quarantined`, `retire_candidate`.
    - Initial expected mapping: chain_completed and volume_spike are `trusted_experimental`; narrative_prediction is `needs_hard_filter`; TG/X is `context_only`; first_signal remains soak-gated until 2026-05-31.
    - Must be derived from current prod evidence, not vibes.

2. **BL-NEW-SIGNAL-FAMILY-SCORECARDS** - Build per-signal scorecards.
   - **Status:** SHIPPED 2026-05-26 - replacement PR #289 merged `GET /api/signal_trust/scorecards`, dashboard rendering, anti-consumption metadata, and endpoint/contract tests. Original PR #276 is closed/superseded.
   - For each signal_type: 7d/14d/30d opens, closes, net PnL, win rate, average PnL, median PnL, max loss, open count, actionable pass rate, would_be_live pass rate, and current maturity state.
   - Include sample-size warnings and Wilson/bootstrap guards where useful.
   - No automatic parameter changes in V1.

3. **BL-NEW-ACTIONABLE-VS-WOULD-BE-LIVE-ARBITRATION** - Make disagreement explicit.
   - Cases:
     - `actionable=1` + `would_be_live=1`: strongest experimental candidate.
     - `actionable=1` + `would_be_live=0`: metadata says plausible, live rules disagree; review only.
     - `actionable=0` + `would_be_live=1`: investigate rule mismatch; do not silently trade.
     - both false: reject unless operator explicitly overrides.
   - Feed this into the live decision cockpit and signal scorecards.

4. **BL-NEW-NARRATIVE-PREDICTION-HARD-FILTER** - Stop weak narrative predictions from becoming paper/live candidates without an explicit downgrade.
   - If narrative reasoning or counter-risk contains weak fit, dead_project, weak_community, already_peaked, high counter-risk, or fit below configured floor, mark the candidate `watch` or `reject`.
   - Use Hermes as enrichment/reference for semantic classification, but only structured fields may drive final filters.
   - No suppression until plan/design verifies current prediction false-positive/false-negative cohorts.

5. **BL-NEW-SIGNAL-QUARANTINE-RULES** - Formalize context-only and quarantine behavior.
   - TG/X remains context-only until source-call price coverage and rankability gates pass.
   - Signals with repeated stale/faded entries, unresolved identity, or missing price freshness become `quarantined` for candidate ranking but remain visible for observability.
   - Quarantine must be reversible and operator-visible; no silent auto-disable in V1.

6. **BL-NEW-SIGNAL-TRUST-DASHBOARD** - Add a signal trust panel.
   - Show signal families by maturity state, recent PnL, sample size, actionability pass rate, `would_be_live` pass rate, current open exposure, and next data-bound gate.
   - Avoid "best KOL" or source ranking until price coverage unlocks it.
   - Link each signal family to examples of recent trade/watch/reject candidates.

7. **BL-NEW-HERMES-SIGNAL-EXPLANATION-BRIDGE** - Use Hermes to generate concise "why this signal is interesting / dangerous" text.
   - Inputs must be structured DB facts and current signal evidence.
   - Output is explanation only; it cannot mutate score, price, identity, PnL, or live state.
   - Include audit logs or cached explanation records only after a plan confirms cost/rate behavior and prompt-injection safety.

**Plan/design gates before implementation:**
1. Drift-check existing scorecard, actionability, live_eligibility, source_calls, and dashboard health surfaces.
2. Hermes-first analysis must check installed VPS Hermes skills plus public Hermes skill hub and awesome-hermes-agent.
3. Runtime-state verification: query current signal_params, recent per-signal PnL, actionability cohorts, source-call rankability, and prediction counter-risk fields.
4. No live execution, sizing, pruning, auto-disable, KOL ranking, or source pruning in V1.
5. Every signal maturity decision must include sample-size and data-quality caveats.

**Acceptance for V1:**
- Operator can answer "which signal families do I trust today?" in one panel or endpoint.
- Every signal family has a maturity state, data-bound next gate, and refusal reason if not trusted.
- Narrative predictions with explicit weak/unsafe reasoning are not treated as equally tradable.
- TG/X remains context-only until price-coverage rankability unlocks source measurement.
- Hermes explanations are present only as enrichment, never as price/PnL/identity/execution truth.
