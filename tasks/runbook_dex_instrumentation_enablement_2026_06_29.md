# Runbook — DEX-outcome instrumentation enablement (observe-only)

**Date:** 2026-06-29
**Feature:** PRs #383/#384/#385 (implementation merged to `master`; #386 runbook + #387 `LOOPS.md` are docs-only on top)
**Status:** NOT DEPLOYED, NOT ENABLED. This is the operator runbook only — no live execution here.
**Spec:** `spec_dex_outcome_instrumentation_i1_i2_i3_2026_06_28.md`

## Boundaries (must hold throughout)
No gate recalibration · no threshold change · no scoring change · no paid Helius/Moralis · no
trading-alert behavior change · the `txns_h1_buys` proxy is **captured-not-scored** until a future
evidence-backed PR.

## Targets / current state (probed read-only 2026-06-29)
- Deploy target: **the current operator-approved `master` HEAD** (the approved SHA at deploy time —
  do not hardcode). Behavior baseline = the #385 implementation merge; every master commit since
  (#386 runbook, #387 `LOOPS.md`) is **docs-only**, so runtime behavior is identical regardless of which
  of those SHAs is current. srilu runtime lags master (was `c1ac43fc` at last probe).
- VPS: `ssh srilu-vps` → `/root/gecko-alpha`. Units: `gecko-pipeline` (cycles/migrations/watchers),
  `gecko-dashboard` (:8000) — both `active`.
- The 3 instrumentation tables do **not** exist yet (created by `_migrate_dex_instrumentation_v1` on
  pipeline startup at deploy).
- `.env`: `TELEGRAM_CHAT_ID=-1003845334764` (**trading channel**). `TELEGRAM_HEALTH_CHAT_ID` **unset**,
  `DEX_INSTRUMENTATION_ENABLED` **unset** (→ default `False`).
- ⚠️ **Set `TELEGRAM_HEALTH_CHAT_ID` to a SEPARATE chat before enabling.** If left empty, watchdog
  alerts fall back to `TELEGRAM_CHAT_ID` (the trading channel) — violating "health alerts only to the
  operator/health channel." This is a hard precondition for step 2.

---

## 1. Flag-OFF deploy verification (deploy current approved `master`, flag stays False)

**Deploy (flag remains False — `.env` unchanged):**
```bash
ssh srilu-vps
cd /root/gecko-alpha
git fetch origin && git checkout master && git pull --ff-only origin master   # current approved master SHA
find . -name __pycache__ -type d -prune -exec rm -rf {} +   # mandatory after Python pull
sudo systemctl restart gecko-pipeline             # migration runs on startup
```

**1a. Runtime commit = current approved master SHA**
```bash
cd /root/gecko-alpha && git rev-parse HEAD        # must equal the operator-approved master SHA
git rev-parse origin/master                        # sanity: same SHA
```
The check is **"runtime commit equals the approved `master` SHA"**, not a hardcoded commit. Code
behavior is identical to the #385 implementation merge; all later master commits (#386 runbook, #387
`LOOPS.md`) are docs-only.

**1b. Flag is False**
```bash
grep -E "DEX_INSTRUMENTATION_ENABLED" /root/gecko-alpha/.env || echo "unset -> default False"
```

**1c. Tables exist (migration ran) but stay EMPTY (flag off → no writes)**
```bash
sqlite3 /root/gecko-alpha/scout.db "
SELECT 'contract_coin_map', count(*) FROM contract_coin_map
UNION ALL SELECT 'entry_mcap_snapshots', count(*) FROM entry_mcap_snapshots
UNION ALL SELECT 'txns_h1_buys_snapshots', count(*) FROM txns_h1_buys_snapshots;"
# Expect all three present and count = 0. Re-run after ~15 min: counts MUST stay 0.
```
Also confirm the migration row:
```bash
sqlite3 /root/gecko-alpha/scout.db "SELECT version, description FROM schema_version WHERE version=20260629;"
```

**1d. No new health or trading alerts from instrumentation**
```bash
journalctl -u gecko-pipeline --since "15 min ago" | grep -E "dex_instrumentation_|dex_resolver_" || echo "none (correct with flag off)"
```
Expect no `dex_instrumentation_*` / `dex_resolver_*` events. No new messages in either Telegram channel.

**1e. Scoring/gate behavior unchanged**
Structural guarantee: `scorer.py`/`gate.py` are byte-identical to pre-merge; GT counts live in
instrumentation-only `gt_txns_*` (scorer never reads them); CI regression test
`test_geckoterminal_buy_pressure_does_not_enter_scorer` proves `buy_pressure` stays DexScreener-only.
Spot-check prod parity:
```bash
# candidate scoring continues normally (rows still being scored/written)
sqlite3 /root/gecko-alpha/scout.db "SELECT max(first_seen_at) FROM candidates;"
# GT-sourced tokens must NOT newly show buy_pressure (BLOCKING-1 guard); sanity scan recent rows:
sqlite3 /root/gecko-alpha/scout.db "SELECT chain, count(*) FROM candidates WHERE first_seen_at > datetime('now','-1 hour') GROUP BY chain;"
```
**Gate to pass step 1:** 1a–1e all hold (commit correct, flag off, tables present+empty after 15 min,
no dex_* logs, scoring/gate parity). If any fails → STOP, do not enable; investigate.

---

## 2. Enablement checklist (deliberate; only after step 1 passes)

**2a. Env vars** — add to `/root/gecko-alpha/.env`:
```ini
DEX_INSTRUMENTATION_ENABLED=True
TELEGRAM_HEALTH_CHAT_ID=<separate operator/health chat id>   # REQUIRED — not the trading chat
# Optional (defaults shown):
# DEX_RESOLVER_BUDGET_PER_CYCLE=5
# DEX_RESOLVER_NEGATIVE_TTL_SEC=3600
# DEX_TXNS_RETENTION_DAYS=30
# DEX_RESOLUTION_HEALTH_FLOOR=0.05
# DEX_NONZERO_MCAP_FLOOR=0.90
# DEX_NONNULL_TXNS_FLOOR=0.50
```
**Precondition:** `TELEGRAM_HEALTH_CHAT_ID` set to a non-trading chat (the bot must be a member).

**2b. Health-channel routing** — verify the bot can post to the health chat (send one manual test) so
delivery isn't silently failing.

**2c. Restart sequence**
```bash
find /root/gecko-alpha -name __pycache__ -type d -prune -exec rm -rf {} +
sudo systemctl restart gecko-pipeline
sudo systemctl is-active gecko-pipeline    # expect active
```

**2d. Rollback sequence (fast — flag flip, no code change)**
```bash
# set DEX_INSTRUMENTATION_ENABLED=False in .env (or remove the line)
sudo systemctl restart gecko-pipeline
# capture stops immediately; existing rows are harmless/observe-only. No data is fed to scorer/gate.
```
Code rollback (only if needed): check out the last pre-feature `master` SHA (the commit before the #385
implementation merge — `c1ac43fc` at time of writing) + restart. Tables persist (additive, never read
by scorer/gate).

**2e. Expected first-hour behavior (flag on)**
- `txns_h1_buys_snapshots` grows every cycle (fastest table).
- `entry_mcap_snapshots` grows ~ new-DEX-contract rate (tens/hr); finalized rows have `mcap_usd_at_entry>0`.
- `contract_coin_map` grows as CG-native coin_ids resolve (≤`DEX_RESOLVER_BUDGET_PER_CYCLE`/cycle).
- Hourly maintenance emits one `dex_instrumentation_metrics` log + runs the watchdog + prune (no-op early).
- `dex_resolution_health` starts near 0 and climbs over days; `dex_measurable_cohort_size` grows slowly
  (a DEX contract must get a coin_id AND an entry-mcap AND ≥1 outcome surface to count). Low early values
  are EXPECTED, not a failure — distinguish from fresh-but-empty (§4).
- No new trading-channel messages. Watchdog alerts (if any) go to the health chat only.

---

## 3. Soak monitoring query pack

**Run on the VPS:** `sqlite3 /root/gecko-alpha/scout.db "<query>"`. Also tail the structured logs.

**3a. Coverage metrics (`dex_resolution_health`, `dex_measurable_cohort_size`)**
```sql
WITH listed AS (
  SELECT m.contract_address ca,
    (SELECT 1 FROM entry_mcap_snapshots e WHERE e.contract_address=m.contract_address LIMIT 1) has_entry,
    (CASE WHEN m.coin_id IN (
       SELECT coin_id FROM gainers_snapshots
       UNION SELECT coin_id FROM momentum_7d
       UNION SELECT coin_id FROM conviction_watchlist_snapshots) THEN 1 ELSE 0 END) has_outcome
  FROM contract_coin_map m
  WHERE m.coin_id IS NOT NULL AND m.address_type IN ('evm','solana')
)
SELECT count(*) AS listed_dex,
       sum(CASE WHEN has_entry AND has_outcome THEN 1 ELSE 0 END) AS dex_measurable_cohort_size,
       round(CAST(sum(CASE WHEN has_entry AND has_outcome THEN 1 ELSE 0 END) AS REAL)
             / NULLIF(count(*),0), 3) AS dex_resolution_health
FROM listed;
```
Or read the emitted metric directly:
```bash
journalctl -u gecko-pipeline --since "1 day ago" | grep dex_instrumentation_metrics | tail -5
```

**3b. Entry-mcap non-zero (finalized) rate**
```sql
SELECT count(*) total,
       sum(CASE WHEN mcap_usd_at_entry>0 THEN 1 ELSE 0 END) finalized,
       round(CAST(sum(CASE WHEN mcap_usd_at_entry>0 THEN 1 ELSE 0 END) AS REAL)/NULLIF(count(*),0),3) nonzero_rate
FROM entry_mcap_snapshots;
```

**3c. `txns_h1_buys` non-null rate**
```sql
SELECT count(*) total,
       sum(CASE WHEN txns_h1_buys IS NOT NULL THEN 1 ELSE 0 END) nonnull,
       round(CAST(sum(CASE WHEN txns_h1_buys IS NOT NULL THEN 1 ELSE 0 END) AS REAL)/NULLIF(count(*),0),3) nonnull_rate
FROM txns_h1_buys_snapshots;
```

**3d. Fresh-but-empty signatures** (table fresh AND quality-rate ≈ 0 → silent failure)
```sql
-- freshness (last write per table):
SELECT 'coin_map', max(resolved_at) FROM contract_coin_map
UNION ALL SELECT 'entry_mcap', max(captured_at) FROM entry_mcap_snapshots
UNION ALL SELECT 'txns', max(scanned_at) FROM txns_h1_buys_snapshots;
```
Cross-reference with 3b/3c: rows present + recent writes + rate ~0 = fresh-but-empty → STOP.

**3e. Negative resolver marker counts (failed/unknown resolutions)**
```sql
SELECT count(*) attempt_markers FROM contract_coin_map WHERE chain='__attempt__';
SELECT count(*) resolved FROM contract_coin_map WHERE coin_id IS NOT NULL AND chain<>'__attempt__';
```
Also the per-pass counters:
```bash
journalctl -u gecko-pipeline --since "1 day ago" | grep dex_resolver_pass | tail -10   # attempted/recorded/failed
```

**3f. Watchdog dispatched/delivered counts**
```bash
journalctl -u gecko-pipeline --since "1 day ago" | grep -c dex_instrumentation_alert_dispatched
journalctl -u gecko-pipeline --since "1 day ago" | grep -c dex_instrumentation_alert_delivered
journalctl -u gecko-pipeline --since "1 day ago" | grep dex_instrumentation_alert_failed   # expect none
```
dispatched == delivered (and 0 failed) = healthy delivery. dispatched > delivered = delivery problem.

---

## 4. Proceed / Stop gates

**PROCEED** (toward the F1 DEX-cohort re-run) only when ALL hold over the soak:
- Metrics are **fresh AND semantically non-empty**: tables growing, `nonzero_rate ≥ DEX_NONZERO_MCAP_FLOOR`
  (0.90), `nonnull_rate ≥ DEX_NONNULL_TXNS_FLOOR` (0.50), resolver producing resolutions.
- `dex_measurable_cohort_size` reaches the proceed-gate: **n ≥ 30 with ≥1 token having run ≥10×**.
- Watchdog `dispatched == delivered`, `failed == 0`, alerts only in the health channel.

**STOP** (disable via §2d, investigate) if ANY:
- **Fresh-but-empty**: rows being written but `nonzero_rate`/`nonnull_rate` ≈ 0, or `map` rows with 0
  resolved (the watchdog should also fire on these — confirm it routed to health).
- **Trading-alert behavior changes** — any instrumentation alert reaches the trading channel, or trading
  alert volume/shape shifts.
- **Scoring/gate output differs from the flag-off baseline** — e.g., a GT-sourced token shows
  `buy_pressure` it would not have pre-enablement, or conviction/alert behavior shifts.
- Resolver `failed` counter dominates (persistent fetch failures) or budget exhausted every cycle.

**Hard reminder:** reaching the cohort-size gate does NOT authorize gate recalibration or proxy scoring.
The F1 re-run is a separate read-only analysis; recalibration remains blocked and must report the
never-listing survivorship bound alongside the cohort.

---

## Appendix — reference

**Tables:** `contract_coin_map(contract_address, chain, coin_id, resolved_at, source, confidence,
address_type)` · `entry_mcap_snapshots(contract_address PK, chain, first_seen_at, mcap_usd_at_entry,
liquidity_usd_at_entry, token_age_days_at_entry, captured_at)` · `txns_h1_buys_snapshots(id,
contract_address, txns_h1_buys, txns_h1_sells, source, scanned_at)`.

**Log events:** `dex_instrumentation_metrics` (hourly), `dex_instrumentation_alert_dispatched` /
`_delivered` / `_failed`, `dex_resolver_pass` (attempted/recorded/failed), `dex_resolver_fetch_failed`,
`dex_txns_snapshots_pruned`, `dex_instrumentation_watchdog_failed`.

**Env (all default-safe):** `DEX_INSTRUMENTATION_ENABLED=False`, `DEX_RESOLVER_BUDGET_PER_CYCLE=5`,
`DEX_RESOLVER_NEGATIVE_TTL_SEC=3600`, `DEX_TXNS_RETENTION_DAYS=30`, `DEX_RESOLUTION_HEALTH_FLOOR=0.05`,
`DEX_NONZERO_MCAP_FLOOR=0.90`, `DEX_NONNULL_TXNS_FLOOR=0.50`, `TELEGRAM_HEALTH_CHAT_ID=""`.
