# Findings - Live-readiness audit report - 2026-06-22

## Status (2026-07-10)

Superseded by `tasks/findings_live_trading_m1_audit_2026_07_06.md` (4 S1
blockers; NOT prod-ready) and the standing LIVE-ENABLE GATE
(`LIVE_TRADING_ENABLED` + `LIVE_USE_ROUTING_LAYER` stay OFF until all 4 S1s
land). This document is a dated historical record and is filed retroactively
as closed/superseded — the body below is unaltered.

## Evidence-only verdict

**Not ready for live enablement.** This is a read-only readiness finding, not
approval to trade. No live enablement, order placement, credential activation,
config mutation, signal eligibility change, or DB write was performed. Final
live-trading approval remains operator-only.

The current production posture is safe from live execution but blocked for live
trading because Binance credentials are unset, every signal has
`live_eligible=0`, queried last-30-day paper cohorts are negative, venue
metadata is empty, venue health/balance snapshot writers are unproven, and
watchdog/timer evidence is incomplete.

## Drift check

- Current `origin/master` already shipped the offshore handoff in
  `docs/offshore_handoff_live_trading_2026_06_21.md`.
- No existing `live-readiness` findings report was found under `docs/`,
  `tasks/`, or `backlog.md`.
- Open PR #375 is a Solana on-chain execution venue proposal and remains
  outside this report's autonomous merge scope because it touches live
  execution and requires explicit operator approval.
- Open PR #374 is stale-worktree cleanup and does not close Phase 1
  live-readiness.

## Hermes-first analysis

Checked the Hermes skill hub at
`https://hermes-agent.nousresearch.com/docs/skills` on 2026-06-22 and the
awesome-hermes-agent ecosystem query for live trading readiness/exchange
adapter coverage.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Production runtime-state audit | none found | build as repo/runtime findings because evidence comes from srilu systemd, `.env`, and SQLite state |
| Binance live-trading readiness | none found | build as repo-specific report; exchange keys and permissions must be verified operator-side |
| Dashboard/watchdog health verification | none found | use existing repo scripts and systemd checks |
| Paper-performance cohort evidence | none found | query `paper_trades` directly; no Hermes replacement |

Awesome-hermes-agent ecosystem verdict: no reusable skill was identified for
Gecko's production live-readiness report; Hermes remains orchestration/memory,
while Codex records repo-grounded runtime evidence.

## Runtime evidence

Captured from `srilu-vps` using SSH redirect-to-file workflow on 2026-06-22 UTC.
Host identity returned `ubuntu-4gb-hel1-1`.

### Deployed state

- `/root/gecko-alpha` is on `master...origin/master`.
- Deployed commit is `3751120` (`feat(conviction): Prospective sub-$30M
  high-conviction watchlist (V1, observe-only)`), one commit behind current
  `origin/master` in this worktree (`b0df1720`, offshore handoff doc only).
- Untracked production files include maintenance logs and DB backups:
  `maintenance_vacuum_2026-06-18.log`, `scout.db.bak.20260620T030000Z`,
  `scout.db.bak.20260621T030001Z`, `scout.db.bak.20260622T030006Z`,
  `scout.db.pre-vacuum-shm`, and `scout.db.pre-vacuum-wal`.

### Services and hardening

- `gecko-pipeline`: active, `NRestarts=0`, `OnFailure` configured to
  `codex-systemd-failure-alert@...` and
  `codex-systemd-auto-remediate@...`, drop-in
  `/etc/systemd/system/gecko-pipeline.service.d/10-telegram-onfailure.conf`.
- `gecko-dashboard`: active, `NRestarts=0`, same `OnFailure` alert/remediation
  shape and drop-in.
- Inactive service units in this check: `gecko-backup-watchdog`,
  `minara-emission-persistence-watchdog`, `systemd-drift-watchdog`,
  `chain-anchor-health-watchdog`.
- Caveat: these watchdogs are timer/oneshot style surfaces, so an inactive
  `.service` can be normal between timer fires. This run did not capture
  `*.timer` last-trigger/next-trigger or recent journal exit status, so
  watchdog health is **not proven**, rather than proven broken.
- Live-deploy systemd hardening remains unclosed: this run verified `OnFailure`
  and `NRestarts=0`, but did not prove the live runbook's restart-throttling
  expectations (`Restart=on-failure`, `RestartSec=30s`, `StartLimitBurst=3`,
  `StartLimitIntervalSec=300s`) on runtime units or drop-ins.

### Config posture

Secret values were not recorded.

- `.env`: `LIVE_MODE=shadow`, `LIVE_SIGNAL_ALLOWLIST=first_signal`.
- `.env`: `LIVE_TRADING_ENABLED`, `LIVE_USE_REAL_SIGNED_REQUESTS`,
  `LIVE_USE_ROUTING_LAYER`, `LIVE_TRADE_AMOUNT_USD`,
  `LIVE_MAX_EXPOSURE_USD`, `LIVE_MAX_OPEN_POSITIONS`,
  `LIVE_DAILY_LOSS_CAP_USD`, `LIVE_SLIPPAGE_BPS_CAP`,
  `LIVE_VENUE_PREFERENCE`, `BINANCE_API_KEY`, `BINANCE_API_SECRET`,
  `MINARA_ALERT_ENABLED`, and `MINARA_ALERT_AMOUNT_USD` are unset.
- Telegram bot token and chat id are present by redacted length only.
- `python -m scout.main --check-config` exited 0 and resolved:
  `LIVE_MODE=shadow`, `live_signal_allowlist_set=['first_signal']`,
  `LIVE_DAILY_LOSS_CAP_USD=50`, `LIVE_MAX_EXPOSURE_USD=500`,
  `LIVE_MAX_OPEN_POSITIONS=5`, TP 40%, SL 25%, max duration 168h.

### Signal eligibility

`signal_params` shows no live-eligible signal:

| Signal | enabled | tg_alert_eligible | live_eligible | suspended_reason |
|---|---:|---:|---:|---|
| chain_completed | 0 | 0 | 0 | hard_loss |
| first_signal | 1 | 0 | 0 |  |
| gainers_early | 0 | 1 | 0 | hard_loss |
| losers_contrarian | 0 | 1 | 0 | hard_loss |
| narrative_prediction | 1 | 1 | 0 |  |
| slow_burn | 0 | 0 | 0 | hard_loss |
| tg_social | 1 | 1 | 0 |  |
| trending_catch | 0 | 0 | 0 | hard_loss |
| volume_spike | 0 | 1 | 0 | hard_loss |

### Paper performance, last 30 days

All queried last-30-day paper cohorts with trades are negative. Columns are
signal, trades, closed trades, net PnL, average closed PnL, and winning closed
trades.

| Signal | Trades | Closed | Net PnL | Avg Closed PnL | Wins |
|---|---:|---:|---:|---:|---:|
| chain_completed | 132 | 95 | -1935.35 | -20.37 | 49 |
| first_signal | 16 | 14 | -454.01 | -32.43 | 6 |
| narrative_prediction | 62 | 62 | -1625.62 | -26.22 | 9 |
| slow_burn | 39 | 39 | -572.43 | -14.68 | 14 |
| tg_social | 13 | 11 | -337.93 | -30.72 | 2 |
| volume_spike | 26 | 26 | -179.98 | -6.92 | 9 |

`would_be_live=1` last 30 days: 29 trades / 22 closed / -713.44 net.
`actionable=1` last 30 days: 185 trades / 152 closed / -2786.02 net.

### Fire-rate sanity

Last seven days paper opens:

- `first_signal`: 2
- `slow_burn`: 19
- `tg_social`: 2

This is not enough positive live-readiness evidence for first_signal or
tg_social inside a short readiness window. Slow burn has volume, but the repo
already records it as failed and suspended/retired from dispatch.

### Live/shadow ledgers and kill switch

- `shadow_trades`: 17 `closed_duration`, 5 `closed_sl`, 2 `closed_tp`,
  109 `rejected`, and 0 open.
- `live_trades`: 0 open/submitted/partially-filled rows in this check.
- `live_control.active_kill_event_id=1`, but active unexpired kill events = 0.
  This stale pointer should be reconciled before any live-mode attempt.
- `venue_listings` count = 0.
- `cross_venue_exposure` count = 2.
- No `venue_health` or `wallet_snapshots` rows printed in this query. This is a
  hard Phase 1 evidence gap: there is no proof here that health-probe/balance
  snapshot loops are running, no signed venue-auth proof, no balance freshness
  cadence, and no wallet snapshot writes to support live mode.

### Alert and dashboard health

- Dashboard contract smoke:
  `OK: dashboard contracts clean (live_candidates=0, trade_inbox=0,
  todays_focus=0)`.
- `tg_alert_log` had 0 recent 429/retry_after rows in the last seven days.
- Recent TG alert rows: `first_signal=2`, `slow_burn=19`, `tg_social=2`,
  `trade_surface=154`.
- Caveat: this is log-level health evidence only. This run did not fire an
  end-to-end Telegram test alert or an `OnFailure` alert.

## Handoff question answers

1. Branch/commit: pipeline/dashboard run from `/root/gecko-alpha` at commit
   `3751120` on `master...origin/master`; current repo master has one newer
   docs-only handoff commit.
2. `LIVE_*`, `BINANCE_*`, `MINARA_*`, Telegram: live mode is shadow, Binance
   keys unset, Minara settings unset, Telegram configured by redacted presence.
3. Enabled/live/TG signals: listed above; none are live eligible.
4. Signals with enough closed trades to justify live eligibility: none. Recent
   cohorts are negative, including `would_be_live=1` and `actionable=1`.
5. Venue health/listings/wallet snapshots: not ready. `venue_listings=0` and
   no freshness rows printed for `venue_health` or `wallet_snapshots`.
6. Open shadow/live rows: none.
7. Kill control: active unexpired kill events = 0, but `live_control` still
   points at id 1; reconcile before live-mode work.
8. Watchdogs: dashboard contracts are clean, SQLite maintenance check was not
   completed by this run, and watchdog timer health remains unproven because
   this run checked service activity but not timer last/next trigger or recent
   journal exits.
9. Telegram alert health: no recent 429 rows; source labels exist in current
   alert rows, including `trade_surface`. No end-to-end delivery test was run.
10. Rollback: fastest no-git rollback for any future live-mode attempt is to
   disable live routing/signed requests (`LIVE_USE_ROUTING_LAYER=False` and
   `LIVE_USE_REAL_SIGNED_REQUESTS=False`) and restart the service; before code
   rollback, set `LIVE_MODE=paper` and restart. Approver must be the operator.

## Next operator action

Do not enable live trading. Operator should first decide whether to:

1. keep Phase 1 open and ask for a follow-up audit that captures timer
   last/next triggers, recent journal exits, SQLite maintenance evidence,
   source-call watchdog coverage, and venue health/balance writer cadence, or
2. explicitly authorize a separate, secret-safe Binance credential/permission
   verification run that confirms signed `/api/v3/account`, IP whitelist, spot
   permission, account funding, and NTP prerequisites without exposing secrets.

## Review folds

- Plan reviewer 1: changed the plan and report language from `go/no-go` to an
  evidence-only readiness verdict and added explicit no-live-action boundaries.
- Plan reviewer 2: added paper-performance cohorts, secret-safe evidence
  standards, Binance permission/whitelist gate, systemd `OnFailure`, Telegram
  429 source attribution, venue/listing/exposure checks, rollback command, and
  approver requirement.
