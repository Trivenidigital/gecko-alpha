**New primitives introduced:** NONE executable. Nothing is built or run now.
- A fixed manual checklist of stock read-only commands (section 4). There is no script, wrapper, framework, package or tool, and none is implied later.
- An evidence-record template (section 7).
- **Review item:** three fail-closed result classes beyond the plan's six: `TARGET_UNVERIFIED`, `NOT_INITIAL_NS` and `PRECHECK_UNKNOWN`. Each maps to NO BUILD. Reviewers decide whether this is an acceptable extension or needs a plan amendment.
- No application code, tests, dependency, config, schema, secret, workflow, host, sysctl, cgroup, account or DB change. No SSH or network mechanism.

## Hermes-first analysis
The coordinator reported these checks at 14:39 on 2026-09-17. They are attributed to the coordinator; this author did not re-run them.

| Domain | Evidence | Verdict |
|---|---|---|
| [Hermes skills hub](https://hermes-agent.nousresearch.com/docs/skills) | The catalog still showed "Loading". | Absence is **unproven**. No skill verified. |
| [awesome-hermes-agent](https://github.com/0xNyk/awesome-hermes-agent) | The directory was checked. | No exact E0 preflight contract verified. This is not proof that none exists. |
| Repo | `rg` over `scripts/` found no E0 preflight. | No existing primitive to reuse. |
| OS facilities | procfs, coreutils, and the distro package database. | Reuse these. E0 contains no custom code. |

# Design D1a: E0 read-only environment preflight, revision 2, 2026-09-17
**Status: DRAFT. This revision folds two REQUEST CHANGES verdicts at 628ada68. It is not approved.**
- This design sits under `tasks/plan_containment_caps_prerequisite_2026_09_17.md`, Environment gate, item E0.
- It can be approved only as a **target-contingent contract**. Such an approval is not execution proof and not build authorization.
- **No target has been supplied, so nothing is executed.**
- PREREQ-1 and PREREQ-2 remain open. The condition remains NO BUILD.
- Production facts are unchanged and are not relevant to E0.

## 1. Purpose and boundaries
- E0 collects passive facts that can *disqualify* an operator-identified non-production Linux environment for the planned unprivileged `unshare`/`prlimit` slice. It cannot qualify the mechanism.
- E0 never does any of the following:
  - Execute `unshare` or `prlimit`, including `--version` and `--help`.
  - Launch anything synthetic.
  - Use sudo.
  - Install or enable anything.
  - Change a sysctl, an AppArmor profile or a workflow.
  - Define or use SSH or any network transport.
  - Touch the pipeline, config, DB or accounts.
  - Intentionally change target contents/configuration or create output files. Query side effects such as access metadata, shell history, and package-library behavior are not controlled; no forensic zero-write guarantee is made.
  - Run on the VPS, on production, or on the internal `docker-desktop` WSL distro.

## 2. Target provenance (stage A, before any command)
- The existing standing read-only authority applies once the operator identifies a non-production target. **No fresh go-ahead for each target is required or invented here.**
- The operator's identification must record the following, quotable in the evidence record:
  1. A target label and how the local session is reached. The session must be a console or an existing local terminal. E0 defines no transport.
  2. That the target is **non-production** and is not the VPS or any production host. E0 cannot verify this, and it is never inferred from a hostname.
  3. The expected hostname, which is compared at C2. This follows the lessons.md host-identity rule.
  4. The environment kind.
     - The record must say whether the session is a direct login on the OS that owns the kernel (bare metal or a full VM), or something else (a container, a WSL distro, a chroot or a sandbox).
     - This statement is the **only** thing that can establish an initial-namespace session. No E0 read can prove it (see C4).
- A missing item 1, 2 or 3 gives **`TARGET_UNVERIFIED`**, and nothing runs.
- If item 4 is missing, or does not state a direct login on the kernel-owning OS, the final class cannot be better than `PRECHECK_UNKNOWN`.

## 3. Execution and observation limits
- The checks run in one interactive POSIX shell as the unprivileged account intended for the later driver. They run once, in listed order, and are never retried. A rerun is a new record.
- The operator first runs `export LC_ALL=C`, which is a shell builtin.
- Every command is typed exactly as `timeout -k 2 5 <cmd>; echo "rc=$?"`. The `echo` is a builtin.
- **A check with no `rc=` line in the transcript is unknown.**
- If `timeout`, `head`, `readlink` or `stat` is absent, the result is `PRECHECK_UNKNOWN`.
- **E0 has no hard bounds.**
  - `head -c` caps only that command's stdout.
  - Nothing caps stderr, or the output of `uname`, `readlink`, `stat` and the package queries. These outputs are merely expected to be small.
  - `timeout` is best effort. It cannot terminate a process in D state, and no wall-time guarantee is claimed.
  - Kernel-enforced caps belong to the synthetic D3 slice, where they are a proof obligation. E0 only observes.
- E0 therefore fails closed on what is observed. A check is **unknown** if any of these holds:
  - Its stdout length equals the `head -c` cap, which is treated as truncation. The evaluator measures the length offline from the saved transcript, and an unmeasurable length also counts as unknown.
  - An uncapped command prints more than 5 lines.
  - The command prints any stderr.
  - The return code is non-zero, including 124 and 137.
  - The output is empty or cannot be parsed.
- Observation stopping limits: 19 external commands at most (18 on rpm), 120 seconds for the session, 128 KiB visible transcript, and 5 lines per uncapped metadata command. These are abandonment thresholds, not hard ceilings. Any limit, truncation, or missing exit evidence stops further checks with `PRECHECK_UNKNOWN`; remaining checks are NOT_RUN. The timeout signals do not prove termination. No retry or new record is started while an earlier command remains unresolved; the operator takes no privileged cleanup action.
- `/proc/self` refers to the reader process, which inherits the session's credentials and namespaces. E0 describes **that session only**.

## 4. Checks: the exact commands
There are 19 commands on the dpkg family and 18 on the rpm family.

| # | Command | Satisfied only if | Otherwise |
|---|---|---|---|
| C1 | `uname -srm` | The first token is `Linux`. | Any other value gives `NOT_LINUX`, and the checklist **STOPS**. |
| C2 | `head -c 256 /proc/sys/kernel/hostname`<br>`head -c 256 /proc/sys/kernel/random/boot_id`<br>`head -c 2048 /etc/os-release` | The hostname equals item 3 of section 2. The boot_id and os-release are recorded. | A mismatch, or an unknown result on the hostname read, gives `TARGET_UNVERIFIED`, and the checklist **STOPS**. |
| C3 | `head -c 4096 /proc/self/status` | `Uid:` and `Gid:` each have four fields, all non-zero. `CapPrm`, `CapEff` and `CapAmb` are all-zero. `Seccomp:` is `0`. | A zero id or a non-zero cap gives `ROOT_OR_CAPS_PRESENT`. A missing line or `Seccomp` ≠ 0 gives unknown. `CapInh`, `CapBnd` and `NoNewPrivs` are recorded and do not gate. |
| C4 | `readlink /proc/self/ns/user`<br>`readlink /proc/self/ns/pid` | The links read `user:[4026531837]` and `pid:[4026531836]`, **and** item 4 of section 2 states a direct login on the kernel-owning OS. | Any other well-formed value gives `NOT_INITIAL_NS`. A matching value without the item 4 statement gives unknown. |
| C5 | `head -c 256 /proc/self/uid_map`<br>`head -c 256 /proc/self/gid_map` | Each file is a single line reading `0 0 4294967295`. | Any other mapping gives `NOT_INITIAL_NS`. |
| C6 | `head -c 65536 /proc/self/mountinfo` | See section 5. | `PROC_RESTRICTED` or unknown, per section 5. |
| C7 | `head -c 64 /proc/sys/user/max_user_namespaces`<br>`head -c 64 /proc/sys/user/max_pid_namespaces` | Both are integers greater than 0. | A value of `0` gives `USERNS_RESTRICTED`. |
| C8 | `head -c 64 /proc/sys/kernel/unprivileged_userns_clone` | The value is `1`. | A value of `0` gives `USERNS_RESTRICTED`. **Any failed read, including an absent file, gives unknown.** |
| C9 | `head -c 64 /proc/sys/kernel/apparmor_restrict_unprivileged_userns` | The value is `0`. | A value of `1` gives `USERNS_RESTRICTED`. **Any failed read, including an absent file, gives unknown.** |
| C10 | `readlink -e /usr/bin/unshare`<br>`readlink -e /usr/bin/prlimit`<br>`stat -c '%n %F %A %u' <resolved1> <resolved2>` | Both paths resolve. For each, `%F` is `regular file`, `%A` has an execute bit and no `s` or `S`, and `%u` is `0`. | If `readlink -e` returns rc=1 with no output, the result is `TOOL_MISSING`. Any other type, mode or owner gives unknown. |
| C11 | On dpkg: `dpkg-query -S <resolved1> <resolved2>`, then `dpkg-query -W -f='${Package} ${Version} ${Status}\n' <owner>`.<br>On rpm: `rpm -qf --qf '%{NAME} %{VERSION}-%{RELEASE}\n' <resolved1> <resolved2>`. | The owner is a util-linux package, it is installed, and a version string is recorded. | A non-util-linux owner, an unowned path (including a usr-merge mismatch), any other package manager, or any error gives unknown. The binaries are never executed. |

In every row, any unknown condition from section 3 also yields unknown.

Notes on the checks:
- **C4 and C5.**
  - The inode values and the identity map are Linux *implementation signatures*. They are consistent with the initial namespaces but are **not sufficient proof** of them. For example, a privileged container shows the same identity map.
  - A mismatch disqualifies the target. A match only means the target is not disqualified.
  - The positive basis is the operator's item 4 statement. That is provenance, not E0 proof.
  - Reviewers should confirm the two inode values against the kernel source.
- **C3.** `NSpid` is not used. Read through the reader's own procfs, it shows the same value whether or not the process is nested, which is the lessons.md non-discriminating-evidence case.
- **C8 and C9.**
  - A manual read cannot distinguish an absent file from an access failure without interpreting error text. Absence is therefore never scored as satisfied.
  - **Consequence, disclosed:** `PRECHECK_OK` is reachable only on targets that expose both files. On any other target the result is unknown and NO BUILD.
  - Any absence rule would be a separately reviewed amendment and is not part of this design.
- **C11.**
  - E0 records the version string only.
  - The minimum version and the availability of each option are D2 obligations, to be established from primary sources. D2 judges the recorded string and may convert the record to `TOOL_MISSING`.
  - This design states no version floor.

## 5. `/proc` visibility rule for C6
Consider the lines whose mount point is `/proc` or starts with `/proc/`.
- **Satisfied** only if all of these hold:
  - Exactly one line has mount point `/proc`.
  - That line has fstype `proc` and root field `/`.
  - Neither its mount options nor its super options contain a `subset=` token.
  - Neither contains a `hidepid=` token with a value other than `0` or `off`.
  - The only mount under `/proc/` is `/proc/sys/fs/binfmt_misc`, with fstype `autofs` or `binfmt_misc`.
- A `hidepid` or `subset` restriction gives `PROC_RESTRICTED`. So does a root field other than `/`, a non-proc fstype, or any other mount under `/proc/`.
- Zero or multiple `/proc` lines gives unknown. So does truncation, or a line that cannot be parsed.
- This shows mount options only. It does not show complete process visibility ([proc(5)](https://man7.org/linux/man-pages/man5/proc.5.html)).

## 6. Decision contract
- **Stage A (section 2).** If this stage fails, the result is `TARGET_UNVERIFIED` and nothing runs.
- **Stage B (C1 and C2).** A failure gives `NOT_LINUX` or `TARGET_UNVERIFIED`. The checklist **STOPS**, and no later check runs or is scored.
- **Stage C (C3 to C11).**
  - Reached only after stage B passes. Run and score these checks unless a section 3 stopping condition fires; stopping conditions always take precedence, and remaining checks are NOT_RUN.
  - The result is **`PRECHECK_OK` if and only if stages A and B passed and every stage C check is positively satisfied.**
  - Otherwise the record reports the first class that applies in this order:
    1. `ROOT_OR_CAPS_PRESENT`
    2. `NOT_INITIAL_NS`
    3. `PROC_RESTRICTED`
    4. `USERNS_RESTRICTED`
    5. `TOOL_MISSING`
    6. `PRECHECK_UNKNOWN`
  - The record also lists every check that was not satisfied.
- Every class except `PRECHECK_OK` means **NO BUILD now**.
- No fallback is substituted, and nothing on the host is changed to remedy a finding.
- An unknown result is never upgraded by argument. The only upgrade path is a new record.

**What `PRECHECK_OK` means.**
- On the identified target, in the recorded boot and session, no enumerated passive disqualifier was observed, and every gating read returned with exit evidence and without truncation. It is **suitability evidence only**.
- It does **not** assert any of the following:
  - That `unshare` or `prlimit` run, or that their options exist.
  - That namespace creation, teardown, reaping or the oracle works.
  - That any cap binds.
  - That the on-disk binaries match the package database.
  - That the session is in the initial namespaces.
  - That the target is non-production. That and the item above rest on operator provenance.
- All of those remain D2 and D3 obligations.
- The D3 driver still runs its own preflight at launch.

**Blockers E0 cannot see.**
- E0 cannot see SELinux or other LSM policy, per-profile AppArmor rules, a seccomp filter applied later, or per-user namespace accounting.
- **Forward flag to D2, not resolved here.** [namespaces(7)](https://man7.org/linux/man-pages/man7/namespaces.7.html) subjects reads of `/proc/<pid>/ns/*` to a ptrace access check, so an unprivileged oracle may meet unreadable entries. Under the plan, an unreadable entry means `UNPROVEN`.

## 7. Evidence record fields
- The design SHA and the plan SHA.
- The quoted operator identification, items 1 to 4 of section 2.
- The executor identity.
- UTC start and end times.
- The target label, hostname, boot_id, os-release ID and VERSION_ID, and `uname -srm` output.
- For each check:
  - The exact command as typed.
  - The `rc=` value.
  - The stdout byte count against its cap, or the line count for uncapped commands.
  - Whether any stderr appeared.
  - The parsed value.
  - One of satisfied, a named class, or unknown.
- The resolved tool paths with their type, mode and owner, the owning package and version, and the package-manager family.
- The stage reached, the final class, and the full list of checks that were not satisfied.
- A deviations list. Any command outside section 4 and the explicit section 3 shell/timeout/exit-evidence operations voids the record, as does any reordering or retry.
- Only the `/proc`-related `mountinfo` lines are committed.
  - The full transcript stays with the operator.
  - No environment variables, secrets or home-directory contents are read.
- The record is bound to the boot_id, the kernel release, the package version, the uid and the target label. A change in any of these makes it stale, and a stale record counts as no record.

## 8. Safety cases

| Case | Handling |
|---|---|
| No target supplied (the current state) | Nothing is executed and the condition stays NO BUILD. |
| The session is on the VPS or production, or host identity is ambiguous | The result is `TARGET_UNVERIFIED` at stage A or B, and the checklist STOPS. |
| A root shell or sudo is offered | The result is `ROOT_OR_CAPS_PRESENT`. E0 never uses sudo. |
| A container, a WSL distro, a nested namespace, or a restricted procfs | The result is `NOT_INITIAL_NS`, `PROC_RESTRICTED` or unknown. This is not worked around. |
| The AppArmor or sysctl user-namespace restriction is active | The result is `USERNS_RESTRICTED`. Enabling user namespaces is a host change and is out of scope. |
| Optional sysctl files are absent | The result is unknown, per C8 and C9. |
| A hung or D-state command, an oversized or truncated output, or a missing `rc=` line | `PRECHECK_UNKNOWN`. The operator stops and takes no privileged cleanup action. |
| A request to "just try `unshare`" or check `--version`, or to wrap the checks in a script | Forbidden, and the record is void. |
| A distro outside the dpkg and rpm families, or package-database gaps | The result is unknown. The design may be amended only under review. |

## 9. Review checklist
1. No command executes `unshare` or `prlimit`, launches a synthetic process tree, intentionally changes target contents/configuration, uses the network, or needs privilege.
2. The command list is exact and its count is as stated. Each command has defined exit evidence. Section 3 claims no hard bound and no wall-time guarantee.
3. Every check fails closed, and no branch reaches `PRECHECK_OK` from an absent, unreadable, truncated or ambiguous read.
4. The stages in section 6 are consistent, with an early STOP versus scoring every stage C check.
5. C4 and C5 claim no positive proof of the initial namespaces, and provenance is the stated basis.
6. `PRECHECK_OK` cannot be read as proof of namespace creation, caps or containment.
7. The three added result classes are an acceptable extension, or a plan amendment is needed first.
8. The narrowing consequence of C8 and C9 is acceptable.
9. The inode values, the `hidepid` and `subset` tokens, and the package commands are checked against primary sources.
10. No approval gate is invented, no custom executable is introduced, and the top markers are truthful.

## 10. Exact next gate
- Two independent terminal reviews of this revision at a fixed SHA, with findings folded. An approval is a target-contingent contract only.
- **Then** the operator identifies a non-production Linux target with the section 2 record. Execution proceeds under the existing standing read-only authority.
- **Then** an E0 record, with no deviations, whose final class is `PRECHECK_OK`.
- Only after that do the plan's D2 design reviews begin.
- Until then the condition is NO BUILD.
  - The zero-porcelain gate is unchanged.
  - The rejection of PR593 is unchanged.
  - The branch-protection gates are unchanged.
- This document claims no approval, no implementation and no execution.
