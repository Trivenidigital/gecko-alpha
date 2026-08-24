# Merge report — PR #560

**Branch:** `test/recompute-axis-coverage-and-lapse-gate`
**Base:** `master` at `cdbb8475` (the #559 merge)
**Date:** 2026-08-24

## What this PR is

Three things, all follow-ups to the #559 deploy:

1. **F3 parametrisation** and the two preserved reviewer harnesses.
2. **Runbook coverage** for the PK-rebuild watchpoint and for the
   migration→backfill zero-coverage interval.
3. **The CI reviewer-lapse gate** — `scripts/check_reviewer_clearances.py`,
   which fails a merge candidate whose required clearances are absent, stale
   relative to the tree, or lapsed.

## Reviewer clearances

| vector | SHA | mandatory | verdict |
|---|---|---|---|
| concurrency | `85ae29a0` | yes | clean — 7 call sites fail-closed, ~90 runs no flake |
| logic | `dde828cf` | yes | clean — after blocking twice |
| ops-safety | `d197bbea` | yes | clean — after requiring the pins |
| silent-failure | `d197bbea` | yes | clean — after two self-reversals |
| test-efficacy | `e02fcbe7` | no | clean — after three refusals |

Every SHA was named by its reviewer **at that revision** and re-derived after
each head move; none was carried forward. Nine head-moves occurred during
review, each lapsing the whole table — that is the gate working, not failing.

Gate output at the declaration commit: **all four required vectors hold**,
exit 0. Each SHA was verified as an ancestor of HEAD with no `watch` path
differing by tree hash between it and HEAD.

> ### Read this before reading the table above as coverage
>
> **No combination of clearances here substitutes for verifying the installed
> units on the box after merge. This repository is not the deployed artefact.**
>
> Every slot read the repository at a revision. None observed the deploy. The
> caveat sits here rather than in an appendix deliberately: a reader scanning
> five greens will not go looking for it.
>
> It is not abstract. `.service` and `.timer` files **do not arrive via `git
> pull`** — they require `install` + `daemon-reload`. So the `ExecStartPre`
> preflights and the executable-bit fix in this PR are **inert on the box until
> that step runs**, and a unit present in the diff, green in CI, and absent from
> `/etc/systemd/system` is indistinguishable from a working one by any check in
> this repository.
>
> The standing instrument for exactly that drift —
> `scripts/systemd-drift-watchdog.sh` — **cannot see either unit this PR adds**,
> in either direction. It enumerates `$REPO_DIR/systemd` only, and these two are
> the sole units in the tree living under `scripts/` (17 are in `systemd/`).
> Ticket 18.

### What the clearances are worth, stated honestly

The gate is a lapse **detector**, not enforcement. `.reviewers.toml` is
committed and author-writable: repointing every entry at HEAD is a four-line
edit that turns the check green *and* makes the tree comparison vacuous. The
standard fix — reading commit-bound approvals from the GitHub API — has nothing
to read here, because this project's independent reviewers are agents whose
verdicts live in session transcripts. **Zero GitHub approvals exist on any
recent PR**, and `reviewDecision` is empty on all 15 most recent merged PRs.

Two operator actions, neither of which is code, are the only path to the
mechanical enforcement originally asked for:

- **Branch protection is OFF.** `gh api .../branches/master/protection` returns
  404 and rulesets are empty. With none configured, `mergeStateStatus` reads
  `UNSTABLE` rather than `BLOCKED` — **no CI check in this repo can block a
  merge.** This PR was mergeable with its own gate red.
- **There is no author-external review record.** Until verdicts land somewhere
  the author cannot write, the record is self-attested by construction.

## The defect CI found after five rounds and five clearances

**The gate and its own conformance test were mutually exclusive.** The gate
FAILS a branch with a watched delta unless clearances are recorded in
`.reviewers.toml`; `test_the_shipped_declaration_records_no_stale_clearances`
read the **working tree** and asserted that file carried none. Clearances
absent → gate red. Clearances present → that test red. **No single commit could
satisfy both**, so the gate was unusable on exactly the branches it exists to
police.

**Why it survived five rounds and four independent vectors:** every CI run to
that point was red for *lack* of clearances — an explicable red that masked the
fact that fixing it turns a *different* job red. Each failure mode hid the
other. "Of course it's red, there are no clearances yet" is a complete,
satisfying, and wrong explanation, and the configuration that would have exposed
the contradiction was never once constructed. Nothing asserted that the two
mechanisms were **jointly** satisfiable — only that each was individually
correct. Ticket 22.

### Then the fix reproduced the defect it was fixing

The repair added a helper that split "missing ref" from "missing file"
correctly — and ended `return got.stdout if got.returncode == 0 else None`,
routing every nonzero `git show` into the `None` the caller reads as "master
carries no declaration". A corrupt object read as a clean master and **passed
silently**. That is the `_tree` conflation this PR exists to close, split
correctly at the ref layer and rebuilt one layer down at the file layer, inside
the fix for it. Absence is now determined **positively** via `git ls-tree`.

### And that fix shipped unpinned

All three properties it established reverted **silently green** under mutation:
`ls-tree`-failure→None, `show`-failure→None, and `skip`→bare-`return`. The third
is the premise ticket 21's deferral rests on, so a one-line revert would have
invalidated a recorded decision with nothing to report it. Now pinned by three
tests, each killing exactly one mutant, each carrying an anti-vacuity counter.

**The generator was constant across all fourteen rounds:** a fix correct about
the branch it was aimed at, widening a channel just past it. Mutation found
these; reading did not.

One property of the pinning worth carrying past this PR: **each mutant kills
exactly one test, and the correct one.** "All three died" and "one catch-all
died three times" are the same observation until you look — and the difference
is whether a future editor is told *what* broke or merely *that* something did.
This generalises past mutation runs to any suite where a single summary line
stands in for N properties.

## What four reviewers cost, and what it bought

Every finding above was found by a reviewer, not by the author. More useful than
the findings is that **the reviewers' own failure modes were uncorrelated** —
three vectors examined one line and produced three different epistemics:

| vector | what it did | outcome |
|---|---|---|
| ops-safety | rated reachability without exercising the path | wrong — under-rated a blocking fail-open |
| silent-failure | exercised it, harness failed on Windows read-only objects, read that as evidence about the code | wrong — reported unreachable |
| logic | constructed the failing case | **decisive — blocked** |

And the concurrency vector discarded two of its own broken probes before
reporting: the first reported `FAIL-OPEN` for an injection that never fired, the
second tested three labels against a four-call sequence. Reporting either would
have handed the author a false blocking finding or a false clean.

**These are not equally recoverable — which is not the same as one being
harmless.** An un-exercised rating was wrong and nearly let a blocking fail-open
through; it was *recoverable* only in that anyone who ran the path would have
found it. A broken harness is worse in kind: it produces **evidence-shaped
output**, so "I tried to reproduce it and couldn't" reads as a stronger
clearance than "I didn't try". It manufactures confidence *and argues against
the next attempt.* That asymmetry — not reviewer diligence — is the argument for
overlapping vectors, and it is why the concurrency vector discarding two of its
own probes before reporting was worth more than any single verdict.

The distinguishing question was identical in all four cases, and in the author's
two: **did my probe actually exercise the path I am making a claim about?**

### Each vector's misses are recorded beside its catches, deliberately

At the reviewers' own insistence, and because omitting them would be the
inflated-review-record failure this PR spent a section on. Every slot that found
something here also produced at least one claim another slot had to correct:

- **ops-safety** — rated a fail-open's reachability without exercising it; then
  demonstrated a severity-raise on the wrong subject; then cleared a paragraph
  whose appended safety claim nobody had measured.
- **silent-failure** — reported a defect unreachable when its harness had failed
  for its own reasons; proposed a pin against a configuration the code cannot
  enter.
- **logic** — cleared a SHA without checking whether the fix was pinned at all;
  approved wording broader than the single case its probe had built.
- **concurrency** — generalised an *absence* of defect from one branch of a
  two-branch path; discarded two of its own broken probes before reporting.
- **author** — ten enumeration drifts, an invalid mutation proof, two
  unreached-mechanism claims, and guards written without pins after recording
  the rule that says to pin them.

**That ratio is the case for four vectors, better than any individual finding
is.** The failure modes were uncorrelated: every error above was caught by a
vector that was not looking where its author was looking.

## Enumerations here were verified. Attributions were not.

Stated because a reader will otherwise assume one standard throughout. Every
file/count claim was re-derived from `git diff` or `git ls-files` before being
written. **Attribution claims — which slot found what — had no such check**, and
one was wrong: a message credited the logic slot with another slot's finding and
inverted which side of an argument each had taken. It was caught only because
the slot it named read it and objected. That is luck, not a procedure.

An enumeration has a cheap decision procedure. An attribution does not.

**Ten drifts occurred. The tenth was a new kind and it explains why the first
nine survived carefulness:** a *correct* value attached to the *wrong anchor*
(`+21/−6` was real, but measured above a different SHA than the one named). It
survives spot-checking, because the figures verify and only the label is false.

The remedy adopted — **quote the invocation with its output, so the base and the
figures come from the same text** — works for a reason worth stating, because it
predicts where it will hold: *it deletes a transcription step rather than asking
for an existing step to be performed better.* Nine drifts survived being
careful. The test for any future fix of this shape is the same question: **does
it remove a step, or does it ask someone to do an existing step more
diligently?**

And it does **not** generalise to attribution, which has no command that prints
it. That gap stays open, with exactly one check — the person named reading what
they actually wrote. Recorded explicitly so the numeric fix does not create the
impression the whole class is closed.

### Two kinds of unexercised-hypothetical claim, and only one is dangerous

Four comments on this PR were corrected for asserting things about mechanisms
nobody had exercised. The fourth differs from the first three in a way worth
separating:

- **Accurate about a mechanism the code never reaches** — *misleads* a reader.
  Three instances, including two of the author's.
- **Asserts protective behaviour that is actively harmful in one of the
  movements it claims to cover** — *justifies a wrong decision.* One instance:
  "real defence if `cwd` or `REPO_ROOT` ever moves", where under nesting
  `--full-tree` **causes** the vacuous skip it was added to prevent.

The second is the dangerous class, because a reader acting on it keeps a flag
that makes things worse. Both were caught by reviewers running the case the
sentence asserted about.

Note the direction: the correction *removed* credit from the slot that raised
it. That is the direction nobody is motivated to catch, which is why an inflated
review record is more dangerous than an incomplete one.

**Eight enumeration drifts occurred on this PR.** The two that mattered:
a `+64/−9` test hardening described as "docs", and a worked example in the
document teaching the AST rule that cited a range where the rule *fails* —
because the figure was stated without naming the commits it spanned. Both were
caught by reviewers, not by me. The generator was identified twice
independently: **a delta stated without its anchors is half a claim.**

## Deferred, as separate tickets — not folded into this PR

Per the standing instruction not to opportunistically mix residuals into a
tests-only follow-up:

| # | subject |
|---|---|
| 13 | clearance-check structural residuals + the two operator actions |
| 14 | recompute-probe liveness heartbeat (§12a) |
| 15 | repo-path executable-bit derivation (closed in-PR, class recorded) |
| 16 | five unpinned checker alert strings |
| 17 | `cron-drift-watchdog.sh` outside every guard |
| 18 | this PR's two units are invisible to the unit-drift watchdog |
| 19 | `by_glob` derived from the tuple under test — inert diagnostic |
| 20 | the gate flags docstring-only deltas as lapsed (do **not** fix in code) |

Tickets 18 and 19 were both raised by reviewers as non-blocking and deferred for
the same reason: `tests/` and the runbook are watched or operator-verified, and
taking a non-blocking improvement in the last SHA before merge would lapse four
mandatory clearances for a diagnostic-quality gain.

Ticket 20 carries an explicit **do not fix this in code** — teaching the gate to
parse files means acquiring a parser, syntax errors, non-Python watched paths,
and an exception handler deciding whether an unparseable file counts as changed.
That is a fail-open surface in the one script whose entire job is not failing
open. Endorsed independently by the logic and test-efficacy slots.

## Required after merge

**1. `install` + `daemon-reload` on the box.** See `docs/deploy_notes_pr560.md`.
Until it runs, the unit-file half of this PR is not deployed, and **nothing in
this repository or its CI can tell you so.**

**2. Reset master's `[clearances]` to empty.** Squash-merge carries this PR's
clearance SHAs onto master, where they stop being ancestors. Unlike step 1 this
one is **loud** if forgotten — master's own push-CI goes red with an assertion
naming the remedy. Empty the table; do not delete the file.

The two are not equivalent and the difference is the whole point: step 1 is
silent, step 2 is detected. Ticket 21 removes step 2 entirely by giving each PR
its own file.
