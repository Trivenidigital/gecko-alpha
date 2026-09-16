**New primitives introduced:** NONE beyond the two disposable artifacts the approved plan already names (`scripts/receipt_inventory_sequence.py`, `tests/test_receipt_inventory_sequence.py`). This amendment removes one tool (`scp`), scopes two unresolved prerequisites that block production execution (local transport containment, aggregate physical local-storage bound), and binds ownership, provenance and quoting. It introduces no containment mechanism, no dependency, schema, secret, config, host write outside the licensed temp directory, or persistent coordinator. If a prerequisite later needs machinery, that machinery is declared in its own separately reviewed amendment, not here.

# Plan amendment 1, revision 2: production read-only receipt inventory integration

Amends `tasks/plan_receipt_inventory_integration_2026_09_16.md` (approved at 035cd914). Design PR593 at b3fe776a (revision 15216a1f) stays REJECTED and is not amended. Revision 1 of this amendment received REQUEST CHANGES from both plan reviewers; this revision folds all six findings. Approval of this amendment is **prerequisite-scoping only**: it does not claim that transport containment or the physical storage bound is proven. No build, staging, runtime execution, cleanup, commit or PR is authorized by this text.

## Context

Design revision 2 was rejected on transport containment, capture bound, ownership and IN_FLIGHT state, staged-script provenance and nested quoting. Revision 1 of this amendment overclaimed on the first two: it asserted an exhaustive list of OpenSSH client children (omitting at least `ssh-sk-helper`) and treated remote `head -c` as a physical bound although login-shell output, interpreter and supervisor startup stderr, and client diagnostics bypass it. Both claims are withdrawn below.

## Hermes-first analysis

| Domain | Evidence | Date limit and verdict |
|---|---|---|
| Journal reduction, host metadata validation | Skills hub `hermes-agent.nousresearch.com/docs/skills` | Checked 2026-09-14, 2026-09-16 19:30 UTC and fresh coordinator check 2026-09-16 23:43 UTC: catalog still Loading, no matching skill verified. Not an exhaustive absence claim |
| Ecosystem | `github.com/0xNyk/awesome-hermes-agent` | Same dates, latest 23:43 UTC: general orchestration listings, not a replacement |
| Deployed VPS Hermes surface | `hermes-gateway` active (23:42:36 UTC). Installed skills and plugins not inventoried for SSH transport or journal reduction | Open per `tasks/lessons.md` "Hermes-first review scope"; not required to approve this amendment |
| Hermes cron `no_agent` shell mode as on-host executor | Exists on the host (lessons 2026-05-20) | Rejected: requires a host write to `~/.hermes/cron/jobs.json` and creates a persistent coordinator |
| Remote execution and staging | Stock OpenSSH client, coreutils `head`, `sha256sum`, `mktemp`, `stat`, `pgrep` | Reuse. `scp` dropped in favour of `ssh` with `head -c` on stdin; both stock |
| Local process control | Python stdlib only | Reuse; no custom transport expansion |

Verdict: no Hermes replacement verified for any domain; the amendment adds no dependency and removes one tool.

## Current runtime note (dated)

Fresh read-only production read at 2026-09-16 23:42:36 UTC: deployed HEAD 77751890c9f1f51ed348c365d4e7a5985ea2827d; full `git status --porcelain` still 11 entries; pipeline, dashboard and hermes-gateway active; worker ExecStartPre auth-guard last attempt 19:22:35 UTC exit 21, timer active. Nothing was changed. Op 2's zero-porcelain gate therefore still FAILS.

## A. Transport: ssh only; containment is an unresolved prerequisite

1. Every operation is one fresh `ssh` process; `scp` is removed. Ops 5 to 7 become `ssh ... 'head -c 32768 > TMP/NAME'` with stdin opened read-only on the staged file; all other operations use `stdin=DEVNULL`. Raw bytes, no framing. A short, truncated or altered transfer fails op 8 `HASH_MISMATCH`.
2. Fixed client options, asserted by a static test on the rendered argv: `-F none -o BatchMode=yes -o ControlMaster=no -o ControlPath=none -o ProxyCommand=none -o ProxyJump=none -o PermitLocalCommand=no -o HostbasedAuthentication=no -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o LogLevel=ERROR`, optionally `-i IDENTITY -o IdentitiesOnly=yes`. These options **reduce** the client's local child surface. They are not claimed to eliminate it: `ssh-sk-helper` and any helper this list does not name may still be spawned. No exhaustive childlessness claim is made.
3. The driver's `kill()` of its direct child is therefore best effort, not containment proof. Revision 1's `/proc` group scan and `LOCAL_ORPHAN` verdict are withdrawn; they were containment machinery presented as none.
4. **PREREQ-1, local transport containment.** Production execution is blocked until a separately reviewed amendment establishes, for the local machine that runs the driver, that after a wall-clock or oversize kill no descendant of the transport process survives, verified by an oracle independent of the driver. Its scope must fit one page, prefer stdlib or OS-native primitives, declare its own primitives honestly, and may conclude "run the driver only from Linux under the existing group-ownership harness" if that is the narrowest proof. This amendment does not choose.
5. Connection budget unchanged: 18 sequence operations, one recovery, one removal, hard cap 20.

## B. Capture bound: sentinel rejection now; physical bound is an unresolved prerequisite

1. Remote `head -c N` under `set -o pipefail` is applied to every operation (control 4,097; staging 4,097; observation 131,073). It bounds the **intended payload only**. Login-shell output, interpreter and supervisor startup stderr, and client diagnostics bypass it. The aggregate physical bound on local storage per operation is **not established** by this amendment.
2. Sentinel rejection is mandatory and independent of rc: any `opNN.out` of size at least `N` (sentinel byte present) is rejected even at rc 0. Control operations: `FATAL REMOTE_OVERSIZE`. Observation operations: `UNPROVEN`. No reliance on `SIGPIPE`; a buffered write can complete before truncation is observed.
3. The design's size-polling kill remains defense in depth and may not appear in any proof sentence. `opNN.err` limits are acceptance limits, labelled as such in findings.
4. **PREREQ-2, physical local-storage bound.** Production execution is blocked until either design revision 3 establishes the plan's original requirement (a hard bound on bytes written to local storage per operation, on stdout and stderr, enforced before the write and not by observation after it) or a separately reviewed scope amendment explicitly weakens the contract. Silent weakening is not permitted.

## C. Ownership and IN_FLIGHT

1. Every subcommand creates `RUNDIR/busy` with `O_CREAT|O_EXCL` holding `{pid, started_at, run_id}` and removes it on exit. A pre-existing file is `FATAL BUSY_OR_CRASHED`; the driver never reclaims it.
2. Manual lock removal is permitted only after the operator verifies **both** that the recorded driver pid is not running and that no transport process it spawned is running. If either is uncertain, the lock is retained and findings record `OWNERSHIP_UNCERTAIN`.
3. Before `Popen` of any operation the driver persists `in_flight = {op, attempt, connection_index, started_at}` and increments `connections_used` (temp-file, `os.replace`, `fsync`). Flush and fsync the temporary file before replacement; persist the containing directory after replacement where supported. Unsupported durability is an explicit design prerequisite, not an assumed guarantee. Fault tests must cover each persistence boundary. The connection is consumed whether or not the spawn happens.
4. `in_flight` is cleared only in the single atomic state write that also records the `opNN.rc` reference, the verdict, `next`, and the ledger entry. A crash before that write leaves `in_flight` set; every later subcommand treats the operation as `AMBIGUOUS` (control: `FATAL`; observation: attempted with no envelope, `UNPROVEN`, `next` is `R`). No operation is ever re-run: replay of an in-flight or evaluated operation is `REPLAY_REJECTED`.

## D. Provenance bound to reviewed trees, byte-exact

1. Materialization is byte-exact by construction: a Python `subprocess.run(["git", "show", "SHA:PATH"], stdout=<binary file handle>, timeout=...)` with argv, no shell, no text mode. Shell pipes or redirection are not permitted unless a test first proves the shell binary-safe by round-tripping a fixture containing 0x00, 0x0A, 0x0D and 0xFF. Digests are computed with `hashlib` over the exact materialized bytes.
2. Source pins (five files) are materialized at `expected_head` (77751890…), which op 9 verifies against the host. Script pins (three files) and the staged bytes are materialized at `MERGED_SHA`, the approved PR592 master tree; never from a working tree.
3. `MERGED_SHA` must be verified as an ancestor of freshly fetched `origin/master` and its three script blob ids must equal those of the reviewed merge commit 5281f047346a951acb05926a27fe0489bc3cafd4. Both checks are recorded in findings with their outputs. Whether the driver's `init` performs them or a documented step precedes it is a design decision.

## E. Quoting bound

1. Exactly two shell parse levels per remote string: the login shell (bash, proven at op 1) and one inner `bash --noprofile --norc -o pipefail -c`. The level-1 payload is single-quoted; the grammar forbids `'` in every parameter. Revision 2's double-quoted `C(body)` is rejected because level 1 expands `$(...)` and `$d` before the inner bash runs.
2. Level-2 content contains no quote of either kind. Control template: `set -o pipefail; timeout -k 2 10 bash --noprofile --norc -o pipefail -c 'BODY' 2>&1 | head -c 4097`. Observation template: `set -o pipefail; python3 -I -S TMP/receipt_inventory_supervisor.py --pgid-file TMP/wrapper.pgid.N -- timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c 'PAYLOAD' | head -c 131073`. Exact bodies remain a design obligation.
3. Tests: `shlex.split(remote)` equals the intended argv; the quoted argument equals the payload byte for byte; single quotes occur only as the two delimiters; the Linux synthetic executor runs the identical string under `bash -c REMOTE`.

## F. Zero-porcelain gate retained

Op 2 keeps full `git status --porcelain | wc -l` with "0 or fatal". Last two observations (20:39:46 and 23:42:36 UTC, 2026-09-16) both show 11 entries. This amendment authorizes no cleanup, no `-uno`, no ignore rule, no tracked-only bypass and no definition of acceptable runtime artifacts. Production execution stays blocked on this gate independently of PREREQ-1 and PREREQ-2.

## G. Superseded plan sentences

| Location | Change |
|---|---|
| Operation model, bullet 1 | "`ssh` or `scp`" becomes "`ssh`" with section A options, stated as reduction not proof |
| Operation model, bullet 3 | `scp` row becomes "staging 30 s, stdin one local file, remote `head -c 32768`"; limits labelled payload-bound or acceptance; sentinel rejection at any rc |
| Exact sequence, ops 5 to 7 | Tool column `ssh` |
| Op 8 gate | "recorded from merged PR592 master" becomes section D procedure |
| Final cleanup-safety fold, sentence 1 | "each observation ATTEMPTED" becomes "every operation IN_FLIGHT before spawn, cleared only atomically with its verdict" |
| Gates | Add PREREQ-1 and PREREQ-2 as production blockers; build only after two design approvals |
| Obligations for the design | Add sections A to E as stated |

Unchanged: cleanup proof, recovery, interpretation contract, stops, wall clocks, licensed write phase, and the accepted design-level items (test-owned process identities, deterministic publication failure, saturation means completeness unknown).

## Gates and next steps

1. Two independent plan reviews of this revision. Approval means the prerequisites are correctly scoped, not resolved.
2. Design revision 3 folds this amendment; two independent design reviews. **No build and no synthetic exercise before both design approvals.**
3. Build under TDD, two PR reviews, exact-head Linux CI green including the synthetic exercise.
4. Production execution only when all of: PREREQ-1 approved and satisfied, PREREQ-2 approved and satisfied, zero-porcelain gate passes under operator disposition, recorded approval.

## Verification of this amendment

Docs-only: `git diff --check`; reviewers confirm each section G row maps to a live sentence in the approved plan and that no sentence claims containment or a physical storage bound as complete.
