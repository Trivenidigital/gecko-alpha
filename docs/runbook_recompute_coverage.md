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
surface, raised when recovery improves, never lowered. Ordinary attrition rides
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

| code | meaning | Telegram |
|---:|---|:--:|
| 0 | healthy, or nothing to recover | no |
| 1 | **alarm** — a surface is recovering nothing | yes |
| 2 | the check could not run (unreadable DB) | no |
| 3 | `.env` missing | no |
| 4 | Telegram credentials missing — the alarm fired and could not be delivered | no |
| 5 | `APP_DIR` invalid | no |
| 6 | interpreter missing, or the app cannot be imported to read the gate | no |

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
