# Plan amendment 1: production read-only receipt inventory integration

Amends `tasks/plan_receipt_inventory_integration_2026_09_16.md` (approved at 035cd914). Design PR593 at b3fe776a (revision 15216a1f) stays REJECTED and is not amended here. This amendment needs two independent plan reviews before design revision 3. No build, staging, runtime execution, cleanup, commit or PR is authorized by this text.

**New primitives introduced:** NONE beyond the two disposable artifacts the approved plan already names (`scripts/receipt_inventory_sequence.py`, `tests/test_receipt_inventory_sequence.py`). This amendment removes one tool (`scp`) and binds the driver to stdlib mechanisms already used in `scripts/receipt_inventory_supervisor.py`: `start_new_session`, temp-file plus `os.replace`, `os.open(O_CREAT|O_EXCL)`, and the `/proc` stat scan (`read_stat`). No dependency, schema, secret, config, host write outside the licensed temp directory, or persistent coordinator.

## Context

Both design reviews rejected revision 2 on five points: local transport containment (killing only the direct `ssh`/`scp` child does not prove descendant cleanup), the capture bound (file-size polling is not a physical bound), ownership and pre-spawn IN_FLIGHT state for every operation, staged-script provenance (digests computed from working-tree files, not the approved revision), and nested shell quoting (double-quoted `bash -c` bodies containing `$(...)` and `$d` are expanded by the login shell before the inner bash runs). The closeout at 20:44 UTC added that production preflight currently fails the zero-porcelain gate.

## Does the approved plan suffice?

| Finding | Approved plan today | Resolution level |
|---|---|---|
| Transport containment | Names `scp`; silent on local descendants | **Amend**: ssh-only, fixed option set (section A) |
| Capture bound | States byte limits without saying where they are physically enforced | **Amend** wording; mechanism is existing remote `head -c` (section B) |
| Ownership, IN_FLIGHT | ATTEMPTED record for observations only | **Amend**: every operation, plus exclusive run lock (section C) |
| Script provenance | "values recorded from merged PR592 master" (correct intent) | **Clarify** the procedure so design cannot deviate (section D) |
| Nested quoting | Not addressed | **Bound** at plan level; exact strings stay a design obligation (section E) |
| Zero porcelain | Op 2 gate exists | **Retain** unchanged (section F) |

## Hermes-first analysis

| Domain | Evidence | Date limit and verdict |
|---|---|---|
| Journal reduction, host metadata validation | Skills hub `hermes-agent.nousresearch.com/docs/skills`: catalog stayed Loading, nothing verified | Checked 2026-09-14 (`plan_suppression_receipt_inventory_2026_09_14.md`) and 2026-09-16 19:30 UTC (plan revision 3). No fresh check for this amendment; not an exhaustive absence claim |
| Ecosystem | `github.com/0xNyk/awesome-hermes-agent`: general orchestration listings | Same two dates. Not a replacement |
| Deployed VPS Hermes surface | `hermes-gateway` active at 20:39:46 UTC 2026-09-16 (closeout). Installed skills and plugins were NOT inventoried for SSH transport or journal reduction | Open per `tasks/lessons.md` "Hermes-first review scope"; a names-only listing may ride a future routine read-only runtime read. Not required to approve this amendment |
| Hermes cron `no_agent` shell mode as on-host executor | Exists on the host (lessons 2026-05-20) | Rejected: needs a write to `~/.hermes/cron/jobs.json`, which the no-host-write licence forbids, and creates a persistent coordinator |
| Remote execution and staging | Stock OpenSSH client, coreutils `head`, `sha256sum`, `mktemp`, `stat`, `pgrep` | Reuse. `scp` dropped in favour of `ssh` with `head -c` on stdin; both stock |
| Local process control | Python stdlib; patterns already in the merged supervisor | Reuse unchanged |

Verdict: no Hermes replacement is verified for any domain; the amendment adds no dependency and removes one tool.

## A. Transport containment: ssh only, no local descendants by construction

1. Every operation is one fresh `ssh` process. `scp` is removed. Ops 5 to 7 become `ssh ... 'head -c 32768 > TMP/NAME'` with the driver's stdin for that process opened read-only on the staged file; every other operation uses `stdin=DEVNULL`. This is raw bytes, not a framing protocol: the plan's "no custom stdin framing or acknowledgement" clause is unchanged. A short, truncated or altered transfer fails op 8 `HASH_MISMATCH`, so no new gate is needed.
2. Fixed client options, asserted by a static test on the rendered argv: `-F none -o BatchMode=yes -o ControlMaster=no -o ControlPath=none -o ProxyCommand=none -o ProxyJump=none -o PermitLocalCommand=no -o HostbasedAuthentication=no -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o LogLevel=ERROR`, optionally `-i IDENTITY -o IdentitiesOnly=yes` where `IDENTITY` obeys the local-path grammar. `-F none` disables config-driven `ProxyCommand`, `LocalCommand` and `Match exec`; `BatchMode` disables askpass; `HostbasedAuthentication=no` disables `ssh-keysign`. These are the only ways the OpenSSH client spawns a local child, so the driver's child has no descendants and `kill()` of `child.pid` is complete containment.
3. On POSIX the child is spawned with `start_new_session=True` (lemma L7: pid equals pgid), and after any kill the driver records `local_group_empty` by scanning `/proc` with the supervisor's `read_stat` pattern, bounded by `SURVIVOR_SCAN_CAP`. `false` is `FATAL LOCAL_ORPHAN`. On Windows the field is `null` and the claim rests on item 2 alone. Residual stated: no live `sshd` exists in CI, so the real client is verified by configuration and static assertion, not by a process-tree probe.
4. Connection budget unchanged: 18 sequence operations, one recovery, one post-recovery removal, hard cap 20.

## B. Actual capture bound: physical on the host, acceptance locally

1. The physical bound is remote `head -c N` under `set -o pipefail`, applied to every operation including observations: control `N = 4097`, staging output `N = 4097`, observation `N = 131073`. Bytes past `N` never leave the host. The supervisor writes exactly one line of at most 131,072 bytes and never writes stderr, so `head` never truncates a valid envelope; if it does, the producer dies of `SIGPIPE` (rc 141) or the supervisor's `bounded_write` fails (rc 11), both already unusable.
2. Locally, `opNN.out` is read with `read(N + 1)`; a file larger than `N` is `FATAL TRANSPORT_ANOMALY` because it contradicts item 1. The design's size-polling kill stays as defense in depth only and may not appear in any proof sentence.
3. `opNN.err` holds only the local client's diagnostics under `LogLevel=ERROR` (remote stderr is folded into `head` for control ops and is `DEVNULL` under the supervisor). Its 65,536-byte figure is an acceptance limit, stated as such in findings, not a physical bound.

## C. Ownership and pre-spawn IN_FLIGHT for every operation

1. Exclusive run ownership: every subcommand creates `RUNDIR/busy` with `O_CREAT|O_EXCL` on entry and removes it on exit. A pre-existing file is `FATAL BUSY_OR_CRASHED`; the driver never reclaims it. Operator disposition is to inspect `state.json` and delete the file by hand, which is recorded in findings.
2. Before `Popen` of any operation (not only observations) the driver writes `in_flight = {op, attempt, connection_index, started_at}` into `state.json` by temp-file, `os.replace` and `fsync`, and increments `connections_used`. `in_flight` is cleared only after `opNN.rc` is persisted. The counted connection is consumed whether or not the spawn happened.
3. Any subcommand that starts with `in_flight` non-null treats that operation as `AMBIGUOUS`: control operations are `FATAL`; observation operations become an attempted observation with no envelope, so cleanup is `UNPROVEN` and `next` is `R`. Recovery quantifies over every IN_FLIGHT record, superseding the plan's "ATTEMPTED locally before starting its SSH invocation" wording, which covered observations only.

## D. Script provenance bound to the approved revision

1. `MERGED_SHA` is the origin/master commit whose tree the run stages. The three script pins are produced by the same documented local procedure as the five source pins: `git show MERGED_SHA:scripts/<name> | sha256sum`. The staged files themselves are materialized by `git show MERGED_SHA:scripts/<name> > RUNDIR/staged/<name>`, never copied from a working tree.
2. `init` verifies each staged file's sha256 equals its pin and records the pins-file digest; op 8 and op 12 compare against the same pins. The driver runs no git command; the operator records `git merge-base --is-ancestor MERGED_SHA origin/master` in the findings, and a reviewer can re-derive all three digests from `MERGED_SHA` alone.

## E. Quoting bound: two parse levels, single-quoted payload

1. Every remote string has exactly two shell parse levels: the login shell (proven bash at op 1) and one inner `bash --noprofile --norc -o pipefail -c`. The level-1 payload is single-quoted. The parameter grammar already forbids `'`, so substitution cannot close the quote. The rejected design's double-quoted `C(body)` was wrong because level 1 expanded `$(mktemp ...)` and `$d` before the inner bash ran; single quotes deliver `$(...)`, `$f`, `$g`, `$?` to level 2 intact, which is the intent.
2. Level-2 content contains no quote of either kind. Control template: `set -o pipefail; timeout -k 2 10 bash --noprofile --norc -o pipefail -c 'BODY' 2>&1 | head -c 4097`. Observation template: `set -o pipefail; python3 -I -S TMP/receipt_inventory_supervisor.py --pgid-file TMP/wrapper.pgid.N -- timeout -k 2 10 bash --noprofile --norc -o pipefail +m -c 'PAYLOAD' | head -c 131073`. Exact bodies and payloads remain a design obligation.
3. Test bound replacing the design's "no single quote anywhere": `shlex.split(remote)` equals the intended argv list, the single-quoted argument equals the template payload byte for byte, single quotes occur only as the two delimiters, and the Linux synthetic executor runs the identical string under `bash -c REMOTE`, the same parse `sshd` applies.

## F. Production zero-porcelain gate retained

Op 2 keeps `git status --porcelain | wc -l` over the full tree, and the gate stays "line count is 0, nonzero is fatal". Last observation: 11 untracked entries at 20:39:46 UTC 2026-09-16 on the repository-documented Gecko host at HEAD 77751890c9f1f51ed348c365d4e7a5985ea2827d. This amendment authorizes no cleanup, no `-uno`, no ignore rule, no tracked-only bypass and no definition of acceptable runtime artifacts. Design revision 3, the build and the synthetic exercise may proceed; the production sequence stays blocked until the operator disposes of the artifacts or a separate reviewed amendment defines acceptable artifacts.

## G. Superseded plan sentences

| Location in approved plan | Change |
|---|---|
| Operation model, bullet 1 | "one fresh `ssh` or `scp` invocation" becomes "one fresh `ssh` invocation" with the section A option set |
| Operation model, bullet 3 | `scp` row becomes "staging 30 s, stdin one local file, remote `head -c 32768`"; every byte limit is labelled physical (remote `head`) or acceptance (local read) |
| Exact sequence, ops 5 to 7 | Tool column `ssh` |
| Op 8 gate | "values recorded from merged PR592 master" becomes "pins derived from `MERGED_SHA` by `git show`" |
| Final cleanup-safety fold, sentence 1 | "records each observation as ATTEMPTED" becomes "records every operation IN_FLIGHT before spawn" |
| Obligations for the design | Add: section A argv, section B labels, section C state fields, section D pins procedure, section E templates and tests |

Unchanged: cleanup proof, recovery, interpretation contract, stops, per-operation wall clocks, licensed write phase, and the design-level items already accepted in revision 2 (test-owned process identities, deterministic publication failure, saturation means completeness unknown).

## Gates and next steps

1. Two independent plan reviews of this amendment (docs-only; no runtime).
2. Configured author folds the amendment into design revision 3; two independent design reviews.
3. Build under TDD, two PR reviews, exact-head Linux CI green including the synthetic exercise.
4. Production sequence only after the zero-porcelain gate passes under the operator's disposition, with recorded approval.

## Verification of this amendment

Docs-only: `git diff --check` on the plan file; reviewers confirm every superseded sentence in section G maps to a live sentence in the approved plan; no application tests are needed.
