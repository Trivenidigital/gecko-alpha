# Runbook — Deploy #3 (folds in #424 per operator ruling 2026-07-03)

**Operator-executed. Procedure at master HEAD; code at the pin.** Prior deploys: #1 (0372a699), #2 (b23ef0a2). Deploy-#3 supersedes the earlier "deploy-#3 prep" notes.

## Scope (merged, awaiting this deploy)
- #421 (dispatcher-suppressed recall lane) + #423 (liveness coverage) — code-only, NO new migration (both build on #406's 20260704, already live at deploy-#2).
- #420 (ledger liveness heartbeat) — code-only.
- #422 (systemd deterministic dashboard stop) — unit file; needs `daemon-reload`.
- #419 (watchdog +x + CI guard) — already merged; its +x lands in-tree at any post-#419 checkout.
- **#424 (frozen-suppression-lock)** — folded in per operator ruling; the ONLY migration in this batch.

## PIN
**Post-#424-merge master SHA — minted at #424's merge, recorded in the #424 merge report** (same discipline as the #408→deploy-#2 pin). `<deploy-3 pin>` = that SHA; never substitute current master.

## Migration (the "confirm migration-free" line is REPLACED by this)
#424 adds one column: `combo_performance.perm_suppression_alerted_at TEXT` (nullable), via a **PRAGMA-guarded idempotent ALTER** at boot inside `initialize()`.
- **Quiescence: LIVE-SAFE — no engine-stopped window required.** SQLite `ALTER TABLE ADD COLUMN` on a nullable column is O(1) metadata-only (no table rewrite, no long lock), and there is **NO backfill** (unlike #408's EXCLUSIVE 2,270-row backfill which required the stopped window). It runs during the normal restart. A brief `busy_timeout` (90s, set at bootstrap per #403) covers the metadata write.
- Fresh install: column is in `CREATE TABLE`. Upgrade: guarded by `PRAGMA table_info` check → idempotent. Rollback: nullable column old code ignores — do NOT drop.

## Pre-checkout (from the earlier prep — the export-script trap)
```bash
rm -f /root/gecko-alpha/scripts/ledger-eviction-export.sh   # remove the untracked step-4 manual copy so checkout lands the tracked +x version cleanly
```

## Deploy sequence
```bash
cd /root/gecko-alpha
git fetch && git checkout <deploy-3 pin>            # NOT git pull; pin from #424 merge report
uv sync && find . -name __pycache__ -type d -exec rm -rf {} +
systemctl daemon-reload                             # for #422 unit change
systemctl restart gecko-pipeline                    # boot runs the #424 column-add (live-safe, <1s)
systemctl restart gecko-dashboard                   # picks up #422 KillMode=mixed/TimeoutStopSec=20 + recall-lane dashboard reads
# --- export-script exec-bit closure (the untracked-copy trap, condition b) ---
ls -l scripts/ledger-eviction-export.sh             # MUST show -rwxr-xr-x (tracked +x via #419 lands on checkout)
bash scripts/ledger-eviction-export.sh              # one manual run — expect: ledger_eviction_export_run status=ok appended=0
#   (OR quote the next Monday-04:15 tick log line from /var/log/gecko-alpha-ledger-eviction-export.log)
```

## Post-deploy verification
```bash
# 1. #424 migration applied
sqlite3 scout.db "PRAGMA table_info(combo_performance);" | grep perm_suppression_alerted_at   # column present

# 2. #422 unit hardening live
systemctl show gecko-dashboard -p KillMode -p TimeoutStopSec   # KillMode=mixed, TimeoutStopSec=20s

# 3. Recall lane (#421) live: dispatcher-suppressed rows begin appearing in the ledger
sqlite3 scout.db "SELECT COUNT(*) FROM signal_outcome_ledger WHERE json_extract(gate_verdicts,'$.source_layer')='dispatcher';"   # >0 within a cycle or two

# 4. Liveness coverage (#423): a FRESH-covered token stamps not_needed (not enrolled) — one such row
sqlite3 scout.db "SELECT COUNT(*) FROM signal_outcome_ledger WHERE enrollment_status='not_needed';"   # >0 = liveness gate working (fresh-covered tokens skip enrollment); enrollments stay << 200 cap

# 5. Heartbeat (#420): poll/label heartbeat every pass even when empty
journalctl -u gecko-pipeline --since '-15 min' | grep -cE "ledger_poll_heartbeat|ledger_label_heartbeat"   # >=1

# 6. FROZEN-LOCK RETROACTIVE VISIBILITY (#424, condition a): the first nightly refresh_all
#    (03:00Z) after deploy alerts the pre-existing latched set. Expect §12b Telegram alerts +
#    journal permanent_suppression_alert_delivered for gainers_early, losers_contrarian
#    (latched since mid-May), and chain_completed once it latches (~07-04 03:00Z).
sqlite3 -header scout.db "SELECT combo_key, suppressed, perm_suppression_alerted_at FROM combo_performance WHERE suppressed=1 AND perm_suppression_alerted_at IS NOT NULL;"
journalctl -u gecko-pipeline --since '-1 day' | grep permanent_suppression_alert_delivered
#    Each alert fires ONCE (deduped). These convert silent artifact-latch → visible operator decision.
```

# 7. Exec-bit spot-check (#419 guard, post-checkout): every cron-invoked script executable
for f in held-position-price-watchdog revival-verdict-watchdog acceleration-heartbeat-watchdog ledger-eviction-export; do ls -l scripts/$f.sh | cut -c1-11; done   # all -rwxr-xr-x

## Rollback
`git checkout <prior pin>` + pycache clear + `daemon-reload` + restart both. The perm_suppression_alerted_at column is additive/nullable — leave it. Ledger tables/columns from #406 are already live (deploy-#2) and unaffected.
