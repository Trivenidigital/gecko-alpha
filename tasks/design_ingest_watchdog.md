**New primitives introduced:** `IngestSourceSample` and `IngestWatchdogEvent` value objects; module-level per-source starvation state; `INGEST_WATCHDOG_ENABLED`; `INGEST_STARVATION_THRESHOLD_CYCLES`; per-source starvation and recovery structured logs; Telegram dispatch wrapper for watchdog alerts using existing `scout.alerter.send_telegram_message(parse_mode=None)`. No DB schema change.

# BL-NEW-INGEST-WATCHDOG Design

## Goal

Detect when an ingestion source keeps returning zero usable items while the pipeline itself continues to run, and alert the operator once per starvation episode.

## Drift Check

| Check | Evidence | Verdict |
|---|---|---|
| Existing aggregate heartbeat | `scout/heartbeat.py` tracks process-wide counters and emits `heartbeat`. | Reuse module, but insufficient for per-source starvation. |
| Existing ingestion failure logs | `scout/main.py` logs exceptions for DexScreener, GeckoTerminal, CoinGecko markets, trending, volume, and midcap. | Reuse errors as sample metadata, but logs are per-cycle and not episode alerts. |
| Existing failure-streak pattern | `_combo_refresh_failure_streak` and `_combo_refresh_streak_last_alerted` in `scout/main.py`. | Reuse one-alert-per-episode pattern. |
| Existing alert sender | `scout.alerter.send_telegram_message(..., parse_mode=None)` is used by system-health alerts. | Reuse; no new alert sender. |
| Existing backlog item | `backlog.md` marks `BL-NEW-INGEST-WATCHDOG` as proposed and net-new. | Proceed. |

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| In-process per-source ingestion starvation detection | None found in installed VPS skills or public bundled catalog. Installed skills include `devops/webhook-subscriptions`, but it handles external POST-triggered agent runs, not gecko-alpha cycle-local source counters. | Build custom inside existing heartbeat/main loop. |
| Operator notification delivery | Installed `devops/webhook-subscriptions` can deliver incoming webhook payloads to Telegram/Discord/Slack, including direct delivery mode. | Do not use for V1. It would require Hermes gateway/webhook runtime and a new internal POST path. Reuse existing gecko-alpha Telegram alerter, which is already configured and tested. |
| Generic monitoring dashboards | Awesome Hermes ecosystem lists Hermes UI/dashboard projects, including ops dashboards. | Track as future operator UX references only. They do not replace source-specific watchdog logic in gecko-alpha. |
| X/KOL narrative watcher | Installed `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, and `xurl` exist. | Not part of this ingestion-source gap. |

Awesome-hermes-agent ecosystem check: checked the public awesome list for monitoring/dashboard resources. It surfaces UI and ops dashboards, not a reusable CoinGecko/DexScreener/GeckoTerminal starvation watchdog.

One-sentence verdict: Hermes provides useful delivery and dashboard-adjacent capabilities, but no installed or public Hermes skill owns gecko-alpha's in-process per-source ingestion starvation state, so a small custom watchdog is justified while reusing existing project alert delivery.

## Runtime-State Verification

| Assumption | Runtime evidence | Impact |
|---|---|---|
| Pipeline service is active | VPS `gecko-pipeline` is active at commit `0ce1540`. | Watchdog will run in the existing loop once deployed. |
| Telegram credentials exist | VPS `.env` has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set. | Alert delivery can use existing alerter. |
| Default cycle interval | VPS `.env` sets `SCAN_INTERVAL_SECONDS=60`; observed cycles are roughly 80-100 seconds due API work. | Threshold 3 means roughly 4-5 minutes in practice. |
| CoinGecko API key | VPS `.env` has `COINGECKO_API_KEY` empty. | Watchdog must avoid noisy false positives during free-tier throttling. |
| Midcap cadence | Midcap scan skips 2 out of 3 cycles by design and returns on every third cycle. | Off-cadence midcap samples must be ignored by watchdog. |
| Current source health | Recent logs show CoinGecko markets, trending, volume, and midcap returning data. GeckoTerminal logs Ethereum 404 warnings but continues. | Initial deployment should not immediately alert for CoinGecko. GeckoTerminal behavior needs episode thresholding. |

## Design

1. Add small stateful helpers in `scout/heartbeat.py`.
   - `IngestSourceSample(name, count, expected=True, error=None)`.
   - `IngestWatchdogEvent(kind, source, consecutive_empty_cycles, threshold, last_success_at, error)`.
   - `_ingest_watchdog_state` stores `consecutive_empty`, `alerted`, and `last_success_at` by source.
   - `observe_ingest_sources(samples, settings, now=None)` returns events and logs `ingest_source_starved` / `ingest_source_recovered`.

2. Add config in `scout/config.py`.
   - `INGEST_WATCHDOG_ENABLED: bool = True`.
   - `INGEST_STARVATION_THRESHOLD_CYCLES: int = 5`.
   - Validator rejects values below 1.

3. Wire samples in `scout/main.py` immediately after `asyncio.gather` exception handling.
   - Track raw-source health, not post-filter candidate count. `raw_count > 0` is healthy even if `usable_count == 0`.
   - Candidate count is supporting context only, because quiet/filter regimes are not ingestion starvation.
   - Error lanes count as zero with `error=str(exc)`.
   - Exclude held-position refresh from V1 because it is price-cache maintenance, not candidate ingestion.
   - Include midcap only when its fetcher actually attempted a scan, not on off-cadence cycles.
   - Include GeckoTerminal chain-level samples (`geckoterminal:<chain>`) so one dead configured chain cannot be masked by healthy chains.
   - Include DexScreener token-detail samples (`dexscreener:tokens`) separately from boost-list samples so a healthy boost list cannot mask detail endpoint starvation.

4. Add minimal midcap cadence introspection in `scout/ingestion/coingecko.py`.
   - Maintain `last_watchdog_samples` metadata for each CoinGecko lane.
   - For midcap, emit `expected=False` at the start of every call, switch to `expected=True` only immediately before an actual scan, and reset helper clears both cadence and sample state.
   - Clear raw caches at fetch start where stale raw rows would otherwise mask source failure.
   - GeckoTerminal samples carry concrete failure context (`http_404`, `http_500`, exception type) into the watchdog event so operator alerts are actionable without journalctl spelunking.

5. Dispatch watchdog events in `scout/main.py`.
   - `dry_run=True` logs events but does not send Telegram.
   - Starvation and recovery both emit Telegram in normal mode.
   - Use `parse_mode=None` and `raise_on_failure=True`.
   - Log `ingest_watchdog_alert_dispatched`, `ingest_watchdog_alert_delivered`, and `ingest_watchdog_alert_failed`.
   - Add a per-event-source dispatch cooldown equal to the starvation threshold episode guard; duplicate starvation cycles do not send additional Telegrams until recovery resets the episode.

## Test Plan

1. `tests/test_heartbeat.py`
   - Threshold event only fires on Nth consecutive empty expected sample.
   - Duplicate empty samples after alert do not re-alert.
   - Recovery event fires on first non-empty sample after starvation and resets the episode.
   - `expected=False` samples do not increment counters.
   - `_reset_heartbeat_stats()` clears watchdog state.

2. `tests/test_coingecko.py`
   - Midcap off-cadence emits an `expected=False` watchdog sample.
   - Midcap attempted cycle emits an `expected=True` sample using raw fetched count, not gated token count.
   - Top movers / trending / volume samples use raw counts so zero usable candidates do not equal source starvation.

3. `tests/test_ingest_watchdog.py`
   - Telegram dispatch uses `parse_mode=None`.
   - Dry-run skips Telegram but logs.
   - Delivery failure logs failed and does not crash the cycle.
   - `run_cycle` calls `observe_ingest_sources()` and dispatches returned events.
   - GeckoTerminal chain-level samples are included when present.
   - Settings defaults and validator are covered.

4. Focused verification
   - `python -m pytest tests/test_heartbeat.py tests/test_ingest_watchdog.py tests/test_coingecko.py -q`
   - `python -m pytest tests/test_main_cryptopanic_integration.py::test_run_cycle_includes_midcap_gainers_in_aggregate_and_raw_cache -q`

## Rollout

Deploy behind enabled default because Telegram credentials are present and thresholding suppresses single-cycle noise. Revert knob is `INGEST_WATCHDOG_ENABLED=False`.
