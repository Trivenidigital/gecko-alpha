# Design: overnight closeout reconciliation — 2026-10-02

**New primitives introduced:** NONE

## Intent and boundary

Correct stale task-state records using fresh, independently checkable evidence.
This is a documentation-only reconciliation, not a receipt-collection,
deployment, runtime-repair, dashboard, signal-policy, vendor, or trading
change.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Orchestration / scheduling | Existing Hermes role and public ecosystem orchestration options | Retain the in-repo operating model; do not install or replace anything |
| Durable runtime provenance | None verified that can establish Gecko service or CI truth | Use git, GitHub check-run metadata, and bounded runtime observation |

The public Skills Hub catalog was still loading when checked on 2026-10-02.
The awesome-hermes-agent catalog describes optional scheduler/orchestration
surfaces, but also requires trust-boundary review before enabling unattended
jobs. Verdict: no listed capability replaces Gecko's existing runtime and
provenance evidence; add no dependency or custom primitive.

## Evidence model

1. `origin/master` at `5281f047` is the source-code truth for the PR #592
   merge. GitHub run `35140345937` is the separate exact-head CI receipt for
   head `65d93065`; neither fact is inferred from the other.
2. The bounded production observation at 2026-10-02T05:19:57Z is unit status
   only: pipeline/dashboard/Hermes/timer are active, timer last triggered at
   01:24:20Z, and the worker has no main-process start timestamp. It neither
   proves a successful loop run nor permits OAuth repair.
3. The raw production porcelain count is 11. It is not classified, and that
   checkout is not used for source-revision attestation.

## Change and safety

Change only the durable closeout records:

- Add this design and the evidence-only plan/status record.
- Mark the stale PR #592 CI/merge task complete with its distinct merge and CI
  receipts.
- Add the PR #594 reviewer-clearance declaration and a durable reviewer/fold
  record; neither changes runtime behavior.
- Mark PR #592's four Linux synthetic mutation proofs complete: the exact-head
  CI receipt completed successfully. Leave production receipt collection,
  account/OAuth recovery, a named non-production Linux target for later
  collection work, and all operator-only actions explicitly open.

No test behavior, runtime configuration, service, data, or public API changes.
Rollback is a normal documentation revert; no production restore is needed.

## Verification and review

- Two plan reviews use factual/drift and operational-safety attack vectors.
- Two design reviews verify evidence separation and scope containment.
- Two final reviews verify the exact resulting diff and claims; retain their
  reviewer/vector, SHA, finding, fold, and verdict in the durable review record.
- Run `git diff --check` and a focused text assertion that the stale unchecked
  PR #592 four-proof line is absent while the no-production-collection gate
  remains.

## Operator-only gates retained

OAuth/account repair, paid vendor calls, receipt collection on production,
runtime deployment/configuration, destructive DB work, dispatch policy,
source/signal changes, execution, sizing, and pruning remain outside this run.
The immediate operator action is to restore the intended worker login without
bypass and name a non-production Linux target for the pending proof/collection
workflow.
