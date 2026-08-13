# Two-vector migration brief — `reversal_alert_pending_json`

For the PR body of `fix/reversal-alert-durable-retry`. This PR is
migration-bearing, so per the approvals discipline the merge ask carries a
fresh-install / upgrade-with-data / rollback brief.

## The migration

One column, additive and nullable, on `combo_performance`:

```sql
ALTER TABLE combo_performance ADD COLUMN reversal_alert_pending_json TEXT
```

Added to the `CREATE TABLE IF NOT EXISTS` body **and** as a guarded `ALTER`
behind a `PRAGMA table_info` column check, exactly mirroring its two siblings
`perm_suppression_alerted_at` (#424) and `retest_incomplete_alerted_at` (#523).
No other schema change. No index. No backfill. No data rewrite.

**Shape:** JSON text, `{"transition": ..., "detected_at": ..., "message": ...}`.
The rendered page body is stored so a retry re-sends the page describing the
transition that actually happened, rather than one re-derived from state that
has since moved on.

**Polarity note.** The two sibling columns record "already alerted". This one
records "still owed" — the inverted polarity is deliberate and is what makes the
retry possible. NULL means nothing is pending, which is why NULL is also the
correct value for every pre-existing row.

### Why there is no `schema_version` row

`docs/migration_versions.md` is "the authoritative registry of every
`schema_version` integer **allocated by a migration**", and
`tests/test_schema_version_uniqueness.py` enforces write-site → doc-row. This
migration is the `#424`-style **bare-additive** shape, which the allocation doc
explicitly says to prefer; it stamps no `schema_version`, exactly like both
sibling columns, neither of which has a registry row. Adding a row here would
record an allocation that no code performs. See the deviation note in the PR
report.

## Vector 1 — fresh install

`CREATE TABLE IF NOT EXISTS combo_performance (...)` includes the column, so a
new database has it from the first `initialize()`. The guarded `ALTER` is then a
no-op because the PRAGMA check finds the column present.

Every row starts with the column NULL. The delivery pass selects
`WHERE reversal_alert_pending_json IS NOT NULL`, so a fresh install has an empty
pending set and pages only on transitions it actually detects. No cold-start
page storm.

Covered by every test in the enumeration and reversal suites, which all build
their DB via `Database(tmp_path).initialize()`.

## Vector 2 — upgrade with data

The `ALTER` runs once against the existing table. SQLite `ADD COLUMN` with no
`NOT NULL` and no default is O(1) metadata-only — it does not rewrite the 6.8 GB
file and does not hold a long write lock.

Existing rows land at **NULL = "no pending alert"**, which is the correct
pre-cutover state: pages that were lost before this PR existed are genuinely
lost and cannot be reconstructed, so the upgrade must not invent them. The
system starts owing nothing and begins recording from the first refresh after
deploy.

Consequence worth stating plainly: this PR does **not** recover the pages
already missed. It stops the next one from being missed.

Pinned by `test_upgrade_adds_pending_reversal_column_to_existing_table`, which
builds the OLD table shape explicitly (a fresh `tmp_path` DB is created
already-migrated and would never exercise the `ALTER`), inserts a row, migrates,
and asserts the column landed, the pre-existing data and suppression state
survived, and the new column reads NULL.

### Interaction with the 30d UPSERT

`_refresh_combo_locked` rewrites the 30d row on every refresh via
`ON CONFLICT ... DO UPDATE SET` with an explicit column list. That list does not
include `reversal_alert_pending_json`, so a pending page survives every
subsequent refresh — which is precisely what makes the retry work. This is not
incidental: `test_repeated_failures_reattempt_every_refresh_without_duplicating`
runs three full refreshes against a pending marker and would fail the moment
anyone added the column to that `SET` list.

## Vector 3 — rollback

Rolling the code back to any commit at or before `895e3663` while the column
remains in the database is **safe**:

- Old code never references `reversal_alert_pending_json`. Every read is an
  explicit column list; there is no `SELECT *` against `combo_performance` in
  the reversal path.
- The 30d UPSERT names its columns explicitly, so old code writing the row
  leaves the extra column untouched rather than erroring on arity.
- The column is nullable with no default, so old `INSERT` statements that omit
  it remain valid.

Behavioural effect of a rollback: any page still pending at that moment stops
being retried and becomes dormant data in the column. It is not lost from the
database, and rolling forward again resumes retrying it — the delivery pass
simply selects it once more. Nothing needs to be cleaned up by hand.

No `DROP COLUMN` is required or recommended. SQLite's `DROP COLUMN` (3.35+)
rewrites the table, which on a live 6.8 GB database is a far larger operation
than leaving one nullable column in place.

## Limits of the durability guarantee

Added after independent adversarial review. These are the edges of what the
column actually promises, stated so nobody reads it as stronger than it is.

**At most ONE outstanding page per combo.** The marker is a single column, and a
newer transition overwrites an undelivered older one (latest-wins). If a combo
transitions twice while delivery is failing, only the second page is ever sent;
the first survives solely as the `suppression_reversal_alert_superseded`
structured log, which carries its transition, detection time, rendered body and
raw bytes. This is a deliberate simplicity trade, not an oversight — a queue
would page about states that no longer hold.

**The flip→record crash window is narrowed, not closed.** The suppression flip
commits inside `_refresh_combo_locked`; the pending page is recorded afterwards,
in `_process_suppression_reversals`. A process death between those two commits
still loses the page permanently, exactly as before this PR — the reversal is
diffed across one refresh and is never re-detected. What changed is the size of
the window: it used to span the entire Telegram round-trip (seconds, and every
network failure mode), and now spans only the gap between two local commits.
Closing it completely requires recording the pending page in the same
transaction as the flip, which is a larger change to the refresh transaction
boundary. **Ticketed as a follow-up; not attempted here.**

**Roll-forward after a rollback resumes delivering possibly-stale pages.** Pages
left pending while the code was rolled back are still pending when it rolls
forward, and will then be delivered — potentially describing a suppression that
has since been cleared. Mitigated, not eliminated, by the R1 detection stamp:
any page not first attempted on the current run is delivered with
`[detected <iso>]` appended, so the operator can see the body describes a state
observed at that time rather than now.

## Blast radius summary

| Vector | Effect | Verified by |
|---|---|---|
| Fresh install | Column present, all NULL, empty pending set | full reversal + enumeration suites |
| Upgrade with data | Metadata-only ALTER; existing rows NULL; nothing invented | `test_upgrade_adds_pending_reversal_column_to_existing_table` |
| Rollback | Column ignored by old code; pending pages dormant, not lost | explicit-column-list audit above |
| Steady state | Pending survives the 30d UPSERT; retried until delivered | `test_repeated_failures_reattempt_every_refresh_without_duplicating` |
