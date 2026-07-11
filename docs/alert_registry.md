# Alert Registry (ALR-06)

Canonical enumeration of every Telegram alert kind the pipeline can emit. An
"alert kind" is the string-literal `source=` label passed to
`alerter.send_telegram_message(...)` (scout/alerter.py:140) — the label used for
TG-burst attribution and `telegram_message_delivered` observability.

**Coverage is CI-enforced.** `tests/test_alert_registry_coverage.py` AST-parses
every `send_telegram_message(...)` call in `scout/` and `scripts/`, extracts the
literal `source=` value, and asserts each one appears in the table below. A new
alert that ships without a registry row fails CI — future alerts must register.

Scope notes:
- The CI check covers **literal** `source=` values. One call site
  (`scout/api/internal_alert.py:126`) builds the label dynamically
  (`source=f"internal_alert:{payload.source}"`) — documented in
  [Dynamic labels](#dynamic-labels) but not asserted (its value is not statically
  resolvable).
- Shell/`curl`-direct watchdogs (e.g. `scripts/*-watchdog.sh`) do **not** go
  through `send_telegram_message` and are out of scope for this registry — see
  [Out of scope](#out-of-scope).

`parse_mode` column: `None` = plain text (the correct default for any body that
can contain signal names with `_`; CLAUDE.md §12b). `Markdown` = legacy MarkdownV1
(only for bodies whose user-data fields are `_escape_md`-escaped).

`§12b` column: `Y` = the call site emits an explicit `*_alert_dispatched` +
`*_alert_delivered` structured-log pair around the send. `N` = relies on the
shared alerter's built-in `telegram_message_delivered` log
(scout/alerter.py:274), which fires on every HTTP 200. Every alert that
**reverses operator-applied state** (`auto_suspend`, `kill_switch`,
`combo_refresh_permanent_suppression`) is `Y`, as §12b requires.

`Channel` column: `trading` = default `TELEGRAM_CHAT_ID`. `health` = routed to
`TELEGRAM_HEALTH_CHAT_ID` via the `chat_id=` override (config.py:289; empty →
falls back to trading). `Severity` is the proposed classification introduced by
this registry (used by the routing proposal at the bottom); it is documentation,
not yet a runtime field.

## Registry

| Kind | `source=` | Trigger | Channel | parse_mode | Severity | §12b |
|------|-----------|---------|---------|-----------|----------|------|
| Combo suppression reversal (§12b) | `combo_refresh_suppression_reversal` | Nightly refresh newly-suppresses or parole-exhausts a combo | trading | None | warning | Y |
| Early-detection candidate alert | `detection_alert` | Fresh CG candidate, early vs CG trending (lane flag-gated) | trading | None | info | Y |
| Alert-channel-death watchdog (script) | `alert_channel_watchdog` | No trading-chat alert delivered within the watchdog window | trading | None | warning | Y |
| Signal auto-suspension | `auto_suspend` | Tier-1b combined gate (hard_loss / drawdown) suspends a signal — **reverses operator-enabled state** | trading | None | warning | Y |
| Operator briefing | `briefing` | Periodic briefing synthesis | trading | None | info | N |
| Calibration dry-run (calibrate module) | `calibrate` | Per-signal calibration proposals | trading | None | info | Y |
| Legacy candidate alert | `candidate_alert` | Conviction-gate candidate alert (main.py gate → `send_alert`) | trading | Markdown | info | N |
| Chain-pattern completion | `chain_alert` | Chain signal pattern completes | trading | None | info | N |
| Cohort digest | `cohort_digest` | Weekly `would_be_live` cohort digest | trading | None | info | N |
| Permanent combo suppression | `combo_refresh_permanent_suppression` | Combo permanently suppressed after parole exhaustion — **state reversal** | trading | None | warning | Y |
| Conviction-watchlist watchdog | `conviction_watchlist_watchdog` | Conviction watchlist staleness | trading | None | warning | Y |
| Counter-signal risk | `counter_risk` | Counter-signal risk flag on a candidate | trading | None | warning | N |
| Daily summary | `daily_summary` | Daily pipeline summary | trading | None | info | N |
| DEX-instrumentation health | `dex_instrumentation_watchdog` | DEX instrumentation health degraded | **health** | None | warning | Y |
| Calibration dry-run (scheduler path) | `feedback_scheduler` | Calibration proposals from the feedback scheduler (main.py) | trading | None | info | N |
| Ingestion watchdog | `ingest_watchdog` | Ingestion source stalled (consecutive empty cycles ≥ threshold) | trading | None | warning | Y |
| Live kill switch | `kill_switch` | Live-trading kill switch tripped — **state reversal** | trading | None | critical | Y |
| Live decision | `live_decision` | Live-trade decision / dispatch notice | trading | None | info | N |
| Live startup | `live_startup` | Live subsystem startup announcement | trading | None | info | N |
| LunarCrush social (dead path) | `lunarcrush_social` | LunarCrush social alert (LunarCrush dropped — inert) | trading | Markdown | info | N |
| M1.5c announcement | `m1_5c_announce` | One-time M1.5c Minara feature announcement | trading | None | info | N |
| Narrative agent | `narrative_agent` | Narrative-agent alert | trading | None | info | N |
| Combo-refresh failure streak | `price_refresh_streak` | `combo_refresh` failed ≥3× consecutively | trading | None | warning | N |
| Second-wave detector | `secondwave` | Second-wave detector fires | trading | None | info | N |
| Source-call coverage watchdog (script) | `source_call_coverage_watchdog` | Source-call price-coverage staleness | trading | None | warning | Y |
| Stale SQLite reader watchdog | `sqlite_stale_reader_watchdog` | Stale reader pinning the WAL | trading | None | warning | Y |
| WAL checkpoint-busy watchdog | `sqlite_wal_checkpoint_busy` | WAL checkpoint repeatedly busy | trading | None | warning | Y |
| Suppression event | `suppression` | Suppression event alert | trading | None | warning | N |
| Suppression-cost rollup (script) | `suppression_cost_rollup` | Weekly suppression-cost rollup digest | trading | None | info | Y |
| Primary trader alert | `tg_alert_dispatch` | Paper-trade opportunity alert (the core trader alert) | trading | None | info | Y |
| TG allowlist announcement | `tg_allowlist_announce` | One-time TG allowlist announcement sentinel | trading | None | info | N |
| Trade-expiry anomaly | `trade_expiry_anomaly` | Trade-expiry anomaly detected | trading | None | warning | Y |
| Trade-surface actionability | `trade_surface_alerts` | Trade-surface actionability alert | trading | None | info | Y |
| Velocity detector | `velocity_alert` | Velocity detector fires | trading | Markdown | info | N |
| Weekly digest | `weekly_digest` | Weekly combo-oriented feedback digest | trading | None | info | N |
| Weekly alerts scoreboard (ALR-04) | `weekly_alerts_scoreboard` | Weekly sent-alert → paper-trade outcome scoreboard | trading | None | info | Y |

## Dynamic labels

| Kind | `source=` expression | Call site | Channel | parse_mode | §12b |
|------|----------------------|-----------|---------|-----------|------|
| HMAC operator-alert endpoint | `f"internal_alert:{payload.source}"` | scout/api/internal_alert.py:126 | trading | None | Y (`operator_alert_dispatched` / `_delivered`) |

The suffix is caller-supplied over the authenticated `/internal/operator-alert`
HTTP route, so the concrete label is not statically enumerable. The CI coverage
test skips non-literal `source=` values by design.

## Out of scope

Watchdogs that alert by `curl`-ing Telegram directly (not through
`send_telegram_message`) are intentionally excluded from this registry and its
CI check. They carry their own `*_alert_dispatched` / `*_alert_delivered` logs.
Known instances: `scripts/acceleration-heartbeat-watchdog.sh`,
`scripts/source-calls-lag-watchdog.sh`, `scripts/revival-verdict-watchdog.sh`,
`scripts/audit_stop_loss_false_negatives.sh`, `scripts/ledger-eviction-export.sh`.

## Findings / drift (ALR-06)

1. **Channel routing is ad-hoc.** Only `dex_instrumentation_watchdog` routes to
   the health chat; every other watchdog / health / state-reversal alert
   (`ingest_watchdog`, `sqlite_stale_reader_watchdog`, `sqlite_wal_checkpoint_busy`,
   `conviction_watchlist_watchdog`, `price_refresh_streak`, `auto_suspend`,
   `kill_switch`, plus the `scripts/` watchdogs) posts to the **trading** chat,
   interleaving operator-health noise with trade opportunities. This is the
   mis-routing ALR-06 was opened to surface.

2. **Proposed severity → channel routing table** (for a future centralized
   dispatch; not yet wired):

   | Severity | Channel | Kinds |
   |----------|---------|-------|
   | `critical` | health | `kill_switch` |
   | `warning` (health/ops) | health | all `*_watchdog`, `auto_suspend`, `combo_refresh_permanent_suppression`, `ingest_watchdog`, `sqlite_*`, `price_refresh_streak`, `trade_expiry_anomaly`, `counter_risk`, `suppression` |
   | `info` (trader-facing) | trading | `tg_alert_dispatch`, `trade_surface_alerts`, `candidate_alert`, `chain_alert`, `secondwave`, `velocity_alert`, `narrative_agent`, `live_decision`, digests/announcements |

3. **Three trader-facing alerts still use MarkdownV1** (`candidate_alert`,
   `lunarcrush_social`, `velocity_alert`). Each escapes its user-data fields
   with `_escape_md`, so they are CLAUDE.md §12b-clean today, but they are the
   only remaining non-plain-text surfaces and should migrate to `parse_mode=None`
   if their bodies ever interpolate an unescaped signal name.
