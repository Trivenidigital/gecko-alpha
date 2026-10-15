# gecko core package — follow-up tickets from the Plan 1 reviews (2026-10-09)

**New primitives introduced:** NONE (ticket list only).

Source: per-task and final-review findings triaged TICKET (not merge-blocking). Each line: area — finding — suggested fix — owner plan.

## Store / identity
- ~~Observations retention (spec §4: 180 d)~~ — **CLOSED** 72e474c1 (+ 9bd6031a, 7d3f5c62): `prune` job, 180 d, `benchmark:btc` kept, 30 d floor. The candidate admission scan bound is still open — Plan 3.
- ~~`runs` retention would reset the freshness bootstrap rule~~ — **CLOSED** 72e474c1: `runs` pruned at 90 d, earliest ok row per job kept.
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
- ~~Permanent-403 token and BTC-gap-at-entry rows stay due forever~~ — **CLOSED** 9c51a1df: rows 14 d overdue expire with a reason.
- Attempts bump once per run with no elapsed-time gate (ledger runs daily) — gate on time since `due_at` — Plan 2.
- Per-run `(chain, address)` price cache — Plan 2.

## Backtest
- Missing-history-DB guard unpinned by a test; malformed OHLCV rows crash the runner (catch TypeError/IndexError) — Plan 2.
- ~~No `--from/--to`; FAIL verdict exits 0~~ — **CLOSED** 02813844: `--from/--to` window, FAIL exits 1. Bootstrap CI recomputation (figures still quoted from the study) remains open — later.

## Ops / CI
- `gecko` CI job (loc guard + black) is not a required check — add to branch protection — owner.
- gecko tests also run inside the legacy `test` job (redundant) — accepted.
- ~~No boundary test at the tests limit; no gecko-missing-dir guard test~~ — **CLOSED** 875f872c (`test_guard_passes_at_exact_tests_limit`, `test_guard_fails_when_tests_limit_exceeded`, `test_guard_exits_2_when_counted_dir_missing`; limits re-pinned at 3650/7500 in 985e0172).
- Notify tests lack timeout and non-200+ok:true cases; `--as-of` freshness backfill writes past-dated dedup rows — Plan 2.

## From the final fix-wave re-review (ticketed, not blocking)
- ~~Ledger: `_resolve_entry` may freeze a CoinGecko fallback price before the 48 h window closes~~ — **CLOSED** 9c51a1df: CoinGecko fallback only after the 48 h window.
- Refresh queue: targets with no DexScreener pair sort NULL-first every run until aging removes them; order open-watchlist rows, then actives, ahead of the NULL-first key — Plan 2.
- Candidate aging keys on `first_seen_at`, so a token can be demoted mid-way through a legitimate 24 h survival window that started late; key on the last in-band reading instead — Plan 2.
- Structural probe in the session scratchpad uses an invalid address fixture and no longer runs — scratch only, no action.

## Plan 3 deployment checklist items (from the ops-safety and concurrency vectors)
- **FIRST: `GECKO_DB_PATH` is already used by legacy scripts (`scripts/gecko-backup-create.sh`, `scripts/chain-anchor-health-watchdog.sh`, `cron/README.md`) to mean the path to `scout.db`.** The same collision holds for **`GECKO_ENV_FILE`**, which legacy code reads as the path to the legacy `.env` (`scripts/alert-channel-watchdog.sh`, `scripts/acceleration-heartbeat-watchdog.sh`, `scripts/audit_stop_loss_false_negatives.sh`, `scout/config_alert.py`); gecko does not read it at all. Never export either globally; set `GECKO_DB_PATH` only in `/root/gecko-alpha/.env.gecko` or the Hermes per-job env. Ticket: rename the gecko env prefix (e.g. `GK_`) before cutover.
- `.env.gecko` is resolved relative to the job's working directory: every Hermes job must run with `workdir=/root/gecko-alpha`.
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
- Ledger runbook: a watchlist entry whose BTC benchmark is missing for more than 36 h around `entered_at` stays due and pages via `forward_returns_backlog` every 6 h; remediation today is a manual `set_return` with a backfilled BTC price or an exit with reason `no_benchmark` — ~~add an automatic finalizer~~ **CLOSED** 9c51a1df (14 d overdue expiry).
- GeckoTerminal: a response-shape change that makes every pool skipped pages only after the 360 min row-anchored SLO; add a faster warning when `written == 0` while `items > 0` — Plan 2. ~~`Store.schedule_returns` is now unused; remove~~ — **CLOSED** 726d56eb. ~~The guard docstring in `scripts/gecko_loc_guard.py` is stale~~ — **CLOSED** 985e0172 (docstring and `LIMITS` both read 3650/7500).

## From Plan 2 (2026-10-14)
- GoPlus CU/call calibration — measure during the soak — Plan 3.
- `gp_daily_cap` tuning — calibrate from the GoPlus dashboard after day 1 — Plan 3.
- Cashtag-only calls never resolve to a token — by design, v1.
- A call resolved after `as_of` is visible to a replayed `as_of` (resolution is not point-in-time) — live-only ranking, accepted.
- X `ingest-call` CLI — deferred to Plan 3 with the X lane.
- `calls._matches` (`gecko/calls.py:76`) full-scans `tokens` (no index on `address`) every 120 s poll, for every address in 7 d of unresolved calls, and over the whole legacy history in `migrate-calls` (`since=None`) — switch to primary-key lookups (`token_id IN (token_id(c, a) for c in chains)`) — Plan 3, **before reader activation and before migrate-calls runs on the VPS**.
- A `source_calls` parse regression stays green forever: run-anchored freshness accepts ok runs that write 0 rows, so "every message parsed as no-call" never pages — add a long row-anchored key (e.g. `source_calls:rows`, 72 h) active only when channels are configured — Plan 3.
- Reader marks come only from stored calls: a channel with no stored call restarts newest-N only, so more than `tg_catchup_limit` messages during downtime are skipped under an ok run — persist per-channel marks (`tg_marks`) or document — Plan 3.
- GoPlus budget: `credits_used` counts tokens, not HTTP attempts (`get_json` retries up to 3× on 429/5xx, so the cap can undercount 4×); code 4029 (rate limit inside a 200 body) does not stop the run; two overlapping `enrich` runs share no cap reservation — calibrate in the soak before relying on `gp_daily_cap` — Plan 3.
- Handle normalization: `@foo`, `foo` and `t.me/foo` give different `source_id`/`event_id` — marks reset, calls duplicate, caller identity splits; consistent today only if the operator pastes the `migrate-calls` channel list — Plan 3.
- Spec drift left after the 2026-10-15 fold: §11 `goplus` MCP args, line 22 Hermes row, decoys — docs, Plan 3.
- GoPlus parsing: a malformed holder `percent` reads 0 %, and an all-excluded top 10 reads 0.0 — concentration fails open on malformed bodies only; the malformed-`dex` test is weak — later.
- `enrich --as-of` stamps live GoPlus data at the replayed time — docstring — later.
- DexScreener seeds: pair URLs posted as calls are offered as seeds (cap-bounded); `best_pair` trusts `chainId`; seed expansion of multi-chain addresses is untested — later.
- Telegram reader: FloodWait `seconds` is logged, not honoured; resolution rescans 7 d every poll; `tg-login` requires channels configured; `tg_catchup_truncated` log naming — Plan 3.
- `migrate-calls`: per-row commit fsync cost; removed channels untested; `study.py` URI escaping of the history path — later (run-after-ingestion is on the checklist).
- Prune: a reader with only failed runs loses them at 90 d (then reads as never-run); floor/boundary tests and an end-to-end `prune_job` test — later.
- Ledger: `close_unmeasured` docstring and expiry boundary tests — later.
- Backtest: window-bound pins; `--from` > `--to` is silently empty (exit 1 ambiguity is on the checklist) — later.

### Plan 3 deployment checklist additions
- Run `python -m gecko tg-login` once on the VPS as the gecko user with `GECKO_TG_SESSION_PATH=/root/.gecko/gecko_tg.session` (absolute, outside the repo); confirm mode 0600.
- **Never reuse the legacy `TG_SOCIAL_SESSION_PATH` as `GECKO_TG_SESSION_PATH`** — gecko needs its own session (another device to Telegram); sharing the legacy file collides with the legacy listener.
- The history copy (`/root/gecko-history/scout.db`) must be checkpointed / set to `journal_mode=DELETE` BEFORE `chmod 0444`: gecko opens it `immutable=1`, which ignores any `-wal` sidecar, so un-checkpointed rows are invisible.
- `python -m gecko migrate-calls --history-db /root/gecko-history/scout.db` AFTER the read-only copy exists; paste its `GECKO_TG_CHANNELS=` line into `.env.gecko`. Run it after ingestion has filled `tokens` (or re-run it then) — calls only resolve to tokens that exist.
- `gecko-telegram-reader.service`: `Restart=always`, `RestartSec=30`, `WorkingDirectory=/root/gecko-alpha`.
- Cron: `enrich` hourly (stagger off :00), `prune` daily (off-peak, after backup).
- Set `GECKO_OBS_RETENTION_DAYS` from measured observation bytes/row and the disk headroom (floor 30).
- Flip `require_security_known=True` only after `enrich` has covered every active token at least once.
- Re-run the F4 backtest anchor on the VPS study data; expect PASS with rc 0. Backtest exit 1 means FAIL **or** a crash — read the output, not just the code.
- Verify every job has run once after deploy: a never-run writer is invisible to freshness.
- An X-lane writer must not share the `source_calls` freshness key with `telegram-reader` — give xkol its own key, or a live reader masks a dead X lane (and vice versa).

## From the PR #598 vector reviews (2026-10-15)
Format: area — finding — fix — owner plan. Items fixed in the PR are not listed.

**Ops-safety (vec-ops)**
- Reader — one handle that will not resolve kills the whole reader on every restart (`get_input_entity` in a dict comprehension); it now pages, but no channel is read — resolve handles one by one, skip and log the bad one — Plan 3.
- Reader unit — a crash loop has no systemd-level alert — add `OnFailure=codex-systemd-failure-alert@%n.service` (template exists on prod) to `gecko-telegram-reader.service` — Plan 3.
- `tg-login` — silently adopts an existing file at `GECKO_TG_SESSION_PATH`, so a COPY of the legacy session outside the repo runs as the legacy device — refuse when the file exists (force a fresh device) — Plan 3.
- Store — `prune` is the first DELETE path and only a schema clash stops it on the wrong DB (a leaked legacy `GECKO_DB_PATH` would create gecko tables inside scout.db before failing) — stamp and check `PRAGMA application_id` in `Store.open`, refuse a foreign DB before any DDL; raises the priority of the prefix rename — Plan 3, before cutover.
- Ledger — expiry is a silent automated closure (a warning log only) and expired rows leave the backlog count — put the `expired` count in the health page / digest — Plan 3.
- Retention — at the 30 d floor plus a ledger outage over 14 d, a late entry-price resolution finds the 48 h window already pruned — size `GECKO_OBS_RETENTION_DAYS` well above `30 + ledger_max_overdue_days` — Plan 3 (sizing).
- Checklist — Telethon does not create the session directory and `session_problem` checks only the file mode — `install -d -m 700 /root/.gecko` before `tg-login` — Plan 3.
- Checklist — with `GECKO_TG_CHANNELS` set and the unit not yet installed, freshness pages `source_calls` every 6 h (intended) — set `GECKO_TG_CHANNELS` only when the unit goes live — Plan 3.

**Silent-failure (vec-silent)**
- Enrich — a token that fails GoPlus deterministically (error code, non-retryable 4xx) writes no row, stays at the FIFO head and burns a call every hour for 14 d; enough of them starve new candidates while the run stays ok — write a `{"goplus_status":"error","code":…}` marker (freshness/features already ignore non-ok rows) and log the full queue length — Plan 3.
- Ledger — expired rows carry no durable reason (all value columns NULL, the cause only in journald), a survivorship risk for the scorecard denominator — a status/reason column or sentinel, or the scorecard reports the expired cohort; `rows_written` should not count expiries — Plan 3, before the scorecard.
- Prune — nothing watches that `prune` keeps running (disk-cliff history on this VPS) — a run-anchored freshness key on `prune` at ~26 h — Plan 3.
- Enrich — a per-token `security_fail` disqualification has no `token_id` log line — log it per token — later.
- Enrich — `--as-of` stamps live GoPlus data at a past `observed_at` (replay lookahead) — refuse `--as-of` like `ledger`/`prune` — later.
- Freshness — the never-started-reader anchor needs the freshness cron's env to carry `GECKO_TG_CHANNELS`; a separate unit EnvironmentFile or a different `GECKO_DB_PATH` silently disables it — ONE shared env file for cron and the unit, plus a deploy check that both resolve the same `db_path` and `tg_channels` — Plan 3 checklist.
- Freshness — the never-started anchor is the first freshness run ever, so configuring channels on an existing DB pages at once with no 30 min grace (loud, not silent) — accept, or anchor on the config change — later.
- Reader — if `fail()` itself raises (a lock), it replaces the original cause in `job_crashed` (the original is only on `__context__`) — log the original before recording — later.

**Concurrency (vec-concurrency)**
- Reader / `tg-login` — nothing stops a second Telethon client on the same session (`AUTH_KEY_DUPLICATED` can revoke it) — `fcntl.flock(LOCK_EX|LOCK_NB)` on `<session>.lock` in both jobs, exit 2 when held — Plan 3, before reader activation.
- Enrich — the budget is checked before the run and spent with no hold: two overlapping runs each spend the full remainder; SIGKILL or a Hermes timeout records no credits — a per-job `flock` or Hermes no-overlap (Hermes overlap behaviour unverified) — Plan 3.
- Runs — the failure-path `record_run` (run_job and the reader's `fail()`) can hit the same lock that caused the failure, losing the row and its credits — retry the failure row — later.
- Reader unit — start the unit BEFORE adding channels to the freshness env (else one false page, deduped 6 h); consider `RestartPreventExitStatus=2` so a config error (rc 2) stops instead of looping — Plan 3 runbook.
- Reader — any SQLite error crashes the process and recovery relies on `Restart=always` (no data loss: marks recompute from stored calls, inserts are idempotent) — accept, or catch per poll — later.
- Reader — SIGTERM skips the `finally` cleanup (harmless for gecko.db; the Telethon session's update state is not saved) — `loop.add_signal_handler(SIGTERM, task.cancel)` — later.

**Logic (vec-logic)**
- Enrich — **OPERATOR RULING NEEDED before the soak writes rows:** a first-sight GoPlus read disqualifies a candidate permanently (`security_fail` is never re-read), and anti-snipe launch taxes over 10 % that drop to 0 within hours are common, so the early cohort the user targets is killed for good — re-read before disqualifying, or disqualify only on immutable flags (honeypot/blacklist) and leave taxes to the point-in-time feature — Plan 3, operator.
- CoinGecko — prune deletes `cg_unmapped` markers at 180 d; the bulk initial sweep ages out together and every coin is re-looked-up via `/coins/{id}` (capped 300/run, draws on the 8000/month budget) — exempt `cg_unmapped` from prune, or document a 180 d re-check as intended — Plan 3.
- GoPlus coverage gaps (all under-flag; spec §6 implemented as written) — EVM `is_open_source=0`, `can_take_back_ownership`, `hidden_owner`, `owner_change_balance` unchecked (a reclaimable "renounced" owner passes); a missing `is_mintable`/`is_proxy` reads clean; EVM concentration skips ALL contracts (team Safes, the 22.6 % BRETT holder); Solana Token-2022 `transfer_fee` (a sell tax) unchecked; non-Raydium Solana AMMs rely on GoPlus listing the pool in `dex[]` (unverified) — extend the flag set and verify AMM coverage — Plan 3 / later.
- Ledger — the 1 d horizon can be measured at 30-48 h or later (the CG fallback waits for the 48 h deadline); spec-intended, but the `due_at`→`measured_at` delay is unflagged; no consumer today — flag or document — later.
- Enrich queue — the comment says "never-enriched first" but watchlist then actives sort ahead; above ~800 actives due per 20 h, candidates are never enriched — fix the comment, monitor the queue — later.
- Backtest — a malformed `--from`/`--to` crashes with a traceback (uncaught ValueError after `load_study`), rc 1 indistinguishable from FAIL — validate the dates up front, rc 2 — later.
- Enrich — `--as-of` replay lookahead — see the silent-failure entry above.
