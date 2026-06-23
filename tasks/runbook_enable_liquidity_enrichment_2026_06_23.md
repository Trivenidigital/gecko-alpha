# Runbook — enable the liquidity-enrichment writer (b2 Phase 1a-ii)

**Why:** `paper_trade_entry_snapshots.liquidity_usd_at_entry` is NULL in 100% of
rows because the CoinGecko cohort never carries pool liquidity. The enrichment
schema + writer/watchdog scripts shipped (2026-05-30) but the writer was never
turned on (`LIQUIDITY_ENRICHMENT_ENABLED=false`, 0/1420 coverage). This runbook
turns it on. Companion to the snapshot read-side fix (PR #381).

**Guardrails honored (b2 design):** read-only enrichment into decoupled
`candidates.liquidity_usd_enriched*` columns; NO ranking/filtering/sizing/
execution consumes liquidity; killswitch via `.env`.

## Order matters
The cron block (this PR) is inert until two things are true on the VPS:
`LIQUIDITY_ENRICHMENT_ENABLED=true` in `.env`, and the heartbeat dir exists.
Do the `.env` flip + a manual verify run **before** installing the cron block,
or the watchdog will (correctly) alert writer-heartbeat staleness.

## Steps (on gecko-vps)

```bash
# 0. Pull the merged PR
cd /root/gecko-alpha && git pull --ff-only origin master

# 1. Create the heartbeat dir (writer touches this on every successful tick)
mkdir -p /var/lib/gecko-alpha/liquidity-enrichment

# 2. Flip the killswitch ON (back up .env first)
cp .env .env.bak.$(date -u +%Y%m%dT%H%M%SZ)
#   add or set:  LIQUIDITY_ENRICHMENT_ENABLED=true
#   (optionally tune LIQUIDITY_ENRICHMENT_TTL_SEC / LIQUIDITY_BACKFILL_BATCH_MAX)

# 3. VERIFY with one manual tick BEFORE installing cron
.venv/bin/python scripts/backfill_dexscreener_liquidity.py \
    --db /root/gecko-alpha/scout.db \
    --heartbeat-file /var/lib/gecko-alpha/liquidity-enrichment/heartbeat
#   expect: exit 0, "liquidity_enrichment_tick_elapsed_sec", heartbeat file created

# 4. Confirm rows actually got enriched (read-only)
sqlite3 -readonly "file:scout.db?mode=ro" \
  "SELECT liquidity_enriched_confidence, COUNT(*) FROM candidates \
   WHERE liquidity_enriched_at IS NOT NULL GROUP BY 1;"

# 5. Install the cron block (idempotent managed-block merge)
GECKO_REPO=/root/gecko-alpha bash cron/deploy.sh
crontab -l | grep liquidity-enrichment   # sanity: 2 lines present

# 6. Watch one writer + watchdog cycle
tail -n 40 /var/log/gecko-alpha-liquidity-enrichment-writer.log
tail -n 40 /var/log/gecko-alpha-liquidity-enrichment-watchdog.log
```

## Verify the payoff (after coverage builds)
New paper trades on enriched tokens should now stamp liquidity + provenance:

```bash
sqlite3 -readonly "file:scout.db?mode=ro" \
  "SELECT liquidity_confidence_at_entry, COUNT(*) \
   FROM paper_trade_entry_snapshots \
   WHERE captured_at >= '2026-06-23' GROUP BY 1;"
```

## Rollback
Set `LIQUIDITY_ENRICHMENT_ENABLED=false` (writer no-ops, returns 2, no
heartbeat). To fully remove: delete the 2 liquidity lines from
`cron/gecko-alpha.crontab`, re-run `cron/deploy.sh`. The enrichment columns are
decoupled (never read by trading logic) so leaving stale values is harmless.

## Notes / risks
- The writer shares the CoinGecko 30/min rate budget with ingest
  (`configure_from_settings`) — a backlog drain may marginally slow ingest.
  `*/5` cadence + `LIQUIDITY_BACKFILL_BATCH_MAX` (default 50) bounds this.
- Brand-new tokens (e.g. fresh `volume_spike` launches) often won't be enriched
  yet at trade-open — they'll stamp `confidence='stale'`/absent and pick up a
  value on a later re-enrichment pass. Established tokens benefit immediately.
- ~923 CG rows with 0 coverage; at batch 50 every 5 min, first full pass ≈ 1.5h.
