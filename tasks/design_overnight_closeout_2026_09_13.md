**New primitives introduced:** NONE

# Design: bounded autonomous status evidence

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Orchestration | Existing Hermes scheduling; Skills Hub catalog unavailable in fetched page | Preserve it; no scheduler integration |
| Repository status | No replacement verified | Amend existing reporter text and governance docs |

Checked https://hermes-agent.nousresearch.com/docs/skills and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13.
Awesome ecosystem verdict: orchestration tools do not replace evidence about
this checkout; no new library, skill installation or custom primitive.

## Plan review folds

Two independent parallel reviews approved (scope_audit: evidence/logic;
loop_audit: authority/safety). Fold their nonblocking precision: do not imply
the reporter parses reconciliation or inspects external state; sanitize runtime
reports; wait for every registered PR reviewer to finish before merge.

## Exact behavior

1. Backlog heading becomes `Backlog anchors (historical; best-effort)`.
   An unconditional note says these are extracted item headers, not the current
   work queue, and tells readers to follow reconciliation and verify the named
   forward tracker before scoping superseded items. No parser or existence
   claim about that tracker is added.
2. After runner candidate output, on BOTH branches, emit:
   `External scheduling and run history: NOT INSPECTED by this local report.`
   `Candidate files or their absence do not establish activation, execution,
   or first-run status; verify the scheduler and run artifacts separately.`
   Remove the unconditional manual/runbook first-run inference.
3. Align status runbook interpretation; distinguish saved configuration,
   observed invocation, verified completion and historical attestation.
4. Role map makes read-only DB/service/config access session-dependent, subject
   to existing authorization and secret-safe evidence. Access never grants
   mutation authority. Current operator-only gates remain unchanged.
5. Add dated backlog note and closeout report: legacy successor exact path
   unavailable, parents remain superseded; observed external Codex config is
   separate from Hermes scheduling. Do not rewrite historical snapshot entries.

## Verification and ship boundary

Update existing no-runner and runner-present regression tests to require the
unknown-external-state text and reject the manual inference. Add a backlog
qualification assertion. Run focused pytest, node syntax and direct report.
No network/DB/SSH/external file reader or new CLI option in reporter.

Commit artifacts, open PR, obtain two parallel independent reviews covering
logic/concurrency and ops-safety/silent-failure. Clearance records may name
only SHAs actually cleared by those reviewers. Fold all actionable findings;
any watched change requires renewed relevant review. Merge after exact-head
CI green. No production deploy is necessary for local tooling/docs; rollback
is a revert PR through normal review/CI, with no runtime restore.

Current production-push permission is the action authorization. Findings report
will record its scope, actions, timestamps and exact limitations.
