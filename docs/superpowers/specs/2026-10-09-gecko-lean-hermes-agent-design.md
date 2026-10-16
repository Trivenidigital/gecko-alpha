# gecko — lean Hermes-orchestrated early-winner watchlist (design spec)

**New primitives introduced:** `gecko/` package (collectors, store, features, rank, ledger, backtest, freshness); `gecko.db` (7 tables); Hermes cron jobs (script-mode collectors, agent-mode research/digest/scorecard); Hermes MCP servers (coingecko, helius, goplus, blockscout); forward-return ledger; pre-registration protocol for ranking rules. (Amended 2026-10-16, Plan 3: scheduling is systemd timers as root over the template unit `gecko-job@.service`, not Hermes cron; `research`/`digest`/`scorecard` are Python jobs; Hermes adds only the read-only `gecko-query` skill over a nightly snapshot; no `goplus` MCP server.)

Status: DESIGN APPROVED conversationally 2026-10-09 (operator rulings: greenfield package + freeze old; retire all trading/execution; ~$50-100/mo data budget; lightweight process). Spec awaiting operator review before the implementation plan is written.

Companion evidence: `docs/audit_structural_2026_10_09.md`, `docs/research_hermes_crypto_ecosystem_2026_10_09.md`.

---

## Hermes-first analysis

**Domains checked against the hub at `hermes-agent.nousresearch.com/docs/skills` (official optional catalog, 156 skills) and GitHub taps, 2026-10-09:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Scheduling / retries / incidents | yes — built-in cron (`hermes cron`, jobs.json, incidents, retries 5/15/30 min) https://hermes-agent.nousresearch.com/docs/user-guide/features/cron | use it; replaces main.py loop, 5 systemd timers, 19 cron lines (amended 2026-10-16: NOT used for the script jobs — they run on systemd timers as root, see §3; Hermes v0.14.0 runs as `gecko-agent`, which cannot read `/root/gecko-alpha`) |
| Poll-and-dedupe JSON endpoints | yes — `official/devops/watchers` (`watch_http_json.py`, watermark state) | not used in v1: it emits new ids but does not persist feature rows, and running it beside the collectors would create two dedupe mechanisms; the Python collectors dedupe on `tokens.token_id`. Revisit only if collectors grow past budget |
| Market data (listed coins, trending, new pools, holders) | yes — CoinGecko official remote MCP (keyless or Demo key) + `coingecko/skills` | use it; no Pro dependency |
| Solana token / holders / tx | yes — Helius official MCP; `official/blockchain/solana` (authorities, top-5 holders) | agent-side MCP only (Plan 3); the deterministic write path uses GoPlus, which returns holders + top-10 on all four chains keyless (probed 2026-10-14; Blockscout base is Cloudflare-blocked and has no BSC instance) (amended 2026-10-14); solana skill stays read-only per inventory ruling |
| EVM explorer data | yes — Blockscout hosted MCP (free PRO key); `official/blockchain/evm` (reference only per inventory) | agent-side MCP only (Plan 3); the deterministic write path uses GoPlus, which returns holders + top-10 on all four chains keyless (probed 2026-10-14; Blockscout base is Cloudflare-blocked and has no BSC instance) (amended 2026-10-14) |
| Smart-money / launch trenches / contract DD | yes — GMGNAI/gmgn-skills (vendor SKILL.md, demo key read-only) | install read-only subset; execution skills excluded |
| Holder-cluster / sybil / bundle analysis method | yes — agiprolabs/claude-trading-skills `token-holder-analysis`, `sybil-detection`, `pumpfun-mechanics` | install as method prompts for the research agent |
| X / KOL narrative | partial — built-in `xurl` + existing gecko skills (kol_watcher, narrative_classifier) | keep existing skills; convert cron to agent mode; needs X credentials re-paired (owner) |
| Telegram channel reading (user account) | none — gateway is Bot API only | build thin Telethon collector (fold from existing listener) |
| Durable datastore | none — memory is 2.2 KB; state.db is sessions only | build `gecko.db` (SQLite, 7 tables) |
| Feature computation / ranking / backtest | none | build (deterministic Python) |
| Delivery (Telegram digest, Q&A) | yes — gateway + cron `deliver: telegram:<chat>`; `[SILENT]` | use it; replaces alerter.py, trade-alert-watcher, dashboard (amended 2026-10-16: digest/scorecard are deterministic Python jobs sending through the Bot API; the gateway serves Q&A via the `gecko-query` skill, §8) |
| Watchlist page | yes — `official/productivity/live-dashboard` | optional v1.1; v1 delivers via Telegram only |
| Research dossiers | yes — `delegate_task` with `output_schema`, `delegation.model` | deferred (amended 2026-10-16): not verified on v0.14.0 and the agent cannot write `gecko.db`; built as the Python job `research` over the Anthropic Messages API, same output schema (§8) |

**Awesome-hermes-agent ecosystem check:** 0xarkstar, ZeroPointRepo and 0xNyk lists checked; no maintained on-chain discovery source beyond the items above (details in the research doc).

**Verdict:** Hermes supplies scheduling, polling, market/on-chain/security data access, research, memory and delivery; custom code is limited to the datastore, deterministic features/ranking, the Telethon collector, the forward ledger and the backtest harness (~1,800 LOC). Tag: extends-Hermes.

---

## 1. Goal and success criteria

**Goal.** Surface small-cap crypto tokens early enough to hold for ~6 months at 7x+ potential, as a single ranked watchlist with an honest forward track record, operated through Hermes with minimal custom code.

**Success criteria (measurable):**

| # | Criterion | Target |
|---|---|---|
| S1 | Custom code | `gecko/` ≤ 3,000 LOC excluding tests (raised from 2,500 → 2,800 → 3,000 on 2026-10-09: the per-token refresh pass, frozen entry price, address validation, ledger canary, per-source freshness SLOs and concurrency repairs required by the independent reviews were missing from the original estimate; still ~36× smaller than `scout/`); tests ≤ 6,000 LOC (raised from 5,000 on 2026-10-09 for the falsifier tests the vector reviews required); raised 3,000 → 3,600 and tests 6,000 → 7,500 on 2026-10-14 (operator ruling: GoPlus enrichment, Telegram reader, prune and ledger tickets were in scope but not in the estimate); raised 3,600 → 3,650 on 2026-10-14 (Plan 2 execution: ~100 lines of review-mandated fixes — unknown-as-safe and crash-path hardening, Telegram catch-up loss, failing-reader paging, tg-login umask, immutable read-only opens, prune --as-of guard; no dead code remained); raised 3,650 → 4,500 and tests 7,500 → 9,500 on 2026-10-16 (Plan 3: §8 agent jobs, `check`, keyless BTC benchmark, 14 pre-activation review tickets; final planned scope) |
| S2 | Discovery breadth | ≥ 3 independent discovery sources alive (DexScreener, GeckoTerminal, CoinGecko new/trending, GMGN trenches, Telegram callers) with per-table freshness within SLO for 14 consecutive days |
| S3 | Delivery | Daily digest delivered to Telegram 14 consecutive days; `[SILENT]` days count as delivered |
| S4 | Track record | Forward returns at 1/7/30/90/180 d recorded for 100% of watchlist entrants; weekly cohort scorecard vs BTC |
| S5 | Pre-registration | Every live ranking rule has a backtest report and a prereg hash before activation |
| S6 | Cutover | Old `scout/` pipeline stopped and deleted after S2+S3 hold and 30 days of forward data exist |
| S7 | Cost | Data + model spend ≤ $100/month, measured from provider dashboards and the runs table |

Signal quality itself (do watchlist cohorts beat BTC?) is evaluated by S4 over time; this spec does not claim an edge.

## 2. Non-goals (anti-scope)

- No paper or live trading engine, no exits, no sizing, no exchange or DEX execution. `scout/trading/`, `scout/live/`, Minara, 0x, Kraken, Solana lanes are retired. Live trading is a separate future decision gated on S4 evidence.
- No ML ranking until ≥ 300 watchlist entrants have 90-day outcomes.
- No dashboard web app in v1 (Telegram digest + on-demand Q&A). `live-dashboard` skill may be added in v1.1.
- No LLM in any data write path: collectors, features, ranking and ledger are deterministic code. The agent reads the store and writes only `research_notes`.
- No reviewer-clearance files, receipt validators or watchdog fleet in `gecko/`.
- No new chains beyond solana, base, ethereum, bsc in v1. Young-chain launch capture (rh_pons) is not carried; a `young_chain` flag from a configured chain-launch-date list replaces it.

## 3. Architecture

```
            systemd timers as root (template unit gecko-job@.service, one timer per job)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ collectors: dexscreener · geckoterminal · coingecko · benchmark · enrich ·    │
 │ (gmgn, x_kol later) · rank (hourly) · ledger (daily) · freshness (30 min) ·   │
 │ prune (daily) · research (daily, Anthropic API) · digest (daily) ·            │
 │ scorecard (weekly) · check (self-test, on demand)                             │
 └───────────────┬──────────────────────────────────────────────────────────────┘
                 ▼  all writes (research writes ONLY research_notes)
            gecko.db (SQLite): tokens · observations · source_calls · watchlist · forward_returns · research_notes · runs
                 ▲                                   │ nightly snapshot copy
 systemd: gecko-telegram-reader (Telethon, long-running) ──► source_calls
                                                     ▼
 systemd: hermes-gateway (user gecko-agent) ── gecko-query skill (read-only SQL over the snapshot) ── Telegram Q&A
```

Scheduling (amended 2026-10-16, replaces "Hermes cron"): every script job runs as `python -m gecko <job>` from one systemd template unit (`gecko-job@.service`, `WorkingDirectory=/root/gecko-alpha`, one shared env file) with one timer per job, as root, because the jobs own `/root/gecko-alpha/gecko.db` and the Hermes install on the box (v0.14.0) runs as `gecko-agent`, which cannot read `/root`. A timer never starts a unit that is still running, so a job never overlaps itself; `OnFailure=` pages job crashes through the existing `codex-systemd-failure-alert@` template. Each job writes a `runs` row and exits non-zero on failure. The `telegram-reader` long-running unit is unchanged.

Hermes primitives used (amended 2026-10-16): gateway (Telegram, allow-listed) and one local read-only skill, `gecko-query`, over a nightly snapshot copy of `gecko.db` readable by `gecko-agent`; `MEMORY.md` (operator preferences only). `cron`, `mcp_servers`, `delegate_task` and the community skills are not used in v1; revisit after a Hermes upgrade (owner decision).

## 4. Store (`gecko.db`)

SQLite, WAL mode, single writer per table per process. Keyed on canonical identity `(chain, address)` where the address is lowercased ONLY for EVM chains (base, ethereum, bsc) and kept case-exact for Solana (base58 is case-sensitive) — implemented once in `gecko/identity.py` (amended 2026-10-09 from "lowercase always"). CoinGecko ids are stored separately (the old store put CG slugs in `contract_address`; this is not repeated). Every stored timestamp is canonicalized to UTC `%Y-%m-%dT%H:%M:%SZ` by the store so lexical comparisons are chronological.

| Table | Key columns | Purpose |
|---|---|---|
| `tokens` | `token_id` PK (= `chain:lowercase_address`), `chain`, `address`, `symbol`, `name`, `cg_id` NULL, `first_seen_at`, `first_source`, `pool_created_at` NULL, `deployer` NULL, `status` (`active`/`disqualified`/`dead`), `disqualify_reason` | identity + lifecycle |
| `observations` | `id`, `token_id`, `source`, `observed_at`, `features_json` | point-in-time raw features per collector run (price, mcap, liquidity, vol_24h, buys/sells, holders, top10_pct, security flags, smart_money_count, trenches rank, …). Append-only; pruned only by the retention rule below. |
| `source_calls` | `id`, `token_id` NULL, `source_type` (`telegram`/`x`), `source_id` (channel/handle), `event_id` UNIQUE, `posted_at`, `text_hash`, `cashtags_json`, `addresses_json`, `resolved_at` NULL | social calls; identical semantics to today's `source_calls` so caller history can be migrated |
| `watchlist` | `token_id` PK, `entered_at`, `rule_id`, `rank`, `score`, `features_at_entry_json`, `last_ranked_at`, `exited_at` NULL, `exit_reason` NULL | ranked state; entry row is immutable (features frozen at entry for the ledger) |
| `forward_returns` | `token_id`, `horizon_days` (1/7/30/90/180), `due_at`, `entry_price`, `price`, `btc_entry`, `btc_price`, `ret`, `ret_vs_btc`, `measured_at` NULL, PK(`token_id`,`horizon_days`) | the track record |
| `research_notes` | `id`, `token_id`, `created_at`, `model`, `schema_version`, `note_json`, `cost_usd` | agent dossiers (only table the agent writes) |
| `runs` | `id`, `job`, `started_at`, `finished_at`, `ok`, `rows_written`, `credits_used`, `error` | freshness + cost accounting (§12a) |

Retention: `observations` 180 days (configurable, floor 30) except `benchmark:btc`, which is never pruned (the 180-day horizon reads BTC at `entered_at`); `runs` 90 days, keeping the earliest ok run per job; everything else permanent (amended 2026-10-14). Historical `scout.db` is copied to `/root/gecko-history/scout.db` read-only before any cutover and is the backtest substrate.

## 5. Collectors

All collectors: aiohttp, explicit timeouts, exponential backoff on 429/5xx, write a `runs` row, exit 1 on failure. Universe filters live in one config object (`GeckoSettings`, pydantic-settings, ~15 fields).

| Source | Endpoint(s) | Cadence | Budget / limits | Role | Universe filter |
|---|---|---|---|---|---|
| DexScreener | `token-boosts/latest`, `token-profiles/latest`, `tokens/v1/{chain}/{addrs}` | 10 min | free; 60 rpm boosts/profiles, 300 rpm pairs | discovery + price/liq/vol/buys-sells | chains ∈ {solana,base,ethereum,bsc}; liquidity ≥ $250K; pair age ≥ 24 h |
| GeckoTerminal | `networks/{net}/new_pools`, `networks/{net}/trending_pools`, pool OHLCV daily | 15 min; OHLCV daily | keyless, backoff | discovery + `pool_created_at` + DEX 7d/prior-7d volume (F4 input) | same as above |
| CoinGecko (REST, Demo key; MCP for the agent) | `coins/markets` (mcap desc, pages covering $200M→$500K), `coins/{id}` platforms (once per new coin), `search/trending`, `simple/price` (BTC benchmark hourly). `coins/list/new` is paid-only and NOT used; new listings come from GeckoTerminal/DexScreener | markets daily; trending + BTC hourly | Demo 10K credits/mo; plan ≈ 2.7K/mo; hard cap 8K enforced in `runs` | listed universe + benchmark | mcap $500K–$200M |
| GoPlus (REST, keyless) | `token_security/{1,56,8453}`, `solana/token_security`, ONE address per call (free tier silently truncates batches) | hourly job; first sight for candidates, daily for active/watchlist, daily retry while GoPlus does not know the token | free 150 CU/min, 30K CU/day; `gp_daily_cap` enforced from `runs` | `security_ok`, holders, `top10_pct`, deployer | all |
| GMGN (SKILL.md, demo key) | `market trenches` (new_creation/near_completion), `gmgn-contract-dd`, `gmgn-track` smart money | 30 min trenches; DD on first sight | demo read-only; paid only after backtest shows value | smart-money wallet count, DD score | solana/base/bsc/eth |
| Telegram callers | Telethon user session over configured channels (migrated from `tg_social_channels`) | long-running | Telegram limits | `source_calls` with cashtags/addresses; caller quality from history | all |
| X / KOL | existing Hermes skills (`kol_watcher` → `narrative_classifier` → `coin_resolver`) re-pointed to write `source_calls` via `python -m gecko.ingest_call` | hourly, agent mode | X API tier (owner) | narrative mentions | all |

Token admission into `tokens` requires: in universe band, security result not failed, liquidity ≥ $250K for ≥ 24 h CONTIGUOUS (the qualifying anchor observation must be later than the most recent below-floor observation; only observations carrying `liquidity_usd` count) — the survivor filter; most rugs manifest within 1 h per the research.

## 6. Features (point-in-time, computed hourly from `observations`)

Each feature uses only observations with `observed_at ≤ as_of`. Definitions:

| Feature | Definition | Source |
|---|---|---|
| `age_days` | `as_of − pool_created_at` (earliest pool seen; None when no pool is known — the registered F1 excludes coins without a resolvable pool, amended 2026-10-09) | GT / DexScreener |
| `mcap_usd`, `liquidity_usd`, `vol_24h_usd` | latest | DexScreener / CG |
| `vol_7d_ratio` | sum(vol for t−7d ≤ ts < t) / sum(vol for t−14d ≤ ts < t−7d) over the deepest pool's daily candles; NULL if the prior window has zero volume or the last window has no candle. This is EXACTLY the registered F4 definition (prereg f7d38fa2); any candle-count minimum requires a new prereg (amended 2026-10-09 — a 14-candle minimum was removed after the backtest showed it dropped 34 of the 101 F4 coins, the youngest pools averaging 2.47×) | GT OHLCV |
| `holders`, `holder_growth_7d` | latest count; (now − 7 d ago)/7 d ago | GoPlus (amended 2026-10-14) |
| `top10_pct` | sum of the `percent` of GoPlus's top-10 holders, ×100, EXCLUDING exactly: locked balances (`is_locked=1`), burn addresses, pool addresses (`dex[].pair`/`dex[].id`), EVM contracts (`is_contract=1`) and known Solana AMM authorities. `lp_holders` is NOT excluded: it lists holders of the LP token (the dead address, lockers, the V3 position manager and often the deployer EOA), not pools — pools are already covered by `dex[]` and contracts by `is_contract`, so the only addresses it would add are EOAs that supplied liquidity, which is the rug pattern this measure exists to see. Holders beyond the reported 10 are not backfilled, so this is a lower bound (amended 2026-10-14; `lp_holders` exclusion removed 2026-10-15) | GoPlus |
| `bundle_flag` | GMGN DD score < 60 OR top10_pct > 40 | GMGN / holders |
| `smart_money_wallets` | distinct smart-money wallets holding (GMGN) | GMGN |
| `callers_7d`, `caller_quality` | distinct `source_id` calling token in 7 d; mean historical 30-d hit rate of those callers (from forward_returns joined to source_calls) | source_calls |
| `narrative_mentions_7d` | X items referencing token in 7 d | X skills |
| `security_ok` | False if any: EVM honeypot / cannot_sell_all / blacklist / buy or sell tax > 10% / mintable or proxy with a non-renounced owner; Solana mintable / freezable / balance-mutable authority / non-transferable / transfer hook (a `trusted_token` overrides to True). True only when the core flags are present and clean — core flags: EVM honeypot / cannot_sell_all / blacklist (taxes optional; `owner_address` required when mintable or proxy), Solana mintable / freezable / balance-mutable authority / non-transferable; None otherwise (unknown is never safe; any failing flag still gives False) (amended 2026-10-14). The feature is the NEWEST enrich read taken as is (ties on `observed_at` go to the later write): a newer unknown read (an unknown marker or a partial body) gives None and never falls back to an older True (amended 2026-10-15). A tax-only failure (buy or sell tax > 10 % with every structural flag clean) gives False here but does NOT disqualify the token — see `security_structural` (amended 2026-10-16) | GoPlus |
| `security_structural` | True / False / None over the STRUCTURAL flags only (taxes excluded): EVM honeypot / cannot_sell_all / blacklist / mintable or proxy with a live (non-renounced) owner; Solana mintable / freezable / balance-mutable authority / non-transferable / transfer hook. False if any structural flag fails; True only when the core structural flags are present and clean; None otherwise (unknown is never safe). Same newest-read rule as `security_ok`. `enrich` re-reads tax-failed candidates every `gp_refresh_hours` so a launch tax that falls is seen (amended 2026-10-16, ruling R4) | GoPlus |
| `young_chain` | chain launch date (config) within 180 d | config |
| `momentum_ctx` | 7-d price change (context only, never a rule input in v1) | CG / DexScreener |

## 7. Ranking and pre-registration

**v1 live rule set** (the only rules active at launch):

- `R-F4` (pre-registered 2026-09-18, hash f7d38fa2): `age_days ≤ 90 AND vol_7d_ratio ≥ 2.0 AND security_ok AND NOT bundle_flag`. Plan 1 ruling: while no enrichment exists, `security_ok is None` is admitted (`require_security_known=False`); Plan 3 flips it to True at cutover. `security_ok is False` always rejects.
- Security semantics (ruling R4, amended 2026-10-16): a structural failure (`security_structural = false`) disqualifies permanently (`status = disqualified`). A TAX-only failure never sets `status = disqualified`: the token stays a candidate, `enrich` re-reads it every `gp_refresh_hours`, and it is admitted once both taxes are under 10 % (`security_ok` true). A watchlist member whose taxes rise above 10 % exits with reason `security_tax`; no re-entry in v1.
- Hard disqualifiers (applied before any rule): `security_structural = false` (amended 2026-10-16; was `security_ok = false`), `top10_pct > 60`, liquidity gate < $250K where the gate value is the MAX of each DEX source's latest priced reading within 6 h (so one shallow DexScreener pair cannot disqualify a token GeckoTerminal still sees as deep; when no DEX reading exists within 6 h the liquidity gate does not fire and only the dead gate decides absence — amended 2026-10-09), `status = dead` (no priced observation for 7 d). Candidates never admitted within 14 d of first sighting are demoted (`stale_candidate`) and leave the refresh and admission scans; an in-band rediscovery by any discovery collector revives them as fresh candidates (so aging only retires tokens that have left every feed).
- Score for ordering within the watchlist: `score = z(vol_7d_ratio) + z(holder_growth_7d) + 0.5·z(smart_money_wallets) + 0.5·z(callers_7d·caller_quality) − 1.0·bundle_flag`, z-scored over the current candidate set. Score orders; rules admit. Watchlist cap 50 applied to NEW entrants only (incumbents are never evicted by the cap; ties order by token_id); exits only when a disqualifier fires, taxes rise (`security_tax`, amended 2026-10-16), the ledger finds no entry price, or 180 d elapse; re-entry after an exit is unsupported in v1.

**Pre-registration protocol** for any new or changed rule: (1) write `docs/prereg/<rule>.md` with the rule, universe, horizon, benchmark and pass criteria; hash it; (2) run `python -m gecko.backtest --rule <rule>` against `/root/gecko-history/scout.db` + `/root/xs_study` charts; (3) rule activates only if the backtest meets its own criteria; (4) forward evaluation by the ledger; (5) rule retired if 90-d cohort underperforms BTC at n ≥ 30. Known F4 fragility (ex-top-3 0.75×, median 0.53×) is recorded in the prereg; F4 is a forward test, not a proven edge.

## 8. Agent jobs

Amended 2026-10-16 (rulings R1/R2): `research`, `digest` and `scorecard` are Python jobs on systemd timers (§3), not Hermes agent-mode cron jobs; Hermes serves Q&A only.

| Job | Schedule | Behaviour | Guardrails |
|---|---|---|---|
| `gecko-research` = Python job `research` | daily 06:00 UTC | For each watchlist entrant since last run (max `research_max_per_run`, default 10): one Anthropic Messages API call per token (aiohttp, model `claude-sonnet-5-5`, server-side `web_search` tool with `max_uses` 5), asked for a JSON object with exactly this schema: {`what_it_is`, `team_or_deployer_history`, `narrative_fit`, `red_flags[]`, `verdict` ∈ {avoid, watch, strong}, `confidence`, `sources[]`}; the job validates the model's JSON against that fixed schema, truncates strings, drops unknown keys and writes one `research_notes` row. Enabled only when `anthropic_api_key` is set. | daily USD cap `research_daily_usd_cap` (default 1.5) and per-run cap `research_max_per_run`, cost recorded in `research_notes.cost_usd` and `runs.credits_used`; the module writes ONLY `research_notes`; model output is untrusted data; verdicts never change rank (display only; ranking never reads `research_notes`) |
| `gecko-digest` = Python job `digest` | daily 07:00 UTC | Deterministic: watchlist diff since the last OK `digest` run's `started_at` (never wall-clock midnight) + latest research verdicts; posts entrants, exits, top-N with one-line reasons via the Bot API (`gecko.notify.send_telegram`). Silent day when nothing changed. Plain text, no `parse_mode` (underscores in symbols), ≤ 4,000 chars per message (longer digests split into numbered parts). | read-only on the store |
| `gecko-scorecard` = Python job `scorecard` | weekly Monday 08:00 UTC | Deterministic: per-rule cohort stats at each horizon vs BTC: n, mean, median, % ≥ 2×, % ≥ 7×, hit rate, with expired and unmeasurable counts reported beside n; marks `INSUFFICIENT_DATA` under n = `scorecard_min_n` (default 20). Sent via the Bot API, plain text. | read-only on the store |
| `gecko-xkol` | hourly | Existing kol_watcher → classifier → resolver chain, agent mode so failures create incidents. | optional until X credentials re-paired |
| Q&A | on message | "why is X ranked 3", "show base entrants this week" — the Hermes gateway agent queries a nightly snapshot copy of `gecko.db` (readable by `gecko-agent`) through the `gecko-query` skill. SQL allow-list: SELECT only; tables `tokens, observations, watchlist, forward_returns, research_notes, runs, source_calls`; LIMIT forced ≤ 200. | `TELEGRAM_ALLOWED_USERS` = operator only; never touches the live `gecko.db` |

## 9. Forward-return ledger

`python -m gecko.ledger` daily (live-only; `--as-of` is refused): for each `watchlist` row, schedule horizons at entry; when `due_at` passes, fetch the price from DexScreener `tokens/v1` (deepest pair; the same source for listed and DEX-born tokens, amended 2026-10-09) and BTC from the hourly `benchmark:btc` observations; write `ret`, `ret_vs_btc`. Missing price (404, empty, non-JSON body, or a definite 4xx other than 403) after 3 attempts over 3 daily runs ⇒ `ret = −1.0` (treated as total loss, flagged `dead`). Transient upstream failures (connection errors, 403, 429, 5xx) and a missing BTC benchmark observation skip the row without spending an attempt; BTC is looked up within ±6 h of the timestamp, then ±36 h as a logged fallback. Entry price = the first priced market observation at or after entry, resolved ONCE per watchlist entry and stored in `watchlist.entry_price` so every horizon shares it; if none exists within 48 h of entry the whole entry is finalized as unmeasurable (`ret` NULL) and exits with reason `no_entry_price` (the token itself is not marked dead). The CoinGecko price is a fallback for the entry price only after the 48 h window has closed with no priced DEX observation, so an in-window DEX row is never pre-empted. ANY row still unmeasured `ledger_max_overdue_days` (default 14, floor 7 so the 72 h backlog page fires first) past `due_at`, whatever the cause (a permanent 403, a BTC benchmark gap at entry, or a ledger outage longer than the window), is closed unmeasured with no `ret` and logged `ledger_row_expired`; this is irreversible (amended 2026-10-15). Every row closed with `ret` NULL carries a durable `forward_returns.closed_reason` ∈ {`expired`, `no_entry_price`} so the scorecard can report those cohorts beside n (amended 2026-10-16). The `benchmark:btc` observations may be written by the keyless `benchmark` job (GeckoTerminal WBTC price, hourly) or by `coingecko-hourly` when a Demo key exists; the ledger reads either (amended 2026-10-16, ruling R5). The DexScreener collector refreshes every candidate, active and watchlisted token each run (unconditionally, below-floor readings included) so this observation exists for live tokens. This ledger is the sole input to the scorecard and to any future live-trading gate.

## 10. Backtest harness

`python -m gecko.backtest --rule <id> --from 2026-04-09 --to <date>`: rebuilds point-in-time features from historical `scout.db` tables (`gainers_snapshots`, `trending_snapshots`, `dex_pool_discoveries`, `volume_spikes`, `source_calls`, `tg_social_signals`) and `/root/xs_study` CG charts; applies the rule as of each day; reports cohort returns vs BTC at each horizon with bootstrap 90% CI, ex-top-3 mean, median, halving rate — the same battery as the 09-18 study. Excludes the 54 re-denominated coins by the stored-price-ratio check.

## 11. Hermes configuration (srilu-vps, user `gecko-agent`)

Amended 2026-10-16 (ruling R1): Hermes = gateway + the `gecko-query` read-only skill over a nightly snapshot. It schedules nothing and writes nothing.

- Hermes upgrade to **v0.21.6 pinned** (never `latest`; back up `~/.hermes` first) is deferred to an owner decision and is NOT a prerequisite for the soak (amended 2026-10-16).
- After that upgrade only (agent-side, not used in v1): `config.yaml` `mcp_servers: {coingecko: {url: https://mcp.api.coingecko.com/mcp, headers: {x-cg-demo-api-key: $COINGECKO_DEMO_API_KEY}}, helius: {command: npx, args: [-y, helius-mcp], env: {HELIUS_API_KEY}}, blockscout: {url: https://mcp.blockscout.com/mcp, headers: {...}}}`; `tools.exclude` any swap/order/send tool names from GMGN; `delegation.model` cheap. GoPlus is called keyless from the `enrich` job, so no `goplus` MCP server is configured (amended 2026-10-16).
- Skills: v1 installs ONE local skill, `gecko-query` (read-only SQL allow-list, §8), over `/var/lib/gecko/gecko-snapshot.db`, refreshed nightly from `gecko.db` and readable by `gecko-agent`. Deferred until after the upgrade: `official/blockchain/solana` (existing guarded wrapper retained), `coingecko/skills`, `GMGNAI/gmgn-skills` (read subset only: gmgn-token, gmgn-contract-dd, gmgn-track, market-trenches, gmgn-wallet-score), agiprolabs `token-holder-analysis`, `sybil-detection`, `pumpfun-mechanics`. Every community install goes through `hermes skills audit`; a `dangerous` verdict is a hard no regardless of source (inventory §2 policy).
- Gateway: Telegram enabled, `TELEGRAM_ALLOWED_USERS` = operator, `TELEGRAM_HOME_CHANNEL` = digest chat; health chat separate.
- Scheduling (amended 2026-10-16, replaces the Hermes cron jobs.json list): systemd timers as root over the template unit `gecko-job@.service` (§3), one timer per script job: `dexscreener` 10 m, `geckoterminal` 15 m, `geckoterminal-ohlcv` daily (staggered off the pool-job slots), `coingecko-markets` daily, `coingecko-hourly` hourly (staggered from markets), `benchmark` hourly, `enrich` hourly off :00, `rank` hourly, `ledger` daily, `freshness` 30 m, `prune` daily off-peak after the backup, `research` daily, `digest` daily, `scorecard` weekly. `gmgn-trenches` and `xkol` are not scheduled in v1. `telegram-reader` is a long-running unit, not a timer job (unchanged; amended 2026-10-15).
- Memory: `MEMORY.md` holds operator preferences (chains of interest, digest style); no pipeline state.

## 12. Operations and health

- Long-running units: `hermes-gateway.service`, `gecko-telegram-reader.service`. All other old units/timers/crontab lines are removed at cutover.
- `freshness` job (script): per-table SLO (`observations` ≤ 30 min, `source_calls` run-anchored on `telegram-reader` ok runs ≤ 30 min (channels may be legitimately silent for days; a reader that cannot poll writes `ok=0` runs and pages — amended 2026-10-14), `observations:enrich` ≤ 26 h (amended 2026-10-14), `watchlist` ≤ 2 h, `forward_returns` ≤ 26 h, `research_notes` ≤ 26 h only when `anthropic_api_key` is set (amended 2026-10-16), `prune` run-anchored on ok `prune` runs ≤ 26 h (amended 2026-10-16), `source_calls:rows` row-anchored ≤ 72 h, active only when channels are configured and the reader has at least one ok run, so a parse regression that stores nothing under ok runs pages (amended 2026-10-16)); breach ⇒ one plain-text Telegram message to the health chat, de-duplicated per table per 6 h. This is the §12a minimum.
- Job crashes page through systemd `OnFailure=` on the template unit and the reader unit (amended 2026-10-16; was Hermes cron incidents); the freshness job covers "ran but wrote nothing": per-source observation SLOs on written rows (dexscreener 30 min, geckoterminal 360 min, coingecko 26 h, OHLCV-with-candles 26 h), a `benchmark:btc` SLO of 3 h, and a forward-returns backlog breach when any row is more than 72 h past due and unmeasured; the backlog text carries the overdue count and the expired count, so a new stall or a new expiry changes the page (amended 2026-10-16). The ledger prices a per-chain canary (WSOL, WETH, WBNB) each run; an unpriced canary marks every missing price on that chain transient and fails the run, so a DexScreener schema or route change cannot be booked as per-token losses (amended 2026-10-09).
- Backup: nightly `sqlite3 gecko.db ".backup"` + existing off-host transport script retained (only ops script carried over).
- Logs: structlog JSON to journald as today.

## 13. Security

- Read-only everything: no execution tools installed; GMGN execution skills excluded; solana/evm skills remain under the existing read-only rulings.
- Keys in `~/.hermes/.env` and `/root/gecko-alpha/.env.gecko` only (NEVER in the legacy `/root/gecko-alpha/.env`: the frozen `scout` settings use `extra="forbid"` and would crash-loop on any `GECKO_*` line — amended 2026-10-09); never in argv, prompts, PRs or chat. Transport-error logs record the exception type and status only, never its repr (aiohttp embeds request headers). Telethon `*.session` mode 0600, outside the repo, never committed.
- LLM output is untrusted: only the `research` job writes model output, only to `research_notes`, after validating it against the fixed JSON schema (amended 2026-10-16; was the agent via `python -m gecko.note`); ranking never reads `research_notes`. The Hermes agent reads a snapshot copy only and writes nothing.
- Prompt-injection: digest/research prompts treat all token names, descriptions and social text as data; `parse_mode=None` on every Telegram send.

## 14. Migration and cutover

1. **Preserve**: copy `scout.db` to `/root/gecko-history/scout.db` (read-only, 0444) and verify `PRAGMA quick_check`; keep `/root/xs_study`.
2. **Freeze `scout/`**: no further PRs except cutover; prod stays on its current side branch until retired (it is harmless; CG is already dark).
3. **Build `gecko/`** on branch `feat/gecko-lean-agent` with CI; deploy to the VPS alongside the old pipeline (different DB file, different units).
4. **Telethon session**: gecko uses its OWN Telethon session created by `python -m gecko tg-login` (a second device on the same account), so there is no session handover; the legacy listener is stopped at retirement (step 7) like every other unit (amended 2026-10-14). Migrate channel list and `source_calls` history into `gecko.db`.
5. **Hermes** (rewritten 2026-10-16): no Hermes upgrade is required for the soak. Install the nightly `gecko.db` snapshot (readable by `gecko-agent`) and the read-only `gecko-query` skill on the existing gateway; scheduling is systemd timers (§3, §11). The upgrade, MCP servers, community skills and the X scanner's conversion to agent mode are deferred to an owner decision.
6. **Soak**: S2 + S3 for 14 days; ledger accumulating.
7. **Retire**: after 30 days of forward data, stop and disable `gecko-pipeline`, `gecko-dashboard`, `trade-alert-watcher`, all legacy `gecko-*` timers (NOT the `gecko-job@` timers or `gecko-telegram-reader`, which are this design's units — amended 2026-10-16), the crontab lines and watchdog units; one PR deletes `scout/`, `dashboard/`, `scripts/` (except backup transport), `systemd/`, `cron/`, `.reviewers/`, the receipt tooling and their tests; CI reduced to `pytest tests/gecko`.
8. **Owner actions required** (cannot be automated): create a free CoinGecko Demo API key; GMGN demo key; optionally a Helius key (agent MCP only); run `python -m gecko tg-login` once (amended 2026-10-14: GoPlus is keyless, Blockscout dropped); decide on X API re-pairing; prune the ~180 stale local git worktrees.

## 15. Testing

- TDD per module; `aioresponses` for every HTTP source; `tmp_path` SQLite.
- Point-in-time correctness: a test that inserts a later observation and asserts a feature computed `as_of` earlier is unchanged (no lookahead).
- Ledger: entry price is the first post-entry observation; missing-price path yields `ret = −1.0`.
- Ranking: every disqualifier has a test asserting WHICH disqualifier fired; the rule admits a known F4 fixture and rejects age 91 d / ratio 1.9.
- Freshness: a table with no writes past SLO produces exactly one alert; a second run within 6 h produces none.
- Collectors: a 429 then 200 sequence succeeds with backoff; a persistent failure exits 1 and writes `runs.ok = 0`.
- Backtest: reproduces the 09-18 F4 cohort statistics within tolerance on the frozen study data (regression anchor).
- Target: `tests/gecko` ≤ 9,500 LOC (see S1; amended 2026-10-16).

## 16. Budget

| Item | Monthly |
|---|---|
| CoinGecko Demo | $0 (≈ 2.7K of 10K credits) |
| Helius free (agent MCP only, optional) | $0 |
| GoPlus (keyless), DexScreener, GeckoTerminal | $0 |
| GMGN | $0 demo; paid tier only after backtest evidence (price unverified) |
| Research model (OpenRouter cheap model, ≤ 20 dossiers/day) | ≤ $30 |
| Digest/scorecard/Q&A model | ≤ $10 |
| Reserve (GMGN paid or X API if justified) | ≤ $50 |
| **Total** | **≤ $100** |

## 17. Risks and open items

| Risk | Mitigation |
|---|---|
| F4 is fragile; watchlist may underperform BTC | ledger + scorecard make this visible at n ≥ 20; rule retirement protocol |
| Free tiers change or throttle | per-source `runs.credits_used`; hard caps; sources are independent so one outage degrades, not kills |
| GMGN demo key may be insufficient for trenches | smart-money feature is NULL-tolerant; rule set does not depend on it |
| X credentials remain unpaired | X lane optional; callers via Telegram remain |
| Telethon session collision | separate gecko session via tg-login; no shared session file (step 14.4, amended 2026-10-14) |
| Hermes patch regressions | pinned version; `~/.hermes` backup before upgrade |
| Historical backtest substrate has gaps (CG dark since 09-19) | backtest window ends 2026-09-18; forward window starts at launch |

## 18. Deliverables order (for the implementation plan)

1. Store + settings + freshness + runs (foundation). 2. DexScreener + GeckoTerminal collectors. 3. CoinGecko Demo collector + ledger. 4. Features + rank + F4 + tests. 5. Backtest harness + F4 regression anchor. 6. GoPlus enrichment. 7. Telethon reader on gecko's own session (amended 2026-10-14). 8. systemd timers, digest/scorecard/research jobs, gecko.db snapshot + gecko-query skill (amended 2026-10-16; the Hermes upgrade, MCP and community skills are deferred). 9. Soak + cutover PR.
