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
- granting access/authorization for runtime verification and retaining authority
  over operator-only mutations; Codex may perform authorized read-only checks
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

## Runtime truth sources (access is session-dependent)

| Truth domain | Source of truth | Codex access | Notes |
|---|---|---|---|
| Shipped code + contracts | git repo (`origin/master`) | yes | drift-check closes proposals |
| Backlog status | Top reconciliation in `backlog.md`, its named forward tracker, `tasks/todo.md` | repo-local when present | verify successor exists; historical headers are not the queue |
| DB state (tables/rows) | prod `scout.db` | session-dependent, authorized read-only queries | record query/time; no mutation authority implied |
| Service health | systemd + bounded logs | session-dependent, authorized read-only checks | verify host and deployed revision |
| .env / flags / secrets | production config | session-dependent, secret-safe inspection only | report permitted flag values or presence, never secret values |
| Vendor quotas/billing | vendor account | only with appropriate access and authorization | paid calls and account changes remain operator-only |

If access is unavailable, mark the specific fact **unverified** and supply the
read-only check needed. Do not substitute source defaults or old memory for
runtime evidence. Read access never expands the mutation permissions above.

Separate runner ownership from runner evidence: Hermes is the intended durable
orchestrator; a Codex app automation can invoke workers outside this repository.
Check the actual scheduler configuration, observed invocation and completion
artifacts separately. The local status reporter cannot establish external runner
health or first-run history from the presence/absence of tracked files.

Before shared PR or production mutations, check active automation runs and
operator tasks for ownership of the same work. A recent-task listing may omit
automation tasks; inspect known prior run IDs and their latest turn status.
If another active task owns the same PR or production operation, leave that
operation with its owner. Independent read-only findings or isolated docs work
can continue. An inactive task or saved scheduler configuration alone does not
prove a production operation completed; verify its result separately.

The September 13 closeout observed hourly invocations overlapping a six-hour
mission. This guidance is a preflight convention, not a scheduler lock or an
atomic mutual-exclusion mechanism. Scheduler overlap policy still needs explicit
reconciliation; this document does not alter the automation configuration.

**Freshness caveat:** when `git fetch origin` cannot run (no credentials / restricted network), any “compare against `origin/master`” drift-check may be stale. Record the base commit SHA + commit timestamp used for the session, and treat drift conclusions as conditional until a successful fetch confirms the base is current.

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
