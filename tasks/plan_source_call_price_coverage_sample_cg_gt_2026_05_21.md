**New primitives introduced:** NONE. Plan + design + sample decision packet only. No code, no schema, no vendor calls, no prod DB writes. Implementation gated on operator approval of packet's sample call.

# Plan (v2): BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT

**Branch:** `feat/source-call-price-coverage-sample-cg-gt`
**Date:** 2026-05-21
**Status:** PLAN v2 (post 2-reviewer fold)
**Parent BL:** `BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION` (DESIGN-SHIPPED, PR #208) → vendor track shifted from GoldRush (daily-only, PR #220 finding) to CoinGecko MCP / GeckoTerminal.

## 0. v1 → v2 fold map

| Reviewer | Finding | v2 resolution |
|---|---|---|
| A.C1 | Synthetic 30m candle conflates aggregate with return | §6.1 explicit pre-registration: forward_30m_pct = close-at-bucket return, NOT OHLCV composite |
| A.C2 | "Which pool is the price" unaddressed (Solana memecoins have 3-10 pools) | §6.2 deterministic rule: highest `reserve_in_usd` at call_ts, tiebreak lexical pool address |
| A.C3 | Lookback window not probed — POPCAT is too recent | §6.3 sample targets OLDEST `dex:*` row (verified: 2025-10-20, ~7mo old) + median + newest = N=3 tokens |
| A.I1 | n=1 sample can't validate 5m corpus-wide gap robustness | §8 acceptance criterion #4 re-scoped to "5m queryable for sampled token-day"; corpus gap-rate is impl-phase gate, not sample-phase |
| A.I2 | call_ts storage format unverified | Verified 2026-05-21: ISO-8601 with explicit `+00:00`, 0 non-conformant rows. No tz risk. Documented §6.3. |
| A.I3 | No stopping rule on "try a different token" | §8 stopping rule: 3 tokens (oldest/median/newest), reject GT if 0/3 resolve to pools; record attenuation factor if 1-2/3 |
| A.I4 | ~15% ceiling vs design's coverage ≥ 0.50 gate | §4c recommendation: ACCEPT 202-row V1 ceiling + file `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` upstream BL |
| B.C1 | Srilu Hermes skill surface unchecked | Verified 2026-05-21: 20+ skills installed on srilu, NONE match OHLCV/historical-price. Documented §3 + cited backlog line 660. |
| B.C2 | MCP-vs-Hermes-skill distinction unclear | §3 CG-MCP row rewritten: "MCP protocol surface, not Hermes-managed skill; §7b lever is skill hub + awesome-hermes-agent, both checked." |
| B.C3 | Criterion #5 over-claims (2 calls can't validate cap) | §8 criterion #5 rewritten: rate-limit signal *observable* (headers reported), not capacity floor |
| B.C4 | Criterion #4 false-positive trap | Same as A.I1 — re-scoped above |
| B.I5 | §10 Q1 needs pre-sample operator framing | §10 reframed as operator-decision flags, not open questions |
| B.I6 | §10 Q2 needs recommendation | §10 + §4c: explicit ACCEPT-202-ceiling recommendation |
| B.I7 | Windows SSH stdout constraint | §6.5 explicit two-step SSH pattern documented |

All Critical + Important findings folded. None require rework of plan structure.

## 1. Problem (lever-vs-data-path framing, CLAUDE.md §9c) — unchanged from v1

`source_calls` is SHIPPED-DEPLOYED but PRICE-COVERAGE-LIMITED: 1323 rows, 14 with `price_at_call`, **zero** with forward_30m/1h/6h/24h. PR #208 designed 30m as primary horizon; PR #220 found GoldRush daily-only; operator leans path C (CG/GT). This plan evaluates path C and produces a docs-only sample packet.

## 2. Drift-check (§7a) — unchanged

- In-tree: `scout/ingestion/geckoterminal.py` uses `/trending_pools` only. No OHLCV / historical calls anywhere.
- No intraday/OHLCV/candle table in scout.db (only `price_cache`).
- Backlog: parent BL is design-shipped; no overlapping BL for CG/GT sample track.
- Master-merge: branched off `5ed9bdb` (PR #220 merged + deployed).

## 3. Hermes-first analysis (v2 — folded)

**Srilu Hermes skill scan (2026-05-21):** `ls /home/gecko-agent/.hermes/skills/` returns 20+ installed skills. The crypto-relevant ones (`crypto_narrative_scanner`, `coin_resolver`, `kol_watcher`, `narrative_alert_dispatcher`) are project-owned gecko-alpha skills, none expose OHLCV/historical-price. Generic skills (`inference-sh`, `mcp`, `data-science`, `mlops`) are not data-retrieval. **Negative confirmed** — backlog line 660 already documented this; cross-cite preserved.

| Candidate | Historical/intraday? | Timestamp semantics | Chain identity | Trust tier | Verdict |
|---|---|---|---|---|---|
| **CoinGecko MCP server** | YES — `/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}` (minute 1/5/15; hour 1/4/12; day 1). No native 30m. | unix epoch seconds | pool address (chain in path) | HIGH (CG Pro, paid Analyst+) | Reject as first sample (paid). NOTE: CG MCP is an **MCP protocol surface**, not a Hermes-managed skill — the §7b Hermes-first lever (skill hub + awesome-hermes-agent) is checked separately and returns negative. |
| **GeckoTerminal public API** | YES — same path/shape; CG's `/onchain` endpoints are GT's data per CG MCP docs | unix epoch seconds | pool address | MEDIUM-HIGH (same data, no SLA, beta-status) | **Recommended first sample.** No API key, 30 req/min. |
| **In-tree extension of `scout/ingestion/geckoterminal.py`** | n/a today | n/a | n/a | n/a | Implementation path post-sample-approval. Reuses `_get_json` + `GECKO_BASE` + chain-network map. |
| **Hermes skill hub** | none found | n/a | n/a | n/a | KEEP_CUSTOM |
| **awesome-hermes-agent ecosystem** | topic empty | n/a | n/a | n/a | KEEP_CUSTOM |
| **Srilu-installed skills** | none match OHLCV (20+ skills inspected, all project/generic) | n/a | n/a | n/a | KEEP_CUSTOM (backlog line 660 already documents) |

## 4. The four critical findings the packet must surface — unchanged + extended

### 4a. 30m is NOT a native candle interval — DERIVE
Native: minute 1/5/15, hour 1/4/12, day 1. No 30m. Design's forward_30m_pct is a **point-in-time return**, not a candle composite:
```
forward_30m_pct = (close_at_5m_bucket(call_ts + 30m) - close_at_5m_bucket(call_ts)) / close_at_5m_bucket(call_ts)
```
This is the explicit pre-registration. NEVER compute as 6×5m OHLCV composite. (Reviewer A.C1)

### 4b. Pool selection rule — DETERMINISTIC
Same token can have 3-10 pools on Solana (Raydium v4, Raydium CLMM, Orca Whirlpool, Meteora DLMM, pump.fun). Inter-pool divergence can hit 5-50% at 30m post-call. Rule:
```
pool = argmax(reserve_in_usd at call_ts)
       tiebreak: lexical pool_address
```
NOT 24h volume (lookahead bias toward winners). Recorded in source_call's price_observation row alongside `pool_address` + `reserve_in_usd_at_call`. (Reviewer A.C2)

### 4c. ~85% structurally unbackfillable — ACCEPT 202-ROW V1 CEILING
Recommendation (no longer open question per Reviewer B.I6):
- ACCEPT the 202-row eligible ceiling as V1 scope.
- File `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` as the upstream BL that unlocks the ~85% (NULL, "(unresolved)", non-dex coin_ids).
- Dashboard's `not_rankable_label` may not flip from "no sources rankable" until identity is solved AND 202 rows accumulate enough coverage. That's honest; the gate stays as-is.

### 4d. Trust tier downgrade GT vs CG Pro
Same data, different SLA. Record `vendor=geckoterminal_free_public` distinct from `vendor=coingecko_onchain_pro`. Operator can later authorize Pro upgrade without re-running validation if the schema commits identical shape.

## 5. Proposed deliverable shape

Single docs PR:
- `tasks/plan_source_call_price_coverage_sample_cg_gt_2026_05_21.md` (this v2)
- `tasks/design_source_call_price_coverage_sample_cg_gt_2026_05_21.md`
- `tasks/vendor_sample_decision_packet_cg_gt_2026_05_21.md` (operator-facing)
- `backlog.md` entry for the new BL
- `backlog.md` entry for `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` (filed but PROPOSED)
- `tasks/todo.md` session record

No code. No vendor calls. No schema. No prod DB writes.

## 6. Sample call shape (v2 — folded)

### 6.1 forward_30m_pct semantics (pre-registration)
```
forward_30m_pct = (close_at_5m_bucket(call_ts + 30m) - close_at_5m_bucket(call_ts)) / close_at_5m_bucket(call_ts)
```
Both prices come from the 5m candle whose start ≤ instant < end. No OHLCV composite. Same rule for 1h (12 buckets), 6h (72), 24h (288).

### 6.2 Pool selection (pre-registration)
For each `source_calls` token, fetch GT `/networks/{network}/tokens/{address}/pools`, sort by `reserve_in_usd` descending, tiebreak on `pool_address` lexical. Use the top pool. Record selection metadata.

### 6.3 Token selection — N=3 tokens spanning lookback range

| Position | Selection criterion | Purpose |
|---|---|---|
| 1 | Oldest `dex:*` `source_calls` row (verified 2025-10-20, ~7mo old) | Test lookback-window boundary |
| 2 | Median `dex:*` `source_calls` row (~2026-02-15) | Confirm mid-range works |
| 3 | Newest `dex:*` `source_calls` row (verified 2026-05-21) | Control / fresh-token sanity |

Each token: 1 pool-resolve call + 1 OHLCV fetch = **6 GT public API calls total**. Well under 30 req/min and within the parent design's sample-budget discipline.

### 6.4 Storage
Cache responses under `tasks/vendor_samples/gt_<token>_<call_ts_date>.json`. **Precondition:** confirm `.gitignore` includes `tasks/vendor_samples/` before any call (the script must fail if the path is git-tracked).

### 6.5 Execution host + Windows SSH constraint
Sample runs on srilu-vps (where the .venv + the existing GT client live). Per global CLAUDE.md two-step pattern:
```
# Step 1: SSH redirect to file
ssh srilu-vps 'python3 /tmp/sample.py <args> > /tmp/sample.out 2>&1' > .ssh_tmp/sample.txt 2>&1

# Step 2: Read .ssh_tmp/sample.txt locally
```

No `2>&1`-then-read-from-stdout patterns. Sample script's output is the cached JSON + a structured-log summary.

## 7. Test plan

**Plan phase (this PR):** none — docs only.
**Design phase:** none — docs only.
**Sample-execution phase (operator-gated):** evaluate the 7 acceptance criteria in §8.

## 8. Acceptance criteria (v2 — folded)

The sample is ACCEPTED only if all 7 hold:

1. **Pool resolution works.** For ≥1 of 3 sampled tokens, GT returns ≥1 pool. (If 0/3 resolve, reject GT entirely — record the attenuation factor and consider CG Pro or identity-resolution first.)
2. **OHLCV endpoint returns data.** Each token-day fetch returns 2xx with `ohlcv_list` (or equivalent) ≥ 1 candle.
3. **Timestamps are unix-epoch seconds.** First element of each candle = 10-digit unix timestamp. Not 13-digit ms. Not ISO. (Reviewer A.C1)
4. **5m aggregate path returns ≥ 1 candle for at least one sampled token-day.** Corpus-wide 5m gap rate is an **implementation-phase gate**, not a sample-phase gate. (Reviewer A.I1 / B.C4 re-scope.)
5. **Rate-limit signal is observable.** Either response headers (`X-RateLimit-*`) OR the public docs' 30 req/min cap can be honored by clock-based count. NOT a capacity-floor measurement. (Reviewer B.C3)
6. **Pool address round-trips.** The pool address returned by step 1 is the same address used in step 2 — not an opaque internal ID.
7. **Lookback covers the oldest sampled call_ts.** The 2025-10-20 oldest-token-day fetch returns non-empty `ohlcv_list`. If empty, GT's lookback cap is shorter than gecko-alpha's source_call history; need to bound the implementation's eligible-row set to "call_ts within last 6 months" or similar. (Reviewer A.C3)

If criterion 7 fails: NOT a hard reject — record the GT lookback cap empirically, narrow the eligible-row set, recompute the coverage ceiling, and re-decide.

## 9. Hard constraints (mirror operator's task list)

- No `_fetch_snapshot_rows` change.
- No schema migration.
- No dashboard ranking surface.
- No KOL/source ranking.
- No source pruning.
- No actionability consumption.
- No paid API calls without operator approval (this plan does not authorize any).
- No prod DB writes.
- No secret values committed (GT requires no API key; discipline preserved anyway).
- No live config changes.

## 10. Pre-sample operator decisions (v2 — reframed from open questions)

Operator must confirm before any sample call fires:

| # | Decision |
|---|---|
| 1 | **Vendor target for implementation (if sample passes):** GT free (no SLA, beta) OR CG Pro (paid Analyst+, SLA). If CG Pro, budget BEFORE sample run, not after. |
| 2 | **202-row V1 ceiling:** ACCEPT and file `BL-NEW-SOURCE-CALL-IDENTITY-RESOLUTION` as upstream BL? OR reject this BL until identity is solved first? |
| 3 | **30m derivation policy:** confirm "close-at-5m-bucket return" pre-registration per §6.1 — NEVER OHLCV composite. |
| 4 | **Pool selection rule:** confirm "highest reserve_in_usd at call_ts, lexical tiebreak" per §6.2 — NOT 24h volume. |
| 5 | **Sample call budget:** authorize exactly 6 GT public API calls (N=3 tokens × 2 calls each) under §6.3, OR scope down to 1 token (oldest only) at 2 calls? |

Sample script does not run until operator answers each.
