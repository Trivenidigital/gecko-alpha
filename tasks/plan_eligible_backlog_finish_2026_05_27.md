**New primitives introduced:** NONE

# Eligible Backlog Finish Plan - 2026-05-27

**Goal:** Finish every backlog item that is actually eligible now, without rebuilding parked/gated work or shipping policy changes on stale assumptions.

**Architecture:** This is a current-state closeout and eligibility audit. Code changes are allowed only if drift/runtime checks prove an unshipped lever is real, safe, and not blocked by an operator/data gate. Otherwise the build output is a status PR that retires stale active-work entries and pins the next unlock condition.

**Tech stack:** Markdown backlog artifacts, existing dashboard/API tests, prod SQLite/runtime checks on srilu, GitHub PR workflow.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| backlog triage / scheduled reminders | yes - Hermes Automation Templates include nightly backlog triage and cron delivery | keep gecko-alpha backlog truth in repo; Hermes is useful for future wakeups, not for changing repo state here |
| runtime cron / soak reminders | yes - Hermes cron supports script-only scheduled jobs | do not add a new Hermes job in this PR; current eligible items are either gated or already shipped |
| narrative resolution | none found for gecko-alpha's SQLite `narrative_alerts_inbound` to local `candidates` reconciliation | do not build custom resolution unless runtime proves a canonical target exists |
| held-position stale alerting | generic Hermes notification/cron exists, but no project-specific stale-price logic | keep custom if baseline crosses threshold; current baseline does not |

Awesome-Hermes-agent ecosystem check: generic automation and notification patterns exist, but no skill owns gecko-alpha's durable trading DB contracts, signal semantics, or backlog status authority. Repo-local status evidence remains the source of truth.

## Runtime/Drift Evidence Collected

- Open PRs: `gh pr list --state open` returned none.
- `BL-NEW-SIGNAL-FAMILY-SCORECARDS`: code and tests are already present on `origin/master` (`/api/signal_trust/scorecards`, backend models/helper, frontend tab, focused tests). The remaining todo checkbox says "Open replacement PR"; that is stale against current base.
- Trade Inbox counter-risk context: code and tests are already present on `origin/master` (`counter_risk_score`, `counter_flags`, `counter_risk_predicted_at`, UI rendering, contract tests). The remaining todo checkbox says "PR, reviews, merge, deploy"; that is stale against current base.
- `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN`: prod soak still has only two mature UTC days from `scripts/trade_inbox_tracker_promotion_soak.sql`: `2026-05-26|17`, `2026-05-25|50`. It remains gated until three mature UTC days or the 14-day backstop. Reviewer fold: record an explicit next check on 2026-05-28 UTC and note that the current SQL uses present-time open-paper suppression, so counts are unlock guidance rather than immutable historical labels.
- `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT`: 7d prod journal baseline is available now, not just a 24h sample: 3,878 `held_position_refresh_summary` rows from `2026-05-20T01:42:52Z` through `2026-05-27T01:39:12Z`; `stale_open_count` min/p50/max `2/4/5`, `held_total` min/p50/max `125/139/150`, and zero cycles exceed the suggested `max(5, 0.05 * held_total)` threshold. This does not close the §12a residual permanently; it means the alert implementation is not eligible without a threshold revision or a future baseline breach. Keep as baseline-measured/low-rate, not shipped.
- `BL-NEW-HERMES-NARRATIVE-DEFERRED-RESOLUTION-SWEEP`: prod has 24 unresolved CA rows in the last 7d and 39 all-time, but only 3 recent rows match `candidates`, all for one Solana CA. The `candidates` table has no `coingecko_id`; `/api/coin/lookup` currently returns `found=true` with `coin_id=null` from `candidates`. Table audit found `coingecko_id` only on `second_wave_candidates`, not on `candidates` or narrative rows. Hermes cron `gecko-x-narrative-scanner` is enabled and last `ok` at `2026-05-27T01:00:52Z`. Writing a fake `resolved_coin_id` would overstate source-call rankability and not improve price coverage. Keep the backlog item open as blocked on a canonical CA->coin_id resolver or source-call identity-resolution design; do not close it as harmless.
- PR #33: GitHub reports `CLOSED` at `2026-05-22T18:13:02Z`, not open/design-review-required. `backlog.md` still has stale open-PR wording and must be corrected.
- PR #280: GitHub reports `CLOSED` at `2026-05-26T18:30:51Z`; the backlog should say closed/superseded rather than "close rather than merge."
- Track 3 (`BL-NEW-X-OUTCOME-LINKAGE`, `BL-NEW-TG-OUTCOME-LINKAGE`, `BL-NEW-NO-PEAK-RISK-HANDLING`): no longer blocked by the actionability row-count gate, but eligible only for stale-PR/current-base re-scope. This PR should classify them as `RE-SCOPE-ELIGIBLE`, not build them.

## Eligible Work Decision

Build a status/closure PR with no runtime behavior changes:

1. Retire the stale active-work tails for Signal Trust scorecards and Trade Inbox counter-risk in `tasks/todo.md`.
2. Update `backlog.md` so the same two items are not picked up again as "open PR / needs rebase" work.
3. Correct stale PR status rows for PR #33 and PR #280.
4. Add a findings document with the runtime evidence above and explicit branch decisions for TG alerts, held-position stale alerts, deferred narrative resolution, and Track 3 re-scope eligibility.
5. Keep all truly parked/operator-gated rows parked: source/KOL ranking, TG alert qualification until the soak gate clears, first-signal 2026-05-31 decision, source-call price coverage, CG key setup, operator-alert activation, and fallback `/coins/{id}`.

## Non-Scope

- No Telegram alert qualification, urgency tiers, ranking, source pruning, signal parameter changes, auto-suspend changes, live execution, sizing, paper-trade policy, new cron, new DB table, or paid/vendor calls.
- No narrative writeback that sets `resolved_coin_id` to a non-canonical surrogate.
- No held-position alert until stale counts cross a baseline-derived threshold or the operator changes the policy.

## Plan Tasks

- [ ] Plan review by two parallel agents:
  - Reviewer A: backlog eligibility and stale-status drift.
  - Reviewer B: runtime-state and silent-failure risk.
- [x] Fold plan review findings:
  - PR #33 and PR #280 stale statuses must be corrected.
  - Track 3 items must be explicitly classified as re-scope-eligible, not omitted.
  - Held-position stale alert needs 7d baseline evidence, not 24h snapshot.
  - Narrative deferred resolution remains blocked/open, not closed.
  - TG alert soak needs explicit next-check/backstop language and SQL-stability caveat.
- [ ] Write design doc for the no-runtime-change status PR.
- [ ] Design review by two parallel agents:
  - Reviewer A: docs/status consistency.
  - Reviewer B: branch-decision correctness.
- [ ] Fold design findings.
- [ ] Build docs/status edits.
- [ ] Verify with `git diff --check` and focused tests proving already-shipped surfaces still pass.
- [ ] Create PR.
- [ ] Get two parallel PR reviews and fold any issues.
- [ ] Merge and deploy only if the final diff contains runtime code; otherwise mark "no deploy needed."

## Verification Commands

```powershell
git diff --check
uv run pytest -q tests/test_signal_trust_scorecards_endpoint.py tests/test_signal_trust_registry_endpoint.py tests/test_trade_inbox_endpoint.py tests/test_check_trade_inbox_contract.py tests/test_dashboard_frontend_layout.py
```

## Self-Review

- Spec coverage: covers all currently surfaced eligible/gated items from `tasks/todo.md` and the top backlog tracks.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type/API consistency: no code/API changes are planned; verification targets existing shipped endpoints.
