# Receipt timeout CI validation review record

Base: b5daecfc3ca39ca4719ce4f20d4f9a32d4660ac6. Selected residual: synthetic Linux cleanup proof for PR589's receipt inventory design. No production collector or data read is included.

| Gate | Logic / test validity | Ops safety / concurrency | Disposition |
|---|---|---|---|
| Plan 4736f19b | blocker_review approved | timeout_ops approved | Readiness, group ownership, bounded reaping and failure cleanup moved into design |
| Design 4be827cf | blocker_review approved | timeout_ops approved | No blocking findings; implementation must guard PGIDs and clean group before worker termination |
| Initial PR590 f972e992 | blocker_review approved logic/silent-failure | timeout_ops approved ops/concurrency | Code inspection only; later Linux failure prevents merge or cleanup-readiness claims |

## State checked
Current origin/master b5daecfc includes PR589, merged 2026-09-14T22:40:38Z. Prior automation memory omitted this closeout; fresh repo/GitHub evidence supersedes memory. AGENTS.md is absent in this checkout; supplied AGENTS rules, CLAUDE.md, lessons, todo and top backlog snapshot/reconciliation were read.

Identity-only production preflight 2026-09-16T00:23:29Z returned revision 77751890c9f1f51ed348c365d4e7a5985ea2827d and active pipeline/dashboard. This does not attest hashes, logging, receipts, flags or database state.

Docker version failed with missing dockerDesktopLinuxEngine pipe; WSL lists only docker-desktop and cannot execute python3. Independent review identified existing Ubuntu PR CI as the alternative. Local Docker repair is optional, not an operator gate. Historical inventory remains unexecuted and evidence dimensions remain UNKNOWN.

## Verification boundary
Windows discovery/syntax checks passed with five explicit Linux skips; they are not Linux cleanup proof. Linux CI run35040279065/job104618372972 ran all five cases: three passed, two failed before fallback cleanup. The descendant case retained producer2340 and child2343 in group2338; the TERM-ignoring case retained producer2407 in group2405. PR590 was converted to draft; assertions and original wrapper were preserved. No merge or deployment.

Initial independent code approvals are not runtime validation. Both reviewers examined the failing evidence and agreed collection must remain blocked. Diagnostic candidate0fed34b3 adds bounded process-state/exit/elapsed/version evidence; it changes neither wrapper nor acceptance conditions. A reviewed replacement process-lifecycle design is required before changing the wrapper.

## Confirmed Linux failure
Diagnostic [run35040455363, job104618917638](https://github.com/Trivenidigital/gecko-alpha/actions/runs/35040455363/job/104618917638) ran candidate0fed34b31c10cd05e18d6a1c86850c78aaae9ca9 with GNU coreutils9.4. Five cases ran in44.631s: three passed, two failed.

| Case | Wrapper exit / elapsed | Before-fallback observation |
|---|---|---|
| Pipe-holding descendant | 124 / 10.035s | Producer2316 and child2319 remained in PGID2314, both STAT=S (live sleeping, not zombies) |
| TERM-ignoring producer | 124 / 10.004s | Producer2383 remained in PGID2381, STAT=S |
| Silent producer | 124 / 10.005s | Group empty |
| Stderr flood | 124 / 10.002s | Group empty |
| Detector negative control | Deliberately live group detected | Explicit cleanup then proved group empty |

Each failed case checked after up to two seconds of reaping without sending cleanup signals. The only exception shown is the pre-fallback assertion; the finally block subsequently killed/reaped the isolated fixture group and its post-cleanup empty assertion raised no error.

Mechanism: `timeout` supervises its immediate Bash child. When TERM ends Bash, `timeout` returns before the delayed KILL can enforce descendant cleanup. The live fixtures remain in the original group; the producer is adopted by the test subreaper. This observed path matches [GNU9.4 timeout source](https://raw.githubusercontent.com/coreutils/coreutils/v9.4/src/timeout.c): cleanup arms escalation (lines192-198), while the main wait tracks only monitored_pid and returns after it exits. Process-group membership alone is insufficient. No claim is made about untested hosts or production process states.

**Disposition:** draft findings/reproducer PR590, deliberately red cleanup assertions. Do not merge or use the PR589 wrapper for collection. Do not convert failures into skips/xfails, kill fixtures before the assertion, or treat exit124 alone as proof of cleanup. Next engineering work is a reviewed replacement supervisor design followed by these Linux falsifiers; no operator decision or Docker repair is required to start that work. Broader backlog children remain unexamined.

Next product gate after cleanup proof: implement and adversarially test reducer/META parsing, then perform the separately approved bounded receipt inventory after fresh source identity checks. No trust ranking, warning removal, paid calls or activation follows from cleanup success.
