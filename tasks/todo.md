# Backlog — gecko-alpha

## Active Work: 2026-05-25 — Trade Opportunity Inbox

**Status:** DESIGN REVIEW COMPLETE. Goal: turn high-volume early detections into a trader-facing read-only queue: Act Now, Watch, Already Ran, Blocked.

Workflow checklist:
- [x] Trader-lens problem framed from screenshot: TOES was detected early but buried in historical/evidence tables.
- [x] Drift-check found `/api/live_candidates`, Now Tradable, and Top Gainers Tracker; residual gap is prioritization, not raw detection.
- [x] Hermes-first analysis recorded in `tasks/plan_trade_opportunity_inbox_2026_05_25.md`.
- [x] Plan drafted: `tasks/plan_trade_opportunity_inbox_2026_05_25.md`.
- [x] Plan reviewed by 2 parallel agents and Critical/Important folds applied.
- [x] Design drafted: `tasks/design_trade_opportunity_inbox_2026_05_25.md`.
- [x] Design reviewed by 2 parallel agents and Critical/Important folds applied.
- [ ] Build with TDD.
- [ ] PR opened.
- [ ] PR reviewed by 2 parallel agents and folds applied.

Review notes:
- Read-only only: no execution, sizing, pruning, signal suppression, or config changes.
- Plan review folds: group before per-group slice using broad cohort; add TOES-shaped regression; exact score table/sort key; stale warning vs hard-stale routing; audit fields retained; client-local new/seen/changed state; mandatory static frontend wiring test; full pytest verification.
- Design review folds: visible per-group overflow/show-more; source_limit/source_truncated diagnostics; Review Now display label; novelty not cleared on background render; dismissed restore; diagnostic empty states; exact builder counters/signature; exact sort_key encoding; score pseudocode; stale boundary tests; read-only `_ro_db` guard.

## Active Work: 2026-05-25 — Live candidates determinism + contract delta

**Status:** PR-READY (local commit). Goal: make `/api/live_candidates` deterministic (backend ordering + frontend keys) and extend the contract validator to enforce deterministic ordering + uniqueness.

Workflow checklist:
- [x] Drift-check (confirm current gaps vs V1 shipped)
- [x] Plan drafted + reviewed by 2 parallel agents
- [x] Design drafted + reviewed by 2 parallel agents
- [x] Build (backend ordering + validator checks + UI keys)
- [x] Focused verification (pytest + frontend build)
- [ ] PR opened (blocked: GitHub creds unavailable in this sandbox)
- [ ] PR reviewed by 2 parallel agents and folds applied

Review notes (live):
- Plan doc: `tasks/plan_live_candidates_determinism_contract_delta_2026_05_25.md`
- Design doc: `tasks/design_live_candidates_determinism_contract_delta_2026_05_25.md`
- Branch: `feat/live-candidates-determinism-contract-delta`
- Commit: local branch tip after review folds

## Active Work: 2026-05-24 — Dashboard open-positions width fix

**Status:** PR-READY. Goal: stop the Open Positions table from being horizontally cut off on wide screens by using the available left/right viewport space more effectively.

Workflow checklist:
- [x] Root cause confirmed from screenshot and frontend CSS.
- [x] Regression guard added for the dashboard width contract.
- [x] Scoped layout fix implemented in dashboard frontend.
- [x] Vite production build refreshed.
- [x] Local verification completed with focused test/build/browser check.
- [x] PR opened and reviewed.
- [ ] Other VPS PR status checked before deploy.
- [ ] Clean deploy completed if merge gate is clear.

Review:
- Root cause: global `.dashboard` width was capped at 1400px, leaving wide monitor space unused while the 16-column Open Positions table overflowed horizontally.
- Fix: `.dashboard` now expands up to 1880px while preserving 16px viewport gutters; Open Positions gets a dedicated compact table class with 8px horizontal cell padding.
- Regression: `tests/test_dashboard_frontend_layout.py` locks the wide dashboard width contract and Open Positions compact-layout hook.
- PR: #241 (`fix/dashboard-open-positions-width`) opened and self-reviewed; diff is limited to dashboard source, regenerated Vite dist, task log, and static regression coverage.
- Verification: `python -m pytest tests/test_dashboard_frontend_layout.py -q` passed; `python -m pytest tests/test_dashboard_frontend_layout.py tests/test_trading_dashboard.py -q` passed (`26 passed`); `npm run build:codex` passed via the existing local `node_modules` junction after `npm ci` was blocked by local npm/PyPI certificate failures; Chrome headless 1920px screenshot confirmed panels span the wider viewport; GitHub Actions `test` passed on PR #241.

## Active Work: 2026-05-23 — Codex/Hermes systemd auto-remediation

**Status:** IN-PROGRESS. Goal: convert Codex/Hermes systemd failure handling from alert-only to alert + guarded deterministic repair, with Telegram outcome updates.

Workflow checklist:
- [x] Root cause identified: alert template used `%I`, causing `hermes-gateway.service` to appear as invalid `hermes/gateway.service`.
- [x] Drift check found alert helpers and manual runbook, no general auto-remediator.
- [x] Hermes-first analysis recorded in `tasks/design_systemd_auto_remediation_2026_05_23.md`.
- [x] Design drafted: `tasks/design_systemd_auto_remediation_2026_05_23.md`.
- [x] Design reviewed by 2 parallel agents and Critical/Important folds applied.
- [x] Build with TDD.
- [x] Deploy to all 3 VPSes and verify Telegram outcome path.
- [x] PR opened.
- [x] PR reviewed by 2 parallel agents and Critical/Important folds applied.

Review:
- Local focused tests: `uv run pytest tests/test_codex_telegram_helpers.py tests/test_codex_fleet_telegram_status.py -q` passed with 23 tests.
- Deployed `/usr/local/bin/codex-systemd-failure-alert` and `/usr/local/bin/codex-systemd-auto-remediate` to all 3 VPSes.
- Handler templates now use `%i`, set `Restart=no`, and monitored units have `OnFailure=codex-systemd-failure-alert@%n.service codex-systemd-auto-remediate@%n.service`.
- Disposable verification on main-vps proved `OnFailure` launches both handlers; unallowlisted disposable failure skipped safely; controlled flaky service repaired to `active` via explicit disposable `--allow-unit` verification.
- `codex-fleet-telegram-status.timer` was restored to `enabled` / `active` after an external disable left it inactive during verification.
- PR #236 review folds: source-controlled systemd templates/drop-in + installer, injectable advisory lock path, runner exception handling with audit/Telegram outcome, disabled/masked/lock/audit/command-order tests.
- PR #236 CI: GitHub Actions `test` passed on head `ad67d9d4`.

## Active Work: 2026-05-23 — Fleet Telegram status for 3 VPSes

**Status:** DEPLOYED 2026-05-23. Goal: configure Telegram status reporting for `main-vps`, `vpin-vps`, and `srilu-vps`: one central rolling 7-hour digest every 8 hours from Main VPS time, plus instant Telegram alerts for monitored Codex/Hermes systemd unit failures.

Workflow checklist:
- [x] Design approved in chat: central digest on main-vps + local instant failure hooks on each VPS
- [x] Host map verified from SSH config: `main-vps` (`46.62.206.192`), `vpin-vps` (`89.167.55.176`), `srilu-vps` (`89.167.116.187`)
- [x] Existing ops surface discovered: daily `codex-readonly-operator-brief` on all 3, daily `codex-fleet-operator-brief` on main
- [x] TDD red/green for digest formatter (`tests/test_codex_fleet_telegram_status.py`)
- [x] Implementation plan saved: `docs/superpowers/plans/2026-05-23-fleet-telegram-status.md`
- [x] Deploy Telegram env + sender to all 3 VPSes
- [x] Deploy 8-hour fleet digest timer on main-vps
- [x] Deploy instant failure alert hooks on all 3 VPSes
- [x] Verify Telegram smoke messages, service success, timer status, and manual failure-alert path

Review:
- Main timer: `codex-fleet-telegram-status.timer` enabled/active; next run observed at 2026-05-23 22:42 UTC, with `TimeoutStartSec=5min`.
- Telegram verified: smoke messages sent from all 3 VPSes; synthetic `codex-systemd-failure-alert@...` units returned `Result=success` / `ExecMainStatus=0` on all 3.
- Live status path: main collector forced-key entries on vpin/srilu now run `/usr/local/bin/codex-fleet-remote-status`, so every digest includes current `failed_units` instead of stale daily brief prose.
- Current failures were reported once via Telegram during setup: main has 3 pre-existing failed units (`logrotate.service`, `prune-expense-receipts.service`, `shift-agent-backup.service`); srilu has 4 (`logrotate.service`, `prune-expense-receipts.service`, `send-routing-accuracy-summary-failure.service`, `send-routing-accuracy-summary.service`); vpin has 0.
- Hygiene: removed stray `UNIT` line from `codex-production-push-loop-vpin.timer`; timer remained active/enabled after daemon reload.

## Active Work: 2026-05-23 — BL-NEW-LIVE-DECISION-COCKPIT (V1 live_candidates endpoint)

**Status:** SHIPPED-MERGED. Read-only `/api/live_candidates` shipped in
`f81b63e` / PR #228; counter_flags rich-dict regression fixed in `db19e79` /
PR #229. This entry remains as the operator-facing status anchor for the
endpoint slice; dashboard "Now Tradable (V1)" + "Signal Trust (V1)" tabs merged
in PR #239 (merge commit `050fe12b`) and remain visibility-only.

Workflow checklist (mandatory for non-trivial work):
- [x] Plan drafted + Hermes-first section included (`tasks/plan_live_decision_cockpit_v1_2026_05_23.md`)
- [x] Plan reviewed by 2 parallel agents; Critical/Important folds applied (2026-05-23)
- [x] Design drafted (API contract + deterministic verdict rules) (`tasks/design_live_decision_cockpit_v1_2026_05_23.md`)
- [x] Design reviewed by 2 parallel agents; Critical/Important folds applied (2026-05-23)
- [x] Build: endpoint + DB query + tests implemented (`dashboard/api.py`, `dashboard/db.py`, `dashboard/models.py`, `tests/test_live_candidates_endpoint.py`)
- [x] Verification: focused endpoint test suite exists; 2026-05-23 sandbox rerun attempted with
  `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_live_candidates_endpoint.py -q`
  but dependency resolution was blocked by restricted DNS for `pydantic-settings==2.13.1`
- [x] PR opened/merged: PR #228 (`f81b63e`) + PR #229 (`db19e79`)

Operator gates respected:
- No live execution, sizing, pruning, suppression, or threshold changes
- TG/X remains context-only (no rank/boost) until source-call price coverage is rankable
## Active Work: 2026-05-22 — Close-development block (operator-authorized)

**Status:** EXECUTED — paced through 7 phases (P0 state sync → P7 final report). All docs-only changes; no code shipped this block. Two PRs from this block: #221 (BL-NEW-DEPLOY-FILEMODE-CRLF-HYGIENE filed) MERGED, #226 (historical pool-selection probe NEGATIVE findings) OPEN/under review at session end.

**Phases ran:**
- P0 state sync: prod healthy (gecko-pipeline + gecko-dashboard active, x_alerts 305ms / source_calls 59ms, heartbeat fresh).
- P1 #221 merged (squash, 9fb1e659) — backlog docs-only, no prod pull needed.
- P2 #33: state verified DIRTY + 2654 LOC + 32d old; left OPEN per operator directive, no merge/close. Status anchor filed (see BL-NEW-PR-33-DESIGN-REVIEW-REQUIRED below).
- P3 BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE: ran 8/10 GT free calls under operator-authorized packet; 0 of 5 informative pool-probes covered call_ts. Token A (CIPHER 7mo): 3 oldest pools all 401. Token B (4mo): 2 oldest pools have data only ~14d pre-call_ts. Token C (3mo): rate-limited. PR #226 recommends PARK historical GT backfill; operator picks Path 2 (CG Pro paid) or Path 3 (GT-free forward-only). Two-vector plan review + findings PR review folded (V-A I1/I2/I3 hedged via §3.5 distribution analysis).
- P4 actionability data gate: THIN. n_actionable=21 (≥20 ✓) but n_exploratory=3 (<5 ✗); no early-fire trip; actionable cohort positive (+$224.58, 19/21 wins). Next data-bound trigger: n_exp≥5, est. 2026-05-25 to 2026-05-29. See `tasks/findings_actionability_gate_check_2026_05_22.md`.
- P5 operator-alert activation: OPERATOR-GATED (no authorization received this block). All requirements documented in `tasks/runbook_operator_alert_activation_2026_05_19.md` + `tasks/runbook_operator_alert_skill_patch_2026_05_21.md`; status anchor added to BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE.
- P6 park-list explicit: see new section in backlog.md.
- P7 final close-dev report: delivered to operator in conversation; this todo entry is the persisted artifact.

**Recommended final implementation path:** if historical-pool-selection probe finds confirmed by operator, then either Path 2 (CG Pro paid sample) or Path 3 (forward-only). Both deferred until operator decision; no implementation work scoped this block.

**Hermes-first:** N/A — close-development docs block, no new external service integration.

## Active Work: 2026-05-22 — Historical pool-selection probe packet (operator-authorized)

**Status:** PACKET-SHIPPED-AUTHORIZED. Operator authorized budget (max 10 GT free calls, 2-3 old tokens, findings-only). Packet: `tasks/probe_packet_historical_pool_selection_2026_05_22.md`.

Target tokens (Solana-only, non-pump, oldest first):
- A: 2025-10-20 CIPHER (cornerstone — same as failed oldest in PR #223 sample)
- B: 2026-01-06 `3wh5z...` (~4mo old)
- C: 2026-02-09 `J3DpH...` (~3mo old)

Call allocation: 4 + 3 + 3 = 10 calls hard cap. Per-token: 1 `/pools` + 2-3 `/ohlcv` against non-top-reserve pools.

**What ships in this PR:**
- `tasks/probe_packet_historical_pool_selection_2026_05_22.md`
- backlog status flip: `BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE` PROPOSED → **PACKET-SHIPPED-AUTHORIZED**
- todo.md session record

**What does NOT ship:**
- No vendor calls (operator runs the probe script next)
- No code change
- No prod DB writes

**Next:** operator runs the probe (~10 GT free calls); findings doc gets filed in a follow-up docs PR.

## Active Work: 2026-05-22 — GT findings correction + new historical-pool-selection BL

**Trigger:** operator ran the authorized lookback-cap probe (`BL-NEW-SOURCE-CALL-GT-LOOKBACK-CAP-PROBE`). 3 GT free OHLCV calls. No prod writes. Temp files cleaned.

**Result:** GT free returns 5m OHLCV at 60/120/180 days back for the CIPHER pool that originally 401'd. The "GT free has short lookback cap" diagnosis from §4 of the original findings is **REFUTED**.

**Honest correction:** the original 401 was specifically about THIS pool's OHLCV history start, not a global free-tier cap. Real blocker is V1 `current_reserve_proxy_v1` rule picking today's top-reserve pool, which may not have existed at `call_ts` for old `source_calls`.

**What ships in this PR:**
- §11 CORRECTION section appended to `findings_source_call_gt_sample_2026_05_22.md` (original §1-§10 preserved unedited for audit trail; §11 supersedes §4 Interpretation and §7 sub-finding)
- backlog status flip: `BL-NEW-SOURCE-CALL-GT-LOOKBACK-CAP-PROBE` → PROBE-RUN-REFUTED
- backlog status flip: `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT` → SAMPLE-RUN-FAILED-WITH-CORRECTION
- new BL: `BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE` (next deliverable)
- This todo.md entry

**What does NOT ship:**
- No code change
- No vendor calls
- No prod DB writes
- No implementation work

**Next:** await operator authorization for `BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE` (~5-10 GT free calls; findings-only).

## Active Work: 2026-05-22 — GT sample-run findings (BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT)

**Status:** SAMPLE-RUN-FAILED. 2/7 criteria failed (criterion 2 OHLCV-for-every-resolved + criterion 7 oldest-lookback). Implementation gate STAYS CLOSED. Findings recorded in `tasks/findings_source_call_gt_sample_2026_05_22.md`.

### Sample inputs (3 dex:* rows)
- Oldest 2025-10-20 Solana `Ciphern...` (non-pump): 20 pools resolved, OHLCV **401 / zero candles** → criterion 7 FAIL
- Median 2026-04-27 Solana `*pump`: 6 pools resolved, OHLCV **300 5m candles**, epoch-second → clean pass
- Newest 2026-05-21 Solana `*pump`: **0 pools** → classified `bonding_curve_pre_graduation_unverified` per packet §7

### Headline finding
GT free returns HTTP 401 on historical OHLCV beyond an undocumented recency cap. 7-month corpus cannot be backfilled via GT free as a single source.

### Three forward paths (operator-decided)
- [ ] Path 1: Narrow GT eligibility to recent call_ts only. Trigger: `BL-NEW-SOURCE-CALL-GT-LOOKBACK-CAP-PROBE` (binary-search GT cap, ~3 calls).
- [ ] Path 2: Try CG Pro for older history. Trigger: `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-PRO` (paid, ~$129/mo, packet TBD).
- [ ] Path 3: Treat GT as forward-only. Trigger: `BL-NEW-SOURCE-CALL-FORWARD-ONLY-COVERAGE` (plan + design + impl).

### What ships in this PR
- `tasks/findings_source_call_gt_sample_2026_05_22.md` (the evidence doc)
- backlog status flip for the parent BL (PACKET-SHIPPED → SAMPLE-RUN-FAILED)
- 3 new BL entries for the three forward paths
- todo.md session record

### What does NOT ship
- No `_fetch_snapshot_rows` change
- No schema migration
- No vendor calls (the sample script lives only in operator-local env)
- No prod DB writes

## Active Work: 2026-05-21 evening — BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT (docs only)

**Status:** PACKET-SHIPPED (PR pending). Docs only. No code. No vendor calls. No prod DB writes.

Workflow followed:
- [x] P0 state + drift-check (master HEAD `5ed9bdb`, source_calls 1323 rows, 0 forward coverage, no in-tree OHLCV client, no intraday table)
- [x] P1 Hermes-first vendor docs eval (CG MCP, GT public, awesome-hermes-agent, srilu skills — 20+ installed, none own OHLCV)
- [x] Plan v1 drafted
- [x] Plan v1 review by 2 vectors: A (temporal integrity / measurement) + B (Hermes-first / cost / safety)
- [x] Plan v2 — folded all Critical + Important findings (12+ folds)
- [x] Design v1 drafted
- [x] Design v1 review by 2 vectors: A (source identity / chain correctness, verdict REWORK) + B (impl risk / prod safety / rollback, verdict APPROVE_WITH_FOLDS)
- [x] Design v2 — folded Reviewer A's REWORK on `reserve_in_usd` semantics + chain map gaps + 12 other findings
- [x] Vendor decision packet written
- [x] `.gitignore` entry for `tasks/vendor_samples/`
- [x] Backlog entries filed (CG/GT sample + identity-resolution upstream + chain-map-extension)
- [ ] PR opened
- [ ] PR review by 2 parallel reviewers
- [ ] Operator decision on 5 pre-sample items (§3 of packet)
- [ ] Operator authorization of sample call (or not)

Key findings surfaced (operator-facing):
1. **30m horizon is NOT a native candle interval.** Design pre-registers 30m as a *return* between two 5m candles, NEVER an OHLCV composite. Operator must confirm before any code ships.
2. **GT cannot return reserves at call_ts.** Pool selection is honestly downgraded to "current_reserve_proxy_v1" with `pool_drift_risk_flag` band based on `(now - call_ts).days`. Older call_ts → wider drift band.
3. **~15% V1 eligible ceiling.** Only 202/1323 source_calls rows can EVER be backfilled regardless of vendor (the dex:chain:contract subset). Recommendation: ACCEPT 202-row V1 ceiling AND file `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` upstream BL to unlock the other 85%.
4. **GT lookback cap unknown.** 7-month source-call corpus; sample probes the boundary via oldest-token (2025-10-20). Failure of criterion #7 → narrowed eligibility, operator-decided in follow-up plan.

Operator-gated next steps:
- Decisions §3 of packet (vendor target, ceiling acceptance, 30m policy, pool rule, sample budget)
- Authorize sample call OR reject
- If approved: future session writes `scripts/vendor_sample_gt.py` + runs ≤6 GT calls

## Active Work: 2026-05-21 autonomous build block (cockpit usability + silent-failure closure)

**Status:** SHIPPED-DEPLOYED. 9 PRs merged + deployed to srilu-vps (HEAD `c73eacf`). P0 soak verified clean (zero post-restart errors; x_alerts limit=80 0.18s p50; heartbeat advancing; lag-watchdog quiet).

### Shipped PRs
- [x] PR #211 — `feat(observability)`: writer cron-tick watchdog folded into existing lag-watchdog. Heartbeat-file mtime probe + three statuses + 6h `writer_never_fired` escalation. §12b log triplet + plain-prose remediation alert text.
- [x] PR #212 — `docs(operator-alert)`: concrete SKILL.md patch addendum (Step 5). Reduces activation from shape-level design to copy-paste. Still operator-gated on `OPERATOR_ALERT_HMAC_SECRET`.
- [x] PR #213 — `perf(dashboard)`: batched entry-price preload for `/api/x_alerts`. 400 queries → 5 queries. Set up the win; final 50× speedup landed with #215.
- [x] PR #214 — `feat(dashboard)`: read-only `/api/source_calls/health` endpoint. Aggregate rollup, no per-source identifiers (regression-tested).
- [x] PR #215 — `perf(dashboard)`: functional `UPPER(symbol)` indexes for x_alerts resolver. Post-deploy smoke revealed the resolver SCANning 2.5M-row volume_history_cg was the real bottleneck. Final: 9.30s → 0.18s p50 (~50× faster).
- [x] PR #216 — `hotfix(config)`: declare `WRITER_HEARTBEAT_FILE` in Settings. Pydantic `extra="forbid"` was crash-looping pipeline post-activation. Caught + fixed during the activation cycle.
- [x] PR #217 — `hotfix(wrappers)`: source `.env` early so cron's sparse env picks up `WRITER_HEARTBEAT_FILE`.
- [x] PR #218 — `hotfix(wrappers)`: reorder `source .env` BEFORE setting `DB_PATH` default — `.env` had `DB_PATH=scout.db` (relative) which `set -a` was clobbering the wrapper's absolute fallback.
- [x] PR #219 — `feat(dashboard)`: `SourceCallsHealthPanel.jsx` slotted into `HealthTab`. Renders writer freshness badge, counts, rates, outcome distribution, price-coverage bars, rankability blocker banner. 30s auto-refresh. Vite dist committed.

### Workflow followed
For each non-hotfix PR: plan → 2 parallel reviewers (orthogonal attack vectors) → fold Critical/Important → design → 2 parallel reviewers → fold → build → PR → 2 parallel reviewers → fold. Hotfix PRs shipped without the full workflow because they were 1-3 line fixes to flagged regressions, with explicit operator authorization ("execute all steps").

### Operator gates respected
- No trading-behavior changes
- No classifier changes
- No source ranking / pruning / "best source"
- No price-coverage implementation (gated on vendor sample approval per PR #208)
- No paid API calls
- No live config changes beyond approved activation (writer heartbeat env line)

### What's still operator-gated post-block
- `OPERATOR_ALERT_HMAC_SECRET` absent on srilu-vps; Hermes SKILL.md unchanged. PR #176 (endpoint) + PR #212 (docs) are merged but the wire is dormant. Activation runbook: `tasks/runbook_operator_alert_activation_2026_05_19.md` + `tasks/runbook_operator_alert_skill_patch_2026_05_21.md`.
- Source-call price-coverage implementation: gated on operator vendor-sample approval per PR #208's design doc.

### Prod state @ 2026-05-21T18:07Z
- HEAD `c73eacf`; gecko-pipeline + gecko-dashboard both `active`
- `/api/x_alerts?limit=80`: 0.16-0.18s p50 (target <2s)
- `/api/source_calls/health`: 0.03s
- Heartbeat last touched 18:05:02 (3 min ago, cron tick)
- Zero ALERT_SENT in last 1h
- Zero post-restart errors since 17:35Z
- 1316 source_calls rows; unresolvable_rate=98.78%; 0 sources rankable (20 below min_sample=10) — gate honestly surfaced via `not_rankable_label`

## Active Work: BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION post-merge status

- [x] Create isolated worktree from `origin/master`: `C:\projects\gecko-alpha-bl-new-source-call-price-coverage` on `plan/bl-new-source-call-price-coverage`.
- [x] Review `tasks/lessons.md` and apply Hermes-first installed-surface rule.
- [x] Run in-tree drift-check for historical price/cache writers and `_fetch_snapshot_rows`.
- [x] Query prod-copy `scout.db` for source-call coverage, identity, and table-shape evidence.
- [x] Complete Hermes-first/vendor documentation matrix without paid API calls.
- [x] Draft plan: `tasks/plan_source_call_price_coverage_expansion_2026_05_21.md`.
- [x] Get plan reviewed by 2 parallel agents and fold Critical/Important findings.
- [x] Draft design only after plan-review folds.
- [x] Get design reviewed by 2 parallel agents and fold Critical/Important findings.
- [x] Build gate evaluated: skipped implementation because design is not trivial and reviewers required non-code folds only.
- [x] Docs/status cleanup: backlog now records SOURCE-CALL-OUTCOME-LEDGER as MERGED / DEPLOYED / BACKFILLED / WRITER-LIVE / WATCHDOG-LIVE / PRICE-COVERAGE-LIMITED.
- [x] Open PR and record final verification/review results: https://github.com/Trivenidigital/gecko-alpha/pull/208
- [x] Post-merge status update: PR #208 merged as design record; backlog now says DESIGN-SHIPPED / IMPLEMENTATION-GATED.

Review:
- Baseline `uv run pytest --tb=short -q` did not reach tests because `uv` failed fetching `hatchling` from PyPI with TLS `UnknownIssuer`.
- Current prod-copy status: 1,253 `source_calls`; only 14 at-call prices; zero 1h/6h/24h forward returns; 1,237 unresolvable. No acceptance target is met today.
- Plan-review folds: OHLCV availability semantics, identity-eligible denominator, horizon-specific coverage, trust-tier dimension separation, CoinGecko MCP correction, and explicit vendor cost budgets.
- Design drafted: `tasks/design_source_call_price_coverage_expansion_2026_05_21.md`; build decision is no code in this PR because the substrate is not trivial.
- Design-review folds: as-of historical identity resolution, canonical/provider chain IDs, coverage label, liquidity evidence kind, idempotent observation key, canonical timestamp/epoch storage, valid-vs-missing CHECK constraints, 30m primary horizon, and migration idempotence requirements.
- Backlog cleanup evidence: `origin/master` has PR #206 `aaffa6b0` and PR #207 `df76d851`; srilu `/root/gecko-alpha` HEAD is `df76d85`; prod crontab includes source-call live writer and lag watchdog; prod DB has 1,254 `source_calls`, 14 `price_at_call`, 0 forward coverage.
- Verification: `git diff --check origin/master..HEAD` passed. `uv run pytest --tb=short -q` blocked before test collection because PyPI TLS `UnknownIssuer` prevented fetching `hatchling`.
- PR #208 opened. No PR-review subagents were dispatched because no code was built.
- PR #208 merged on 2026-05-21 as `d57f6d59`; GitHub Actions `test` check passed.
- Next implementation must not start until the operator explicitly authorizes the vendor sample/timestamp-semantics check.
- Scope remains plan/design-only unless reviewers explicitly clear a trivial implementation.

## Active Work: BL-NEW-SOURCE-CALL-OUTCOME-LEDGER

- [x] Resume branch and verify plan/design folds are present
- [x] Plan reviewed by 2 parallel agents; Critical/Important findings folded
- [x] Design reviewed by 2 parallel agents; Critical/Important findings folded
- [x] Write focused RED tests for migration, TG/X backfill, bounded outcomes, summaries, and §12a lag watchdog
- [x] Implement `source_calls` migration + source-quality helpers + watchdog scripts
- [x] Run focused and adjacent verification
- [x] Open draft PR #206
- [x] Get PR reviewed by 2 parallel agents and fold Critical/Important findings

Review notes:
- Scope is measurement substrate only: no dashboard endpoint, no frontend, no X/TG pruning, no trading behavior change.
- Hermes-first remains enforced: X/KOL collection/classification stays on Hermes; custom code only covers durable gecko-alpha attribution and summaries.
- PR review folds: preserve refreshed outcomes on backfill rerun; watchdog refuses missing DB/read-only opens; duplicate rows no longer skew summary averages; stale at-call prices suppress extrema; conflicted X heuristic linkage does not write a concrete paper-trade id.

Last updated: 2026-05-19 (cycle 15: overnight drift-cleanup audit — closed 12 stale items with inline evidence citations; 3 items annotated as STILL OPEN with elapsed-date flags after PR #82 was DEPLOY-VERIFIED 2026-05-19 via runtime srilu schema evidence; PR-stage R3 fold downgraded KEEP-ON verdicts to docs-only-PRESUMED per §9a)

## Audit closure (2026-05-19)

**Scope:** docs-only; no code, schema, scripts, settings, live config, or secrets touched. Per operator's overnight assignment Priority 1: "Create a docs-only PR that marks stale items closed/superseded or moves still-live items into a clear current section."

**Runtime-state caveat (per CLAUDE.md §9a):** All `ELAPSED-WITHOUT-REVERT` and `KEEP-ON-*` closures below rely on docs-only evidence (backlog.md entries, memory checkpoints, inline sneak-peek decisions). Prod runtime state on srilu (e.g., whether `STABLE_PAIRED_BONUS` was reverted, whether `.env` widened-lifecycle settings were rolled back) was NOT SSH-verified in this audit per scope. Closure verdicts express "no revert documented in source-of-truth surfaces I can read" — not "revert provably did not happen." Operator can disconfirm any closure by surfacing runtime evidence to the contrary.

**Closed in this audit (12 items, line numbers reflect post-edit positions; verdict downgraded from CONFIRMED→PRESUMED per PR-stage R3 fold + §9a):**
- BL-NEW-HELIUS cross-finding marker inside Moralis audit (L94) — Helius audit shipped AUDITED-PHANTOM 2026-05-18 per `backlog.md:981-986`.
- BL-NEW-QUOTE-PAIR D+3 mid-soak (L413) — ELAPSED-WITHOUT-REVERT (docs-only).
- BL-NEW-QUOTE-PAIR D+7 soak end (L414) — ELAPSED-WITHOUT-REVERT (docs-only).
- Paper-lifecycle widening soak end (L420) — KEEP-ON-PRESUMED (docs-only).
- PR #59 strategy tuning soak end (L421) — KEEP-ON-PRESUMED-PERMANENT (docs-only); commit `3c83fb7`.
- gainers_early reversal re-soak 7d (L422) — ELAPSED-AUTO-SUSPENDED per `backlog.md:1797-1798` (event-evidenced, not just docs-only).
- PR #59 duplicate re-check entry (L461) — duplicate of L421 head closure.
- BL-063 moonshot soak (L463) — duplicate of L419 head closure (KEEP-ON-PRESUMED-PERMANENT docs-only).
- BL-064 14d TG social soak (L464) — ELAPSED-OPERATIONAL-GAP; superseded by Narrative Scanner V1.1.
- Paper-lifecycle widening duplicate (L465) — duplicate of L420 head closure.
- narrative_prediction token_id divergence (L516) — duplicate of L521 head closure (PR #80 `eaf3523`, event-evidenced via commit SHA).
- first_signal revival decision (L524) — DECIDED-REVIVE-AND-SOAK per `backlog.md:1791`; 14d soak ends 2026-05-31 (operator-gated, do not pre-close).

**Runtime-verified after PR-stage R3 CRITICAL fold (1 item):**
- PR #82 BL-NEW-MOONSHOT-OPT-OUT deploy (L426) — DEPLOY-VERIFIED 2026-05-19. srilu `signal_params` has `moonshot_enabled INTEGER NOT NULL DEFAULT 1`; `paper_migrations` has `bl_moonshot_opt_out_v1|2026-05-09T16:15:22.266116+00:00`; `schema_version` has `20260507|bl_moonshot_opt_out_v1`. The earlier audit-row example was not the actual migration shape; `scout/db.py:_migrate_moonshot_opt_out_column` stamps `paper_migrations` + `schema_version`.

**Still open at audit time (3 items remain intentionally not closed after the 2026-05-19 runtime verification above; line numbers post-edit):**
- 2026-05-15 RE-SCOPED system health checkpoint (L433) — checkpoint date elapsed; operator-driven 3-question review still owed.
- PR #58 BL-064 lenient-safety soak (L459) — re-check window elapsed; closure deferred to operator-initiated retrospective.
- Audit fix #4 24h hard-exit (L518) — operator-deferred "accumulate more data first".

**Live operator-gated items NOT touched (per scope, no audit needed; line numbers post-edit):**
- BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE operator action (L63-65).
- BL-NEW-CG-LANE-ORDER-HELD-POSITION-FIRST residual + #158 24h validation (L112-113) — evidence-gated per assignment guardrail.
- BL-NEW-CRON-DRIFT-WATCHDOG operator scheduling (L237) — operator-gated.
- BL-NEW-SOCIAL-MENTIONS-DENOMINATOR operator B-vs-C response (L257) — operator-gated.

**Methodology:**
- Drift-check per CLAUDE.md §7a before each closure (no closure without file:line / PR / commit / memory / backlog evidence).
- Lever-vs-data-path attribution per CLAUDE.md §9c (gainers_early closure: visible lever was 2026-05-13 audit-id=24 KEEP-ON memory; controlling lever was PR #150 new evaluator contradicting it 2026-05-17).
- Conservative "leave-as-is and document" bias on operator-decision items.

**Reviewer signals:** plan-stage 2 reviewers (evidence-rigor + scope/blast-radius) flagged 2 CRITICAL (bulk-delete assumption + phantom PR #82 backlog grep) and 2 IMPORTANT (gainers_early disambiguation + line-number staleness); v2 plan folded all four before edits. See `tasks/plan_drift_cleanup_2026_05_19.md` for the full fold history.

**Findings-only audits (Priority 4, no code change per scope):**

- **BL-NEW-BL060-CYCLE-VERIFY:** AUDITED-CYCLE-INDEPENDENT 2026-05-19. Pacing is event-driven (per-trade-open) not time-driven. `scout/trading/paper.py:110` + `scout/trading/live_eligibility.py:104` (`WHERE would_be_live=1 AND status='open'` against `PAPER_LIVE_ELIGIBLE_SLOTS`). Comment at `live_eligibility.py:31-32`: "quality subset, not a FCFS-20 cap on the firehose." backlog.md flipped PROPOSED → AUDITED-CYCLE-INDEPENDENT in this PR.
- **BL-NEW-REVIVAL-VERDICT-WATCHDOG:** SCRIPT-SHIPPED / SCHEDULING-PENDING-OPERATOR 2026-05-19 (this PR). Design at PR #185 (commit `d0de684` after reviewer fold). Implementation: `scripts/revival-verdict-watchdog.sh` + `tests/test_revival_verdict_watchdog.py` (18 cases, 18/18 passing on VPS). Cron entry **NOT installed**; activation runbook at `cron/README.md` § "Revival-verdict-watchdog". Operator next actions: (1) smoke-test `bash scripts/revival-verdict-watchdog.sh` on srilu (expected exit 0 — 0 provisional rows today), (2) when ready, follow the activation runbook to install daily cron entry, (3) flip backlog status to `SCHEDULED` once cron is active.
- **BL-NEW-SOCIAL-DENOMINATOR-RE-EVAL-WATCHDOG:** PROPOSED (unchanged). Drift-check: `dashboard/db.py:1284-1319` + `dashboard/api.py:973,1013` + `dashboard/search.py:263` read `narrative_alerts_inbound` and `tg_social_messages` for display, but no watchdog primitive in `scripts/` or `cron/` covers the re-eval triggers. Status accurate; tied to operator B-vs-C decision per scope.
- **BL-NEW-SCORER-DEAD-SIGNAL-COMMENT-CONVENTION:** SHIPPED 2026-05-19. Drift-check: `scout/scorer.py:121` already carries `# DEAD SIGNAL — pending BL-NEW-SOCIAL-MENTIONS-DENOMINATOR-AUDIT re-eval` and `scorer.py` has the original Signal 13 (CryptoPanic) gated-comment precedent. This PR codifies the convention in `docs/gecko-alpha-alignment.md` so future scorer audits do not rediscover it from examples only.
## Active Work: 2026-05-19 profit-pattern segmentation

- [x] Review project lessons and isolate branch for analysis
- [x] Confirm local `scout.db` is schema-only and not usable for outcome segmentation
- [x] Pull production outcome aggregates without modifying prod DB
- [x] Segment profitable and junk patterns across requested dimensions
- [x] Propose Actionability Gate v1, dashboard fields, and paper-trade rule changes
- [x] Record final verification/results here

Review:
- Findings written to `tasks/findings_profit_patterns_2026_05_19.md`.
- Prod analysis used read-only SQLite access through `/tmp/analyze_profit_patterns.py`; no production DB writes.
- Primary cohort: 531 current-regime closed trades since `2026-05-01 14:06:00`, +$1,545.85 net, +$2.91/trade, 58.8% win.
- Best current-regime signal types: `narrative_prediction` (+$1,294.96 / n=78), `chain_completed` (+$1,123.15 / n=16), `volume_spike` (+$593.88 / n=28).
- Worst current-regime signal types/cells: `losers_contrarian` (-$803.22 / n=146), `gainers_early` (-$382.93 / n=252), `gainers_early + mcap:5-10m` (-$701.77 / n=49), `gainers_early + confluence:3` (-$468.14 / n=37).
- Data gaps: X handle and liquidity are not rankable from closed trade outcomes; X alerts have 215 rows but 0 priced outcomes due unresolved `resolved_coin_id`; TG channel has only 2 current-regime closed linked trades.

## Active Work: BL-NEW-ACTIONABILITY-VISIBILITY-VALIDATION

- [x] Isolated worktree created: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-actionability-visibility-validation` on `codex/actionability-visibility-validation`.
- [x] Baseline relevant suite: `42 passed`.
- [x] PR #181 deployed to srilu-vps: `/root/gecko-alpha` fast-forwarded `ec4f35c` -> `7506adc`; `gecko-pipeline` and `gecko-dashboard` active.
- [x] DB migration verified on srilu-vps: `paper_trades.actionable`, `actionability_reason`, and `actionability_version` present; marker `bl_new_actionability_gate_v1` cut over at `2026-05-19T11:39:09.121422+00:00`.
- [x] Fresh post-deploy paper-trade row stamp verified.
- [x] Plan drafted: `tasks/plan_actionability_visibility_validation.md`.
- [x] TDD backend dashboard API support complete.
- [x] Dashboard UI support complete.
- [x] Validation runbook complete.
- [x] Verification complete.
- [x] PR opened: https://github.com/Trivenidigital/gecko-alpha/pull/182

Review:
- Fresh-row verification: post-deploy rows `2206`, `2207`, and `2208` all stamped `actionable`, `actionability_reason`, and `actionability_version='v1'`; row `2208` is actionable (`v1_pass_core_signal_mcap_10_50m`), rows `2206`/`2207` are exploratory (`v1_block_tg_social_low_n`).
- Backend red/green: 4 new tests failed before implementation, then passed after adding row fields, history filter/count semantics, and `/api/trading/actionability`.
- Full dashboard API test file after backend slice: `24 passed`.
- Frontend build after Trading tab UI change: `npm run build` passed; Vite emitted `dist/assets/index-Ca4N1ClP.js`.
- Local dashboard smoke: temp-DB server on `127.0.0.1:8765` returned 200 for `/` and `/api/trading/actionability`.
- Final targeted suite: `46 passed`.
- Final diff hygiene: `git diff --check` clean.
- Validation runbook drafted: `tasks/runbook_actionability_validation_2026_05_19.md`.
- PR #182 CI `test` passed in `3m56s`.
- Scope remains visibility + validation only. Do not suppress exploratory paper trades or change live/capital allocation in this slice.

## Active Work: BL-NEW-ACTIONABILITY-GATE-V1

- [x] Isolated worktree created: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-actionability-gate-v1` on `codex/actionability-gate-v1`
- [x] Audit artifacts cherry-picked from `3fb6084`
- [x] Baseline relevant suite via shared venv: `51 passed, 1 skipped, 1 warning`
- [x] Drift check: existing `would_be_live` is live-slot eligibility, not actionability
- [x] Hermes-first check: no actionability-gate primitive; reuse Hermes X/KOL only as raw telemetry
- [x] Plan drafted: `tasks/plan_actionability_gate_v1.md`
- [x] Plan reviewed by 2 parallel agents; structural BLOCK folded into revised plan
- [x] Design drafted and reviewed by 2 parallel agents; integration findings folded
- [x] TDD implementation complete
- [x] PR opened and reviewed by 3 parallel agents

Review:
- Plan reviewer A (`APPROVE_WITH_CHANGES`) flagged chain-completed mcap support and gainers_early mcap ambiguity.
- Plan reviewer B (`BLOCK`) flagged engine signal-data mismatch, nullable schema semantics, missing migration marker assertion, and insufficient engine-path tests.
- Folded into `tasks/plan_actionability_gate_v1.md`: DB mcap enrichment, chain-completed missing-mcap exception, gainers_early `10-50m` observe block, nullable actionability columns, marker idempotence tests, and real engine-path fallback tests.
- Design reviewers both returned `APPROVE_WITH_CHANGES`; folded volume-spike mcap carry-forward, stack-failure fail-closed metadata for `gainers_early`, non-suppressing actionability exception policy, existing-DB upgrade test, and persisted `signal_data` immutability test.
- Implementation commits:
  - `008b734` `feat: add actionability gate classifier`
  - `14fa4c0` `feat: add actionability paper-trade columns`
  - `bd3b3fe` `feat: stamp paper-trade actionability`
- Verification:
  - Post-rebase focused suite: `82 passed, 1 skipped, 1 warning`
  - Post-rebase adjacent trading suite: `329 passed, 1 skipped, 1 warning`
  - Warning is the existing `aiosqlite` event-loop-closed thread warning from `tests/test_trading_db_migration.py::test_post_migration_assertion_raises_on_incomplete_schema`.
- Deferrals: X/TG ranking waits for outcome linkage; `peak_pct < 5` risk handling waits for a separate exit-policy design; dashboard UI deferred; live trading policy unchanged.
- PR #181 opened: https://github.com/Trivenidigital/gecko-alpha/pull/181
- PR review:
  - Structural/migration reviewer: `APPROVE`, no findings; targeted `75 passed, 1 skipped, 1 warning`; full suite with dummy secrets `2455 passed, 80 skipped, 12 warnings`.
  - Operational/silent-failure reviewer: `APPROVE`, no findings; targeted `24 passed`; full suite with `UV_NATIVE_TLS=true` + dummy secrets `2455 passed, 80 skipped, 12 warnings`.
  - Behavioral reviewer: `APPROVE_WITH_CHANGES`; fixed invalid non-numeric mcap key skipping DB enrichment and added `tg_social` classifier coverage.
- Post-review fix verification:
  - Focused suite: `84 passed, 1 skipped, 1 warning`
  - Adjacent trading suite: `331 passed, 1 skipped, 1 warning`
- PR #181 pre-merge status: CI `test` green; merge state clean; no unresolved GitHub comments.
- Post-merge follow-ups for next session:
  - Deploy and verify one fresh paper-trade open has `actionable`, `actionability_reason`, and `actionability_version`.
  - Add small dashboard/reporting pass: actionable vs exploratory counts, reasons, and table filter by actionable status.
  - Run 24-48h post-deploy comparison: actionable cohort PnL, exploratory cohort PnL, and false negatives among exploratory winners.
  - Do not tighten live/paper entry suppression yet; let the classifier collect evidence first.

Last updated: 2026-05-18 (cycle 14: narrative-operator-alert-wire + chain-anchor status correction + Helius + Moralis plan audits + CG budget attribution + stale PR triage)

## Active Work: BL-NEW-PEAK-GIVEBACK-FRESHNESS-FILTER-AUDIT

- [x] Isolated worktree created: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-peak-giveback-freshness-audit` on `analysis/peak-giveback-freshness-audit-2026-05-19`
- [x] Guardrail accepted: no suppression, entry-rule, sizing, capital-allocation, live-trading, or gate-threshold changes before the 24h actionability validation
- [x] Drift check: existing `scripts/analyze_profit_patterns.py` and `tasks/findings_profit_patterns_2026_05_19.md` cover broad profit buckets but not a pre-entry TTL/giveback threshold sweep
- [x] Production schema checked read-only for `paper_trades` and snapshot price sources
- [x] Run read-only stale-entry threshold sweep on production `scout.db`
- [x] Draft findings doc with proposed V2 gate candidates only
- [x] Verify script syntax/output and document verification limits

Review:

- `python -m py_compile scripts/audit_peak_giveback_freshness.py` passed locally.
- `git diff --check` passed.
- Production read-only audit ran against `/root/gecko-alpha/scout.db` for both current-regime and all-history windows; output captured locally in ignored `.ssh_out_peak_audit_*.txt` files.
- `uv run pytest tests/test_trading_db.py -q` did not reach tests in the fresh worktree because uv failed to fetch `hatchling` from PyPI due `invalid peer certificate: UnknownIssuer`.

## Active Work: BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE (ENDPOINT-SHIPPED / HERMES-SKILL-PENDING)

- [x] Drift-check: existing `_verify_hmac` in `scout/api/narrative.py:121` is already V2-PR-review hardened (query-string binding, body-size cap, timestamp window, replay LRU, structured rejection logs). Reuse; no duplicate hardening.
- [x] Hermes-first re-check 2026-05-18 (4 surfaces): installed VPS skills (0 hits), Hermes optional-skills catalog (`telephony` exists but is SMS/voice not chat/webhook-out), awesome-hermes-agent (`hermes-ai-infrastructure-monitoring-toolkit` candidate 404'd + per its description is a standalone cron toolkit, not an importable library), `devops/webhook-subscriptions` confirmed INBOUND-only. Verdict carry-forward from 2026-05-13: no Hermes path. Wire into in-tree `scout.alerter`.
- [x] Evidence gate verified: `SELECT COUNT(*) FROM narrative_alerts_inbound` returned 204 vs ≥10 threshold (gate fired 20×).
- [x] Implementation: `scout/api/internal_alert.py` (new). POST `/api/internal/operator-alert`. Reuses `_verify_hmac` + `_reject` from narrative.py. Calls `send_telegram_message(parse_mode=None, raise_on_failure=True)`. §12b log triplet: `operator_alert_dispatched` (before TG call) → `operator_alert_delivered` (on success) OR `operator_alert_failed` (on exception). 502 to caller on delivery failure so the Hermes side doesn't silently swallow.
- [x] Dashboard wiring: `dashboard/api.py` mounts the new router with the same stub-503 pattern as the narrative router.
- [x] Tests: `tests/test_internal_alert_api.py` covers 503 (disabled), 401 (missing headers), 403 (bad sig), 409 (replay), 400 (bad payload), 200 (delivery success + log triplet order), 502 (delivery failure + failed log), and 4 secret-leakage scans (success / auth-fail / delivery-fail / disabled paths).
- [x] Backlog status flipped PROPOSED → PR-OPEN.
- [x] **Reviewer 1 P1 fold:** new `OPERATOR_ALERT_HMAC_SECRET` Settings field; `_verify_hmac` parameterized via `secret_field` / `feature_label` kwargs with narrative defaults preserved; internal-alert endpoint authenticates against its own secret so it can still raise alerts when `NARRATIVE_SCANNER_HMAC_SECRET` is missing/broken (the exact failure mode this endpoint exists to surface). 4 new tests cover gate-independence + 503 detail accuracy + narrative-default-preserved regression.
- [x] CI green on the new code + tests commit (CI was green on `f4b7b0b`; merged as `012e67c` 2026-05-18T23:52:24Z).
- [x] **Phased post-merge status (Reviewer 1 P2 fold):** backlog flipped to `ENDPOINT-SHIPPED / HERMES-SKILL-PENDING` — NOT full SHIPPED. The Hermes-side dispatcher still uses Path B log-only until the SKILL.md update lands.
- [ ] Operator sets `OPERATOR_ALERT_HMAC_SECRET` on srilu `.env` (32-byte hex, e.g. via `python3 -c "import secrets; print(secrets.token_hex(32))"`) AND configures the Hermes dispatcher's SKILL.md with the same value.
- [ ] Operator runs SKILL.md update on srilu (`/home/gecko-agent/.hermes/skills/narrative_alert_dispatcher/SKILL.md`) to switch dispatcher from `narrative_dispatcher_misconfig` log-only to the active endpoint — out of repo, operator action after PR merges.
- [ ] Operator runs smoke test confirming the dispatcher's HMAC POST reaches `/api/internal/operator-alert` and `operator_alert_dispatched` fires on gecko-alpha. Only then flip to `SHIPPED`.

## Active Work: BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX — SHIPPED (backlog status correction)

- [x] Drift-check post-Helius-audit: `active_chains` rows/day = 11 → 104 → 117 across 2026-05-16/17/18 (vs audit's claimed MAX 2026-05-11T16:42Z). Symptom resolved.
- [x] PR-history check found PR #146 (`5860d17`) merged 2026-05-17T16:50:40Z — the causal fix. Backlog entry was stale at `PR-READY`; flipped to `SHIPPED`.
- [x] Verified `chain-anchor-health-watchdog.{sh,service,timer}` shipped with PR #146 in `scripts/` and `systemd/` directories on master.
- [x] Findings doc: `tasks/findings_chain_anchor_resolved_2026_05_18.md` — status correction + post-deploy verification + §9c near-miss note (drift-check almost filed a duplicate watchdog follow-up).
- [x] **No follow-up filed.** Recurrence-prevention watchdog already shipped via PR #146. If a future audit shows the watchdog is missing a surface or threshold, file the gap then.

## Active Work: BL-NEW-HELIUS-PLAN-AUDIT — AUDITED-PHANTOM

- [x] Drift-check: `scout/ingestion/holder_enricher.py:32-68` (Solana branch + `_enrich_solana` JSON-RPC `getTokenAccounts`) + `scout/config.py:129` + per-cycle fan-out at `main.py:944-948`. No throttle/cache/interval primitives.
- [x] Runtime-state verification (srilu-vps): `HELIUS_API_KEY=` empty; 0 Helius log hits in 24h; `holder_snapshots` table = 0 rows total.
- [x] Cohort calibration (24h DB): 177 Solana candidates; 7d = 621. If-enabled projection at today's 12 cycles/hr × audit's 121 solana/cycle figure: ~35k/day × 30 ≈ **~1.05M/month**. Helius Free plan = **1M monthly credits** per current docs (Reviewer 1 P1 correction — earlier ~100k/day reference was stale). At-or-marginally-above the cap at today's rate; ~2.6× over at 30 cycles/hr; ~5.2× over at audit's 60 cycles/hr. Rate-limit envelope (10 req/s) is not binding.
- [x] Hermes-first (4 surfaces, 2026-05-18): installed VPS skills (0 helius/Solana-holder hits), Hermes optional-skills catalog `blockchain/solana` (partial top-5 holders only, no `getTokenAccounts` full-count, not installed), awesome-hermes-agent (no Helius/Solana RPC entry), GoldRush/Covalent (no SPL holder enumeration). Verdict: keep in-tree path.
- [x] Findings doc: `tasks/findings_helius_plan_audit_2026_05_18.md`.
- [x] Backlog flipped PROPOSED → AUDITED-PHANTOM with full evidence + rate-dependent nuance.
- [x] Conditional follow-up filed: BL-NEW-HELIUS-ENABLEMENT-GUARDRAIL (operator checklist includes plan-tier check AND cycle-rate check before enablement).

## Active Work: BL-NEW-MORALIS-PLAN-AUDIT — AUDITED-PHANTOM

- [x] Drift-check: `scout/ingestion/holder_enricher.py:13-95` + `scout/config.py:130` + per-cycle fan-out at `main.py:944-948`. No throttle/cache/interval primitives.
- [x] Runtime-state verification (srilu-vps): `MORALIS_API_KEY=` empty; 0 Moralis log hits in 24h and 7d; `holder_snapshots` table = 0 rows total.
- [x] Cohort calibration (24h DB): 12 ethereum + 18 base + 0 polygon = 30 EVM-mappable tokens. If-enabled projection: ~200-260k calls/month (5-7× over legacy-free 40k cap; lower than audit's original 25× projection because actual cycle rate is ~12/hr not 60/hr).
- [x] Hermes-first (4 surfaces, 2026-05-18): installed VPS skills (0 hits across 28 dirs), Hermes optional-skills catalog (`blockchain/evm` not installed, doesn't cover holders), awesome-hermes-agent (no holder skill), GoldRush/Covalent agent-skills (4 skills surveyed; no ERC20 holder-count capability). Verdict: keep in-tree path.
- [x] Findings doc: `tasks/findings_moralis_plan_audit_2026_05_18.md`.
- [x] Backlog flipped PROPOSED → AUDITED-PHANTOM with full evidence summary.
- [x] Conditional follow-up filed: BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL (trigger = operator intent to enable; 6mo backstop).
- [x] Cross-finding (separate task): BL-NEW-HELIUS-PLAN-AUDIT likely same shape — `HELIUS_API_KEY=` also empty, `holder_snapshots` covers both chains. Out of scope for this audit per assignment guardrail. **CLOSED 2026-05-19 (audit): BL-NEW-HELIUS-PLAN-AUDIT shipped AUDITED-PHANTOM 2026-05-18 via PR #174 per `backlog.md:981-986`. The "same shape" hypothesis was confirmed (key empty, 0 log hits, 0 `holder_snapshots` rows). Conditional follow-up filed as BL-NEW-HELIUS-ENABLEMENT-GUARDRAIL.**

## Active Work: BL-NEW-CG-LANE-ORDER-HELD-POSITION-FIRST (PR #170)

- [x] Drift-check: existing primitive `_fetch_coingecko_lanes` in scout/main.py:628-667; ratified shape from PR #131; no new primitive needed.
- [x] Hermes-first: same domain as BL-NEW-CG-RATE-LIMITER-BURST-PROFILE; design doc's prior negative check (no Hermes lane-orchestration primitive) carries forward.
- [x] VPS log attribution (2h window): 9 cycles; 42 `cg_429_backoff`; 9 `coingecko_lanes_stopped_for_backoff`. Cycle 1 (post-flip) succeeded due to fortuitous 40.9s pre-cycle backoff; cycles 2+ failed wholesale (refreshed_count=0, not_found_count=145-147).
- [x] Root cause: held_position runs LAST in `_fetch_coingecko_lanes`; scanner lanes consume ~7-10 calls of the 6/min budget before /simple/price fires; CG IP-rate-limit window is saturated by the time held_position arrives.
- [x] Code fix: reorder so `fetch_held_position_prices` runs FIRST; preserve `held_position_raw` in every stop-on-backoff early-return path.
- [x] Tests: 3 new tests in `tests/test_main.py` covering (held-first happy path / scanner-skipped-when-held-trips-backoff / scanner-backoff-after-held-preserves-payload).
- [x] Findings doc: `tasks/findings_cg_budget_attribution_2026_05_18.md`.
- [x] Backlog entry: `BL-NEW-CG-LANE-ORDER-HELD-POSITION-FIRST` filed.
- [x] PR #170 opened against master.
- [x] CI green: GitHub Actions Tests workflow `SUCCESS` on `feat/cg-budget-attribution`.
- [x] Reviewer 1 doc/status fold applied: fresh Hermes-first check (3 surfaces clean on 2026-05-18) + backlog flipped PROPOSED→PR-OPEN + this CI box.
- [x] PR #170 merged at `47f0835` on 2026-05-18T18:38:58Z; deployed to srilu-vps at 18:39:46Z (pycache cleared + restart).
- [x] 3-consecutive-clean gate met: cycles 1-3 post-deploy (18:41-18:47Z) `refreshed=148/147/147`; `simple_price_missing_ids=[]`.
- [x] Backlog flipped PR-OPEN → SHIPPED with post-deploy evidence summary; follow-up filed (BL-NEW-CG-FREE-TIER-DEMO-API-KEY).
- [ ] **Residual:** cycles 4-8 of the 30min sample window (18:50-18:59Z) showed wholesale failure recurrence during a sustained CG IP-rate-limit cooldown; held_position now hits 5/13 `coingecko_lanes_stopped_for_backoff` events (separate counter from 12 `cg_429_backoff` events — lane-stop fires at each cycle-lane boundary where the limiter is still in cooldown, so a single 429 can produce multiple lane-stop detections). Net success rate ~55% (vs ~10% pre-fix) — material improvement but partial fix. Next: operator-only Demo API key registration per BL-NEW-CG-FREE-TIER-DEMO-API-KEY.
- [ ] **#158 24h validation:** STILL OPEN — extended journal evidence outside sustained 429 windows required.

## Stale PR triage (2026-05-18)

| PR | Verdict | Action |
|---|---|---|
| #117 (overnight repo review findings) | OBSOLETE | CLOSED with evidence |
| #118 (clickable X alert assets) | SUPERSEDED (already on master) | CLOSED with evidence |
| #32 (LunarCrush drop + Sprint 1 promotion) | SUPERSEDED (already on master via #152) | CLOSED with evidence |
| #105 (Phase B daily audit snapshot, WIP) | STILL VALUABLE | COMMENTED with rebase recommendation; left open |
| #34 (BL-051 DexScreener top-boosts) | STILL VALUABLE | COMMENTED with rebase recommendation; left open |
| #33 (BL-050 paper-trade edge detection) | STILL VALUABLE | COMMENTED with rebase recommendation; left open |

Triage rationale: STILL VALUABLE entries are not small per operator guardrail ("rebase/build only if clearly still valuable and small"). Conflict surfaces span schema migrations, scorer refactors, cron-layout pattern changes — operator decides rebase priority.

## Active Work: items 1 + 2 (PR #155) — BL-NEW-AUDIT-SURFACE-ADDENDUM + BL-NEW-POLYMARKET-VERIFY

- [x] **Item 1: BL-NEW-AUDIT-SURFACE-ADDENDUM**: 5-category mini-sweep clean (nginx/caddy not-found, /etc/systemd/system.conf only [Manager], /etc/apt/sources.list.d/ minimal, docker/containerd not-found, systemd inventory matches cycle-6 captures). Status PROPOSED → AUDITED 2026-05-18. Findings: `tasks/findings_audit_surface_addendum_2026_05_18.md`.
- [x] **Item 2: BL-NEW-POLYMARKET-VERIFY**: `/opt/polymarket-ml-signal/` does NOT exist; stale cron entry confirmed (outside gecko-alpha managed block, silently failing every 6h). Status PROPOSED → AUDITED 2026-05-18. Findings: `tasks/findings_polymarket_verify_2026_05_18.md`. Operator-pastable removal command embedded.

## Active Work: items 4 + 5 (PR #157) — BL-NEW-SETTINGS-IMMUTABILITY + BL-NEW-DEX-PRICE-COVERAGE audit findings

- [x] Isolated worktree
- [x] Drift-check + Hermes-first
- [x] Cross-tree mutation-site grep: 1 production (main.py:1534 CLI override) + ~10 test + 1 monkeypatch + ~25 Settings(**defaults) constructors
- [x] Classification: 0 unsafe mutations of validator-relevant fields
- [x] Recommendation: do NOT implement frozen=True (cost > benefit; hypothetical-only protection)
- [x] DEX coverage audit: pure-DEX/contract-address held cohort remains empty; refresh-rate gap follow-up is superseded by shipped PR #158.
- [x] Findings docs + backlog flip + follow-up filing
- [x] PR #157 opened
- [x] Post-merge bookkeeping: PR #157 squash-merged to master at `be36bfb` on 2026-05-18; audit findings are now landed.

## Active Work: BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT

- [x] Evidence gate respected: no implementation started before manual `/coins/{id}` probe.
- [x] Manual VPS probe after rate-limit window: `pythia` and `iagon` returned HTTP 200 with USD prices; `superwalk` hit HTTP 429.
- [x] Hermes-first: optional Hermes blockchain skills use CoinGecko-backed price lookup, but no skill replaces gecko-alpha's in-process held-position `price_cache` fallback.
- [x] Minimal fallback design added to PR #163: `tasks/design_held_position_fallback_coins_endpoint.md`.
- [x] Backlog status updated to `DESIGN-READY 2026-05-18 — PR #163 merged 2f8f187`; implementation remains gated on PR #158 post-deploy `simple_price_missing_ids` evidence.
- [x] 2026-05-18 post-flip follow-up: `simple_price_missing_ids` now appears during active CoinGecko 429/backoff, so fallback implementation remains blocked until misses recur outside rate-limit windows and `/coins/{id}` probes fit the budget.

## Active Work: BL-NEW-HELD-POSITION-REFRESH-RATE-GAP (PR #158)

- [x] Isolated worktree
- [x] Drift-check + Hermes-first (no relevant Hermes primitive)
- [x] Empirical diagnosis via srilu SQL: 21/148 stale opens are CG-lane-EXCLUSIVE (0/21 in gainers_snapshots or trending_snapshots over 24h with 4617+645 entries). Stale-source hypothesis confirmed.
- [x] Plan v2 (post-2-reviewer fold): all CRITICAL findings folded (KeyError on updated_at via new `_get_cached_price_ages` helper, `parse_iso` → `datetime.fromisoformat`, Task 4 descoped pending CG-rate-limit-clear verification)
- [x] Build: `_get_cached_price_ages` helper + `stale_open_count` gauge + per-token persistent-stale WARN with 24h dedup + 1 new Settings key + `_reset_warned_today_for_tests`
- [x] TDD: 27/27 tests pass on srilu Python 3.12.3 (21 existing + 6 new using `structlog.testing.capture_logs()`)
- [x] Findings doc with empirical evidence + post-deploy soak plan
- [x] backlog.md: new entry filed with PR-OPEN/SCRIPT-READY status + 2 evidence-gated follow-ups (`BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` + `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT`)
- [x] 2026-05-18 validation deployment check: VPS `/root/gecko-alpha` reached master `147cba4`, but validation is blocked because effective config has `HELD_POSITION_PRICE_REFRESH_ENABLED=False` (no `.env` or systemd override); no `held_position_refresh_summary` / `simple_price_missing_ids` evidence collected and 24h validation remains incomplete.
- [x] 2026-05-18 operator flip: VPS `.env` now has `HELD_POSITION_PRICE_REFRESH_ENABLED=True` + `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES=1`; first cycle refreshed 150/150, later cycles hit repeated CG 429/backoff with `refreshed_count=0` and 25-26 `simple_price_missing_ids`; 24h validation remains incomplete.
- [x] Validation prep doc added via PR #163: `tasks/validation_pr158_held_position_refresh_rate_gap.md` with two-step SSH commands, required journal fields, stale-cohort overlap comparison, and `/coins/{id}` fallback promotion gate.
- [x] PR #158 created + 3 PR reviewers folded; operator P1/P2 false-positive/tz-normalization fold landed.
- [x] Post-merge: bookkeeping flip per cycle-12+13 convention (`SHIPPED 2026-05-18 — PR #158 merged a649032`)

## Active Work: BL-NEW-PARSE-MODE-AUDIT-EXTEND-URLLIB-DISPATCH

- [x] Isolated worktree: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-urllib-parse-mode` on `codex/urllib-parse-mode-audit`
- [x] Dependency check: `scout/config_alert.py` exists on PR #160 / `origin/feat/settings-validation-alert`, not on `origin/master`; this PR is stacked on PR #160.
- [x] Drift-check: PR #160 already filed the follow-up at `backlog.md` (`BL-NEW-PARSE-MODE-AUDIT-EXTEND-URLLIB-DISPATCH`). Current AST harness covers `send_telegram_message(...)` and `.post(.../sendMessage)` but not `urllib.request.urlopen(Request(...sendMessage...))`.
- [x] Hermes-first: Hermes messaging/gateway docs cover Telegram as a platform, but no Hermes skill replaces gecko-alpha's Python AST parse-mode hygiene test. awesome-hermes-agent lists messaging integrations, not in-repo AST audit enforcement. Verdict: extend the local test harness.
- [x] Baseline: `python -m pytest tests/test_parse_mode_hygiene.py tests/test_config_alert.py -q` -> `33 passed, 3 warnings`.
- [x] Write failing regression proving `config_alert.py`'s urllib `Request(...sendMessage...)` site is audited structurally.
- [x] Extend AST scanner to resolve urllib `Request` + `urlopen` dispatch payloads and enforce plain-text/no `parse_mode`.
- [x] Verify parse-mode and config-alert targeted tests.
- [x] Update backlog/memory review notes, commit, push, and create stacked PR: #162 (`https://github.com/Trivenidigital/gecko-alpha/pull/162`).
- [x] Post-merge: PR #162 squash-merged to master at `54da462` on 2026-05-18; backlog status flipped to `SHIPPED`.

Review:
- TDD red: `test_config_alert_urllib_dispatch_is_structurally_audited_as_plain_text` first failed because `_find_urllib_telegram_dispatches` did not exist, then failed with `len(dispatches) == 0` until module-level constants were added to the resolver.
- Implementation: `tests/test_parse_mode_hygiene.py` now resolves `urllib.request.urlopen(req)` where `req` is a `urllib.request.Request(...)`, resolves `ALERT_URL_FMT.format(...)`, unwraps `json.dumps({...}).encode("utf-8")`, and fails if the Telegram payload is unresolved or contains `parse_mode`.
- Verification: `python -m pytest tests/test_parse_mode_hygiene.py tests/test_config_alert.py -q` -> `34 passed, 3 warnings`.
- `git diff --check` clean. `python -m black tests/test_parse_mode_hygiene.py --check` could not run because this Python environment does not have `black` installed.

## Active Work: BL-NEW-SETTINGS-VALIDATION-ALERT (PR #160)

- [x] Isolated worktree: `.claude/worktrees/feat-settings-validation-alert`
- [x] Drift-check + Hermes-first (Hermes has no python-stdlib Telegram-push primitive; in-tree curl-direct pattern)
- [x] Plan + 2-reviewer fold (R1 timeout-3s + os.environ caveat; R2 CRITICAL mock-target specification + hashlib scope + 4 coverage adds + autouse fixture)
- [x] TDD: 18 tests RED (ModuleNotFoundError) → implement `scout/config_alert.py` + wire `scout/config.py:load_settings()` → 18/18 GREEN on srilu Python 3.12.3
- [x] Existing `tests/test_config.py` regression-free (75/77 pass; 2 fails are pre-existing `test_coingecko_config_defaults` — verified on origin/master)
- [x] `backlog.md` PROPOSED → PR-OPEN
- [x] PR #160 created + reviewers folded; squash-merged to master at `788059a` on 2026-05-18
- [x] Post-merge: bookkeeping flip (`SHIPPED 2026-05-18 — PR #160 merged 788059a`)

## Active Work: BL-NEW-CRON-DRIFT-WATCHDOG-ENV-WHITESPACE-TOLERANCE

- [x] Isolated worktree: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-cron-env-whitespace` on `codex/cron-env-whitespace-tolerance`
- [x] Merge/bookkeeping hygiene: PRs #158/#159/#160 checked via `gh pr view`; all remain OPEN, so no status-flip PR is applicable yet.
- [x] Dependency check: target script exists on `origin/feat/cron-drift-watchdog` / PR #156, not on `origin/master`; this PR is stacked on PR #156 rather than duplicating the cron watchdog on master.
- [x] Drift-check: `rg`/`git grep` found no existing `BL-NEW-CRON-DRIFT-WATCHDOG-ENV-WHITESPACE-TOLERANCE`; `scripts/cron-drift-watchdog.sh` still uses strict `^TELEGRAM_*=` parsing while PR #159's `scripts/systemd-drift-watchdog.sh` uses `[[:space:]]*` tolerance.
- [x] Hermes-first: Hermes Cron supports scheduled script-only jobs and Telegram delivery, but does not replace gecko-alpha's repo-vs-live crontab diff or `.env` credential parsing. Hermes Watchers cover RSS/JSON/GitHub watermarks, not local crontab drift. awesome-hermes-agent has no crontab-drift parser replacement. Verdict: small in-tree parity fix.
- [x] Write failing test for leading-whitespace `.env` token/chat parsing on the cron watchdog prod curl path.
- [x] Implement minimal parsing parity with PR #159's systemd watchdog.
- [x] Verify targeted tests and source-level parse-mode guard.
- [x] Update backlog/memory review notes, commit, push, and create stacked PR: #161 (`https://github.com/Trivenidigital/gecko-alpha/pull/161`).
- [x] Post-merge: PR #161 squash-merged to master at `01efcbd` on 2026-05-18; backlog status flipped to `SHIPPED`.

Review:
- TDD red evidence: Git Bash stub run with indented `TELEGRAM_*` keys exited before curl (`rc=1`, empty stderr/stdout) under the strict parser.
- Root-cause addendum: strict `grep` under `set -euo pipefail` also exited before the documented exit-5 error branch when Telegram keys were absent; added `test_prod_env_missing_telegram_keys_exits_5`.
- Green evidence: Git Bash stub run with indented keys now reaches curl and emits `ALERTED: HTTP 200`; missing-key stub run now exits 5 with `TELEGRAM_BOT_TOKEN missing/placeholder`.
- Windows pytest evidence: `python -m pytest tests/test_cron_drift_watchdog.py -q` reports `22 skipped` because this watchdog suite is module-skipped on win32.
- Parse-mode guard: source grep for `parse_mode` in `scripts/cron-drift-watchdog.sh` returns no matches.

## Active Work: BL-NEW-CRON-DRIFT-WATCHDOG (item 3, PR #156)

- [x] Isolated worktree: `.claude/worktrees/feat+cron-drift-watchdog`
- [x] Drift-check: HEAD = `cdeb31f` = origin/master (zero divergence; includes PRs #150-#154). Grep for `cron-drift-watchdog` returns ZERO files — net-new.
- [x] Hermes-first: no per-token Hermes primitive for crontab drift; reuse in-tree curl-direct Telegram pattern. awesome-hermes-agent reachable; x-twitter-scraper exists but unrelated.
- [x] Plan v2 (post-2-reviewer fold): `tasks/plan_cron_drift_watchdog.md` — 14 reviewer findings folded (1 CRITICAL + 8 IMPORTANT + 5 MINOR across 2 reviewers).
- [x] Design consolidated into plan v2 per CLAUDE.md §10 (fold table + code blocks specify all design decisions; separate design doc would duplicate).
- [x] Build: `scripts/cron-drift-watchdog.sh` (~215 LOC mirroring cycle-10 systemd-drift-watchdog with reviewer-fold improvements) + `tests/test_cron_drift_watchdog.py` (14 tests).
- [x] TDD: 14/14 tests pass on srilu Python 3.12.3 / pytest 8.4.2. Mid-build bug caught: `diff -u` includes tempfile mtime headers, breaking sha256 ack stability. Fixed via `--label`.
- [x] Prod-crontab dry-run: CLEAN (managed block matches repo fragment).
- [x] backlog.md status: PROPOSED → PR-OPEN / SCRIPT-READY / SCHEDULING-PENDING-OPERATOR (per Reviewer 1 PR-review-3 P2: "SHIPPED" wording reserved for post-merge state; pre-merge says SCRIPT-READY) + 2 follow-ups filed
- [x] PR + 3 parallel PR-stage reviewers → all CRITICAL+IMPORTANT folded (commit 9e9a208)
- [x] Reviewer-2 PR-review fold: ACK_DIR mkdir failure now exits 9 with clear message (vs prior warn-then-fail-cryptically); test_ack_dir_unwritable_exits_9 added; 20/20 tests pass on srilu
- [x] Reviewer-2 PR-review fold: scope trim — BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT now systemd-only (cron-watchdog ships ACK_DIR-exit-9 fix)
- [x] Reviewer-3 PR-review fold: backlog wording corrected — "SCRIPT-SHIPPED" was premature pre-merge; renamed pre-merge state to "PR-OPEN / SCRIPT-READY / SCHEDULING-PENDING-OPERATOR"; post-merge action text updated with 3-stage convention (PR merge → SCRIPT-SHIPPED with SHA → operator scheduling → SHIPPED/SCHEDULED)
- [x] Post-merge stage 1 (bookkeeping): flipped PR-OPEN/SCRIPT-READY → SCRIPT-SHIPPED with PR #156 merge SHA `7f9aee6`
- [x] Post-merge follow-up: PR #159 squash-merged to master at `63aeef0` on 2026-05-18; backlog status for BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT flipped to SHIPPED.
- [ ] Post-merge stage 2 (operator scheduling, separate): operator adds cron line via cron/README §Setup; then flip → SHIPPED/SCHEDULED

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

- [x] **D+3 mid-soak verification** — query `candidates` table for fraction satisfying `quote_symbol ∈ stables AND liquidity_usd >= 50K`. Threshold: < 40% to keep current bonus magnitude. Query in `docs/runbook_high_peak_fade.md`-adjacent runbook if needed. **CLOSED 2026-05-19 (audit): ELAPSED-WITHOUT-REVERT. D+3 was 2026-05-12 (7d before audit). Per `backlog.md` §BL-NEW-QUOTE-PAIR (SHIPPED 2026-05-09 PR #85 `3774591`, magnitude `+5 raw / +2 normalized`), no revert trigger fired in source-of-truth docs; STABLE_PAIRED_BONUS remains at default. Memory `project_bl_quote_pair_2026_05_09.md` confirms 7d soak ended 2026-05-16.**
- [x] **D+7 soak end** — alert volume must not exceed +10% baseline. Revert via `STABLE_PAIRED_BONUS=0` env override if breached. **CLOSED 2026-05-19 (audit): ELAPSED-WITHOUT-REVERT. D+7 was 2026-05-16 (3d before audit). Same evidence chain as D+3 above.**

## Pending verifications (time-gated)

- [x] **2026-05-04 ~01:09Z+ — BL-071 guard verification (24h check).** **PASS (with caveat).** Verified 2026-05-04T15:35Z. `full_conviction` + `narrative_momentum` still `is_active=1` ✓. `volume_breakout` retired 2026-05-04T01:01:48Z via the `chain_pattern_retired` path (hit_rate=1.82%, 1 hit in 55 attempts) — legitimate individual underperformance, NOT a guard failure. The guard only short-circuits on `total_hits_across_all == 0`; with non-zero hits on at least one pattern, individual retirement is allowed (correct behavior). chain_completed paper_trades count: 7 → 10 in 24h (+3 new). Chain dispatch alive. No action needed.
- [x] **2026-05-04 13:58Z — BL-063 moonshot soak ends. DECISION: keep on permanently.** Verified 2026-05-04T15:35Z. Moonshot path: **19 closes / +$2,232.86 net / +$117.52/trade / 100% win**. Regular-trail comparison (peak ≥30, no moonshot armed): 13 closes / +$773.52 net / +$59.50/trade / 100% win. Moonshot delta = +$1,459.34 net — exceeds the +$1,420 sneak-peek prediction by ~3% and ~3× the regular-trail per-trade. Permanent.
- [x] **2026-05-04 22:24Z — Paper-lifecycle widening soak ends.** Sneak-peek +$1,234 net / 91 closes. Decision: keep on. **CLOSED 2026-05-19 (audit): KEEP-ON-PRESUMED (docs-only) per inline sneak-peek decision; soak ended 15d before audit with no documented revert. .env continues to carry the widened lifecycle settings (see "Prod .env current state" block below in this same file). Per §9a caveat at top of file: evidence is docs-only; not SSH-verified.**
- [x] **2026-05-05 22:58Z — PR #59 strategy tuning soak ends.** Sneak-peek +$1,994 net / 135 closes / 67.4% win / 20% expired. Decision: keep on permanently. **CLOSED 2026-05-19 (audit): KEEP-ON-PRESUMED-PERMANENT (docs-only) per inline decision + early-signal at 13.5h. PR #59 (`3c83fb7`) per `tasks/todo.md` "What shipped this session" table below. Soak ended 14d before audit; no documented revert. Per §9a caveat: docs-only evidence.**
- [x] **2026-05-10 15:53Z — gainers_early reversal re-soak (7d).** Watch for performance vs the +$190/day sneak-peek that justified reversal. If actuals < +$100/day for 7d, re-evaluate. **CLOSED 2026-05-19 (audit): ELAPSED-AUTO-SUSPENDED. Re-soak window 2026-05-10 → 2026-05-17 elapsed. Per `backlog.md:1798` (inside `BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING`, entry header at backlog.md:1797) the new PR #150 evaluator returned `gainers_early=FAIL contradicting 2026-05-13 audit-id=24`, and the same entry records auto-suspend firing 2026-05-17T01:02:46Z (audit ids 26/27). Memory `project_soak_closure_2026_05_13.md` reflects the pre-PR-#150-evaluator KEEP-ON verdict that was explicitly contradicted by the new evaluator.**
- [x] **2026-05-13 02:13Z — losers_contrarian post-BL-NEW-AUTOSUSPEND-FIX revival 7d soak.** **KEEP ON (permanent).** Closed 2026-05-13T04:05Z. n=55, net +$826.68, per_trade +$15.03, win 69.1%. Both gate clauses cleared by ~4×. Zero auto-suspend fires during soak. Drivers: `peak_fade` n=26 +$1,688; `stop_loss` n=11 −$917 drag. Audit row id=23.
- [x] **2026-05-13 02:15Z — gainers_early post-BL-NEW-AUTOSUSPEND-FIX revival 7d soak.** **KEEP ON (permanent).** Closed 2026-05-13T04:05Z. n=128, net +$1,894.37, per_trade +$14.80, win 72.7%. Both gate clauses cleared. Zero auto-suspend fires during soak. `conviction_lock_enabled=1` stays armed. Drivers: `peak_fade` n=38 +$2,499 + `trailing_stop` n=54 +$888; `stop_loss` n=13 −$1,059 drag. Audit row id=24.
- [x] **2026-05-13 02:18Z — HPF dry-run 7d soak (BL-NEW-HPF Phase 1).** **KEEP DRY-RUN. Do NOT flip the flag.** Closed 2026-05-13T04:05Z. n=7 would-fires (6 gainers_early + 1 losers_contrarian). Aggregate counterfactual: HPF +$1,078.15 vs actual +$1,123.63 — **delta −$45.48 (negative)**. Subset reading (structural §9c): HPF beats `moonshot_trail` 3/3 (+$238) but loses to existing `peak_fade` 3/4 (−$285). Re-evaluate at n≥20 scoped to `moonshot_trail`-subset only (filed BL-NEW-HPF-RE-EVALUATION). Audit row id=25.
- [x] **2026-05-13+ — Deploy PR #82 BL-NEW-MOONSHOT-OPT-OUT (held overnight 2026-05-06).** Migration adds `signal_params.moonshot_enabled INTEGER NOT NULL DEFAULT 1` — no behavior change on deploy (default opt-IN preserves existing floor). Per-signal opt-out via `UPDATE signal_params SET moonshot_enabled=0 WHERE signal_type='X'`. Backtest applicability caveat: `findings_high_peak_giveback.md` PnL projection used floored regime; opted-out signal must re-run backtest with floor removed before projecting impact. **DEPLOY-VERIFIED 2026-05-19:** read-only srilu query confirms `signal_params.moonshot_enabled` exists with default `1`; `paper_migrations` row `bl_moonshot_opt_out_v1|2026-05-09T16:15:22.266116+00:00`; `schema_version` row `20260507|bl_moonshot_opt_out_v1`. No per-signal opt-out was applied; this closes deploy verification only.
- [x] **2026-05-17 — chain_complete fire-rate observation post-PR #80: CLOSED.** Lifetime: full_conviction=201, narrative_momentum=210, volume_breakout=301 chain_matches. Post-PR-#146 recent: active_chains=83 rows in 14d (oldest 2026-05-11T16:41Z), all 4 narrative anchor events fired 139× each in 7d. Paper-trades: 12 chain_completed in 14d, +$1,034 net, +$207/trade. Observability bump served purpose; PR #154 reverts `scout/chains/patterns.py` full_conviction + narrative_momentum from `medium` → `low` (also code-vs-prod-state alignment — PR #146 snapshot-restore already had prod at `low`). 14/14 chain_patterns tests pass including new closure-test `test_builtin_patterns_alert_priority_post_observability_revert`.

## Active soaks (don't disturb)

- [x] **Tier 1a flip — gainers_early kill REVERSED 2026-05-03T15:53Z** — original kill was based on pre-PR-#59 30d data. Sneak-peek of post-#59 data (4.7d window) showed gainers_early at +$508 / 59 closes / +$8.61/trade / 67.8% win — clearly profitable under the new adaptive trail. PR #59 fixed gainers_early; the kill was forfeiting ~$190/day. SQL reversal + restart verified: 5 new gainers_early trades opened at 15:58:29Z, zero `trade_skipped_signal_disabled` events. Tier 1a `SIGNAL_PARAMS_ENABLED=true` flag stays on for the other 7 signals (per-signal params still honored). Audit row in signal_params_audit. Backup: `scout.db.bak.gainers_revive_20260503_155322`.

- [ ] **2026-05-15 14:06Z — RE-SCOPED system health checkpoint (was: "Tier 1a kill 14d soak").** **STILL OPEN AT 2026-05-19 (audit): checkpoint date elapsed 4d before audit; operator-driven 3-question review has no documented closure in memory or backlog. SQL queries below remain valid for operator's next session.** The original A/B (kill gainers_early, see net swing) was invalidated 2026-05-03 when we reversed the kill based on post-PR-#59 data. New scope: 2-week strategic checkpoint after a flurry of changes (Tier 1a flag on, per-signal params live, chain_completed dispatch wired + long-hold tuned, BL-071 guard live). Three concrete questions:
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
- [ ] **PR #58 BL-064 lenient-safety soak** — flag flipped 2026-04-28T15:17Z. Re-check window: 2026-05-12. **STILL OPEN AT 2026-05-19 (audit): re-check window elapsed 7d before audit. Operational-gap risk per the inline note: curators may not have posted CA-bearing messages in the window. Closure deferred to operator-initiated BL-064 retrospective; memory `project_bl064_deployed_2026_04_27.md` documents original bootstrap, and memory `project_narrative_scanner_v1_1_shipped_2026_05_13.md` covers the follow-on KOL list work.**
  - Decision gate: ≥40% win rate + avg pnl_pct >0 → keep on. As of 2026-04-29T12:25Z: 0 trades dispatched yet (curators haven't posted CA-bearing messages since flag flipped). Operational gap, not code.
- [x] **PR #59 strategy tuning soak** — deployed 2026-04-28T22:58Z. Re-check window: 2026-05-05. **CLOSED 2026-05-19 (audit): KEEP-ON-PRESUMED-PERMANENT (docs-only) (duplicate of L421 closure above in this file; same PR #59 / `3c83fb7`). 9× improvement in $/trade was the early-signal evidence; full-soak +$1,994 net / 135 closes / 67.4% win documented at L421. Soak ended 14d before audit; no documented revert. Per §9a caveat: docs-only evidence.**
  - Early signal at 13.5h: 23 closes, +$650 net, ~70% win rate, 0 expired closes. 9× improvement in $/trade vs historical −$3.05. Letting it ride.
- [x] **BL-063 moonshot soak** — flag flipped 2026-04-27T13:58Z. Soak ends 2026-05-04T13:58Z. **CLOSED 2026-05-19 (audit): KEEP-ON-PRESUMED-PERMANENT (docs-only) (duplicate of L419 closure above in this file). Per L419 (pre-existing operator decision): "Moonshot path: 19 closes / +$2,232.86 net / +$117.52/trade / 100% win. Permanent." Soak ended 15d before audit. Per §9a caveat: docs-only evidence.**
- [x] **BL-064 14d TG social soak** — ends 2026-05-11T22:10Z. **CLOSED 2026-05-19 (audit): ELAPSED-OPERATIONAL-GAP. Soak ended 8d before audit. BL-064 surfaced trending_catch which auto-killed 2026-05-11T01:00:26Z (`hard_loss`, net -$317) per memory `project_trending_catch_soak_2026_05_10.md`. BL-064 was superseded by Narrative Scanner V1.1 KOL-list direction shipped 2026-05-13 per memory `project_narrative_scanner_v1_1_shipped_2026_05_13.md`.**
- [x] **Paper-lifecycle widening soak** — .env tweaks deployed 2026-04-27T22:24Z. Soak ends ~2026-05-04T22:24Z. **CLOSED 2026-05-19 (audit): KEEP-ON-PRESUMED (docs-only) (duplicate of L420 closure above in this file). Soak ended 15d before audit; no documented revert. Per §9a caveat: docs-only evidence.**

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
- [x] `narrative_prediction` token_id divergence fix — 32 of 56 stale-young open trades have empty/synthetic token_ids that don't appear in `price_cache`. Separate upstream fix. **CLOSED 2026-05-19 (audit): UPSTREAM FIX SHIPPED 2026-05-06 (duplicate of L521 closure above in this file). Per L521: PR #80 (`eaf3523`) per-laggard emission with `token.coin_id` (was `accel.category_id`); pre-fix 2,770 anchors → 2 chain_completes, post-fix `narrative_prediction` token_ids resolve in `price_cache`.**
- [x] **2026-05-06 @s1mple_s1mple verdict — DO-NOT-ADD (off-thesis).** Background investigation 2026-05-06: `@s1mple_s1mple` doesn't resolve via Bot API (likely user account, not channel — incompatible with Telethon listener). `@s1mplegod123` resolves as Russian-language esports diary "Дневник Симпла" (Counter-Strike pro s1mple of NaVi), 256K subscribers, ZERO crypto content across t.me sample + 1,220 cross-channel mention rows. No DB references in 5 tables. Operator can still add as `trade_eligible=0, cashtag_trade_eligible=0` watch-only with 30-day re-eligibility check if desired despite fit, but default action is no-add. See investigation notes inline; no separate findings file written.
- [ ] Audit fix #4 (24h hard-exit if peak<5%) deferred — accumulate more data first. **STILL OPEN AT 2026-05-19 (audit): genuinely pending per inline "accumulate more data first" decision. No backlog entry or memory checkpoint indicates operator has revisited. Defer to operator's next strategy-tuning cycle.**
- [x] **BL-NEW-REVIVAL-COOLOFF — SHIPPED 2026-05-06** (PR #81 / `57192cb`). 7-day default cool-off on `revive_signal_with_baseline` with `force=True` bypass. Plan-stage MUST-FIX: positive `applied_by='operator'` filter. Design-stage MUST-FIX: settings DI. PR-stage CRITICAL: caplog→capture_logs. All applied. Smoke-tested on VPS: cool-off correctly blocks losers_contrarian re-revival.
- [x] **#3 Channel-list reload — CLOSED-AS-SHIPPED 2026-05-06.** Drift-check: PR #73 (`a12603f`, 2026-05-04) shipped channel hot-reload via `_channel_reload_once` + heartbeat factory + channels_holder TypedDict. todo.md item was stale.
- [x] **narrative_prediction token_id divergence — UPSTREAM FIX SHIPPED 2026-05-06** (PR #80 / `eaf3523`). Original symptom (32/56 stale-young opens) resolved by PR #72 + zombie cleanup. Real upstream cause was agent.py emitting `category_heating` with `token_id=accel.category_id`, breaking chain pattern matching. Pre-fix: 2,770 anchors → 2 chain_completes. Post-fix: per-laggard emission with `token.coin_id`.
- [x] **#5 @s1mple_s1mple verdict — DO-NOT-ADD 2026-05-06.** Esports diary, no crypto.
- [x] **moonshot floor nullification — UPSTREAM FIX MERGED 2026-05-06** (PR #82, deploy held until 2026-05-13). Per-signal `moonshot_enabled INTEGER NOT NULL DEFAULT 1` opt-out flag.
- [x] **first_signal revival decision** — under combined-gate rule, first_signal would NOT auto-fire (-$132 30d net is borderline). Operator decision: revive for soak, or leave suspended. Note: revival now subject to 7-day cool-off (PR #81); first revival ever bypasses cool-off cleanly. **CLOSED 2026-05-19 (audit): DECIDED-REVIVE-AND-SOAK per `backlog.md:1792-1793` (BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION SHIPPED-WITH-DECISION 2026-05-17, Option A REVIVE-AND-SOAK 14d window ending 2026-05-31). Memory checkpoint: `project_first_signal_revival_decision_2026_05_31.md`. Pre-registered verdict criteria + n≥10 trip-wire + 28d auto-extend + early-halt at n≥20 per CLAUDE.md §11. The 2026-05-31 soak-end is operator-gated per the assignment guardrail ("do NOT start... first_signal 2026-05-31 early").**

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
TELEGRAM_BOT_TOKEN=<wired 2026-05-06 — see project_telegram_wired_2026_05_06.md memory>
TELEGRAM_CHAT_ID=<wired 2026-05-06 — same memory>
```

Active TG channels (7):
- `@detecter_calls` (trade_eligible, safety_required=0)
- `@thanos_mind` (trade_eligible, safety_required=0)
- `@cryptoyeezuscalls` `@Alt_Crypto_Gems` `@nebukadnaza` `@alohcooks` `@CallerFiona1` (alert-only, strict)
- `@gem_detecter` (retired — typo, doesn't exist on Telegram)

## Resume hook — superseded 2026-05-21

Prior "Resume hook" referred to pre-Telegram-wired state (placeholder bot token) and the Tier 1a+1b PR which has since shipped and been measured. That section was stale.

Current resume hook (2026-05-21, post overnight decision-harvest):

1. **Operator decisions pending** (not closed by night session):
   - PR #33 (BL-050 first_signal edge detection): CLOSED 2026-05-22 as stale/superseded after relevance review; revisit only as fresh rewrite if first_signal survives the 2026-05-31 soak
   - PR #34 (BL-051 DexScreener velocity_boost): OPERATOR CLOSE recommended as PARKED-PENDING-PRICE-COVERAGE (substrate rejected by PR #208 design)
   - PR #105 (Phase B volume_history snapshot): OPERATOR CLOSE recommended as STALE-WIP (CI FAILED, body says "DO NOT MERGE", no updates since 2026-05-18)

2. **Data-bound re-check triggers** (clauses 2-4 trigger a re-validation pass; only clause 1 triggers implementation-eligibility consideration):
   - **Primary (implementation-eligibility):** n_actionable_closed≥20 AND n_exploratory_closed≥5
   - **Early-fire re-evaluation (a):** ≥1 exploratory closed with `pnl_usd > 0` (false-negative signal)
   - **Early-fire re-evaluation (b):** n_exploratory≥5 AND ≥4 losses with one-sided binomial 95% LB exceeding null (loss-rate > 50% baseline)
   - **Early-fire re-evaluation (c):** n_actionable≥15 AND cohort `total_pnl<-$50`
   - Multi-clause precedence: if multiple clauses fire, treat as independent investigation tracks, not summed evidence

3. **Implementation gates** (none active tonight):
   - Hermes + Codex operating model: BL-NEW-HERMES-CODEX-OPERATING-MODEL filed 2026-05-22. Direction: Hermes owns durable memory/orchestration/scheduling; Codex owns repo-grounded plan/design/build/review/verification; runtime evidence beats remembered state.
   - Price-coverage expansion (PR #208 design): operator-approved vendor sample call to GoldRush/Covalent/CoinGecko MCP
   - Source-call ranking / pruning / dashboard "best source" surfaces: blocked until price coverage materially improves
   - Live decision cockpit: BL-NEW-LIVE-DECISION-COCKPIT filed 2026-05-22 from trader-lens exercise. Goal is one per-token "trade / watch / reject / data insufficient" surface; V1 is read-only, Hermes-enriched but not Hermes-load-bearing, and must keep TG/X context-only until source-call price coverage becomes rankable.
   - Signal trust roadmap: BL-NEW-SIGNAL-TRUST-ROADMAP filed 2026-05-22. Goal is per-signal maturity states, scorecards, actionability-vs-would_be_live arbitration, narrative hard filters, and Hermes explanation bridge. V1 is read-only and must not auto-disable, prune, size, rank KOLs, or execute.

4. **Health surfaces** (verified clean tonight; re-check on next operator request):
   - source_calls writer parity: 100% (tg=858/858, x=399/399)
   - Hermes narrative cron: `last_status=ok`, 92 inbound rows/24h
   - 0 `ALERT_SENT` in last 24h
   - No exceptions, no service restarts

Full state record: `tasks/findings_overnight_decision_harvest_2026_05_21.md` (this PR).

Default suggestion if user opens with a generic "what's up": run the post-deploy check-in (option 2) — it's quick and gives them fresh data.

## Active Work: 2026-05-23 - Srilu/VPIN pytest gate repair

- [x] Confirm target VPS roles and exclude Main Shift Agent from this repair pass.
- [x] Create isolated branches on `srilu-vps:/root/gecko-alpha` and `vpin-vps:/opt/btc15minutebot`.
- [x] Reproduce and cluster Srilu Gecko failures; preserve local unpushed commits while checking upstream drift.
- [x] Reproduce and cluster VPIN BTC failures; first verify pytest environment before code edits.
- [x] Apply smallest root-cause fixes and rerun focused plus full native pytest gates.
- [x] Record final review / residual risks here before reporting.

Review:
- Srilu Gecko root causes: local checkout was 2 ahead / 5 behind origin; repo tests also leaked production `.env`, assumed a bare `python` executable, used SQLite numeric underscores unsupported on the VPS SQLite, and inherited production writer-heartbeat env in wrapper tests.
- VPIN BTC root causes: venv lacked `pytest-asyncio` despite dev dependency declaration, causing broad async-test collapse; remaining failures were production `.env` leaking into unit-test defaults/dashboard auth.
- Verification: Srilu `/root/gecko-alpha` full native suite passed: `2707 passed, 11 skipped, 11 warnings`. VPIN `/opt/btc15minutebot` non-integration suite passed: `2543 passed, 10 deselected, 217 warnings`.
- Residual: BTC venv was repaired in-place with `pytest-asyncio`; pyproject already declares it under dev extras, so no dependency-file change was needed. Pre-existing VPIN untracked files `.env.bak.20260318_214429` and `scripts/per_hour_contrarian_analysis.sql` were left untouched.
