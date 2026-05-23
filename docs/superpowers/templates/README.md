# Gecko session templates

These templates are reusable scaffolds for Gecko-Alpha work sessions. They are intentionally **read-only by default** and include explicit operator gates.

## When to use which

- `implementation_session.md` — when you expect to plan → design → build → PR.
- `findings_only_session.md` — when the correct outcome is a findings/no-build report.
- `runtime_state_verification.md` — when decisions depend on runtime state (DB, env, flags, external service config).
- `vendor_probe_packet.md` — when a vendor sample/probe is needed (must be operator-approved if paid).
- `pr_review.md` — for structured PR review with risk gates and verification expectations.
- `no_build_decision.md` — when you explicitly decide not to build (drift closure, operator gate, infeasible).
- `closeout_report.md` — end-of-block closeout summary (done/blocked/parked/next operator action).

## Required sections (all templates)

- `**New primitives introduced:**`
- `## Hermes-first analysis` (drift-check → Hermes Skills Hub → awesome-hermes-agent verdict)
- `## Operator-only gates` (explicit list of gates relevant to the session)
- `## Runtime-state verification` (if any claims depend on prod state outside git)

