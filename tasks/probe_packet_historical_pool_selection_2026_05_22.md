# Probe Packet — Historical Pool Selection (BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE)

**Date:** 2026-05-22
**Parent BL:** `BL-NEW-SOURCE-CALL-HISTORICAL-POOL-SELECTION-PROBE`
**Filed:** PR #224 (merged `15b8e30`) §11.5 + backlog entry under "Active Findings."
**Operator authorization status:** **AUTHORIZED 2026-05-22** with explicit budget per operator's directive — max 10 GT free calls, findings-only, no prod DB writes, target 2-3 old tokens, inspect multiple pools per token.

## 1. Question

For old `source_calls` rows where `current_reserve_proxy_v1` (today's top-reserve pool) returns HTTP 401 / no OHLCV at `call_ts`: does **any** pool in the token's `/pools` response have OHLCV covering `call_ts`?

If YES → V1 pool-selection rule is the blocker, not GT. New rule (e.g., "first pool returned that has OHLCV at call_ts") can rescue old rows.
If NO → GT structurally can't cover the old corpus regardless of pool choice. Falls back to Path 2 (CG Pro) or Path 3 (forward-only).

## 2. Budget (operator-fixed)

| Item | Limit |
|---|---|
| Total GT free calls | **≤ 10** (hard cap) |
| Token targets | **2-3** old `source_calls` rows |
| Prod DB writes | **0** (URI mode=ro only) |
| Pools inspected per token | bounded by per-token allocation (see §4) |
| Vendor API keys | **none required** (GT public API) |
| Paid API calls | **0** |

## 3. Target tokens (selected from prod, Solana-only)

Resolved at packet-write from `SELECT id, call_ts, token_id FROM source_calls WHERE token_id LIKE 'dex:solana:%' AND token_id NOT LIKE '%pump' ORDER BY call_ts ASC LIMIT 5`. Solana-only because BSC/monad/hyperevm are not yet in `_geckoterminal_network_for_chain` (per `BL-NEW-GT-CHAIN-MAP-EXTENSION`); restricting scope keeps the probe focused on the pool-selection question, not the chain-coverage question.

| Token | call_ts | source_calls.id | Contract |
|---|---|---|---|
| A | 2025-10-20T19:22:52+00:00 | 157 | `Ciphern9cCXtms66s8Mm6wCFC27b2JProRQLYmiLMH3N` (same as failed oldest in PR #223 sample) |
| B | 2026-01-06T16:52:02+00:00 | 128 | `3wh5zc1BXzwLaiq4g71q69pM6mpHGo8YYicfma71txu3` (~4-month-old, intermediate) |
| C | 2026-02-09T00:23:51+00:00 | 292 | `J3DpHpw8yT5x1cjwjsJyfas3hXrbjC5FXzQYcfK2rJ8R` (~3-month-old) |

Token A is the cornerstone — if any pool for CIPHER has OHLCV at `call_ts = 2025-10-20`, V1 rule needs replacement.

## 4. Call allocation (≤ 10 total)

For each token: 1 `/pools` call (returns up to N pools, often 6-20) + up to 2-3 `/ohlcv` probes against different pools from that list.

Plan:
- Token A: 1 pools + 3 ohlcv = **4 calls** (most important; CIPHER's pools list returned 20 entries in the original sample)
- Token B: 1 pools + 2 ohlcv = **3 calls**
- Token C: 1 pools + 2 ohlcv = **3 calls**

**Total: 10 calls.** Hard cap.

Pool selection within each token: oldest by `pool_created_at` if GT exposes it (likely), else first 2-3 in the `/pools` response after the top-reserve. The current-reserve top pool is intentionally NOT probed — that's the V1 rule that already failed; we're testing whether OTHER pools work.

## 5. Exact call shape

### 5.1 `/pools` resolve
```
GET https://api.geckoterminal.com/api/v2/networks/solana/tokens/{contract}/pools
```
No auth. Record full response under `tasks/vendor_samples/historical_pool_selection_{token_id}_pools.json` (gitignored).

### 5.2 `/ohlcv` probe per pool
```
GET https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_address}/ohlcv/minute?aggregate=5&before_timestamp={call_ts_unix + 1800}&limit=10
```
- `before_timestamp = call_ts_unix + 1800` — window centered on call_ts + 30m so we see whether the pool had a candle at the call moment.
- `limit=10` — just need to see if data exists; not a full backfill.

Record each response under `tasks/vendor_samples/historical_pool_selection_{token_id}_pool_{pool_address[:8]}_ohlcv.json`.

## 6. Pool selection within `/pools` response

The probe script:
1. Reads `/pools` response.
2. Sorts pools by `attributes.pool_created_at` ASCENDING (oldest first) IF the field exists. Else sorts by `attributes.reserve_in_usd` ASCENDING (smaller pools earlier — proxy for "older / pre-migration").
3. Skips the top-reserve pool (already known-failed via V1 rule).
4. Probes the next 2-3 pools in the sorted list.
5. Records which (if any) returned non-empty OHLCV at `call_ts ± 30m`.

## 7. Accept / output criteria

This is findings-only — no pass/fail gate. The probe produces evidence:

| Outcome | Implication |
|---|---|
| ≥1 pool returns OHLCV at call_ts for Token A (CIPHER) | V1 pool-selection rule replaceable. GT free remains viable. Next deliverable: design replacement rule (probably "first pool with OHLCV coverage at call_ts," possibly with `pool_created_at` precedence). |
| 0/3 pools return OHLCV for Token A but ≥1 returns for Token B or C | Mixed — older rows (~7mo) may truly be uncovered, but newer-ish "old" rows (~4mo) are covered via better pool selection. Forward-only path can be relaxed: pre-call_ts < ~4mo is recoverable. |
| 0 pools return OHLCV across all 3 tokens | Even best-pool selection can't rescue. GT structurally inadequate for the old corpus. Fall back to Path 2 (CG Pro) or Path 3 (forward-only). |

## 8. Operational mechanics

### 8.1 Execution host
srilu-vps via `/root/gecko-alpha/.venv/bin/python /tmp/probe.py` (mirrors the GT sample script convention). Reuses `from scout.ingestion.geckoterminal import _geckoterminal_network_for_chain`.

### 8.2 Two-step SSH pattern (Windows operator)
```
# Step 1
ssh srilu-vps '/root/gecko-alpha/.venv/bin/python /tmp/probe.py --i-have-read-the-packet > /tmp/probe.out 2>&1' > .ssh_tmp/probe.txt 2>&1

# Step 2
Read .ssh_tmp/probe.txt
```

### 8.3 Cache directory precondition
Sample script must enforce two-layer `.gitignore` check (per packet PR #222 §3 design) — `tasks/vendor_samples/` is already gitignored from PR #222.

### 8.4 Post-run cleanup
`ssh srilu-vps 'rm -f /tmp/probe.py /tmp/probe.out'`

## 9. What this packet does NOT do

- Does not call any vendor API.
- Does not write to prod DB.
- Does not commit any vendor response / secret to git.
- Does not edit `.env`.
- Does not start implementation. The probe is **findings-only**; implementation remains gated.

## 10. What changes if the probe runs and returns YES

A follow-up docs PR records the findings + a v2 pool-selection rule design (replacement for `current_reserve_proxy_v1`). Implementation remains gated until operator authorizes the schema/backfill work.

## 11. What changes if the probe returns NO

Operator picks Path 2 (CG Pro packet) or Path 3 (forward-only plan). The GT track is closed for the old corpus, but GT free may still serve forward-only coverage (Path 3).

## 12. Rollback

Findings-only. Nothing was deployed. Cached JSON files are gitignored (`tasks/vendor_samples/`). `/tmp` cleaned post-run. Zero blast-radius.

## 13. Sign-off

Operator authorization recorded in conversation 2026-05-22:
> "I'd authorize it with a tight budget: max 10 GT free calls, findings-only, no prod DB writes, target 2-3 old tokens, for each token, inspect multiple pools and determine if any pool has OHLCV covering call_ts, output whether 'historical pool selection' can rescue old rows."

Packet maps each constraint to a section:
- max 10 calls → §2 + §4
- findings-only → §7 + §10 + §11
- no prod DB writes → §2 + §8 + §12
- target 2-3 old tokens → §3
- inspect multiple pools per token → §4 + §6
- output rescue/no-rescue decision → §7 (three outcome rows)
