# Findings — Four-chain coverage matrix (evidence-backed), 2026-09-13

Companion to `tasks/design_rh_pons_discovery_delta_2026_09_13.md`. Every cell
cites code, a dated finding, or is an explicit UNKNOWN. "Configured",
"implemented", "observed", and "verified" are distinct claims and are not
collapsed. Telegram identity recognition does not establish chain support.

Legend: ✅ implemented+in-repo-evidence · ⚠️ partial/conditional · ❌ proven
negative (dated) · ❓ unknown (no evidence either way)

## 1. The matrix

| Surface | Robinhood Chain (4663) | Base (8453) | BNB Smart Chain (56) | Ethereum (1) |
|---|---|---|---|---|
| **Discovery** | ❌ none today; ⚠️ this increment adds the INERT Pons collector (`scout/ingestion/rh_pons.py`, disabled, deployment unverified) | ⚠️ provider-passthrough only: DexScreener boosts carry whatever `chainId` DS reports (`scout/ingestion/dexscreener.py:114-126`); GT lanes only poll configured networks — `DEX_DISCOVERY_NETWORKS` default `["solana"]` (`scout/config.py:370`) | ⚠️ same passthrough caveat | ⚠️ same passthrough caveat; CG markets/trending are listing-level (`chain='coingecko'`), not chain-resolved at ingest |
| **Identity** | ⚠️ `dex:robinhood:{addr}` identity class exists (`scout/config.py:1236`, `scout/social/telegram/shadow.py:298`) — recognition only, deliberately separated so Solana coverage can't mask it (`tasks/design_tg_signal_rehabilitation_2026_08_12.md:445`); `contract_coin_map` (I1) is chain-agnostic | ✅ contract-keyed via I1 | ✅ contract-keyed via I1 | ✅ contract-keyed via I1 |
| **Pricing** | ❌ unpriceable today: `TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES` example is exactly `["dex:robinhood"]` (`.env.example:193`); Minara: PROVEN NEGATIVE 2026-08-01 (`tasks/findings_minara_capability_matrix_2026_08_01.md:77`) — dated, provider-specific | ✅ DS/GT/CG price paths | ✅ DS/GT/CG price paths | ✅ DS/GT/CG price paths |
| **Safety (GoPlus)** | ❌ not in `CHAIN_ID_MAP` (`scout/safety.py:12-17`); chain string passes through verbatim → no record → strict returns `(False, False)` = unknown. NOTE: the legacy alert-path `is_safe()` would FAIL-OPEN such a token to "safe" (`scout/main.py:1344`) — currently unreachable (no RH candidates are emitted), recorded as a precondition for any future RH candidate emission | ✅ mapped `"base": "8453"` | ⚠️ NOT in the map despite the comment naming 56; name passes through — whether GoPlus accepts `"bsc"` verbatim is UNVERIFIED | ✅ mapped `"ethereum": "1"` |
| **Quoting (0x)** | ❌ no AllowanceHolder declared for 4663 (`scout/live/evm/flows.py:196-198`) → `assert_spender_is_not_settler` refuses fail-closed; Minara 2026-08-01: no unsigned-tx path on either side | ❌ **NOT verified**: allowlist contains chain 1 ONLY — "Base may remain the first execution reference" requires adding + verifying Base's AllowanceHolder first | ❌ same — not declared | ✅ declared + observed on live artifacts 2026-08-02 (`flows.py:195-198`; `tasks/findings_zeroex_unsigned_tx_probe_2026_08_02.md`) |
| **Simulation** | ❌ nothing | ❌ blocked on quoting row | ❌ blocked on quoting row | ⚠️ adapter declares `supports_simulation=True` (`scout/live/evm/adapter.py`); live tests exist under `tests/live/` — implemented, not routinely exercised |
| **Reconciliation** | ❌ nothing | ❌ nothing chain-specific | ❌ nothing chain-specific | ⚠️ artifact validation + `live_trades.intent_hash` venue-neutral binding (`scout/db.py` migration 20260802); EVM receipts-based reconciliation not yet end-to-end |

Solana is deliberately out of the four-chain scope but is the currently
best-covered chain (own execution lane `scout/live/solana_lane.py`, Helius
holder enrichment, `+5` scorer bonus at `scout/scorer.py:269-272`) — stated so
aggregate results are not read as if the four target chains produced them.

## 2. Execution-path safety trace (ruling 4)

Traced 2026-09-13, whole-repo grep + read:

- `scout/main.py:1344` — legacy FAIL-OPEN `is_safe()`; **alert path only**.
- `scout/social/telegram/resolver.py:269` — `is_safe_strict()`; BL-064 TG
  dispatcher lane (fail-closed).
- `scout/live/**` — **zero** imports of `scout.safety`; execution gating is
  capability declarations (`VenueCapabilities`, fail-closed), address
  allowlists (`ALLOWANCE_HOLDER_BY_CHAIN`, refuse-unknown), and mandate/intent
  binding. So the execution path is NOT downstream of the fail-open wrapper —
  the alert-path fail-open does not imply the execution path is unsafe. The
  gap statement is narrower: token-level safety (GoPlus verdict) is simply not
  an input to execution eligibility yet; this increment's
  `execution_eligibility()` mapper (unknown ⇒ ineligible, reusing
  `is_safe_strict` semantics) is the seam where it becomes one.

## 3. Suppression boundaries modeled separately (ruling 3)

| Boundary | Where | Value | Kind of suppression | Downstream effect |
|---|---|---|---|---|
| Scorer liquidity floor | `scout/scorer.py:169`, `MIN_LIQUIDITY_USD` (`scout/config.py:131`) | $15,000, **exempts `chain=='coingecko'`** | Score rejection | Token scores 0 → never reaches gate/alerts; still ingested + persisted |
| DEX discovery dust floor | `scout/ingestion/gt_new_pools.py:157`, `DEX_DISCOVERY_MIN_LIQUIDITY_USD` (`scout/config.py:375`) | $1,000 | **Discovery exclusion** | Pool never recorded in `dex_pool_discoveries` — invisible to research too |
| Counter liquidity_trap | `scout/counter/flags.py:222-237` | hardcoded $15k (high) / $30k (medium) | **Advisory RedFlag only** | Rendered on counter-risk surface; suppresses nothing |
| Age scoring | `scout/scorer.py:199-212` | <3h and >7d score 0 age points (peak 12–48h) | Score dampening, NOT rejection | Newborns lose up to 15/100 normalized points |
| Counter token_too_new | `scout/counter/flags.py:240-249` | <6h high, <12h medium | Advisory RedFlag only | Display only |
| 7d volume history | `scout/scorer.py:241-244` | `vol_7d_avg` required for vol_acceleration (25 raw pts) | Signal unavailability | Newborns can't earn the largest single signal; capability-divisor question — whether the denominator adjusts — is a shadow-audit item, NOT changed here |
| Solana bonus | `scout/scorer.py:269-272` | +5 raw | Relative disadvantage to all non-Solana chains | Cross-chain ranking skew |

These are four different mechanisms (discovery exclusion, score rejection,
score dampening/signal unavailability, advisory flag) and are not
interchangeable; any curve-native shadow scoring must use **real reserves and
executable depth** — virtual curve reserves are not withdrawable liquidity.
No production threshold is changed by this increment.

## 4. Seeded historical findings (ruling 5 — dated, provider-specific)

- `tasks/findings_minara_capability_matrix_2026_08_01.md` — Minara: zero
  Robinhood occurrences across its 453KB docs corpus; PROVEN NEGATIVE for that
  provider on that date. Not "Robinhood unsupported everywhere".
- `docs/hermes-skills-inventory.md:208` — one EVM client's chain list lacks
  Robinhood Chain; that client, that date.
- `tasks/findings_zeroex_unsigned_tx_probe_2026_08_02.md` — 0x AllowanceHolder
  observed live on Ethereum mainnet 2026-08-02.
- Freshness caveat: all pre-date this increment by ~6 weeks; Robinhood Chain
  launched 2026-07-01 and its ecosystem (Pons v2 shipped 2026-08-03) moves
  fast. Re-verification required before relying on any negative.
