# Gecko autonomous operating model (Hermes ↔ Codex ↔ Operator)

**Status:** V1 (read-only governance + handoff)

## Roles

### Hermes (orchestrator)

Owns:
- durable memory of decisions, gates, and “what’s next”
- scheduling / routing of recurring runs
- lightweight enrichment (classification, summarization) where it is not the source of truth

Does not own:
- production truth for price/identity/execution/PnL
- durable gecko-alpha DB writes
- code changes, tests, or PR hygiene

### Codex (repo-grounded worker)

Owns:
- reading the repo + backlog + tests to discover in-tree reality (drift-check)
- planning, designing, implementing, verifying, and producing PR-ready diffs
- writing durable artifacts in-repo: plans/designs/findings/runbooks/templates/scripts

Does not own:
- operator-only actions (see gates below)
- paid vendor calls (without explicit operator approval)
- production secret changes

### Reviewers (parallel agents)

Own:
- orthogonal “attack vectors” review (safety/runtime state, structural/code, strategy/judgment, prod-state)
- critical/important folds that must be applied before shipping

### Operator (human-in-the-loop)

Owns:
- explicit approval for operator-only gates
- runtime-state verification when it requires prod access (SSH, DB, secrets, external accounts)
- final authority on go/no-go for any move toward live execution

## Operator-only gates (must be explicit)

Requires explicit operator approval:
- paid APIs or paid vendor sample calls
- live trades or order execution
- position sizing / capital allocation
- source/KOL deletion, pruning, or suppression
- signal auto-disable/enable or threshold changes affecting live/paper dispatch
- destructive DB writes, data deletion, irreversible migrations
- changing production secrets, paid quotas, or external account state

## Runtime truth sources (and when Codex must defer)

| Truth domain | Source of truth | Accessible to Codex in sandbox? | Notes |
|---|---|---|---|
| Shipped code + contracts | git repo (`origin/master`) | yes | drift-check closes proposals |
| Backlog status | `backlog.md`, `tasks/todo.md` | yes | docs are not runtime truth |
| DB state (tables/rows) | prod `scout.db` | no | operator must verify |
| Service health | systemd + logs | no | operator must verify |
| .env / flags / secrets | production config | no | operator must verify |
| Vendor quotas/billing | vendor account | no | operator must verify |

## Multi-vector review dispatch

Trigger multi-vector review when:
- operator flags “critical / most important”
- change is expensive to revert (schema, durable audit surfaces, external boundaries)
- change touches money, execution, pruning/suppression, or irreversible state

Suggested orthogonal vectors:
- **Prod-state**: does runtime state match assumptions?
- **Structural/code**: does the lever actually get reached? any hidden overrides?
- **Strategy/judgment**: is this the right thing to do now vs cheaper alternative?
- **Statistical**: is the claimed effect real (n, regime, multiple comparisons)?

## Dashboard safety note (read-only surfaces only)

The dashboard server includes write endpoints; do not add UI affordances that could be mistaken for enabling writes unless auth/network restriction is explicitly verified by the operator.

