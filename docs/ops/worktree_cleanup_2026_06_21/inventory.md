# Worktree cleanup inventory — 2026-06-21

Lightweight archival record of the stale worktrees/branches pruned on 2026-06-21,
created before deletion so the history is recoverable.

- Source of truth at cleanup time: `origin/master` = local `master` = `b0df1720`
  (includes `docs/offshore_handoff_live_trading_2026_06_21.md`).
- Method: scanned all `git worktree list` entries (202). For each, counted commits
  not on any remote (`git rev-list --count <head> --not --remotes`). 32 worktrees
  had unpushed commits; every one was verified to correspond to an **already-merged
  feature** (signature file present in `origin/master` and/or a recorded shipped PR).
  None contained unmerged feature/code work — the "unpushed" commits are pre-squash
  history whose squashed equivalents are already on `master`.

## Excluded from deletion (not part of the 32)
- `docs/offshore-live-trading-handoff` @ `25d9881e` — the primary checkout
  (`C:/projects/gecko-alpha`). Squash-merged to master as `b0df1720` (#373); its
  remote branch was deleted, which is why it now looks "local-only." Content is on
  master. Left in place.

## Preserved artifacts
- `patches/append-price-path-audit-snapshot-20260528.patch` — historical srilu prod
  price-path coverage snapshot (the only potentially-unique content among the 32).
- `patches/append-liquidity-audit-snapshot-20260528.patch` — historical srilu prod
  liquidity coverage snapshot.
- `scratch/tg-dedup_ASSESS.txt` — untracked git-state diagnostic note found in the
  `tg-dedup` worktree (transient; preserved as insurance).
- `feat/todays-focus-v0` had an untracked **0-byte** `.scp_out.txt` (empty SSH
  scratch) — nothing to preserve.

## The 32 stale worktrees/branches removed

| Branch | Unpushed | Shipped as / status | Worktree path |
|---|---|---|---|
| docs/actionability-followup-calibration | 1 | actionability (#205) docs leftover | .claude/worktrees/docs+actionability-revalidation-2026-05-26 |
| chore/eligible-backlog-finish-2026-05-27 | 2 | backlog reconciliation docs | .claude/worktrees/eligible-backlog-finish-2026-05-27 |
| chore/eligible-backlog-sweep-2026-05-27 | 2 | backlog reconciliation docs | .claude/worktrees/eligible-backlog-sweep-2026-05-27 |
| worktree-feat+chain-completed-priority-revert | 1 | chain priority revert (#146) | .claude/worktrees/feat+chain-completed-priority-revert |
| docs/tg-alert-unlock-criteria | 1 | backlog docs | .claude/worktrees/tg-alert-unlock-criteria |
| feat/todays-focus-v0 | 5 | Today's Focus (#189/#190 family) | .claude/worktrees/todays-focus-v0 |
| docs/kraken-autotrade-prep-backlog-20260527 | 1 | backlog docs | .codex-worktrees/kraken-autotrade-prep-backlog-20260527 |
| test/pending-closeout-review-coverage | 1 | review staging | .codex-worktrees/pending-items-closeout-20260527 |
| feat/todays-focus-residuals | 1 | Today's Focus follow-ups | .codex-worktrees/todays-focus-residuals |
| feat/todays-focus-ux-pass-20260527 | 1 | Today's Focus UX | .codex-worktrees/todays-focus-ux-pass-20260527 |
| pr-294-current | 1 | review staging (PR #294) | .review-pr294 |
| review/pr-294 | 1 | review staging (PR #294) | .review-pr294-wt |
| codex/gt-429-handler-closeout | 1 | GeckoTerminal 429 handler | gecko-alpha-gt-429-handler |
| (detached) | 1 | midcap gainers scan (two-corpus, shipped) | gecko-alpha-pr121-review |
| (detached) | 1 | trending hydrate/breadth (shipped) | gecko-alpha-pr124-review |
| feat/clean-price-path-audit | 2 | audit script in master | gecko-alpha-wt/clean-price-path |
| fix/trade-surface-alert-pacing | 1 | trade-surface alert pacing | gecko-alpha-wt/focus-now-alerts-20260531 |
| feat/api-system-health-status-enum | 2 | health-status enum (#337, incl. fuzz NIT) | gecko-alpha-wt/health-enum |
| docs/autonomous-review-closeout | 1 | review closeout docs | gecko-alpha-wt/overnight-review-20260531 |
| docs/social-denominator-option-b-shipped | 1 | social denominator option B docs | gecko-alpha-wt/social-denominator-evidence-20260601 |
| feat/tg-alert-24h-dedup | 1 | TG 24h dedup (#336) | gecko-alpha-wt/tg-dedup |
| codex/x-alerts-dashboard | 3 | X Alerts tab (#190) | gecko-alpha-x-alerts |
| feat/review-live-candidates-determinism-contract-delta | 1 | cockpit determinism (#228/#229) | .codex/automations/.../review-20260525-live-candidates-determinism |
| chore/append-liquidity-audit-snapshot-20260528 | 1 | **patch preserved** | .codex/worktrees/gecko-alpha-liquidity-coverage-audit-20260528 |
| chore/append-price-path-audit-snapshot-20260528 | 1 | **patch preserved** | .codex/worktrees/gecko-alpha-price-path-coverage-audit-20260528 |
| feat/tg-alert-qualification-diagnostic-design-20260529 | 2 | design doc in master | .codex/worktrees/gecko-alpha-tg-alert-qualification-design-20260529 |
| docs/record-pr304-deploy | 1 | deploy-record docs | .codex/worktrees/gecko-alpha-todays-focus-block-links-20260528 |
| feat/todays-focus-market-context-strip-20260529 | 1 | Today's Focus PR-D | .codex/worktrees/gecko-alpha-todays-focus-market-context-strip-20260529 |
| feat/todays-focus-polish-followups-20260528 | 1 | Today's Focus polish | .codex/worktrees/gecko-alpha-todays-focus-polish-20260528 |
| feat/todays-focus-pr-a-detection-age-20260528 | 1 | Today's Focus PR-A | .codex/worktrees/gecko-alpha-todays-focus-pr-a-20260528 |
| fix/sparkline-remove-response-model-20260528 | 1 | sparkline hotfix | .codex/worktrees/gecko-alpha-todays-focus-sparkline-20260528 |
| feat/todays-focus-trade-packet-20260528 | 2 | Today's Focus trade-packet | .codex/worktrees/gecko-alpha-todays-focus-trade-packet-20260528 |

Total: 32 worktrees removed; 30 local branches deleted (2 were detached HEADs).
The 170 other worktrees had no unpushed commits and were left untouched.
