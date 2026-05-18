# Helius plan audit — BL-NEW-HELIUS-PLAN-AUDIT

Date: 2026-05-18
Backlog: BL-NEW-HELIUS-PLAN-AUDIT (filed 2026-05-13, decision-by 2026-05-20)
Status: AUDITED — phantom under current configuration; if-enabled within free-tier at today's rate

## Executive summary

The cycle-change audit (2026-05-13, B5) flagged Helius Solana holder
enrichment as `Broken-if-free / Phantom-if-paid` with a projected
~174k/day call rate vs Helius free-tier ~100k/day cap. That projection
was contingent on `HELIUS_API_KEY` being set AND the audit's assumed
60 cycles/hr rate. Both assumptions need verification.

**Verified phantom under current configuration.** Three independent surfaces:

1. **`.env` state on srilu-vps:** `HELIUS_API_KEY=` empty.
2. **Runtime logs:** 0 Helius events in 24h (7d count likely 0 — query
   truncated, but 24h zero + no key set implies steady-state zero).
3. **`holder_snapshots` table:** 0 rows total since DB creation.

Early-return guard at `scout/ingestion/holder_enricher.py:33-34`
(`if not settings.HELIUS_API_KEY: return token`) prevents any Helius
HTTP call when the key is empty. **Today the path is dead. The
"Broken-if-free" classification cannot fire.**

**If-enabled projection at today's rate is WITHIN free-tier.** Audit's
60 cycles/hr was the rate before the lane-reorder fix. Today's rate is
~12 cycles/hr post-#170 deploy. Recalibration: 121 solana/cycle × 12
cycles/hr × 24h ≈ **35k/day**, well under the Helius ~100k/day free-tier
cap. This is materially different from the Moralis result (5-7× over
legacy-free).

**Recommendation:** AUDITED-PHANTOM close + conditional guardrail. No
code change. The guardrail differs from Moralis's: at today's rate
Helius enablement is safe on free-tier; the risk surfaces only if the
cycle rate climbs back toward the audit's original 60 cycles/hr
(e.g., after Demo API key lifts the CG ceiling).

## Drift-check (against master `ef0a64a`)

### Code paths

| Location | Behavior |
|---|---|
| `scout/config.py:129` | `HELIUS_API_KEY: str = ""` (Pydantic default empty) |
| `scout/ingestion/holder_enricher.py:32-35` | Early-return when key empty — `if not settings.HELIUS_API_KEY: return token` |
| `scout/ingestion/holder_enricher.py:43-68` | `_enrich_solana` → `POST https://mainnet.helius-rpc.com/?api-key={KEY}` JSON-RPC `getTokenAccounts` with `limit=1` |
| `scout/main.py:944-948` | Per-cycle fan-out: `await asyncio.gather(*[enrich_holders(token, session, settings) for token in all_candidates])` |

### Same no-throttle structure as Moralis

`grep -nE "throttle|cache|interval|rate_limit" scout/ingestion/holder_enricher.py`
returns nothing relevant. The module has:

- No Helius-specific rate limiter
- No per-token cache or dedup (every call is a fresh `getTokenAccounts`)
- No wall-clock interval gating
- Bounded only by `asyncio.gather` cycle cadence
- `try/except Exception` at `:62-68` silently swallows failures —
  cross-confirmed by silent-failure audit §2.5's empty `holder_snapshots`

### Method semantics

`getTokenAccounts` with `params: {"mint": addr, "limit": 1}` returns
`data["result"]["total"]` — the total holder count. Each call is 1
Helius credit per the audit's documentation reference.

### No prior throttle / cache / swap work in tree

`grep -ril "helius\|HELIUS\|Helius"` across the repo returns:
- `scout/config.py`, `scout/ingestion/holder_enricher.py` (production)
- `tasks/findings_cycle_change_audit_2026_05_13.md` (source audit, B5)
- `tasks/research_hermes_crypto_skills_2026_05_14.md` (Hermes overlay)
- `tasks/findings_silent_failure_audit_2026_05_11.md` (empty
  `holder_snapshots` cross-ref)
- `tasks/findings_moralis_plan_audit_2026_05_18.md` (the cross-finding
  flagged at Moralis audit close)
- `tests/test_holder_enricher.py`, `tests/test_config.py`
- `README.md`, `.env.example`

No throttle / cache / provider-swap work exists in tree. Backlog status:
PROPOSED.

## Runtime-state verification (srilu-vps, 2026-05-18T~20:35Z)

### Env keys

```
HELIUS_API_KEY=         # empty
MORALIS_API_KEY=        # empty (related — closed 2026-05-18 as AUDITED-PHANTOM)
```

### Log evidence

- `journalctl -u gecko-pipeline --since "24 hours ago" | grep -ic "helius"` → **0**
- `journalctl -u gecko-pipeline --since "7 days ago" | grep -ic "helius"` → query timed out before count returned; the 24h count is 0 and the key has not been set, so 7d is structurally bounded by the same zero. (Cross-confirmed by `holder_snapshots=0`.)

### DB evidence

- `SELECT COUNT(*) FROM holder_snapshots` → **0** (entire table, all-time)
- `holder_count` column exists on `candidates`; default = 0; never overwritten.

### Cohort calibration

Solana candidates by `first_seen_at` (srilu DB):

| Window | Count |
|---|---|
| 24h | 177 |
| 7d | 621 |

The audit's "121 solana/cycle" figure came from `tokens_per_cycle=289 × ~42% solana`. At today's measured 12 cycles/hr (post-#170 conservative CG limiter), if-enabled projection:

```
121 solana/cycle × 12 cycles/hr × 24h × 1 credit/call = ~34,848 credits/day
```

**~35k/day vs Helius free-tier ~100k/day = within cap by ~3×.**

If the cycle rate climbs back to the audit's assumed 60 cycles/hr (e.g.,
post-Demo-API-key when the CG ceiling lifts), projection scales to
~174k/day, returning to the audit's original "Broken-if-free" zone.

**The risk is rate-dependent and currently inert.**

## Hermes-first (fresh check 2026-05-18, 4 surfaces)

### Surface 1: installed VPS Hermes skills

`ls /home/gecko-agent/.hermes/skills/`: 28 directories (same set as
Moralis audit). `grep -ril "helius\|getTokenAccounts\|solana.*holder\|
SPL.*holder"`: 0 substantive hits. Tangential matches in
`creative/popular-web-designs/templates/` (framer/warp/miro) are
unrelated design-template text containing the word "solana" as a
buzzword. No `blockchain/solana` directory installed.

### Surface 2: Hermes optional-skills catalog

`https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/blockchain/blockchain-solana`
(fetched 2026-05-18). Capabilities: wallet balances, token portfolios
with USD values, transaction details, NFTs, whale detection, network
stats — via RPC + CoinGecko. **Partial holder support:** the `token`
command returns "top 5 holders with percentages" for individual SPL
tokens. **Does NOT provide `getTokenAccounts` for total holder count
enumeration** — relies on heuristic-based detection (amount=1, decimals=0)
for NFTs and standard balance queries.

The Hermes `blockchain/solana` skill is NOT installed on srilu. Even if
installed, the partial top-5 coverage doesn't replace gecko-alpha's
full-count use case (`token.holder_count` is a scalar total used by the
scorer).

### Surface 3: awesome-hermes-agent ecosystem

`https://github.com/0xNyk/awesome-hermes-agent` (fetched 2026-05-18).
Zero entries cover Helius / Solana token holder count / Solana RPC /
`getTokenAccounts` / Solana on-chain enrichment.

### Surface 4: GoldRush/Covalent (assignment-specific)

Carry-forward verified from Moralis audit (same fetch 2026-05-18). The
4 GoldRush skills (foundational REST, streaming, CLI, x402) cover
balances/transactions/NFTs/prices across 100+ chains but **do not
enumerate token holders** — the foundational API description does not
list holder enumeration. Applies equally to Solana SPL tokens. No
Solana-specific holder-count capability documented.

### Verdict table

| Domain | Hermes/external skill found 2026-05-18? | Decision |
|---|---|---|
| SPL token holder count (full enumeration) — installed VPS | No (0 hits across 28 skills) | Keep in-tree path |
| SPL holder count — Hermes optional `blockchain/solana` | Partial only (top-5 holders, not full count); not installed | Keep in-tree path |
| SPL holder count — awesome-hermes-agent | No | Keep in-tree path |
| SPL holder count — GoldRush/Covalent (assignment-specific) | No (balances/transactions/NFTs/prices listed; holder enumeration absent for both EVM and Solana) | Keep in-tree path |
| Helius-specific Hermes skill or repo | No Helius-specific skill found in any surface | Keep in-tree path |

One-sentence verdict: no installed or external Hermes/GoldRush skill
replaces gecko-alpha's Helius `getTokenAccounts`-based holder-count use
case. In-tree path stays correct in shape.

## Risk classification

| Scenario | Today's state | Hazard |
|---|---|---|
| `HELIUS_API_KEY=""` (default) | **CURRENT** | None. Path dead. |
| Key set, today's 12 cycles/hr cadence | Hypothetical | **Safe** — projected ~35k/day < ~100k free cap |
| Key set, cycle rate climbs to ~30 cycles/hr (e.g., post-Demo-API-key partial relief) | Hypothetical | **Borderline** — projected ~87k/day approaches free-tier cap |
| Key set, cycle rate climbs to audit's 60 cycles/hr | Hypothetical | **Broken-if-free** — projected ~174k/day (1.7× over free cap) |

Helius risk is **rate-dependent** in a way Moralis's was not. The
binding variable is cycle rate, which is itself a function of CG
rate-limit headroom (more headroom → faster cycles → more Helius
calls if enabled).

## Recommendation

**Close BL-NEW-HELIUS-PLAN-AUDIT as AUDITED-PHANTOM** + file conditional
guardrail. No code change in this PR.

### Why not build a throttle now

- Path is dead. Throttle would have nothing to throttle.
- Throttle design depends on plan-tier decision AND projected cycle
  rate — both uncertain. Premature design.
- Per assignment guardrail: "Do not add a new provider integration. Do
  not rewrite holder enrichment. Do not change live config."

### Why not deprecate / remove the path

- The Solana early-return at `holder_enricher.py:33-34` is correct
  shape — protects against the dead-key case.
- `holder_count` is read by the scorer; removing the column or path
  cascades into scoring contract.
- Option-value of enabling later (e.g., if a holder-count-based signal
  proves valuable) is worth preserving.

### Conditional guardrail entry (filed as new BL)

`BL-NEW-HELIUS-ENABLEMENT-GUARDRAIL` — operator-gated. Before setting
`HELIUS_API_KEY` on prod:

1. Confirm plan tier (free vs paid) via Helius dashboard.
2. Confirm current cycle rate via `journalctl ... | grep -c
   secondwave_cycle_complete` over a recent 1h window. If still ≤15
   cycles/hr: free-tier likely safe at observed cohort. If ≥30
   cycles/hr: add per-token holder cache (24h TTL) before enabling.
3. Capture pre-enablement baseline + post-enablement 2h validation
   window (mirrors `runbook_cg_demo_api_key_2026_05_18.md` structure):
   verify `holder_snapshots` row-rate, log absence of `cg_429_backoff`-
   equivalent (Helius 429s would surface as `Helius holder lookup failed`),
   confirm credit-usage at the Helius dashboard.
4. Re-check Hermes-first / GoldRush at enablement time — by then a
   Solana-specific holder skill may exist and become preferable.

## Differences from Moralis audit (worth documenting)

| Dimension | Moralis (closed PR #173) | Helius (this audit) |
|---|---|---|
| Provider type | REST | JSON-RPC |
| Method | `GET /erc20/{addr}/owners` | `POST getTokenAccounts` |
| Auth shape | `X-API-Key` header | Query-param `?api-key=KEY` |
| Free-tier cap | 40k/month | ~100k/day (~3M/month) |
| If-enabled projection at today's rate | ~200-260k/month = **5-7× over** | ~35k/day = **0.35× of cap (under)** |
| Risk shape | Always over-cap if enabled | **Rate-dependent** — currently safe; climbs back to risk if cycles speed up |

The risk profiles are materially different. Helius is safer-by-luck
under today's degraded cycle rate. Moralis is structurally over-cap on
legacy-free.

## What this doc is NOT

- Not a Helius enablement plan. Operator-gated.
- Not a code change. The early-return guard is correct; no throttle
  added; no provider swap.
- Not a Moralis re-audit. PR #173 closed that.
- Not a `holder_count`-removal proposal. Path is intentionally
  preserved for future enablement.
- Not a recommendation to enable Helius now. The decision to enable
  is operator-gated and warrants the BL-NEW-HELIUS-ENABLEMENT-GUARDRAIL
  checklist first.
