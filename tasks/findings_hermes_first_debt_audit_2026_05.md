**New primitives introduced:** NONE - audit/findings artifact only.

# Hermes-First Custom-Code Debt Audit - 2026-05-14

## Purpose

Classify existing gecko-alpha custom code and open backlog items against the
current Hermes / agent-skill ecosystem. This is a debt-reduction pass, not a
runtime change. The goal is to stop future work from extending custom surfaces
where an installed or credible upstream skill can own the workflow.

## Inputs checked

### Drift-check in tree

- Market ingestion exists in `scout/ingestion/coingecko.py`, with
  `fetch_top_movers`, `fetch_trending`, and `fetch_by_volume` at lines 61, 137,
  and 188.
- `scout/main.py` already combines CoinGecko market rows into
  `_raw_markets_combined` around line 536 and feeds volume/gainers/momentum/
  slow-burn/velocity surfaces at lines 546-672.
- `scout/models.py` still has dead or weakly populated fields:
  `holder_growth_1h` and `social_mentions_24h` at lines 35-36.
- `scout/scorer.py` still scores `holder_growth_1h` and
  `social_mentions_24h` at lines 100 and 121.
- Hermes/X narrative rows are already represented locally:
  `narrative_alerts_inbound` migration in `scout/db.py:3342`, insert path in
  `scout/api/narrative_resolver.py:96`, and X Alerts dashboard reads in
  `dashboard/db.py:1282`.
- Telegram social rows already exist: `tg_social_messages` /
  `tg_social_signals` migrations in `scout/db.py:1161` and `scout/db.py:1176`,
  writes in `scout/social/telegram/listener.py:498` and line 580.
- Existing custom ops/watchdog scripts include
  `scripts/gecko-backup-watchdog.sh`,
  `scripts/held-position-price-watchdog.sh`, and
  `scripts/minara-emission-persistence-watchdog.sh`.

### Hermes / agent-skill checks

- Installed VPS Hermes skills include `social-media/xurl`, `kol_watcher`,
  `narrative_classifier`, `narrative_alert_dispatcher`, and
  `crypto_narrative_scanner`.
- No installed VPS Hermes skill provides CoinGecko/GeckoTerminal market-breadth
  runtime ingestion.
- CoinGecko first-party Agent SKILL exists and documents endpoints,
  parameters, and common workflows: https://docs.coingecko.com/docs/skills and
  https://github.com/coingecko/skills.
- GoldRush/Covalent provides Hermes MCP and agent skills for wallet, holder,
  transfer, pricing, DEX pair, and security-style blockchain data:
  https://goldrush.dev/agents/hermes-agent/ and
  https://github.com/covalenthq/goldrush-agent-skills.
- HermesHub exists as a community registry and includes communication/data
  skills such as `relay-for-telegram`, `slack-bot`, `data-analyst`, and
  `scrapling`: https://github.com/amanning3390/hermeshub.
- PRB agent-skills includes `coingecko-cli`, `coingecko-historical`,
  `etherscan-api`, and `evm-chains`, but it is lower-provenance than
  first-party CoinGecko/GoldRush and should be treated as secondary reference:
  https://github.com/PaulRBerg/agent-skills.

## Classification legend

- `KEEP_CUSTOM`: gecko-alpha should own this runtime/persistence/scoring
  primitive.
- `USE_SKILL_AS_REFERENCE`: a skill improves endpoint/API correctness, but
  gecko-alpha keeps the production runtime.
- `REPLACE_WITH_HERMES`: retire or avoid custom code in favor of an installed
  or upstream skill.
- `BRIDGE_TO_HERMES`: gecko-alpha should expose or consume a narrow interface
  while Hermes owns the workflow.
- `DELETE_OR_DEFER`: stale backlog surface or no longer worth building.

## Findings

| Area | Current custom surface | Classification | Decision |
|---|---|---|---|
| CoinGecko market ingestion | `scout/ingestion/coingecko.py`, `_raw_markets_combined`, snapshot tables, signal detectors | `KEEP_CUSTOM` + `USE_SKILL_AS_REFERENCE` | Keep runtime in gecko-alpha. Use CoinGecko SKILL/API docs for endpoint correctness in the upcoming breadth/hydration fix. |
| CoinGecko trending hydration gap | `fetch_trending` stores trending raw separately; downstream raw-market surfaces ignore `last_raw_trending` today | `KEEP_CUSTOM` + `USE_SKILL_AS_REFERENCE` | Build the hydration fix in gecko-alpha. Skill is reference, not runtime replacement. |
| GeckoTerminal / DexScreener ingestion | Existing aiohttp ingestion, retries, dashboard/signal consumers | `KEEP_CUSTOM` | No installed/upstream Hermes skill replaces durable DEX market ingestion. Continue custom but enforce drift/Hermes checks per diff. |
| `social_mentions_24h` scorer field | Field exists in `CandidateToken`; scorer gives points; no reliable writer | `BRIDGE_TO_HERMES` or `DELETE_OR_DEFER` | Do not build custom Twitter/LunarCrush first. Audit existing Hermes X + Telegram rows; either re-map to `kol_mentions_24h` / `narrative_mentions_24h` or delete the dead field. |
| Custom Twitter/LunarCrush roadmap | BL-032 and old Early Detection roadmap proposed third-party social APIs | `REPLACE_WITH_HERMES` for X/KOL; `DELETE_OR_DEFER` for paid social until residual gap is proven | Installed Hermes X path is the first-line social source. LunarCrush/Santiment require a new residual-gap design. |
| Telegram social listener | `scout/social/telegram/*`, `tg_social_messages`, `tg_social_signals` | `KEEP_CUSTOM` | This is project-specific ingestion from operator-curated channels. Hermes may help with future analysis, but the DB writer stays custom. |
| Narrative scanner | Hermes skills write into `narrative_alerts_inbound`; gecko-alpha endpoint persists | `BRIDGE_TO_HERMES` | Correct split: Hermes owns X/KOL intelligence; gecko-alpha owns HMAC endpoint, DB, dashboards, and downstream signal evaluation. |
| Helius/Moralis audits | BL-NEW-HELIUS-PLAN-AUDIT and BL-NEW-MORALIS-PLAN-AUDIT | `BRIDGE_TO_HERMES` / `DELETE_OR_DEFER` pending provider comparison | Before throttles/upgrades, compare each use case with GoldRush MCP/skills and optional Hermes blockchain skills. Prefer provider consolidation over parallel custom integrations. |
| Dune/Nansen/smart-money roadmap | Virality roadmap proposes custom provider builds | `BRIDGE_TO_HERMES` | GoldRush is now first comparison point for wallet/holder/transfer intelligence. Dune/Nansen only after GoldRush fails a specific requirement. |
| pump.fun copycat watcher | Roadmap proposes custom new-deploy watcher | `DELETE_OR_DEFER` pending residual-gap analysis | Not started today. If revived, compare against GoldRush streaming/new DEX pair support and Solana optional skills first. |
| Prometheus/Grafana | BL-043 full metrics stack | `DELETE_OR_DEFER` / `BRIDGE_TO_HERMES` | Keep deferred. Hermes ops/HermesHub communication skills may reduce operator-facing needs; DB freshness watchdogs still stay custom. |
| Table freshness/watchdogs | backup, held-position, minara persistence, future per-table SLOs | `KEEP_CUSTOM` | Watchdogs read gecko-alpha-specific DB/output subsets. Skills can notify, but cannot replace the project-owned checks. |
| Operator alert delivery | Existing `scout.alerter` and narrative Path C1 backlog | `KEEP_CUSTOM` / `BRIDGE_TO_HERMES` | Installed Hermes had no outbound alert primitive. Keep gecko-alpha alerter as the project alert boundary until a concrete Hermes notification skill is installed and reviewed. |
| Minara/live execution | BL-074, M1.5c Minara alert extension, live adapter plans | `BRIDGE_TO_HERMES` | Minara/Hermes should own DEX execution UX; gecko-alpha should own signal generation, risk gates, audit, and reconciliation. Avoid expanding custom execution adapters without BL-055/live-policy gates. |
| GEPA/eval/model routing | BL-073 Phase 1 and meta-classifier roadmap | `REPLACE_WITH_HERMES` / `BRIDGE_TO_HERMES` | Prefer Hermes self-evolution/eval harness for narrative prompt/classifier improvement before building another bespoke classifier loop. |

## Priority actions from this audit

1. **Next build:** CoinGecko breadth + trending hydration fix. Runtime stays
   custom; CoinGecko SKILL/API docs are mandatory reference.
2. **Next audit:** BL-032 social feature audit. Verify schemas and live row
   rates for `tg_social_*` and `narrative_alerts_inbound` before adding any
   social API.
3. **Provider cleanup:** turn Helius/Moralis plan audits into an on-chain
   provider consolidation comparison that includes GoldRush.
4. **Roadmap cleanup:** treat LunarCrush/Santiment/Nansen/Dune/pump.fun
   roadmap items as historical unless a new Hermes-first residual-gap design
   revives them.
5. **Guardrail:** every future plan touching market data, social data,
   on-chain data, ops alerts, or execution must cite this findings file plus
   `tasks/research_hermes_crypto_skills_2026_05_14.md`.

## Non-goals

- No runtime code changes in this audit.
- No new skill/plugin installation.
- No deletion of existing custom modules without a separate migration/design.
- No claim that CoinGecko/GoldRush skills are production drop-in replacements
  for gecko-alpha persistence and scoring.
