**New primitives introduced:** NONE beyond the revision5 design.

# Receipt cleanup revision7 amendment — candidate, NO BUILD

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Existing supervisor design and fault harness | Prior bounded checks in the revision5 design; not re-run | Amend existing design only; no new primitive |

Awesome-hermes-agent and self-evolution checks remain the historical checks in
the parent design. Verdict: this amendment adds no ecosystem scope or runtime
re-attestation.

This amendment explicitly replaces the named paragraphs and rows of
`design_receipt_cleanup_replacement_2026_09_16.md` at38dd5cca. Other text remains
unchanged, not newly approved. Both files together are the candidate revision7.
Build remains blocked until two independent design approvals.
**Section 6, replace "Identity acquisition, normal path" and "Release handshake":**

**Identity acquisition and acknowledgment, normal path.** After readiness records appear, W reads `DIR/wrapper.pgid` (retry until present, 4 s bound), asserts `/proc/<G>/stat` has `pid == pgrp == session == G` and `ppid == S.pid`, and that every readiness record's `pgid == G`. S's `Popen.pid` is never a group id. Only after all three checks pass does W write `DIR/identity.ready` by atomic rename, then `DIR/release`. W waits for exactly one supervisor-side file before writing `identity.ready`: `wrapper.pgid`, which S publishes before any gated fault can act. W never waits for `fault.fired` before writing `identity.ready`, and every identity-gated fault waits for `identity.ready` before writing `fault.fired`, so the dependency order is acyclic. For gated faults W then waits for `DIR/fault.fired` (4 s bound, failure `FAULT_NOT_FIRED`) while continuing to drain, and later asserts `fault.fired` `st_mtime_ns` is not earlier than `identity.ready`'s. Because `release` follows `identity.ready`, no release-gated fixture can let L exit, and therefore no cleanup phase can begin, before W holds verified identity.

**Publication-free path.** `publish_error` and `die_before_publish` never produce `wrapper.pgid`, so W performs no live `/proc` acquisition and writes no `identity.ready`. W waits for `producer.json` (4 s), then for `fault.fired` (4 s), asserts `wrapper.pgid` absent, and takes identity from the readiness record, cross-checked against status `pgid` (`publish_error`) or the kernel-derived group from recovery (`die_before_publish`). S-side these faults gate only on `producer.json`, written by the fixture independently of W, so neither side waits on the other.

**Section 6, replace the fault table rows:**

| Fault | Gate (bounded 4 s, failure exit 13) | Behavior |
|---|---|---|
| `stall_wait` | `identity.ready` present | `tick("wait")` writes `fault.fired`, then `supervisor.stalled`, then sleeps forever |
| `stall_cleanup` | `identity.ready` present, checked in `tick("wait")` before any cleanup | first `tick("cleanup")` writes `fault.fired`, then `supervisor.stalled`, then sleeps forever |
| `publish_error` | `producer.json` present | `publish` writes `fault.fired`, raises `OSError`, writes nothing else |
| `die_before_publish` | `producer.json` present | `publish` writes `fault.fired`, self-SIGKILLs, writes nothing else |
| `die_after_ready` | `identity.ready` present | `tick("wait")` writes `fault.fired`, self-SIGKILLs |
| `capture_error` | `identity.ready` present, awaited inside the first `read_capture` call | writes `fault.fired`, raises; no later call occurs because capture is disabled |
| `inject_signal:startup:<n>` | none | rendezvous writes `fault.fired`, raises the signal n times |
| `inject_signal:wait:<n>` | `identity.ready` present | `tick("wait")` writes `fault.fired`, raises n times |
| `inject_signal:cleanup:<n>` | `identity.ready` present, checked in `tick("wait")` before any cleanup | first `tick("cleanup")` writes `fault.fired`, raises n times |

Gate ordering is therefore `producer.json` → `identity.ready` → `fault.fired` → action for every identity-gated fault. A gate wait blocks S for at most 4 s, inside S's 13 s wait partition; `capture_error`'s gate sits in `read_capture`, which S calls after publication and before the first `tick("wait")`, so W can acquire identity while S waits.

**Section 6, replace the "Fault-specific identity flow in W" table:**

| Faults | W identity flow |
|---|---|
| `stall_wait`, `stall_cleanup`, `die_after_ready`, `capture_error`, `inject_signal:wait`, `inject_signal:cleanup` | normal path; write `identity.ready`; write `release`; wait `fault.fired`; assert `fault.fired` not earlier than `identity.ready` |
| `publish_error`, `die_before_publish` | publication-free path above; no `identity.ready` written |
| `inject_signal:startup` | no identity: assert `wrapper.pgid`, `identity.ready` and readiness records absent, `fault.fired` present, `teardowns` 0 |

**Section 6, replace the W trigger sentence:** W-level recovery cases use the same routine with `direct=S`: W triggers on `supervisor.stalled` (written by `stall_wait` and `stall_cleanup`), on pipe EOF without a complete status line, or on `D_S`.

**Section 5.1, add class assignment after the deadline table:** Timeout classes: silent, stderr_flood, ignore_term, descendant, continuous_flood, raw_wrapper_leak_*, and `capture_fault`. Fast classes: every remaining non-recovery case. `capture_fault` is a timeout class because the accepted capture-fault semantics keep the lifecycle unchanged: a capture-side defect must not shorten the inner bound or abort a workload whose output may still complete, so S runs the ignore_term fixture to the inner timeout near `T0+10`, cleans up by `T0+12`, and reports before `T0+15.5`. Under `D_S` 8 W would kill a healthy S before `SUPERVISOR_ERROR` could be reported; under `D_S` 17 the existing 9 to 16 s external check applies unchanged. Cleanup faults instead use the `early_exit_orphan` fixture so the cleanup phase begins immediately after release rather than at the inner timeout, keeping them fast class while retaining a live TERM-ignoring member for `killpg` to act on.

**Section 7, replace these case rows:**

| Case | Expected | Discriminator |
|---|---|---|
| capture_fault (timeout class) | SUPERVISOR_ERROR, `error` capture | ignore_term fixture; `identity.ready` written before `fault.fired`; external 9 to 16 s; inner exit in timeout set; `dropped` 0 and frozen; `output` null; `kills ≥ 1`; `cleanup_proof` true; empty |
| stall_cleanup | W post-kill reaping recovery | `early_exit_orphan` fixture; `identity.ready` precedes `release` precedes `fault.fired`; S's cleanup loop issues `killpg` under L3 once, then the first `tick("cleanup")` writes `fault.fired`, `supervisor.stalled`, and hangs before S reaps. W triggers on the marker: `pid_kills == [direct S, state S or R]`; the killed members reparent to W and are reaped through the anchored loop, so `reaped ≥ 1`; `kills` may be 0 and `live_before_signal` may be 0; `scan.complete` true; G equals published and readiness pgid; empty oracle. This proves recovery of a group already signaled but unreaped by a stalled supervisor. Pre-kill live recovery is proved independently by `stalled_supervisor` and `parent_live_supervisor`, whose `live_before_signal ≥ 1` and `kills ≥ 1` requirements are unchanged |
| injected_cleanup_repeat | INTERRUPTED | `early_exit_orphan` fixture; `inject_signal:cleanup:2`; `fault.fired` not earlier than `identity.ready`; `teardowns` 1; `signals_received` 2; `signal_phase` cleanup; empty |
| die_after_ready (used by parent_zombie_supervisor, parent_reaped_supervisor) | as in revision 5 | additionally `fault.fired` not earlier than `identity.ready`, so W held verified identity before S died and the workload was never signaled by S |
| stalled_supervisor, parent_live_supervisor | as in revision 5 | additionally `fault.fired` not earlier than `identity.ready` |

**Section 8, add fold row:**

| Finding | Fold | Where |
|---|---|---|
| 7. Identity-gated faults could act before W acquired identity; `stall_wait` ordering; `capture_fault` misclassified | `identity.ready` acknowledgment written by W only after live `/proc` and readiness checks; every identity-gated fault waits for it before `fault.fired` and action; cleanup faults gate in `tick("wait")` and use the release-gated `early_exit_orphan` fixture so cleanup cannot precede identity; publication-free faults gate only on `producer.json` with no W-side ack, avoiding circular waits; `capture_fault` assigned timeout class `D_S` 17 | sections 5.1, 6, 7 |

**Section 9, add residual line:** The 4 s fault gates are harness synchronization only and do not exist in the production script; a gate timeout is exit 13 from the fault runner and fails the case, never a silent fallthrough.
