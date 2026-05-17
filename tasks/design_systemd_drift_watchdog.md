**New primitives introduced:** Same as `tasks/plan_systemd_drift_watchdog.md` post V45/V46 fold (`3664704`) — `scripts/systemd-drift-watchdog.sh` + `systemd/systemd-drift-watchdog.{service,timer}` + `tests/test_systemd_drift_watchdog.py` + ack-tombstone file `/var/lib/gecko-alpha/systemd-drift-watchdog/last_alerted_hash`.

# Design: BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK (cycle 10)

**Plan reference:** `tasks/plan_systemd_drift_watchdog.md` (`2a57734` + V45/V46 fold `3664704`)

## Hermes-first analysis

Same as plan §Hermes-first. No Hermes skill for systemd-drift / DevOps config-in-git enforcement. Custom in-tree, mirroring `scripts/gecko-backup-watchdog.sh`. Verdict: build.

## Design decisions

### D1. Script structure: 5-pass linear

```
1. Setup: parse env, defaults, ack-dir bootstrapping (mkdir -p with mode 0700)
2. Direction-A enumeration (repo → prod diff + drop-in check); append to DRIFT_REPORT
3. Direction-B enumeration (prod → repo; UNTRACKED PROD UNIT detection)
4. Hash + ack-check: sha256 of DRIFT_REPORT; compare ACK_FILE; suppress if match
5. Alert: env extract → curl → record new hash to ACK_FILE on success
```

`set -euo pipefail` global with **explicit `if ! diff -q ...` guards** in the loops (V45 MUST-FIX #2). Drop-in check via `compgen -G` (returns false on empty `.d/`).

### D2. Ack tombstone semantics

```
States:
  ACK_FILE absent + DRIFT_REPORT empty   → CLEAN; exit 0
  ACK_FILE absent + DRIFT_REPORT present → ALERT + write hash; exit 1
  ACK_FILE present + DRIFT_REPORT empty  → SELF-RESET (rm ACK_FILE); exit 0
  ACK_FILE present + hash matches        → silent_suppress_same_drift_set; exit 1 (no alert)
  ACK_FILE present + hash differs        → ALERT + overwrite hash; exit 1
```

Self-reset on CLEAN (state 3) is critical: without it, after the operator fixes a drift, the NEXT drift would silently suppress because the OLD hash is still present. Operator override: `rm $ACK_FILE`.

journalctl events:
- `systemd_drift_clean` (info) — no drift detected
- `systemd_drift_alerted` (info) — alert sent with hash
- `systemd_drift_silent_suppress_same_drift_set` (debug) — hash match, no alert
- `systemd_drift_alert_delivery_failed` (warning) — HTTP_STATUS != 200

### D3. Test harness reuses `tests/test_backup_rotate_script.py:354-379` seam

V45 MUST-FIX correction: the actual pattern is `_make_uv_stub` + PATH injection + marker-file at `tmp_path/"alert_marker"`. Test file:

```python
import pytest, subprocess, sys
from pathlib import Path

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash watchdog; Linux only")

def _make_uv_stub(tmp_path: Path) -> Path:
    """Replicate the pattern from tests/test_backup_rotate_script.py."""
    stub = tmp_path / "uv"
    stub.write_text("""#!/usr/bin/env bash\necho "$@" > "$ALERT_MARKER"\n""")
    stub.chmod(0o755)
    return stub

def _run_watchdog(tmp_path, **env):
    """Run scripts/systemd-drift-watchdog.sh against fixture-built repo + prod dirs."""
    ...
```

12 tests per plan §Task 1 expanded matrix. All test-fixture file content written via `Path.write_bytes(content.encode())` (V45 SHOULD-FIX — CRLF guard).

### D4. systemd unit timing — 09:30 UTC

`OnCalendar=*-*-* 09:30:00` resolves to **systemd local time**, which is UTC on srilu. 30min after gecko-backup-watchdog.timer (09:00 UTC); same `Persistent=true` posture for missed-fire recovery.

### D5. Cross-file invariants

| Invariant | Source | Verification |
|---|---|---|
| `set -euo pipefail` global with `if ! diff` guards | `scripts/systemd-drift-watchdog.sh` | `test_multi_unit_drift_reports_all` exercises 2 simultaneous drifts |
| Direction-A repo-side enumeration via `find -print0` | script + `systemd/README.md:53` deploy block | `test_filename_with_spaces` |
| Direction-B prod-side enumeration with prefix filter | script | `test_prod_only_unit_alerts` |
| Drop-in detection via `compgen -G` | script + `systemd/README.md:59-69` audit | `test_drop_in_alerts` + (negative) empty `.d/` case |
| Ack tombstone hash-based dedup | script + `ACK_FILE` path | `test_unchanged_drift_set_suppresses_re_alert` + `test_changed_drift_set_re_alerts` |
| Telegram curl-direct pattern (env → JSON → POST → HTTP check) | script lines mirror `gecko-backup-watchdog.sh:71-113` | `test_telegram_http_failure_exits_7` + `test_placeholder_token_exits_5` |
| Payload ≤ 4000 chars | script truncation logic | `test_payload_truncation_under_4096` |
| Timer fires daily 09:30 UTC, persistent | `systemd/systemd-drift-watchdog.timer` | Operator-side verification post-deploy (`systemctl list-timers`) |
| Service unit `Type=oneshot`, root user | `systemd/systemd-drift-watchdog.service` | Mirrors gecko-backup-watchdog.service |

## Commit sequence (4 commits, bisect-safe)

1. `feat(tests): failing tests for systemd-drift-watchdog (cycle 10 commit 1/4)` — `tests/test_systemd_drift_watchdog.py` only; tests fail (script doesn't exist yet); CI tolerates per master-baseline. Could split this commit but combining keeps a tight TDD cycle.
2. `feat(scripts): systemd-drift-watchdog.sh implementation (cycle 10 commit 2/4)` — script alone; tests pass.
3. `feat(systemd): drift-watchdog service + timer units (cycle 10 commit 3/4)` — `systemd/systemd-drift-watchdog.{service,timer}`; README update.
4. `docs(backlog): close BL-NEW-SYSTEMD-DRIFT-PRECOMMIT-HOOK + memory checkpoint (cycle 10 commit 4/4)` — backlog flip + memory checkpoint file.

## Risk register additions (beyond plan §Risk register)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ACK_FILE writable on `/var/lib/gecko-alpha/...` | Refuted | — | mkdir + chmod 0700 in script bootstrap; root-owned (script runs as root via systemd) |
| ACK_FILE corrupted (partial write mid-fsync) | Very low | Low | sha256 mismatch on corrupted hash → falls through to ALERT path (re-write); benign |
| Direction-B prefix filter excludes future unit naming | Medium | Low | Filter currently `gecko-*` + `minara-*` + `systemd-drift-watchdog.*`. If future PR adds e.g. `scout-*` units, filter must extend. **Locked in code comment + risk register row to remind future-self** |
| `find -print0` + `read -d ''` doesn't work in some shell variants | Refuted | — | Tested via `test_filename_with_spaces`. Plain `bash` (set in shebang) supports both |

## Out of scope

- Auto-fix of drift (operator decides repo or prod is source of truth)
- Pre-commit hook variant (backlog explicitly prefers cron+TG)
- Watchdog for OTHER untracked config (BL-NEW-OTHER-PROD-CONFIG-AUDIT covers as cycle 11)
- §12a freshness-SLO daemon
- BL-NEW-WATCHDOG-META-WATCHDOG (filed as follow-up; requires §12a daemon)
- BL-NEW-WATCHDOG-ALERT-DEDUP (filed contingent on observed redundant alerts)

## Deployment verification (autonomous post-3-reviewer-fold)

Per plan §Deployment with V46 MUST-FIX gate:

1. `git pull` on srilu
2. `find ... -exec cp -t /etc/systemd/system/ {} +`
3. `systemctl daemon-reload`
4. **Pre-flight diff (V46 MUST-FIX):** `diff -q systemd/systemd-drift-watchdog.{service,timer} /etc/systemd/system/`
5. `systemctl enable --now systemd-drift-watchdog.timer`
6. Smoke: `systemctl start systemd-drift-watchdog.service` + `journalctl -u systemd-drift-watchdog.service --since "1 minute ago"`
7. Verify first run exits 0 with "OK: N unit files match" — no false-positive drift

8. Operator records expected first-Monday-09:30-UTC fire in calendar.
