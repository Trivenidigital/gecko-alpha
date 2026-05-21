**New primitives introduced:** NONE. Design specifies the operator-facing sample packet + the post-sample implementation shape if approved. Implementation primitives (`source_call_price_observations` table, `_fetch_snapshot_rows` extension, etc.) inherited from parent design `tasks/design_source_call_price_coverage_expansion_2026_05_21.md`; this design specifies vendor-specific deltas only.

# Design (v2): BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT

**Plan:** `tasks/plan_source_call_price_coverage_sample_cg_gt_2026_05_21.md` v2
**Status:** DESIGN v2 (post 2-reviewer fold)
**Date:** 2026-05-21

## 0. v1 → v2 fold map (design-stage reviewers)

| Reviewer | Finding | v2 resolution |
|---|---|---|
| A.C1 | `reserve_in_usd` is current-state, NOT historical at call_ts — pool rule unimplementable on 2-call budget | §4 + §5: rule honestly downgraded to "current_reserve_as_proxy" with explicit drift caveat. Schema records `pool_selection_method='current_reserve_proxy_v1'`. Older `call_ts` → wider drift; flagged in `forward_pct_quality` field. |
| A.C2 | `CHAIN_TO_NETWORK` silently drops bsc(19), monad(2), hyperevm(1) — 11% of corpus | §3 + §4: reuse in-tree `_geckoterminal_network_for_chain` (covers chains the existing GT client supports); raise explicitly on unsupported chains (no silent skip). Filed `BL-NEW-GT-CHAIN-MAP-EXTENSION` as upstream follow-up. |
| A.C3 | Must reuse in-tree `_geckoterminal_network_for_chain` not redefine | §3: `from scout.ingestion.geckoterminal import _geckoterminal_network_for_chain` |
| A.I1 | pre-graduation pump.fun tokens are structurally unbackfillable on `/pools` | §9 risk row + sample classification: tokens whose contract ends in `pump` (Solana pump.fun convention) AND return 0 pools → classify `bonding_curve_pre_graduation`, NOT `gt_no_coverage` |
| A.I2 | Pool migration mid-forward-window unaddressed | §4: optional horizon-time pool re-resolution (1 extra `/pools` call at each horizon) — out of sample scope (would exceed 6-call budget); implementation-phase concern. Sample script records `pool_address` + drift caveat. |
| A.I3 | Criterion #6 terminology — `attributes.address` vs `id` (GT relationship-id format `solana_<addr>`) | §3 + §8: explicit "use `attributes.address`, NOT `id`" — cross-referenced to `scout/models.py:177-182` which already strips `solana_` from base_token IDs |
| B.C1 | `.gitignore` doesn't include `tasks/vendor_samples/` — gap shipped if not added IN THIS PR | This PR adds the line to `.gitignore` |
| B.C2 | `git check-ignore` honors uncommitted edits — need two-layer check | §3 docstring: two-layer (committed ignore-rule via `git check-ignore -v` parsed source + `git ls-files --error-unmatch` returns "did not match") |
| B.C3 | `HARD_CALL_CAP` enforcement unpinned | §3: `CallBudget` class with `.spend()` context manager, raises `BudgetExhausted` BEFORE HTTP dispatch, retries count against cap |
| B.I1 | Use `.venv/bin/python` not system `python3` | §3 + §6.5: `/root/gecko-alpha/.venv/bin/python /tmp/sample.py` |
| B.I2 | Per-token call quota (2 max, 0-pool token still consumes 1) | §3: per-token quota documented; 0-pool token consumes 1, OHLCV skipped, sample proceeds to next token |
| B.I3 | Cache wrapper schema | §3.5: explicit wrapper schema with `call_url`, `call_ts_utc`, `response_status`, `rate_limit_headers`, `elapsed_ms`, `response`, `bl`, `criterion_evidence_for` |
| B.I4 | Rate-limit pacing (250ms sleep) | §3: `await asyncio.sleep(0.25)` minimum between calls; recorded in cache wrapper |
| B.I5 | /tmp cleanup on srilu | §6: caller deletes `/tmp/sample.py` and `/tmp/sample.out` after Read step |
| B.I6 | Criterion 7 lookback-failure path commits to behavior change | §8 footnote: failure-of-7 path is *operator-decided in follow-up plan*; this design does NOT pre-commit a config-knob shape |

All Critical findings folded. None require plan-stage rework. Plan v2 stays as-is; design v2 incorporates all folds.

## 1. Scope (unchanged)

1. **Packet phase (this PR):** operator-facing decision packet.
2. **Sample phase (operator-gated):** the script that runs ≤6 GT public API calls if authorized.

Out of scope: any implementation post-sample.

## 2. Packet contents (operator-facing) — unchanged

`tasks/vendor_sample_decision_packet_cg_gt_2026_05_21.md` contains:
- 5 pre-sample operator decisions (plan §10).
- Vendor selection rationale.
- Exact GT endpoint URLs + payload bytes.
- The 3 selected tokens (resolved at packet-write from SQL).
- The 7 acceptance criteria (plan §8) verbatim.
- Trust-tier labeling spec.
- 30m forward-return semantics pre-registration.
- Pool selection rule **with v2 drift caveat**.
- `.gitignore` precondition documentation.
- Two-step SSH execution pattern.
- Explicit "does not call API / does not write prod DB" section.
- Rollback path.

## 3. Sample script shape (post-approval) — v2

```python
"""
GT public API vendor sample for BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT.

Runs ≤6 public-API calls (no API key needed) against api.geckoterminal.com.
Caches responses under tasks/vendor_samples/ with provenance wrapper.
Evaluates the 7 acceptance criteria from the plan. Produces a findings doc.

Calls MUST be sequenced resolve → ohlcv per-token; pool_address from resolve
is the input to ohlcv. Never reverse.

Hard constraints (enforced in code):
- Two-layer .gitignore check at startup:
  (a) `git check-ignore -v tasks/vendor_samples/` returns committed source.
  (b) `git ls-files --error-unmatch tasks/vendor_samples/` exits non-zero
      ("did not match" confirming nothing tracked).
- Refuses to run without --i-have-read-the-packet flag.
- Reads tokens from scout.db via aiosqlite URI mode=ro.
- Never writes to scout.db.
- Records dispatched/delivered structured-log triplet around each call.
- CallBudget(max=6) enforced via context-manager .spend() that raises
  BudgetExhausted BEFORE HTTP dispatch. Retries count against the cap.
- 250ms minimum sleep between calls.
- Per-token quota of 2; 0-pool token consumes 1 and skips OHLCV.

Cache wrapper schema (every call writes one JSON to tasks/vendor_samples/):
  {"call_url": str, "call_method": "GET", "call_ts_utc": ISO8601,
   "response_status": int, "response_headers": {...},
   "rate_limit_headers": {...},        # X-RateLimit-* subset
   "elapsed_ms": int, "response": <raw GT body>,
   "bl": "BL-NEW-SOURCE-CALL-PRICE-COVERAGE-SAMPLE-CG-GT",
   "criterion_evidence_for": [list of criterion-ids this call evidences],
   "prev_call_ts_utc": ISO8601 | null,  # for pacing audit
  }

Execution host: srilu-vps via /root/gecko-alpha/.venv/bin/python
  (NOT system python3 — script imports aiohttp + aiosqlite which the
  system Python lacks).

Mirror conventions from scripts/source_calls_live_writer.py:
  - shebang `#!/usr/bin/env python3`
  - module-level docstring header
  - structlog → stderr (stdout reserved for JSON)
"""

import aiohttp, aiosqlite, asyncio, json, time, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the in-tree chain→network translator (do NOT redefine —
# A.C3 fold).
from scout.ingestion.geckoterminal import _geckoterminal_network_for_chain

GT_BASE = "https://api.geckoterminal.com/api/v2"
HARD_CALL_CAP = 6
PER_TOKEN_QUOTA = 2
RATE_LIMIT_PER_MIN = 30  # documented public-API cap
MIN_PACE_SECONDS = 0.25

class CallBudget:
    """Enforces global cap + per-token quota. Spend() raises BEFORE HTTP."""
    def __init__(self, global_cap, per_token_quota): ...
    def spend(self, token_key: str): ...  # context manager
class BudgetExhausted(Exception): ...

async def precheck_gitignore() -> None:
    """Two-layer check: committed gitignore rule + nothing tracked under path."""
    # git check-ignore -v parses source from committed .gitignore
    # git ls-files --error-unmatch confirms nothing tracked
    ...

async def fetch_pools_for_token(session, network, contract, budget):
    """GET /networks/{network}/tokens/{contract}/pools — 1 of 2 calls per token."""
    ...

async def fetch_ohlcv(session, network, pool_address, from_ts, to_ts, aggregate=5, budget=None):
    """GET /networks/{network}/pools/{pool_address}/ohlcv/minute?aggregate=5 — 1 of 2."""
    ...

def select_tokens_for_sample(db_path: str) -> list[dict]:
    """N=3 tokens: oldest/median/newest dex:* call_ts.
       Read-only sqlite3 URI mode=ro. No side effects."""
    ...

def evaluate_criteria(samples: list[dict]) -> dict:
    """Evaluates 7 acceptance criteria. Returns {criterion_id: pass|fail|n/a, evidence}."""
    ...
```

## 4. Schema commitments (for post-sample implementation, deferred) — v2

Implementation PR adds `source_call_price_observations` with CG/GT-track columns:

| Column | Type | Note |
|---|---|---|
| `source_call_id` | INTEGER NOT NULL | FK to source_calls.id |
| `horizon` | TEXT NOT NULL CHECK (horizon IN ('at_call', '30m', '1h', '6h', '24h')) | Discrete horizons; 30m derived per plan §6.1 |
| `vendor` | TEXT NOT NULL | `geckoterminal_free_public` or `coingecko_onchain_pro` (extensible — future `dexscreener_*` allowed) |
| `pool_address` | TEXT NOT NULL | `attributes.address` from `/pools` response, NOT `id` (A.I3 fold) |
| `reserve_in_usd_observed` | REAL | Pool's reserve at OBSERVATION time (i.e., when this row was written), NOT at call_ts (A.C1 fold) |
| `pool_selection_method` | TEXT NOT NULL CHECK (... IN ('current_reserve_proxy_v1', 'manual_override', 'fallback_only_pool', 'bonding_curve_pre_graduation_unverified')) | The `current_reserve_proxy_v1` value explicitly documents the V1 drift caveat — historical reserves at call_ts are not retrievable via GT public API. `bonding_curve_pre_graduation_unverified` uses the `_unverified` suffix because pump-suffix-with-0-pools is a heuristic, not a confirmed bonding-curve state (PR-reviewer A.I3). |
| `pool_drift_risk_flag` | TEXT NOT NULL DEFAULT 'not_computed' CHECK (... IN ('not_computed', 'low_call_ts_within_30d', 'medium_call_ts_30_90d', 'high_call_ts_older_than_90d', 'pool_migration_suspected')) | A.I2 partial fold — drift band based on call_ts age. Default `not_computed` (NOT `unknown`) so downstream consumers can grep for the script-failed-to-populate case distinctly from a future legitimate band. Band thresholds (`30d`, `90d`) are **placeholder_pending_empirical_calibration** per Reviewer A.C2 — Phase-2 BL will calibrate via observed pool-migration rate sweep on Solana memecoins. |
| `candle_ts_unix` | INTEGER NOT NULL | 5m bucket start, unix seconds |
| `price_close` | REAL NOT NULL | Close of the 5m candle |
| `forward_pct` | REAL | (close@horizon − close@at_call) / close@at_call |
| `forward_pct_quality` | TEXT NOT NULL DEFAULT 'normal' CHECK (... IN ('normal', 'pool_migration_window', 'high_drift_band', 'derived_30m')) | Quality flag for downstream consumers |
| `provider_timestamp_semantics` | TEXT NOT NULL DEFAULT 'unix_seconds_utc' | Inherited from parent design |
| `observed_at` | TEXT NOT NULL DEFAULT (datetime('now')) | When this row was written |

**Pool selection v2 rule (honest):**
- The script CANNOT fetch reserves-at-call_ts from GT public API. Documented limitation.
- V1 uses *current* `reserve_in_usd` from the `/pools` response as a proxy.
- The `pool_drift_risk_flag` band based on `(now - call_ts).days` is the operator-facing drift indicator.
- Pool migrations during the forward-window are NOT detected in V1; flagged as `forward_pct_quality='pool_migration_window'` only if a later sweep detects it (out of sample scope).

Not implemented in this PR.

## 5. Why GT free vs CG Pro for sample (not for implementation) — unchanged
- Sample validates shape; GT free has the same data at zero cost.
- Implementation target is operator cost-governance decision.
- Schema's `vendor` dimension makes the data comparable.

## 6. Rollback — v2

If sample script runs and any criterion fails:
1. No prod DB writes happened (script never opens scout.db in write mode).
2. `tasks/vendor_samples/` is gitignored AND not committed (precheck enforced).
3. Findings doc records pass/fail per criterion.
4. Caller deletes `/tmp/sample.py` and `/tmp/sample.out` on srilu after the Read step (no /tmp residue).
5. Operator decides: retry with adjusted tokens, switch to CG Pro, re-scope, or reject.

No code revert needed.

## 7. Rollback (post-implementation, deferred)
Inherited from parent design.

## 8. §12 compliance audit — v2

| Rule | Compliance |
|---|---|
| §12a | N/A for this PR. **Future impl PR MUST file the freshness-SLO + watchdog as a *blocking* sub-task** for `source_call_price_observations` (B.N4 fold). Not a deferred follow-up. |
| §12b | N/A — no automated state reversal. |
| Resilience-layered-failure | Sample script surfaces failures as criterion failures, not swallow. Explicit in §3 docstring. |
| §9c | Pre-registered pool-selection rule (v2 honest version) + forward_30m_pct semantics prevent the lever-vs-data-path trap. |

## 9. Risks — v2

| Risk | Mitigation |
|---|---|
| GT lookback cap shorter than corpus 7mo | Criterion #7 probes; failure → operator-decided follow-up (§8 footnote), not pre-committed config. |
| Pre-graduation pump.fun memecoins return 0 pools (A.I1) | Sample script classifies `bonding_curve_pre_graduation` if contract suffix is `pump` AND 0 pools; distinct from `gt_no_coverage` |
| 5m candle gaps on low-volume tokens | Sample doesn't validate corpus-wide gap rate; impl-phase gate |
| Pool migration mid-forward-window (A.I2) | V1 acknowledged limitation. `pool_drift_risk_flag` band + `forward_pct_quality` flag in schema. Future BL filed for active migration detection. |
| `reserve_in_usd` proxy drift (A.C1) | V1 honest framing: `pool_selection_method='current_reserve_proxy_v1'`. `pool_drift_risk_flag` bands operator visibility. |
| Chain map drift if in-tree mapper changes (A.C3) | Reuse `_geckoterminal_network_for_chain`, don't duplicate. |
| Unsupported chains silently skipped (A.C2) | Sample raises explicitly on unsupported chain. `BL-NEW-GT-CHAIN-MAP-EXTENSION` filed. |
| GT beta-status breaking changes | Future cron smoke-test for shape drift. Out of sample scope. |
| Cache directory accidentally committed | Two-layer .gitignore precheck (B.C2). Script refuses to run if path tracked. Plus `.gitignore` updated IN THIS PR (B.C1). |
| Operator approves sample, sample passes, impl is expensive | Impl effort scoped in parent design. |
| HARD_CALL_CAP bypassed by retry logic | `CallBudget.spend()` raises BEFORE HTTP dispatch; retries count (B.C3). |

## 10. Out-of-scope confirmations
- No code change in this PR (plan + design + packet + .gitignore line + backlog/todo).
- No vendor calls.
- No prod DB writes.
- No schema migration.
- No dashboard surface change.
- No source ranking / pruning.
- No actionability consumption.
- No `.env` edits.

## 11. Implementation order (post-sample-approval, deferred)

1. Run sample script (≤6 GT public API calls).
2. Evaluate criteria; produce findings doc.
3. If passed: design implementation PR (separate session) — must include §12a freshness-SLO + watchdog as blocking sub-task per B.N4 fold.
4. If failed criterion 7: operator-decided follow-up (not pre-committed).
5. If failed criteria 1-6: re-park or shift vendor track.

This design does NOT authorize any of the above.
