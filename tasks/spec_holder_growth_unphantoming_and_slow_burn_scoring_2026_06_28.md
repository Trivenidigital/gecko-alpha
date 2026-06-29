**New primitives introduced:** PROPOSED ONLY (none authorized for build in this document) —
`txns_h1_buys_snapshots` table, `txns_growth_1h` model field, `TXNS_GROWTH_THRESHOLD` setting,
`buy_txn_growth` scorer signal (free-tier proxy for the dead `holder_growth`). Plus config
recalibration of `MIN_SCORE` (not a new primitive). A dedicated slow-burn lane is named but
explicitly deferred.

# Spec — Holder-growth un-phantoming + slow-burn scoring (decision framing)

**Date:** 2026-06-28
**Status:** DECISION FRAMING. **Not an implementation authorization.**
**Evidence base:** `findings_slow_burn_under_gate_backtest_2026_06_28.md`
**Trigger:** "why didn't we catch ANSEM (477×) early, and how do we catch plays like this?"

## Scope guardrails (operator-imposed, 2026-06-28)

- ❌ **Do NOT change prod thresholds yet** (`MIN_SCORE`, `vol_liq_ratio`, `token_age`).
- ❌ **Do NOT build the slow-burn lane yet.**
- ❌ **Do NOT enable paid Helius/Moralis blindly** — paid feed is "on the table" pending the
  costed memo below.
- ✅ **Default design constraint: free-tier-first, paid-feed-compatible.**
- ✅ Next gate is **evidence, not code.**

---

## Hermes-first analysis

Checked against the Hermes skill hub (`hermes-agent.nousresearch.com/docs/skills`) and the
awesome-hermes-agent ecosystem for capabilities this work would otherwise build custom.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| On-chain holder enrichment | None found (hub catalog did not render a crypto/Solana/holder skill; no match in the agent-skills ecosystem) | Build from scratch — but prefer the **free DexScreener proxy** over any new API integration |
| Solana token analytics | None found | Build from scratch — DexScreener/GeckoTerminal parsing already in-tree (`models.py`) |
| Crypto holder-distribution data | None found | Build from scratch — N/A; proxy avoids the need |

**awesome-hermes-agent ecosystem check:** the `NousResearch/awesome-hermes-agent` repo returns
404 and no discoverable registry covers crypto/Solana holder analytics. **Verdict:** no Hermes
skill applies; the chosen direction (free in-tree proxy) needs no external capability anyway.

## Drift-check (existing in-tree primitives — do not reinvent)

- `scout/ingestion/holder_enricher.py` — Helius/Moralis enricher **already exists** (96 LOC),
  guarded off when keys are empty (`:33–39`). Un-phantoming = supplying a feed OR adding a proxy,
  **not** writing an enricher.
- `volume_snapshots` table + `vol_7d_avg` rolling computation (`main.py:1096`) — the **pattern** to
  copy for a `txns_h1_buys_snapshots` accumulation tracker.
- `holder_snapshots` table — exists, empty for Solana; the proxy can write an analogous table.
- Cross-surface conviction (#364) and prospective sub-$30M watchlist (#372) — already shipped,
  observe-only. Findings show they are **structurally lagging** for this archetype (surfaces 2–6 are
  CG-trending-derived and only fire at $7M+), so they are **not** the fix here. Do not duplicate.

---

## Problem statement — four separated claims

### A. CONFIRMED issue — the alert gate is unreachable; winners sit under `MIN_SCORE`

Over 7 days (1,606 scored contracts) the **max score reached was 59**; **0 contracts hit `MIN_SCORE=65`**;
**0 alerts fired**; the gate **never ran**. ANSEM peaked at 50 and decayed to 13. This is not a
ranking problem — it is an **unreachable-threshold** problem. (Findings §2, §3.)

### B. CONFIRMED phantom — Solana `holder_growth` (25 pts) cannot fire

`HELIUS_API_KEY`/`MORALIS_API_KEY` empty → 0/491 Solana rows have `holder_count>0` → the 25-pt
accumulation signal is dead for 100% of the corpus where the winners live. (Findings §4.)

### C. UNPROVEN-BUT-LIKELY — `token_age` >7d cliff and `vol_liq_ratio` 5.0× suppress slow-burn memes

ANSEM was 9 days old at the $190K sighting (`token_age` → 0) with a 2.3× vol/liq ratio
(`vol_liq_ratio` 5.0× → 0). Plausible these thresholds penalize accumulation-phase tokens — **but
not proven at scale**; the clean-join denominator (Findings F1) is required before acting.

### D. OPEN business decision — paid holder feed vs free-tier/proxy design

Resolved below.

---

## Costed decision memo — holder data (resolves claim D)

*(Sourced from read-only research, 2026-06-28; code refs verified in-tree, pricing per vendor pages.)*

### Calls/day at current cadence
- `SCAN_INTERVAL_SECONDS=60` (`config.py:33`) → **60 cycles/hr = 1,440 cycles/day.**
- Enrichment call site `main.py:1076` runs over **all** deduped candidates (no top-N cap);
  realistic **~20 Solana tokens/cycle**.
- Helius `getTokenAccounts` = **10 credits/call.**

| Scenario | Calls/day | Helius credits/mo | vs free 1M/mo | Verdict |
|---|---:|---:|---|---|
| 20 tok/cycle | 28,800 | **8.64M** | 8.6× over | ❌ infeasible |
| 5 tok/cycle (pre-filtered) | 7,200 | 2.16M | 2.2× over | ❌ infeasible |
| 1 tok/cycle | 1,440 | 0.43M | within | ✅ but defeats purpose |

### Free-tier feasibility
- **Helius free (1M credits/mo, 2 req/s DAS):** exhausted in **<4 days** at 20 tok/cycle. **Infeasible.**
- **Paid:** Developer **$49/mo / 10M credits** — *borderline* (need 8.64M, no burst headroom);
  Business **$499/mo / 100M** — comfortable.
- **Moralis free (40k CU/day, 40 req/s):** EVM-only path in code; **no confirmed Solana holder
  endpoint**; per-call CU **unverified** (docs paywalled). Existing `_enrich_evm` also has a latent
  bug — `len(holders)` counts only page 1 (`holder_enricher.py:88`). **Not viable as-is.**

### Rate-limit risk
Helius DAS 2 req/s is *not* the binding constraint at 20 tok/cycle spread over 60s; the **credit
budget** is. Moralis Solana support is unconfirmed.

### Fallback behavior when holder data missing
Current: silent zero (`holder_growth` never fires). Target: the **proxy signal** below becomes the
default; a real holder feed, if later funded, runs in parallel and coexists.

### Cheaper proxy — can it cover part of `holder_growth`?
**Yes.** We already fetch DexScreener `txns_h1_buys` / `txns_h1_sells` every cycle
(`models.py:39–40`). A per-cycle snapshot + delta (`txns_growth_1h`) measures **organic buy-side
accumulation** — which *leads* holder count — at **zero new API calls**. GeckoTerminal pool
`transactions.h1.buys` (currently unparsed, `models.py:171`) adds free cross-source corroboration in
~2 lines.

| Proxy | Feasibility | Cost |
|---|---|---|
| DexScreener `txns_h1_buys` delta | ✅ data already collected; copy `volume_snapshots` pattern | **Free** |
| GeckoTerminal `transactions.h1.buys` | ✅ ~2-line parser add | **Free** |
| Birdeye free tier | ❌ holder counts paywalled (403) | paid |
| Solana RPC `getTokenLargestAccounts` | ❌ top-20 only, not a holder count | n/a |

### Memo verdict
**Do NOT fund a paid holder feed now.** Free-tier holder APIs are infeasible at 60 cycles/hr, and a
free DexScreener-buy-txn-growth proxy measures the same underlying phenomenon at higher temporal
resolution with zero marginal cost. Re-evaluate a $49/mo Helius key only **after** the proxy shows
positive expectancy in soak.

---

## Proposed workstreams (priority order) — **none authorized for build here**

> Each ships only after its stated evidence gate. Sequencing matters: a recalibrated gate is the
> prerequisite that makes every other signal change observable.

### Track 3 — Recalibrate the unreachable gate *(foundation; cheapest; highest leverage)*
The gate must reach the realized distribution before any signal work is measurable. Options:
recalibrate `MIN_SCORE` to the corpus's achievable range, and/or fix `SCORER_MAX_RAW=193`
normalization that assumes unreachable signals. **Config + recalibrated tests; soak with alert-rate
cap.** Evidence gate: Findings §6 alert-volume table + a precision floor on the clean-join cohort.

### Track 1 — Un-phantom accumulation via the free proxy *(clean, free, targeted)*
Add `txns_h1_buys_snapshots` + `txns_growth_1h` + `TXNS_GROWTH_THRESHOLD` + `buy_txn_growth` signal
(proxy for the dead `holder_growth`). Paid-feed-compatible: a real Helius feed, if later funded,
coexists. Evidence gate: proxy fires on a known accumulation case (ANSEM 06-17→06-24 backfill) and
shadow-soaks for a baseline fire rate before counting toward score.

### Track 2 — Relax `token_age` cliff + `vol_liq_ratio` threshold *(unproven — backtest first)*
Do NOT touch until Findings **F1** (same-asset clean-join, ≥30-day window) confirms these thresholds
suppress genuine winners with acceptable precision.

### Track 4 — Dedicated slow-burn lane *(product answer — most deferred)*
May become the right answer IF F1 shows the archetype is distinct (week-long accumulation → vertical)
with precision a tuned scorer cannot match. Gated entirely on F1.

---

## Acceptance criteria before ANY implementation (met by the companion findings)

- ✅ Alerts/day a fix would add — known (Findings §6).
- ✅ Which fix would have caught the cohort — known (Findings §7: recalibration + holder repair).
- ✅ Expected false-positive cost — known (~1–2% precision at thresh 50).
- ✅ Holder-data cost/rate decision — resolved (free proxy; no paid feed now).

## Open questions for operator

1. **Track 3 first?** Confirm gate recalibration is the foundation to pursue before signal work.
2. **Proxy vs paid:** endorse the free DexScreener-buy-txn proxy as the holder-growth substitute
   (with paid Helius deferred to a post-soak re-eval)?
3. **F1 scope:** authorize building the clean same-asset join (contract↔coin_id, ≥30-day,
   `score_history`-based) as the next read-only step that unlocks Tracks 2 & 4?
4. **Alert-rate ceiling:** what daily alert volume is acceptable? This bounds how far Track 3 can
   lower the gate (≈16/day catches ANSEM-class at peak 50).
