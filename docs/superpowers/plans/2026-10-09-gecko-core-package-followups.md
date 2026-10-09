# gecko core package — follow-up tickets from the Plan 1 reviews (2026-10-09)

**New primitives introduced:** NONE (ticket list only).

Source: per-task and final-review findings triaged TICKET (not merge-blocking). Each line: area — finding — suggested fix — owner plan.

## Store / identity
- Observations retention (spec §4: 180 d) and a bound on the candidate admission scan — add a `prune` job — Plan 2.
- `runs` retention would reset the freshness bootstrap rule (empty tables stay silent) — prune `runs` only by age ≥ 90 d and keep the earliest ok row per job — Plan 2.
- `tokens.updated_at` is wall-clock; token state has no point-in-time provenance; an empty watchlist lets a rewound `--as-of` rank write disqualifications — document; consider `status_changed_at` — later.
- Two CoinGecko ids on one address re-fetch daily; a coin mapped under a since-unconfigured chain is never re-chosen — Plan 2 when chains change.
- Equal-timestamp observations resolve by `o.id`; sub-second canon_ts floor — accepted.

## Collectors
- `buys_24h`/`sells_24h` not coerced to int — fold into `gecko/numeric.py` — Plan 2.
- GeckoTerminal `mcap "0.0"` semantics unverified against a live response — probe once — Plan 3 soak.
- `geckoterminal_ohlcv_done` log should break down ok/empty/not_found — Plan 2.
- The two GeckoTerminal jobs pace independently (combined ~57 rpm if overlapping) — stagger cron schedules — Plan 3.
- A dead pool that stays deepest in the latest GT snapshot never gets candles — skip pools whose last marker was `not_found` within N days — Plan 2.
- CoinGecko: page reservation ignores retry attempts; concurrent sweep/hourly share no limiter; `CancelledError`/SIGTERM bypasses credit recording — stagger schedules, record credits incrementally — Plan 3.
- `UpstreamError.body` is a latent sink if ever logged — never log it — standing rule.
- Agent-side CoinGecko MCP (spec §11) shares the Demo key but `runs` cannot see MCP spend — separate key or a shared ledger — Plan 3.
- Verify `cg_max_pages=14` reaches the $500K floor (page 14 ≈ rank 3,500) — probe once — Plan 3.

## Features / rank
- `caller_quality` anchored at watchlist entry, not at call time — revisit with the caller lane — Plan 2.
- `young_chain` boundary now exact 180 d; exclusive bound at `t` unpinned; momentum "now" freshness unbounded (dead check covers it) — tests — Plan 2.
- `compute_features` ≈ 12 queries per token hourly; ever-exited active tokens still computed — skip exited ids — Plan 2.
- `rank_once` is not one transaction (next run repairs) — accepted.

## Ledger
- BTC primary window width unpinned between 11 h and 20 h — test — Plan 2.
- Permanent-403 token and BTC-gap-at-entry rows stay due forever with a log each run — add a max-age after which a row finalizes unmeasurable with a reason — Plan 2.
- Attempts bump once per run with no elapsed-time gate (ledger runs daily) — gate on time since `due_at` — Plan 2.
- Per-run `(chain, address)` price cache — Plan 2.

## Backtest
- Missing-history-DB guard unpinned by a test; malformed OHLCV rows crash the runner (catch TypeError/IndexError) — Plan 2.
- No `--from/--to`, no bootstrap CI recomputation (figures quoted from the study), FAIL verdict exits 0 — add flags and exit 1 on FAIL — Plan 2.

## Ops / CI
- `gecko` CI job (loc guard + black) is not a required check — add to branch protection — owner.
- gecko tests also run inside the legacy `test` job (redundant) — accepted.
- No boundary test at exactly 5,000 test lines; no gecko-missing-dir guard test — Plan 2.
- Notify tests lack timeout and non-200+ok:true cases; `--as-of` freshness backfill writes past-dated dedup rows — Plan 2.

## From the final fix-wave re-review (ticketed, not blocking)
- Ledger: `_resolve_entry` may freeze a CoinGecko fallback price before the 48 h window closes (1 d horizon resolves at ~24 h); take the CoinGecko fallback only after the deadline — Plan 2.
- Refresh queue: targets with no DexScreener pair sort NULL-first every run until aging removes them; order open-watchlist rows, then actives, ahead of the NULL-first key — Plan 2.
- Candidate aging keys on `first_seen_at`, so a token can be demoted mid-way through a legitimate 24 h survival window that started late; key on the last in-band reading instead — Plan 2.
- Structural probe in the session scratchpad uses an invalid address fixture and no longer runs — scratch only, no action.

## Plan 3 deployment checklist items (from the ops-safety and concurrency vectors)
- **FIRST: `GECKO_DB_PATH` is already used by legacy scripts (`scripts/gecko-backup-create.sh`, `scripts/chain-anchor-health-watchdog.sh`, `cron/README.md`) to mean the path to `scout.db`.** Never export it globally; set it only in `/root/gecko-alpha/.env.gecko` or the Hermes per-job env. Ticket: rename the gecko env prefix (e.g. `GK_`) before cutover.
- `.env.gecko` is resolved relative to the job's working directory: every Hermes job must run with `workdir=/root/gecko-alpha` (or an absolute `GECKO_ENV_FILE`).
- `GECKO_*` variables live in `/root/gecko-alpha/.env.gecko` (or the Hermes job env); NEVER in the legacy `.env` (scout `extra="forbid"` crash-loop).
- Set `GECKO_DB_PATH` absolute for every job; include `gecko.db` in the backup set; check srilu disk headroom before cutover (observations grow up to ~86k rows/day until pruning exists).
- During coexistence with the legacy pipeline (which polls GeckoTerminal every 60 s), set `GECKO_GT_MIN_INTERVAL_SEC=3.0` and schedule `geckoterminal-ohlcv` off the :00/:15/:30/:45 pool-job slots; stagger `coingecko-markets` away from `coingecko-hourly`.
- Hermes script timeout for `coingecko-markets` ≥ 20 min (normal run ≈ 11 min); credits are recorded only when a run ends.
- Wire Hermes cron incidents to the health chat; a `coingecko-markets` budget stop exits 0 by design (warning log only).
- Add a per-job `flock` (or Hermes `max_parallel_jobs`/no-overlap) so the same job never overlaps itself; rank-rank overlap can exceed the cap.
- Configure structlog JSON rendering for journald (spec §12) and add `gecko check` (config/Telegram/key self-test) before the soak.
- The agent-side CoinGecko MCP must use a separate Demo key or share the `runs` ledger; MCP spend is invisible to the cap.

## From the vector re-clears (ticketed)
- Freshness: put the overdue-row count in the `forward_returns_backlog` text so a new stall changes the page; per-source SLOs are silent while a writer has never run (a never-scheduled OHLCV/GT job is invisible — Plan 3 must verify every job has run once after deploy); OHLCV partial failures (error markers) keep the run green — add an error-ratio warning/breach.
- Ledger: canary is sampled once per run before token fetches; mid-run degradation can still spend attempts (bounded by the 20 h spacing).
- Ledger runbook: a watchlist entry whose BTC benchmark is missing for more than 36 h around `entered_at` stays due and pages via `forward_returns_backlog` every 6 h; remediation today is a manual `set_return` with a backfilled BTC price or an exit with reason `no_benchmark` — add an automatic finalizer (Plan 2).
- GeckoTerminal: with the per-source SLO anchored on the last ok run, a response-shape change that makes every pool skipped stays green; add a warning/breach when `written == 0` while `items > 0` — Plan 2. `Store.schedule_returns` is now unused (repair_schedules replaced it); remove — Plan 2.
