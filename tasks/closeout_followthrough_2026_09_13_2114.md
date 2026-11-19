# Closeout follow-through — 2026-09-13 21:14 UTC

## Result and scope

Documentation correction only. Three alert-registry rows claimed health routing
that the call sites do not implement. Disk-pressure, disk-recovery and CoinGecko
budget-pace alerts use the default trading destination. Correct the rows and
name the helper paths; do not change routing or send messages. Add an ownership
preflight convention to the operating model; it is not an atomic scheduler lock.

Source evidence at origin/master 6dd4327b:
- `scout/trading/detection_alert.py:116-124`: `_send_disk_page` supplies no chat override;
  calls at 715/719 pass the lane settings unchanged.
- `scout/main.py:1656-1710`: `_send_cg_budget_pace_alert` supplies no chat override.
- `scout/alerter.py:171`: explicit override or `settings.TELEGRAM_CHAT_ID`.
- `scout/instrumentation/watchdog.py:96-107`: DEX watchdog uses the health override.

The previous automation-directory overlap report's inference that the registry
paragraph was stale was incorrect: it trusted table labels without tracing the
send calls. The three table labels are stale. This correction does not prove
message delivery or configuration of distinct chat destinations.

## Runtime and ownership evidence

- Fresh origin/master 6dd4327b; clean isolated branch
  `feat/overnight-closeout-20260913-2114` before changes.
- SSH read-only at 2026-09-13T21:16:52Z: production HEAD d2f0d61edc63cb55ae159ec952cce404991f21f5,
  clean tracked checkout; gecko-pipeline, gecko-dashboard and hermes-gateway active.
  This is service state, not proof of pipeline freshness or end-to-end delivery.
- Both known earlier automation turns completed, independently confirmed with
  task snapshots. The 20:14 invocation is not still active.
- Task `Prepare for improvements` (01a09bfb-c05b-7de3-9ebf-0efa353bdf2e) is active,
  integrating PR572 and coordinating sustained capture work with Claude.
  It retains those PR/deployment operations. PR572 was OPEN at dcb5dd26 with test
  CI IN_PROGRESS and reviewer-clearances SUCCESS at inspection.
- PR573 is independently confirmed MERGED as 6dd4327b. PR571 deployment/smoke/test
  history is in the prior closeout report; this run did not repeat that deployment.

## Drift verdict and remaining work

All six templates, role map, status reporter, cockpit and Signal Trust V1 exist.
No custom primitive is proposed; no Hermes capability needs replacing. This
routine documentation repair does not introduce a plan/design/spec or build.

DASH09 closed-trade SELECT at dashboard/db.py:3217 lacks frozen stop comparison;
DASH05 recorder at scout/postmortem/moved_already.py:152 has no dashboard consumer;
ALR06 severity routing remains unwired. These are real residuals, not closures.
Current successor tasks/backlog_fable_analysis_2026_07_10.md remains untracked in
C:/projects/gecko-alpha and absent from git. It is dated requirements evidence.
Publish/reconcile it before treating its statuses as current runtime truth.
Independent docs work is safe; this run does not claim these engineering gaps
are operator-only gates or that the backlog is exhausted.

Paid historical samples and live execution/sizing/pruning/dispatch changes retain
the explicit operator gates. Negative historical GT probes remain parked.
No paid API, DB/config/service mutation, external message or routing change.

## Verification and handoff

Existing alert-registry coverage: 2 passed using the integration checkout's
installed Python environment against this worktree's tests. The initial local
`uv run --no-sync` attempt could not find pytest in the empty local environment;
no dependency change was needed. `git diff --check` passed.

Two independent PR reviews and exact-head CI are required before merge; their
final results will be recorded in the PR and automation memory. Docs-only change:
no deploy or production smoke required; rollback is a normal documentation revert.

Permanent prompt still needs scheduler configuration/invocation/completion
separation, active ownership checks, and a tracked current queue. Configuration
is unchanged. Next operator action: reconcile/publish that queue and confirm
single-owner scheduler policy while the active capture task owns PR572.
