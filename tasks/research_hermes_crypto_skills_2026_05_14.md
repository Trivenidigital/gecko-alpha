**New primitives introduced:** NONE - research/tracking artifact only.

# Hermes Crypto Skills Research - 2026-05-14

## Purpose

Capture the crypto-relevant Hermes / agentskills ecosystem findings from the
Top Gainers gap investigation before any gecko-alpha implementation work starts.
This prevents the new skills from being lost in chat history and makes the next
CoinGecko breadth/hydration design referenceable.

## Installed VPS Hermes surface

Checked on `srilu-vps` / `ubuntu-4gb-hel1-1` under
`/home/gecko-agent/.hermes` on 2026-05-14.

| Capability area | Installed skill/plugin found? | Notes |
|---|---|---|
| X/KOL monitoring | yes - `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, `social-media/xurl` | Reuse for X-side narrative signals. Do not duplicate in gecko-alpha ingestion. |
| CoinGecko market breadth / top-gainer screening | no | No installed CoinGecko market-screening skill. |
| GeckoTerminal market hydration / DEX screening | no | No installed GeckoTerminal-specific screening skill. |
| On-chain wallet/token intelligence | optional skills present - `blockchain/base`, `blockchain/solana`, `research/polymarket`; not installed as project runtime | Useful for future on-chain enrichment, not a direct replacement for current CoinGecko breadth ingestion. |
| Operator notification primitive | no project-ready outbound alert skill | Prior Path B decision remains valid for narrative dispatcher V1. |

Inventory command output was reviewed during this research pass; the durable
summary is captured in the table above.

## Public ecosystem findings

| Finding | Source | What it covers | gecko-alpha decision |
|---|---|---|---|
| CoinGecko Agent SKILL | [CoinGecko docs](https://docs.coingecko.com/docs/ai-agent-hub/skills), [coingecko/skills](https://github.com/coingecko/skills) | First-party SKILL package for CoinGecko API endpoints, parameters, and workflows. CoinGecko says it works with Codex CLI and other SKILL-compatible agents. | Track as API-reference skill. Use it during design/review for CoinGecko endpoint correctness. Do not replace gecko-alpha runtime persistence, dedupe, scoring, or watchdog logic. |
| CoinGecko MCP Server | [CoinGecko docs](https://docs.coingecko.com/docs/ai-agent-hub/skills) | MCP pairing recommended by CoinGecko for interactive exploration. | Optional research tool only. Not appropriate as V1 production ingestion path unless separately justified. |
| GoldRush Agent Skills | [covalenthq/goldrush-agent-skills](https://github.com/covalenthq/goldrush-agent-skills) | Agent skills for historical and near-real-time blockchain data across 100+ chains, streaming GraphQL, CLI/MCP, x402 access. | Track for future on-chain wallet/holder/DEX intelligence. Not a substitute for CoinGecko top-gainer market breadth. |
| GoldRush Hermes MCP integration | [GoldRush Hermes guide](https://goldrush.dev/agents/hermes-agent/) | Hermes can connect GoldRush through MCP for wallet monitoring, token holders, transfers, OHLCV, DEX pairs, and scheduled on-chain analysis. | Candidate future capability for on-chain validation and whale/holder analysis. Requires API key or x402 decision; not free custom code replacement today. |
| HermesHub | [amanning3390/hermeshub](https://github.com/amanning3390/hermeshub) | Early curated Hermes skill registry. Lists skills such as `relay-for-telegram`, `slack-bot`, `data-analyst`, `scrapling`, and x402 marketplace primitives. | Add to future Hermes-first search surface. Useful discovery layer, but no specific CoinGecko breadth skill found. |
| awesome-hermes-agent | [0xNyk/awesome-hermes-agent](https://github.com/0xNyk/awesome-hermes-agent) | Curated ecosystem list. Newly relevant entries include `chainlink-agent-skills`, `ripley-xmr-gateway`, `AgentCash`, HermesHub, and Hermes payment/search plugins. | Keep in mandatory Hermes-first checks. Chainlink/ripley are not current fit; AgentCash/x402 may matter if paid data APIs become desirable. |

## Hermes-first decision for the Top Gainers gap work

| Domain | Hermes skill found? | Decision |
|---|---|---|
| CoinGecko top-gainer / market breadth ingestion | Upstream first-party CoinGecko SKILL found, but not installed on VPS and not a production ingestion runtime. | Use CoinGecko SKILL as API-reference input; implement persistence/scanning changes inside existing gecko-alpha ingestion after drift check. |
| Trending-token market hydration | CoinGecko SKILL covers API knowledge; no installed Hermes runtime primitive hydrates gecko-alpha's DB. | Build in `scout/ingestion/coingecko.py`, guided by CoinGecko SKILL/API docs. |
| On-chain token/holder/wallet enrichment | GoldRush skills/MCP found upstream; optional Hermes blockchain skills present. | Defer as separate on-chain validation backlog, not part of the current CoinGecko breadth fix. |
| X/KOL narrative signals | Installed Hermes `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, and `xurl` cover this domain. | Reuse Hermes path; keep X/KOL work out of gecko-alpha market ingestion. |

Awesome-hermes-agent ecosystem check: relevant ecosystem repositories now exist
for CoinGecko API knowledge, GoldRush/Covalent chain data, Chainlink oracle data,
and registries such as HermesHub. None replaces gecko-alpha's production
market-breadth ingestion, persistence, scoring, or signal observability.

One-sentence verdict: for the current Top Gainers miss, Hermes-first does not
justify a runtime handoff, but it does require using CoinGecko's first-party
SKILL/API docs as design-review input before changing custom ingestion code.

## Follow-up tracking

1. `BL-NEW-HERMES-CRYPTO-SKILLS-TRACKING` records the recurring discovery work.
2. The next CoinGecko breadth/hydration design should cite this file and include
   the CoinGecko SKILL as a checked source.
3. Do not install new VPS skills/plugins until a design names the concrete use,
   auth/cost surface, failure semantics, and rollback path.
