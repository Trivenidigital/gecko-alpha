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

Both escalate on **recovered credit**, never on row count, and neither pages on
partial coverage — history genuinely runs out for older anchors, and an alarm
that fires on the steady state gets muted before the day it matters.

Install:

```bash
install -m 0755 scripts/recompute-coverage-watchdog.sh /usr/local/bin/
# scripts run from /usr/local/bin, so `git pull` alone deploys NOTHING here
```

## Reading the status breakdown

| Status | Meaning | Earns credit |
|---|---|---|
| `verified_canonical` | canonical identity confirms the detection | **yes** |
| `canonical_below_gate_indeterminate` | canonical match, reconstructed lead under the gate — *not* a proven negative (left-censoring) | no |
| `indeterminate_history` | the anchor falls in a coverage gap; absence of a match is not evidence of absence | no |
| `verified_prefix_only` | demonstrated prefix collision — the lead was fabricated | no |
| `no_legacy_credit` | never held chains credit; outside the population | n/a |
| `unjoinable_row` | archived row with no coin_id or no anchor | n/a |

Statuses always sum to the archived population. If they do not, a row was
dropped rather than classified — that is a defect, not a rounding artefact.
