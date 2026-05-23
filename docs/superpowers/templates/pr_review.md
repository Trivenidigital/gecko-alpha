# PR review template

**New primitives introduced:** (copy from PR description)

## Scope statement

One paragraph: what changes and what explicitly does not change.

## Drift check (is this PR obsolete?)

- Compare against current `origin/master` (not only merge-base intent).
- Identify any primitives already shipped on master.

## Hermes-first analysis (if PR introduces new primitives)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| <domain> | <yes — name + url> OR <none found> | <use it> OR <keep custom (rationale)> |

Awesome-hermes-agent ecosystem check: <checked; verdict>.

## Operator-only gates

Confirm PR does NOT cross operator-only gates (or explicitly mark it blocked):
- paid APIs / vendor calls
- live execution or capital sizing
- pruning/suppression/auto-disable/threshold changes
- destructive DB writes or irreversible migrations
- secrets / external account state

## Safety / correctness checklist

- Read-only invariants preserved (no unintended writes)?
- Failure semantics reasonable (no silent success)?
- Idempotency where applicable?
- Backward compatibility maintained?
- Encoding/line-ending risk contained (no accidental CRLF churn)?

## Verification evidence required

- Unit/integration tests run:
- Script smoke checks:
- Any manual smoke steps:

## Review verdict

- APPROVE / APPROVE_WITH_FOLDS / REWORK / BLOCK
- Critical folds:
- Important folds:

