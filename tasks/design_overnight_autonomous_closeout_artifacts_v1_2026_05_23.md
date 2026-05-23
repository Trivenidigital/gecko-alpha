# Design — Overnight autonomous closeout artifacts V1 — 2026-05-23

**Scope:** docs/scripts only. No prod access. No secrets. No vendor calls. No DB writes.

**New primitives introduced:**
- `docs/superpowers/templates/*` + `docs/superpowers/templates/README.md`
- `docs/runbooks/gecko-autonomous-operating-model.md`
- `scripts/report_autonomous_status.mjs`
- `docs/runbooks/autonomous-status-report.md`
- (Optional) `docs/superpowers/registries/signal_trust_registry.v1.json` + `scripts/validate_signal_trust_registry.mjs`

## Goals

1. Make “overnight closeout” repeatable: every session produces the same artifacts and the same operator-facing closeout format.
2. Encode Hermes↔Codex responsibilities and operator-only gates in a durable place (runbook).
3. Provide a safe status surface that can run locally without prod/SSH (report script + runbook).
4. Provide a low-risk onramp for `BL-NEW-SIGNAL-TRUST-ROADMAP` (registry skeleton only), explicitly **EXPERIMENTAL / not-for-pruning**.

## Non-goals (explicitly out of scope)

- Any live execution, sizing, threshold changes, auto-disable/enable, pruning/suppression.
- Any paid API calls or vendor samples.
- Any DB migrations or prod DB writes.
- Any UI/API work that could be mistaken for enabling write access.

## Design: `docs/superpowers/templates/`

**Directory:** `docs/superpowers/templates/`

**Files:**
- `README.md` — short index + “when to use which template”.
- `implementation_session.md` — plan→design→build→PR→verify skeleton.
- `findings_only_session.md` — findings-only / no-build report.
- `pr_review.md` — reviewer checklist + veto gates.
- `vendor_probe_packet.md` — packet for operator-approved vendor probes; defaults to “no paid calls”.
- `runtime_state_verification.md` — assumption table + verification checklist (per backlog §9a).
- `no_build_decision.md` — explicit “we chose not to build” decision record.

**Format constraints:**
- Must include a top section: `**New primitives introduced:**` and `## Hermes-first analysis`.
- Hermes-first section must include:
  - Drift-check evidence for relevant in-tree primitives
  - Hermes Skills Hub check
  - Awesome-hermes-agent ecosystem check + one-sentence verdict
- Must include “operator-only gates” block for any item that could drift into gated territory.
- Must not enforce ASCII-only: existing repo docs contain Unicode; templates must be UTF-8 and allow Unicode.

## Design: operating model runbook

**File:** `docs/runbooks/gecko-autonomous-operating-model.md`

**Contents:**
- Role map: Hermes orchestrator, Codex worker, reviewer agents, operator.
- Truth sources: repo (source-of-truth for shipped code), prod DB / env / service configs (truth for runtime state).
- Operator-only gates: explicit list copied from the overnight assignment.
- Review dispatch: when to run 2-vector vs multi-vector reviews.

## Design: status surface

**Script:** `scripts/report_autonomous_status.mjs`

**Constraints:**
- Read-only, local-only: reads only repo files + git metadata; does not connect to network, DB, or services.
- Inputs: `--since <iso>` optional; `--out <path>` optional; defaults to stdout.
- Output: Markdown report with:
  - current HEAD commit + branch
  - changed files since `--since` (best-effort via `git log`/`git diff`)
  - status anchors for key backlog tracks (presence of BL ids + status phrases in `backlog.md`)
  - template coverage (which templates exist)
  - reminders for operator-only gates and runtime-state verification

**MUST NOT (hard gates):**
- Must not read `.env`, secrets, or any credential files.
- Must not make any network calls.
- Must not open or write any DB file.
- Must not attempt to SSH.
- Must not modify the working tree (no writes except optional `--out` file).

**Runbook:** `docs/runbooks/autonomous-status-report.md` documents how to run + how to interpret.

## Design: signal trust registry (optional V1)

**File:** `docs/superpowers/registries/signal_trust_registry.v1.json`

**Constraints:**
- Read-only registry used for operator visibility only; **not consumed by production logic**.
- The file header and every entry must clearly state: `visibility_only`, `not_for_pruning`, `not_for_auto_disable`.
- Every entry must carry:
  - `signal_type`
  - `maturity_state` (enum)
  - `data_quality` (coverage/freshness caveats)
  - `operator_gate` (visibility-only gates)
  - `next_gate` (data-bound threshold, not calendar-bound)

**Validator:** `scripts/validate_signal_trust_registry.mjs` validates schema and required fields; no scoring.

## Required reviews

- Design review by 2 parallel agents before landing non-trivial artifacts.

