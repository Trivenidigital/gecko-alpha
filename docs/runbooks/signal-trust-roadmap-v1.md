# Signal trust roadmap V1 (read-only)

This runbook is the V1 onramp for `BL-NEW-SIGNAL-TRUST-ROADMAP`.

V1 is **visibility-only**. It MUST NOT be used for pruning, suppression, auto-disable, sizing, or live execution decisions.

## What ships in V1

- Registry (JSON): `docs/superpowers/registries/signal_trust_registry.v1.json`
- Validator (Node): `scripts/validate_signal_trust_registry.mjs`
- Read-only export + dashboard tab: `GET /api/signal_trust_registry` + "Signal Trust (V1)" (merged in-tree via PR #239, merge commit `050fe12b`; does not assert prod deployment)

## What V1 is (and is not)

**Is:**
- A lightweight, human-readable place to record per-signal maturity state and the next data-bound gate.
- A coordination surface between Hermes (orchestration) and Codex (repo-grounded execution) without touching production behavior.

**Is not:**
- A source of runtime truth. Any claim that depends on production state must be verified via runtime evidence (DB queries, service health, deployed endpoints).
- A scoring engine.
- A policy engine.

## Registry invariants (hard gates)

The registry MUST keep these invariants true:
- `experimental=true`
- `visibility_only=true`
- `not_for_pruning=true`
- `not_for_auto_disable=true`
- Every entry includes:
  - `signal_type`
  - `maturity_state` (enum)
  - `data_quality` (warnings/caveats)
  - `operator_gate` (must include `visibility_only`, `not_for_pruning`, `not_for_auto_disable`)
  - `next_gate` (data-bound threshold; not calendar-bound)

If any future proposal wants to consume this registry in production logic, that is operator-gated and requires a separate plan/design/review cycle.

## How to validate

From repo root:

```bash
node scripts/validate_signal_trust_registry.mjs
```

Or with an explicit path:

```bash
node scripts/validate_signal_trust_registry.mjs --path docs/superpowers/registries/signal_trust_registry.v1.json
```

## How to update safely

1. Make the smallest possible edit to the JSON.
2. Add or update the `data_quality.warning` field with the uncertainty and the missing evidence.
3. Keep `next_gate.threshold` explicit and data-bound (example: "verify 7d/14d/30d cohorts"; "min_sample>=10 and coverage>=0.50 with temporal integrity").
4. Run the validator.
5. Record the evidence path (DB query, runbook, deploy SHA) in the session's findings doc. Do not embed runtime "facts" into the registry without citing where they were verified.
