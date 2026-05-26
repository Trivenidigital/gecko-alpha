# cron/

Repo-tracked source-of-truth for the gecko-alpha crontab entries on srilu. Mirrors the `systemd/` pattern from cycle 6.

## Files

| File | Purpose |
|---|---|
| `gecko-alpha.crontab` | Sentinel-bracketed managed block with the 2 weekly archive cron entries |
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

Currently 6 entries:
- `30 3 * * 0` — `scripts/tg_burst_archive.sh` (Sunday 03:30 UTC)
- `45 3 * * 0` — `scripts/wal_archive.sh` (Sunday 03:45 UTC)
- `*/5 * * * *` — `scripts/source-calls-live-writer.sh` (every 5 min)
- `*/10 * * * *` — `scripts/source-calls-lag-watchdog.sh` (every 10 min)
- `*/10 * * * *` — `scripts/check_trade_decision_events.py`
  (every 10 min; logs `trade_decision_events` freshness status)
- `20 9 * * *` — `scripts/audit_stop_loss_false_negatives.sh --alert`
  (daily stop-loss false-negative gate; logs every run and sends Telegram only
  when status leaves `WAIT_MORE_MATURE_DATA`)

Future high-cadence triggers should prefer `systemd/*.timer` (cycle 10 canon) over cron. The 5 entries above stay as cron for simplicity (see BL-NEW-CRON-TO-SYSTEMD-TIMER decision-by 2026-06-14).

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
without a fresh operator verdict. **Status: SCRIPT-SHIPPED /
SCHEDULING-PENDING-OPERATOR.** The script lives in the repo; the
cron entry is NOT installed by default.

Design: `tasks/plan_revival_verdict_watchdog_2026_05_19.md` (PR #185).

### Smoke test (no scheduling)

```bash
ssh root@srilu-vps
cd /root/gecko-alpha
git pull
bash scripts/revival-verdict-watchdog.sh
# Expected on prod today (0 provisional rows): exit 0,
# stdout "revival_verdict_watchdog_run expired_count=0".
```

### Setup (one-time, opt-in to scheduled firing — operator approval required)

```bash
# 1. Add to cron managed block
echo "30 9 * * * /root/gecko-alpha/scripts/revival-verdict-watchdog.sh >> /var/log/revival-verdict-watchdog.log 2>&1" \
    >> cron/gecko-alpha.crontab
# 2. Commit + push the change
# 3. Deploy
bash cron/deploy.sh
# 4. Verify
crontab -l | grep revival-verdict-watchdog
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
