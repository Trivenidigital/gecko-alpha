# Merge report — PR #559, versioned legacy-provenance recomputation

**Candidate:** `89b070ef` · **Base:** `master` · 31 commits, 35 files,
12 new test files.

Every cleared item below records **the SHA it was measured on**. That is not
bookkeeping: across nine revisions, four reviewer clearances were invalidated by
changes that were themselves fixes for other findings, and none of those
invalidations was visible from the diff that caused them. "A clearance is a
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
| exact-head CI | green at `33f9de54`; `f26c412f` running at time of writing | first green after the wrapper tests landed |
| full production replay | 9 revisions, stable | `c1a50e33` … `f26c412f` |
| totals reconcile | 2,891 statuses = 2,891 written = population | `reconciliation_report` |
| independent reviewers | 4 dispatched, all reached terminal state and re-verified | see §4 |

**Replay result** (unchanged across all nine revisions except one deliberate
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

| vector | last verdict | measured on |
|---|---|---|
| structural / concurrency | **CLEAN, terminal** — regression re-run in full, 4/4 mutants | `97365248` |
| silent failure / observability | **CLEAN, terminal** — 6/6 mutants, two verified as intended-assertion kills | `b90f66fa` |
| ops safety / evidence integrity | CLEAN — lapsed, production code moved since | `1076f56d` |
| recompute logic | classification CLEAN, re-executed — lapsed, production code moved since | `89b070ef` |

Two clearances **hold at HEAD** (production code unchanged since, verified by
`git diff --name-only <sha>..HEAD`). Two **lapsed** under the rule above and were
re-requested rather than carried forward.
| silent failure / observability | S1–S4, all fixed | `f2847774` |
| ops safety / evidence integrity | N-1…N-6 + R1–R5, all fixed | `c8b5e009` |
| recompute logic | nothing blocking; R1 implemented rather than deferred | `47847f47` |

Final verdicts on `f26c412f` requested from all four; merge is blocked until
each returns terminal.

## 5. Open residuals, carried deliberately

| residual | why not closed |
|---|---|
| Coverage is a global span, not per-token | A design change. Every outcome it produces is conservative now that `alias_unique` cannot promote. |
| Ratchet's population guard is one-sided | It re-establishes a mark recorded against a transiently small population. It does **not** cover shrinkage — see the row below. Written as a ratio it silently *lowered* the mark on ordinary growth (0.60 → 0.36 measured), which is the defect R2 caught. |
| Ratchet has no downward re-calibration | The surviving population is not a random sample — rows that never re-appear skew toward old anchors that resolve indeterminate, so the rate over a shrinking remainder can fall benignly. `population` is recorded, so the hook exists; the composition cannot be measured before a post-deploy population exists. |
| `coverage_intervals=None` falls back to a global floor | Latent: no runtime caller outside the ops script, which always passes explicit intervals. |
| Post-archive rows can never be covered | Named in the runbook and counted as `unarchivable` in both the payload and the alert text. |

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

**A clearance is a property of a revision, not of a component.** Four reviewer
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
