# Operator decision pack — P5 inert flags + DEX activation (2026-07-20)

Current-state update of the ops pack's Priority 5 briefs (frozen at 07-18)
plus the paste-ready DEX-first activation sequence. Every action below is a
prod-state change: operator-executed, per-item decision, recorded approval
discipline applies. Nothing here was executed by the agent session.

**Context that changed since 07-18:** CG restored on the Basic (paid) plan
with tier routing (#468); pipeline confirmed back online by the VPS session;
detection lane reported re-enabled under the #466 gate; DEX-first Phase 1
merged to master (`32d1ca4e` = #469+#470+#471), ready for
deploy-without-activate. All commands run on the VPS as the pipeline user.

---

## Part A — P5 flag decisions (one verdict each)

### A1. `DETECTION_ALERT_LANE_ENABLED` — VERIFY, likely already closed

The VPS closing report says the detection lane was re-enabled 07-19/20. So
this is now a verification, not a decision:

```bash
grep -E "^DETECTION_ALERT_LANE_ENABLED=" /root/gecko-alpha/.env
journalctl -u gecko-pipeline --since "24 hours ago" --no-pager \
  | grep -c detection_alert_funnel
```

- Flag `=true` AND funnel events > 0 → **item closed**; record "enabled
  2026-07-19 by VPS session" in the ops log and just keep the week-one
  watch: funnel counters (pool / gated_out / eligible / sent) should show
  sent > 0 within days now that CG rows are flowing.
- Flag absent/false → flip it (`.env`), restart, verify funnel events
  appear within 2 cycles. Rollback = flag off, restart.

### A2. `MOVED_ALREADY_POSTMORTEM_ENABLED` (#459) — RECOMMEND ON, at the 32d1ca4e deploy

Unchanged recommendation, now with a natural moment: you are about to
deploy `32d1ca4e` anyway (Part B step 0). Bare-additive table + hourly
recorder; without it, moved-already evidence keeps evaporating with the
7-day `gainers_snapshots` retention — and the DEX-discovery corpus makes
that evidence MORE valuable (it is the graduation-lag measurement).

```bash
# in .env at the same deploy:
MOVED_ALREADY_POSTMORTEM_ENABLED=true
# verify within 2h:
journalctl -u gecko-pipeline --since "2 hours ago" --no-pager \
  | grep -c moved_already_postmortem
sqlite3 -readonly /root/gecko-alpha/scout.db \
  "SELECT COUNT(*) FROM moved_already_postmortems;"
```

Rollback = flag off, restart. No schema risk (additive only).

### A3. `LIQUIDITY_ENRICHMENT_ENABLED` (#382) — HOLD until CG burn-rate is known

The 07-18 caution was CG credit budget. That constraint CHANGED (Basic
plan), but the safe order is: collect ~3 days of post-restore credit
burn-rate first (already a standing item for the rate-limit raise), then
decide both together — enrichment adds CG calls per cycle.

```bash
# burn-rate check (dashboard is authoritative; local proxy:)
journalctl -u gecko-pipeline --since "24 hours ago" --no-pager \
  | grep -c cg_request
```

If headroom is comfortable after the rate-limit decision: enable per
`tasks/runbook_enable_liquidity_enrichment_2026_06_23.md` (its lag watchdog
lives in the same managed cron block — activate both together). Otherwise
stay OFF; nothing degrades.

### A4. `RETIRE_DEAD_TABLES_ENABLED` (#461) — KEEP OFF (unchanged)

Destructive DROPs; rollback is restore-from-backup only. Flip only in a
deploy where a fresh backup is verified on disk first:

```bash
ls -lh /root/backups/scout-*.db | tail -3   # confirm fresh + nonzero size
# then: flag on → deploy → verify DROPs logged → flag OFF again same day
```

No urgency; recommend deferring past the DEX activation window so two
irreversible-ish changes never share a deploy.

### A5. Missing forward tracker (unchanged nag)

`tasks/backlog_fable_analysis_2026_07_10.md` is referenced by `backlog.md`
as authoritative but is not in the repo. If it exists locally, commit it.

---

## Part B — DEX-first activation (staged sequence, approved 2026-07-20)

Approved only through this exact sequence (PR #471 merge ruling). Evidence
note for the record at each step; production is **Solana-only**
(`DEX_DISCOVERY_NETWORKS`) — multi-network expansion requires per-network
heartbeats first.

```bash
# 0. Deploy master 32d1ca4e with BOTH gates off (deploy-without-activate)
cd /root/gecko-alpha && git fetch origin master && git checkout 32d1ca4e
# (or your normal deploy script) ; refresh the managed crontab block so the
# :25 watchdog line (gated, inert) is installed; restart the pipeline.
grep -E "^DEX_DISCOVERY_ENABLED=" .env          # absent or =false
crontab -l | grep dex-discovery-watchdog        # line present, gate var =true only in cron... NOT yet

# 1. Confirm Solana-only
grep -E "^DEX_DISCOVERY_NETWORKS=" .env || echo "default: solana-only"

# 2. Enable discovery (lane only, watchdog still off)
echo 'DEX_DISCOVERY_ENABLED=true' >> .env && systemctl restart gecko-pipeline

# 3. Verify one successful poll + ledger reconciliation (within ~3 cycles)
journalctl -u gecko-pipeline --since "30 minutes ago" --no-pager \
  | grep dex_discovery_ledger_pass | tail -3
#    expect: poll_ok=true, heartbeat_written=true, and
#    candidates = attempted + budget_skipped ; attempted = succeeded + failed_none
sqlite3 -readonly scout.db \
  "SELECT source, consecutive_misses, updated_at FROM ingest_watchdog_state
   WHERE source='dex_discovery';"
sqlite3 -readonly scout.db "SELECT COUNT(*) FROM dex_pool_discoveries;"

# 4. Arm the watchdog via CRON ENV ONLY (never .env — a stray .env entry is
#    ignored by design and tested)
crontab -e   # the managed line already carries DEX_DISCOVERY_WATCHDOG_ENABLED=true

# 5. One manual wrapper run BEFORE trusting cron — expect healthy JSON
DEX_DISCOVERY_WATCHDOG_ENABLED=true bash scripts/dex-discovery-watchdog.sh
#    expect stdout: {"status": "ok", ...} and exit 0 (echo $?)

# 6. Record the activation evidence (timestamps of steps 3+5, Solana-only
#    note) — the ledger evidence clock starts at the FIRST PRODUCTION
#    ENROLLMENT timestamp from step 3, not the merge date. First ripe read
#    ≈ that timestamp + 11 days.
sqlite3 -readonly scout.db \
  "SELECT MIN(created_at) FROM signal_outcome_ledger WHERE surface='dex_new_pool';"
```

Abort/rollback at any step: `DEX_DISCOVERY_ENABLED=false` + restart (lane
is byte-identical off), and remove the cron gate var. Nothing else changes.

---

## Standing items (unchanged)

1. **CG key regeneration** after a day of clean operation — the current key
   appeared in session transcripts; regenerate in the CG dashboard, update
   `.env`, restart, verify one cycle.
2. **`COINGECKO_RATE_LIMIT_PER_MIN` raise** (25 → ~50–100) once burn-rate
   data is in (couples with A3).
3. **PR #467 merge ruling** — gate closed on head `dac97b5`, CI green,
   awaiting reviewer decision.
