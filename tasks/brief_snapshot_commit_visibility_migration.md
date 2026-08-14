# Two-vector migration brief — snapshot commit visibility (B1-residual)

For the PR body of `fix/snapshot-commit-visibility`. Migration-bearing, so per
the approvals discipline the merge ask carries fresh-install / upgrade-with-data
/ rollback.

## The defect being closed

`source_call_price_snapshots.created_at` is stamped at INSERT, but the writer
commits a whole cycle at once. A row inserted before an `as_of` and committed
after it therefore satisfies **both** existing knowability bounds while having
been genuinely unknowable at `as_of`. The first-inserted rows of a long cycle
approach full-cycle exposure. That is **future leakage**: a historical feature
can read a price the decision could not have seen.

## The migration

`source_call_snapshot_batches_v1`, schema_version **20260813**:

1. New table `source_call_snapshot_batches (batch_id PK, visible_at,
   rows_written, created_at)` + index on `visible_at`.
2. New table `source_call_snapshot_visibility_epoch (id=1, epoch_cutover_ts)`,
   seeded with the migration timestamp.
3. Additive nullable column `source_call_price_snapshots.batch_id` + index.

No backfill. No rewrite of existing rows. No change to any existing column.

## The rule

A snapshot is knowable as-of `T` iff its ordinary bounds hold **and**:

- `batch_id IS NOT NULL` and its batch row exists with `visible_at <= T`; **or**
- `batch_id IS NULL` and `created_at <= epoch_cutover_ts` (pre-mechanism row).

Both halves matter. The first closes the leak. The second is the epoch rule —
and its `created_at <= epoch_cutover_ts` clause is what stops it degenerating:
a NULL-batch row created *after* the cutover is a writer bug, and the
conservative reading of a bug is **invisible**, not always-visible. Without that
clause any future stamping regression silently restores the original leak.

The two bounds differ in strictness **and lean the same way**:
`visible_at <= T` is inclusive (a marker committed exactly at `T` was knowable
then); `created_at < epoch_cutover_ts` is exclusive because `datetime('now')` has
one-second resolution, so a row written in the same second as the migration
compares EQUAL to the cutover and `<=` would grandfather it — the admitting
direction. Both are pinned by test.

Both comparison pairs also share a single timestamp producer, which is the rule
that B-2 violated: `visible_at`/`as_of` are both Python `.isoformat()`;
`created_at`/`epoch_cutover_ts` are both SQLite `datetime('now')`. Mixing them
inverts a bound silently, because ' ' (0x20) sorts before 'T' (0x54).

## Vector 1 — fresh install

All three objects are created by `initialize()`; the epoch row is seeded with
that moment. Every row the writer produces from then on carries a `batch_id`,
and each cycle publishes its marker after its data commit. There are no
pre-epoch rows, so the grandfather branch is inert.

## Vector 2 — upgrade with data

`ADD COLUMN` with no default is O(1) metadata-only — it does not rewrite the
6.8 GB file and takes no long write lock. Every existing row keeps
`batch_id IS NULL` and `created_at` earlier than the epoch stamped at migration
time, so **all existing history stays readable** through the grandfather branch.
Production currently holds 865 contract rows plus the new CG rows; none are
stranded.

The first post-migration cycle allocates `batch_id = 1` and publishes normally.

## Vector 3 — rollback

Rolling the code back while the schema stays is safe:

- Old readers never reference `batch_id` or either new table; the ledger's
  pre-change query has no `as_of` parameter and selects an explicit column list.
- Old writers `INSERT` without `batch_id`, which is nullable — valid.
- Rows written by the rolled-back writer land NULL-batch **after** the epoch, so
  when the code rolls forward they are **invisible** to as-of readers. This is
  the conservative direction and is the intended behaviour, but it is a real
  consequence: a rollback window produces a hole in as-of visibility that a
  repair (below) can close.

No `DROP COLUMN` or `DROP TABLE` is needed. Both are rewrites on a live 6.8 GB
database, and the objects are inert to old code.

## Crash and recovery story

A crash between the data commit and the marker commit leaves rows with a
`batch_id` and no batch row. Those rows are **invisible to as-of readers, and
remain so** until repaired. Invisible is the accepted outcome — the alternative
(assume visible) is the leak.

They are *not lost*: the data is durable and appears in coverage reads. Recovery
is a single insert per orphaned batch:

```sql
-- USE THIS ONE. `?` is bound from Python: datetime.now(timezone.utc).isoformat()
-- — the same producer as every other `visible_at`, and REPAIR TIME rather than
-- row time. The rows genuinely became knowable when the repair published them.
INSERT INTO source_call_snapshot_batches (batch_id, visible_at, rows_written)
SELECT s.batch_id, ?, COUNT(*)
FROM source_call_price_snapshots s
LEFT JOIN source_call_snapshot_batches b ON b.batch_id = s.batch_id
WHERE s.batch_id IS NOT NULL AND b.batch_id IS NULL
GROUP BY s.batch_id;
```

Batch ids are allocated as `MAX(stamped, published) + 1`, so a crashed cycle's
id is **never reused** — reusing it would retroactively publish the orphan rows
under a later cycle's marker, which is the early-visibility this mechanism
exists to prevent. Pinned by test.

### Two repair variants that LEAK — do not copy these

Recorded because both look reasonable and each was in an earlier draft of this
document:

- `visible_at = MAX(s.created_at)` **backdates** visibility to before the batch
  was ever committed, reintroducing the exact future leakage this mechanism
  closes — through the repair path.
- `visible_at = datetime('now')` is SQLite-shaped while every other `visible_at`
  is Python `.isoformat()`. Compared against a `.isoformat()` `as_of`, the
  space separator sorts before `'T'`, so the repaired marker reads as EARLIER
  than it is and admits rows sooner — measured ~4.8h early.

## Concurrency

Two overlapping cycles allocate the same batch id (allocation reads
`MAX(batch_id)` before either publishes). Two defences, both required:

- the `.sh` wrapper takes an **flock** (house precedent:
  `gecko-backup-create.sh`, `cron-drift-watchdog.sh`), so a slow cycle is
  skipped rather than run concurrently;
- `_publish_batch` is a **plain INSERT**, so a collision that gets past the lock
  RAISES. `INSERT OR IGNORE` silently discarded the second marker, leaving the
  later cycle's rows published under the earlier cycle's earlier `visible_at` —
  backdated visibility, the leak wearing a different hat.

## §12a / watchdog implications

A writer that commits data but persistently fails to publish markers is a silent
failure: as-of coverage decays to zero while every existing coverage metric
looks healthy, because coverage readers are deliberately **ungated**.

This PR does not add a watchdog — it makes the condition *detectable* and states
the check the ops lane should adopt:

```sql
SELECT COUNT(*) FROM source_call_price_snapshots s
LEFT JOIN source_call_snapshot_batches b ON b.batch_id = s.batch_id
WHERE s.batch_id IS NOT NULL AND b.batch_id IS NULL;
```

Non-zero and *not shrinking* across consecutive runs means markers are failing.
Recorded as a follow-up rather than built here, because the writer is not
activated for the CG lane yet and a watchdog with no traffic is untestable.

## Reader split — stated at the helper, not only here

- **Decision / as-of readers** **must** gate. Both are adopted IN THIS PR:
  `ledger._fetch_snapshot_rows` (with `as_of`) and the Stage B caller-feature
  provider `caller_features._load_observations`. The provider's adoption is here
  rather than in a later PR because `VISIBLE_AS_OF_SQL` exists only on this
  branch — merging the marker without it would ship the infrastructure while its
  primary consumer kept the exact residual the marker supersedes.
- **Coverage / observability readers** (`source_quality/watchdogs.py`) **must
  not** — gating them makes coverage under-report during the marker-lag window
  and pages an operator about a lane that is working correctly.

The rule is documented at `VISIBLE_AS_OF_SQL` in `snapshot_writer.py`, where the
next reader author will look, not only in this brief.

## Production-behaviour note

This PR is **not merely additive schema**. Gating `ledger._fetch_snapshot_rows`
changes the live source-call outcome lane: some rows become visible one refresh
later than before.

**Correction (review round 2).** An earlier version of this section claimed that
change while it was NOT actually occurring: the live call site passed
`"contract"` as the third positional argument, which bound `identity_kind` and
left `as_of` at its `None` default, so the predicate was skipped in production
entirely and the lane kept pricing from unpublished batches. The call site now
passes keyword arguments and an explicit `as_of`, and a lane-level test drives
`refresh_source_call_outcomes` to prove an unpublished batch cannot price a
call. The behaviour change described here is real as of that fix. The change is conservative and self-healing — late-visible
rows are picked up on the next outcome refresh, and the maturity states already
absorb that lag — but it is production-behaviour-relevant, which is a further
reason the PR stays merge-held.
