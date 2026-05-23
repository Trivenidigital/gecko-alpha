# Implementation session template

**New primitives introduced:** (fill; `NONE` if docs-only)

## Goal

One sentence: what “done” means for this session.

## Drift-check (does it already exist in-tree?)

- Grep results (file paths + line evidence) for any primitives you plan to introduce.
- If an in-tree match fully covers the need, stop and switch to `no_build_decision.md`.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| <domain> | <yes — name + url> OR <none found> | <use it> OR <build from scratch (rationale)> |

Awesome-hermes-agent ecosystem check: <checked repos/index; verdict>.

## Operator-only gates

Do not cross without explicit operator approval:
- paid APIs / paid vendor sample calls
- live trades / order execution
- position sizing / capital allocation
- source/KOL deletion, pruning, or suppression
- signal auto-disable/enable or threshold changes affecting live/paper dispatch
- destructive DB writes, data deletion, irreversible migrations
- changing production secrets, paid quotas, or external account state

## Runtime-state verification

List assumptions that depend on runtime state outside git, and how to verify.

| Assumption | Where verified | Current value | Active/enabled? | Path reaches lever? | Fire-rate sanity |
|---|---|---|---|---|---|
| <assumption> | <query/log/runbook> | <value> | <yes/no> | <yes/no> | <n/day> |

## Plan

- Step 1:
- Step 2:
- Step 3:

## Plan review (2 parallel agents)

- Reviewer A (vector): <notes + folds>
- Reviewer B (vector): <notes + folds>
- Folds applied: <yes/no + summary>

## Design

### Public contract (if any)

- Inputs:
- Outputs:
- Error semantics:

### Safety invariants

- Read-only constraints:
- No-op guarantees:
- Backward compatibility:

## Design review (2 parallel agents)

- Reviewer A (vector): <notes + folds>
- Reviewer B (vector): <notes + folds>
- Folds applied: <yes/no + summary>

## Build

- Files changed:
- Tests added/updated:

## Verification evidence

- Commands run:
- Output summary:
- Known limits / not verified:

## PR

- Branch:
- PR link:
- PR review folds (2 parallel agents):

## Deploy / smoke (only if allowed)

- Rollback note:
- Smoke evidence:

