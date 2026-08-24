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
| concurrency | `e1c7b8ca` | yes | clean |
| logic | `dfab433f` | yes | clean |
| ops-safety | `dfab433f` | yes | clean |
| silent-failure | `dfab433f` | yes | clean, one deferred finding |
| test-efficacy | `68cd3f0e` | no | clean after three refusals |

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

## Enumerations here were verified. Attributions were not.

Stated because a reader will otherwise assume one standard throughout. Every
file/count claim was re-derived from `git diff` or `git ls-files` before being
written. **Attribution claims — which slot found what — had no such check**, and
one was wrong: a message credited the logic slot with another slot's finding and
inverted which side of an argument each had taken. It was caught only because
the slot it named read it and objected. That is luck, not a procedure.

An enumeration has a cheap decision procedure. An attribution does not.

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

**`install` + `daemon-reload` on the box.** See `docs/deploy_notes_pr560.md`.
Until it runs, the unit-file half of this PR is not deployed, and nothing in
this repository or its CI can tell you so.
