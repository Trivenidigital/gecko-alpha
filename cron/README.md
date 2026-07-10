# cron/

Repo-tracked source-of-truth for the gecko-alpha crontab entries on srilu. Mirrors the `systemd/` pattern from cycle 6.

## Files

| File | Purpose |
|---|---|
| `gecko-alpha.crontab` | Sentinel-bracketed managed block with the scheduled gecko-alpha cron entries |
| `deploy.sh` | Idempotent awk-based merge script |

## Deploy

After pulling a PR that touches anything in `cron/`:

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
bash cron/deploy.sh
crontab -l   # verify
```

The deploy script:
- Reads `cron/gecko-alpha.crontab` (the sentinel-bracketed managed block)
- Reads current crontab via `crontab -l`
- Replaces the existing managed block (if present) OR appends if first deploy
- **First-deploy migration:** strips any `/root/gecko-alpha/scripts/` line found OUTSIDE the sentinel block — those existed pre-cycle-11 unbracketed; the new managed block replaces them. Operator manual entries pointing to OTHER paths (polymarket, etc.) are preserved.
- Stages to tempfile, then atomically installs via `crontab <tempfile>`

Idempotent: re-running `cron/deploy.sh` produces an identical crontab.

## Sentinel convention

The managed block is bracketed by:

```
# === BEGIN gecko-alpha managed block (do not edit between sentinels) ===
<lines>
# === END gecko-alpha managed block ===
```

**Do not hand-edit the sentinel lines** — the awk regex matches them as literal anchors. Edits to the entries themselves go via repo PR + redeploy.

## What's in scope

Currently 9 entries:
- `30 3 * * 0` — `scripts/tg_burst_archive.sh` (Sunday 03:30 UTC)
- `45 3 * * 0` — `scripts/wal_archive.sh` (Sunday 03:45 UTC)
- `*/5 * * * *` — `scripts/source-calls-live-writer.sh` (every 5 min)
- `*/10 * * * *` — `scripts/source-calls-lag-watchdog.sh` (every 10 min)
- `*/10 * * * *` — `scripts/check_trade_decision_events.py`
  (every 10 min; logs enablement-aware multi-signal
  `trade_decision_events` freshness status)
- `20 9 * * *` — `scripts/audit_stop_loss_false_negatives.sh --alert`
  (daily stop-loss false-negative gate; logs every run and sends Telegram only
  when status leaves `WAIT_MORE_MATURE_DATA`)
- `*/15 * * * *` — `scripts/acceleration-heartbeat-watchdog.sh`
  (gainer-acceleration detector execution-heartbeat; alerts if
  `acceleration_scan_complete` is absent from the journal > 60 min)
- `*/5 * * * *` — `scripts/held-position-price-watchdog.sh`
  (held-position price-refresh lane freshness; alerts when open paper
  trades have `price_cache` rows stale > 30 min for 3 consecutive runs)
- `30 9 * * *` — `scripts/revival-verdict-watchdog.sh`
  (daily; alerts when a `keep_on_provisional_until_<iso>` soak verdict has
  passed its embedded expiry without a fresh operator verdict)

Future high-cadence triggers should prefer `systemd/*.timer` (cycle 10 canon) over cron. The managed entries above stay as cron for simplicity (see BL-NEW-CRON-TO-SYSTEMD-TIMER decision-by 2026-06-14).

## Stderr redirection convention (Round 6 hardening)

**Every managed-block cron line MUST redirect both stdout and stderr to
`/var/log/gecko-alpha-<scriptname>.log` via `>> /var/log/... 2>&1`.** On
srilu the local MTA is not configured, so cron's default behavior of
mailing the job's stderr to `root` drops the output on the floor — any
script failure becomes invisible. Per-job log files give the operator a
journalctl-equivalent trail for these bash workloads.

Static test `tests/test_cron_stderr_redirection.py` enforces this on
every managed-block entry to prevent regression.

## Why this exists

Cycle 11 audit (`tasks/findings_other_prod_config_audit_2026_05_17.md`) found these 2 cron entries existed on srilu but were repo-untracked at the schedule level. The shell scripts were in repo, but their cron schedule was operator-only. Same substrate class as cycle 6 (BL-NEW-SYSTEMD-UNIT-IN-REPO).

## Drift detection (BL-NEW-CRON-DRIFT-WATCHDOG, cycle 12)

`scripts/cron-drift-watchdog.sh` detects post-deploy operator edits to the managed block. Runs ad-hoc OR scheduled.

### Setup (one-time, opt-in to scheduled firing)

```bash
# 1. Add to cron managed block
echo "0 4 * * * /root/gecko-alpha/scripts/cron-drift-watchdog.sh >> /var/log/cron-drift-watchdog.log 2>&1" \
    >> cron/gecko-alpha.crontab
# 2. Commit + push the change
# 3. Deploy
bash cron/deploy.sh
# 4. Verify
crontab -l | grep cron-drift-watchdog
```

### Disable / revert

If the watchdog itself misfires or floods Telegram:

```bash
# Fast disable: strip from live crontab
crontab -l | grep -v cron-drift-watchdog | crontab -

# OR clean revert: remove from cron/gecko-alpha.crontab and redeploy
sed -i '/cron-drift-watchdog/d' cron/gecko-alpha.crontab
bash cron/deploy.sh
```

### Heartbeat freshness check (until `BL-NEW-CRON-DRIFT-WATCHDOG-HEARTBEAT-MONITOR` ships)

```bash
# Flag if heartbeat > 25h old (covers daily cron + 1h slack)
find /var/lib/gecko-alpha/cron-drift-watchdog/heartbeat -mmin +1500 -type f \
    -exec echo "STALE: cron-drift-watchdog heartbeat > 25h" \;
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | CLEAN — managed block matches repo fragment; heartbeat touched |
| 1 | DRIFT alerted OR silently suppressed (sha256 ack match) |
| 4 | `.env` missing |
| 5 | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` missing/placeholder |
| 6 | required binary missing (`crontab` or `python3`) OR `UV_BIN` set without test opt-in |
| 7 | Telegram HTTP delivery failed; ACK NOT written; next fire re-alerts |
| 8 | `cron/gecko-alpha.crontab` fragment missing in repo |

## Revival-verdict-watchdog (BL-NEW-REVIVAL-VERDICT-WATCHDOG)

`scripts/revival-verdict-watchdog.sh` alerts when a
`signal_params_audit` row of the form
`keep_on_provisional_until_<iso>` has passed its embedded expiry
without a fresh operator verdict. **Status: SCHEDULED in the managed
block** (`30 9 * * *`, daily — 10 min after the stop-loss FN audit so
the two daily jobs don't overlap).

Historical note: from PR #186 (2026-05-19) through 2026-07-02 this
watchdog was SCRIPT-SHIPPED / SCHEDULING-PENDING-OPERATOR — the script
lived in the repo but the cron entry was intentionally NOT installed,
per the opt-in convention for operator-judgment *verdict* watchdogs.
The 2026-07-02 production review (`tasks/prod_review_2026_07_02.md`
GA-03) found it dark on prod alongside `held-position-price-watchdog`
and is the decision record for scheduling both. Daily cadence is ample:
provisional verdicts carry 30-day expiry horizons, and per-signal
idempotency state re-alerts at most weekly (`REALERT_HOURS=168`).

Design: `tasks/plan_revival_verdict_watchdog_2026_05_19.md` (PR #185).

### Smoke test

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
bash scripts/revival-verdict-watchdog.sh
# Expected when no provisional verdicts are expired: exit 0,
# stdout "revival_verdict_watchdog_run expired_count=0".
```

### Disable / revert

```bash
# Fast disable: strip from live crontab
crontab -l | grep -v revival-verdict-watchdog | crontab -

# Clean revert: remove from cron/gecko-alpha.crontab and redeploy
sed -i '/revival-verdict-watchdog/d' cron/gecko-alpha.crontab
bash cron/deploy.sh
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | No expired provisional verdicts, OR alert suppressed by per-signal re-alert window |
| 1 | Alert delivered |
| 4 | DB not found / SQL error / malformed ISO in `new_value` |
| 5 | Telegram token / chat_id missing or placeholder |
| 6 | `python3` not available (JSON encoding) |
| 7 | Telegram HTTP delivery failed |

## Acceleration heartbeat watchdog (gap-fill 2026-06-02)

`scripts/acceleration-heartbeat-watchdog.sh` is an EXECUTION-heartbeat watchdog
for the gainer-acceleration detector (`scout/gainers/acceleration.py`). Zero
acceleration rows can be healthy (no token qualified this window), so it does NOT
check row-rate; it greps the journal for the `acceleration_scan_complete` line
the detector emits every cycle it runs and alerts only when that heartbeat is
stale (default 60 min). Inert when `ACCELERATION_ENABLED` is falsey. **Status:
SCHEDULED in the managed block** (`*/15`, alongside `source-calls-lag-watchdog`).
The detector ships active (`ACCELERATION_ENABLED=True`), so per §12a its freshness
watchdog ships active too — it is in `gecko-alpha.crontab` and goes live on the
next `cron/deploy.sh`. (Codex + the silent-failure review both flagged shipping
the writer active without an active watchdog; auto-scheduling resolves it. The
opt-in convention applies to operator-judgment *verdict* watchdogs like
`cron-drift`, not pipeline-freshness monitors. `revival-verdict` started under
the opt-in convention and was later scheduled per
`tasks/prod_review_2026_07_02.md` GA-03.)

### Smoke test

```bash
ssh root@srilu-vps
cd /root/gecko-alpha && git pull
bash scripts/acceleration-heartbeat-watchdog.sh   # exit 0 if heartbeat fresh
```

Cadence 15 min << the 60-min staleness threshold. The `>> … 2>&1` redirect is
required by `tests/test_cron_stderr_redirection.py` for managed-block lines.

### Disable / revert

```bash
crontab -l | grep -v acceleration-heartbeat-watchdog | crontab -
# clean revert:
sed -i '/acceleration-heartbeat-watchdog/d' cron/gecko-alpha.crontab && bash cron/deploy.sh
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Heartbeat seen in window, OR detector disabled |
| 1 | Stale → Telegram alert delivered (HTTP 200) |
| 2 | Stale, `.env` missing (alert to stdout) |
| 3 | Stale, Telegram creds missing (alert to stdout) |
| 7 | Stale, Telegram delivery failed |
| 64 | Unknown argument |

## Held-position price watchdog (GA-03, 2026-07-02)

`scripts/held-position-price-watchdog.sh` is the ONLY alarm for the
held-position price-refresh lane silently breaking. The lane
(`tasks/plan_held_position_price_freshness.md`, Alt A) is itself the
mitigation for a live S1 — open paper trades whose `price_cache` rows go
stale skip ALL exit evaluation (trailing stops / stop-losses can't fire on
frozen prices; see `tasks/prod_review_2026_07_02.md` GA-01/GA-02). The
script shipped with the lane per §12a but was never scheduled anywhere
(neither crontab nor `systemd/`) — a dark watchdog. **Status: SCHEDULED
in the managed block** (`*/5`, per the design doc's stated cadence).
Decision record: `tasks/prod_review_2026_07_02.md` GA-03.

Cadence rationale: the script alerts when open `paper_trades` have
`price_cache` rows missing or stale > 30 min (`HELD_POSITION_STALE_AFTER_MIN`)
for 3 consecutive runs (`HELD_POSITION_WATCHDOG_HYSTERESIS`, anti-blip).
At `*/5` that means detection ~45 min after the lane breaks (30 min
staleness threshold + 3×5 min hysteresis), matching the design doc's
"runs every 5 min ... 3 consecutive cycles" spec. A slower cadence would
stretch the hysteresis window proportionally (e.g. `*/15` → 30+45 min).

### Prerequisites (all already satisfied on srilu)

- `/root/gecko-alpha/scout.db` (override: `GECKO_DB_PATH`) — missing DB
  fails loudly: stderr + exit 4 into the per-job log.
- `/root/gecko-alpha/.env` with real `TELEGRAM_BOT_TOKEN` +
  `TELEGRAM_CHAT_ID` (override: `GECKO_ENV_FILE`) — needed only at alert
  time; missing/placeholder creds fail loudly (stderr + exit 4/5), never
  a silent exit-0.
- `sqlite3` and `python3` CLI binaries (python is for JSON-encoding the
  Telegram payload; missing → exit 6).
- Hysteresis state dir `/var/lib/gecko-alpha/held-position-watchdog/`
  is auto-created (`mkdir -p`) — no manual setup.

No heartbeat-file wiring is required: unlike the acceleration watchdog,
this script queries DB output directly (rows + timestamps), per the
"watchdog reads OUTPUT not heartbeats" discipline.

### Smoke test

```bash
ssh root@srilu-vps
cd /root/gecko-alpha && git pull
bash scripts/held-position-price-watchdog.sh
# Healthy: exit 0, stdout "stale_count=0 ... OK: 0 held positions with stale price_cache"
```

### Disable / revert

```bash
crontab -l | grep -v held-position-price-watchdog | crontab -
# clean revert:
sed -i '/held-position-price-watchdog/d' cron/gecko-alpha.crontab && bash cron/deploy.sh
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | No stale held positions, OR stale but below 3-consecutive-runs hysteresis |
| 1 | Alert delivered (HTTP 200) |
| 4 | DB not found / SQL error / `.env` missing at alert time |
| 5 | Telegram token / chat_id missing or placeholder |
| 6 | `python` not available (JSON encoding) |
| 7 | Telegram HTTP delivery failed |

## Stop-loss false-negative audit gate

`scripts/audit_stop_loss_false_negatives.sh` tracks the post-held-position-
refresh stop-loss false-negative gate from the 2026-05-26 trading-signal
quality audit.

It is scheduled daily in the managed block. Normal state is log-only:
`WAIT_MORE_MATURE_DATA`. Telegram fires only when either:
- mature post-enable stop-loss false negatives reach `n>=30` across
  `gainers_early`, `losers_contrarian`, and `trending_catch`;
- mature post-enable stop-loss false negatives reach `n>=15` for
  `gainers_early`;
- the calendar backstop `2026-08-26` arrives; or
- the broad post-enable mature `gainers_early` PnL cohort reaches `n>=20`.

The script intentionally requires `first_runner_at > closed_at` for stop-loss
false negatives. The 2026-05-26 cleanup found 15/42 historical
`gainers_early` rows where the runner-board event preceded stop close, which
overstated the old false-negative bucket.

Residual §12a surface: the daily cron writes a heartbeat at
`/var/lib/gecko-alpha/stop-loss-fn-audit/heartbeat`, but no stale-heartbeat
watchdog is wired yet. Because this gate has a 2026-08-26 backstop, that is
accepted as low-priority unless this pattern becomes a longer-lived monitor.

## Alert-channel + digest freshness watchdog (§12a)

`scripts/alert-channel-watchdog.sh` (wrapping `scripts/alert_channel_watchdog.py`)
is the ONLY alarm for two pipeline tables that went silently stale and were
noticed only weeks later: the Telegram alert channel (`tg_alert_log`) had zero
`outcome='sent'` rows for 14 days (2026-06-25 → 07-08), and the daily digest
(`paper_daily_summary`) stopped writing after 2026-06-26. Per the operator
amendment, ONE script monitors BOTH tables:

- **Check 1 — `tg_alert_log`**: latest `outcome='sent'` row must be newer than
  `ALERT_SENT_SLO_HOURS` (default 48).
- **Check 2 — `paper_daily_summary`**: `MAX(date)` must be within
  `DIGEST_SUMMARY_SLO_DAYS` (default 2; yesterday's row lands ~02:00 UTC daily).

A missing OR empty table is itself a breach (silence is never ambiguous). On any
breach the script sends ONE plain-text Telegram page (`parse_mode=None`, §12b —
the table names contain `_`) naming each breached table, its last-seen
timestamp, and its SLO, with `alert_channel_watchdog_alert_dispatched` /
`_alert_delivered` / `_alert_failed` structured logs around the send. The send
passes `raise_on_failure=True` so a rejected page raises (→ `_alert_failed` +
exit 1) instead of the alerter's default swallow-and-return — the watchdog must
never report its own page delivered when Telegram rejected it. Read-only on the
DB; it queries table OUTPUT (rows/timestamps), not heartbeats.

**Per-table SEND cooldown** (`ALERT_CHANNEL_WATCHDOG_COOLDOWN_HOURS`, default 24;
state files `last_alert_<table>` under `ALERT_CHANNEL_WATCHDOG_STATE_DIR`,
default `/var/lib/gecko-alpha/alert-channel-watchdog`, auto-`mkdir -p`): at most
one page per breached table per window, so the hourly cron does not emit ~24
identical pages/day on a standing breach. The cooldown suppresses the SEND only
— a breach **always** exits 5 (logged `_alert_suppressed_by_cooldown` with the
next-eligible time); detection is never suppressed. State is written only after
a successful send, so a failed send re-alerts next run.

**Status: SCHEDULED in the managed block** (`50 * * * *`, hourly at :50), gated
on `ALERT_CHANNEL_WATCHDOG_ENABLED=true` (set inline in the cron line — the
deploy-without-activate flag; a manual run without it is a safe no-op).
Activation occurs when the operator runs `cron/deploy.sh`, per operator
approval. Cadence rationale: hourly is far inside both SLOs (48h / 2d); the
cooldown makes cadence choice mostly about detection latency, not spam.

> **⚠️ ACTIVATION PREREQUISITE (deploy ordering):** activate this watchdog only
> AFTER PR #429 (daily-digest yesterday-fix) is deployed AND has written **≥1
> fresh `paper_daily_summary` row** — otherwise the first digest pages fire for a
> known-broken-being-fixed writer. The per-table cooldown bounds the blast
> radius to one page/table/window, but the ordering is still the correct
> sequence.

### Smoke test

```bash
ssh root@srilu-vps
cd /root/gecko-alpha && git pull
# Preview WITHOUT sending or touching cooldown state:
.venv/bin/python scripts/alert_channel_watchdog.py --db scout.db --enabled true --dry-run
```

### Disable / revert

```bash
crontab -l | grep -v alert-channel-watchdog | crontab -
# clean revert:
sed -i '/alert-channel-watchdog/d' cron/gecko-alpha.crontab && bash cron/deploy.sh
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Both fresh, OR watchdog disabled (no-op) |
| 5 | One or more freshness breaches (page dispatched and/or cooldown-suppressed, or `--dry-run` preview) |
| 1 | DB missing / runtime error / alert-dispatch failure (send raised) |
| 64 | Unknown argument (wrapper) |
