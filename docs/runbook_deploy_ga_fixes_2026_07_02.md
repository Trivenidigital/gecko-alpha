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
