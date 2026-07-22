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

## Part B — DEX-first activation (REVISED 12-step gate, ruling 2026-07-20)

Supersedes the earlier 6-step sequence: activation is now HELD until the
CoinGecko key is rotated, all untracked worktree files are resolved, and
the watchdog cron entry is proven installed-but-inert BEFORE discovery is
enabled. Production is Solana-only; the DEX evidence clock (T0) starts at
the first verified production ledger enrollment, first ripe checkpoint
~T0 + 11 days. Record evidence at every step.

```bash
# 1. Install the NEW CoinGecko key without printing it (no-echo entry per
#    tasks/runbook_cg_demo_api_key_2026_05_18.md hygiene; backup .env first).
# 2. Restart and verify several authenticated CG cycles:
systemctl restart gecko-pipeline
journalctl -u gecko-pipeline --since "30 minutes ago" --no-pager | grep -c cg_cycle_ok
# 3. Revoke the OLD key in the CG dashboard (only after step 2 is clean).
# 4. Resolve or document ALL 13 untracked files: record paths, then prove
#    none are imported modules / executable scripts / config-env files /
#    service DB files / files inside managed deploy or cron paths — or
#    remove/archive them:
cd /root/gecko-alpha && git status --porcelain | grep '^??'
# 5. Deploy/cron-refresh pass with BOTH features off:
#    DEX_DISCOVERY_ENABLED=False in .env, watchdog cron env gate false —
#    this pass INSTALLS the missing watchdog crontab line (it is currently
#    absent, not merely gated).
# 6. Verify the watchdog cron entry is installed but inert:
crontab -l | grep dex-discovery-watchdog
# 7. Reconfirm Solana-only:
grep -E "^DEX_DISCOVERY_NETWORKS=" .env || echo "default: solana-only"
# 8. Enable discovery:
#    set DEX_DISCOVERY_ENABLED=true in .env && systemctl restart gecko-pipeline
# 9. Verify an EXECUTED pass: poll_ok=true, heartbeat_written=true, counter
#    equations reconcile, and NO failed_none:
journalctl -u gecko-pipeline --since "30 minutes ago" --no-pager \
  | grep dex_discovery_ledger_pass | tail -3
sqlite3 -readonly scout.db \
  "SELECT source, consecutive_misses, updated_at FROM ingest_watchdog_state
   WHERE source='dex_discovery';"
# 10. Record T0 = first successful production ledger enrollment:
sqlite3 -readonly scout.db \
  "SELECT MIN(created_at) FROM signal_outcome_ledger WHERE surface='dex_new_pool';"
# 11. Manual wrapper run with the gate EXPLICITLY in the process env —
#     expect healthy JSON and exit 0:
DEX_DISCOVERY_WATCHDOG_ENABLED=true bash scripts/dex-discovery-watchdog.sh; echo $?
# 12. Enable the watchdog in the CRON environment and verify its first
#     scheduled execution (next :25 run) in syslog/cron logs.
```

Abort/rollback at any step: `DEX_DISCOVERY_ENABLED=false` + restart (lane
is byte-identical off) and remove the cron gate var. Nothing else changes.

---

## Part C — evidence-language constraints (ruling 2026-07-20)

- `gainers_early`: revival gate PASSED on registered closed-trade terms —
  keep enabled and unsuppressed. Do NOT claim positive expectancy: closed
  realized +$175.52, open-position MTM ~−$169 → combined ~+$6.52, i.e.
  approximately flat before fees/execution realism. Wait for the open
  positions to close and a net-of-cost cohort.
- Suppression wording: "Suppression was observed cleared following a
  subsequent nightly refresh." Do not claim the exact clearing mechanism
  more strongly without a transition log or before/after audit row.
- Cohort artifact repair (VPS): preserve the machine-captured query/output
  verbatim (no transcription), and re-run the cohort window from the EXACT
  revival audit timestamp 2026-07-17T12:28:52.954712Z; separately report
  qualifying trades opened in 12:28:00→12:28:52.954712Z (zero → 43-trade
  result stands; nonzero → recompute). One-command collector (also covers
  unique-token counts and the time_death counterfactual run):
  `bash investigation/ruling_response_queries.sh 2>&1 | tee /tmp/ruling_response_$(date -u +%Y%m%dT%H%M%SZ).log`
- Next checkpoint must report unique token/contract count alongside unique
  trade IDs (43 rows ≠ 43 independent exposures).
- `time_death`: "loss-making in realized terms and pending counterfactual
  adjudication as a possible loss-mitigation mechanism." Adjudicate with
  `investigation/time_death_counterfactual.py` (measured / dry-run-era /
  unresolved never blended; per-trade incremental_benefit; coverage %;
  no look-ahead).

---

## Standing items

1. **PR #467 + DEX series**: merged (`9690df1a` / `32d1ca4e`). Docs rescue
   may proceed independently of runtime state.
2. **CG key rotation** is now step 1 of the activation gate above — not a
   separate afterthought.
3. **`COINGECKO_RATE_LIMIT_PER_MIN` raise** (25 → ~50–100) once burn-rate
   data is in (couples with A3).
