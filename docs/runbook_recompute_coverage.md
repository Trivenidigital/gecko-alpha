# Runbook — legacy chains-provenance overlay (`chain_identity_recompute_v1`)

## What this protects

Pre-cutover "chains" detections had their lead times derived by PREFIX matching
on token symbols. Ruling C decided that prefix similarity is not identity, so
those leads can no longer be trusted as-is. Dropping them outright is the
*naive cutover*: on the production population it takes gainers `tier_high` from
**341 to 187**.

The overlay re-verifies each archived row against canonical identity and
records the verdict in a versioned table. Archived rows are never rewritten —
the overlay sits beside them. With the overlay populated, `tier_high` is **315**:
128 of the 154 high-tier rows the naive cutover would destroy are recovered,
and the residual 26 are rows whose provenance genuinely cannot be reconstructed
plus 4 demonstrated prefix-only fabrications.

## The failure mode this runbook exists for

**The overlay has no runtime writer.** It is filled by an offline ops step. A
deploy that ships the code and skips that step leaves it empty, and an empty
overlay is not neutral — every pre-cutover chains detection fails the trust
check and loses its credit. The metric collapses to the 187 number, silently,
with every log green. It looks exactly like a real decline in detection quality.

A fully-populated overlay can fail the same way. Only `verified_canonical`
earns credit, and reconstruction depends on preserved `/root` snapshots that
are being deleted over time. Run the backfill after they are gone and roughly
four rows in five land `indeterminate_history` — overlay full, credit zero,
same collapse. **Never judge this by row count.**

## Activation

```bash
cd /root/gecko-alpha
.venv/bin/python -m scripts.backfill_chain_identity_recompute --dry-run   # reports, writes nothing
.venv/bin/python -m scripts.backfill_chain_identity_recompute --apply
```

Exit codes: `0` credit recovered · `2` schema not deployed (deploy first) ·
`3` ran but recovered nothing — **do not treat as success**, check that the
history snapshots under `/root` still exist.

Re-runnable. Keyed on `(source_table, source_row_id)`, so a second run
overwrites its own rows and nothing else.

## Deploy watchpoint 1 — the PK rebuild may not run at all

`_migrate_chain_identity_recompute_pk_v2` rebuilds the overlay's PRIMARY KEY to
include `semantics_version`. **It only rebuilds on a database that already
carries the earlier shape.** A database with no `chain_identity_recompute_v1`
takes the fresh-install path: `CREATE TABLE IF NOT EXISTS` already emits the v2
shape, and the migration returns after stamping.

The two paths stamp **differently**, and this has already misled once:

| path | `paper_migrations` | `schema_version` 20260826 |
|---|---|---|
| rebuild (upgrade) | stamped | **stamped** |
| fresh install | stamped | **absent** |

So on a first deploy there is **no `schema_version = 20260826` row**, and
looking for one reports a failure that has not happened. Verify the **shape**,
not the stamp:

```bash
sqlite3 scout.db "SELECT sql FROM sqlite_master WHERE name='chain_identity_recompute_v1';"   | grep -c 'PRIMARY KEY (source_table, source_row_id, semantics_version)'   # expect 1
sqlite3 scout.db "SELECT 1 FROM paper_migrations WHERE name='chain_identity_recompute_pk_v2';"
```

`schema_version` is therefore **not** a reliable indicator that the v2 shape is
present. Tracked as BL-NEW-RECOMPUTE-PROBE-OBSERVABILITY-RESIDUALS.

On a database that *does* rebuild, the first production run is also the first
concurrent-startup exercise of the archive CTAS. Verified under injected
failure is not the same as having run once in production. Watch that the
migration completes before any other process's `initialize()` reaches it, and
that no `chain_identity_recompute_v1_pk2` orphan survives — `BEGIN EXCLUSIVE`
makes the scratch `CREATE` part of the transaction so a rollback removes it,
and `DROP TABLE IF EXISTS` recovers a database already wedged by an older
build. This is the same shape as the incident where a 2-minute cron migrated
the live DB and produced 74 `database is locked` errors: readers must open
read-only **before** `initialize()`.

## Deploy watchpoint 2 — the migration→backfill zero-coverage interval

**Between the migration and the completion of the backfill, coverage is `0.0`.
That is a deployment phase, not normal runtime.**

In that interval the mark is `0.0`, which makes `rate < best * _COLLAPSE_FRACTION`
unsatisfiable for every rate — so **`collapsed` cannot fire, by construction.**
`dark_surfaces` is the *only* observable, and it is load-bearing. Silencing it,
or letting alert dedup/cooldown fold it into background noise, converts a
temporary deployment state into an invisible permanent one.

**The backfill is part of the deployment, not a later optional operation.** Do
not leave the system sitting at `0.0`. Minimise the interval and timestamp it:

```
migration_complete → backfill_start → first_nonzero_coverage → backfill_complete
```

A worked interval, from the 2026-08-24 production deploy of `cdbb8475`:

```
migration_complete    2026-08-23T23:54:37.555Z
backfill_start        2026-08-23T23:57:06.892Z
backfill_complete     2026-08-23T23:57:31.699Z      (2m54s total)
```

**Note what that window does *not* prove.** The probe runs inside
`_run_hourly_maintenance` — hourly. A window shorter than the probe period
contains no probe tick, so `dark_surfaces` never gets an opportunity to fire.
Its absence there is a consequence of minimising the window as instructed, and
is **not** evidence the observable works. Do not record "dark_surfaces behaved
correctly" on the strength of a window it never sampled.

Because the probe is hourly, a freshly-backfilled deploy can otherwise sit
un-armed for up to an hour. Arm it deliberately rather than waiting — the same
call the scheduler makes:

```python
cov = await db.chain_identity_recompute_coverage_probe(gate_minutes=1440.0)
```

`collapsed` becomes reachable only once that baseline row exists. That ordering
is correct and worth confirming rather than assuming.

## Monitoring (§12a)

Two layers, because neither is sufficient alone:

| Layer | Where | Fires when |
|---|---|---|
| In-process probe | hourly maintenance, logs `chain_identity_recompute_coverage` every pass | escalates `chain_identity_recompute_NOT_RECOVERING` |
| Watchdog | `scripts/recompute-coverage-watchdog.sh` (timer) | exit 1 → Telegram |

The probe alone is not enough: nothing on this box reads journald, so a
`logger.error` there is operationally silence. The watchdog alone is not
enough either: it samples on a timer and carries no per-surface detail.

Both escalate on **recovered credit**, never on row count, and on two distinct
conditions:

| condition | meaning |
|---|---|
| `dark_surfaces` | a surface with a population recovered **nothing** — a cliff: the backfill was never run, or ran with no history |
| `collapsed_surfaces` | a surface fell below half its recorded high-water recovery **rate** — history was deleted underneath it |

The second exists because the first only fires at exactly zero, and the
degradation this system actually expects is not zero. Delete the `/root`
snapshots, re-run the backfill, and roughly four rows in five land
indeterminate — 20%, not 0% — so a fall from the measured ~53% baseline to 5%
would page on neither. That is 95% of readers going blind under a green alarm.

The high-water mark is a **ratchet**: recorded from the first observation of a
surface, raised when recovery improves, never lowered — enforced by the write
itself (`best_rate = MAX(stored, new)`), not by the caller. The one exception is
a mark recorded against a population below twice the judging floor: measured on
a handful of rows it is not comparable to a full one, so it is **deleted** and
re-established. A deliberate re-arm is a delete; nothing else can lower a mark.
Note that this covers a transiently small FIRST observation, not population
shrinkage; see the residuals in the acceptance report. Ordinary attrition rides
underneath it (archived rows draining to `canonical_v1` shrink population and
recovered together, so the *rate* holds), and a collapse crosses it. Surfaces
with fewer than 20 credit-bearing rows are not judged on rate at all — one row
moves it too far to distinguish a cliff from noise.

To reset after a deliberate change that lowers recovery:

```sql
DELETE FROM recompute_coverage_baseline WHERE source_table = '<surface>';
```

Neither condition pages on partial coverage per se — history genuinely runs out
for older anchors, and an alarm that fires on the steady state gets muted before
the day it matters.

### Install — and arm it

A watchdog nothing schedules is deploy-without-activate one level up, which is
the failure class this whole subsystem exists to close. The timer ships in the
repo for that reason.

```bash
chmod +x scripts/recompute-coverage-watchdog.sh
install -m 0644 scripts/recompute-coverage-watchdog.{timer,service} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now recompute-coverage-watchdog.timer
systemctl list-timers recompute-coverage-watchdog.timer   # VERIFY it is armed
```

The unit runs the script **from the repo**, deliberately. Installing the `.sh`
to `/usr/local/bin` while it invokes the `.py` from the repo splits the
watchdog across two deploy paths — the python half updates on `git pull`, the
shell half needs a re-`install` and silently does not deploy. That trap is
already in this project's history with the backup scripts. Only the unit files
are installed, and those change rarely.

### Exit contract

Only exit 1 notifies. Everything else is a failure **of the watchdog**, which
nothing else reports — so they are distinct codes rather than a shared one.
That distinctness is asserted by test: 6 and 7 were the same code until it
caught them.

| code | meaning | Telegram |
|---:|---|:--:|
| 0 | healthy, or nothing to recover | no |
| 1 | **alarm** — a surface is recovering nothing | yes |
| 2 | the check could not run (unreadable DB) | no |
| 3 | `.env` missing | no |
| 4 | Telegram credentials missing — the alarm fired and could not be delivered | no |
| 5 | `APP_DIR` invalid | no |
| 6 | interpreter missing | no |
| 7 | the app could not be imported to read the gate | no |

Codes 5 and 6 exist because `cd "$APP_DIR"` under `set -euo pipefail` exits
**1** — the same code the alarm path uses after a successful send. A dead
watchdog and a firing watchdog were indistinguishable to anything downstream.

The gate is read from the application, never parsed out of `.env`: first from
`Settings` (so a `.env` override is honoured), then from the field default in
`scout/config.py` if `Settings` cannot be built. There is deliberately **no**
literal fallback in the shell — a threshold written here would drift from the
one the readers use, which is the divergence the whole gate re-check exists to
remove. If neither form answers, the watchdog exits 6 rather than measuring
against a guess.

The systemd unit sets `SuccessExitStatus=0 1`, so an alarm is not a service
failure but 2–6 surface in `systemctl status`.

## Reading the status breakdown

| Status | Meaning | Earns credit |
|---|---|---|
| `verified_canonical` | canonical identity confirms the detection | **yes** |
| `canonical_below_gate_indeterminate` | canonical match, reconstructed lead under the gate — *not* a proven negative (left-censoring) | no |
| `alias_tier_not_verifiable` | matched only on symbol equality; the TIER cannot be verified under censored history, whatever the lead | no |
| `indeterminate_history` | the anchor falls in a coverage gap; absence of a match is not evidence of absence | no |
| `verified_prefix_only` | demonstrated prefix collision — the lead was fabricated | no |
| `no_legacy_credit` | never held chains credit; outside the population | n/a |
| `unjoinable_row` | archived row with no coin_id or no anchor | n/a |

Statuses always sum to the **replayed** population — archived rows minus those
already stamped `canonical_v1`, which have no legacy provenance to recompute.
`reconciliation_report` emits `population`, `skipped_canonical`, `replayed` and
per-surface `stored` so the numbers can be checked rather than assumed. If they
do not reconcile, a row was dropped rather than classified — a defect, not a
rounding artefact.

## When the backfill cannot help

The alert reports `unarchivable` separately. Those are rows written **after**
the archives were taken: the archive step self-guards and never re-runs, the
stamp step runs every startup, and the backfill only reads archives. Re-running
`--apply` will not clear them.

If `credit_recovered` is 0 and `unarchivable` equals the population, the page is
not telling you to run the backfill — it is telling you the population has
drained to rows that predate no archive. That is a design limit, not an
incident.
