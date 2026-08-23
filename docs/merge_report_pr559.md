# Merge report — PR #559, versioned legacy-provenance recomputation

**Base:** `origin/master` (`f05dd47f`) · **footprint measured at `d14a704f`:**
57 commits, 36 files, 13 new test files · **0 behind**, no rebase pending.
(The SHA is stamped because both numbers are only meaningful against one — §5b.)

Every cleared item below records **the SHA it was measured on**. That is not
bookkeeping: across twelve revisions, **four reviewer clearances lapsed and a
fifth was wrong** — invalidated by changes that were themselves fixes for other
findings, and none of those invalidations visible from the diff that caused
them. The lapsed four are `1076f56d`, `89b070ef`, `97365248`, `b90f66fa`; the
wrong one is `6276d586`, a fully green battery over a blocking regression
(§5a). Collapsing those two categories into one count would undo the only
distinction §5a exists to draw. "A clearance is a
property of a revision, not of a component" is the single most useful sentence
produced by this review, and it came from a reviewer flagging it against their
own earlier verdict.

---

## 1. What this ships

Pre-cutover "chains" detections had lead times derived by **prefix matching on
token symbols**. Ruling C decided prefix similarity is not identity, so those
leads cannot stand as-is. Dropping them outright — the naive cutover — takes
gainers `tier_high` from **341 to 187**.

This adds a versioned overlay that re-verifies each archived row against
canonical identity. Archived rows are never rewritten. With the overlay,
`tier_high` is **315**: 128 of the 154 high-tier rows the naive cutover would
destroy are recovered.

Full acceptance evidence: `docs/acceptance_chain_identity_recompute.md`.
Operations: `docs/runbook_recompute_coverage.md`.

## 2. Verification state

| gate | state | evidence |
|---|---|---|
| exact-head CI | required green at the final head | production tree already green at `2fde4cf8`; head is docs-only above it |
| full production replay | 12 revisions, stable | `c1a50e33` … `2fde4cf8` |
| totals reconcile | 2,891 statuses = 2,891 written = population | `reconciliation_report` |
| independent reviewers | 4 dispatched; **all four terminal and holding at head** | see §4 |

**Replay result** (unchanged across all twelve revisions except one deliberate
status split):

| status | rows | earns credit |
|---|---:|:--:|
| `verified_canonical` | 1,543 | **yes** |
| `indeterminate_history` | 968 | no |
| `canonical_below_gate_indeterminate` | 249 | no |
| `no_legacy_credit` | 124 | n/a |
| `verified_prefix_only` | 4 | no |
| `alias_tier_not_verifiable` | 3 | no |

**All 968 `indeterminate_history` rows have anchors outside the coverage
intervals; zero are inside-coverage-but-unmatched.** That is the discriminating
number in the whole tranche — it is what separates "the resolver declines to
assert negatives where history does not reach" from "the resolver silently
failed to match", and a reader would otherwise have to take it on faith.

**Independent cross-check:** the collapse alarm's ratchet, on its first
observation of the replay, recorded 0.4930 / 0.5472 / 0.7145 per surface. The
acceptance report states 49.3% / 54.7% / 71.5%, derived from the status tally —
same numbers, different code path.

## 3. What this does NOT establish

- **1,220 rows cannot be decided in either direction.** Their history predates
  every surviving source. The drop is dominated by *unverifiable history*, not
  by demonstrated fabrication — which supports neither "~96% should recover"
  nor "mostly a metadata artefact". That gap is permanent.
- Only **4 rows** are best explained by a prefix collision, and even those are
  not proof: the coverage predicate is a global span over all tokens' events, so
  it establishes that we were recording, never that this coin's canonical-id
  token would have been seen. Per-token coverage is the named residual.
- Recovery once the `/root` snapshots are deleted. This is why the alarm
  escalates on recovered credit and on rate collapse, not on row count.

## 4. Reviewer slots (ruling D)

**Production code on this branch ends at `2fde4cf8`.** The delta above it is
`docs/merge_report_pr559.md` and `backlog.md` — two files, one of which is not
under `docs/`, so "documentation only" is the loose phrasing and the exact claim
is *no `scout/` or `scripts/` delta*. Stated as the corrected rule rather than
the name-level one:
`git merge-base --is-ancestor <sha> HEAD` for the ancestry guard, then
`git rev-parse <sha>:scout` and `<sha>:scripts` compared to head's. Name-level
diffing misses a file restored to the wrong baseline; tree hashes cannot.

```
2fde4cf8:scout == 1aa81f7e:scout == 014ce900:scout == HEAD:scout  = a0a2f4b8
97365248:scout == b90f66fa:scout                                  = e55fc284
```

So any clearance measured at `2fde4cf8` or later covers the merging tree.

Lapse status is **computed, not asserted** — for each clearance SHA, is there a
`scout/` or `scripts/` delta to head. Three steps, in order, and the first was
missing until it cost us:

1. **`git fetch origin`** — the base ref itself may be stale
2. `git merge-base --is-ancestor <sha> HEAD` — ancestry guard
3. compare `<sha>:scout` and `<sha>:scripts` tree hashes against head's

Step 2 catches *wrong branch named* and is **silent** on *right branch, stale
ref*. The ops reviewer found the first spelling (`origin/HEAD` resolving to
`origin/master`, producing a false LAPSED reporting 3,546 deletions) and fixed
that spelling without asking whether the base ref was current — the same
root-cause family, one step over. Step 1 is what closes it.

| vector | last verdict | measured on | holds at `f7a200cf`? |
|---|---|---|---|
| structural / concurrency | **CLEAN, terminal** — re-run after lapse; 4 questions answered with discriminating controls | `f7a200cf` | **HOLDS** |
| silent failure / observability | **CLEAN, terminal** — re-run after lapse; defect reproduced then confirmed fixed; one non-blocking finding (F3, ticketed) | `f7a200cf` | **HOLDS** |
| ops safety / evidence integrity | **CLEAN, terminal** — S1–S7, N-1…N-6, R1–R5, T1, U1/U2, W1/W2, Y1 | `014ce900` | **HOLDS** |
| recompute logic | CLEAN — "ship it"; E1 and the `armed_rate` trap closed | `1aa81f7e` | **HOLDS** |

The three commits that lapsed the first two slots are the entire ratchet
rework — `6276d586` (name the decision), `d831fb56` (name what replaced a
discarded mark), `2fde4cf8` (**the mark must be able to rise**). The second and
third of those exist *because* of findings raised after those clearances, which
is precisely the case ruling D's re-run requirement is for: both slots cleared a
`scout/db.py` that no longer exists, and one of the intervening commits fixed a
blocking regression on the silent-failure vector itself (§5a).

**All four slots terminal and holding at head.** Both lapsed slots re-ran
against the current tree rather than being carried forward — and the rule earned
itself on its first live occasion: it caught a real code change (the entire
ratchet rework, including a blocking regression on the silent-failure vector)
rather than documentation churn. A clearance is not carried forward across a
production delta, and the implementer's own review never satisfies an
independent slot.

**Stamp the SHA whenever citing a commit count or a file count.** Measured at
three heads on this branch: 55/36, 56/36, 57/36 — the commit count moves with
every documentation commit, the file count does not. The tempting conclusion is
"cite files, they are stable." That is wrong, and the ops reviewer caught it
before it shipped: 36 is stable *because production froze at `2fde4cf8`*. It is
stable while the tree is, not intrinsically. If production moves, a reader
trusting the file count is misled in the other direction — by a number this
report recommended. The durable rule is the stamp; the choice of metric is
secondary. *(My first version of this paragraph asserted the metric property.
It is recorded as the fifth instance in §5b-iii.)*

**A stale local ref made the branch look like it carried other PRs' work.**
Recomputing the header against `master` gave 66 commits / 91 files, including
CG-governor and first-seen-substrate files that belong to already-merged PRs.
The local `master` ref was pinned at `3e936cd8` while `origin/master` had moved
to `f05dd47f`. Against the real base the branch is 56 commits / 36 files and
**0 behind**. Recorded because the failure is silent in the direction that
matters: it *overstates* footprint, so it reads as diligence rather than as an
error, and the same stale ref would make a rebase check report "up to date"
against a base that no longer exists.

### Findings raised and closed, by slot

| vector | findings | fixed at |
|---|---|---|
| structural / concurrency | shared-connection laundering; ratchet write moved to its own connection | `97365248` |
| silent failure / observability | S1–S4, all fixed | `f2847774` |
| ops safety / evidence integrity | N-1…N-6 + R1–R5, all fixed | `c8b5e009` |
| recompute logic | nothing blocking; R1 implemented rather than deferred | `47847f47` |

### The re-run found something the first pass could not

The concurrency slot's re-run answered four questions with discriminating
controls rather than clean results — a detector that only ever says "clean"
proves nothing, so they ran it against a subject that *does* launder:

```
cache_prices (shared connection)        sibling rows surviving rollback: 1  LAUNDERED
_record_coverage_baseline (own conn)    sibling rows surviving rollback: 0  clean
detector discriminates: YES
```

That settles the question I could not settle myself: the dedicated connection is
an **escape**, not a relocation, because connection A's `COMMIT` cannot commit
connection B's pending work — which is also why the read-back is sound here
while the same pattern on a shared connection was not.

**And one question came back against them.** I asked whether making a guarded
commit unconditional widened its blast radius. It does: the `count == 0` path
now commits, and it launders. Reachability is low — all four call sites guard
with a non-empty check (`main.py:1004`, `agent.py:325`, `outcome_ledger.py:978`
and `:1065`), so it needs a non-empty list in which every element lacks `id`,
i.e. malformed API data. Not blocking.

The reviewer's own framing of it is the part worth keeping: they had recommended
that change having verified the concurrency context of one of the two call sites
and generalised to the other from its shape — *"same shape, second instance,"*
the exact reasoning they had spent the review flagging in other people's work.
That is the sixth instance of §5b-iii's generator and the second contributed by
a reviewer against their own recommendation.

**A larger pre-existing defect surfaced while measuring it**, and it is not this
PR's: `cache_prices` launders on its *dominant* path, every pipeline cycle from
`main.py:1006`, concurrently with `run_chain_tracker`'s bare `BEGIN`/
`rollback()`. It predates this branch and widening it from "most calls" to "all
calls" does not change its character. Ticketed, not merged against.

## 5. Open residuals, carried deliberately

| residual | why not closed |
|---|---|
| Coverage is a global span, not per-token | A design change. Every outcome it produces is conservative now that `alias_unique` cannot promote. |
| Ratchet's population guard is one-sided | It re-establishes a mark recorded against a transiently small population. It does **not** cover shrinkage — see the row below. Written as a ratio it silently *lowered* the mark on ordinary growth (0.60 → 0.36 measured), which is the defect R2 caught. |
| Ratchet has no downward re-calibration | The surviving population is not a random sample — rows that never re-appear skew toward old anchors that resolve indeterminate, so the rate over a shrinking remainder can fall benignly. `population` is recorded, so the hook exists; the composition cannot be measured before a post-deploy population exists. |
| `coverage_intervals=None` falls back to a global floor | Latent: no runtime caller outside the ops script, which always passes explicit intervals. |
| Post-archive rows can never be covered | Named in the runbook and counted as `unarchivable` in both the payload and the alert text. |

## 5a. A clearance that was WRONG

Recorded at the reviewer's own request, and it matters more to a future reader
than any single defect here.

The ops-safety slot cleared `6276d586`. That SHA carried a **blocking
regression**: the collapse alarm's high-water mark could never rise, and
because the probe is hourly while the backfill is a manual post-deploy step,
the first observation arms at `0.0` — which makes `rate < best * FRACTION`
unsatisfiable for every possible rate. Collapse detection dead on every
surface, from the first pass after deploy. `dark_surfaces` still fires
pre-backfill, so the operator is not blind at deploy; the moment the backfill
runs, `dark` clears and the collapse alarm is silently gone.

Their whole battery ran green against that SHA. The reason is structural and
they evidenced it rather than guessed: **they had found the original defect on
the *falling* axis — a mark being lowered — so every scenario they built
afterwards moves the rate down or holds it flat.** Arm then collapse. Arm then
dilute. Arm, hold, hold, drop. Not one script ever tested a rate *rising*.

That is the same generator as §5b's harness-axis rule, and they were the
reviewer who taught it to me — one round after filing it against my harness,
their own battery had exactly the shape they had described. Diagnosing the
pattern elsewhere bought no protection against it.

**A superseded clearance and a wrong clearance are different things**, and a
report that records only the former is misleading. Weight the clearances in §4
accordingly.

## 5b. Process rules this review produced

These are not observations about this PR. Each cost a blocking round, and
each is a rule about how review itself is run.

**A verdict without a revision attached is not a weaker verdict — it is not a
verdict.** The cheap instance of this bit within an hour of being named: a
crossed message, caught in one round, no cost. The expensive instance is the
same mechanism at distance — someone reads "reviewed and clean" in this report
in six months, against a branch that has moved eleven commits, and re-runs
nothing. The SHA-tagged verdict lines in §4 are what prevent that, and they only
work if the report carries the SHA every time it carries the word "clean".

**A clearance is a property of a revision, not of a component** — and the cheap
mechanical form is a tree hash, not a filename diff. A name-level diff misses a
file restored to the *wrong* baseline, which is exactly what a killed mutation
harness leaves behind.

```bash
B=origin/fix/canonical-identity-semantics     # NAME the branch; never origin/HEAD
git merge-base --is-ancestor "$SHA" "$B" || echo "WRONG TARGET — not a revert"
[ "$(git rev-parse $SHA:scout)"   = "$(git rev-parse $B:scout)" ] &&
[ "$(git rev-parse $SHA:scripts)" = "$(git rev-parse $B:scripts)" ] &&
  echo CARRIES || echo LAPSED
```

**The ancestry guard is the load-bearing line, and it was missing from the first
version of this rule** — which I published here before running it. `origin/HEAD`
resolves to `origin/master`, which does not contain this tranche, so the tree
hashes differ and the check reports 15 files and 3,546 deletions. That reads as
a mass revert of the entire feature. It is the tranche's *absence* from the
wrong branch. Without the guard, "production moved" and "you asked the wrong
branch" are indistinguishable — and the wrong-branch case is far louder, being
the one result likely to trigger an emergency response.

Found by the reviewer who proposed the rule, by executing it once, one message
after handing it over. **The artefact either of us was most confident about was
the one neither of us had run** — which is the same lesson as §5c, at the
smallest possible scale, and the cheapest instance of it in the review. Four reviewer
clearances were invalidated by later changes that were themselves fixes for
other findings, and none of those invalidations was visible from the diff that
caused them. A clearance with a SHA beside it is a *record*, not a gate — so
**the head at merge must equal the head that was cleared, or the slot reopens
automatically.** I proved the need for the automatic form by failing the manual
one: I closed a reviewer's slot citing their clean verdict on an older SHA while
their verdict on the current one had two blockers.

**A reconciliation is new code and needs its own pass.** When two reviewers
converge on one root cause from opposite ends and their fixes conflict, what
ships is a third design *neither of them reviewed* — and the seam is the
least-attacked code in the change. Both of the last two blocking findings lived
exactly there. This is a dispatch rule, not a coding one.

**Ask whether a fix created a new sanctioned exception to the invariant it just
closed.** A deliberately-left hole is load-bearing and under-tested by
construction. The explicit-clear added to reconcile two fixes opened a dead
band on the first try and a starved-clear contradiction on the second.

**Mutation evidence is asymmetric, in the direction opposite to intuition.**
Four distinct ways a mutation run lies, all found in this review — two in my
harness, one in a reviewer's, the third only by reading what a mutant actually
did, and the fourth by a reviewer turning the third against their own sweep:

| failure | what it looks like | guard |
|---|---|---|
| the edit never applied | a clean pass, i.e. a survival | `assert old in source` |
| the edit applied and broke syntax | pytest exits non-zero — indistinguishable from a kill | `ast.parse(mutated)`, and score on `FAILED`, never on `ERROR` |
| the edit applied, parsed, ran, and did not express the defect | a survival, or a kill by the wrong test | read what the mutation *does*; no automated guard exists |
| the mutant died for a reason that is **not** the defect it is named for | a clean kill — nothing in the output looks wrong | check the mutant fails *the way the defect failed* |

**Read row four first.** The rows are in discovery order, but the concurrency
reviewer's ranking is the useful one for a successor: rows one to three produce
mutants that are *visibly* wrong once you read the output. Row four produces one
that looks right. A false result that reads as a genuine kill is the one that
survives review — and both reviewers hit it against their own instruments within
the same hour, on different vectors, which says the generator is the instrument
rather than the operator. That is the argument for a discriminating negative
control being mandatory rather than good practice: a detector that only ever
says "clean" has not been shown to be able to say anything else.

**The fourth row is a different kind of lie.** Rows one to three produce false
*kills* or false survivals. The fourth is a genuine kill that is a false
negative about **test quality** — which is worse, because the run looks healthy.
The silent-failure reviewer found it by applying row three to their own sweep: a
mutant dropping `raise_mark` from the write arm died with `AssertionError:
unhandled coverage-mark decision: 'raise_mark'` — the exhaustiveness guard
firing, not a test observing a frozen mark. A real kill of that mutant, and no
evidence at all that the tests can see the production defect, because the real
defect never produced an unhandled decision: **the arm did not exist to be
unhandled.** Their corrected mutant modelled the original shape — classifier
routes improvements to `compare`, every arm still handled, nothing rises — and
failed on stored state with `assert 0.0 == 0.53`.

Their rule, which is the executable form: **when a mutant is named for a
historical defect, check that it fails the way the defect failed. If the failure
signature is a guard rather than an observation, the guard is doing the work and
the observation is still unpinned.**

There is a second-order consequence worth naming. The exhaustiveness guard was
itself one of my fixes, and it made the mutation harness *look* healthier by
converting an observation failure into a guard failure. A fix that raised the
kill count while degrading what the kill count meant — the only defect here that
landed on the measuring instrument rather than on the code. The reviewer's
sharper statement of it: **the fix raised the kill count while lowering what a
kill was worth** — the instrument got quieter about its own blind spot. That
generalises past mutation testing to any defensive check added near a test
boundary.

The third row produced a false result in **both** harnesses independently, which
is what makes it a property of the technique rather than carelessness. Mine: a
re-audit mutant that added a stray `SELECT` instead of the original's inline
write *and commit*, so it exercised nothing and survived. Theirs: flipping one
of two independent `source_table ==` checks for the losers time bound, leaving
the second, so the mutant was too weak to express the defect and survived. In
both cases the only thing that caught it was re-reading what the edit actually
changed.

That yields the sharper statement of the asymmetry, and it is symmetric in
obligation rather than in strength:

- a **survival** obliges you to prove the mutant was strong enough to express
  the defect — a weak mutant and a genuine gap are indistinguishable;
- a **kill** obliges you to prove it failed for the right reason — a broken
  mutant and a detected one are indistinguishable.

Neither direction is self-certifying, and the two guards are different. That is
why the third row has no mechanical entry and cannot get one.

**The four items below catch a harness that is broken. The second-order check
above catches a harness working exactly as designed and still telling you less
than it did yesterday** — which is why it belongs in front of the contract
rather than after it. The mechanical items are cheap and finite; that one is
neither, and no automated guard exists for it.

The resulting harness contract is four items, each catching a different lie:

1. `assert old in source` — the edit never applied;
2. `ast.parse(mutated)` — the edit applied and broke syntax;
3. detect on `FAILED`, never on `ERROR` — `errors during collection` is a broken
   mutant, not a dead one;
4. **restore on ENTRY, not only in `finally`** — a hard timeout kills the
   process before cleanup runs. Both of us hit this; between us it left mutants
   in the working tree four times.

The second is the nastiest, because the file on disk really did change — it
*looks* applied. The third has no mechanical guard at all: I substituted a
weaker mutant during a re-audit, it survived, and reporting that at face value
would have been a false negative sitting beside my false positives. A fourth,
operational: **restore must survive the harness being killed** — `finally` does
not run through a hard timeout, and three of my sweeps left a mutant in the
working tree that way.


A *survival* is strong evidence — a mutant that passes is one nothing caught. A
*kill* is weak: the mutant may have failed for the wrong reason. I recorded a
kill that was a binding-count `ProgrammingError`, not detection; the clause was
unpinned for two more rounds. Two mitigations, both adopted here: neutralise a
predicate in place rather than deleting it, so arity is preserved by
construction; and verify a kill failed on the intended assertion rather than on
any error.

**When someone documents where a risk bites hardest, check the new code against
that document first.** I wrote a comment naming trending as oscillating around
20, then shipped a dead band starting at exactly 20. The evidence was already in
hand, which is precisely why it went unread.

## 5b-ii. A check that restates its own premise will always pass

Two findings in this review turn out to be one shape, and it is the shape that
neither discipline nor an agreement harness can catch:

```
anchor_covered             asserts PER-TOKEN coverage      from a GLOBAL span
the exhaustiveness assert   asserts DECISION completeness    from ARM completeness
```

**The recognition test**, because the shape above is only usable by someone who
already recognises it:

1. find the level at which the guarantee is **stated**
2. find the level at which the thing is **determined**
3. if they are not the same level, the check restates its own premise

Stated without those three steps the formulation is close to unfalsifiable —
any check can be described that way by a reader motivated to dismiss it, and no
reader can tell which of *their* checks it indicts. That warning came from the
evidence-semantics reviewer, and it is the same failure this tranche kept
producing in another medium: a claim recorded at a higher level of generality
than what was actually verified. Committing that into the lessons section itself
would have been a poor ending.

In both, the check exists, runs, and passes — while evaluating a restatement of
its own premise rather than the thing the premise is about. An exhaustiveness
check over a taxonomy proves the taxonomy's **arms** are handled; it says
nothing about whether the taxonomy covers the decision's **axes**. The boolean
chain branched on comparability *and* improvement; the taxonomy named
comparability; the improvement axis had no arm to be unhandled in, so it
vanished while the assert stayed satisfied.

Neither is a skipped check, so discipline does not find them. An agreement
harness does not either — both layers hold the same premise and agree while
both are wrong. **Only running the underlying question can fail:** the
per-token question, the per-axis question.

Recorded because I asserted the false guarantee and the evidence-semantics
reviewer endorsed it in writing one message later — two people, both looking
directly at it, neither deriving it. It took the defect.

## 5b-iii. Why the vectors must be orthogonal, not merely numerous

The usual argument for multiple reviewers is that more people find more. That is
not what happened here, and it is not why the structure worked. The
recompute-logic reviewer put it correctly:

> Attention doesn't scale against your own premises, because the premise is what
> is doing the looking. A second reviewer isn't more attention — it's a
> different premise.

Four reviewers on one axis would have found none of the five generality defects.
The evidence is who caught each:

| # | instance | caught by |
|---|---|---|
| 1 | ops reviewer's falling-axis battery | the defect surfacing |
| 2 | ops reviewer's `origin/HEAD` stale-ref sibling | me, accidentally, chasing a stale header |
| 3 | my false-guarantee commit message | the defect — the logic reviewer had *endorsed* it one message later |
| 4 | my false enumeration, written while fixing a false-enumeration finding | review |
| 5 | my file-count over-generalisation | the ops reviewer, pre-ship |

**5/5, and not one of them caught by its author re-reading their own claim.**
Stated as 5/5 with the list rather than as "never," because a bare "never" is
the same move this section is about. Two were caught by accident rather than by
review, which weakens the structural argument and is left in for that reason.

This also explains the one result that would otherwise read as a process
failure: the logic reviewer endorsing my false guarantee was not inattention. It
was a shared premise, and no amount of care on either side would have separated
them from it.

## 5b-iv. The report is authoritative; messages are lossy

Three attributions in a message I sent a reviewer were wrong — I credited them
with a finding that was another reviewer's, with a thread that was not theirs at
all, and with a refinement that was mine. **The report had all three right.** The
error existed only in the correspondence.

The ops reviewer drew the structural point, and it is better than the resolution
I had reached ("be more careful in messages," which is a wish, not a mechanism):
a report defect has many readers; a correspondence defect has exactly one, so it
survives unless that one reader happens to be checking. Keep the report as the
authoritative artefact and treat messages as lossy by design.

Worth noting they caught it by *declining credit twice in one message*,
including for the three-readings column in §5c — which, had it stayed
misattributed, would have made §5c fail inside its own worked example.

## 5b-v. A reviewer's battery inherits the shape of the first defect they found

The best generalisation in this review, contributed by the silent-failure
reviewer as the rule behind both my warning to them and their own last finding:

> A reviewer's battery inherits the shape of the first defect they found. The
> defence is not more scenarios along the axis you know; it is asking which axis
> the suite never varies.

The evidence is three batteries, each blind in the shape of its own first catch:

| reviewer | first defect found | axis their battery then never varied |
|---|---|---|
| ops safety | a mark being *lowered* | direction — every later scenario fell or held flat |
| silent failure | *layer divergence* (probe vs checker) | surface — two-layer scenarios, one surface |
| me | the seam itself | which arm exists at all (§5b-ii) |

I warned the silent-failure slot about the first row's failure and they checked
before defending — their battery *did* contain two rises, so the axis was not
absent, but two incidental rises is not coverage. They built a dedicated rising
battery (six shapes, all correct, including that the ratchet self-heals on the
pass after a starved write), and then found **F3**: the suite is blind to
*surface*. A mutant raising the mark only for gainers passes all 116 tests,
while the real deploy sequence run against two surfaces shows trending frozen at
the pre-backfill `0.0`, collapse silent, nothing red — the `6276d586` regression
exactly, hiding on the surface most exposed to it.

No live defect: the shipped classifier has no surface conditional, which is why
the finding had to add one. It is ticketed
(`BL-NEW-RECOMPUTE-SUITE-AXIS-COVERAGE`) rather than fixed here, because
changing `tests/` changes what CI runs and every clearance in this tranche is
measured on a tree without it.

For this component the axes are **direction, surface, layer, durability of
stored state.** The suite ranges over three of the four, and that sentence is
the deliverable — not the count.

## 5c. The axis inventory — a step, not a principle

Both reviewers and I independently articulated "a matrix built from the code's
axes cannot see a failure on a different axis", credited it, and wrote it
down — and then each of us built the *next* artefact from the axis of the bug
we happened to have in hand. **The principle did not transfer.** It was
understood by everyone and prevented nothing.

What transferred was a step, contributed by the ops reviewer after their own
clearance turned out to be wrong:

> **Axis inventory.** List every scenario in the battery as
> `start-state -> end-state`. Sort by direction. If every row moves the same
> way, the battery has one axis and the clearance is worth one axis. Then write
> down the directions with NO row — rising, flat, absent, first-ever-observation
> — and add one scenario per missing direction **before** clearing.

Their ratchet battery was four rows: `0.8→0.184`, `0.184→0.04`,
`0.8→0.8→0.8→0.1`, `0.92→0.30`. All falling or flat. Zero rising, zero
first-observation. **The regression lived in both empty columns.** Two minutes,
mechanical, and neither of us managed it by noticing.

Run against this PR's probe battery, the same inventory gives:

| direction | rows |
|---|---|
| falling | 7 |
| flat | 2 |
| **rising** | 1 — `test_the_mark_RISES_when_recovery_improves`, added *by the fix* |
| **first observation** | 1 — `test_the_production_deploy_ORDER_does_not_freeze_the_mark`, added *by the fix* |
| population growth | 2 |
| **population shrinkage** | **0** |

An empty column has **three** readings, not two, and the third is what keeps the
step usable:

| reading | meaning | action |
|---|---|---|
| covered | a row exists | none |
| **not covered** | no row, no reason | **defect — add a scenario before clearing** |
| covered by a decision | no row, but a recorded residual explains why | none; cite the residual |

Shrinkage is the third case here: empty, but mapped to the recorded residual
about a non-random surviving population — untested on purpose, not by omission.
Without that distinction the inventory flags every gap equally, manufactures
work, and gets ignored within two uses. With it, the two genuinely empty columns
stand out — and they are exactly where the dead alarm lived.

The general form, which is why this section exists at all: **an artefact that
is executable starts working immediately; one that is a principle does not
work at all.** The four-item harness contract caught things within the hour.
"Watch out for axis blindness" caught nothing until a reviewer found the dead
alarm.

## 6. What this review actually cost, and why

29 commits for one feature is not a good ratio, and the reason is worth
recording. **Six of the defects found were introduced by fixes for earlier
defects**, at successively deeper levels:

1. The archive stopped being rewritten.
2. Then the overlay stopped being rewritten (`semantics_version` in the key).
3. Then nothing told the reader *which version was current*.

Each fix was correct and each created the next level's problem. The reviewers
named this pattern before I did.

Three recurring failure shapes in my own work:

- **Fixes landing in the layer that logs rather than the layer that pages** —
  three times (`unarchivable`, the population-comparability guard, per-surface
  escalation). I reach for the component I am already editing.
- **Fixtures that cannot see the defect they were written for** — six times.
  Once I added a `mkdir` that created the directory a test existed to find
  *missing*, turning a working test green against a broken wrapper. Worse, the
  last one had the defect **in its own output**: a test drove the ratchet's
  population guard, watched the high-water mark fall from 1.00 to 0.20 in its
  own fixture, and asserted only that nothing paged. Five of the six were mine,
  so this is a pattern in how I write fixtures, not six unrelated slips.
- **Asserting a method rather than running it** — false parity claims in
  comments four separate times, each stating that a guard covered something it
  did not.

**CI caught two defects my machine structurally could not**, because the shell
wrapper tests skip on win32. Both were in the alarm that exists precisely
because nothing on that box reads journald.

---

## 7. What actually made this work

Four vectors meant no single blind spot was load-bearing. But the structure is
not what found the defects — **disclosure** is. Two reviewers audited their own
tooling only because someone else disclosed a flaw in theirs first. A reviewer
asked that their own clearance be recorded as *wrong* rather than superseded. I
said "my fix caused this" six times, and the framing that predicted five of the
six seam defects came out of saying it.

None of that is structurally forced. A successor copying the four-vector shape
without it gets four reviewers agreeing with each other.

**But "be candid" is a virtue, and virtues do not survive a deadline.** The
version worth copying has a mechanism attached: *"my fix caused this" was the
framing that predicted where the next defect would be* — six defects at one
seam, five of them anticipated by it. (Five is what was verified; the sixth was
not checked, and is not rounded up here.) A successor cannot be exhorted into
candour, but can adopt a framing that pays for itself.

The general form, contributed by the ops-safety reviewer and one level up from
§5c: **prefer the version of a virtue that has a mechanism attached.** §5c says
an executable artefact starts working immediately while a principle does not
work at all; this is the same claim about dispositions rather than checks.

And the practice that made the disclosures *possible* is worth copying more
than the disposition: every reviewer reproduction was **kept, namespaced, and
re-runnable across every revision of this branch**. That is why a reviewer could re-check their own
record in four minutes instead of rebuilding a harness — and a reviewer who
discards their scripts cannot audit themselves even if they want to. The
willingness costs nothing if the evidence is gone.

*(closing observations contributed by the ops-safety reviewer)*
