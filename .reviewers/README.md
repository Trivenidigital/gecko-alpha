# Per-PR reviewer clearance records

One file per PR: `.reviewers/<PR-number>.toml`. The PR under evaluation owns
its own record, and **master never carries live cross-PR clearance state.**

## Why per-PR, and not one shared table

The previous design kept a single `.reviewers.toml` at the repo root. Because
this repository squash-merges, a cleared PR's clearance SHAs landed on master
and immediately stopped being ancestors of anything branched from it — so every
subsequent branch reported `IS NOT AN ANCESTOR` on work that had nothing to do
with them, and a post-merge "reset master's `[clearances]`" step was required
after every cleared merge.

That step was not merely tedious. The guard that detects a forgotten reset reads
**master as committed**, so once master carried stale clearances *every* branch
went red — including the branch that removed them. The remedy was unmergeable
through the ordinary path, and breaking the deadlock took a one-time authorized
direct push to master (`87604f44`). See backlog tickets 21 and 25.

Per-PR records remove the state that made that possible. There is nothing on
master to go stale, so there is nothing to reset and nothing to block.

## Files here are EVIDENCE, not active state

A record for a merged PR is inert. The gate reads exactly one file — the one
matching the PR it was told to evaluate — and never consults any other. Old
records are kept as an audit trail of which vectors cleared which revision.

## Ownership is not the filename

Each record declares its own `pr = <number>`, cross-checked against the identity
the gate resolved from trusted metadata. A file copied or renamed from another
PR keeps its `pr` field and is rejected. **The HEAD branch name is never
consulted** — it is author-controlled and travels with the tree, so inferring
ownership from it would let any branch claim any PR's clearances.

## Record schema

```toml
pr = 564                      # REQUIRED. Must match the PR being evaluated.
record_version = 1            # optional; see below
required = ["concurrency", "logic", "ops-safety", "silent-failure"]
watch = [
    "scout", "scripts", "tests", ".github", "dashboard", "cron", "ops",
    "systemd", ".claude", "pyproject.toml", "uv.lock", "Dockerfile",
    "docker-compose.yml", "start.sh",
]

[clearances]
concurrency    = "<40-hex sha the concurrency reviewer named>"
logic          = "<40-hex sha>"
ops-safety     = "<40-hex sha>"
silent-failure = "<40-hex sha>"
```

`required` and `watch` are **floors, not choices** — a record may add to them
and may not narrow them. The gate prints what is missing if you do.

**The record must be COMMITTED.** It is read out of the revision under review,
not off disk, so an uncommitted record does not clear anything — a green has to
be reproducible from the SHA alone.

**A PR may only touch its own record.** Editing, renaming or deleting another
PR's record is refused, including on an otherwise docs-only PR.

### `record_version` may be omitted

**Missing means 1, permanently and by decision.** A reader years from now can
rely on this: an absent field means version 1, whether the record predates
versioning or the writer simply omitted an optional field. Both mean 1.

(An earlier draft said "never that the writer forgot". That asserted provenance
nothing enforces — once the field is optional, an author may omit it for any
reason. The rule is unchanged; the unenforceable claim is gone.)

Requiring it was considered and declined 3-1. Requiring it *today* would buy
nothing — the case a version marker exists for (a v2 record read by a v1 gate)
is prevented by the **v2 writer** declaring, which a v2 migration enforces at
that time. Today's requirement would only distinguish "written before
versioning" from "written after versioning, v1", and both mean 1.

An *unsupported* version is a hard error: strict on the open future, permissive
on the closed past.

## What this does NOT solve

Per-PR files fix clearance **lifecycle and isolation**. They do not establish
reviewer independence: these records are still author-writable, so the gate
remains a lapse *detector*.

Of the two controls that would change that, **one now exists.** Branch
protection with `test` and `reviewer-clearances` required was enabled
2026-08-28 (ticket 13(d), closed), so a failing check blocks a merge. **A
reviewer record the PR author cannot manufacture still does not exist**
(13(e), open) -- and that is now the whole of the gap. A green check proves a
record exists naming ancestor SHAs with no watched delta since. It does not
prove anyone other than the author put them there.
