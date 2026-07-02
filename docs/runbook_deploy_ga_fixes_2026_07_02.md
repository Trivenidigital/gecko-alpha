# Runbook — Deploy GA-fix batch to srilu (merges of 2026-07-02)

**Scope:** master `0372a699` — five squash merges: #403 (hygiene: busy_timeout bootstrap, task-exception logging), #402 (GA-19 ingest-watchdog persistence, **adds table `ingest_watchdog_state`, schema_version 20260703**), #399 (GA-03 schedules held-position-price + revival-verdict watchdogs), #404 (GA-01 unpriceable-position safety: dispatch gate + expiry-anomaly alert + stats exclusion), #401 (GA-05 delivery-claim ordering).
**Operator-executed.** The review session does not deploy. Prod is currently at `2e28fbaf`.

## Pre-deploy checklist
1. `systemctl is-active gecko-pipeline gecko-dashboard` → both `active`.
2. Note deployed HEAD: `cd /root/gecko-alpha && git rev-parse HEAD` (expect `2e28fbaf…`).
3. Confirm last nightly backup exists (03:00 rotation): `ls -la scout.db.bak.*` — the #402 migration is additive-only, so no special backup is required beyond the nightly.
4. (Independent but recommended first) GA-01 containment applied: `sqlite3 scout.db "SELECT enabled, suspended_reason FROM signal_params WHERE signal_type='tg_social';"` → `0|ga01_containment_operator`.

## Deploy sequence
```bash
cd /root/gecko-alpha
git pull                                  # → 0372a699
uv sync                                   # no new deps expected (pyproject untouched)
find . -name __pycache__ -type d -exec rm -rf {} +   # stale-.pyc lesson
systemctl restart gecko-pipeline          # dashboard NOT touched by this batch; no dashboard restart
bash cron/deploy.sh                       # installs the 2 new watchdog entries (managed block 7→9)
```
Note: `git pull` also brings PR #390's narrative-observability code (merged 06-30, not yet deployed) and everything else on master since `2e28fbaf`. This runbook's verification covers the GA batch; #390 has its own runbook (`tasks/plan_narrative_resolve_pr390_deployment.md`).

## What restarts / blast radius
- **gecko-pipeline restart:** ~10–20 s detection gap; paper-trade opens blocked for 180 s (PAPER_STARTUP_WARMUP_SECONDS); the boot runs the additive migration (<1 s). Watchdogs that key off pipeline output tolerate a single restart.
- **New per-cycle writes:** ingest-watchdog persist = one UPSERT per source per cycle (~6 rows/min) — negligible WAL churn.
- **Two new cron jobs:** held-position-price watchdog (*/5, read-only sqlite + curl on alert), revival-verdict watchdog (daily 09:30).
- **Behavior changes armed immediately:** unpriceable dispatch gate (PAPER_REQUIRE_PRICEABLE_TOKEN_ID defaults True — inert while tg_social is disabled); expiry-anomaly TG alerts; candidate/velocity delivery-claim ordering; auto_suspend/calibrate/combo stats now exclude `expired_stale_no_price` rows (retroactive — the 12 historical rows already carry that exit_reason).
- **No .env changes required.** New Settings (SQLITE_BUSY_TIMEOUT_MS=90000, PAPER_REQUIRE_PRICEABLE_TOKEN_ID=True) have correct defaults.

## Post-deploy verification
```bash
# 1. Migration applied
sqlite3 scout.db "SELECT version, description FROM schema_version WHERE version=20260703;"
#    → 20260703|ingest_watchdog_state_v1

# 2. Boot hydration + no migration failures
journalctl -u gecko-pipeline --since '-10 min' | grep -E "ingest_watchdog_state_hydrated|schema_migration_failed|settings_validation_failed"
#    → one hydrated line (sources=0 on first boot), NO failure lines

# 3. Watchdog counters accumulating (wait 2-3 cycles, ~3 min)
sqlite3 scout.db "SELECT * FROM ingest_watchdog_state;"
#    → one row per ingest source, mostly consecutive_misses=0

# 4. Cron entries installed
crontab -l | grep -cE "held-position-price-watchdog|revival-verdict-watchdog"   # → 2

# 5. Watchdog smoke runs
bash scripts/held-position-price-watchdog.sh; echo "exit=$?"
#    EXPECTED while trade 2613 is still open (until ~2026-07-04 12:50Z): it has NO
#    price_cache row, so an alert about it is CORRECT behavior (exit 1, TG message),
#    not a deploy failure. After 2613 expires: exit 0.
bash scripts/revival-verdict-watchdog.sh; echo "exit=$?"    # → exit 0, expired_count=0

# 6. GA-01 expiry-anomaly alert wiring (event, not immediate):
#    when 2613 force-closes (~2026-07-04 12:50Z) expect journalctl pair
#    trade_expiry_anomaly_alert_dispatched / _delivered + a plain-text TG message.
#    (2610 expires ~13:23Z 2026-07-02 — if deploy lands after that, it closes
#    silently under old code as a 13th fabricated row; it is already covered by
#    the retroactive stats exclusion. No action needed.)

# 7. Candidate alert path unchanged in the success case
journalctl -u gecko-pipeline --since '-30 min' | grep -c alert_delivered   # >0 if any alerts fired

# 8. Stats exclusion live (auto-suspend sees real numbers)
sqlite3 scout.db "SELECT COUNT(*), ROUND(SUM(pnl_usd),2) FROM paper_trades WHERE signal_type='tg_social' AND status LIKE 'closed_%' AND COALESCE(exit_reason,'') != 'expired_stale_no_price';"
#    → 9|-488.93 (real rows only; 12 fabricated rows excluded)
```

## Rollback
```bash
cd /root/gecko-alpha && git checkout 2e28fbaf && find . -name __pycache__ -type d -exec rm -rf {} + && systemctl restart gecko-pipeline
# The ingest_watchdog_state table + schema_version/paper_migrations rows are additive;
# old code ignores them — do NOT drop. Cron revert:
sed -i '/held-position-price-watchdog/d;/revival-verdict-watchdog/d' cron/gecko-alpha.crontab && bash cron/deploy.sh
```

---

# Deploy #2 — Phase 6 batch (ledger 20260704 + provenance/stale-onset 20260705 + cockpit slice 1)

**Scope:** #406 `signal_outcome_ledger` + `ledger_enrollments` (migration 20260704, module scout/outcome_ledger.py, default-ON writers/labeler/poller); #408 five nullable `paper_trades` columns + backfills (migration 20260705), price-source open invariant, exit provenance, stale-onset exit (STALE_ONSET_EXIT_HOURS default 6); #407 dashboard cockpit slice 1 (no schema).
**Ordering (operator-executed, both):** deploy-#1 (runbook above, pre-#406 snapshot `0372a699`) PRECEDES deploy-#2. Deploy-#2 ships only after PRs #406/#408 merge.
**Staged-deploy compatibility:** deploy-#1 and deploy-#2 may be applied together or separately in either staging — `initialize()` orders migrations 20260703 → 20260704 → 20260705; each is independently guarded/idempotent/additive. A combined single deploy applies all three in sequence.

## Upgrade vectors (two-vector review summary, both migrations)
- **Fresh install:** both CREATE IF NOT EXISTS + sentinel; clean.
- **Upgrade with existing data (srilu, 2.33GB) — ENGINE-STOPPED WINDOW REQUIRED:** perform deploy-#2 with BOTH units stopped so the 20260705 EXCLUSIVE backfill cannot contend with live readers/writers:
  ```bash
  # 0. STOP FIRST — the snapshot must be taken with the engine quiescent:
  #    a close landing between snapshot and stop would shift a count and
  #    false-abort the assertion (reviewer catch, 2026-07-02).
  systemctl stop gecko-pipeline gecko-dashboard
  # 1. PRE-DEPLOY SNAPSHOT (abort-reference for the assertion below)
  sqlite3 scout.db "SELECT COALESCE(exit_reason,'(null)'), COUNT(*) FROM paper_trades WHERE status LIKE 'closed_%' GROUP BY 1;" | tee /root/pre_deploy2_exit_reasons.txt
  git fetch && git checkout <deploy-2 pin SHA>   # pin discipline; NOT git pull
  # PIN SOURCE OF TRUTH: the #408 merge report entry in
  # tasks/gecko-alpha-fable-review_2026_07.md (approvals log) states the exact
  # SHA, minted as post-#408-squash master (the last deploy-#2 code gate).
  # Never substitute "current master" — that is git pull with extra steps.
  uv sync && find . -name __pycache__ -type d -exec rm -rf {} +
  systemctl start gecko-pipeline        # boot runs 20260704 + 20260705 (<2s)
  # POST-MIGRATION ASSERTION — abort deploy on mismatch:
  sqlite3 scout.db "SELECT exit_provenance, COUNT(*) FROM paper_trades WHERE status LIKE 'closed_%' GROUP BY 1;"
  #   REQUIRED: entry_fallback count == pre-snapshot expired_stale_no_price count
  #             (12 as of 2026-07-02 13:20Z; +1 per additional such close before deploy, e.g. trade 2610)
  #             stale_snapshot count == pre-snapshot expired_stale_price count (128 baseline)
  #             market == all remaining closed rows; NULL only on OPEN rows
  #   plus: sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades WHERE price_source IS NULL;"  # MUST be 0
  # MISMATCH => ABORT: systemctl stop gecko-pipeline; git checkout <pre-deploy SHA>; restart; investigate before retry.
  systemctl start gecko-dashboard       # only after the assertion passes
  ```
  20260704 creates two empty tables + indexes (<1s); 20260705 backfills ~2,270 closed rows in one EXCLUSIVE pass (sub-second at current size).
- **Rollback:** git checkout previous SHA + restart. All additions are nullable columns / new tables the old code never reads — do NOT drop. Ledger rows written before rollback are inert. Re-deploy re-skips via sentinels.

## Behavior armed at restart (no .env edits required)
- **Ledger default-ON** (`LEDGER_ENABLED=True`): records delivered alerts, paper-trade opens, 1-in-25 sampled gate blocks (each row carries anchor cache-age + enrollment_status per operator condition (c)); hourly labeling pass; per-cycle enrollment poller = at most ONE extra CG /simple/price batch per cycle (~3% of the 30/min Demo budget) + DexScreener batches (separate budget). Kill switch: `LEDGER_ENABLED=False` + restart.
- **Stale-onset exit ON** (STALE_ONSET_EXIT_HOURS=6): open positions whose price_cache mark goes >6h stale exit at last-good price with `closed_stale_onset`, mark provenance recorded, operator TG alert (parse_mode=None). NOTE: no-price positions (no cache row at all, e.g. trade 2613) still freeze by design — they exit only at max_duration with `entry_fallback` provenance + alert.
- **Open invariant:** paper opens now stamp `price_source`; unpriceable token_ids blocked (was #404's gate, now also model+column-enforced).
- **Dashboard:** integrity chips / 7d window labels / live-joined Signal Trust appear after `systemctl restart gecko-dashboard` + pycache clear (this batch DOES touch dashboard — restart both units).

## Post-deploy verification
```bash
sqlite3 scout.db "SELECT version, description FROM schema_version WHERE version IN (20260704,20260705);"   # both rows
sqlite3 scout.db "SELECT exit_provenance, COUNT(*) FROM paper_trades WHERE status LIKE 'closed_%' GROUP BY exit_provenance;"  # 12 entry_fallback / 128 stale_snapshot / rest market
sqlite3 scout.db "SELECT COUNT(*) FROM paper_trades WHERE price_source IS NULL;"   # 0 (all backfilled 'legacy' or stamped)
sqlite3 scout.db "SELECT kind, COUNT(*) FROM signal_outcome_ledger GROUP BY kind;" # dispatch/gated_out_sample rows within minutes; alert rows only when funnel reopens
journalctl -u gecko-pipeline --since '-2 hour' | grep -cE "ledger_label_pass|ledger_enrollment_poll"   # >=1 each after an hourly pass
# WEEKLY after deploy-#2 (journal-head-age check — #413 deadline governor):
journalctl -u gecko-pipeline --no-pager 2>/dev/null | head -1   # note the oldest-entry date
#   If oldest-entry age starts DROPPING week-over-week, rotation is accelerating
#   under the new write volume and the BL-NEW-LEDGER-EVICTION-DB-MARKER deadline
#   tightens accordingly (the ~24d window was measured at pre-deploy rates).

# WEEKLY after deploy-#2 (eviction-record export — interim durability until the
# DB-marker slice lands; the journal is the lossy store, this is the idempotent copy):
mkdir -p /var/lib/gecko-alpha && journalctl -u gecko-pipeline --since '-8 days' -o json 2>/dev/null | grep ledger_enrollment_evicted >> /var/lib/gecko-alpha/ledger_eviction_export.jsonl
#   -o json = journald envelope per line (guaranteed JSONL even if an emitter
#   ever logs non-JSON; the structlog event sits in .MESSAGE). Append-only; the
#   1-day overlap can duplicate lines — dedup at read time on
#   (.__REALTIME_TIMESTAMP, .MESSAGE). Cron automation of this line is a separate
#   small ops PR if wanted; manual-weekly keeps this docs-only.

# Event to expect: trade 2613 force-closes ~2026-07-04T12:50Z -> trade_expiry_anomaly_alert_dispatched/_delivered pair + plain-text TG message; its row gets exit_provenance='entry_fallback'.
```

## Interaction note (operator-accepted risk surface)
The ledger's dex poller is the `dex:` namespace's first price_cache writer (labeling lane). While a dex token is enrolled (7d TTL), the #404/#408 open gate's "price_cache row exists" branch can admit it for paper trading; if its price support then dies, the position exits via stale-onset (~6h, alerted, provenance-labeled) instead of the old silent 168h fabrication. Bounded and observable — but it means dex paper trades become possible again if tg_social is re-enabled while a token is enrolled. Gate-tightening option exists if churn appears (require price_source='cg_lane' OR fresh-cache-within-X).
