# F3 — Durable alert-event ledger (`alert_events`)

**New primitives introduced:** `alert_events` table (append-only control-plane event ledger, migration `20260814`); `scout/trading/alert_events.py` writer module (`record_alert_event`); `refresh_completed` heartbeat event; `ALERT_EVENTS_SLO_HOURS` watchdog check in `scripts/alert_channel_watchdog.py`.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Durable audit/event ledger (SQLite) | none found (hub catalog is JS-rendered; static fetch 2026-08-15 returned no entries; adopted Hermes skills in this project are solana / rest-graphql — unrelated) | build in-house |
| Alert-delivery tracking | none found | build in-house (extends existing §12b triplets) |
| journald retention | n/a — handled as system config (drop-in applied 2026-08-15, see §7) | no code |

awesome-hermes-agent ecosystem: nothing covering in-process SQLite audit ledgers. Verdict: custom code warranted.

## Authority

Operator ruling 2026-08-15 (session restart brief): F3 HIGH — fix now, two layers. DB migration/deploy authorized autonomously given capacity checks, rollback, exact-head CI, migration tests, runtime verification. No TG/trading-policy changes.

## Problem

Prod journald retention collapses to minutes during the daily 03:00 backup window (KeepFree vacuum; verified 2026-08-15). Worse (mapping 2026-08-15): the four decisive suppression transitions (initial latch, re-latch, clear, parole re-arm) and the parole slot decrement emit **no log event at all** — they exist only as mutated `combo_performance` rows. Suppression/retest/alert acceptance evidence is therefore unreconstructable after the fact.

## Design

### Table (append-only; no prune — deliberate, rows are rare and tiny)

```sql
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,          -- write/attempt time, UTC ISO space-form
    event_type TEXT NOT NULL CHECK (event_type IN (
        'suppression_transition',      -- any _refresh_combo_locked state branch
        'parole_slot_spent',
        'parole_slot_refunded',        -- incl. stale-generation leak (delivery_result='stale_generation')
        'reversal_pending_recorded',
        'alert_dispatched','alert_delivered','alert_failed',
        'marker_stamped','marker_cleared','marker_anomaly',
        'refresh_completed'            -- 1/run heartbeat carrying summary counts
    )),
    combo_key TEXT,
    signal_type TEXT,
    alert_source TEXT,                 -- docs/alert_registry.md source label for alert_* rows
    transition TEXT,                   -- branch label / marker kind. AS SHIPPED the vocabulary is
                                       -- _classify_reversal's, reused verbatim on both axes:
                                       -- newly_suppressed|parole_exhausted_resuppressed|clear|page_rearm
                                       -- plus the preserve-branch classifications
                                       -- waiting|contaminated|accounting_inconsistent|
                                       -- terminal_incomplete_held|parole_stalled, and the marker
                                       -- kinds (column names) for marker_* rows.
                                       -- (The initial_latch|first_write_latch|relatch spelling in
                                       -- the original draft was renamed during review and never
                                       -- shipped; first-write is newly_suppressed + detail="first_write".)
    detected_at TEXT,                  -- transition/detection ts (payload-bound where applicable)
    delivery_result TEXT,              -- alert_*: 'ok' | 'error:<ExcType>'; anomalies: reason
    retry INTEGER,                     -- alert rows: 0/1 (payload detected_at != run_iso)
    payload_hash TEXT,                 -- sha256 hex of the exact payload/message text
    state_json TEXT,                   -- before/after generation triple {suppressed, suppressed_at,
                                       -- parole_at, parole_trades_remaining} + marker state
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_events_combo_created ON alert_events(combo_key, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_events_type_created ON alert_events(event_type, created_at);
```

Migration `_migrate_alert_events_v1`, version **20260814** (next free per `docs/migration_versions.md` — register in the SAME PR; CI-enforced). House rules: `BEGIN EXCLUSIVE`, indexes inside the migration step, rollback-on-BaseException, post-DDL verification. Follow `_migrate_tg_act_shadow_v1` (`scout/db.py:5984-6153`) as the template.

### Writer — `scout/trading/alert_events.py`

`async def record_alert_event(db, *, event_type, combo_key=None, ..., managed_txn=False)`:
- `managed_txn=True`: caller already holds `db._txn_lock` inside an open transaction → plain `INSERT`, no commit (atomic with the state change it records).
- `managed_txn=False`: acquire `db._txn_lock`, `INSERT` + `commit()`.
- **Never raises.** A ledger failure must never block the control path: catch, `log.error("alert_event_write_failed", err_id="ALERT_EVENT_WRITE", event_type=..., combo_key=...)`. (Exception-swallow is deliberate and must be tested — see lessons re `except:` blocks.)
- `payload_hash`: `hashlib.sha256(text.encode()).hexdigest()` — never `Decimal.normalize()`-style ambient formatting; hash the exact sent string.

### Insertion sites (from mapping 2026-08-15; verify line refs at head)

In-txn (`managed_txn=True`, immediately before the commit at `combo_refresh.py:532`):
1. `suppression_transition` — one row for whichever branch fired (360-366, 370-375, 407-412, 413-418, 423-449 hold, 493-501 re-arm), with `state_json` = before/after generation triple, `transition` = branch label, `detected_at` = `now_iso`.
2. `refresh_completed` — one row per `refresh_all` run (write at summary time, own short txn), `state_json` = the `combo_refresh_summary` counts. Guarantees ≥1 row/day for §12a.

In-txn at `suppression.py:191-201` (same txn as the slot UPDATE): `parole_slot_spent`, `state_json` = generation triple + remaining-after.
Refund path `suppression.py:363-433`: `parole_slot_refunded` (ok / stale_generation / clamped detail). **Also migrate `total_changes` → `cur.rowcount` at 394-406** (behavior-preserving correctness on the instrumented path; test it).

Self-committed short-txn sites (delivery axis, reacquire `_txn_lock`, `cur.rowcount` convention):
3. `reversal_pending_recorded` at `combo_refresh.py:867-880` (in the same pass/txn is fine — it IS the pending txn; superseded case → `detail`).
4. `alert_dispatched`/`alert_delivered`/`alert_failed` bracketing the three suppression-axis senders (reversal :996-1021, terminal-incomplete :115-126, permanent-suppression :1165-1179) with `alert_source` = existing registry labels, `retry`, `detected_at`, `payload_hash` of the exact message.
5. `marker_stamped` (perm :1183-1188, incomplete :134-145), `marker_cleared` (payload-bound clear :1027-1052), `marker_anomaly` (stale-generation :160-165, clear-missed :1036-1052 neutral case, unreadable-pending :962-979).
6. `auto_suspend.py`: bracket `_send_suspend_alert` (175-212) with `alert_dispatched`/`alert_delivered`/`alert_failed` (`signal_type` axis, `combo_key` NULL) **and add `raise_on_failure=True`** to its `send_telegram_message` call so `_delivered` (log + ledger) can no longer stamp on a swallowed non-200 — same #525 class, ruled precedent. The durable state change itself already has `signal_params_audit`; do not duplicate it.
7. `suppression.py` `_record_fallback` TG send (:471): bracket with dispatched/delivered/failed rows (`alert_source="suppression"`).

Out of scope: kill_switch (live-money adjacent), tg_alert_dispatch (already has its own durable outcome ledger `tg_alert_log`), `_record_suppressed_ledger_emission` (stays as-is beside the new ledger; different concern — sampling vs control-plane).

### §12a registration

- `scripts/alert_channel_watchdog.py`: add `alert_events` freshness check keyed on `refresh_completed` age (SLO default 27h, env `ALERT_EVENTS_SLO_HOURS`), riding the existing cron line + per-table cooldown; wire the flag in `scripts/alert-channel-watchdog.sh`.
- Dashboard: append `("alert_events","created_at")` to the hardcoded table list at `dashboard/db.py:888-911`. Do NOT populate `HEALTH_FRESHNESS_SLO_MINUTES` (ships empty by operator decision).

### Constraints

- No new `send_telegram_message` source labels → no `docs/alert_registry.md` additions needed; if any sender wrapper changes signature-adjacent text, keep registry test green.
- No TG activation / policy / threshold / geometry changes. `raise_on_failure=True` on auto_suspend's system-health alert is a delivery-truthfulness fix (ruled class #525), not a policy change; auto_suspend's *state* behavior is untouched (send already happens outside the txn; a raise propagates to `maybe_suspend_signals`' caller — verify the scheduler containment at `scout/main.py:357-372` and add a containment test so a failed page cannot crash the pipeline loop: catch at the call site, log loudly, state stays applied — matching #525's "raise, not stamp" semantics at the marker layer).
- Tests: migration (fresh + OLD-shape upgrade with the load-bearing "fixture is not the OLD shape" assert + idempotent double-initialize), writer fail-soft (both txn modes; forced INSERT failure logs and does not raise or poison the outer txn), one test per insertion site asserting existence + COUNT (never `if captured:`), delivered-vs-failed truthfulness for auto_suspend (mock alerter non-200 with `spec=`), rowcount-migration test for the refund path, watchdog check unit test.

## 7. journald layer (already applied, 2026-08-15 ~01:32Z)

`/etc/systemd/journald.conf.d/90-gecko-retention.conf`: `SystemMaxUse=2G`, `SystemKeepFree=1G`, `SystemMaxFileSize=128M`. Measured: steady ~34MB/h → ~59h; the 03:00 backup transient (~7.4G, create-then-rotate) was the daily vacuum trigger; 3.2G of regenerable caches freed to restore headroom. Residual: disk erodes ~0.6-0.7G/day (DB growth × 4 copies) — operator capacity flag. Rollback: rm drop-in + restart journald. The DB ledger above is the real durability fix.
