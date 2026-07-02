# Runbook — X Performance Accrual writer + watchdog activation (#392)

**Status:** PREPARED — do **NOT** execute. Activation is a separate operator
decision (requires deploying a new runtime SHA to `srilu` and adding crons)
and must **not** happen during the active DEX observe-only soak without
explicit approval.

**Build state (2026-07-01):** C0–C4 all merged to `master`, **none deployed**.
The writer and watchdog crons are **default-off**. `srilu` is running the
pre-C1 SHA, so none of the accrual pipeline is live yet.

---

## 0. What "activation" actually means (two independent steps)

Because nothing is deployed, activation is **two** operator decisions, in order:

1. **Deploy** merged `master` to `srilu` (git pull + `__pycache__` clear +
   service restart). This changes the runtime SHA — the reason it's soak-gated.
   Migrations (`source_call_price_snapshots`, `source_call_price_snapshot_runs`)
   run idempotently on service start; they are additive and safe.
2. **Enable** the crons (flip flags + install cron entries) so the pipeline
   actually accrues data.

Deploying without enabling is inert (writer/watchdog stay off). Enabling
without deploying does nothing (the code isn't there). **Both** are required
to accrue; **both** are blocked until approved and out of the soak.

---

## 1. Env vars / cron entries

### 1a. Snapshot writer (accrues forward prices)
- **Flag (Settings/.env):** `SOURCE_CALL_SNAPSHOT_WRITER_ENABLED=true`
  (default `False`; declared in `scout/config.py`).
- **Optional (.env):** `SOURCE_CALL_SNAPSHOT_HORIZON_HOURS` (default 28 —
  the 24h forward window closes at call+28h).
- **Optional (env):** `SCPS_WRITER_HEARTBEAT_FILE=/path/to/scps_writer.heartbeat`.
- **Cron (≤15 min):**
  ```
  */15 * * * * /root/gecko-alpha/scripts/source-call-price-snapshots-writer.sh >> /var/log/scps-writer.log 2>&1
  ```
  The wrapper sources `.env`, reads `SOURCE_CALL_SNAPSHOT_WRITER_ENABLED` /
  `SOURCE_CALL_SNAPSHOT_HORIZON_HOURS`, and passes `--enabled` / `--horizon-hours`.
- **Prerequisite:** the existing `source_calls_live_writer` cron must be running —
  its `refresh_source_call_outcomes` is what prices `eligible_contract` rows
  (C3) from the snapshots the new writer records.

### 1b. Coverage watchdog (alarms on silent failure)
- **Flag (CRON ENV — deliberately NOT a Settings field, not in `.env`):**
  `SOURCE_CALL_COVERAGE_WATCHDOG_ENABLED=true`.
- **Cron (e.g. every 30 min):**
  ```
  */30 * * * * SOURCE_CALL_COVERAGE_WATCHDOG_ENABLED=true /root/gecko-alpha/scripts/source-call-coverage-watchdog.sh >> /var/log/scps-watchdog.log 2>&1
  ```
  The flag lives on the cron line (cron environment), so it never touches the
  Settings surface (`extra="forbid"` safe). The wrapper still sources `.env`
  for Telegram credentials used by the alert path.

---

## 2. Default-off verification (BEFORE enabling — run manually on `srilu`)

Confirm both scripts are inert with the flags absent:
```bash
cd /root/gecko-alpha
.venv/bin/python scripts/source_call_price_snapshots_writer.py --db scout.db
#   expect: {"ok": true, "skipped": "writer_disabled"}   exit 0

.venv/bin/python scripts/source_call_coverage_watchdog.py --db scout.db
#   expect: {"ok": true, "skipped": "watchdog_disabled"}  exit 0
```
Both must print `skipped` and exit 0 (no DB write, no network). If either does
real work with no flag set, STOP — do not enable.

---

## 3. First-hour verification (AFTER enabling)

Within the first 1–2 cycles (~30 min):

1. **Writer is recording runs:**
   ```sql
   SELECT COUNT(*), MAX(ran_at) FROM source_call_price_snapshot_runs;
   ```
   Row count > 0 and `MAX(ran_at)` within the last cadence window.
2. **Snapshots are landing (if any active eligible_contract CA calls exist):**
   ```sql
   SELECT COUNT(*), MAX(snapshot_at) FROM source_call_price_snapshots;
   ```
3. **Pricing hookup fired (C3):** an `eligible_contract` X call now has a price:
   ```sql
   SELECT source_event_id, resolved_state, price_at_call, outcome_status
   FROM source_calls
   WHERE source_type='x' AND resolved_state='eligible_contract'
     AND price_at_call IS NOT NULL LIMIT 5;
   ```
   (Only expected once a snapshot exists within `[call_ts, call_ts+900s]`.)
4. **Watchdog cleared suppression:** once runs exist, `writer_freshness` should
   read `ok` (not `suppressed`), and `provider_error_spike` `ok`/`suppressed`
   (not `alert`) unless GT is genuinely failing. Inspect the watchdog stdout JSON
   or `journalctl` for `scps_writer_cycle` / `source_call_coverage_watchdog_*`.

---

## 4. Rollback (fully reversible, no schema rollback needed)

- **Writer:** set `SOURCE_CALL_SNAPSHOT_WRITER_ENABLED=false` in `.env` (or remove
  the cron) → next cycle is an inert no-op. Restart not required for cron; the
  wrapper re-reads `.env` each run.
- **Watchdog:** remove the cron line or set the cron-env flag to `false`.
- **Data:** the `source_call_price_snapshots` / `_runs` tables and any priced
  `eligible_contract` rows are harmless observability data — leave them. The
  migrations are additive + idempotent; there is nothing to un-migrate.
- **Code:** if the deploy itself must be reverted, roll `srilu` back to the
  pre-C1 SHA; the additive tables simply go unread again.

---

## 5. Expected table growth (bound the blast radius)

- `source_call_price_snapshot_runs`: **1 row per writer cycle** → ~4/hr at 15-min
  cadence (~96/day). Trivial.
- `source_call_price_snapshots`: **≈ (distinct active CA identities) × (cycles)**.
  Per the inventory (`findings_x_assets_called_inventory_2026_06_30.md`): ~20
  distinct CA identities, only calls within the 28h horizon are snapshotted, so
  steady-state ≈ 20 identities × ~112 cycles/28h ≈ low thousands of rows, then
  flat (old calls age out of the horizon). If growth is materially higher than
  this, investigate an over-broad selection.
- No growth on `source_calls` row count (C3 only updates existing rows in place).

---

## 6. Watchdog routing

- Alerts go to **`TELEGRAM_CHAT_ID`** (the main operator chat) via
  `send_telegram_message(..., parse_mode=None, source="source_call_coverage_watchdog")`.
  `parse_mode=None` is mandatory — check names contain `_` (`fresh_calls_no_snapshots`,
  `provider_error_spike`) which MarkdownV1 would mangle (§12b / Class-3).
- Each send is bracketed by `source_call_coverage_watchdog_alert_dispatched` and
  `_alert_delivered` structured logs (journalctl-traceable). A send failure logs
  `_alert_failed`, prints `{"ok": false, "error": "alert_dispatch_failed"}`, and
  exits 1 — it is surfaced, not swallowed.
- **Exit codes:** `0` = ok (no alerts), `5` = one or more alerts fired (and
  dispatched), `1` = DB missing / runtime / alert-dispatch failure. Cron may
  treat non-zero as the alert signal; findings are also in the stdout JSON and
  journalctl regardless.
- **Optional routing to a health channel:** `send_telegram_message` accepts a
  `chat_id=` override; the watchdog script does not set one today. If a separate
  ops/health channel is wanted, thread a `--chat-id` arg through the script (a
  small future change; not required for activation).

---

## 7. Distinguishing expected `INSUFFICIENT_SAMPLE` from real failure

This is the most important operational nuance. **Low data volume is EXPECTED and
is NOT a failure.**

| Signal | Meaning | Action |
|---|---|---|
| C5 ranking would show `INSUFFICIENT_SAMPLE` | Too few CA-resolved-24h calls per influencer (design §8: only ~4 KOLs have ≥8 CA calls; CA-only top-10 is likely structurally impossible) | **Expected. Do nothing.** This is the honest accrual ceiling, not a bug. |
| `matured_all_null` fires **right after enable** | Pre-existing `eligible_contract` calls that already aged past 28h before the writer ever ran → they can never get a price | **Expected one-time.** These historical calls are permanently unpriceable (forward-only). Not a failure. |
| `matured_all_null` fires for calls that were **fresh after enable** | Calls that were live while the writer was running still ended with no price/forward | **Investigate** — the writer or GT pricing isn't covering active calls. |
| `writer_freshness=alert` | Writer cron stopped (runs table stale) | **Investigate** — cron/service outage. |
| `fresh_calls_no_snapshots=alert` | Writer ran but wrote **0** snapshots while active eligible calls exist | **Investigate** — CA→pool resolution or GT fetch broken (check `provider_error_spike`, `pools_unresolved`). |
| `provider_error_spike=alert` | GT provider-error rate high over recent runs | **Investigate** — GT availability / rate-limit. |
| `eligible_no_snapshots` high **and rising** | Coverage not filling over time | **Investigate** — as above. A small, stable count is fine. |

**Rule of thumb:** `INSUFFICIENT_SAMPLE` / low coverage = *not enough data* (expected,
small cohort). A **watchdog `alert`** = *pipeline broken* (investigate). Never
promote a thin ranking to hide `INSUFFICIENT_SAMPLE` (design §5 non-goal).

---

## 8. Standing guardrails (unchanged by activation)

Activation enables **data accrual only**. It does **not** unblock: C5 ranking
(stays gated on N-gates + real data), any scoring/gate/threshold/trading-alert
change, cashtag resolver, fuzzy matching, paid feeds, or DEX-soak logic. The
snapshot writer never writes trading signals; the watchdog only reads.
