# Plan: Linux-only synthetic containment and caps prerequisite (PREREQ-1 / PREREQ-2), 2026-09-17

**New primitives introduced:**
- A PID-namespace launch chain built from stock util-linux tools: `prlimit … -- unshare --user --map-current-user --pid --fork --kill-child=SIGKILL -- PAYLOAD`.
- A `/proc` namespace-inode emptiness oracle.
- A fail-closed environment probe.
- Two disposable, non-production artifacts: `scripts/synthetic_containment_probe.py` and `tests/test_synthetic_containment_probe.py`.
- No dependency, daemon, config, schema, secret, workflow or host change.
- No cgroup machinery and no sandbox platform.

## Hermes-first analysis

| Domain | Evidence | Verdict |
|---|---|---|
| Hermes skills hub `hermes-agent.nousresearch.com/docs/skills` | Fresh check on 2026-09-17: the catalog was still "Loading". | Absence is **unproven**. No skill verified. |
| `github.com/0xNyk/awesome-hermes-agent` | Directory checked on 2026-09-17. | No exact containment or file-cap contract verified. |
| Repo drift | Verified this session: `rg -i 'cgroup\|RLIMIT_\|unshare\|setrlimit\|prlimit\|systemd-run' scripts/` returns nothing. The only `setsid` hit is a comment at `scripts/receipt_inventory_supervisor.py:462`, which is session-group logic and already rejected as containment. | No existing primitive to reuse. |
| OS facilities | The kernel PID namespace and `RLIMIT_FSIZE`, plus util-linux `unshare` and `prlimit`. | Reuse these. Custom code is glue only. |

**Status: DRAFT - two independent plan reviews pending; no design/build approval.**

## Context
The findings of 2026-09-16 (NO BUILD) left two gaps:
- **PREREQ-1:** no descendant survives, including one that calls `setsid`.
- **PREREQ-2:** a kernel-enforced, before-the-write bound on both redirected files, with no pipe in the path.

This plan covers one synthetic, Linux-only, non-production slice. It does **not** clear rejected design PR593, satisfy either prerequisite for production, or authorize collection.

## Mechanism selection
- **cgroup v2 `cgroup.kill` plus recursive `cgroup.events populated=0`: not selected.**
  - The oracle is clean, recursive and kernel-maintained.
  - It needs a writable delegated subtree, which means root or a systemd user manager. That is an environmental dependency.
  - Kernel delegation containment only blocks moves *across* the delegation boundary. A same-uid payload can write a sibling `cgroup.procs` inside it, so "no migration out" fails without a second uid (privilege) or a delegation-wide kill, which would sweep unrelated processes.
  - If cgroups are ever revisited, recursive populated-emptiness stays mandatory.
- **PID namespace: selected.**
  - Membership is immutable, and orphans reparent to the namespace init.
  - Death of the init SIGKILLs every member, including nested namespaces (pid_namespaces(7)).
  - `setsid`, a double fork or a nested `unshare` cannot leave the namespace.

**Caps.**
- `prlimit` sets soft and hard limits equal for `--fsize=N`, and sets finite `--cpu`, `--as`, `--nofile` and `--core=0`. The exact values are a design obligation.
- `--nproc` semantics under a user namespace are a design-review item, not a proof term.
- Per getrlimit(2), exceeding `RLIMIT_FSIZE` yields `SIGXFSZ`/`EFBIG` at the write. Raising a hard limit needs `CAP_SYS_RESOURCE` **in the initial user namespace**, which the payload never has. `--map-current-user` runs the payload without root inside the namespace.

**Output.**
- The driver opens `op.out` and `op.err` as separate files with `O_CREAT|O_EXCL|O_APPEND`.
- It passes them as fds with `stdin=DEVNULL`.
- A static test asserts there is no `PIPE` and no `capture_output`.
- The files are read only in a later tool call, after the oracle result is recorded.

**Oracle.**
- The oracle runs as a separate later invocation, not the driver.
- It passes only if no `/proc/[pid]/ns/pid` link equals the recorded namespace inode.
- Any unreadable entry gives `UNPROVEN`, never a pass.

**Refused claims.**
- OpenSSH compatibility under a user namespace.
- An aggregate filesystem bound beyond the two redirected files. A payload may create other files, each capped at N. A size-bounded filesystem needs mount privilege and is out of scope.
- Any proof from wall-clock or size polling.
- Production readiness.

## Falsifiers
Any one of these means the slice fails:
1. A descendant survives teardown. Test payloads use `setsid`, a double fork, a nested `unshare --pid`, and a fork during the kill.
2. A payload raises a hard limit.
3. Either file exceeds N bytes.
4. The cap is observed only after the write.
5. A pipe appears in the output path.
6. The proof rests on a skipped test. A skip counts as `UNPROVEN`.
7. Passing requires sudo, a sysctl change or a workflow edit.

## Gates and deliverables
**Gates.**
- Two separate plan reviews, then two separate design reviews, before any build or synthetic exercise.
- TDD with tests.
- Two PR reviews.
- Exact-head CI green on the full `test` job and `reviewer-clearances`, with `strict` on, before merge.

**Out of scope this slice.** Production, SSH or network use, installation, workflow edits, and account, DB or config changes.

**Unchanged gate.** The zero-porcelain gate stands: 11 untracked entries on production at 12:53 UTC 2026-09-17. No cleanup or bypass is authorized.

**Deliverables.**
- D1: this plan, as `tasks/plan_containment_caps_prerequisite_2026_09_17.md`.
- D2: a design doc.
- D3: the probe and tests (the two declared artifacts).
- D4: a findings doc.

## Execution gate and no-build fallback
- No verified non-production Linux driver exists. The workstation is win32, and the VPS and production are never test hosts.
- The probe fails closed with one of these codes: `NOT_LINUX`, `USERNS_DENIED`, `UNSHARE_UNSUPPORTED`, `PRLIMIT_MISSING`, `PROC_UNREADABLE`.
- Known risk, recalled from memory and unverified here: Ubuntu 24.04 images may restrict unprivileged user namespaces through AppArmor. If the existing CI job denies them, enabling them is an environmental or workflow change outside this slice.
- In that case stop with **NO BUILD: PRIMITIVE_UNTESTABLE(<code>)**. Record the probe output in D4. PREREQ-1 and PREREQ-2 stay open, and no fallback mechanism is substituted silently.

## References
- Kernel cgroup v2 (`cgroup.kill`, `cgroup.events`, delegation containment): `docs.kernel.org/admin-guide/cgroup-v2.html`
- getrlimit(2): `man7.org/linux/man-pages/man2/getrlimit.2.html`
- pid_namespaces(7)
