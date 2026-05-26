**New primitives introduced:** NONE. This removes one noisy structured DEBUG event from the MiroFish fallback path; no new DB table, alert, endpoint, scheduler, or trading behavior.

# MiroFish Debug Noise Suppress Design

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Structured-log filtering | None found for gecko-alpha's local `structlog` fallback event. | Build the one-line local removal. |
| Journal hygiene | No checked Hermes skill owns srilu journald grep semantics. | Keep local and verify with journalctl after deploy. |
| MiroFish fallback behavior | Not applicable. | Preserve fallback scoring and parse-error paths. |

Awesome-hermes-agent ecosystem check: no checked skill replaces this repo-local observability hygiene change.

## Current Behavior

`scout/mirofish/fallback.py` calls Anthropic haiku fallback and then emits:

```python
logger.debug("fallback_raw_response", text=text[:300])
```

Prod evidence on 2026-05-26 from srilu:

- current SHA: `a455365`
- `fallback_raw_response_24h=50`
- `fallback_raw_response_7d=350`
- broad `error|exception|traceback` health grep saw `4` hits involving this event in 24h

This is noisy because project `structlog` DEBUG events still reach journald; adding TRACE or a global filter would widen scope.

## Target Behavior

Successful fallback responses do not log raw response text.

Parse failures still raise `FallbackScoringError` with truncated raw text:

```python
Failed to parse LLM response: <reason>. Raw text: <text[:200]>
```

No scoring, prompt, model, or gate behavior changes.

## Code Changes

Modify `scout/mirofish/fallback.py`:

- remove `import structlog`;
- remove `logger = structlog.get_logger()`;
- remove `logger.debug("fallback_raw_response", text=text[:300])`.

Do not change `_extract_json`, the Anthropic call, or the returned `MiroFishResult`.

## Tests

Modify `tests/test_fallback.py`.

Add import:

```python
import structlog.testing
```

Add `test_fallback_success_does_not_log_raw_response`:

- arrange a valid Anthropic JSON response;
- wrap `score_narrative_fallback(...)` with `structlog.testing.capture_logs()`;
- assert returned score still parses;
- assert no captured event has `event == "fallback_raw_response"`.
- assert the raw response string is absent from every captured log field so the test does not pass if the event is merely renamed.

Add `test_fallback_error_keeps_truncated_raw_text`:

- arrange invalid response text as `("A" * 200) + "TAIL_SENTINEL_AFTER_200"`;
- assert `FallbackScoringError` message includes `"Raw text:"`;
- assert it includes the first 200 chars;
- assert it does not include `TAIL_SENTINEL_AFTER_200`.

Do not use `caplog`.

## Backlog And Task Record

Update `backlog.md` after build/verification:

- status `SHIPPED 2026-05-26`;
- record pre-deploy runtime counts;
- record that successful fallback raw-response logging was removed;
- record post-deploy observation, distinguishing "no live fallback observed" from "fallback fired cleanly".

Update `tasks/todo.md` with plan/design/review/build/verification evidence.

## Verification

Local:

```powershell
uv run pytest -q tests/test_fallback.py
uv run pytest -q tests/test_gate.py tests/test_fallback.py
git diff --check
```

Post-deploy:

```bash
journalctl -u gecko-pipeline --since '<deploy time>' --no-pager | grep -c 'MiroFish failed, falling back to Anthropic' || true
journalctl -u gecko-pipeline --since '<deploy time>' --no-pager | grep -c 'Anthropic fallback also failed' || true
journalctl -u gecko-pipeline --since '<deploy time>' --no-pager | grep -c 'fallback_raw_response' || true
journalctl -u gecko-pipeline --since '<deploy time>' --no-pager | grep -Ei 'error|exception|traceback' || true
```

Interpretation:

- If fallback attempts are `0`, record "no live fallback observed" and do not claim the live path fired.
- If fallback attempts are `>0`, require `fallback_raw_response=0` and no Anthropic fallback failures attributable to this change.
