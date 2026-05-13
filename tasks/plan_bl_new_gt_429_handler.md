**New primitives introduced:** `scout.ingestion.geckoterminal._get_json(...)` helper mirroring the existing DexScreener retry helper shape; no new dependencies, settings, schema, service, or operator workflow.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| GeckoTerminal ingestion / trending-pools API | none found in installed VPS skills or public Hermes skills hub (`https://hermes-agent.nousresearch.com/docs/skills`, checked 2026-05-13) | Build inline; this is an existing gecko-alpha ingestion lane, not a Hermes runtime skill. |
| HTTP 429 / 5xx retry helper | adjacent installed Hermes utility `/home/gecko-agent/.hermes/hermes-agent/agent/retry_utils.py`, checked on srilu 2026-05-13, but it is a synchronous internal Hermes-agent jitter helper, not an aiohttp/gecko-alpha ingestion library | Do not vendor/import Hermes internals. Reuse the in-tree DexScreener aiohttp pattern instead. |
| Installed VPS skill/plugin surface | checked with `find /home/gecko-agent/.hermes/skills -maxdepth 3 -name SKILL.md` plus Hermes plugin/artifact scan for `gecko|terminal|http|api|retry|backoff|webhook|dex`; hits were `webhook-subscriptions`, `xurl`, debugging skills, `kol_watcher`, and Hermes-agent internal `retry_utils.py` | No installed operator-facing GeckoTerminal or aiohttp retry skill applies. |
| Webhook / notification skills | installed `devops/webhook-subscriptions` is inbound event delivery; not relevant to outbound GeckoTerminal polling | Not applicable. |
| X/social-media skills | installed `social-media/xurl` handles X API access only | Not applicable. |

awesome-hermes-agent ecosystem check: `https://github.com/0xNyk/awesome-hermes-agent` contains no GeckoTerminal/DexScreener ingestion or aiohttp 429 retry primitive suitable for this path.

Self-Evolution Kit check: `https://github.com/NousResearch/hermes-agent-self-evolution` is for improving Hermes agents/skills, not a runtime HTTP retry library for gecko-alpha ingestion.

One-sentence verdict: no Hermes skill/plugin should replace this local code; the correct low-debt fix is to reuse gecko-alpha's existing DexScreener retry/backoff pattern inside the GeckoTerminal ingestion module.

## Drift-check

| Check | Evidence | Verdict |
|---|---|---|
| Existing GeckoTerminal 429 handler | `scout/ingestion/geckoterminal.py` currently logs any non-200 and immediately continues | Missing; backlog finding is still live. |
| Existing in-tree pattern | `scout/ingestion/dexscreener.py::_get_json` retries 429 and 5xx with exponential backoff and logs attempts | Reuse pattern; no new abstraction needed. |
| Existing tests | `tests/test_geckoterminal.py` covers success, multi-chain, market-cap filtering, and generic API error, but no 429/5xx retry | Add focused tests. |
| Baseline targeted tests | `tests/test_geckoterminal.py tests/test_dexscreener.py` pass using the pre-provisioned project venv: 8 passed | Safe baseline for changed surface. |

# BL-NEW-GT-429-HANDLER Plan

## Goal

Make GeckoTerminal ingestion retry transient 429 and 5xx responses before giving up, reusing the HTTP-status retry shape from DexScreener while preserving current fail-soft semantics for transport errors and non-retryable statuses like 404.

## Scope

Modify only:
- `scout/ingestion/geckoterminal.py`
- `tests/test_geckoterminal.py`
- `backlog.md` status line for `BL-NEW-GT-429-HANDLER` after implementation
- `tasks/todo.md` active-work checklist

Do not change:
- GeckoTerminal chain list or ethereum 404 behavior; that remains `BL-NEW-GT-ETH-ENDPOINT-404`
- global rate limiter settings
- DexScreener, CoinGecko, or main pipeline orchestration

## Implementation shape

1. Add module constant `MAX_ATTEMPTS = 3`; this means three total HTTP attempts, not one initial plus three retries.
2. Add `_get_json(session, url, *, chain, max_attempts=MAX_ATTEMPTS) -> dict | list | None`.
3. In `_get_json`, retry only HTTP response statuses `resp.status == 429 or resp.status >= 500`.
4. For retryable statuses, emit stable structured warning event `geckoterminal_retrying` with `chain`, `url`, `status`, `wait`, `attempt`, and `max_attempts`, then `await asyncio.sleep(wait)` only when another attempt remains. Backoff sequence for attempts 1 and 2 is `1`, `2`.
5. On retry exhaustion, emit stable structured warning event `geckoterminal_retries_exhausted` with `chain`, `url`, `status`, and `max_attempts`, then return `None`.
6. Return `None` immediately on non-retryable non-200 statuses such as 404 and emit `geckoterminal_non_retryable_status` with `chain`, `url`, and `status`.
7. Preserve current transport-error behavior: catch `aiohttp.ClientError` and `asyncio.TimeoutError` once, log `geckoterminal_request_error`, and continue to the next chain without retry. This avoids broadening the backlog item into a cycle-latency change during full provider outage.
8. Update `fetch_trending_pools` to call `_get_json`; if it returns a non-dict or falsey value, continue to the next chain.

## Test plan

Use TDD in `tests/test_geckoterminal.py`:

1. Add `test_fetch_trending_pools_handles_429_with_backoff`: first response 429, second response 200 with `SAMPLE_POOL`, sleep patched via `patch_module_sleep("scout.ingestion.geckoterminal")`, expect one candidate.
2. Add `test_fetch_trending_pools_handles_5xx_with_backoff`: first response 503, second response 200, expect one candidate.
3. Add `test_fetch_trending_pools_exhausts_429_retries`: register three 429 responses, expect `[]`, assert exactly three GET calls, assert sleep durations `[1, 2]`, and assert `geckoterminal_retries_exhausted` is logged.
4. Add `test_fetch_trending_pools_does_not_retry_404`: register one 404 response, expect `[]`, assert exactly one GET call, assert no sleeps, and assert `geckoterminal_non_retryable_status` is logged.
5. Add `test_fetch_trending_pools_transport_error_does_not_retry`: simulate one `aiohttp.ClientError`, expect `[]`, assert exactly one GET attempt and no sleeps, preserving pre-existing fail-soft-fast behavior.
6. Use a local sleep spy in these tests rather than `patch_module_sleep`, because the tests must verify actual backoff durations.
7. Run targeted tests:
   `uv run pytest tests/test_geckoterminal.py tests/test_dexscreener.py -q`
8. Run ingestion-adjacent tests:
   `uv run pytest tests/test_geckoterminal.py tests/test_geckoterminal_rank.py tests/test_dexscreener.py tests/test_coingecko.py -q`

## Review gates

Plan review uses two vectors:
- Reviewer A: behavior / blast-radius / retry semantics
- Reviewer B: test design / operational observability

Design review repeats the same two vectors after `tasks/design_bl_new_gt_429_handler.md` exists.

PR review uses three vectors:
- code correctness and integration
- test adequacy / TDD proof
- operational risk and Hermes-first compliance

## Rollback

If the change misbehaves, revert the PR. The runtime behavior returns to pre-change fail-soft immediate skip on all non-200 GeckoTerminal responses. No DB or config rollback is involved.

Rollback triggers:
- Pipeline cycle latency increases materially after deploy, especially if GeckoTerminal outage causes repeated retry sleeps every 60s cycle.
- `journalctl -u gecko-pipeline --since '15 minutes ago' | grep -c geckoterminal_retries_exhausted` is high enough to dominate ingestion logs and no candidates recover.
- Any unexpected exception bubbles out of GeckoTerminal ingestion instead of returning `[]`.

Verification commands:

```bash
journalctl -u gecko-pipeline --since '15 minutes ago' | grep -E 'geckoterminal_(retrying|retries_exhausted|non_retryable_status|request_error)'
journalctl -u gecko-pipeline --since '15 minutes ago' | grep -E 'Pipeline cycle|pipeline_cycle|ingestion' | tail -50
```
