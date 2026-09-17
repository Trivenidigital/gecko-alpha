**New primitives introduced:** NONE executable. This document adds the items below, and nothing is built or run now.
- A fixed checklist of stock, read-only commands (section 4). An operator runs them by hand in a session already local to the target. There is no script, wrapper, package, daemon or tool.
- An evidence-record template (section 7).
- Three fail-closed result classes beyond the plan's six: `TARGET_UNVERIFIED`, `NOT_INITIAL_NS` and `PRECHECK_UNKNOWN`. This extends the approved list and is disclosed for review. Each new class maps to NO BUILD, so the extension can only make E0 stricter.
- No dependency, config, schema, secret, workflow, host, sysctl, cgroup, account or DB change. No SSH or network mechanism.

## Hermes-first analysis
All rows are attributed to the approved plan at 26119c04. **Nothing was re-checked for this design, and no fresh network evidence is claimed.**

| Domain | Evidence (source) | Verdict |
|---|---|---|
| Hermes skills hub | Plan record 2026-09-17: catalog still "Loading". | Absence **unproven**. No skill verified. |
| awesome-hermes-agent | Plan record: directory checked 2026-09-17. | No containment or file-cap contract verified. |
| Repo drift | Plan record at b1dd1b44: `rg` for cgroup, rlimit, unshare and prlimit in `scripts/` returned nothing. | No existing primitive to reuse. |
| OS facilities | procfs, coreutils, and the distro package database. | Reuse these. E0 contains no custom code. |

# Design D1a: E0 read-only environment preflight, 2026-09-17
**Status: DRAFT. Not reviewed and not approved.**
- This design sits under `tasks/plan_containment_caps_prerequisite_2026_09_17.md`, Environment gate, item E0.
- **No target has been supplied, so nothing here is executed.**
- PREREQ-1 and PREREQ-2 remain open and unmet.
- The current condition remains NO BUILD.

## 1. Purpose and non-goals
- E0 answers one question. On an operator-identified non-production Linux environment, does any passively readable fact *rule out* the planned unprivileged `unshare`/`prlimit` slice, or leave it unknown?
- E0 can disqualify an environment. It cannot qualify a mechanism.
- E0 does not do any of the following, even in partial form:
  - Execute `unshare` or `prlimit`, including `--version` and `--help`.
  - Launch anything synthetic.
  - Use sudo.
  - Install or enable anything.
  - Change a sysctl, an AppArmor profile or a workflow.
  - Use or define SSH or any network transport.
  - Touch the pipeline, config, DB or accounts.
  - Run on the VPS, on production, or on the internal `docker-desktop` WSL distro.

## 2. Target provenance, required before any check runs
- The exact host is unspecified. Several facts cannot be verified from inside the host, so they are accepted only as **recorded operator statements**. If a statement is missing, the record fails closed.
- Required statements, quotable in the evidence record:
  1. A target label and how the session reaches it. The session must be local: a console, or an existing local terminal. E0 defines no transport.
  2. A statement that the target is **non-production** and is not the VPS or any production host. This cannot be verified by E0, and it is never inferred from a hostname.
  3. The expected hostname, which is compared to `/proc/sys/kernel/hostname`. This follows the lessons.md host-identity rule that output from an unconfirmed host is treated as not verified.
  4. A recorded go-ahead naming this target for E0 execution.
- If a statement is missing, the hostname mismatches, or the executor cannot confirm which host the session is on, the result is **`TARGET_UNVERIFIED`**. No further checks run in that case.

## 3. Execution rules
- The checks run once, in listed order, in one interactive POSIX shell. The shell runs as the unprivileged account intended for the later driver.
- A failed check is never retried. A rerun is a new record.
- Every external command has the form `timeout -k 2 5 <cmd>`. The package queries use 10 seconds instead of 5.
- If `timeout`, `head`, `readlink` or `stat` is absent, the result is `PRECHECK_UNKNOWN`. Nothing runs without a time bound.
- File reads use `head -c <cap> <file>` as the reader. This keeps output bounded and leaves the exit status unmasked by a pipe.
- If the output length equals the cap, the read is treated as truncated and scored **unknown**.
- Exit 124 or 137 (timeout), any other non-zero exit, empty output where content is required, and unparseable output are all scored **unknown** for that check. "No error" is never evidence on its own.
- Budget:
  - At most 20 commands.
  - At most 4 KiB per check, except 64 KiB for `mountinfo`.
  - At most 128 KiB in total.
  - At most 120 seconds of wall time.
  - If the budget is exceeded or a command hangs, the operator aborts and the result is `PRECHECK_UNKNOWN`.
- Nothing is written to the target. The terminal transcript is the raw evidence.
- `self` in `/proc/self/...` refers to the reader process (`head` or `readlink`). That process inherits the session's credentials and namespaces. E0 therefore describes **that session only**.

## 4. Checks

| # | Command (each wrapped per section 3) | Satisfied only if | Otherwise |
|---|---|---|---|
| C1 | `uname -srm` | The first token is `Linux`. | Any other value gives `NOT_LINUX`. An error gives unknown. |
| C2 | `head -c 256` of `/proc/sys/kernel/hostname`, of `/proc/sys/kernel/random/boot_id`, and `head -c 2048 /etc/os-release` | The hostname equals the statement from section 2. | A mismatch gives `TARGET_UNVERIFIED`. |
| C3 | `head -c 4096 /proc/self/status` | `Uid:` and `Gid:` each have four fields and all are non-zero. `CapPrm`, `CapEff` and `CapAmb` are all-zero hex. `Seccomp:` is `0`. | A zero uid or gid, or a non-zero cap, gives `ROOT_OR_CAPS_PRESENT`. A missing line or `Seccomp` ≠ 0 gives unknown. `CapInh`, `CapBnd` and `NoNewPrivs` are recorded and do not gate. |
| C4 | `readlink /proc/self/ns/user` and `readlink /proc/self/ns/pid` | The links read exactly `user:[4026531837]` and `pid:[4026531836]`. | Any other well-formed value gives `NOT_INITIAL_NS`. A malformed value or an error gives unknown. |
| C5 | `head -c 256 /proc/self/uid_map`, and the same for `gid_map` | Each file is a single line reading `0 0 4294967295`. | Any other mapping gives `NOT_INITIAL_NS`. An error gives unknown. |
| C6 | `head -c 65536 /proc/self/mountinfo` | See section 5. | `PROC_RESTRICTED` or unknown, per section 5. |
| C7 | `head -c 64` of `/proc/sys/user/max_user_namespaces` and of `/proc/sys/user/max_pid_namespaces` | Both are integers greater than 0. | A value of `0` gives `USERNS_RESTRICTED`. An absent or unreadable file gives unknown. |
| C8 | `head -c 64 /proc/sys/kernel/unprivileged_userns_clone`, **only if** `test -e` (a shell builtin) shows it exists | The file is absent, or its value is `1`. | A value of `0` gives `USERNS_RESTRICTED`. A file that is present but unreadable or holds another value gives unknown. |
| C9 | The same for `/proc/sys/kernel/apparmor_restrict_unprivileged_userns` | The file is absent, or its value is `0`. | A value of `1` gives `USERNS_RESTRICTED`. Otherwise the result is unknown. |
| C10 | `readlink -e /usr/bin/unshare`, the same for `/usr/bin/prlimit`, then `stat -c '%n %a %u %s'` on each resolved path | Both resolve. Each is a regular file with an execute bit, owned by uid 0. Neither is setuid or setgid. | A missing file gives `TOOL_MISSING`. A setuid or setgid bit, a non-root owner, or an error gives unknown. |
| C11 | On the dpkg family: `dpkg-query -S <resolved>`, then `dpkg-query -W -f='${Package} ${Version} ${Status}\n' <owner>`. On the rpm family: `rpm -qf --qf '%{NAME} %{VERSION}-%{RELEASE}\n' <resolved>`. | The owner is a util-linux package, the status is installed, and a version string is recorded. | A non-util-linux owner (for example busybox), a path the database does not own (including a usr-merge path mismatch), any other package manager, or an error gives **unknown**. The binaries are never executed to settle the question. |

Notes on the checks:
- **C4.** The two values are the fixed initial-namespace inode numbers in the kernel source (`include/linux/proc_ns.h`). They are not a documented ABI, so reviewers must confirm them.
- **C3.** `NSpid` is deliberately **not** used. A process that reads its own status through its own procfs sees a single value whether or not it is nested. Under the lessons.md rule on evidence that does not discriminate, that reading is not evidence.
- **C11 version floor.**
  - The minimum util-linux version is a D2 obligation. D2 must establish the floors for `--kill-child` and `--map-current-user` from the release notes.
  - Author recollection, unverified: `--kill-child` arrived in 2.32 and `--map-current-user` in 2.34.
  - E0 records the version. D2 evaluates the recorded string against its floor without a rerun.
  - A version below the D2 floor converts the record to `TOOL_MISSING`.

## 5. `/proc` visibility rule for C6
Parse the lines whose mount point is `/proc` or starts with `/proc/`.
- **Satisfied** only if all of these hold:
  - Exactly one line has mount point `/proc`.
  - That line has fstype `proc` and root field `/`.
  - Neither its per-mount options nor its super options contain a `subset=` token.
  - Neither contains a `hidepid=` token with a value other than `0` or `off`.
  - The only mount under `/proc/` is `/proc/sys/fs/binfmt_misc`, with fstype `autofs` or `binfmt_misc`.
- A `hidepid` or `subset` restriction gives `PROC_RESTRICTED`. So does a root field other than `/`, a non-proc fstype, or masking mounts under `/proc/`. Masking mounts are the usual container signature.
- Zero `/proc` lines gives unknown. So does more than one `/proc` line (an overmount ambiguity), truncation, or a line that cannot be parsed.

## 6. Decision contract
- All checks are evaluated.
- The result is **`PRECHECK_OK` if and only if every gating check is positively satisfied.**
- Otherwise the record reports the first class that applies in this order, and lists every check that was not satisfied:
  1. `TARGET_UNVERIFIED`
  2. `NOT_LINUX`
  3. `ROOT_OR_CAPS_PRESENT`
  4. `NOT_INITIAL_NS`
  5. `PROC_RESTRICTED`
  6. `USERNS_RESTRICTED`
  7. `TOOL_MISSING`
  8. `PRECHECK_UNKNOWN`
- Every class except `PRECHECK_OK` means **NO BUILD now**.
- No fallback mechanism is substituted silently, and nothing on the host is changed to remedy a finding.
- An unknown result is never upgraded by argument. The only upgrade path is a new record.

**What `PRECHECK_OK` means.**
- On the identified target, in the recorded boot and session, none of the enumerated passive disqualifiers was observed, and every gating read succeeded.
- It is **suitability evidence only**.
- It does **not** assert any of the following:
  - That `unshare` or `prlimit` run, or that their options exist.
  - That user-namespace or PID-namespace creation succeeds.
  - That teardown, reaping or the oracle works.
  - That the caps bind.
  - That the on-disk binaries match the package database.
  - That the target is non-production. That remains the operator's statement.
- All of those remain D2 and D3 synthetic obligations.
- The D3 driver must still run its own preflight at launch, as the plan's Privilege preconditions require.

**Limitations that PRECHECK_OK does not cover.**
- Blockers that these reads cannot see include SELinux or other LSM policy, per-profile AppArmor rules, seccomp applied later by a service manager, and per-user namespace accounting.
- An identity `uid_map` together with the initial-namespace inode constants is necessary for being in the initial namespaces. Neither is proven to be sufficient.
- `mountinfo` shows mount options. It does not show complete process visibility.
- **Forward flag to D2, not resolved here.** [namespaces(7)](https://man7.org/linux/man-pages/man7/namespaces.7.html) states that reading `/proc/<pid>/ns/*` links is subject to a ptrace read-access check. An unprivileged oracle may therefore be unable to read other users' entries. Under the plan, an unreadable entry means `UNPROVEN`. Oracle option (a) must address this in D2, and E0's OK result says nothing about it.

## 7. Evidence record fields
- The design SHA and the plan SHA.
- The quoted operator statements from section 2.
- The executor identity.
- UTC start and end times.
- The target label, hostname, boot_id, the os-release ID and VERSION_ID, and the `uname -srm` output.
- For each check, the exact command, the exit status, the byte count, the parsed value, and one of satisfied, a named class, or unknown.
- The resolved tool paths with their mode and owner, the owning package and its version, and the package-manager family.
- The final class, and the full list of checks that were not satisfied.
- A deviations list, which also covers any command run outside section 4. Any such command invalidates the record.
- Only the `/proc`-related `mountinfo` lines are recorded.
  - The full transcript stays with the operator and is not committed, because other mounts can reveal paths or hosts.
  - No environment variables, secrets or home-directory contents are read.
- The record is bound to the boot_id, the kernel release, the package version, the uid and the target label. A change in any of these makes it stale, and a stale record counts as no record.

## 8. Safety cases

| Case | Handling |
|---|---|
| No target supplied (the current state) | Nothing is executed and the condition stays NO BUILD. |
| The session is actually on the VPS or production, or host identity is ambiguous | The result is `TARGET_UNVERIFIED`, decided before C1, and nothing else runs. |
| A root shell or sudo is offered | The result is `ROOT_OR_CAPS_PRESENT`. E0 never uses sudo. |
| A container, a nested namespace, or a restricted procfs | The result is `NOT_INITIAL_NS` or `PROC_RESTRICTED`. This is the intended outcome and is not worked around. |
| Ubuntu 23.10 or later with the AppArmor restriction | The result is `USERNS_RESTRICTED`. Enabling user namespaces is a host change and is out of scope. |
| A hung read, or a process stuck in D state | The time bound fires, the operator aborts, and the result is `PRECHECK_UNKNOWN`. |
| Someone wants to "just try `unshare`" or check `--version` | This is forbidden. It is a plan violation, and the record is void. |
| A distro outside the dpkg and rpm families, or package-database gaps | The result is unknown. The design may be amended under review, but never ad hoc. |

## 9. Review checklist for the two independent reviewers
1. No path in the checklist executes `unshare` or `prlimit`, launches a process tree, writes to the target, uses the network, or needs privilege.
2. Every command has a time bound, an output cap and defined failure semantics, and truncation is scored as unknown.
3. Every check fails closed. No branch of any check reaches `PRECHECK_OK` from an absent, unreadable or ambiguous read. C8 and C9, where absence counts as satisfied, are the reviewed exceptions.
4. The wording of `PRECHECK_OK` cannot be read as proof of namespace creation, caps or containment.
5. The three added result classes are an acceptable extension of the plan, or a plan amendment is required first.
6. The C4 inode constants, the section 5 `hidepid` and `subset` tokens, and the C11 package commands are confirmed against primary sources.
7. Whether the provenance statements in section 2 are sufficient, given that non-production status cannot be verified from inside the host.
8. The design adds no custom executable, and none is implied for later "automation".
9. The markers at the top of this document are truthful.

## 10. Exact next gate
- Two independent terminal reviews of this design at a fixed SHA, with findings folded.
- **Then** E0 execution, which needs an operator-identified non-production Linux target together with the section 2 statements and a recorded go-ahead.
- **Then** an E0 record, with no deviations, whose final class is `PRECHECK_OK`.
- Only after that do the plan's D2 design reviews begin.
- Until then the condition is NO BUILD.
  - The zero-porcelain gate is unchanged.
  - The rejection of PR593 is unchanged.
  - The branch-protection gates are unchanged.
- This document claims no approval, no implementation and no execution.
