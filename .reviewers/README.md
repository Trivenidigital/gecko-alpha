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

## What this does NOT solve

Per-PR files fix clearance **lifecycle and isolation**. They do not establish
reviewer independence: these records are still author-writable, so the gate
remains a lapse *detector*. Branch protection with required checks, and a
reviewer record the PR author cannot manufacture, are separate controls that do
not exist yet. See tickets 13(d) and 13(e).
