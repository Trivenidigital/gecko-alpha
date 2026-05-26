**New primitives introduced:** NONE. This removes one noisy structured DEBUG event from the MiroFish fallback path; no new DB table, alert, endpoint, scheduler, or trading behavior.

# MiroFish Debug Noise Suppress Plan

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Structured-log filtering | None found in Hermes skills hub that can remove a repo-local `structlog` event from gecko-alpha's Python fallback path. | Build in repo by deleting the noisy event and testing absence. |
| Journal hygiene / observability | No checked Hermes ecosystem primitive owns gecko-alpha journald semantics or `fallback_raw_response`. | Keep local and minimal. |
| MiroFish fallback behavior | Not a Hermes concern. | Preserve existing fallback scoring and parse-error behavior. |

Awesome-hermes-agent ecosystem check: no checked skill replaces this small repo-local observability hygiene patch. Hermes may remember the pattern, but the code change belongs in `scout/mirofish/fallback.py`.

## Runtime / Drift Findings

- Backlog item `BL-NEW-MIROFISH-DEBUG-NOISE-SUPPRESS` is still `PROPOSED`.
- In-tree drift check found exactly one producer: `scout/mirofish/fallback.py` emits `logger.debug("fallback_raw_response", text=text[:300])`.
- Existing tests cover JSON parse success, markdown JSON extraction, model selection, invalid JSON, and missing keys in `tests/test_fallback.py`.
- Prod runtime probe on srilu, 2026-05-26:
  - deployed SHA before work: `a455365`;
  - `fallback_raw_response_24h=50`;
  - `fallback_raw_response_7d=350`;
  - broad health grep saw `healthy_probe_hits_24h=4` involving this event.

## Decision

Remove the `fallback_raw_response` debug event entirely.

Rationale:
- The raw response is still included in `FallbackScoringError` for parse failures, truncated to 200 chars.
- Successful fallback responses do not need raw-response journaling.
- The project already learned that `structlog` DEBUG still appears in journald; demoting to another non-standard level would add complexity without improving the operator grep surface.

## Scope

1. Add a test that successful fallback scoring does not emit `fallback_raw_response`.
   - Use `structlog.testing.capture_logs()`; do not use `caplog`, which is vacuous for the repo's `structlog.PrintLoggerFactory()` setup.
2. Add a test that parse failures still include truncated raw text in the raised `FallbackScoringError`.
3. Remove `logger.debug("fallback_raw_response", ...)` from `scout/mirofish/fallback.py`.
4. Update `backlog.md` status for `BL-NEW-MIROFISH-DEBUG-NOISE-SUPPRESS` to `SHIPPED` after merge/deploy, with runtime before/after verification.
5. Update `tasks/todo.md` with plan/design/review/build/verification record.

## Non-Scope

- No MiroFish client changes.
- No fallback prompt/model/scoring changes.
- No new alerts, counters, DB writes, cron, or dashboard UI.
- No broad log filter or structlog configuration changes.

## Verification

- Red: `uv run pytest -q tests/test_fallback.py` fails on the new no-raw-response test before code change.
- Green: `uv run pytest -q tests/test_fallback.py`.
- Regression: `uv run pytest -q tests/test_gate.py tests/test_fallback.py`.
- Hygiene: `git diff --check`.
- Post-deploy:
  - Count positive fallback attempts in the deploy window via `MiroFish failed, falling back to Anthropic`.
  - Count fallback failures via `Anthropic fallback also failed`.
  - Count `fallback_raw_response`.
  - Run the broad backlog acceptance grep: `journalctl -u gecko-pipeline --since '<deploy time>' --no-pager | grep -Ei 'error|exception|traceback'`.
  - If fallback attempts are `0`, record smoke as "no live fallback observed" rather than proof of the changed path. If fallback attempts are `>0`, require `fallback_raw_response=0` and no Anthropic fallback failures attributable to this change.
