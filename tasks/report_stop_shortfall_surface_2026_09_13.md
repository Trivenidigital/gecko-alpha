# Overnight closeout: historical entry-stop display

## Delivery status

PR575 independently approved; exact-head CI pending. NOT merged or deployed yet.
This slice advances DASH-09, not its all-history aggregate or the whole backlog.

## Change and review evidence

Existing closed-trade history gains a strictly eligible recorded entry-stop
shortfall object and a table column with entry-stop and recorded-return context.
Historical paper / EXPERIMENTAL; not for pruning, sizing or dispatch.
Existing PnL, history rows, pagination, filters and counts are preserved.

Two parallel plan reviews approved after paper semantics and exclusion-reason
folds. Two parallel design reviews approved without further folds. Reviewers:
stop_gap_audit (structural/concurrency) and stale_pr_audit (attribution/ops).
Plans/design are in same-date stop_shortfall_surface artifacts.
Final PR reviews both APPROVE fce7c7c98a6fb60ea388b76e43d30f3640d4ec67:
stop_gap_audit logic/concurrency and stale_pr_audit ops-safety/silent-failure.
Each independently ran 81 targeted tests. No final PR folds remain.
Clearances recorded in .reviewers/575.toml; exact-head CI remains required.

## Verification

- Existing trading dashboard baseline: 26 passed before implementation.
- New test-first run failed at missing helper import, before helper existed.
- Focused arithmetic/exclusions/schema/query-failure plus trading/outcomes/layout:
  134 passed; no tests deleted.
- Dashboard contract suite: 118 passed.
- npm ci --ignore-scripts and npm run build succeeded; built dist is included.
- Browser fixture at 127.0.0.1:8876: visually verified three rows showing
  available 3.00 pp (entry stop 25%, exit return -28%), Modeled and Unavailable
  with price-source reason. Fixture PnL deliberately -99% proves separate
  calculation. No production data was copied into the preview.
- Production mode=ro/query_only transaction 2026-09-13T22:28:22.642125Z using
  candidate pure classifier: 355 stop exits => 23 available, four modeled,
  328 unavailable (324 legacy source, four conviction modified). Mean for the
  23-row validation cohort 1.3471808080779946 pp, matching prior findings.
  This mean is validation evidence, not a new UI aggregate or ranking.
- An initial in-memory smoke harness shadowed datetime and failed; corrected
  harness imports without namespace collision. No product defect was inferred.

## Runtime and ownership

Production observed d2f0d61e, tracked clean. Exact service inventory confirms
gecko-pipeline, gecko-dashboard and hermes-gateway active. The initial checks
of nonexistent gecko-alpha/hermes names are not service-health evidence.
No new DB tables, mutations, external sends, paid calls or configuration changes.

Prepare for improvements (task 01a09bfb-c05b-7de3-9ebf-0efa353bdf2e) is active
and owns capture reliability work. This task does not take over its files,
PRs or deployment. Verify ownership again before any production action.

## Remaining and no-action items

- Existing templates, role map, reporter and V1 cockpit/trust parents: no rebuild.
- DASH-09 aggregate remains open; a page mean would be misleading.
- Postmortem UI, suppression dashboard integration and centralized routing
  remain engineering residuals, not operator-only blockers.
- PR570 retains three unique historical docs despite analyzer supersession;
  read-only audit recommends preserving it, not closing as a duplicate. Its
  existing approval evidence/ownership needs separate verification before merge.
- Successor tracker remains untracked in main checkout; reconciliation into a
  shared queue remains due. Automation cadence remains hourly with six-hour
  mission; prompt should consult reconciliation/current queue, actual prior
  run state and active owner before shared mutations. No config adjusted here.
- Paid vendor/trading/sizing/pruning/dispatch/secrets/destructive gates remain.

## Approval record and rollout

| Action | Class | Authorization | Result |
|---|---|---|---|
| Read-only implementation/feature push | dashboard/API | Current automation production-push request | Implemented; PR575 two-vector review approved |
| Merge | conditional | Same request: exact-head CI and two-vector approval | Not yet performed |
| Deploy | conditional | Same request: smoke/rollback plus current ownership | Not yet performed |

Rollback requires no data restoration: display-only source/dist revert via
reviewed PR or prior deployed revision after preserving unrelated changes.
Do not fast-forward production over separately owned capture changes merely
to ship this display. Next immediate step: complete PR reviews and exact-head CI.
