**New primitives introduced:** Configurable minimum inter-request spacing and optional per-request jitter in the shared CoinGecko async rate limiter.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| CoinGecko market-data API reference | Yes, CoinGecko publishes an API SKILL/reference package, and Hermes optional blockchain skills mention CoinGecko for token pricing. | Do not replace runtime code; these are API-reference/pricing helpers, not a gecko-alpha aiohttp burst-smoothing primitive. |
| Generic retry / rate-limit helper | No installed/public Hermes operator-facing skill found for an async Python limiter suitable for gecko-alpha ingestion. Hermes-agent internals have adjacent retry/backoff utilities, but they are not a stable project dependency. | Build a small extension in the existing `scout.ratelimit.RateLimiter`. |
| CoinGecko top-gainer / breadth ingestion | No Hermes skill found that replaces gecko-alpha's scanner-owned DB writes, source attribution, scoring, watchdogs, and dashboard contracts. | Keep ingestion in gecko-alpha; smooth request cadence at the shared limiter. |

awesome-hermes-agent ecosystem check: `0xNyk/awesome-hermes-agent` search surfaced Hermes platform resources and agent integrations, but no CoinGecko market-screening or aiohttp burst limiter to import for this path.

One-sentence verdict: Hermes can inform API usage, but no installed/public Hermes capability should own gecko-alpha's CoinGecko request scheduling; extending the existing shared limiter is the lowest-debt fix.

## Drift check

- Existing primitive: `scout.ratelimit.RateLimiter` is already shared by CoinGecko call sites.
- Existing behavior: rolling-window cap (`COINGECKO_RATE_LIMIT_PER_MIN`, default 25/min) plus global backoff after 429.
- Missing behavior: no minimum inter-request spacing, so concurrent CoinGecko lanes can acquire back-to-back and create provider-visible bursts despite staying under the rolling minute cap.
- Backlog status: `BL-NEW-CG-RATE-LIMITER-BURST-PROFILE` remains proposed and specifically calls out inter-call jitter as an allowed fix shape.

## Runtime evidence

Post-deploy logs showed active CoinGecko throttling after the ingestion watchdog and midcap scan were deployed:

- `cg_429_backoff` remained frequent.
- Global limiter backoff was firing, which serialized later calls behind provider throttles.
- Parsed cycle cadence over 72 recent cycles: min ~85s, average ~101s, p90 ~133s, max ~263s against the configured 60s cadence.

The symptom is not "too many average requests per minute"; it is burst profile plus synchronized retry waves.

## Design

Add two settings:

- `COINGECKO_MIN_REQUEST_INTERVAL_SEC`: default `0.75`
- `COINGECKO_REQUEST_JITTER_SEC`: default `0.25`

Thread both through `configure_from_settings()` into the shared `RateLimiter`.
`configure_from_settings()` must mutate the existing singleton in place rather
than rebinding it, because `scout.main` imports CoinGecko modules before startup
configuration and those modules hold `coingecko_limiter` by value.

In `RateLimiter.acquire()`:

1. Preserve existing global 429 backoff behavior.
2. Preserve existing rolling-window cap behavior.
3. After those waits, if a previous request was issued too recently, sleep until `last_acquire_at + min_interval + jitter`.
4. Append the timestamp and update `last_acquire_at` only when the request is actually released.

Use lock-held sleep intentionally: this limiter is a shared per-provider gate, so serializing callers is the smoothing mechanism.

## Test plan

- Existing baseline: `tests/test_ratelimit.py tests/test_config.py` -> 35 passed before edits.
- TDD red: add a test proving consecutive acquires with `min_interval_seconds=0.75` sleep before releasing the second and third request.
- Add a deterministic jitter test using injected `random_fn`.
- Add a reset test assertion that reset clears last-acquire spacing state.
- Add a singleton-identity regression test so startup configuration reaches
  modules that imported `coingecko_limiter` before `configure_from_settings()`.
- Add config/default tests for the new settings.
- Run focused verification:
  - `tests/test_ratelimit.py`
  - `tests/test_config.py`
  - `tests/test_coingecko.py`

## Deployment verification

After deploy, check journalctl for:

- Reduced `cg_429_backoff` count over a comparable post-restart window.
- Fewer `rate_limiter_global_backoff` entries.
- Cycle intervals closer to 60s plus normal ingestion latency.

If throttles persist, the next knob is lowering `COINGECKO_RATE_LIMIT_PER_MIN` from 25 to 20 or adding a free Demo API key; do not reduce scanner breadth before measuring the smoothed profile.
