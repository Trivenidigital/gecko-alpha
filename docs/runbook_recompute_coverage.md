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

Re-runnable. Keyed on `(source_table, source_row_id, semantics_version)` --
THREE columns, not two. Re-running the SAME semantics version replaces its
own rows, so the backfill is idempotent; bumping `RECOMPUTE_SEMANTICS` and
re-running ACCUMULATES a new generation beside the old one rather than
overwriting it. That is the point of a versioned derived store, and it is
why the primary key was rebuilt -- see Deploy watchpoint 1. (This line said
two columns until 2026-08-23; it predated the rebuild and was contradicted
by the watchpoint added directly above it.)

## Operator actions the reviewer-lapse check CANNOT do for you

`scripts/check_reviewer_clearances.py` is a **lapse detector**, not an
enforcement gate, and the difference is not a caveat — it is the whole security
model. Two things are required to make an unreviewed merge actually impossible,
and **neither can be done from inside the repository**:

**1. Branch protection is OFF.** Verified:

```bash
gh api repos/Trivenidigital/gecko-alpha/branches/master/protection   # 404 Branch not protected
gh api repos/Trivenidigital/gecko-alpha/rulesets                     # []
```

With no protection, `mergeStateStatus` reads `UNSTABLE` (checks red, merge
permitted) rather than `BLOCKED` — so **no CI check in this repo can block
anything.** PR #560 was mergeable while its own clearance check was failing.
Until an operator enables protection with the checks marked required, every
gate here is advisory text in a log.

**2. The clearance record sits on the author's writable surface.**
`.reviewers.toml` is committed, so repointing all four clearances at the head
is a four-line edit that turns the check green — and it makes the tree
comparison vacuous, because the clearance tree is then compared against itself.

The usual fix is to read approvals from the GitHub API, where they are bound to
a commit and cannot be written by the author. **That does not work here yet:**
this project has *zero* GitHub review approvals on every recent PR (`gh api
repos/.../pulls/N/reviews` → `0` for #555, #557, #558, #559). Its independent
reviewers are agents whose verdicts live in session transcripts. Until review
verdicts are recorded somewhere the author cannot write, the record is
self-attested by construction.

**What the check does buy**, and why it still ships: it catches the *accidental*
lapse — a clearance recorded, production moved underneath it, nobody noticed.
That is exactly what happened on #559, where two clearances were carried in a
report as still holding after `scout/db.py` had moved. It does not defend
against a motivated author, and it does not claim to.

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
sqlite3 "file:scout.db?mode=ro" "SELECT sql FROM sqlite_master WHERE name='chain_identity_recompute_v1';"   | grep -c 'PRIMARY KEY (source_table, source_row_id, semantics_version)'   # expect 1
sqlite3 "file:scout.db?mode=ro" "SELECT 1 FROM paper_migrations WHERE name='chain_identity_recompute_pk_v2';"
```

**`mode=ro`, not bare and not `immutable=1`.** Bare opens read-write, and the
operator most likely to run a verification command is the one diagnosing --
which is exactly when a tool gets pointed at a copy. Against a BACKUP copy use
`immutable=1` instead: sidecars written beside a backup are the 2026-08-15
incident that destroyed every real backup.

Do **not** use `immutable=1` against the live database here. It ignores the
`-wal`, so it would hide everything committed-but-not-checkpointed and print
the most reassuring possible output -- the stale all-clear that
`scripts/check_recompute_coverage.py` exists to prevent.

`schema_version` is therefore **not** a reliable indicator that the v2 shape is
present. Tracked as BL-NEW-RECOMPUTE-PROBE-OBSERVABILITY-RESIDUALS.

**The fresh-install path is also SILENT.** It returns before the completion
log, so it emits neither a `schema_version` row nor a
`pk_v2_migration_complete` journald line. An operator verifying a fresh deploy
by grepping journald finds nothing and concludes the migration did not run --
the identical false failure the missing stamp produces, one layer down. Verify
the shape; the log line is a second stamp with the same failure mode.

On a database that *does* rebuild, the first production run is also the first
concurrent-startup exercise of the archive CTAS. Verified under injected
failure is not the same as having run once in production -- and as of
`cdbb8475` (2026-08-23) that is live status, not generic caution: **the rebuild
branch has never executed in production.** Prod took the fresh-install path,
confirmed on the box by the absence of `pk_v2_migration_complete` from journald
entirely. Whoever triggers the rebuild will be the first to run it for real. Watch that the
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

A worked interval, from the production deploy of `cdbb8475` on
**2026-08-23** (UTC — the session that ran it had already ticked over locally,
and an earlier version of this line said 08-24):

| stamp | value | provenance |
|---|---|---|
| `migration_complete` | `2026-08-23T23:54:37.557033Z` | journald, `chain_identity_recompute_v1_migration_complete` |
| `backfill_start` | `2026-08-23T23:57:06.892Z` | **operator transcript** — the backfill is a manual shell command and writes nothing to journald |
| `backfill_complete` | `2026-08-23T23:57:31.699Z` | **operator transcript**, same caveat |
| elapsed | **2m54.1s** | arithmetic on the above |

**The provenance column is not decoration, and it exists because an earlier
version of this block was wrong in a way that read as precise.** It quoted
`migration_complete` as `…37.555Z` in a fenced code block, which looks like a
verbatim log line. There is no event at `.555`. It was a millisecond rounding
of `…37.554568` — the completion of `chain_identity_semantics_v1`, a
*different* migration from the one Watchpoint 1 is about, whose real completion
is `…37.557033`. Two of the other three values are not in journald at all.

Millisecond precision inside a code fence is a claim about where a number came
from. If some of the row is transcript-sourced, say so, or drop the precision.

**Note what that window does *not* prove.** The probe runs inside
`_run_hourly_maintenance` — hourly. A window shorter than the probe period
contains no probe tick, so `dark_surfaces` never gets an opportunity to fire.
Its absence there is a consequence of minimising the window as instructed, and
is **not** evidence the observable works. Do not record "dark_surfaces behaved
correctly" on the strength of a window it never sampled.

Because the probe is hourly, a freshly-backfilled deploy sits un-armed for a
full hour **from boot** — `_run_hourly_maintenance` is gated on a 3600-second
interval whose timer starts at startup, so a restart resets it and the window
is not a partial remainder. Arm it deliberately rather than waiting.

**Two things this instruction previously got wrong, both dangerous enough to
state before the command:**

**Do not reach for `Database.initialize()`.** It applies ~40 migrations against
a database the pipeline is concurrently writing. This runbook says two sections
earlier that readers must open read-only *before* `initialize()`, and
`scripts/backfill_chain_identity_recompute.py` carries the same warning — both
because a 2-minute cron doing exactly this once produced 74 `database is
locked` errors. An arming snippet that quietly requires `initialize()`
contradicts its own runbook.

**Read the gate from `Settings`; never hardcode it.** The scheduler uses
`settings.CONVICTION_EARLY_LEAD_MINUTES` (default 1440), so a literal `1440.0`
agrees with it today and diverges the moment a `.env` overrides it. Because the
mark is a MAX ratchet, arming under a *looser* gate sets it too high and
manufactures a false collapse page later — the drift this runbook's own
watchdog section warns about.

```bash
cd /root/gecko-alpha
.venv/bin/python scripts/arm_recompute_coverage_mark.py
```

Then confirm from the read-only side. **Run the watchdog itself**, not the
checker bare:

```bash
systemctl start recompute-coverage-watchdog.service
journalctl -u recompute-coverage-watchdog.service -n 5 --no-pager
```

An earlier version of this step said *"this is what the watchdog actually
runs"* and gave `check_recompute_coverage.py` with no arguments. That was
false, and false in the direction that reassures: without `--gate-minutes` the
checker takes its own `DEFAULT_GATE_MINUTES = 1440.0`, while the watchdog
passes the gate read from `Settings`. With `CONVICTION_EARLY_LEAD_MINUTES`
overridden in `.env`, the two disagree completely — a reviewer measured the
bare command printing `recovered 180 of 180` and **exit 0** on a database where
the watchdog printed `recovering NOTHING ... THE BACKFILL CANNOT HELP` and
**exit 1**.

So the verify step hardcoded the gate by omission, two paragraphs after this
runbook forbids hardcoding it. If you must run the checker directly, pass the
gate explicitly:

```bash
GATE=$(.venv/bin/python -c "from scout.config import Settings; print(Settings().CONVICTION_EARLY_LEAD_MINUTES)")
.venv/bin/python scripts/check_recompute_coverage.py --gate-minutes "$GATE"
```

**What to read in the output.** `[collapse detection NOT ARMED on <surfaces>]`
names every surface that is large enough to be judged and has no mark. Its
absence is the criterion for "armed" — but only since it became per-surface;
it used to be a global OR, so one armed surface silenced the notice for the
other two.

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
failure but 2–7 surface in `systemctl status`.

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
