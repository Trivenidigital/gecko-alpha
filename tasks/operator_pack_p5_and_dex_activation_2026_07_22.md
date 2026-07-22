# Operator decision pack — P5 flag/lane decisions + DEX activation (refreshed 2026-07-22)

Refresh of the ops pack's Priority-5 briefs
(`tasks/ops_pack_priorities_2026_07_18.md`) and the DEX-first activation
sequence, updated to the 2026-07-22 deployed state. This is a **refresh of
the stranded commit `e6adbf12`** (dated 07-20), not a replay of it — the
flag verdicts and the evidence clock are re-stated against current runtime.
Every action below is a prod-state change: operator-executed, per-item
decision, recorded-approval discipline applies. Nothing here was executed by
the agent session.

**Deployed state (verified 2026-07-22).** The VPS is on master `9690df1a`
(= #467, squash-merged 2026-07-21 01:20Z); `gecko-pipeline` is active,
CoinGecko is flowing (`cg_ranked=15`), worktree clean. That master already
carries DEX-first Phase 1 (#469 + #470 + #471 — all ancestors of
`9690df1a`) and the CoinGecko paid-tier switch (#468), so the DEX lane is
**deploy-without-activate on the box today**: `DEX_DISCOVERY_ENABLED` is
still false and **its evidence clock has NOT started** (see Part B, T0). All
commands run on the VPS as the pipeline user.

---

## Part A — flag / lane decisions (state as of 2026-07-22, one verdict each)

### A1. `DETECTION_ALERT_LANE_ENABLED` (#460 + #466) — ON, soak running (closed to a watch)

Enabled `=true` since 2026-07-19 under the #466 quality gate
(`DETECTION_ALERT_MIN_QUANT_SCORE=1`, score-ordered slots). Forward soak is
running; this is now a verification + week-one watch, not a decision.

```bash
grep -E "^DETECTION_ALERT_LANE_ENABLED=|^DETECTION_ALERT_MIN_QUANT_SCORE=" /root/gecko-alpha/.env
journalctl -u gecko-pipeline --since "24 hours ago" --no-pager | grep -c detection_alert_funnel
```

Funnel counters (pool / gated_out / eligible / sent) should show sent > 0
within days now that CG rows are flowing. Rollback = flag off, restart.

### A2. `gainers_early` — KEEP (passed its pre-registered revival gate)

Revived 2026-07-17T12:28Z. Since revival: **n=43 closed, +$175.52 net,
67.4% WR — it passed its pre-registered revival gate.** Parole cleared, and
the combo suppression auto-cleared on the nightly refresh. This is the
revival criterion being met, *not* a claim of decisively positive
expectancy. Auto-suspend + §12b operator-alert protections remain armed, so
a regime turn re-suspends and notifies without operator action.

Decision: leave enabled, no flag flip. Keep watching the auto-suspend gate.

### A3. `time_death` exit — CLASSIFIED as loss-mitigation (KEEP live)

Live since 2026-07-17; 21 closes, −$462.59 realized to date. The negative
sum is **not** failure: the reviewer's actual-vs-riding counterfactual has
now been computed and time_death is **CLASSIFIED 2026-07-22 as a
loss-mitigation mechanism** (it caps the bleed on flat/decaying positions),
**not a profitable signal**. Keep `PAPER_TIME_DEATH_DRY_RUN=false` — leave
it live.

Evidence: matched dry-run pairs (n=15, fire-point vs the observed ride on
the same trades) show cutting saved **+$152.70 with 14/15 worse off riding**;
live-cohort drift estimate adds **+$59–150** saved; the clipped-runner gate
holds (0/21 real closes peaked ≥25%, max 9.72%) — it is not clipping winners.

Residual / open items (do not block the classification):
- A shadow `would_fire` logging arm would harden the live-cohort estimate
  (currently a drift estimate off the matched pairs).
- Deeper open item is **entry quality on flat-profile lanes** — time_death
  caps the bleed, it does not fix the entries that produce it.

### A4. `momentum_death` exit — still DRY-RUN (hold)

Gate at 6/15 at last check; stays in dry-run. No action until the gate
closes; do not promote to live early.

### A5. `volume_spike` — AUTO-SUSPENDED, stays suspended

Auto-suspended 2026-07-18 on the hard_loss gate (−$554 over n=35). Leave
suspended; do not re-enable without a positive-expectancy re-qualification.

### A6. `MOVED_ALREADY_POSTMORTEM_ENABLED` (#459) — RECOMMEND ON (unchanged)

Bare-additive table + hourly recorder; without it, moved-already evidence
keeps evaporating with the 7-day `gainers_snapshots` retention — and the
DEX-discovery corpus makes that graduation-lag evidence MORE valuable. Flip
at the next `.env` touch (the CG key-rotation restart, or the DEX activation
edits, are both natural moments).

```bash
MOVED_ALREADY_POSTMORTEM_ENABLED=true   # then restart; verify within 2h:
journalctl -u gecko-pipeline --since "2 hours ago" --no-pager | grep -c moved_already_postmortem
sqlite3 -readonly /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM moved_already_postmortems;"
```

Rollback = flag off, restart. Additive only, no schema risk.

### A7. `LIQUIDITY_ENRICHMENT_ENABLED` (#382) — HOLD until pro-tier burn-rate is known

CG is now on the paid plan (`COINGECKO_API_TIER=pro`, live 2026-07-19
04:48Z), which relaxes but does not remove the budget constraint —
enrichment adds CG calls per cycle. Safe order unchanged: collect ~3 days of
pro-tier burn-rate first (couples with the rate-limit raise below), then
decide both together.

```bash
# burn-rate check (dashboard is authoritative; local proxy:)
journalctl -u gecko-pipeline --since "24 hours ago" --no-pager | grep -c cg_request
```

If headroom is comfortable after the rate-limit decision: enable per
`tasks/runbook_enable_liquidity_enrichment_2026_06_23.md` (its lag watchdog
lives in the same managed cron block — activate both together). Otherwise
stay OFF; nothing degrades.

### A8. `RETIRE_DEAD_TABLES_ENABLED` (#461) — KEEP OFF (unchanged)

Destructive DROPs; rollback is restore-from-backup only. Flip only in a
deploy where a fresh backup is verified on disk first:

```bash
ls -lh /root/backups/scout-*.db | tail -3   # confirm fresh + nonzero size
# then: flag on → deploy → verify DROPs logged → flag OFF again same day
```

No urgency; defer past the DEX activation window so two irreversible-ish
changes never share a deploy.

### A9. CoinGecko tier + key rotation — pro live, ROTATION pending (operator)

`COINGECKO_API_TIER=pro` has been live since 2026-07-19 04:48Z (#468 added
the tier switch). Outstanding item: the current CG key **appeared in session
transcripts** and must be rotated — regenerate in the CG dashboard, update
`.env`, restart, verify one clean cycle. Per the DEX ruling this rotation is
the FIRST step of the activation window (Part B, step 0).

### A10. Missing forward tracker (unchanged nag)

`tasks/backlog_fable_analysis_2026_07_10.md` is referenced by `backlog.md`
as authoritative but is not committed to the repo. If it exists only
locally, commit it.

---

## Part B — DEX-first activation (staged, key-rotation-first)

Approved only through this exact sequence (PR #471 merge ruling). Ordering
per the reviewer: **rotate the CG key FIRST, then the staged activation,
then record T0.** Production is **Solana-only** (`DEX_DISCOVERY_NETWORKS`) —
multi-network expansion requires per-network heartbeats first. Record an
evidence note at each step.

```bash
# 0. CG KEY ROTATION FIRST (A9) — do not enable the DEX lane on a leaked key.
#    regenerate in the CG dashboard → update .env → restart → verify one clean cycle.
grep -E "^COINGECKO_API_KEY=|^COINGECKO_API_TIER=" /root/gecko-alpha/.env
systemctl restart gecko-pipeline
journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -c cg_ranked

# 1. Confirm the deployed tree already carries DEX Phase 1, gates off
cd /root/gecko-alpha && git rev-parse HEAD    # expect 9690df1a (or later master); NO re-deploy needed
grep -E "^DEX_DISCOVERY_ENABLED=" .env         # absent or =false
crontab -l | grep dex-discovery-watchdog       # managed :25 line present, gate var NOT yet armed
#    9690df1a already contains #469+#470+#471. Refresh the managed crontab block
#    only if the gated :25 watchdog line is missing.

# 2. Confirm Solana-only
grep -E "^DEX_DISCOVERY_NETWORKS=" .env || echo "default: solana-only"

# 3. Enable discovery (lane only, watchdog still off) — this is the action that can start the clock
echo 'DEX_DISCOVERY_ENABLED=true' >> .env && systemctl restart gecko-pipeline

# 4. Verify one successful poll + ledger reconciliation (within ~3 cycles)
journalctl -u gecko-pipeline --since "30 minutes ago" --no-pager \
  | grep dex_discovery_ledger_pass | tail -3
#    expect: poll_ok=true, heartbeat_written=true, and
#    candidates = attempted + budget_skipped ; attempted = succeeded + failed_none
sqlite3 -readonly scout.db \
  "SELECT source, consecutive_misses, updated_at FROM ingest_watchdog_state
   WHERE source='dex_discovery';"
sqlite3 -readonly scout.db "SELECT COUNT(*) FROM dex_pool_discoveries;"

# 5. Arm the watchdog via CRON ENV ONLY (never .env — a stray .env entry is
#    ignored by design and tested)
crontab -e   # the managed line already carries DEX_DISCOVERY_WATCHDOG_ENABLED=true

# 6. One manual wrapper run BEFORE trusting cron — expect healthy JSON
DEX_DISCOVERY_WATCHDOG_ENABLED=true bash scripts/dex-discovery-watchdog.sh
#    expect stdout: {"status": "ok", ...} and exit 0 (echo $?)

# 7. Record T0 — the evidence clock (see note below)
sqlite3 -readonly scout.db \
  "SELECT MIN(created_at) FROM signal_outcome_ledger WHERE surface='dex_new_pool';"  # = T0 once nonempty
```

**Evidence clock — T0.** `T0` is the **first VERIFIED production
enrollment**: the first `signal_outcome_ledger` enrollment row written by the
`dex_discovery` lane AFTER activation (step 3), read off the query in step 7.
It is *not* the merge date and *not* the flag-flip time. **The clock does not
run while `DEX_DISCOVERY_ENABLED` is false** — today it has not started. Once
that row exists, record its timestamp as T0 in the ops log; the **first ripe
checkpoint is ≈ `T0 + 11 days`**. Do not pin a calendar date in advance —
read T0 from the ledger, then add 11 days.

**Abort/rollback at any step:** `DEX_DISCOVERY_ENABLED=false` + restart (lane
is byte-identical off), and remove the cron gate var. Nothing else changes.

---

## Standing items

1. **`COINGECKO_RATE_LIMIT_PER_MIN` raise** (25 → ~50–100) once pro-tier
   burn-rate data is in (couples with A7).
2. **CG key rotation** — see A9 / Part B step 0; the current key appeared in
   session transcripts. This is the gating pre-req for the DEX window.

(PR #467 is **merged** — squash-merged 2026-07-21 01:20Z at master
`9690df1a`; no longer a pending decision.)
