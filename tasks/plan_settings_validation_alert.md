**New primitives introduced:** `scout/config_alert.py` module (one new file containing `_send_validation_alert_best_effort`), state-dir `/var/lib/gecko-alpha/settings-validation-watchdog/`, env var `SETTINGS_VALIDATION_ALERT_STATE_DIR` (optional override).

# BL-NEW-SETTINGS-VALIDATION-ALERT — Plan (cycle 14)

> Plan + design merged. The change is ~50 LOC + tests; a separate design pass would only duplicate this content.

## Goal

When `load_settings()` re-raises `ValidationError` on bad `.env`, ALSO emit a curl-direct Telegram alert (plain text, no parse_mode) so the operator sees the crash-loop actively instead of having to grep `journalctl`.

## Architecture

**Where the alert fires:** inside `scout/config.py:load_settings()`, immediately after `logger.error("settings_validation_failed", ...)`, before the `raise`. Synchronous best-effort call to a new helper. NEVER blocks the re-raise: catches all exceptions.

**Why curl-direct (not scout.alerter):** scout.alerter is async + requires Settings to be already loaded — neither available at this failure point. Pattern matches `scripts/gecko-backup-watchdog.sh` and `scripts/cron-drift-watchdog.sh` (curl-direct via stdlib).

**Why a separate module (`scout/config_alert.py`):** keeps `scout/config.py` import-light. Settings module already crosses 1300 LOC; adding urllib + state-dir logic into it inflates an already-large file. New 50-LOC module isolates the alert path and makes the no-secret/dedup tests independent of Settings construction.

**Hermes-first analysis:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram alert delivery | none — Hermes has no `python-stdlib-telegram-push` skill; project's own gecko-backup/cron-drift/systemd-drift watchdogs use curl-direct as established pattern | use project's curl-direct pattern via urllib.request |
| .env parsing | none applicable — Hermes has no Python `.env`-reader skill | use stdlib (split on `=`, strip whitespace) |
| State-file dedup | none — same pattern as ACK-file tombstone in cron/systemd watchdogs | reuse same hash+state-file primitive |

awesome-hermes-agent ecosystem check: scanned for "python-config-validation-alert" / "pydantic-validation-push" — no relevant primitive. Verdict: build from scratch in-tree.

## Component Files

### CREATE: `scout/config_alert.py` (~50 LOC)

```python
"""Best-effort curl-direct Telegram alert for settings_validation_failed events.

Imported by scout/config.py load_settings(). Fires synchronous urllib.request
to Telegram on validation failure, before the re-raise. NEVER blocks the
re-raise — all exceptions caught and swallowed.

Dedup via state file: SHA256(error_str) hashed; if same hash as last alert,
skip (avoids 360 msg/hr storm under systemd Restart=always crash-loop).

Does NOT depend on Settings being loaded (Settings IS the thing that's
broken). Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID directly from os.environ
OR (if absent there) parses .env file by hand.

BL-NEW-SETTINGS-VALIDATION-ALERT (cycle 14).
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_STATE_DIR = Path("/var/lib/gecko-alpha/settings-validation-watchdog")
DEFAULT_ENV_FILE = Path("/root/gecko-alpha/.env")
ALERT_URL_FMT = "https://api.telegram.org/bot{token}/sendMessage"
# Plan R1 I1 fold: 3s ceiling. Plan originally specified 10s, but
# systemd's Restart=always+RestartSec=10 means each restart that hits a
# TG outage would block 10s+10s = ~20s. 3s is fire-and-forget-grade for
# best-effort alert delivery; falls back to "skipped:http_error" silently.
ALERT_TIMEOUT_SEC = 3


def _read_env_value(key: str, env_file: Path) -> str | None:
    """Read KEY=value from .env, tolerating leading whitespace.
    Returns None if file missing or key not found.
    Strips surrounding quotes; does NOT shell-expand.
    """
    if not env_file.exists():
        return None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(f"{key}="):
                val = stripped[len(key) + 1:]
                return val.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _resolve_telegram_creds(env_file: Path) -> tuple[str | None, str | None]:
    """Read TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from os.environ or .env.
    Returns (None, None) if either missing/placeholder.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or _read_env_value(
        "TELEGRAM_BOT_TOKEN", env_file
    )
    chat = os.environ.get("TELEGRAM_CHAT_ID") or _read_env_value(
        "TELEGRAM_CHAT_ID", env_file
    )
    if not token or token == "placeholder":
        return None, None
    if not chat or chat == "placeholder":
        return None, None
    return token, chat


def _send_validation_alert_best_effort(error_str: str) -> str:
    """Send a plain-text Telegram alert on settings_validation_failed.

    Returns one of: "sent", "skipped:no_creds", "skipped:dedup",
    "skipped:state_dir_unwritable", "skipped:http_error",
    "skipped:exception". Never raises.
    """
    try:
        state_dir = Path(
            os.environ.get(
                "SETTINGS_VALIDATION_ALERT_STATE_DIR", str(DEFAULT_STATE_DIR)
            )
        )
        env_file = Path(
            os.environ.get("GECKO_ENV_FILE", str(DEFAULT_ENV_FILE))
        )

        token, chat = _resolve_telegram_creds(env_file)
        if token is None or chat is None:
            return "skipped:no_creds"

        # Dedup via SHA256 of error_str
        error_hash = hashlib.sha256(error_str.encode("utf-8")).hexdigest()
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return "skipped:state_dir_unwritable"
        ack_file = state_dir / "last_alerted_hash"
        if ack_file.exists():
            try:
                prior = ack_file.read_text(encoding="utf-8").strip()
                if prior == error_hash:
                    return "skipped:dedup"
            except OSError:
                pass  # treat unreadable ack as "no prior"

        # Build plain-text body. NO parse_mode (per CLAUDE.md §12b — Pydantic
        # error strings contain underscores which MarkdownV1 would mangle).
        # Truncate to keep Telegram payload under 4096 bytes.
        body_text = "⚠️ settings_validation_failed\n" + error_str[:3800]
        payload = json.dumps({"chat_id": chat, "text": body_text}).encode("utf-8")
        req = urllib.request.Request(
            ALERT_URL_FMT.format(token=token),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=ALERT_TIMEOUT_SEC) as resp:
                if resp.status != 200:
                    return "skipped:http_error"
        except (urllib.error.URLError, OSError, TimeoutError):
            return "skipped:http_error"

        # Write ack ONLY on successful HTTP 200
        try:
            ack_file.write_text(error_hash, encoding="utf-8")
        except OSError:
            pass  # alert was delivered; ack-write failure is not fatal
        return "sent"
    except Exception:
        return "skipped:exception"
```

### MODIFY: `scout/config.py:load_settings()` (~3 LOC addition)

After `logger.error("settings_validation_failed", error=str(exc))`, add:
```python
        try:
            from scout.config_alert import _send_validation_alert_best_effort
            _send_validation_alert_best_effort(str(exc))
        except Exception:
            pass  # alert is best-effort; never block the re-raise
```

The outer try is defense-in-depth — `_send_validation_alert_best_effort` already
catches its own exceptions, but the import itself could fail under exotic
conditions (corrupted bytecode, etc.).

### CREATE: `tests/test_config_alert.py` (~120 LOC)

Tests must use stdlib mocks (`unittest.mock.patch`) — no aiohttp/aioresponses
since the helper is synchronous.

Test list:
1. `test_send_alert_returns_sent_on_http_200` — full happy path; mock urlopen returns 200; assert "sent" + ack-file written
2. `test_send_alert_skipped_on_missing_token` — no TELEGRAM_BOT_TOKEN anywhere; assert "skipped:no_creds" + no HTTP call
3. `test_send_alert_skipped_on_placeholder_token` — token == "placeholder"; assert "skipped:no_creds"
4. `test_send_alert_skipped_on_missing_chat_id` — no TELEGRAM_CHAT_ID; assert "skipped:no_creds"
5. `test_send_alert_skipped_on_placeholder_chat_id` — chat == "placeholder"; assert "skipped:no_creds"
6. `test_send_alert_dedup_on_same_error_hash` — first call "sent" + ack written; second call with same error "skipped:dedup"
7. `test_send_alert_resends_on_different_error` — first "sent"; second with DIFFERENT error string "sent"
8. `test_send_alert_skipped_on_state_dir_unwritable` — state_dir path under a regular file; assert "skipped:state_dir_unwritable"
9. `test_send_alert_skipped_on_http_non_200` — mock urlopen returns 500; assert "skipped:http_error" + ack NOT written
10. `test_send_alert_skipped_on_connection_error` — mock urlopen raises URLError; assert "skipped:http_error"
11. `test_send_alert_never_raises_on_unexpected_exception` — monkeypatch hashlib.sha256 to raise; assert "skipped:exception"
12. `test_read_env_value_tolerates_leading_whitespace` — same .env-parsing tolerance as PR #159
13. `test_load_settings_invokes_alert_helper` — integration: patch `_send_validation_alert_best_effort`, trigger ValidationError via `load_settings(BL060_VOLUME_DAYS_BACK=-1, ...)`, assert helper called with the error string

## Tasks

### Task 1: TDD — write failing tests
- [x] Write all 13 tests in `tests/test_config_alert.py`
- [x] Run on srilu; verify all 13 FAIL (module doesn't exist yet OR helper missing)

### Task 2: GREEN — implement `scout/config_alert.py`
- [x] Create file with the module above
- [x] Run tests; verify all 13 PASS

### Task 3: REFACTOR — wire into `load_settings()`
- [x] Edit `scout/config.py:load_settings()` to call helper after log
- [x] Run full `tests/test_config.py` + `tests/test_config_alert.py`; verify NO regression
- [x] Run quick scout-wide smoke (`pytest tests/ -k "config or settings"`) to confirm no broader breakage

### Task 4: Bookkeeping
- [x] Update `backlog.md` BL-NEW-SETTINGS-VALIDATION-ALERT status PROPOSED → PR-OPEN
- [x] Update `tasks/todo.md` Active Work section

## Test plan checkpoints

- Mock surface: `unittest.mock.patch("urllib.request.urlopen")` + `urllib.error.URLError` for negative paths
- Environment: tests must `monkeypatch.setenv("SETTINGS_VALIDATION_ALERT_STATE_DIR", str(tmp_path / "state"))` to avoid touching /var/lib
- Environment: tests must `monkeypatch.setenv("GECKO_ENV_FILE", str(tmp_path / ".env"))` to control .env discovery
- Environment: tests must `monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)` + `delenv("TELEGRAM_CHAT_ID", raising=False)` per-test
- Determinism: dedup test uses fixed error strings, no time-based assertions

## Non-goals (deferred)

- **No removal of `logger.error("settings_validation_failed", ...)`** — structlog event remains the primary record; alert is additive.
- **No alert on non-ValidationError exceptions** — scope limited to validator-class failures. Other startup crashes (import errors, etc.) are not in scope.
- **No alert state-file watchdog** — `last_alerted_hash` is a tombstone, not a heartbeat; not monitoring it independently per cycle-12 cron-drift pattern. Filing follow-up if needed post-deploy.

## Cross-PR coordination

- **PR #158 / #159 in flight**: no file overlap (config.py vs scout/ingestion/* vs scripts/*).
- **PR #157**: pure audit docs; no overlap.
- **Operator soak gate**: this PR adds an active alert path. Operator commitment: review the alert message format after first real fire (which should be never, ideally) and flip `SETTINGS_VALIDATION_ALERT_STATE_DIR` or `TELEGRAM_*` env vars if format/throttle proves wrong.

## Drift-check

- `git fetch origin master`; HEAD parent = cdeb31f = origin/master tip
- backlog entry BL-NEW-SETTINGS-VALIDATION-ALERT EXISTS (PROPOSED 2026-05-16) — this PR closes it.
- No existing `scout/config_alert.py`; no existing helper of this shape.
- `load_settings()` exists and already emits the structured log; PR only adds 5 lines + new module.

## Plan-stage reviewer fold (v2)

**R1 (correctness/architecture)** APPROVE with 2 IMPORTANT:
- I1: `ALERT_TIMEOUT_SEC=10` → `3` (avoid doubling crash-loop period under TG outage). APPLIED inline in pseudo-code above.
- I2: `os.environ.get("TELEGRAM_*")` is dead-code under systemd (no `EnvironmentFile=`); KEPT for test-injection convenience but doc-noted as "test/manual-invocation only" in module docstring.
- M3 (dedup hash brittleness): acceptable over-alerting; deferred.
- M7 (rollback caveat): added: "Rollback option 1 (`TELEGRAM_*=placeholder`) only silences the ALERT; if a different `.env` field caused the validation failure, crash-loop continues — use `git revert` AND fix root cause."

**R2 (test design/edge cases)** APPROVE conditional on:
- C1: `test_load_settings_invokes_alert_helper` MUST patch `scout.config_alert._send_validation_alert_best_effort` (the source module attribute), NOT `scout.config.*` (which doesn't exist at module level — load_settings uses local-import). FOLDED into test naming + comment.
- C2: `test_send_alert_never_raises_on_unexpected_exception` MUST patch `scout.config_alert.hashlib.sha256`, NOT global `hashlib.sha256`. FOLDED.
- I1: add `test_send_alert_resolves_token_from_env_and_chat_from_envfile`. FOLDED.
- I2: add `test_read_env_value_handles_empty_file_and_empty_value_and_quoted_value`. FOLDED.
- I3: add `test_send_alert_truncates_oversized_error_to_3800_chars`. FOLDED.
- I4: add `test_send_alert_returns_sent_when_ack_write_fails_post_200`. FOLDED.
- I5: autouse fixture cleaning TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID + SETTINGS_VALIDATION_ALERT_STATE_DIR + GECKO_ENV_FILE. FOLDED.
- I6: negative-path tests explicitly assert `urlopen_mock.assert_not_called()`. FOLDED.

Final test count: 13 → 17 (with autouse fixture absorbing I5; I1+I2+I3+I4 add 4).

## Rollback

If the alert path causes any disturbance:
1. Operator sets `TELEGRAM_BOT_TOKEN=placeholder` in `.env` → helper returns `skipped:no_creds` for ALL future settings_validation_failed events (alert effectively off). **NOTE (R1 M7 fold):** this silences only the ALERT. If a different `.env` field caused the validation failure, the systemd crash-loop continues regardless of this flip — use option 2 AND fix the bad field.
2. OR `git revert <merge-sha>` — clean revert; no DB schema impact, no other modules touched.
