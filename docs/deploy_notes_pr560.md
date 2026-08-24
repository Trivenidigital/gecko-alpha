## DEPLOY NOTES — PR #560 (required reading before the next prod pull)

**`git pull` alone leaves part of this PR inert.** Two unit files changed, and
systemd reads `/etc/systemd/system`, not the repo:

    cd /root/gecko-alpha && git pull
    install -m 0644 scripts/recompute-coverage-watchdog.{timer,service} /etc/systemd/system/
    install -m 0644 systemd/systemd-drift-watchdog.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl list-timers recompute-coverage-watchdog.timer   # verify still armed

**Also post-merge, in the repo rather than on the box — reset master's
clearances:**

    # squash-merge carries this PR's clearance SHAs onto master, where they are
    # no longer ancestors and would misreport on unrelated work.
    # Empty the table; do NOT delete the file (deleting it turns the guard
    # green while disabling it -- see the test's SKIP-NOT-RETURN note).
    $EDITOR .reviewers.toml   # [clearances] -> empty

These two steps are NOT equivalent and should not be treated as a pair. Skipping
the `install` above has **no observable anywhere** and lands in production.
Forgetting the reset goes red on master's own push-CI within one run, with an
assertion naming the remedy. Detected-with-remedy is a different risk class from
silent; pairing them invites mis-investment in both directions.

Skip the install and the new `ExecStartPre=/usr/bin/test -x` preflights are
present in the diff, green in CI, and absent from what systemd executes.

**Three deploy mechanics, verified on the box:**

| artefact | arrives by | in this PR |
|---|---|---|
| `.sh` invoked by `ExecStart` | `git pull` | mode 100644 -> 100755 (2 files) |
| `.py` | `git pull` | test-only |
| `.service` / `.timer` | **`install` + `daemon-reload`** | **2 units gained ExecStartPre** |

**Verification status, per the ops-safety slot's convention:**
- mode changes: **fix verified** by fresh clone of the branch on the target host
  (`-rwxr-xr-x`, `test -x` OK, no chmod required); **not yet on prod** — deployed
  checkout is `cdbb847`.
- `ExecStartPre` additions: **verified in repo only.** Nobody has verified them
  on the box, and they cannot be until the install above is run.

---

## Known limitation of this session's PROSE summaries

The per-commit deltas I described to reviewers in messages drifted from the
actual diff **four times**. Three were harmless: an omitted `backlog.md`, a file
*create* described as a *rewrite*, two mode changes attributed to the wrong
commit.

**The fourth was material.** I described a commit as "two changes, both docs"
when it was four files including a `+64/-9` hardening of
`tests/test_watchdog_ships_executable.py`. A reviewer who trusted it would have
run a blob-identity check, seen only docs move, and never mutated the unit scan
— missing both that the new guard bites and that one component of it does not.

**The durable instruction is not "distrust the prose".** In all four cases the
`git diff` was correct, and specifically the **merge-base** diff:

> Measure from the merge base. That is the axis the deploy actually
> experiences, and it is immune to how the work was sliced into commits.
> Incremental deltas are a property of the authoring process; merge-base
> deltas are a property of what ships.

Every reviewer claim on this PR that was anchored to `cdbb8475..HEAD` or to the
box was untouched by all four drifts. The one discipline that caught the
material drift was a per-file blob-identity check — which is itself a diff.

**Fifth drift, same class:** I told a reviewer `scripts/check_recompute_coverage.py`
had moved in a commit where it had not — the delta was `backlog.md` plus two
test files. Their conclusion (re-run rather than transfer) was right and my
reason was wrong, and separating those matters because the correct rule is
theirs:

> Re-run a mutation sweep when the subject **or any consumer** moves.
> Subject-identical plus consumer-changed is exactly the case where a
> transferred survivor list over-reports.

A sweep's result is a function of subject × consumers. Blob-identity on the
subject alone is not sufficient to transfer it.

**Refined, because the rule as written fires on every comment edit** — and a
rule that fires on the common harmless case is the ritual failure mode: it gets
skipped on the day it matters. The trigger is not *"the consumer moved"* but
*"the consumer's **executable behaviour** moved"*, and the decision procedure is
cheap:

```python
ast.dump(strip_docstrings(ast.parse(before))) == ast.dump(strip_docstrings(ast.parse(after)))
```

Subject blob-identical **×** consumer AST-identical ⟹ the sweep transfers with
certainty.

**Worked example, with its anchors — because an earlier draft of this paragraph
gave the figure without them and a reviewer resolved it to the wrong range,
where the rule's own example inverted the rule it teaches:**

| range | file | numstat | raw identical | AST identical |
|---|---|---|---|---|
| `54d94520..7a3f294c` | `tests/test_watchdog_ships_executable.py` | `+13/−2` | no | **yes** |
| `eb72644d..dfab433f` | same file | `+66/−9` | no | **no** |

The first row is the rule firing correctly: a docstring-only edit, so the sweep
transfers without a re-run. The second is the rule *discriminating* — one test
function renamed (`test_the_unit_scan_finds_units` →
`test_the_unit_scan_covers_EVERY_unit_glob`) and the parametrised set narrowed
19 → 16 scripts, so AST equality returns False and a re-run is genuinely
required. **Both rows are needed.** A rule demonstrated only where it says "yes"
has not been shown able to say "no".

A `+13/−2` cited without naming the commits it spans is half a claim, and the
half that was missing is the half that decides which row you are in.

**And its limit, which is the asymmetry that matters:** AST equality *licenses*
skipping a re-run. Blob-identity on the subject alone **never** does. The fifth
enumeration drift on this PR was exactly that case — subject-identity claimed
while consumers had moved — and it genuinely needed the sweep.

---

## Two distinct floor failures, and they need different remedies

A bare `assert len(X) >= N` failed twice on this PR, 80 lines apart in one
file, and they are **not** the same defect:

**Heterogeneity** — the floor is blind to losing a *subgroup*. `INVOKED` was
12 cron + 4 systemd against a floor of 4, so dropping the entire cron glob
left it satisfied by the systemd half alone. *Remedy: per-source assertion
with an anchor in each class.*

**Contamination** — the floor is satisfied by members that **do not belong to
the population at all**. `UNITS` matched on `.sh` suffix with no repo-path
check, pulling in three `/usr/local/bin/gecko-backup-*.sh` units — installed
copies, governed by `install -m 0755` rather than by the checkout. Real
population 4, inflated set 7, floor 4. **The guard could not have failed.**
*Remedy: the derived set must be validated for MEMBERSHIP before any assertion
counts it.*

Both present as "the floor is too weak", which is why fixing the first did not
surface the second. Worth noting the `INVOKED` derivation got membership right
by accident — the interpreter-first-token exclusion was doing that work — not
because anyone checked it.

## What no clearance on this PR can establish

Every review slot here reads the **repository**. The repository is not what
runs. The `ExecStartPre` finding is the proof: an artefact present in the diff,
green in CI, and absent from `/etc/systemd/system` is indistinguishable from a
working one by any repository-level check.

A **runtime** check does exist, and it is worth naming here rather than leaving
the sentence above to imply nothing anywhere can see this.
`scripts/systemd-drift-watchdog.sh` diffs repo units against
`/etc/systemd/system` on a timer and pages on drift -- it is precisely the right
instrument. **It cannot see either unit this PR adds, in either direction:**
Direction A (`:113`) enumerates `$REPO_DIR/systemd` only, and these two units
are the only ones in the tree living under `scripts/` (17 units are in
`systemd/`, 2 here); Direction B's `DIRB_PATTERNS` (`:51-57`) matches
`gecko-*`, `minara-*`, `systemd-drift-watchdog.*` and so would not flag an
installed-but-stale copy as untracked either.

So the manual verification below is manual **because these units sit outside the
standing instrument**, not because no standing instrument exists. Moving them to
`systemd/` is what converts the instruction into a standing check; that is
ticket 18, deliberately not taken in this PR. Note the fix is the move -- adding
the names to `DIRB_PATTERNS` alone would page `UNTRACKED PROD UNIT` forever.

### Enumerations here were verified. Attributions were not.

Stated plainly because a reader will otherwise assume one standard was applied
throughout. Every file/count claim in this tranche was re-derived from `git
diff` or `git ls-files` before being written down. **Attribution claims -- which
slot found what -- had no such check**, and one was wrong: a message credited
the logic slot with an `initialize()` finding belonging to another slot, and
inverted which side of the "documentation nit" argument each of us had taken.

It was caught only because the slot it named read it and objected. That is not a
procedure; it is luck that the misattributed party was still listening. The
asymmetry is the point: **an enumeration has a cheap decision procedure and an
attribution does not.** `git diff` settles the first. The only check on the
second is the person named going back and reading what they actually wrote.

Note the direction of the correction -- it *removed* credit from the slot that
raised it. That is the direction nobody is motivated to catch, which is why an
inflated review record is more dangerous than an incomplete one.

So: **exact-head CI green is necessary and not sufficient**, and a full set of
clearances does not substitute for verifying the installed unit files on the box
after deploy.
