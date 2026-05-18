**New primitives introduced:** `scripts/cron-drift-watchdog.sh`, `tests/test_cron_drift_watchdog.py`. 1 new findings doc capturing prod-run output at PR-stage. 1 follow-up backlog item for heartbeat-monitor wiring (per Reviewer 2 §13 — §12a compliance). No DB schema changes. No modifications to `scripts/systemd-drift-watchdog.sh` (mirrored precedent). No modifications to `cron/deploy.sh` or `cron/gecko-alpha.crontab`. No alerts beyond the curl-direct Telegram pattern already in use.

# BL-NEW-CRON-DRIFT-WATCHDOG Implementation Plan (v2 — post-2-reviewer fold)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Ship a bash watchdog mirroring cycle-10 `scripts/systemd-drift-watchdog.sh` for the cycle-11 cron managed block. Detect drift; alert via curl-direct Telegram with sha256 ack-tombstone dedup; heartbeat-touch on CLEAN. Same exit codes / env-vars / lock semantics as the systemd watchdog precedent.

**Architecture:** `scripts/cron-drift-watchdog.sh` extracts the managed block from live `crontab -l` (between the cycle-11 sentinels), diffs against `cron/gecko-alpha.crontab`, sha256s any drift report, alerts on hash-change, writes ack on HTTP-200. Uses **tempfile-based diff** (Reviewer 1 #2/#3 fold) to avoid command-substitution-newline asymmetry false positives.

**Tech Stack:** bash + curl + python3 (JSON encoding only). pytest subprocess for tests (Linux-only). No Python runtime code introduced.

## v2 fold summary (post-2-reviewer fold)

| Reviewer finding | Severity | Resolution in v2 |
|---|---|---|
| R1 #2/#3 — diff/printf newline asymmetry false positives | CRITICAL | Switched to tempfile-based diff: write EXPECTED + LIVE_BLOCK to tmp files via `mktemp`, then `diff -q file1 file2`. Mirrors systemd-watchdog pattern. Added `test_clean_when_fragment_has_internal_blank_line` to lock in the fix. |
| R1 #1 — awk extraction silent on malformed sentinels | IMPORTANT | Added explicit sentinel-count check via `grep -c '^# === BEGIN gecko-alpha managed block'`. If count != 1 for either sentinel, emit distinct drift line `DRIFT: malformed sentinel structure (begin=N end=M)`. |
| R1 #7 — head -50 vs MAX_BODY=3500 truncation inconsistency | IMPORTANT | Dropped `head -50`. Single source of truth is `MAX_BODY=3500` byte cap (verbatim from systemd-watchdog). |
| R1 #8 — sentinel-text-change silent failure | IMPORTANT | Two-stage check: if any `BEGIN gecko-alpha`-containing line found but doesn't match strict pattern, emit `DRIFT: sentinel text does not match canonical form: <found-line>`. |
| R1 #11 / R2 #15 — rename "systemd-drift-watchdog" strings in copy | IMPORTANT | Plan Task 2 Step 4 explicitly lists 3 string-rename sites: ALERT_BODY prefix, TRUNC_FOOTER journalctl reference, log-event token `cron_drift_ack_write_failed`. |
| R2 #4 — `/tmp/.gecko-drift-resp.$$` symlink attack surface | IMPORTANT | Use `mktemp -t gecko-cron-drift-resp.XXXXXX` + trap-cleanup. Same fix-shape as cron/deploy.sh:21 precedent. File follow-up `BL-NEW-WATCHDOG-SYMLINK-HARDENING` to apply same fix to systemd-watchdog. |
| R2 #12 — flock-held silent-exit can mask hung curl | IMPORTANT | Add `curl --max-time 30` to bound the held-lock window. File follow-up to backport to systemd-watchdog. |
| R2 #13 — heartbeat-monitor unwired (§12a violation) | IMPORTANT | File `BL-NEW-CRON-DRIFT-WATCHDOG-HEARTBEAT-MONITOR` as follow-up. Document in plan §"Out of scope" that the heartbeat is artifact-only without a stale-detector wired (analogous to gecko-backup-watchdog precedent which already monitors a separate heartbeat). |
| R1 #4 — CRONTAB_BIN PATH lookup silent failure | MINOR | Add `command -v "$CRONTAB_BIN" >/dev/null || exit 6` early. |
| R1 #5 — test crontab stub silently absorbs unexpected invocations | MINOR | Stub else-branch echoes ERROR + exit 99 instead of cat-to-devnull. |
| R1 #12 — exit code 4 overload (ENV vs FRAGMENT both 4) | MINOR | Reserve 4=ENV (mirror systemd); add new code 8=FRAGMENT missing. |
| R1 #13 — polymarket example in failure-mode taxonomy misleading | MINOR | Replace with `/root/gecko-alpha/scripts/foo.sh outside sentinels` example. |
| R1 #15 — meta-watchdog scenario (watchdog watching its own line) | MINOR | Add row to failure-mode taxonomy acknowledging the stale-heartbeat watchdog is the right detector. |
| R2 #2 — explicit parse_mode-absence test assertion | SHOULD-FIX | Add `test_payload_does_not_set_parse_mode` to Task 3. |

**Hermes-first analysis:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Bash watchdog for crontab drift detection | None (Hermes skill hub categories + awesome-hermes-agent x-twitter-scraper checked; no operator-infra drift skill applies) | Build in-tree |
| Telegram delivery from bash | Hermes has Telegram MCP servers but they're not bash-callable. Project pattern is curl-direct from bash. | Reuse existing curl-direct pattern (cycle-10 systemd-watchdog precedent) |

awesome-hermes-agent reachable (per cycle-12 PR #152 fold); no relevant primitive. Verdict: **build in-tree, mirror cycle-10 pattern verbatim except for string renames + diff-via-tempfile + mktemp + max-time.**

**Drift-check (per CLAUDE.md §7a):** worktree HEAD = `cdeb31f` = origin/master (zero divergence). Grep for `cron-drift-watchdog|cron_drift_watchdog` returns ZERO files (net-new). Grep for `BL-NEW-CRON-DRIFT-WATCHDOG` returns backlog.md:1558 only (this entry). Adjacent primitives:

- `scripts/systemd-drift-watchdog.sh` (cycle 10) — canonical pattern to mirror
- `cron/deploy.sh` + `cron/gecko-alpha.crontab` (cycle 11) — managed-block source-of-truth
- `scripts/gecko-backup-watchdog.sh` (cycle 9) — curl-direct Telegram precedent

## Files

### Create
- `scripts/cron-drift-watchdog.sh` (~210 LOC est — slightly more than systemd's 212 due to sentinel-count check + mktemp + max-time)
- `tests/test_cron_drift_watchdog.py` (~280 LOC est — adds the 4 new fold-tests on top of the basic 7)
- `tasks/findings_cron_drift_watchdog_prod_run_2026_05_18.md` (post-build prod-run capture)

### Modify
- `backlog.md` — flip status PROPOSED → SHIPPED at PR-merge time + file 2 follow-ups (heartbeat-monitor + systemd-watchdog-backport-symlink+max-time)
- `tasks/todo.md` — Active Work entry

### Do NOT modify
- `scripts/systemd-drift-watchdog.sh` (mirrored precedent; backport via separate PR per follow-up)
- `cron/deploy.sh`, `cron/gecko-alpha.crontab`
- `.env` on srilu-vps
- Any Python runtime code

## Failure-mode taxonomy

| Failure | Severity | Mitigation |
|---|---|---|
| Managed-block content drift | EXPECTED (purpose of watchdog) | Alert fires; operator reverts or runs `cron/deploy.sh` |
| Operator pastes a `/root/gecko-alpha/scripts/foo.sh` line OUTSIDE sentinels | EXPECTED invisible | `cron/deploy.sh:44` strips it on next deploy; this watchdog is managed-block-only scope |
| Sentinel-text changed (e.g., spacing tweak) | DETECTED via two-stage check | Emit `DRIFT: sentinel text does not match canonical form: <line>` (R1 #8 fold) |
| Malformed sentinels (BEGIN without END or vice versa) | DETECTED via grep -c | Emit `DRIFT: malformed sentinel structure (begin=N end=M)` (R1 #1 fold) |
| Telegram HTTP failure | DOCUMENTED exit 7 | Don't write ack on non-200; next fire re-alerts |
| Hung curl holding flock indefinitely | MITIGATED via `--max-time 30` (R2 #12 fold) | Lock held at most 30s before curl errors out |
| Symlink attack on `/tmp/.gecko-drift-resp.$$` | MITIGATED via `mktemp` (R2 #4 fold) | Predictable PID-based path replaced with random tmp |
| Heartbeat file not monitored | DOCUMENTED gap (R2 #13 fold) | File follow-up `BL-NEW-CRON-DRIFT-WATCHDOG-HEARTBEAT-MONITOR` |
| Watchdog's own cron line removed/corrupted | EXPECTED invisible | Stale-heartbeat watchdog is the right detector (R1 #15 fold) |
| `crontab` binary missing from PATH | LOUD exit 6 (R1 #4 fold) | `command -v` check before any use |

## Task decomposition

### Task 1: Skeleton (env + bootstrap + lock)

(Same as v1, but with `command -v "$CRONTAB_BIN" >/dev/null || exit 6` added before any crontab call.)

### Task 2: Sentinel-count guard + extraction + tempfile diff

**Key change vs v1:** instead of `diff <(printf '%s\n' "$EXPECTED") <(printf '%s\n' "$LIVE_BLOCK")` use:

```bash
EXPECTED_FILE="$(mktemp -t gecko-cron-expected.XXXXXX)"
LIVE_FILE="$(mktemp -t gecko-cron-live.XXXXXX)"
trap 'rm -f "$EXPECTED_FILE" "$LIVE_FILE" "$RESP_FILE"' EXIT

# Direct file write — no command-substitution newline stripping
cat "$FRAGMENT_FILE" > "$EXPECTED_FILE"
printf '%s\n' "$LIVE_BLOCK" > "$LIVE_FILE"

# Sentinel-count check (R1 #1)
BEGIN_COUNT=$(printf '%s\n' "$LIVE_FULL" | grep -c '^# === BEGIN gecko-alpha managed block' || true)
END_COUNT=$(printf '%s\n' "$LIVE_FULL" | grep -c '^# === END gecko-alpha managed block' || true)
if [[ "$BEGIN_COUNT" != "1" || "$END_COUNT" != "1" ]]; then
    DRIFT_LINES+=("DRIFT: malformed sentinel structure (begin=$BEGIN_COUNT end=$END_COUNT)")
fi

# Two-stage sentinel-text check (R1 #8)
if [[ "$BEGIN_COUNT" == "0" ]]; then
    LOOSE_BEGIN="$(printf '%s\n' "$LIVE_FULL" | grep -i 'BEGIN gecko-alpha' | head -1 || true)"
    if [[ -n "$LOOSE_BEGIN" ]]; then
        DRIFT_LINES+=("DRIFT: sentinel text does not match canonical form: $LOOSE_BEGIN")
    fi
fi

# Content diff via tempfiles
if [[ -z "$LIVE_BLOCK" ]]; then
    DRIFT_LINES+=("DRIFT: managed block missing from prod crontab")
elif ! diff -q "$EXPECTED_FILE" "$LIVE_FILE" >/dev/null 2>&1; then
    DRIFT_LINES+=("DRIFT: managed block content differs from repo fragment")
    DIFF_BODY="$(diff -u "$EXPECTED_FILE" "$LIVE_FILE" 2>/dev/null || true)"
    if [[ -n "$DIFF_BODY" ]]; then
        DRIFT_LINES+=("$DIFF_BODY")
    fi
fi
```

(Drop `head -50` per R1 #7; `MAX_BODY=3500` byte cap downstream is sufficient.)

### Task 3: Tests (basic 7 + 4 fold-tests)

**Tests added per fold:**
- `test_clean_when_fragment_has_internal_blank_line` (R1 #2/#3 — newline asymmetry regression)
- `test_drift_on_malformed_sentinel_only_begin` (R1 #1)
- `test_drift_on_sentinel_text_typo` (R1 #8)
- `test_payload_does_not_set_parse_mode` (R2 #2)
- `test_crontab_binary_missing_exits_6` (R1 #4)

### Task 4: Telegram-delivery body (rename + mktemp + max-time)

Three string renames from systemd-watchdog copy:
1. `ALERT_BODY="⚠️ systemd-drift-watchdog: ..."` → `ALERT_BODY="⚠️ cron-drift-watchdog: ..."`
2. `TRUNC_FOOTER="...see journalctl -u systemd-drift-watchdog"` → `"...see journalctl for cron-drift-watchdog"`
3. `systemd_drift_ack_write_failed` log-event token → `cron_drift_ack_write_failed`

Plus:
- `RESP_FILE="$(mktemp -t gecko-cron-drift-resp.XXXXXX)"` (R2 #4)
- `curl --max-time 30 ...` (R2 #12)

### Task 5: Regression verification (srilu Linux pytest + actual prod-run capture)

```bash
ssh srilu-vps 'cd /root/gecko-alpha && /root/.local/bin/uv run pytest tests/test_cron_drift_watchdog.py -v'
ssh srilu-vps 'CRON_DRIFT_ACK_DIR=/tmp/cdw-prod-test bash /tmp/scripts-cron-drift-watchdog.sh; echo "exit=$?"'
```

Capture both outputs in `tasks/findings_cron_drift_watchdog_prod_run_2026_05_18.md`.

### Task 6: Backlog + todo + memory + 2 follow-up backlog items

- `BL-NEW-CRON-DRIFT-WATCHDOG-HEARTBEAT-MONITOR` (R2 #13)
- `BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT` (R2 #4 + #12 — backport to systemd-watchdog)

### Task 7: PR + 3 reviewers

### Task 8: Post-merge bookkeeping (status flip with merge-SHA per PR #150/#152 convention)

## Out of scope

- Systemd timer to auto-fire the watchdog (operator adds to managed block as separate item)
- Heartbeat-monitor wiring (deferred to `BL-NEW-CRON-DRIFT-WATCHDOG-HEARTBEAT-MONITOR`)
- Backport of mktemp + max-time fixes to systemd-watchdog (deferred to `BL-NEW-WATCHDOG-SYMLINK-AND-MAXTIME-BACKPORT`)
- Pre-commit hook for cron fragment (analog of systemd-drift-precommit-hook; deferred)
- Auto-revert on drift (intentionally absent)
- Direction-B "untracked gecko cron lines outside sentinels" detection (cron/deploy.sh's concern)

## Execution handoff

Plan v2 saved. Proceeding to inline TDD build per superpowers:executing-plans. Design phase consolidated into this plan v2 since the design is fully specified by file-shape + reviewer folds; a separate design doc would add no information not in the fold-table or Task spec.
