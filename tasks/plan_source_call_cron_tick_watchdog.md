**New primitives introduced:** heartbeat-file emission on `scripts/source_calls_live_writer.py` success path; writer-cadence branch added to existing `scripts/check_source_calls_lag.py` (no new bash script, no new cron line, no new state dir beyond a writer-heartbeat path).

# Plan (v2): BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG — folded into existing lag-watchdog

**Branch:** `feat/source-call-cron-tick-watchdog`
**Date:** 2026-05-21
**Status:** PLAN v2 (post 2-reviewer fold)
**Predecessor:** v1 (rejected REWORK by Reviewer A, APPROVE_WITH_FOLDS by Reviewer B; both pointed at the same overcommitment — separate script + cron line + state dir).

## 0. Why v2 differs from v1

v1 proposed a separate `scripts/source-calls-cron-tick-watchdog.sh` + new
cron line + new state dir + 13 new tests.

**Fold-applied changes:**

| Reviewer A finding | v2 resolution |
|---|---|
| C1 — failure hypothetical | v2 makes the writer-staleness branch additive to existing lag-watchdog, not a separate surface. Cost falls from "new watchdog" to "extra SELECT branch". Pre-emptive defense is justifiable at this cost. |
| C2 — both-sides-quiet not realistic | conceded for narrative-scanner traffic regime; but the writer also fires on EMPTY upstream (idempotent design) — so the staleness-of-writer signal is decoupled from upstream traffic regardless of regime. Branch is cheap to add and always correct. |
| C3 — cheaper alternative | adopted. Extend `source-calls-lag-watchdog` instead of new script. |
| I2 — META-watchdog overlap | sidestepped by composition — no new watchdog → no new recursive-liveness problem. |
| I3 — soft-failure mask | addressed §4d: heartbeat is touched only when backfill+refresh return without exception AND emit non-empty completion JSON. |

| Reviewer B finding | v2 resolution |
|---|---|
| C1 — §12a recursion | removed: no new watchdog to monitor. |
| C2 — §12b log triplet | required in design (§5) and tests. |
| C3 — heartbeat-touch-failed silent | writer emits `source_calls_heartbeat_touch_failed=true` to stderr structlog; existing journald capture surfaces it. Lag-watchdog Python check also detects "heartbeat path exists but is older than threshold" as the operator-visible signal. |
| I4 — cron-drift alert on rollout | removed: no new cron line. |
| I5 — state-dir owner | resolved: deploy runbook step creates dir explicitly. |
| I6 — two-step SSH | included in §6 runbook. |
| I7 — alert body off-by-one | resolved: message reports `last_writer_run_age_minutes` instead of tick count. |
| I8 — heartbeat-dir colocation | resolved: heartbeat lives at `/var/lib/gecko-alpha/source-calls/writer-heartbeat`, NOT under a watchdog state dir, because the writer (not a watchdog) owns it. |

## 1. Problem (lever-vs-data-path framing, §9c) — unchanged

`source-calls-lag-watchdog` proves **upstream → ledger parity** by reading
`MAX(upstream_ts) - MAX(source_calls_ts)`. It cannot prove the writer is
firing at expected cadence: if upstream is also stale, the parity check
reports OK.

The writer is **idempotent and fires on empty upstream too** (verified
`scripts/source_calls_live_writer.py:65-98`). So writer-staleness IS
detectable independently — we just don't currently look.

## 2. Drift-check (§7a) — extended

### 2a. In-tree existence — confirmed none (v1 §2a) + reviewer-B's addition
- `scout/heartbeat.py` exists — but it's **in-process per-cycle source-starvation tracking** inside the gecko-pipeline event loop, NOT cron-tick measurement. Different shape, different producer, different filesystem location. No naming-collision risk because the new writer-heartbeat file is at `/var/lib/gecko-alpha/source-calls/writer-heartbeat`, the in-process counter has no on-disk artifact.
- `scripts/hermes-no-agent-flag-check.sh` — Hermes-side, different pipeline.
- `tasks/design_ingest_watchdog.md` — different surface (source starvation per cycle, not writer cron firing).

### 2b. Backlog drift — `BL-NEW-CRON-DRIFT-WATCHDOG-HEARTBEAT-MONITOR` (PROPOSED 2026-05-18) — still adjacent-not-overlapping. v2 does NOT compose with it (no new watchdog heartbeat to monitor).

### 2c. Master-merge check — branch off `origin/master` HEAD `51901116` (PR #210). No conflicts expected with PR #207 substrate (already merged; v2 only extends files PR #207 introduced).

**Drift verdict:** No in-tree primitive overlaps. Proceed.

## 3. Hermes-first analysis — unchanged

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Cron-job cadence monitoring | none (skill hub checked: webhook-subscriptions is event-driven push, not pull-cadence) | KEEP_CUSTOM |
| Heartbeat-file freshness | none | KEEP_CUSTOM |
| Systemd-timer / cron-tick monitoring | none | KEEP_CUSTOM |
| Dead-man's-switch / status-page | none in Hermes | KEEP_CUSTOM |

Ecosystem checks (all done in v1):
- `https://hermes-agent.nousresearch.com/docs/skills` — no infra monitoring skills
- `awesome-hermes-agent` topic — empty
- `webhook-subscriptions` ruled out — event-driven, not cadence-watching

**Verdict:** KEEP_CUSTOM. Project-internal observability of a project-
internal Python writer.

## 4. Proposed mechanism (v2 — collapsed)

### 4a. Writer-side change (`scripts/source_calls_live_writer.py`)
Add `--heartbeat-file PATH` argument:
- On exit 0 (backfill + refresh completed without exception) touch `PATH`.
- On exit 1 (db missing, runtime error) do NOT touch.
- If `PATH` is None / arg omitted → no-op (back-compat).
- If `PATH.parent` doesn't exist or touch raises `OSError`:
  - emit structured log line: `source_calls_heartbeat_touch_failed=true reason=<errno>` to stderr (already wired structlog → stderr).
  - continue with exit 0 — observability failure does NOT propagate to writer failure.
  - But the staleness will surface to lag-watchdog on next run because the file mtime never advanced. Operator gets a real signal via the EXISTING alerter surface — no swallowed failure.

### 4b. Bash wrapper (`scripts/source-calls-live-writer.sh`)
Resolve `WRITER_HEARTBEAT_FILE` from env (or default
`/var/lib/gecko-alpha/source-calls/writer-heartbeat`) and pass through.

### 4c. Lag-watchdog Python check (`scripts/check_source_calls_lag.py`)
Add a parallel "writer staleness" branch:
- `--writer-heartbeat-file PATH` (default same as 4b)
- `--writer-threshold-minutes N` (default 20 = 4× writer cadence)
- If heartbeat file exists AND mtime is older than threshold → emit status `writer_stale`, exit non-zero.
- If heartbeat file is missing AND grace period elapsed (e.g., ledger has rows, so we know writer ran before) → emit `writer_heartbeat_missing`, exit non-zero.
- If heartbeat file is missing AND ledger is empty (first-run) → DO NOT alert; emit `writer_heartbeat_pending` status only, exit 0. Avoids false alerts during initial deploy.
- Else (heartbeat fresh) → run existing lag check unchanged.

### 4d. Lag-watchdog bash wrapper (`scripts/source-calls-lag-watchdog.sh`)
Pass `--writer-heartbeat-file` and `--writer-threshold-minutes` through.
Differentiate alert text by which branch fired:
- `ledger_lag`: "source-calls-lag-watchdog: ledger lagging upstream. <details>"
- `writer_stale`: "source-calls-lag-watchdog: writer last fired N minutes ago (threshold 20min). <details>"
- `writer_heartbeat_missing`: "source-calls-lag-watchdog: writer heartbeat missing past grace. <details>"

§12b alert hygiene retained:
- `parse_mode=` (plain text) — already in current wrapper, unchanged.
- Add `source_calls_lag_alert_dispatched` / `source_calls_lag_alert_delivered` structured log lines around the curl call (currently only an `ALERT_SENT` echo exists). This is a small §12b drift-fix bundled with v2.

### 4e. No cron change
The lag-watchdog already runs `*/10 * * * *` per the managed block. Just
needs the new args wired in.

### 4f. State-dir creation
Deploy runbook step explicitly:
```
mkdir -p /var/lib/gecko-alpha/source-calls
chmod 0755 /var/lib/gecko-alpha/source-calls
```
Owned by `root` (cron user). No special permissions needed on the file
itself (touched as root by cron writer; read as root by cron watchdog).

## 5. Test plan

### 5a. Python writer tests — `tests/test_source_calls_live_writer.py`
1. `test_heartbeat_file_touched_on_success` — happy path → mtime advances.
2. `test_heartbeat_file_not_touched_on_db_missing` — db_not_found → no touch.
3. `test_heartbeat_file_not_touched_on_backfill_exception` — backfill raises → no touch, exit 1.
4. `test_heartbeat_parent_missing_logs_warning_succeeds` — parent dir absent → structured warning, exit 0.
5. `test_heartbeat_arg_omitted_no_touch_no_warning` — back-compat.

### 5b. Python lag-check tests — `tests/test_check_source_calls_lag.py`
1. `test_writer_heartbeat_fresh_existing_lag_check_runs` — heartbeat fresh, ledger OK → exit 0.
2. `test_writer_heartbeat_stale_exits_nonzero` — mtime old → exit nonzero with status=`writer_stale`.
3. `test_writer_heartbeat_missing_with_ledger_rows_exits_nonzero` — file absent, ledger non-empty → status=`writer_heartbeat_missing`, exit nonzero.
4. `test_writer_heartbeat_missing_empty_ledger_exits_zero_with_pending` — first-run guard, exit 0 with `writer_heartbeat_pending`.
5. `test_writer_fresh_but_ledger_lagging_existing_path_still_fires` — heartbeat fresh, ledger stale → existing `ledger_lag` branch fires (regression).
6. `test_writer_branch_disabled_when_arg_omitted` — back-compat: no heartbeat arg → exit 0 if ledger OK.

### 5c. Bash watchdog tests — `tests/test_source_calls_lag_watchdog.sh` (extend existing if present, else new)
1. `test_alert_text_contains_writer_stale_marker_when_writer_stale` — assert curl --data-urlencode contains "writer last fired".
2. `test_alert_text_contains_ledger_lag_marker_when_ledger_stale` — regression.
3. `test_structured_log_dispatched_emitted_on_alert` — grep stderr for `source_calls_lag_alert_dispatched`.
4. `test_structured_log_delivered_emitted_on_200_response` — mock curl 200 → `source_calls_lag_alert_delivered`.
5. `test_structured_log_failed_emitted_on_non_200_response` — mock curl 500 → `source_calls_lag_alert_failed`, exit nonzero.

### 5d. Manual VPS validation runbook (deploy gate)

Two-step SSH pattern (Windows-bash limitation per global CLAUDE.md):

```bash
# Step 1: trigger writer once, capture exit + heartbeat
ssh srilu-vps 'cd /root/gecko-alpha && ./scripts/source-calls-live-writer.sh ; ls -la /var/lib/gecko-alpha/source-calls/' > .ssh_tmp/v1_writer_post.txt 2>&1
# Step 2: Read .ssh_tmp/v1_writer_post.txt — assert heartbeat file exists, mtime is within last 60s

# Step 1: trigger lag-watchdog, assert exit 0
ssh srilu-vps 'bash /root/gecko-alpha/scripts/source-calls-lag-watchdog.sh ; echo exit=$?' > .ssh_tmp/v2_watchdog_clean.txt 2>&1
# Step 2: Read — assert "OK:" line, exit=0, NO Telegram traffic

# Step 1: simulate writer outage (rename heartbeat to mtime-25min-ago)
ssh srilu-vps 'touch -d "25 minutes ago" /var/lib/gecko-alpha/source-calls/writer-heartbeat && bash /root/gecko-alpha/scripts/source-calls-lag-watchdog.sh ; echo exit=$?' > .ssh_tmp/v3_simulated_outage.txt 2>&1
# Step 2: Read — assert "writer last fired" in alert text, exit=1, Telegram delivered

# Step 1: restore (run writer to refresh heartbeat) and verify recovery
ssh srilu-vps './scripts/source-calls-live-writer.sh ; bash /root/gecko-alpha/scripts/source-calls-lag-watchdog.sh ; echo exit=$?' > .ssh_tmp/v4_recovery.txt 2>&1
# Step 2: Read — assert "OK:", exit=0
```

If any step diverges, abort deploy and revert.

## 6. Rollout sequence (Reviewer-B I4 fold)

1. Merge PR.
2. SSH to srilu-vps; `cd /root/gecko-alpha && git pull`.
3. `mkdir -p /var/lib/gecko-alpha/source-calls && chmod 0755 /var/lib/gecko-alpha/source-calls`.
4. Run `pyc-clear` (`find . -name __pycache__ -exec rm -rf {} +`) per CLAUDE.md memory `feedback_clear_pycache_on_deploy`.
5. Set `WRITER_HEARTBEAT_FILE=/var/lib/gecko-alpha/source-calls/writer-heartbeat` in `.env` (so wrapper picks it up).
6. Wait one writer tick (≤5min); confirm heartbeat file present.
7. Wait one lag-watchdog tick (≤10min); confirm exit 0 in journalctl.
8. Run §5d validation runbook (step-1/step-2 SSH pattern).
9. **No `crontab` reload needed** — no cron line changed.

No cron-drift alert is triggered because the managed block in
`cron/gecko-alpha.crontab` is unchanged.

## 7. Rollback

1. `unset WRITER_HEARTBEAT_FILE` in `.env`. Writer reverts to no-touch
   behavior. Lag-watchdog `--writer-heartbeat-file` arg becomes a no-op
   (file absent + arg-passed-but-ledger-rows path goes to
   `writer_heartbeat_missing` — to avoid that, ALSO remove the env line
   that propagates the arg).
2. Lag-watchdog still functions as today.
3. Reverting the PR is a clean revert (no schema changes, no data writes).

Net blast-radius if reverted: zero.

## 8. Acceptance criteria

- [ ] 5 new writer tests pass.
- [ ] 6 new lag-check tests pass.
- [ ] 5 new lag-watchdog bash tests pass (extend existing test file if present).
- [ ] `git diff master` touches only:
  - `scripts/source_calls_live_writer.py`
  - `scripts/source-calls-live-writer.sh`
  - `scripts/check_source_calls_lag.py`
  - `scripts/source-calls-lag-watchdog.sh`
  - `tests/test_source_calls_live_writer.py`
  - `tests/test_check_source_calls_lag.py`
  - `tests/test_source_calls_lag_watchdog.sh` (new or extend)
  - `backlog.md` (file entry)
  - `tasks/plan_*.md` + `tasks/design_*.md`
- [ ] Hermes-first table + drift-check + cheaper-alternative comparison in PR description.
- [ ] §5d validation runbook executes cleanly on srilu-vps.
- [ ] No new cron line, no new bash script, no new state dir beyond the writer-heartbeat dir.

## 9. Kill criterion (Reviewer-A N2 fold)

If zero real `writer_stale` or `writer_heartbeat_missing` alerts fire by
2026-08-21 (90 days post-deploy), file BL to revert as accumulated
infra-debt. Operator may extend if observed reliability data warrants.

## 10. Open questions for design-stage reviewers

1. Should `writer_heartbeat_pending` (first-run empty-ledger guard) ever
   alert if it persists >24h? Or trust operator deploy-runbook
   confirmation as one-time validation?
2. Should the heartbeat file include the writer's last-run JSON (`{started_at, finished_at, backfill, refresh}`) as content rather than just being touched? Adds 1KB I/O per tick, but lets a debugging session SELECT-without-DB. Cost too high vs benefit?
3. Should the cadence threshold be config-driven via `.env` (`WRITER_STALENESS_THRESHOLD_MINUTES`) or remain a CLI default?
