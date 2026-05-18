# Moralis plan audit — BL-NEW-MORALIS-PLAN-AUDIT

Date: 2026-05-18
Backlog: BL-NEW-MORALIS-PLAN-AUDIT (filed 2026-05-13, decision-by 2026-05-20)
Status: AUDITED — phantom under current configuration

## Executive summary

The cycle-change audit (2026-05-13, B6) flagged a possible 25× over-cap on
Moralis's legacy free tier (994k projected/month vs 40k/month cap). That
projection was hypothetical — contingent on `MORALIS_API_KEY` being set.
**It is not set.** Verified empirically across three independent surfaces:

1. **`.env` state on srilu-vps:** `MORALIS_API_KEY=` is empty.
2. **Runtime logs:** 0 Moralis-related events in the last 24h; 0
   `holder_enrich`/`holder.lookup.failed`/`Helius holder` events in the
   last 7 days.
3. **`holder_snapshots` table:** 0 rows total since DB creation.

The early-return guard at `scout/ingestion/holder_enricher.py:37-38`
(`if not settings.MORALIS_API_KEY: return token`) prevents any Moralis
HTTP call when the key is empty. **Today the path is dead. The 25×
over-cap risk is phantom under current configuration.**

Risk converts from phantom to real if and only if the operator sets the
key without a throttle/cache layer in place first.

**Recommendation:** close `BL-NEW-MORALIS-PLAN-AUDIT` as AUDITED-PHANTOM
with evidence + file a small conditional guardrail entry
(`BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL`) so the path's hazards surface
the moment an operator tries to enable it. **No code change in this PR.**

## Drift-check (against master `b87ccd1`)

### Code paths

| Location | Behavior |
|---|---|
| `scout/config.py:130` | `MORALIS_API_KEY: str = ""` (Pydantic default empty) |
| `scout/ingestion/holder_enricher.py:13-17` | `MORALIS_CHAIN_MAP = {"ethereum": "eth", "base": "base", "polygon": "polygon"}` (3 chains) |
| `scout/ingestion/holder_enricher.py:36-39` | Early-return when key empty — `if not settings.MORALIS_API_KEY: return token` |
| `scout/ingestion/holder_enricher.py:71-95` | `_enrich_evm` → `GET https://deep-index.moralis.io/api/v2.2/erc20/{addr}/owners?chain={chain}` with `X-API-Key` header |
| `scout/main.py:944-948` | Per-cycle fan-out: `await asyncio.gather(*[enrich_holders(token, session, settings) for token in all_candidates])` |

### No throttle / cache / interval controls

`grep -nE "throttle|cache|interval|rate_limit" scout/ingestion/holder_enricher.py`
returns nothing relevant. The module has:

- No request rate limiter wired in
- No per-token cache or dedup
- No wall-clock interval gating (unlike `coingecko_limiter` which spaces calls)
- No bounded retry — `try/except Exception` catches all failures and returns the token unenriched

The implication for the "if-enabled" scenario: every `enrich_holders` call
on every EVM candidate every cycle would hit Moralis directly. This shape
is the source of the original 25× projection.

### No prior audit/throttle/swap work in tree

`grep -ril "moralis\|MORALIS"` across the repo returns:
- `scout/config.py`, `scout/ingestion/holder_enricher.py` (production)
- `tasks/findings_cycle_change_audit_2026_05_13.md` (the source audit)
- `tasks/research_hermes_crypto_skills_2026_05_14.md` (Hermes overlay)
- `tasks/findings_silent_failure_audit_2026_05_11.md` (notes empty
  `holder_snapshots` — cross-confirmation of the current dead-path state)
- `tests/test_holder_enricher.py`, `tests/test_config.py`
- `README.md`, `.env.example`

No throttle / cache / provider-swap work exists in tree. Backlog status:
PROPOSED.

## Runtime-state verification (srilu-vps, 2026-05-18T20:03Z)

### Env keys

```
MORALIS_API_KEY=         # empty
HELIUS_API_KEY=          # empty (related — same module, Solana path also dead)
```

### Service running, but holder-enrichment branch is short-circuited

```
ubuntu-4gb-hel1-1 uv[2543812] 2026-05-18T20:03:02Z heartbeat:
  uptime_minutes=80.8 tokens_scanned=4334 candidates_promoted=241
  alerts_fired=0
```

Pipeline is active and ingesting candidates. `enrich_holders` runs in
`asyncio.gather`, hits the early-return for EVM tokens, returns
unenriched tokens. No Moralis HTTP call is made.

### Log evidence

- `journalctl -u gecko-pipeline --since "24 hours ago" | grep -ic moralis` → **0**
- `journalctl -u gecko-pipeline --since "24 hours ago" | grep -ic "holder_enrich\|enrich_holders\|holder_count"` → **0**
- `journalctl -u gecko-pipeline --since "7 days ago" | grep -ic moralis` → **0**

### DB evidence

- `SELECT COUNT(*) FROM holder_snapshots` → **0**
- `holder_count` column exists on `candidates`; default = 0; never overwritten.

### Cohort calibration — would-be Moralis call rate IF enabled

EVM-chain candidates by `first_seen_at` (last 24h, srilu DB):

| Chain | Count (24h) |
|---|---|
| coingecko | 867 |
| solana | 175 |
| base | 18 |
| ethereum | 12 |
| avalanche | 1 |

Moralis-mappable chains (`ethereum + base + polygon`): **30 distinct
tokens / 24h**. Polygon = 0 in this window.

The audit's "23 EVM/cycle × 60 cycles/hr = 33k/day" math counted
per-cycle fan-out, not unique tokens. With ~12 cycles/hr observed today
(post-#170 deploy under conservative CG limiter) and ~30 unique
EVM-mappable tokens hydrated each cycle (estimate from 30 unique tokens
seen at least once in 24h), the realistic if-enabled rate is a range:

- 23 EVM/cycle × 12 cycles/hr × 24h × 30d = ~200k/month (matches the audit's per-cycle fan-out estimate)
- 30 EVM/cycle × 12 cycles/hr × 24h × 30d = ~260k/month (uses today's observed unique-EVM count as per-cycle proxy)

**Range: ~200k-260k/month, or 5-7× over the Moralis legacy-free 40k/month
cap.** Both endpoints of the range exceed the cap; magnitude is lower
than the audit's original 25× projection (which assumed 60 cycles/hr)
because actual cycle rate is currently ~12/hr.

**Direction confirmed:** if the key were enabled today on legacy-free,
the budget would be exceeded. The audit's conclusion shape is correct
even with the updated rate.

## Hermes-first (fresh check 2026-05-18)

Three surfaces plus the assignment's specific GoldRush comparison.

### Surface 1: installed VPS Hermes skills

`ls /home/gecko-agent/.hermes/skills/` returned 28 directories: apple,
autonomous-ai-agents, coin_resolver, creative, crypto_narrative_scanner,
data-science, devops, diagramming, dogfood, domain, email, gaming, gifs,
github, inference-sh, kol_watcher, mcp, media, mlops,
narrative_alert_dispatcher, narrative_classifier, note-taking,
productivity, red-teaming, research, smart-home, social-media,
software-development, yuanbao.

`grep -ril "moralis\|holder.?count\|token.?holders\|erc20.*owners"
/home/gecko-agent/.hermes/skills/`: 0 hits. No blockchain/EVM/Solana/
on-chain/goldrush/covalent directories installed.

### Surface 2: Hermes optional-skills catalog

`https://hermes-agent.nousresearch.com/docs/reference/optional-skills-catalog/`
(fetched 2026-05-18) — closest match: `blockchain/evm` skill, described
as "Read-only EVM client: wallets, tokens, gas across 8 chains."
Does not specifically cover holder counts. The skill is also NOT
installed on srilu.

### Surface 3: awesome-hermes-agent ecosystem

(Carry-forward from `research_hermes_crypto_skills_2026_05_14.md` plus
spot-check.) Lists Chainlink agent skills, `ripley-xmr-gateway`,
`AgentCash`, HermesHub, payment/search plugins. None covers ERC20 holder
enumeration.

### Surface 4 (assignment-specific): GoldRush / Covalent

`https://github.com/covalenthq/goldrush-agent-skills` (fetched 2026-05-18)
exposes 4 skills:

1. `goldrush-foundational-api` — REST API for historical and near-real-time
   data across 100+ chains: balances, transactions, NFTs, prices.
2. `goldrush-streaming-api` — Real-time GraphQL subscriptions.
3. `goldrush-cli` — Terminal tool with MCP support.
4. `goldrush-x402` — Pay-per-request protocol.

**ERC20 holder count capability: NO.** The foundational API description
emphasizes balances/transactions/NFTs/prices. Holder enumeration is not
listed as a function. Pricing: 14-day free trial + $10/month "vibe coding"
tier (credit allocation unspecified). **GoldRush does not cover the
Moralis use case for gecko-alpha's holder enrichment as it exists today.**

### Verdict table

| Domain | Hermes/external skill found 2026-05-18? | Decision |
|---|---|---|
| ERC20 holder count (ethereum/base/polygon) — installed VPS | No (0 hits across 28 installed skills) | Keep in-tree path |
| ERC20 holder count — Hermes optional catalog | Only adjacent `blockchain/evm` (read-only wallet/tokens/gas, not holders); not installed | Keep in-tree path |
| ERC20 holder count — awesome-hermes-agent | No | Keep in-tree path |
| ERC20 holder count — GoldRush/Covalent (assignment-specific) | No (balances/transactions/NFTs/prices listed; holder enumeration absent) | Keep in-tree path |
| Custom-code minimum-required surface | n/a | Today: no in-tree change because path is dead under current config |

One-sentence verdict: no installed or external Hermes/GoldRush skill
replaces gecko-alpha's Moralis holder-enrichment use case today. The
current in-tree path is correct in shape; only the rate-limit hazard
needs guarding before any future enablement.

## Risk classification

| Scenario | Today's state | Hazard |
|---|---|---|
| `MORALIS_API_KEY=""` (default) | **CURRENT** | None. Path dead. |
| Key set, plan = legacy-free (40k/mo) | Hypothetical | **Broken** — projected 200-260k/mo at current cohort × cycle rate vs 40k/mo cap |
| Key set, plan = CU-based paid | Hypothetical | **Billing-overage exposure** — usage scales with cohort × cycles; bill grows linearly |
| Key set + throttle/cache layer added | Hypothetical, future | Manageable — depends on throttle parameters |

The audit's 25× over-cap classification holds CONDITIONALLY — it requires
the key to be set. With the key empty, the path is **phantom-under-
current-config**.

## Recommendation

**Close BL-NEW-MORALIS-PLAN-AUDIT as AUDITED-PHANTOM** with evidence;
file conditional guardrail `BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL` so the
hazard surfaces if/when an operator decides to enable the path. No code
change in this PR.

### Why not build a throttle now

- Path is dead. Throttle would have nothing to throttle. Code complexity
  added for a zero-fire surface = debt without benefit.
- Throttle design depends on plan-tier decision (legacy-free needs
  hard-cap; CU-based needs budget-target; GoldRush swap would replace
  the surface entirely).
- Per assignment guardrail: "Do not add a new provider integration. Do
  not rewrite holder enrichment. Do not change live config."

### Why not deprecate / remove the path

- The path is correct in shape. The early-return at `holder_enricher.py:37`
  is the right guard. Removing the module would lose the option-value of
  enabling EVM holder enrichment later.
- `holder_count` is read by the scorer (per the silent-failure audit
  cross-ref to `holder_snapshots`); removing the column or the path would
  cascade into the scoring contract.

### Conditional guardrail entry (filed as new BL)

`BL-NEW-MORALIS-ENABLEMENT-GUARDRAIL` — operator-gated. Before setting
`MORALIS_API_KEY` on prod:

1. Confirm plan tier (legacy-free vs CU-based). Plan-tier check is
   ~2-min operator action via Moralis dashboard.
2. If legacy-free: add per-token cache (e.g., 24h TTL on `holder_count`)
   AND a hard daily call cap before enabling.
3. If CU-based paid: set a budget alert; verify that worst-case monthly
   spend at observed cohort × cycle rate stays within tolerance.
4. Re-check the Hermes-first / GoldRush question — by the time operator
   wants to enable, GoldRush or another provider may have added holder
   coverage and become preferable.

## Cross-finding worth flagging

`HELIUS_API_KEY` is also empty on srilu — Solana holder enrichment is
dead by the same mechanism (`holder_enricher.py:33-34`). The same
phantom-vs-real classification applies to `BL-NEW-HELIUS-PLAN-AUDIT`
which was scoped as a separate audit. Per assignment guardrail, Helius
is out of scope for THIS audit, but the same evidence-shape applies:

- `HELIUS_API_KEY=` empty
- 0 Helius events in 24h
- `holder_snapshots` empty (covers both chains)

The Helius audit will likely close as the same phantom finding once it
runs. No prep work needed here.

## What this doc is NOT

- Not a Moralis enablement plan. Operator-gated.
- Not a code change. The early-return guard is correct; no throttle
  added; no provider swap.
- Not a Helius audit closure. BL-NEW-HELIUS-PLAN-AUDIT remains a
  separate task (likely same shape).
- Not a `holder_count`-removal proposal. Path is intentionally
  preserved for future enablement.
