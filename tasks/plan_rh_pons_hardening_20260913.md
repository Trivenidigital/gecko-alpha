**New primitives introduced:** durable RH scan checkpoint (within existing DB layer), RH liveness watchdog using existing watchdog infrastructure; no new trading or ranking mechanism.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| EVM scan checkpoint and canonical event projection | none found; Skills Hub client catalog not readable | Repair existing Python collector and SQLite layer; agent skills do not replace transactional ingestion |
| Discovery latency identity | none found | Repair existing harness with chain-qualified identity |
| Liveness | existing repository watchdog and heartbeat infrastructure | Reuse existing mechanisms, no external daemon dependency |

Checked https://hermes-agent.nousresearch.com/docs/skills/ and
https://github.com/0xNyk/awesome-hermes-agent on 2026-09-13.
Ecosystem verdict: no verified drop-in replacement for these repo-specific repairs;
reuse existing implementation and monitoring conventions.

# RH discovery hardening implementation plan

Goal: finish the reviewed observe-only increment so early-launch collection,
recovery and latency evidence are correct before activation.
Spec: tasks/design_rh_pons_discovery_delta_2026_09_13.md and
tasks/review_claude_rh_pons_2026_09_13.md. User authorized implementation and
Claude/Codex collaboration on 2026-09-13. Retain default-off and ineligible stamps.

## Tasks and ownership

- [ ] Claude Code: failing regressions, then scan checkpoints independent of
  rows, head/chain verification, emitter coverage including launch-block trades,
  real block clocks, RPC error handling and URL redaction. Own rh_pons.py,
  scout/db.py, scout/config.py, migration registry and collector tests.
- [ ] Claude Code: idempotent crash replay and canonical projection reconciliation;
  append-only evidence remains immutable. Checkpoint only completed work;
  bounded overlap verifies recent canonical blocks. Heartbeat successful scans
  even when no launches exist, using ingest_watchdog_state source rh_pons.
- [ ] Codex latency worker: failing regressions then chain-qualified comparisons,
  explicit invalid clocks/censoring, honest paired summaries. Own harness/tests.
- [ ] Codex: reuse watchdog infrastructure; behavior tests for disabled, fresh,
  stale and failure cases; document deployment wiring and measurable SLO.
- [ ] Codex: first-party network and onchain verification attempts; record
  raw public facts and unresolved deployment/ABI boundaries without fabrication.
- [ ] Integrate changes, review along structural and durable-evidence axes,
  run focused plus relevant regression tests, fix attributable failures.
- [ ] Commit finished code and report precise acceptance status and residual
  external blockers. No claim of live effectiveness from synthetic fixtures.

Runtime assumptions before activation: chain ID matches verified network;
factory bytecode and deploy block exist; real event layouts match; active RPC
and flags reach collection; event rate supports measurable latency sample.
Current production values are not known. Live activation requires those facts,
not just a green test run. Pons docs currently return a region-unavailable page.
