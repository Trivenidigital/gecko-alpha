**New primitives introduced:** NONE (analysis only).

# gecko-alpha runtime, Hermes and activation-readiness review (srilu-vps, 2026-10-16 ~01:00-01:10 UTC)

Read-only probes of prod @ 47aa3dc0. Only `/tmp/system-review/` was written (one side effect is disclosed in §2.4). Evidence is in appendix A1-A8. UNVERIFIED marks inference.

## Executive summary

1. The legacy stack is healthy but mostly idle. CoinGecko is dark (1,035 `cg_http_error` 401s/24h). No acceleration scans have run in 24 h. All signals are disabled.
2. **The legacy pipeline already saturates GeckoTerminal.** It made 263 retries on 429s in 24 h, and 111 Solana `new_pools` polls exhausted their retries (about 25% of those polls). gecko's `geckoterminal`, `geckoterminal-ohlcv` and the WBTC benchmark will make this worse.
3. **Alert spam.** `acceleration-heartbeat-watchdog` has no cooldown. It sends a Telegram message every 15 min (96 a day; 3,812 so far) to the legacy `TELEGRAM_CHAT_ID`. gecko must not share that chat.
4. **A daily cron job is silently broken.** `audit_stop_loss_false_negatives.sh` fails with `ERROR env_not_found path=.env` (98 times). It resolves `.env` relative to cron's working directory, `/root`.
5. **Disk is leaking about 180 MB a day while scout.db stays flat.** The largest contributor found is abandoned git `tmp_pack_*` files in the two Hermes installs: 2.2 GB across 19 files, one new pack almost every day.
6. Free disk is 18.0 GB. A gecko.db at 60 d retention plus the history copy plus backups fits only if the tmp_pack leak is stopped. Otherwise the disk fills in about 50 days (§3.3).
7. **Hermes (gecko-agent) is v0.19.1, not 0.21.6.** It already has `--no-agent`, `--script`, `--workdir`, `delegate_task`, `mcp_servers` and `max_parallel_jobs`. It has no `cron incidents` subcommand. **The gateway serves no messaging platform** ("No messaging platforms enabled"), so Q&A over Telegram does not exist today.
8. **The X scanner cron exists and reports `last_status: ok` every hour, but it is dead.** Every run says "0 new tweets … 1 blockers" because the X API returns `402 credits depleted`. Hermes shows it as green.
9. gecko prerequisites on the box: `uv` works, the import check passes, sqlite3 is 3.45.1, `/root/xs_study` holds 87 MB, and the Telegram, TG-API and Anthropic keys are present. **Absent:** a CoinGecko Demo key, a Helius key, `.env.gecko`, `gecko.db`, `/root/gecko-history`, `/root/.gecko`, and every gecko unit and timer.
10. Recommendation: schedule gecko as root systemd timers with a per-job `flock`, a dedicated health chat and its own backup rotation. Keep Hermes read-only for snapshot Q&A and do not upgrade it for Plan 3. Fix the tmp_pack leak and the GeckoTerminal budget before the soak.

## 1. Legacy runtime health and cost

### 1.1 Units (A1)

| unit | state | NRestarts | since | CPU total | mem (cgroup) | output |
|---|---|---|---|---|---|---|
| gecko-pipeline | active/running | 0 | 10-15 23:22 | 35 s | 790 MB (peak 922 MB) | 57.5k log lines/24h; 1,360 cycles/day (~63 s) |
| gecko-dashboard | active/running | 0 | 10-15 23:23 | 32 s | 80 MB | 18.7k access-log lines/24h; a client polls 5 endpoints every ~39 s |
| trade-alert-watcher | active/running | 0 | 10-02 | 111 s | 22 MB | 0 lines/24h; it tails the pipeline journal for trades, and none have happened since 08-09 |
| hermes-gateway (gecko-agent) | active/running | 0 | 09-11 | 4,194 s | 180 MB | cron ticker only |

The host has 3.8 GB RAM, **no swap**, and 2.9 GB available. Load average is 0.6.
Three non-gecko units are **failed**: `codex-auth-guard`, `codex-autonomous-dev-srilu` and `codex-readonly-operator-brief`, all with exit 21. Their OnFailure units fire Telegram alerts.

### 1.2 Top warning and error events, `journalctl -u gecko-pipeline --since -24h` (A1)

Levels over 24 h: 33,322 debug, 21,850 info, 2,321 warning, 51 error.

| count | event | meaning |
|---|---|---|
| 1035 | cg_http_error | 401 from pro-api: 841 markets, 168 trending, 23 /key. Expected, the Pro key is dead. |
| 676 | cg_no_data | same cause |
| 263 | geckoterminal_retrying | **429** on `solana/new_pools` |
| 112 | geckoterminal_retries_exhausted | 429 after 3 attempts, so the poll is lost |
| 92 | dex_discovery_malformed_payload | GT new_pools parse |
| 48+48+23+3 | coingecko_categories_error, narrative.observe_empty, cg_budget_reconcile_unavailable, resolver_auth_error | CG dark |
| 23 | sqlite_stale_reader_detected | pid 824797, see §1.5 |
| 23 | signal_outcome_ledger_REOPEN_E | economics deferral, informational |
| ≤5 each | tg_social_catchup_truncated (CATCHUP_LIMIT=0), parole_retest_stalled, DexScreener retry, GT/CG request_error | known or minor |

### 1.3 Upstream request rate of the legacy stack (estimated from config and event counts)

- **GeckoTerminal.** `CHAINS=solana,base,ethereum` times one `trending_pools` call per cycle comes to about 4,080 a day. `new_pools` for Solana every 3 cycles adds about 453 a day, matching the 454 `dex_discovery_ledger_pass` events. Retries add 263.
  - The total is about 4,800 a day, roughly 3.3 per minute on average. The calls are **bursty**: 3-4 calls within seconds every minute.
  - The `scps-writer` cron also calls GT keylessly every 15 min. Per-request counts are not logged (UNVERIFIED).
  - About 25% of `new_pools` polls still end in 429. GT's effective limit for this IP is below what the documented 30/min suggests, or it is burst-based (UNVERIFIED). GT responses carry no rate-limit headers (A6).
- **DexScreener.** Boosts plus `tokens/v1` run every cycle (1,360 "found candidates" a day). The liquidity-enrichment cron runs every 5 min (14 s per tick). There were only 3 errors in 24 h, so it is not contended.
- **GoPlus.** No legacy GoPlus events in 24 h. It is effectively unused.
- **Telegram sends.** The rate is low, but the main chat is flooded by the accel watchdog (§1.4).
- **CoinGecko.** Legacy calls go to `pro-api.coingecko.com` with the deactivated key. Those 401s do not touch the keyless or Demo quota on `api.coingecko.com`.

### 1.4 Cron lines and timers: do they still write anything useful? (A2, A6)

| job | status | useful? | competes with gecko? |
|---|---|---|---|
| source-calls-live-writer */5 | ok (updated 4,052, inserted 0) | yes, the legacy source_calls ledger | no |
| source-calls-lag-watchdog */10 | OK | yes | no |
| check_trade_decision_events */10 | ok | low (no trading) | no |
| **audit_stop_loss_false_negatives daily 09:20** | **BROKEN**: `env_not_found path=.env` ×98 | no, it cannot alert | no |
| **acceleration-heartbeat-watchdog */15** | **pages every run, no cooldown**: 96 a day, 3,812 total | no, the detector is dead because CG is dark | **Telegram chat noise** |
| held-position-price-watchdog */5 | OK (0 held) | low | no |
| revival-verdict-watchdog daily | ok | low | no |
| ledger-eviction-export weekly | ok | low | no |
| alert-channel-watchdog hourly | 2 standing breaches (digest stale 28 d, narrative inbound 114 d); 24 h cooldown | stale alarms | Telegram noise (2 a day) |
| liquidity-enrichment writer */5 + watchdog */10 | running; coverage 12/428 | marginal | **DexScreener** |
| suppression-cost-rollup weekly | ok | low | no |
| cg-ingestion-watchdog hourly | 2 breaches (CG dark), 24 h cooldown | known outage | Telegram noise |
| dex-discovery-watchdog hourly | ok | yes | no |
| scps-writer */15 | ok | yes (observe-only) | **GeckoTerminal** |
| solana-lane watchdog */2 | ok, nothing stuck | low | no |
| tg_burst_archive / wal_archive weekly | log empty since 05-31 | UNVERIFIED | no |
| timers: pilot, minara-emission, recompute-coverage (hourly); backup 03:00; backup-watchdog 09:00; drift 09:30 | all fired on schedule | backup yes, rest low | backup: disk and IO at 03:00 |

**Safe during coexistence:** everything except the GeckoTerminal consumers (pipeline trending and new_pools, plus scps-writer), the DexScreener consumers (pipeline and liquidity enrichment, which have headroom), and the Telegram noise sources.
**Will compete:** GeckoTerminal. It is already over the limit before gecko adds anything.

### 1.5 Other findings

- **Stale reader.** pid 824797 is `uv run python -` with stdin from a pipe, started 2026-09-03 from an SSH session. It has held `scout.db`, `-wal` and `-shm` open for 1,029 h. The pipeline warns 23 times a day and paged once. Killing it is an operator decision; it was not touched.
- `trade-alert-watcher` runs a permanent `journalctl -u gecko-pipeline -f` child process. It is harmless.
- journald is capped at `SystemMaxUse=2G` and already uses 2.1 GB, so retention is bounded by volume. Any extra gecko log volume shortens the evidence window for the legacy stack too.

## 2. Hermes (gecko-agent install)

### 2.1 Facts (A3, A4)

- Version: `Hermes Agent v0.19.1 (2026.7.30) · upstream 927662e4`. It is a git install with `hermes_agent-0.19.1.dist-info`.
  - The upstream tags include v0.21.5-v0.21.7 and v0.22.0-v0.22.2.
  - The root install at `/usr/local/lib/hermes-agent` is v0.14.0 and unused.
- `cron list` shows **one** job: `c849fffec986 gecko-x-narrative-scanner`, schedule `0 * * * *`, `no_agent: true`, script `gecko_x_narrative_scanner.sh`, workdir `/home/gecko-agent`, deliver `local`. Its last run was 2026-10-16T01:00:23Z with status **ok**.
- `config.yaml` top-level keys: `model` (provider `openrouter`, default `moonshotai/kimi-k2-thinking`), `terminal`, `browser`, `tool_loop_guardrails`, `compression`, `prompt_caching`, `memory`, `session_reset`, `group_sessions_per_user`, `streaming`, `skills.disabled`, `agent`, `platform_toolsets` (11 platforms listed), `stt`, `code_execution`, `delegation.max_iterations`, `display`, `plugins.enabled=[]`, `_config_version`.
  - There is **no `mcp_servers`**, no `cron` block, and no `delegation.model`.
- `~/.hermes/.env` key names: OPENROUTER_API_KEY, TERMINAL_*, BROWSER*, *_TOOLS_DEBUG, GECKO_ALPHA_BASE_URL, NARRATIVE_SCANNER_HMAC_SECRET.
  - **There are no TELEGRAM_*, DISCORD_* or SLACK_* keys.** errors.log at the last gateway start (09-11) says `No messaging platforms enabled.` The gateway serves **no** platform, and there are **no** allow-list keys.
- Skills include the bundled set plus local `coin_resolver`, `crypto_narrative_scanner`, `kol_watcher`, `narrative_alert_dispatcher`, `narrative_classifier`, `blockchain/{evm,solana}` and `mcp/native-mcp`. `scripts/` holds only the X scanner script and its backup.
- Feature checks:

| feature | present in 0.19.1? |
|---|---|
| `--no-agent` | yes |
| `--script` | yes |
| `--workdir` | yes |
| `--model` / `--provider` per job | yes |
| `delegate_task` | yes, 241 source files |
| `output_schema` | yes, 15 files |
| `mcp_servers` | yes, 159 files |
| `cron.max_parallel_jobs` | yes, default None, read in `cron/scheduler.py:4176` |
| `hermes cron incidents` | **no** (subcommands are list/create/edit/pause/resume/run/remove/status/runs/tick) |

- Permissions: `/root` is 0700, gecko-agent has no sudo and no crontab, and `sudo -u gecko-agent test -r scout.db` returns **CANNOT_READ_SCOUT**.

### 2.2 X scanner: exists, runs, and has been dead for at least 2.5 days

- Every retained output file from 2026-10-14 00:00 onward reads `SCANNER_CYCLE: 0 new tweets, 0 alerts dispatched, 1 blockers`. Only 50 output files are retained, so the start date is UNVERIFIED.
- The latest report shows `X API ERROR for @… : 402 credits depleted` for every KOL.
- The wrapper exits with the python status. That status is 0, so Hermes records `ok`. **This is a silent failure.** The X lane needs an owner decision on X API credits, which is spec §14.8.
- It also leaks one report file per hour into `/home/gecko-agent`: 3,689 files and 46 MB since 05-14, never pruned.

### 2.3 Verdicts

- **(a) A read-only `gecko-query` skill over a snapshot: YES, with two prerequisites.**
  - A root timer must publish a snapshot gecko-agent can read. Example: `sqlite3 /root/gecko-alpha/gecko.db ".backup /home/gecko-agent/gecko-ro/gecko.db.tmp"`, then `PRAGMA journal_mode=DELETE`, `chown gecko-agent`, `chmod 0440`, and an atomic rename.
  - The skill must open it with `?mode=ro&immutable=1`. The system sqlite3 is 3.45.1, so the agent's terminal tool can run it.
  - Telegram must be added to the gateway: a bot token, `TELEGRAM_ALLOWED_USERS` and a home channel. That is an **owner action**. A **new bot** is advisable, because the legacy bot's chat is flooded and two `getUpdates` consumers on one token conflict. Whether legacy polls `getUpdates` is UNVERIFIED.
- **(b) Agent jobs writing into `/root/gecko-alpha/gecko.db`: NO.** The user differs, `/root` is 0700, and there is no sudo. Spec §8 also names `python -m gecko.note`, and **that module does not exist** in `gecko/`.
  - A workable pattern: the agent writes JSON files into a gecko-agent-owned inbox. A root timer validates them against the schema and inserts them into `research_notes`. The root side owns the DB.
- **Upgrade to 0.21.6.**
  - It would mean `hermes update` (a git pull plus an editable reinstall in the venv) and a gateway restart. The cron ticker would pause; no chat platform is affected because none is enabled. A `_config_version` migration of config.yaml would follow (UNVERIFIED for 0.19 to 0.21).
  - Risks:
    - Every recent fetch of this repo appears to be aborted, leaving 270-280 MB `tmp_pack_*` files (§3.3), so `hermes update` may fail mid-pull.
    - The previous upgrade backup, `/root/hermes-upgrade-backup-20260801T175814Z` at 782 MB, is from 08-01.
    - Hermes warns that its linked SQLite 3.50.4 has the WAL-reset bug.
  - **Recommendation: do not upgrade for Plan 3.** 0.19.1 has every primitive Plan 3 needs. Scheduling moves to root systemd. Pin `v0.21.6` only when MCP or agent research jobs are actually built, back up `~/.hermes` first, and clear the tmp_packs first.

### 2.4 Side effect of this review (disclosure)

Running `hermes_cli.main --version` as gecko-agent at 01:02:16 appears to have started an upstream fetch. It left `/home/gecko-agent/.hermes/hermes-agent/.git/objects/pack/tmp_pack_Iro0vl`, 276 MB. The timestamp matches; the cause is inferred.
That file was not deleted, in line with the read-only rule. It is safe to remove with no git process running.

## 3. Activation readiness for gecko

### 3.1 Prerequisites on the box (A5)

| item | state | command | owner or automatable |
|---|---|---|---|
| uv | `/root/.local/bin/uv` 0.11.5 | `uv --version` | done |
| imports gecko, telethon 1.39.0, aiosqlite 0.22.1, aiohttp 3.13.3, structlog 24.4.0, pydantic_settings | **ok**, rc 0 | `uv run --frozen python -c "import …"` | done |
| `python -m gecko --list` | 14 jobs incl. rank, telegram-reader, tg-login | | done |
| sqlite3 CLI | 3.45.1 (python links 3.45.1) | `sqlite3 --version` | done |
| `/root/xs_study` | 87 MB, 3,689 files: charts/ 74 MB, btc.json, events.json, coins_list_platforms.json, v2/, extract/, prereg md | `du`, `ls` | done |
| legacy .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_HEALTH_CHAT_ID, TG_SOCIAL_API_ID, TG_SOCIAL_API_HASH, ANTHROPIC_API_KEY, COINGECKO_API_KEY | PRESENT | `grep -qE "^K=."` | copying values into `.env.gecko` is automatable with an operator OK |
| HELIUS_API_KEY, COINGECKO_DEMO_API_KEY, GOPLUS_* | ABSENT | same | **OWNER** (Demo key needed; Helius optional) |
| COINGECKO_API_TIER | `pro` (key deactivated) | | n/a for gecko |
| `GECKO_*` lines in legacy .env | 0 | `grep -c` | good |
| legacy Telethon session | `TG_SOCIAL_SESSION_PATH` unset, so the default file `/root/gecko-alpha/tg_social.session` (0600) is used | `find / -name '*.session'` | **never reuse** |
| `.env.gecko`, `gecko.db`, `/root/gecko-history`, `/root/.gecko`, gecko units/timers | ABSENT | `test -e` | automatable (`install -d -m700 /root/.gecko`, write env, units) |
| `GECKO_DB_PATH` / `GECKO_ENV_FILE` collision | confirmed: legacy scripts read both (A5); `gecko-backup.service` sets `GECKO_DB_PATH=…/scout.db` | grep | never export them globally; set them explicitly in each gecko unit |
| structlog JSON for journald (§12) | **not configured**: no `structlog.configure` anywhere in `gecko/`, so output uses the default console renderer with ANSI colour | local grep | automatable (code) |
| `gecko check` self-test | absent | | code |

### 3.2 Checklist lines (follow-ups doc, spec §11-§14)

| checklist line | state | class |
|---|---|---|
| Prefix rename / `application_id` guard before cutover | not done (code) | automatable (code + review) |
| `workdir=/root/gecko-alpha` per job | n/a under systemd: set `WorkingDirectory=` | automatable |
| `GECKO_DB_PATH` absolute, gecko.db in the backup set, disk check | absent; disk see §3.3 | automatable |
| `GECKO_GT_MIN_INTERVAL_SEC=3.0` plus OHLCV off the quarter-hours | **insufficient**: legacy already gets 429s (§1.3); see §5 | automatable |
| coingecko-markets timeout ≥ 20 min | `TimeoutStartSec=30min` | automatable |
| failure paging to the health chat | template `codex-systemd-failure-alert@.service` exists (`ExecStart=/usr/local/bin/codex-systemd-failure-alert %i`), already wired to pipeline, dashboard and gateway | automatable (`OnFailure=`) |
| per-job flock | absent | automatable (`flock -n /run/gecko/<job>.lock`) |
| agent-side CG MCP separate key | no MCP configured | owner (key) |
| `tg-login` with `GECKO_TG_SESSION_PATH=/root/.gecko/gecko_tg.session` | absent | **OWNER** (interactive code) |
| history copy checkpointed and DELETE-mode before 0444 | absent (needs 1.55 GB) | automatable (operator OK: disk) |
| `migrate-calls` after ingestion fills `tokens` | blocked on history plus ingestion; the `calls._matches` full scan is ticketed before running | automatable later |
| reader unit `Restart=always`, `RestartSec=30`, `OnFailure=` | absent | automatable |
| enrich hourly off :00; prune daily after backup | absent | automatable |
| `GECKO_OBS_RETENTION_DAYS` sizing (above 30 + 14) | see §3.3; 60 d is fine | automatable |
| `require_security_known` flip after full enrich | later | automatable after the soak |
| F4 backtest anchor on VPS study data | xs_study present; history copy absent | automatable after the history copy |
| verify every job ran once | | automatable (post-deploy) |
| X-lane own freshness key | X lane dead (402) | owner (credits) |
| GoPlus first-sight tax ruling | open | **OWNER ruling** |
| Hermes upgrade, MCP, skills (§11) | 0.19.1; no MCP; no Telegram platform | owner keys plus bot; deferrable |
| Demo CG key (§14.8) | absent | **OWNER** |

### 3.3 Disk headroom (A2, A7, A8)

- Free space now: **18,009 MB** of 76,588 MB (76% used). scout.db has been flat at 1,550 MB for 10 days.
- Backup preflight readings: 21,686 MB free on 10-06 and 20,076 MB on 10-15. That is a drift of **-179 MB/day** while the DB did not grow.
- **Largest identified contributor:** `tmp_pack_*` files.
  - `/usr/local/lib/hermes-agent` holds 16 files, 1.4 GB. One appears daily between 06:15 and 06:19, matching `codex-readonly-operator-brief.timer` (UNVERIFIED cause).
  - gecko-agent's install holds 3 files, 785 MB.
  - Other growth: `/var/log` is 2.8 GB, including btmp at 91 MB and syslog at 106 MB.
- Current fixed usage in `/root/gecko-alpha`: scout.db, 3 rotated backups and the pinned `scout.db.predeploy-47aa3dc0` add up to 5 × 1.55 GB = 7.8 GB. The pinned copy is outside the rotation glob, so it is never auto-removed.
- Projection at 60 d retention:

| | gecko-only additions | free at day 60 |
|---|---|---|
| gecko.db 40 MB/d × 60 | 2.4 GB | |
| history copy | 1.6 GB | |
| gecko backups, keep 2 | ~4.8 GB | |
| **subtotal** | **8.8 GB** | **≈ 9.2 GB** if the leak is fixed |
| + leak at 0.18 GB/d × 60 | 10.8 GB | **≈ -1.6 GB: disk full around day 50** |

- WAL and index overhead on 40 MB/day is UNVERIFIED. Measure bytes/row on day 1, as the checklist says.
- Reclaimable with no code change, all owner-approved deletions:
  - tmp_packs: 2.2 GB.
  - The predeploy pin: 1.55 GB, once rollback confidence exists.
  - Operator snapshots in `/root`: gecko-dashboard-release 11 GB, scout.db.pre500 5.5 GB, kraken_rehearsal 5.0 GB, dash11 2.7 GB, pre-deploy2 2.2 GB.

## 4. Upstream reachability from the box (A6)

Command: `curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 20 <url>`. GT headers carry `cache-control: max-age=30` and no rate-limit headers.

| endpoint | HTTP | time |
|---|---|---|
| DexScreener token-boosts/latest/v1 | 200 | 0.03 s |
| GT eth/trending_pools | 200 | 0.07 s |
| GT simple token_price WBTC (keyless) | 200 | 0.03 s |
| GoPlus token_security USDT | 200, `code:1 OK` | 0.26 s |
| CoinGecko keyless simple/price bitcoin | **200** `{"bitcoin":{"usd":82268}}` | 0.05 s |
| CoinGecko keyless coins/markets page 2 | **429** | 0.18 s |
| api.telegram.org | 302 (reachable) | 0.08 s |
| Anthropic /v1/models, no key | 401 `x-api-key header is required` | 0.15 s |

The WBTC body was:

```
{"data":{"id":"1c9364cb-…","type":"simple_token_price","attributes":{"token_prices":{"0x2260fac5e5542a773aa44fbcfedf7c193bc2c599":"82128.174085459"}}}}
```

That is within 0.17% of CoinGecko's BTC at the same minute.
**Verdict:** it is usable as a keyless `benchmark:btc` source at one call per hour. It draws on the already-contended GT budget, so put it on the same GT rate limiter. WBTC peg risk is small but real; a DexScreener WBTC pair is a fallback.
Keyless CoinGecko answers `simple/price` but rate-limits `markets`, so `coingecko-markets` still needs the owner's Demo key.

## 5. Risks of gecko as root systemd timers next to legacy, with mitigations

1. **GeckoTerminal 429 storm.** Legacy runs at about 3.3 requests/min in bursts and already loses about 25% of `new_pools` polls. gecko adds `geckoterminal` every 15 min, OHLCV up to 200 per run, and the hourly WBTC call. Without IP-level coordination, both stacks lose data.
   - Raise `GECKO_GT_MIN_INTERVAL_SEC` to at least 6.
   - Cap `GECKO_OHLCV_MAX_PER_RUN` at about 50 for the soak.
   - Schedule at :07/:22/:37/:52, away from the legacy minute boundaries and the scps-writer quarter-hours.
   - Watch `written==0 while items>0`.
   - Best fix: during the soak, turn legacy `DEX_DISCOVERY_ENABLED` off or set `DEX_DISCOVERY_POLL_EVERY_N_CYCLES` higher. That is an operator decision.
2. **Overlap and self-concurrency.** Every timer runs `flock -n /run/gecko/<job>.lock python -m gecko <job>`, exiting 0 or a distinct code when the lock is busy. The reader unit and `tg-login` share a `<session>.lock`, per the follow-ups.
   - `Type=oneshot` timers do not start a second instance while one is active, but manual runs can still overlap.
3. **SQLite busy.** gecko.db is a separate file from scout.db, so the two stacks never contend for a lock.
   - Inside gecko, the reader plus up to 3 timers write in WAL with `busy_timeout=30000` (`gecko/store.py:142`). A large `prune` DELETE can hold the write lock for more than 30 s.
   - Run `prune` alone at about 04:30, after the 03:00 backup finishes and before the hourly jobs, and delete in batches.
   - Do not run the history `.backup` copy while the 03:00 legacy backup runs. The IO is doubled and disk headroom falls by 1.55 GB at once.
4. **Backup interplay (confirmed safe and incomplete).**
   - `gecko-backup-rotate.sh` only matches `-maxdepth 1 -name 'scout.db.bak.*' -o -name 'scout.db.bak-*'` (`scripts/gecko-backup-rotate.sh:116-117`), so a `gecko.db.bak.*` file is **never touched**.
   - `gecko-backup-create.sh` writes `scout.db.bak.$TS` and cleans only its own `.partial` sidecars (lines 128, 141-146).
   - Consequences:
     - gecko backups need their **own rotation**, or they grow without bound.
     - The legacy preflight (`free ≥ db + 512 MB`) gets tighter as gecko files grow.
     - `gecko-backup-offhost` ships only the newest scout.db backup, so gecko.db has no off-host copy.
   - Write `gecko.db.bak.*` into `/root/gecko-alpha/backups-gecko/` with keep 2 and its own watchdog. Never pass `GECKO_DB_PATH` through the legacy backup units, which set it to scout.db.
5. **Env collision.** pydantic-settings lets process env override `.env.gecko`. A gecko job started from any unit or shell that exports `GECKO_DB_PATH=/root/gecko-alpha/scout.db` would write gecko tables into scout.db, and `prune` would delete rows there.
   - Every gecko unit sets `Environment=GECKO_DB_PATH=/root/gecko-alpha/gecko.db` explicitly.
   - Land the `application_id` guard before cutover.
6. **journald.** The cap is 2 GB and already full. The legacy stack writes about 76k lines a day, and gecko jobs with default console rendering add ANSI noise.
   - Configure the structlog JSON renderer at INFO before deploy.
   - Optionally run the dashboard with `--no-access-log`, an owner decision that frees about 18.7k lines a day.
7. **Alerting channel.** Do not reuse `TELEGRAM_CHAT_ID`, which gets 96 accel pages a day.
   - Point `GECKO_TELEGRAM_HEALTH_CHAT_ID` at a dedicated chat. The bot token can be reused for sends.
   - Fix or silence the accel watchdog by adding a 24 h cooldown like its siblings, or remove the line, since its detector is dead while CG is dark.
   - Wire `OnFailure=codex-systemd-failure-alert@%n.service` into every gecko unit. Verify the template's chat routing first: its script was not read, UNVERIFIED.
8. **Memory.** There is no swap and 2.9 GB is available. `coingecko-markets` (11 min) overlapping the reader and other jobs is fine, but cap with `MemoryMax=512M` per job unit so a runaway job cannot OOM-kill the legacy pipeline.
9. **Silent schedulers.** systemd timers give no "ran but wrote nothing" signal. That is exactly the X-scanner failure mode Hermes shows today.
   - Rely on gecko `freshness` for that.
   - Add the run-anchored `prune` key at 26 h.
   - Verify that every job has run once after deploy.
10. **Pre-existing box issues to clear before the soak** (owner): the stale reader pid 824797, the tmp_pack leak and its source, the broken stop-loss audit cron (needs `GECKO_ENV_FILE=/root/gecko-alpha/.env` or `cd`), and the three failed codex units that page on every run.

## Appendix: commands and excerpts

**A1 — runtime** (`systemctl show …`, `journalctl -u gecko-pipeline --since -24h -o cat | grep -o '"event": *"[^"]*"' | sort | uniq -c`):

```
gecko-pipeline NRestarts=0 MemoryCurrent=790323200 MemoryPeak=921624576 CPUUsageNSec=35144156000 ActiveEnter=2026-10-15 23:22:34
levels 24h: 33322 debug / 21850 info / 2321 warning / 51 error ; "Cycle complete" 1360
{"chain":"solana","url":".../networks/solana/new_pools","status":429,"max_attempts":3,"event":"geckoterminal_retries_exhausted"}
841 {"status":401,"url":"https://pro-api.coingecko.com/api/v3/coins/markets","event":"cg_http_error"}
{"pid":824797,"cmdline":"/root/gecko-alpha/.venv/bin/python3 -","age_hours":1029.4,"event":"sqlite_stale_reader_detected"}
● codex-auth-guard.service / codex-autonomous-dev-srilu.service / codex-readonly-operator-brief.service  failed (status=21)
```

**A2 — cron logs** (`stat` plus `tail -1` per `/var/log/gecko-alpha-*.log`):

```
acceleration-heartbeat-watchdog.log | ALERT_SENT: … detector heartbeat STALE — no acceleration_scan_complete …   (grep -c ALERT_SENT = 3812; acceleration_scan_complete in 24h = 0)
stop-loss-fn-audit.log | ERROR env_not_found path=.env   (count 98; script line 30: ENV_FILE="${GECKO_ENV_FILE:-.env}")
alert-channel-watchdog: digest_write_rate breach age_days 28; narrative_inbound_rate breach age_hours 2736.81
tg-burst-archive.log / wal-archive.log: size 0, mtime 2026-05-31
```

**A3 — Hermes** (`sudo -u gecko-agent env HERMES_HOME=… python -m hermes_cli.main --version | cron list | cron status | cron runs`):

```
Hermes Agent v0.19.1 (2026.7.30) · upstream 927662e4
c849fffec986 [active] gecko-x-narrative-scanner 0 * * * *  Mode: no-agent  Last run: 2026-10-16T01:00:23Z ok
✓ Gateway is running — cron jobs will fire automatically; Ticker heartbeat: 8s ago
errors.log 2026-09-11 06:08:20 WARNING gateway.run: No messaging platforms enabled.
upstream tags: v0.21.5 v0.21.6 v0.21.7 v0.22.0 v0.22.1 v0.22.2
cron subcommands: list,create,add,edit,pause,resume,run,remove,rm,delete,status,runs,history,tick
```

**A4 — X scanner** (latest `scanner-cycle-report-*.log`):

```
SCANNER_CYCLE: 0 new tweets, 0 alerts dispatched, 1 blockers   (every retained output since 2026-10-14 00:00)
X API ERROR for @100xgemfinder: 402 credits depleted   (same for every KOL)
```

**A5 — readiness** (`uv run --frozen python -c "import gecko, telethon, …"`, `grep` key names, `test -e`):

```
ok 1.39.0 0.22.1 3.13.3 24.4.0   rc=0
sqlite3 3.45.1 ; /root/xs_study 87M (charts 74M, 3689 files)
HELIUS_API_KEY ABSENT ; COINGECKO_DEMO_API_KEY ABSENT ; COINGECKO_API_TIER=pro ; GECKO_ keys in legacy .env: 0
CHAINS=["solana","base","ethereum"] SCAN_INTERVAL_SECONDS=60 DEX_DISCOVERY_ENABLED=true (every 3 cycles, solana)
legacy session: /root/gecko-alpha/tg_social.session (0600)
ABSENT .env.gecko, gecko.db, /root/gecko-history, /root/.gecko, gecko-telegram-reader.service
legacy scripts: DB_PATH="${GECKO_DB_PATH:-…/scout.db}" ×6, ENV_FILE="${GECKO_ENV_FILE:-…/.env}" ×19
```

**A7 — disk** (`df -B1M /`; backup preflight lines in `journalctl -u gecko-backup`):

```
/dev/sda1 76588 55411 18009 76%
preflight db=1550MB free=21686MB (10-06) … free=20076MB (10-15) … free=19922MB (10-15 23:21)
journald.conf.d/90-gecko-retention.conf: SystemMaxUse=2G ; /var/log/journal 2.1G ; /var/log 2.8G
```

**A8 — tmp_pack leak** (`ls -la …/.git/objects/pack | grep tmp_pack`):

```
/usr/local/lib/hermes-agent: count 16, 1.4G — daily 06:15-06:19 (10-08 … 10-15), plus 10-16 00:53
/home/gecko-agent/.hermes/hermes-agent: count 3, 785M — 08-01 17:51, 10-09 02:41, 10-16 01:02 (this review)
```
