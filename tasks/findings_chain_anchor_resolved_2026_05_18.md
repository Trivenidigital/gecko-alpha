# BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX — SHIPPED (status correction + post-deploy verification)

Date: 2026-05-18
Backlog: BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX
Status: SHIPPED 2026-05-17 — PR #146 merged `5860d17` at 2026-05-17T16:50:40Z. This doc corrects a stale `PR-READY` backlog status and records post-deploy verification 24h+ after the fix landed.

## What was filed and what shipped

- 2026-05-17 ~07:38Z: PR #144 audit landed (`BL-NEW-CHAIN-COMPLETED-SILENCE-AUDIT`) — confirmed Tier 1a `chain_completed` signal silent 5.5d (narrative) / 13d (memecoin); `active_chains` MAX = `2026-05-11T16:42Z`.
- 2026-05-17 ~16:50Z: PR #146 (`5860d17` "fix(chains): restore chain anchor pipeline") merged 9h later. Branch was `codex/chain-anchor-pipeline-fix`.
- Backlog entry was correctly updated to `PR-READY 2026-05-17` at the time the branch was drafted, but never flipped to `SHIPPED` after merge. The stale status nearly triggered a duplicate-investigation cycle today (see "near-miss" note below).

## Causal mechanism per PR #146

Per the backlog entry's V37 audit-review fold: **all three built-in `chain_patterns` rows were inactive on prod**, so `load_active_patterns()` returned empty and `check_chains()` exited before anchor matching / `active_chains` writes. The mechanism is upstream of the `_check_active_chains` step-matching logic that the audit had focused on.

PR #146's fix shape (per backlog summary, verified in tree at master `3c03e27`):

| Surface | Behavior |
|---|---|
| `scout/chains/patterns.py` and DB schema | `is_protected_builtin`, `disabled_reason`, `disabled_at` columns; protected-builtin provenance prevents inactive-by-default rows for the 3 canonical patterns |
| Lifecycle | Exact prod-snapshot legacy recovery; safe built-in reconciliation preserves operator/code disables and learned `alert_priority`; lifecycle blocked-retirement for protected built-ins |
| Observability | Explicit `chain_no_active_patterns` log event — surfaces the failure mode immediately rather than silently exiting `check_chains()` |
| Watchdog | `scripts/chain-anchor-health-watchdog.sh` + `scripts/check_chain_anchor_health.py` + `systemd/chain-anchor-health-watchdog.{service,timer}` — hourly active-pattern-starvation / stale-`active_chains` watchdog. **This is the §12a recurrence-prevention coverage the audit asked for in step (iv) of its action list.** |

Tests on the merged branch: focused chain-anchor suite 49 passed; wider chain suite 79 passed, 1 skipped.

## Post-deploy verification (today)

Runtime probe of srilu `scout.db` and recent logs (2026-05-18 ~21:10Z, ~28h after PR #146 merged):

```text
===CHAIN_COMPLETED_LAST_FIRE===
2026-05-18T21:10:03.648489+00:00

===ACTIVE_CHAINS_PER_DAY_LAST_14D===
2026-05-18 | 117
2026-05-17 | 104
2026-05-16 |  11

===CHAIN_EVENT_LAST_FIRE===
category_heating | 2026-05-18T20:21:44.173596+00:00
chain_complete   | 2026-05-18T21:12:37.686864+00:00

===CHAIN_PIPELINE_RECENT (last 3d)===
memecoin  | 142
narrative |  90
```

Recovery timing aligns with PR #146 deploy:

- 2026-05-16: 11 rows — partial recovery (likely a transient pre-deploy state or earlier intervention).
- 2026-05-17: 104 rows — full recovery on PR-merge day.
- 2026-05-18: 117 rows — sustained.

Both `memecoin` and `narrative` pipelines firing in volume. `chain_completed` events firing on the latest cycles.

## Hermes-first

No Hermes-first re-run required for this PR — it is a backlog-status correction, not a new primitive. PR #146 itself shipped the in-tree chain-anchor watchdog; no external Hermes/skill replacement applies. The `BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX` audit had already concluded the failure mode was internal to `scout.chains` step-matching, which is correct shape (not Hermes-replaceable).

## §9c near-miss (worth recording)

The initial drift-check today (workspace-hygiene assignment) saw the symptom resolved but did not check git/PR history for an existing fix. The first draft of this findings doc framed the resolution as RESOLVED-BY-OBSERVATION with attribution unknown and proposed filing `BL-NEW-ACTIVE-CHAINS-WRITE-RATE-WATCHDOG` as a follow-up.

That proposal was wrong on two axes:

1. **Resolution is attributable** — PR #146 (`5860d17`, merged 2026-05-17T16:50:40Z) is the cause. The recovery timing (zero pre-2026-05-16 → 11 → 104 → 117 across 2026-05-16/17/18) aligns with the merge+deploy window.
2. **The follow-up watchdog already exists** — `chain-anchor-health-watchdog.{sh,service,timer}` ship with PR #146. Filing `BL-NEW-ACTIVE-CHAINS-WRITE-RATE-WATCHDOG` would have been duplicate work against an already-shipped primitive.

Lesson (§7a, "drift-check before scoping"): when a backlog item appears
to be `PROPOSED` but the symptom has already cleared, check the PR list
for a same-named branch or merge commit before scoping any follow-up.
Backlog status can lag merge state. In this case the backlog entry had
been updated to `PR-READY` (correct at branch-draft time) but never
flipped to `SHIPPED`. The PR existed and was already merged.

This finding doc would have been written as a redundant follow-up if
the reviewer hadn't flagged "PR-READY 2026-05-17 — branch
`codex/chain-anchor-pipeline-fix`" in the backlog entry that the
operator's "fresh drift/Hermes-first backlog pass" instruction
specifically asked me to check.

## Recommendation

This PR contains:

- `tasks/findings_chain_anchor_resolved_2026_05_18.md` (this doc) — status-correction record + post-deploy verification.
- `backlog.md` — flip `BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX` from `PR-READY 2026-05-17` to `SHIPPED 2026-05-17 — PR #146 merged 5860d17`; preserve the existing V37-corrected mechanism narrative.
- `tasks/todo.md` — checkmark the chain-anchor work as SHIPPED and record the post-deploy verification timestamp.

**No follow-up backlog entry filed.** The recurrence-prevention watchdog
PR #146 already shipped (`scripts/chain-anchor-health-watchdog.sh` +
systemd units in `systemd/`). If a future audit shows the watchdog is
missing some surface or threshold (e.g., per-pipeline freshness SLO vs
the current global active_chains check), file the gap then.

## What this doc is NOT

- Not a fix. PR #146 is the fix; this doc records its status correction
  and post-deploy verification.
- Not a closure of the broader silent-failure audit family. Other tables
  still lack §12a freshness watchdogs.
- Not a recommendation for the chain-anchor area. This was a backlog
  hygiene + verification action only.
