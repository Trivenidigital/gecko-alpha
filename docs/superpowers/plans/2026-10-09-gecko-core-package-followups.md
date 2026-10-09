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
