**New primitives introduced:** None beyond the b1dd1b44 draft, and nothing is built now.
- A PID-namespace launch chain from stock util-linux `unshare` and `prlimit`. The exact ordering is a design obligation; see Caps.
- A `/proc` namespace oracle, now structural rather than inode equality.
- A fail-closed environment check, now read-only with no launches.
- Two disposable non-production artifacts, both deferred: `scripts/synthetic_containment_probe.py` and `tests/test_synthetic_containment_probe.py`.
- No dependency, daemon, config, schema, secret, workflow, host or cgroup change, and no sandbox platform.

## Hermes-first analysis
| Domain | Evidence | Verdict |
|---|---|---|
| [Hermes skills hub](https://hermes-agent.nousresearch.com/docs/skills) | Recorded 2026-09-17: catalog still "Loading". Not re-checked in this fold. | Absence is **unproven**. No skill verified. |
| [awesome-hermes-agent](https://github.com/0xNyk/awesome-hermes-agent) | Directory checked 2026-09-17. | No exact containment or file-cap contract verified. |
| Repo drift | Recorded at b1dd1b44: `rg -i 'cgroup\|RLIMIT_\|unshare\|setrlimit\|prlimit\|systemd-run' scripts/` returned nothing. The `setsid` hit at `scripts/receipt_inventory_supervisor.py:462` is a comment about session-group logic, which was already rejected as containment. | No existing primitive to reuse. |
| OS facilities | The kernel PID namespace, rlimits, and util-linux `unshare` and `prlimit`. | Reuse these. Custom code is glue only. |

# Plan: Linux-only synthetic containment and caps prerequisite, scoped and contingent, 2026-09-17
**Status: two independent APPROVE PLAN verdicts at db8d6595 after folds of b1dd1b44 findings.** Approval covers this as a *scoped contingent plan* only. It does not approve a design, a build, or any exercise.

**Current condition: NO BUILD.** PREREQ-1 and PREREQ-2 remain **open and unmet**, and nothing here claims otherwise. This plan covers only this slice and makes no statement about other backlog items.

## Context
The 2026-09-16 findings (NO BUILD) left two gaps:
- **PREREQ-1:** no descendant survives, including one that calls `setsid`.
- **PREREQ-2:** a kernel-enforced bound on the redirected output files that applies before the write, with no pipe in the path.

This plan describes one synthetic, Linux-only, non-production slice. It does not clear rejected design PR593 and does not authorize collection.

## Mechanism selection
- **cgroup v2 `cgroup.kill`: not selected.**
  - It needs a delegated writable subtree, which means root or a systemd user manager.
  - A same-uid payload can migrate between sibling cgroups inside the delegation.
  - If cgroups are ever revisited, recursive `populated=0` emptiness stays mandatory.
- **PID namespace: selected.**
  - Membership is immutable, and orphans reparent to the namespace init.
  - Init death SIGKILLs every member, including those in nested namespaces ([pid_namespaces(7)](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)).
  - SIGKILL *delivery* is not proof of emptiness. The oracle section covers the proof.

## Privilege preconditions
This section corrects the draft.
- `--map-current-user` is an ID mapping, **not a privilege drop**. A root caller stays root-equivalent on the host for file access.
- The driver must positively verify two things before anything else runs:
  - A **non-root uid in the initial user namespace**.
  - A capability preflight read from `/proc/self/status`: empty `CapEff`, `CapPrm` and `CapAmb`, and in particular no `CAP_SYS_RESOURCE` ([user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)).
- If either check fails or cannot be read, the result is `UNPROVEN` and no launch happens.
- The capabilities the payload holds *inside* the new user namespace do not allow it to raise a hard rlimit ([getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)).

## Caps
This section addresses the ops finding.
- **Launcher-startup window.**
  - `prlimit` and `unshare` start with `op.out` and `op.err` already open, before any limit applies.
  - A launcher startup or failure message is therefore uncapped output.
- **Requirement.**
  - Finite hard limits for `FSIZE`, `CPU`, `AS`, `NOFILE` and `CORE=0` must already be **in force and inherited** by the first launcher exec.
  - That covers launcher startup and every launcher failure path.
  - `prlimit` then only re-asserts or lowers the limits ([prlimit(1)](https://man7.org/linux/man-pages/man1/prlimit.1.html)).
- **Inheritance mechanism.**
  - How the limits are inherited is a design decision, and it may use only the proposed tools plus driver glue.
  - If the design cannot show inheritance, all cap evidence must be labelled **PARTIAL: payload-only, launcher window uncovered**.
- **Scope of claims.**
  - `RLIMIT_FSIZE` is per file and per process: two files are capped at N each, **not an aggregate**.
  - `RLIMIT_CPU` and `RLIMIT_AS` are per process, **not aggregate** across forks.
  - `--nproc` under a user namespace stays a design-review item, not a proof term.
- **Fault payloads.**
  - Every fault payload must be **finite**: bounded bytes, bounded forks, bounded runtime, and self-terminating.
  - No unbounded loop may rely on a cap to stop it.

## Output
- `op.out` and `op.err` are separate files opened with `O_CREAT|O_EXCL|O_APPEND` and passed as fds, with `stdin=DEVNULL`.
- A static test asserts there is no `PIPE` and no `capture_output`.
- The files are read only in a later invocation, after the oracle result is recorded.

## Oracle
This section addresses the structural finding. **A bare inode-equality check never yields PASS.**
1. **Registration before release.**
   - The namespace init blocks on a release handshake.
   - The driver, running in the initial PID namespace, reads `/proc/<init>/ns/pid`.
   - It verifies that the link differs from its own and positively binds the init identity (pid/start time) to the owned launch chain; the util-linux launcher and namespace init are distinct identities.
   - It durably records this registration, and **only then** releases the payload.
   - Any missing, partial, stale or in-flight registration gives `UNPROVEN`. So does any launch that has not reached quiescence, meaning the driver has reaped the launcher and recorded its terminal status.
2. **Complete host visibility, positively shown.**
   - The oracle runs in the initial PID namespace.
   - `/proc` is the host procfs with no `hidepid` or `subset` restriction, confirmed from `/proc/self/mountinfo`.
   - Entries hidden by `hidepid` are *absent without raising an error* ([proc(5)](https://man7.org/linux/man-pages/man5/proc.5.html)).
   - "No error" is therefore never evidence. Unverified visibility gives `UNPROVEN`.
3. **Nested namespaces.** The design must choose one of these:
   - **(a) Descendant-ancestry walk.** For every visible pid, the walk uses `NS_GET_PARENT` ([ioctl_nsfs(2)](https://man7.org/linux/man-pages/man2/ioctl_nsfs.2.html), [namespaces(7)](https://man7.org/linux/man-pages/man7/namespaces.7.html)). PASS only if no process is in the registered namespace **or any descendant of it**.
   - **(b) A justified init-death and reaping proof.** It must cite kernel-documented semantics showing that the launcher's successful reap of the init implies every member, nested ones included, has exited. If the justification is not accepted, (a) is mandatory.
4. Any unreadable entry, pid-reuse ambiguity or scan race gives `UNPROVEN`. The oracle runs as a separate, later invocation from the driver.

## Refused claims
- OpenSSH compatibility under a user namespace.
- An aggregate filesystem, CPU or memory bound.
- Any proof from wall-clock or size polling.
- Production readiness.
- PREREQ-1 or PREREQ-2 being met.

## Falsifiers
Any one of these means the slice fails:
1. A descendant survives. The test payloads are `setsid`, a double fork, a nested `unshare --pid`, and a fork during the kill. All are finite.
2. PASS is issued on inode equality alone.
3. The payload is released before registration.
4. An incomplete or in-flight launch is scored as PASS.
5. Hidden `/proc` entries are tolerated.
6. The payload raises a hard limit.
7. Either file exceeds N bytes.
8. The cap is observed only after the write.
9. Launcher-window output is uncapped while the evidence is labelled full.
10. A pipe appears in the output path.
11. The proof rests on a skipped test. A skip counts as `UNPROVEN`.
12. Passing requires sudo, a sysctl change, an installation or a workflow edit.
13. The driver runs as root, or no capability preflight was done.

## Environment gate
The build-probe-to-NO_BUILD loop in the draft was circular and is removed.
- **Fact.** There is no verified non-production Linux environment.
  - The workstation is win32.
  - The Docker engine is currently missing; only the internal `docker-desktop` WSL distro exists, and it is **not** a test environment.
  - The VPS and production are never test hosts.
- This is an **availability** condition. It is not a new permission, and local planning and review work need none.
- **E0: environment verification.** This comes first and sits under its own separately reviewed design. It applies only to a *supplied* non-production Linux environment.
  - It uses bounded, stock, **read-only** capability checks.
  - It makes **no synthetic launches and no `unshare` or `prlimit` execution** at preflight.
  - Candidate reads: `uname`, `id -u`, `/proc/self/status` Cap* lines, `/proc/self/mountinfo`, the `user.max_user_namespaces`, `kernel.unprivileged_userns_clone` and `kernel.apparmor_restrict_unprivileged_userns` sysctl files where present, and the presence and version of `unshare` and `prlimit` ([unshare(1)](https://man7.org/linux/man-pages/man1/unshare.1.html)).
  - A successful read-only precheck is suitability evidence only, not proof that namespace creation or cleanup works; those remain synthetic design/test obligations.
  - Result classes: `NOT_LINUX`, `ROOT_OR_CAPS_PRESENT`, `USERNS_RESTRICTED`, `TOOL_MISSING`, `PROC_RESTRICTED`, `PRECHECK_OK`.
  - Known risk, unverified here: Ubuntu 23.10 and later restrict unprivileged user namespaces through AppArmor ([Ubuntu blog](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces)). Enabling them would be a host change and is out of scope.
- **If no environment is supplied, or E0 does not return `PRECHECK_OK`: NO BUILD now.**
  - Exact next gate: an operator-identified non-production Linux environment, plus an approved E0 read-only preflight design, plus an `PRECHECK_OK` record.
  - No fallback mechanism is substituted silently.

## Gates and deliverables
- **Gates, in order:**
  1. Two plan re-reviews. This is the current step.
  2. E0 design, with two reviews.
  3. The E0 result.
  4. Two design reviews of D2.
  5. TDD build.
  6. Two PR reviews.
  7. Exact-head CI green on the full `test` job and `reviewer-clearances`, with `strict` on, before merge.
- **Out of scope for the synthetic slice:** production, SSH or network use, environment installation, workflow edits, and account, DB or config changes.
- **Unchanged gate:** the zero-porcelain gate stands, with 11 untracked entries on production at 12:53 UTC 2026-09-17. No cleanup or bypass is authorized.
- **Deliverable now:** D1, this plan (`tasks/plan_containment_caps_prerequisite_2026_09_17.md`).
- **Deferred until the gates clear:**
  - D1a: the E0 preflight design (safe to draft before an environment is supplied; execution waits for a target).
  - D2: the design doc, covering cap values, inheritance ordering, the handshake and oracle option (a) or (b).
  - D3: the probe and tests.
  - D4: synthetic-test findings. Current no-build findings are recorded separately now.

## References
- [Kernel cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [pid_namespaces(7)](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)
- [user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)
- [namespaces(7)](https://man7.org/linux/man-pages/man7/namespaces.7.html)
- [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)
- [prlimit(1)](https://man7.org/linux/man-pages/man1/prlimit.1.html)
- [unshare(1)](https://man7.org/linux/man-pages/man1/unshare.1.html)
- [proc(5)](https://man7.org/linux/man-pages/man5/proc.5.html)
