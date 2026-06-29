# LOOPS.md — how long-running agent work proceeds in gecko-alpha

Lightweight conventions for multi-session agent work. The goal is **continuity without
re-verification loops** and **no harness bloat**. Adopt these lightly: they describe how to resume and
hand off work, not a gate to pass on every change.

## 1. Read state before asking "what's next"
Before re-checking PRs/CI/branches or asking the operator what to do, **read the effort's STATE file
first**. Most "let me re-verify everything" loops are answered there. Re-verify only what STATE says is
unknown or stale.

## 2. Three durable files per major effort
Keep these append-friendly and current; they survive session restarts and summarization.

- **STATE** — current status, the single *next allowed action*, blockers, and operator boundaries.
  (What is true right now and what may happen next.)
- **CONTRACT** — acceptance criteria, explicit out-of-scope, and rollback/stop gates.
  (What "done" and "safe" mean for this effort.)
- **TRACE** — append-only evidence: commands run, PR/CI links, runtime checks, DB queries, log greps.
  (Proof, not narration.)

These can be literal files or an agreed mapping onto existing artifacts (see the worked example below).
Don't create ceremony for small efforts — a memory entry + a PR can serve all three.

## 3. Evidence over summaries
A PR/commit summary is not proof. Reviews and status claims **cite CI runs, the exact commands, DB
queries, log lines, and runtime observations** — and link them (TRACE). "It says it's done" ≠ done.

## 4. Roles stay separate
- **Planner** writes the spec/CONTRACT.
- **Implementer** writes code to the contract.
- **Reviewer** attacks the change along *orthogonal* vectors — data-path and silent-failure modes, not
  style. Independent of the author where the change is irreversible or touches money/exits/audit/schema.
- **Operator alone** approves deploys and flag flips. Agents stop at that boundary.

## 5. "File unchanged" is not proof of behavior unchanged
For observe-only / no-regression PRs, **verify the upstream input contract, not just the file diff.**
`scorer.py` untouched does **not** prove scoring is stable — an upstream parser mutating a field the
scorer reads is a silent scoring change. Test the *real input path* (e.g., the actual parser → scorer),
not a hand-built object. Keep instrumentation/observe-only data in separate fields the decision code
never reads. *(Origin: PR #385 BLOCKING-1, where GT `transactions.h1` fed the scorer's `buy_pressure`
while `scorer.py` had zero diff.)*

## 6. Harness hygiene — keep what catches real regressions, retire what doesn't
Keep checks that protect current failure modes: **scoring drift, alert/TG drift, fresh-but-empty
pipeline tables, migration risk, watchdog delivery.** Retire checks that no longer guard a live failure
mode. A check that never fails on a real regression is noise; delete it.

## 7. Stop gates are first-class
Every effort that ships behind a flag or into prod names its **stop gates** in CONTRACT: when to halt,
roll back, or hold for the operator. Reaching a "proceed" threshold never auto-authorizes the next
irreversible step.

---

## Worked example — DEX-outcome instrumentation (2026-06-28/29)
The three roles mapped onto existing artifacts (no new ceremony):
- **STATE** → memory `project_ansem_under_gate_backtest_2026_06_28.md` + `runbook_dex_instrumentation_enablement_2026_06_29.md` §0.
- **CONTRACT** → `spec_dex_outcome_instrumentation_i1_i2_i3_2026_06_28.md` + runbook §2/§4 (enablement preconditions + proceed/stop gates).
- **TRACE** → `findings_*` docs + PRs #383–#386 + their CI runs + the runbook's `sqlite3`/`journalctl` query pack.

Outcome of applying §3/§4/§5 here: a multi-vector review caught the §5 scorer-input leak that a
single "scorer.py unchanged" check passed; the operator boundary held (nothing deployed/enabled by the
agent). The repeated re-verification we hit around #383–#386 is exactly what §1 exists to prevent.
