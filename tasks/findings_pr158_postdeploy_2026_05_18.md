# PR #158 post-deploy cycle-1 evidence

Date: 2026-05-18
Backlog: BL-NEW-HELD-POSITION-REFRESH-RATE-GAP
Related PRs (all merged this session): #158, #161, #162, #163, #166
Status: cycle-1 evidence captured; 24h validation window OPEN

This is a follow-up evidence record. PR #158 (stale_open_count gauge + per-token
WARN) merged to master as `a649032` at 2026-05-18T15:24:26Z; PR #163 (validation
prep + `/coins/{id}` fallback design) merged as `2f8f187` at 15:29:29Z; PR #166
recorded that validation was blocked at that point because the operator flag
was still disabled. This doc records what happened after the operator flipped
the flag.

## Deploy state at time of flip

- Prod `/root/gecko-alpha` HEAD: `147cba4` on `master` (contains #158 emitters
  in `scout/ingestion/held_position_prices.py` lines 244, 261, 322, 403, 419,
  423).
- Service `gecko-pipeline` active, no `HELD_POSITION_*` keys in `.env` (refresh
  lane therefore disabled — consistent with the 0 baseline below).

## What was done

1. Appended to `/root/gecko-alpha/.env` (backup at
   `.env.bak.preflip-2026-05-18`):
   - `HELD_POSITION_PRICE_REFRESH_ENABLED=True`
   - `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES=1`
   - `HELD_POSITION_STALE_WARN_HOURS` left at default 24.
2. `systemctl restart gecko-pipeline` at `2026-05-18T16:09:51Z`. Service active
   by `16:11:21 UTC`, PID 2523485, HEAD unchanged.
3. Waited ~6 minutes for the first cycle to complete.
4. Collected journal evidence per the PR #163 runbook.

## Cycle-1 journal evidence

First post-restart `held_position_refresh_summary` fired at
`2026-05-18T16:16:07.761267Z`:

```json
{
  "event": "held_position_refresh_summary",
  "refreshed_count": 150,
  "held_total": 150,
  "skipped_contract_addr_count": 0,
  "not_found_count": 0,
  "simple_price_missing_ids": [],
  "material_drift_count": 12,
  "largest_drift_pct": -61.33,
  "stale_open_count": 0,
  "stale_open_pct": 0.0
}
```

Three evidence streams from runbook Step 2:

| Stream | Count | Reading |
|---|---|---|
| `held_position_refresh_summary` | 1 | All 150 held positions refreshed in one cycle |
| `simple_price_missing_ids` | `[]` | `/simple/price` returned a price for every held token |
| `held_position_token_persistently_stale` | 0 | No per-token WARNs fired |

Pre-restart baseline (1h window before flip): 0 summary events. Confirms the
refresh lane was effectively off prior to the flip — matches the `.env` having
no `HELD_POSITION_*` keys.

## Cohort overlap (runbook Step 3)

Known stale 21-token cohort from
`tasks/findings_held_position_refresh_rate_gap_2026_05_18.md` (pythia,
argentine-football-association-fan-token, fartboy, iagon, kekius-maximus,
secret, navi, prometeus, ready, olaxbt, marcopolo, safecoin, kinetiq,
anthropic-prestocks-2, bityuan, manyu-2, meme-horse, hippo-protocol,
superwalk, circle-internet-group-ondo-tokenized-stock, folks).

Mentions in post-restart `simple_price_missing_ids` or `persistently_stale`
events: **zero**. All previously-stale tokens now resolve through
`/simple/price` once the refresh lane is active.

## Hypothesis change

Prior triage held that 49/150 held positions had stale `price_cache > 24h`,
which motivated the `/coins/{id}` fallback design (PR #163's
`tasks/design_held_position_fallback_coins_endpoint.md`).

Cycle-1 evidence indicates the prior stale state was **disabled-lane / config
inactivity**, not `/simple/price` coverage failure. The same tokens that were
stale before the flip are now refreshing cleanly through `/simple/price`.

This is a lever-vs-data-path correction (global CLAUDE.md §9c): the visible
lever was "tokens that `/simple/price` can't reach"; the actual upstream gate
was "refresh lane disabled because the flag was unset in `.env`." Until the
upstream gate was opened, the data path never reached the supposedly broken
lever, so the lever was misattributed as the failure point.

## Fallback design — NOT backed out

`BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` is **not** retired by this
evidence. The runbook's promotion gate requires `/simple/price` to miss AND
`/coins/{id}` to recover. Cycle-1 shows `/simple/price` missing nothing, so
the gate did not fire — but a single cycle is not the evidence needed to retire
a fallback design.

Decision: keep PR #163's fallback design on file. If the 24h window stays clean
with `simple_price_missing_ids=[]` and zero WARNs, file a small follow-up to
mark `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` deferred / superseded-by-
evidence — do not implement it.

## Material drift side-finding

`material_drift_count=12 / 150 = 8.0%` and `largest_drift_pct=-61.33%` in a
single refresh confirms the refresh lane was doing real, value-bearing work
that had been silently missing while the flag was unset. At least one held
position had a cached price 61% off the live value at the moment of refresh —
the kind of cache age that defeats trailing-stop and drawdown evaluators
downstream.

This is independent of the fallback question and supports keeping the refresh
lane enabled.

## 24h validation gate — OPEN

The runbook's 24h validation is NOT complete on the strength of one cycle.
Open items:

- Confirm `held_position_refresh_summary` fires reliably across a full 24h
  window of varying CG rate-limit conditions.
- Confirm `simple_price_missing_ids` stays empty OR identify any tokens that
  recur in it — those are the only candidates that would re-motivate the
  `/coins/{id}` fallback.
- Confirm `stale_open_count` stays at 0 OR identify drift conditions that push
  it positive (proves the gauge is exercised, not just floored).
- Confirm `held_position_token_persistently_stale` WARNs only fire when there
  is a real per-token stall, not as a false-positive on transient CG misses.

Re-evaluate at `2026-05-19T16:10:00Z` (24h post-flip).

## Revert path

If anything regresses inside the 24h window:

```bash
ssh root@srilu-vps 'cp /root/gecko-alpha/.env.bak.preflip-2026-05-18 /root/gecko-alpha/.env && systemctl restart gecko-pipeline'
```

The diff between current `.env` and the backup is a 4-line append (verified in
`.ssh_step1_env_edit.txt`), so the restore is clean.

## What this doc is NOT

- Not a 24h validation closure. The closure goes in a follow-up doc once the
  24h window has been observed.
- Not authorization to back out the `/coins/{id}` fallback design. That
  requires the 24h soak to remain clean AND a deliberate backlog flip.
- Not a recommendation to disable the refresh lane. Cycle-1 evidence supports
  leaving it enabled; do not flip it back unless regressions appear in the
  24h window.
