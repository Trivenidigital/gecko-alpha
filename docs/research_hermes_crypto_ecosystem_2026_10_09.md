# Hermes Agent crypto-ecosystem research — 2026-10-09

Source: web-verified research pass run 2026-10-09 (subagent). Items marked UNVERIFIED could not be confirmed on a primary page.

## 1. Hermes Agent core (v0.21.6, released 2026-10-08)

- Repo: https://github.com/NousResearch/hermes-agent (MIT). Docs: https://hermes-agent.nousresearch.com/docs
- Patch cadence ~1-2 weeks. v0.21.0 (08-31) introduced state.db fragility fixed in 0.21.2 — pin a patch version, never `latest`.
- Install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`, then `hermes setup --portal`.
- Architecture: single `AIAgent` core; gateway (25+ platform adapters, session routing, cron ticking); ACP server; batch runner; API server; Python lib. 70+ tools / ~28 toolsets; 7 terminal backends. Plugins from `~/.hermes/plugins/`.

| Primitive | Status | Detail |
|---|---|---|
| Cron / scheduler | YES | `hermes cron create/list/pause/resume/run/remove/status/runs/incidents/doctor/tick`; jobs in `~/.hermes/cron/jobs.json`; gateway ticks every 60s; each run is a FRESH session (opt-in `context_from`/`continuity`); delivery `telegram:<chat_id>` / `discord:#chan`; `[SILENT]` suppresses; retries 5/15/30 min; `cron.max_parallel_jobs`. https://hermes-agent.nousresearch.com/docs/user-guide/features/cron |
| Persistent memory | YES, small | `MEMORY.md` (2,200 chars) + `USER.md` (1,375 chars); `memory` tool; sessions in SQLite `state.db` with FTS5. NOT a datastore — the pipeline keeps its own DB. |
| Skills hub | YES | SKILL.md + YAML frontmatter (agentskills.io-compatible); `hermes skills browse/search/inspect/install/list/check/update/uninstall/audit/tap add <owner/repo>`; official optional skills as `official/<category>/<skill>`; community installs scanned, `--force` cannot override `dangerous`. |
| MCP client | YES | `config.yaml → mcp_servers: {name: {command,args,env}}` or `{url, headers}`; tools exposed as `mcp_<server>_<tool>`; `tools.include/exclude`; `hermes mcp add`, `hermes mcp test`. |
| Telegram gateway | YES (Bot API only) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL` for cron delivery. NO user-account (Telethon) channel reading. |
| Delegation | YES | `delegate_task` (parallel `tasks`, default 10 concurrent, depth 1, `delegation.model` for cheap children, `output_schema` validation). |
| Other | `execute_code`, browser automation, A2A v1.0, Bot Mode (v0.21), Discord. |

## 2. Skills hub — crypto coverage

Official optional catalog (156 skills): https://hermes-agent.nousresearch.com/docs/reference/optional-skills-catalog

| Skill | Install id | Capability | Relevance |
|---|---|---|---|
| solana v0.2.0 | `official/blockchain/solana` | token metadata/price/mcap/authorities/top-5 holders, whales, wallet, tx. Public RPC + CG free. | per-token lookup only; no discovery |
| evm v1.0.0 | `official/blockchain/evm` | 8 chains: wallet/token/tx/decode/gas/allowance/contract/ens/whale | no holder/new-token discovery |
| hyperliquid | `official/blockchain/hyperliquid` | perps | not relevant |
| **watchers** | `official/devops/watchers` | `watch_http_json.py` (any JSON list endpoint, `--id-field`, `--items-path`), `watch_rss.py`, `watch_github.py`; watermark state in `$HERMES_HOME/watcher-state/`; pairs with cron. | HIGH — zero-code "poll + emit only new items" over DexScreener / CG new-pools |
| live-dashboard | `official/productivity/live-dashboard` | cron-refreshed `dashboard.json` + static `index.html` | could replace custom watchlist dashboard |
| research/* | duckduckgo-search, rss-feeds, blogwatcher, scrapling, reddit-reading | news / narrative inputs | medium |
| built-ins | xurl (X CLI, needs X API tier), product-price-monitor, competitor-news-monitor | | |

No official skill for: DexScreener, Birdeye, GoPlus, pump.fun, smart-money, holder growth, backtesting, Telegram channel monitoring.

Community SKILL.md collections:

| Skill | URL | Capability | Maintained |
|---|---|---|---|
| **GMGN skills** | https://github.com/GMGNAI/gmgn-skills (vendor, MIT, 607★) | `gmgn-token`, `gmgn-contract-dd` (0-100 DD score), `gmgn-track` (smart money / KOL trades), `gmgn-wallet-score`, `market trenches` (new_creation / near_completion by launchpad: pump.fun, letsbonk, fourmeme, clanker; dev holdings; KOL entry). sol/bsc/base/eth. Also swap/TP-SL execution (exclude). Demo key read-only; paid tier UNVERIFIED. | yes, active |
| agiprolabs/claude-trading-skills | https://github.com/agiprolabs/claude-trading-skills (MIT, 410★, 20 commits) | 68 skills: birdeye-api, dexscreener-api, solanatracker-api (graduating tokens), coingecko-api, helius-api, defillama-api, pumpfun-mechanics, token-holder-analysis, whale-tracking, wallet-profiling, sybil-detection (bundlers), liquidity-analysis. `--demo` mode. | reference prompts, thin history |
| coingecko/skills | https://github.com/coingecko/skills (vendor) | CG API knowledge incl. onchain: trending pools, new pools, token holders/traders, OHLCV, trades | yes |
| sendaifun/skills | https://github.com/sendaifun/skills (Apache-2.0, 130★) | Solana marketplace: birdeye, coingecko, smart-money, wallet-analysis, helius, jupiter, pumpfun, pyth | active |
| jagres0039/hermes-market-skills | https://github.com/jagres0039/hermes-market-skills (9★) | CG + CCXT + DeFiLlama + CryptoPanic, backtest, optional Telethon ingestion | thin; Telethon-in-Hermes pattern only |
| true402/hermes-skills | https://github.com/true402/hermes-skills | Base-only honeypot simulation | unproven |
| Xquik-dev/hermes-tweet | https://github.com/Xquik-dev/hermes-tweet (37★) | 102 X endpoints via Xquik proxy, prepaid | third-party proxy risk |
| kryptoxerk-dot/kaiba-ultimate-agent-public | https://github.com/kryptoxerk-dot/kaiba-ultimate-agent-public | only end-to-end Hermes-operated memecoin pipeline found: deterministic ingest/scan/score (pump.fun, GMGN smart-money/trenches, Helius, DexScreener), wallet grading + entity clustering, `sm-trenches` lane (≥3 independent smart-money wallets), Hermes cron + MCP + Telegram. Paper-only; author reports −14 to −18% avg/trade over ~190 trades. | architecture reference only |

## 3. awesome-hermes-agent lists

- https://github.com/0xarkstar/awesome-hermes-agent — crypto thin (chainlink skills, cost-control plugin).
- https://github.com/ZeroPointRepo/awesome-hermes-skills — true402 token-safety (beta), hermes-blockchain-oracle (experimental), mercury on-chain forensics https://github.com/hxsteric/mercury (beta), AgentCash x402 APIs, Scavio web data. No Telegram-monitoring entry.
- https://github.com/0xNyk/awesome-hermes-agent — FMP stock+crypto, x-twitter-scraper (Xquik), scraping tools.
- Verdict: no maintained on-chain discovery source in the Hermes-specific lists; coverage comes from vendor MCP servers and the GMGN / agiprolabs / SendAI collections.

## 4. MCP servers (Hermes-loadable)

| Server | Maintainer / URL | Health | Data | Free tier | Auth |
|---|---|---|---|---|---|
| CoinGecko MCP (remote) | official https://docs.coingecko.com/ai-integration/mcp-server | active | `search.trending`, `onchain.networks.trendingPools/newPools`, `onchain.networks.tokens.topHolders/holdersChart`, pool/token trades, `coins.list.getNew` | keyless `https://mcp.api.coingecko.com/mcp` shared limits; Demo key 10k credits/mo (rpm UNVERIFIED) | none / demo key |
| DexScreener MCP | https://github.com/opensvm/dexscreener-mcp-server (24★) | low activity | latest profiles, latest/top boosts, pairs, search; built-in limiter | free, 60 rpm profiles/boosts, 300 rpm pairs/search | none |
| Helius MCP | official npm `helius-mcp`, https://www.helius.dev/docs/agents/mcp | active | DAS token holders, parsed tx history, webhooks/streams | 1M credits/mo, 10 rps RPC, 2 rps DAS; Enhanced WS paid ($49/mo) | `HELIUS_API_KEY` (already in gecko .env) |
| Blockscout MCP | https://github.com/blockscout/mcp-server; hosted `https://mcp.blockscout.com/mcp` | active | 16 tools: address/token/transfers/tx/contract ABI/read, `lookup_token_by_symbol` | free PRO key, credit-metered | header key |
| Etherscan MCP | hosted `mcp.etherscan.io/mcp` | active | 60+ chains explorer | free key tier (limits UNVERIFIED) | bearer |
| Dune MCP | `https://api.dune.com/mcp/v1` | active | SQL over all chains incl. Solana; 31 tools | free credits (amount UNVERIFIED) | OAuth / `x-dune-api-key` |
| GoPlus MCP | https://github.com/GoPlusSecurity/goplus-mcp (7★, 2 commits) | stale wrapper, stable API | token_security (39 EVM), solana_token_security, malicious_address, approval_security | key+secret by request | `--key --secret` |
| Nansen MCP | `https://mcp.nansen.ai/ra/mcp` | active | token screener w/ smart-money filter, flows | 1,000 one-time credits → effectively $49-69/mo | header key |
| Alchemy MCP | hosted `https://mcp.alchemy.com/mcp` | active | prices/balances/transfers | CU-based free | OAuth |
| Moralis Cortex | `@moralisweb3/api-mcp-server` | discovery/trending endpoints REMOVED Jun-Jul 2026 | balances, PnL | paid | key |
| Messari MCP | `https://mcp.messari.io/mcp` | active | mindshare/sentiment/news/unlocks | paid (free UNVERIFIED) | OAuth |
| Santiment | kukapay/crypto-sentiment-mcp (stale) | stale | social volume/dominance | limited free | key |
| Birdeye MCP | existence UNVERIFIED (docs 404) | ? | | $0 tier: 1 rps, 3 endpoints | key |
| X/Twitter | no free maintained server; xurl built-in needs X API tier; Xquik/twitr paid proxies | | | | |

## 5. Early-winner detection techniques (2025-2026)

1. Bundle / entity-cluster before holder counts — MELT (arXiv 2602.13480, 41k Solana launches): ~36.5% of supply on average held by coordinated accounts hiding behind "independent" holders; raw holder growth is manipulable.
2. First-5-minute liquidity + social features predict rugs — "Catching the Rug" (arXiv 2608.20271, 6.4M tokens, XGBoost); most rugs show within 1 hour.
3. Smart-money cohort entry as trigger — GMGN trenches (smart-degen wallet count, age, liquidity), kaiba `sm-trenches` (≥3 independent proven wallets); Nansen screener paid equivalent. kaiba's own result is negative EV → necessary not sufficient.
4. Deployer reputation + funding-chain tracing (same-block sniper clusters, who funded the deployer) beats top-10 holder %. Bubblemaps: >25% in one connected cluster = investigate.
5. Liquidity-lock verification at the locker contract; buy/sell simulation honeypot checks (GoPlus); mint/freeze authority checks.
6. Launchpad-native streams: pump.fun `logsSubscribe` (Helius), Raydium migration decoding (Chainstack pump-fun-bot), SolanaTracker graduating tokens, Base via clanker (GMGN covers).
7. Social velocity: posts per 5 min, author quality, unique-author growth; free X access is the bottleneck.
8. Narrative/mindshare: Messari Signal, Santiment, CryptoPanic velocity; LLM-judged narrative fit (TradingAgents-crypto, CryptoTrade) has no published out-of-sample alpha.
9. Agent frameworks: "Paper Agents, Paper Gains" (arXiv 2605.29174) — most crypto agents show no autonomous execution, negative median holder returns. arXiv 2603.10092: LLM output must be treated as untrusted intent gated by a non-bypassable executor. Hermes is positioned as OpenClaw's successor.
10. Design consensus: deterministic ingest/score/execute services with the LLM as operator/researcher; point-in-time feature logging per signal; paper mode by default.

## Recommended install set (ranked)

| # | Item | Type | Relevance | Maintenance | Free tier | Note |
|---|---|---|---|---|---|---|
| 1 | GMGN skills | SKILL.md | very high (trenches, smart-money/KOL entry, DD score, wallet scoring) | vendor, active | demo read-only; paid UNVERIFIED | exclude execution tools |
| 2 | CoinGecko MCP remote + coingecko/skills | MCP + SKILL.md | high (trending/new pools, top holders, new coins) | vendor | keyless / Demo 10k credits | replaces current CG ingestion |
| 3 | Hermes `watchers` + `cron` | official skill | high as polling/dedup substrate | in-tree | free | zero-code new-pool watcher |
| 4 | DexScreener REST via `watch_http_json.py` (or opensvm MCP) | MCP/REST | high (boosts, profiles, pairs) | API stable | free 60/300 rpm | prefer watchers over MCP |
| 5 | Helius MCP | MCP | high (Solana holders, tx history, webhooks) | vendor | 1M credits/mo | holder-growth / bundle features |
| 6 | GoPlus MCP + official solana authorities | MCP | medium-high (honeypot/tax/mint-freeze gate) | API stable | key by request | |
| 7 | Blockscout MCP hosted | MCP | medium (EVM/Base) | vendor | free PRO key | |
| 8 | Dune MCP | MCP | medium (ad-hoc SQL, backtests) | vendor | credits limited | research, not real-time |
| 9 | agiprolabs selected skills (token-holder-analysis, sybil-detection, pumpfun-mechanics, solanatracker-api) | SKILL.md | high as method prompts | medium | demo | import analysis, skip execution |
| 10 | Nansen MCP | MCP | high (smart-money screener) | vendor | 1,000 credits then paid | evaluate on free credits |

Not recommended: Moralis (discovery removed), Birdeye MCP (unverified, 1 rps free), kukapay servers (stale), any X proxy until cost quantified, Santiment.

Gaps Hermes cannot fill natively: Telegram channel monitoring via user account (keep the Telethon reader as an external service/MCP), durable pipeline datastore, backtest engine.
