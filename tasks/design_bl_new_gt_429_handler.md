**New primitives introduced:** None beyond `tasks/plan_bl_new_gt_429_handler.md`. This design fixes behavior shape, telemetry contract, and test matrix for the existing GeckoTerminal ingestion module.

## Hermes-first analysis

Per the plan's documented Hermes-first check:

| Domain | Hermes skill found? | Decision |
|---|---|---|
| GeckoTerminal ingestion / trending-pools API | none found in installed VPS skills or public Hermes skills hub (`https://hermes-agent.nousresearch.com/docs/skills`, checked 2026-05-13) | Build inline in the existing ingestion lane. |
| HTTP 429 / 5xx retry helper | adjacent installed Hermes internal `agent/retry_utils.py`, but it is synchronous Hermes-agent infrastructure, not an aiohttp ingestion helper | Do not import or vendor Hermes internals. Reuse gecko-alpha's DexScreener pattern. |
| Installed VPS skill/plugin surface | no installed GeckoTerminal/outbound HTTP retry skill; only adjacent `webhook-subscriptions`, `xurl`, debug skills, `kol_watcher`, and Hermes internal retry utility | Build inline. |

awesome-hermes-agent ecosystem check: no suitable GeckoTerminal/DexScreener/aiohttp retry skill in `0xNyk/awesome-hermes-agent`.

Self-Evolution Kit check: `NousResearch/hermes-agent-self-evolution` is not a runtime retry primitive.

Verdict: local implementation is justified; custom code is limited to the existing ingestion module and reuses in-tree conventions.

## Design

`fetch_trending_pools` remains the public entry point. A private `_get_json` helper centralizes response handling for one GeckoTerminal URL.

The helper makes at most `MAX_ATTEMPTS = 3` total HTTP attempts for retryable HTTP statuses. It retries only:

- `429`
- `5xx`

It does not retry:

- `404` and other non-200 non-transient statuses
- `aiohttp.ClientError`
- `asyncio.TimeoutError`

That boundary is intentional. The backlog item is an HTTP response handler defect, not a transport-outage latency change. Transport failures keep the current behavior: log once, return `None`, move to the next chain.

The latency tradeoff is explicit: `fetch_trending_pools` polls chains sequentially. In the common case where 429/5xx responses return immediately, each affected chain adds only the retry sleeps: 3s total (`1s + 2s`). In the pathological case where GeckoTerminal accepts the connection but each retryable response stalls until the current `REQUEST_TIMEOUT` of 30s, the bound is about 93s per chain (`3 * 30s + 1s + 2s`). This is accepted for HTTP 429/5xx because the previous behavior dropped the entire chain on the first transient response, but deployment verification must watch cycle latency. If this bound proves operationally disruptive, rollback is a simple PR revert.

## Telemetry

Stable log events:

- `geckoterminal_retrying`: emitted before a retryable sleep. Fields: `chain`, `url`, `status`, `wait`, `attempt`, `max_attempts`.
- `geckoterminal_retries_exhausted`: emitted when the final retryable response fails. Fields: `chain`, `url`, `status`, `max_attempts`.
- `geckoterminal_non_retryable_status`: emitted once for statuses such as 404. Fields: `chain`, `url`, `status`.
- `geckoterminal_request_error`: emitted once for transport exceptions. Fields: `chain`, `url`, `error`, `error_type`.

These replace the current generic `"GeckoTerminal returned error"` / `"GeckoTerminal request error"` messages for URL fetches so journalctl can distinguish retry, permanent failure, and transport failure.

## Backoff Semantics

Attempts are 1-based in logs:

| Attempt | Behavior on 429/5xx |
|---|---|
| 1 | log `geckoterminal_retrying`, sleep 1s, retry |
| 2 | log `geckoterminal_retrying`, sleep 2s, retry |
| 3 | log `geckoterminal_retries_exhausted`, return `None` |

No final-attempt sleep. This intentionally differs from `scout/ingestion/dexscreener.py`, which sleeps after the final failed attempt; GeckoTerminal should not add delay when no retry remains.

`Retry-After` is intentionally not honored in V1. GeckoTerminal rate posture is undocumented in the cycle audit, and honoring large provider-supplied delays inside this sequential ingestion lane could blow past the 60s pipeline cadence. Fixed capped waits are the safer first fix. If production shows repeated `geckoterminal_retries_exhausted` under 429, a separate follow-up can choose between bounded `Retry-After`, per-provider throttling, or chain-level cadence reduction.

Malformed `200` JSON remains out of scope and should keep the current behavior unless existing code already raises; this fix is only HTTP status handling. A future malformed-payload hardening PR can add fail-soft JSON decode behavior with separate tests.

## Tests

`tests/test_geckoterminal.py` gets focused tests:

- success after 429: two registered responses, one sleep `[1]`, one candidate
- success after 503: two registered responses, one sleep `[1]`, one candidate
- 429 exhaustion: three registered 429s, sleeps `[1, 2]`, zero candidates, exhaustion log
- 404 permanent failure: one registered 404, zero sleeps, zero candidates, non-retryable log
- transport exception: one `aiohttp.ClientError`, zero sleeps, zero candidates, request-error log

Tests use a local sleep spy patched onto `scout.ingestion.geckoterminal.asyncio.sleep` so they verify duration values, not just that tests run quickly.

## Operational Verification

After deploy:

```bash
journalctl -u gecko-pipeline --since '15 minutes ago' | grep -E 'geckoterminal_(retrying|retries_exhausted|non_retryable_status|request_error)'
```

Expected healthy states:

- occasional `geckoterminal_retrying` followed by recovered candidates
- `geckoterminal_non_retryable_status` for the known ethereum 404 side-finding until `BL-NEW-GT-ETH-ENDPOINT-404` is handled
- no unhandled GeckoTerminal exception in the pipeline logs

Pre-deploy latency check: the PR description must state the default chain count and the worst-case added latency bound using current `REQUEST_TIMEOUT`. If default `CHAINS` has three chains, the pathological bound is about 279s for a full GeckoTerminal slow-5xx outage. That bound is acceptable only because `main.py` already runs ingestion under `asyncio.gather` with other sources, and the rollback trigger below is explicit.

## Rollback

Revert the PR if cycle latency visibly worsens, a full GeckoTerminal outage delays the overall pipeline beyond operator tolerance, or retry exhaustion dominates logs during a provider outage. No schema/config rollback is required.
