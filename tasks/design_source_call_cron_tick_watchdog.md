**New primitives introduced:** heartbeat-file mtime emission on writer success path; `writer_stale` / `writer_heartbeat_missing` / `writer_heartbeat_pending` status enum in `check_source_calls_lag.py`; `WRITER_HEARTBEAT_FILE` env passthrough; structured-log triplet (`*_alert_dispatched` / `*_alert_delivered` / `*_alert_failed`) bolted onto existing `source-calls-lag-watchdog.sh`.

# Design: BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG (folded into lag-watchdog)

**Plan:** `tasks/plan_source_call_cron_tick_watchdog.md` v2
**Status:** DESIGN
**Date:** 2026-05-21

## 1. File-by-file deltas

### 1a. `scripts/source_calls_live_writer.py`

Add `--heartbeat-file` arg + touch on success:

```python
# argparse additions
parser.add_argument(
    "--heartbeat-file",
    default=None,
    help=(
        "Path to touch on successful run (mtime = now). "
        "Used by source-calls-lag-watchdog to detect writer cron outages "
        "independently of upstream traffic. Best-effort: touch failures "
        "log a warning but do not fail the writer."
    ),
)

# After successful asyncio.run(_run(...)) returning a result dict:
def _touch_heartbeat(path: Path, log) -> None:
    """Best-effort heartbeat. Touch failures log but don't propagate.

    Per global CLAUDE.md feedback_resilience_layered_failure_modes:
    failing to touch the file is observable to the lag-watchdog (file
    mtime stays stale), so the operator gets a visible signal via the
    existing alerter — best-effort here does NOT swallow the failure,
    it relocates the surface to the watchdog read-path.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError as err:
        log.warning(
            "source_calls_heartbeat_touch_failed",
            path=str(path),
            errno=err.errno,
            error=str(err),
        )
        # Do not re-raise: writer success/failure must not depend on
        # observability surface health.

# In main(): after writer success
if args.heartbeat_file:
    _touch_heartbeat(Path(args.heartbeat_file).expanduser(), structlog.get_logger())
```

Acceptance: touch happens after the writer success path, never before;
errors are logged but never escalated.

### 1b. `scripts/source-calls-live-writer.sh`

Add env passthrough:

```bash
HEARTBEAT_FILE="${WRITER_HEARTBEAT_FILE:-}"

exec_args=(--db "${DB_PATH}")
if [[ -n "$HEARTBEAT_FILE" ]]; then
    exec_args+=(--heartbeat-file "$HEARTBEAT_FILE")
fi

exec "${PYTHON}" "${SCRIPT_DIR}/source_calls_live_writer.py" "${exec_args[@]}"
```

Default `WRITER_HEARTBEAT_FILE` empty → no-op (back-compat).

### 1c. `scripts/check_source_calls_lag.py`

Add writer-staleness branch. Pseudocode of the changes:

```python
parser.add_argument(
    "--writer-heartbeat-file",
    default=None,
    help="Path to writer heartbeat (touched by source_calls_live_writer.py on success).",
)
parser.add_argument(
    "--writer-threshold-minutes",
    type=int,
    default=20,
    help="Alert if writer heartbeat older than this. Default 4x writer cadence.",
)

# After parsing args; BEFORE the existing ledger-lag select:
def _check_writer_heartbeat(
    heartbeat_path: Path | None,
    threshold_minutes: int,
    ledger_has_rows: bool,
    now: datetime,
) -> tuple[str, dict[str, Any]] | None:
    """Returns (status, detail) tuple if a writer-side issue is detected,
    None if writer-side is healthy / not configured.

    Status enum:
      writer_stale            — file exists, mtime older than threshold
      writer_heartbeat_missing— file absent and ledger has rows (writer ran before, stopped touching now)
      writer_heartbeat_pending— file absent and ledger empty (first-run; alert-suppressed)
    """
    if heartbeat_path is None:
        return None  # arg omitted → branch disabled
    if not heartbeat_path.exists():
        if not ledger_has_rows:
            return ("writer_heartbeat_pending", {
                "path": str(heartbeat_path),
                "ledger_rows": 0,
                "alert_suppressed": True,
            })
        return ("writer_heartbeat_missing", {
            "path": str(heartbeat_path),
            "ledger_rows": ledger_has_rows,
        })
    mtime = datetime.fromtimestamp(heartbeat_path.stat().st_mtime, tz=timezone.utc)
    age_minutes = (now - mtime).total_seconds() / 60.0
    if age_minutes > threshold_minutes:
        return ("writer_stale", {
            "path": str(heartbeat_path),
            "age_minutes": round(age_minutes, 1),
            "threshold_minutes": threshold_minutes,
            "last_writer_run_at": mtime.isoformat(),
        })
    return None

# In main():
now = datetime.now(timezone.utc)
ledger_row_count = await _count_source_calls(conn)
heartbeat_path = Path(args.writer_heartbeat_file).expanduser() if args.writer_heartbeat_file else None
writer_finding = _check_writer_heartbeat(
    heartbeat_path,
    args.writer_threshold_minutes,
    ledger_has_rows=(ledger_row_count > 0),
    now=now,
)
if writer_finding is not None:
    status, detail = writer_finding
    if status == "writer_heartbeat_pending":
        # First-run guard: emit JSON but exit 0 (no alert)
        print(json.dumps({"ok": True, "status": status, "detail": detail}, sort_keys=True))
        return 0
    # writer_stale or writer_heartbeat_missing → exit nonzero (alert path)
    print(json.dumps({"ok": False, "status": status, "detail": detail}, sort_keys=True))
    return 1

# Else: existing ledger-lag check runs unchanged (no regression).
```

Critically: writer-staleness is checked FIRST. If the writer is dead,
the ledger-lag check would also report stale (because ledger is no
longer being updated), but the writer-staleness signal is more
actionable. Single fire per outage, with the more-actionable diagnosis.

### 1d. `scripts/source-calls-lag-watchdog.sh`

Pass new args through + parse status from JSON for differentiated alert
text + §12b log triplet:

```bash
WRITER_HEARTBEAT_FILE="${WRITER_HEARTBEAT_FILE:-}"
WRITER_THRESHOLD_MINUTES="${WRITER_THRESHOLD_MINUTES:-20}"

py_args=(--db "${DB_PATH}" --threshold-minutes "${THRESHOLD_MINUTES}")
if [[ -n "$WRITER_HEARTBEAT_FILE" ]]; then
    py_args+=(--writer-heartbeat-file "$WRITER_HEARTBEAT_FILE")
    py_args+=(--writer-threshold-minutes "$WRITER_THRESHOLD_MINUTES")
fi

set +e
result="$("${PYTHON}" "${SCRIPT_DIR}/check_source_calls_lag.py" "${py_args[@]}" 2>&1)"
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
    echo "OK: $result"
    exit 0
fi

# Parse status field from JSON (fallback to generic text if jq absent)
parsed_status="$(echo "$result" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d.get("status","unknown"))' 2>/dev/null || echo unknown)"

case "$parsed_status" in
    writer_stale)
        text="source-calls-lag-watchdog: writer cron stale. ${result}"
        ;;
    writer_heartbeat_missing)
        text="source-calls-lag-watchdog: writer heartbeat missing. ${result}"
        ;;
    ledger_lag|unknown|*)
        text="source-calls-lag-watchdog: ledger lag or unreachable. status=${status} result=${result}"
        ;;
esac

# ... existing env-file / TG-cred guards unchanged ...

# §12b log triplet (NEW)
echo "source_calls_lag_alert_dispatched status=${parsed_status} text=${text}" >&2

set +e
http_status="$(curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${text}" \
    -d "parse_mode=" \
    -o /dev/null -w "%{http_code}" 2>&1)"
curl_rc=$?
set -e

if [[ "$curl_rc" -eq 0 && "$http_status" == "200" ]]; then
    echo "source_calls_lag_alert_delivered status=${parsed_status} http_status=${http_status}" >&2
    echo "ALERT_SENT: $text"
    exit 1
else
    echo "source_calls_lag_alert_failed status=${parsed_status} curl_rc=${curl_rc} http_status=${http_status}" >&2
    echo "ALERT_FAILED_DELIVERY: $text (http_status=${http_status})" >&2
    exit 7
fi
```

`parse_mode=` (plain text) — already in the existing wrapper, preserved.
This matters because `writer_stale` alert text contains underscores in
the JSON detail (`writer_heartbeat_missing`, `last_writer_run_at`).
MarkdownV1 would silently mangle them per CLAUDE.md §12b.

### 1e. `cron/gecko-alpha.crontab` — UNCHANGED

(v1 added a new line; v2 does not.)

### 1f. `backlog.md`

Append entry under Active Work section (file-only):

```markdown
### BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG: detect writer cron outages independently of upstream traffic
**Status:** SHIPPED-IN-PR 2026-05-21 — folded into existing
`scripts/source-calls-lag-watchdog.sh` via writer-heartbeat check
(branch: `feat/source-call-cron-tick-watchdog`).
**Why:** Lag watchdog reads `MAX(upstream) - MAX(source_calls)` — cannot
detect writer-side outage if upstream is also quiet. Class-1 silent-
failure per CLAUDE.md §12a.
**Mechanism:** writer touches `/var/lib/gecko-alpha/source-calls/writer-
heartbeat` on success; lag-watchdog reads mtime, alerts if older than
20min (4× writer cadence).
**Cost:** ~80 LOC additive; no new cron line, no new bash script.
**Hermes-first:** KEEP_CUSTOM — no monitoring skill found.
**Kill criterion:** 2026-08-21 — revert if zero real fires.
```

## 2. Schema / DB changes

NONE. No new tables, no migrations, no DB writes.

## 3. Configuration / env

| Key | Purpose | Default | Required? |
|---|---|---|---|
| `WRITER_HEARTBEAT_FILE` | path passed to writer + watchdog | empty (branch disabled) | NO — system back-compat |
| `WRITER_THRESHOLD_MINUTES` | staleness threshold | `20` (built-in) | NO |

For prod activation: operator sets `WRITER_HEARTBEAT_FILE=/var/lib/gecko-
alpha/source-calls/writer-heartbeat` in `/root/gecko-alpha/.env`. Single
env line activates both sides of the contract.

## 4. Test scaffolding shapes

### Writer tests (`tests/test_source_calls_live_writer.py`)

Use `tmp_path` for heartbeat dir. `subprocess.run` the CLI script (it's
sync entrypoint to async `_run`). Existing test file may not exist — if
so, create with the patterns above.

### Lag-check tests (`tests/test_check_source_calls_lag.py`)

For each status, build a tmp scout.db with `source_calls` rows (or
empty for first-run case), create / touch a tmp heartbeat file with
specific mtime via `os.utime`, run the CLI, assert exit code + stdout
JSON `status` field.

### Bash watchdog tests (`tests/test_source_calls_lag_watchdog.sh`)

Mock `curl` via PATH-override pattern (existing convention in
`tests/test_cron_drift_watchdog.sh`). Capture stdin/argv via fake binary
that writes to tmp file. Mock the Python check via env var
`GECKO_PYTHON=/path/to/stub.py` returning preset JSON.

If `tests/test_source_calls_lag_watchdog.sh` does not exist yet, model
on `tests/test_cron_drift_watchdog.sh` (proven harness).

## 5. §12 compliance audit

| Rule | Compliance |
|---|---|
| §12a (Class 1 freshness watchdog) | **FIXED gap** — writer-cron-tick is now monitored. No new watchdog created, so no recursive-liveness problem (Reviewer B C1). |
| §12b (alert-time visibility for automated state reversal) | N/A — this watchdog does not reverse operator state. But the §12b log triplet (dispatched / delivered / failed) is bolted on as collateral fix to the existing wrapper which previously emitted only ALERT_SENT. |
| §12b parse_mode hygiene | UNCHANGED — wrapper already used `parse_mode=` (empty). Preserved. Alert body underscores (`writer_heartbeat_missing`) are now safe because plain-text. |
| Resilience-layered-failure (memory entry) | Addressed §1a docstring — touch failure logs structured warning but relocates the operator-visible surface to the watchdog read-path. NOT swallowed. |
| §9c data-path attribution | Addressed: writer-staleness is checked BEFORE ledger-lag so that the more-actionable signal wins when both would fire. |

## 6. Risks

| Risk | Mitigation |
|---|---|
| Clock skew on srilu-vps causes false `writer_stale` | NTP is active per systemd-timesyncd default; skew >20min would also break Telegram TLS. Acceptable; documented in plan §I7. |
| First-run alert on fresh DB | Guarded: `writer_heartbeat_pending` for empty ledger. Suppressed. |
| Heartbeat-dir-not-creatable masks writer failure | Writer logs structured warning; lag-watchdog reads "file absent + ledger has rows" → `writer_heartbeat_missing` alert fires anyway. Operator gets signal. |
| Tests use mocked time — drift between mocked + real systems | All tests use explicit `now` injection via stub `--now` arg in test mode; production uses `datetime.now(timezone.utc)`. |
| `python3` not on PATH in lag-watchdog wrapper for JSON parse | Falls back to `unknown` status → generic alert text, still fires correctly. |

## 7. Out-of-scope confirmations

- NO trading-behavior changes.
- NO source-call ranking / scoring / pruning.
- NO dashboard surface changes.
- NO new DB tables / migrations.
- NO new cron lines.
- NO MORALIS / HELIUS / paid-API integrations.
- NO live config changes — operator sets one `.env` line at activation
  time (separate operator action, post-merge).

## 8. Implementation order

1. Update `source_calls_live_writer.py` (arg + touch helper + tests).
2. Update `source-calls-live-writer.sh` (env passthrough).
3. Update `check_source_calls_lag.py` (new branch + tests).
4. Update `source-calls-lag-watchdog.sh` (arg passthrough + §12b triplet + tests).
5. Add backlog entry.
6. Commit; open PR.
7. 2 PR reviewers.
8. Operator runbook §plan-6.

## 9. Open questions resolution

| Q | Resolution |
|---|---|
| Q1: pending status >24h alerting | NO — operator runbook validates within 1 hour of deploy; if forgotten, lag-watchdog ledger-lag branch will fire on real upstream traffic. Don't add 24h timer; adds state. |
| Q2: heartbeat file content vs touch-only | TOUCH-ONLY. The JSON content is already on stdout of the writer captured to journald. SELECT-without-DB debugging is a non-goal for this iteration. |
| Q3: threshold via .env | YES — `WRITER_THRESHOLD_MINUTES` env, fall-through to CLI default 20. Two lines in wrapper; allows operator-tune without re-deploy. |
