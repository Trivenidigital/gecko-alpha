# BL-052 — GeckoTerminal Per-Chain Trending Signal

**Status:** Draft → for parallel review
**Date:** 2026-04-20
**Author:** autonomous loop (Claude)
**Upstream:** backlog.md BL-052 — "GeckoTerminal per-chain trending poller → rotation signal"
**Base branch:** `master` (unrelated to open PR #34 BL-051)

---

## 1. Goal

Promote the existing GeckoTerminal per-chain `trending_pools` endpoint from a pure candidate source into a **scoring signal**. When a candidate token (from any ingestion source — DexScreener, CoinGecko, or GeckoTerminal itself) appears at a high rank within a chain's trending pools, award bonus points for cross-source trending confirmation.

This mirrors how `cg_trending_rank` already works for CoinGecko's global trending endpoint — but at the **DEX pool level, per chain**, which catches different tokens because DEX trending is driven by raw trade activity rather than search/watchlist volume.

## 2. Non-Goals

- **No new endpoint.** We reuse `/networks/{chain}/trending_pools`, already called by `scout/ingestion/geckoterminal.py:fetch_trending_pools`.
- **No scope creep into `/new_pools`** or other GeckoTerminal endpoints — that is a separate backlog item.
- **No change to aggregator semantics** beyond preserving one additional enrichment field.
- **No removal of existing behavior.** `fetch_trending_pools` continues to emit candidates; we only enrich them with rank data that downstream stages can use.

## 3. Design Decision (the "why")

**Observed gap.** The existing `fetch_trending_pools` pushes every returned pool into the candidate list but throws away positional information. A token at position 1 (most-traded right now on Solana) is indistinguishable in scorer inputs from a token at position 20 (also trending but weakly). The scorer currently gives neither a bonus.

**Observed opportunity.** If the same contract shows up in both DexScreener (high `boost_total_amount`) and GeckoTerminal (rank 1 in solana trending pools), that is multi-source confirmation — exactly the confluence the existing `CO_OCCURRENCE_MULTIPLIER` rewards. Without a rank-derived signal, the scorer can't see the GT side of that confluence.

**Why a decorator pattern, not a new poller.** BL-051 needed a separate poller because DexScreener `/token-boosts/top` is a distinct endpoint. BL-052 needs no new network calls — the rank is already in the trending_pools response as the array index. We just capture it during parsing and plumb it through.

## 4. Architecture

```
[fetch_trending_pools]           (existing — modified to capture rank)
        |  idx 0..N → CandidateToken.gt_trending_rank = idx+1
        v
[aggregate]                       (existing — modified to PRESERVE gt_trending_rank)
        |
        v
[scorer.score]                    (existing — modified to award gt_trending signal)
        |  if gt_trending_rank ∈ [1, GT_TRENDING_TOP_N]: +15 points
        v
[downstream stages]               (unchanged)
```

Data flow is a single-field decoration. No new modules. No new HTTP calls. No new DB schema.

## 5. New CandidateToken field

File: `scout/models.py`

```python
# Populated by GeckoTerminal trending_pools parser (BL-052).
# 1-based rank within the emitting chain's trending_pools list
# (position 1 = most-traded). None if the token was not sourced from
# GT trending or the rank info was unavailable.
gt_trending_rank: int | None = None
```

Placement: co-located with the existing `cg_trending_rank` field so readers see both trending fields together.

## 6. Parser change — `fetch_trending_pools`

File: `scout/ingestion/geckoterminal.py`

Replace the current inner loop:

```python
for pool in data.get("data", []):
    try:
        token = CandidateToken.from_geckoterminal(pool, chain=chain)
        ...
```

with:

```python
for idx, pool in enumerate(data.get("data", [])):
    try:
        token = CandidateToken.from_geckoterminal(pool, chain=chain)
        token = token.model_copy(update={"gt_trending_rank": idx + 1})
        ...
```

**Why `model_copy` over passing rank into `from_geckoterminal`?** The classmethod is already used in three places and changing its signature ripples into existing tests. Preferring a post-construction set keeps the classmethod's existing contract stable.

**Semantics:** `idx + 1` yields a 1-based rank so that "rank 1" reads naturally as "top of the trending list". This matches the existing `cg_trending_rank` convention (`cg_trending_rank <= 10` in scorer.py:143).

**GT API ordering contract:** GeckoTerminal's `/networks/{chain}/trending_pools` endpoint returns pools ordered by trading activity (descending 24h volume / trade count — stable since the v2 API launch, documented at https://www.geckoterminal.com/dex-api). The entire validity of the `gt_trending` signal rides on this ordering. Implementation MUST include a one-line comment near `enumerate` pointing at this assumption so future maintainers see it: `# NB: GT returns trending_pools in rank order; idx 0 = most-traded.`. If the ordering changes silently, the signal degrades to "token appears anywhere in top-N pools" which is weaker but still non-destructive.

**Edge cases:**
- Empty `data` list → no candidates emitted, no rank writes. Unchanged behavior.
- Malformed pool entry → existing `except Exception` catches and continues. Unchanged behavior.
- Multiple chains in `settings.CHAINS` → each chain produces its own rank sequence (chain-local ranks). Solana pool ranked 1 and Base pool ranked 1 are both valid; aggregator downstream deduplicates on contract_address only, so two different contracts each at rank 1 is fine.

## 7. Aggregator change — preserve enrichment field

File: `scout/aggregator.py`

Add `gt_trending_rank` to `_PRESERVE_FIELDS`:

```python
_PRESERVE_FIELDS = [
    "cg_trending_rank",
    "gt_trending_rank",      # NEW (BL-052)
    "price_change_1h",
    "price_change_24h",
    "vol_7d_avg",
    "txns_h1_buys",
    "txns_h1_sells",
]
```

**Semantics:** Preserve triggers only when the **new** arrival has `None` and the **old** has a value (see `aggregator.py:37` — `if new_val is None and old_val is not None`). This covers both arrival orders correctly:
- **GT first, DEX second:** old rank=3, new rank=None → preserve condition fires → rank=3 retained on merged token.
- **DEX first, GT second:** old rank=None, new rank=3 → preserve condition does NOT fire (new is non-None) → new token wins trivially, rank=3 retained.

Either way, rank=3 survives. Matches how `cg_trending_rank` is already protected.

**`_PRESERVE_FIELDS` contract (documented explicitly in this PR):** Add a one-line comment above the list: `# Preserve first non-None value on merge. Changing this semantics breaks all rank and enrichment signals.` This hardens an existing implicit contract.

**Join key:** `contract_address` — existing aggregator behavior, no change.

**What if BOTH entries have a non-None rank?** (e.g. two GT calls for different chains both returning the same contract — rare but possible via wrapped tokens.) Last-write-wins on the non-None value. Acceptable — losing a tie on the second rank is not a correctness issue because both values are in the top-N range by construction.

## 8. Scorer change — new `gt_trending` signal

File: `scout/scorer.py`

**Location:** New signal block, inserted after Signal 9 (cg_trending_rank) and before Signal 10 (solana_bonus). Renumber trailing signals 10→11 and 11→12.

```python
# Signal 10: GeckoTerminal per-chain trending rank -- 15 points (BL-052)
if (
    token.gt_trending_rank is not None
    and token.gt_trending_rank <= settings.GT_TRENDING_TOP_N
):
    points += 15
    signals.append("gt_trending")
    logger.info(
        "gt_trending_signal_fired",
        token=token.ticker,
        contract_address=token.contract_address,
        chain=token.chain,
        gt_trending_rank=token.gt_trending_rank,
    )
```

**Points chosen: 15.** Mirrors `cg_trending_rank` exactly. Rationale: both signals answer "is this token trending on a major aggregator right now", just at different granularities (GT is per-chain-DEX, CG is global-aggregator). Equal weight, co-occurrence multiplier will naturally amplify the two-signal confluence.

**Threshold: top 10.** GeckoTerminal returns ~20 trending pools per chain; top 10 restricts the bonus to the upper half — strong trending, not marginal. User-tunable via `GT_TRENDING_TOP_N`.

**SCORER_MAX_RAW bump:** 183 → 198.

**Logger import:** `scorer.py` on master currently has no logger. This spec adds `import structlog` and `logger = structlog.get_logger(__name__)` at module top, mirroring `scout/main.py` style.

**Merge-ordering instruction for the implementer:** BL-051 (PR #34, branch `feat/bl-051-dexscreener-boosts-poller`) also adds `import structlog` and `logger = structlog.get_logger(__name__)` to `scorer.py`. At branch-cut time for BL-052:
- **If PR #34 is already merged into master** → `scorer.py` already has both lines; omit them from BL-052's diff to avoid duplicate imports. Verify with `grep -n "import structlog\\|logger = structlog" scout/scorer.py` on master before editing.
- **If PR #34 is NOT yet merged** → add both lines as described. When PR #34 merges later, expect a trivial conflict on these two lines; resolver keeps one copy.

Also: if PR #34 merges first, `SCORER_MAX_RAW` on master will be 203 (not 183), and BL-052's bump target becomes 203→218 (not 183→198). The plan should read `SCORER_MAX_RAW` on the current master at branch-cut time and compute the target as `current + 15`. The pin test should assert the computed target, not a hardcoded literal.

## 9. Settings additions

File: `scout/config.py`

```python
# -------- GeckoTerminal Per-Chain Trending (BL-052) --------
GT_TRENDING_TOP_N: int = 10
```

File: `.env.example`

**Placement:** `.env.example` has NO existing GeckoTerminal section (verified — GT is unauthenticated and needed no prior config entries). Do NOT append at the end (that lands inside the paper-trading block). Create a new section placed just before the `# === Paper Trading Engine ===` header (or equivalent last section):

```bash
# -------- GeckoTerminal Per-Chain Trending (BL-052) --------
# Rank threshold for the gt_trending scoring signal (+15 pts).
# Lower = stricter; default 10 = top half of GT's ~20-per-chain list.
GT_TRENDING_TOP_N=10
```

No new API keys or secrets — GT is unauthenticated.

## 10. Observability

Two log events:
- **Existing:** `GeckoTerminal returned error` / `GeckoTerminal request error` / `Failed to parse GeckoTerminal pool` — unchanged.
- **New:** `gt_trending_signal_fired` — structured log, one per candidate whose signal fires. Fields: `token`, `contract_address`, `chain`, `gt_trending_rank`.

No metrics endpoint; structlog JSON is sufficient for the existing ops dashboard.

## 11. Test plan

New file: `tests/test_geckoterminal_rank.py`
- Mocks GeckoTerminal response with 5 pools. Asserts emitted candidates have `gt_trending_rank` == [1,2,3,4,5] in order.
- Mocks empty `data` list → zero candidates emitted, no error.
- Mocks malformed pool entry at idx 2 → candidates 1,2,4,5 emitted with ranks 1,2,**4,5** (the failing index is skipped, ranks remain positional — not compacted). This is the correct semantics because rank reflects GT's own ordering, not our parse success rate.

New file: `tests/test_aggregator_gt_rank.py`
- DexScreener emits token X (rank=None), GT emits same token X (rank=3). After aggregate, merged token has rank=3.
- Order-independent: test both arrival orders.

New file: `tests/test_scorer_gt_trending.py`
- Token with `gt_trending_rank=1` + no other signals → `gt_trending` in `signals_fired`, 15 raw pts.
- Token with `gt_trending_rank=11` + default `GT_TRENDING_TOP_N=10` → signal NOT fired.
- Token with `gt_trending_rank=None` → signal NOT fired.
- With `GT_TRENDING_TOP_N=3`, rank=3 fires, rank=4 does not (off-by-one boundary check).
- `capture_logs()` assertion: `gt_trending_signal_fired` event emitted exactly once with expected fields when signal fires, not emitted when below threshold.

New file: `tests/test_scorer_max_raw_bumped_gt.py`
- Asserts `SCORER_MAX_RAW == 198`. Pins the invariant.

Appends to existing test files:
- `tests/test_models.py` (or equivalent) — add a test asserting `gt_trending_rank` default is `None`.
- `tests/test_config.py` — add `test_gt_trending_top_n_default` → default 10.
- **Existing golden-value tests touching `SCORER_MAX_RAW`-dependent normalizations** (there were 2 such test files in BL-051 — list the exact files during plan phase; same treatment: supply `gt_trending_rank=1` to setups that previously asserted "raw = 183 cap-at-100").

Integration test (append to `tests/test_main.py` or new `tests/test_main_pipeline_gt_trending.py`):
- Mock via `aioresponses` at the HTTP layer — NO changes to `run_cycle` signature or any injectable-fetcher plumbing. The existing `run_cycle` already flows through `aiohttp.ClientSession.get`, which aioresponses intercepts.
- `asyncio.gather` returns a `fetch_trending_pools` result that includes ranks; after aggregate → scorer, assert `gt_trending` in `signals_fired` for the emitted token.

## 12. Files created / modified

**Modified:**
- `scout/models.py` — add `gt_trending_rank` field
- `scout/ingestion/geckoterminal.py` — capture idx → rank in parse loop
- `scout/aggregator.py` — add `gt_trending_rank` to `_PRESERVE_FIELDS`
- `scout/scorer.py` — add Signal 10 `gt_trending`, bump `SCORER_MAX_RAW` 183→198, update docstring, add structlog import
- `scout/config.py` — add `GT_TRENDING_TOP_N`
- `.env.example` — document `GT_TRENDING_TOP_N`
- Any golden-value tests that assert a raw-score cap boundary (identify in plan)

**Created:**
- `tests/test_geckoterminal_rank.py`
- `tests/test_aggregator_gt_rank.py`
- `tests/test_scorer_gt_trending.py`
- `tests/test_scorer_max_raw_bumped_gt.py`
- `tests/test_main_pipeline_gt_trending.py` (if integration test needs its own file)

**No DB schema changes. No new dependencies.**

## 13. Acceptance criteria

1. `fetch_trending_pools` unit test emits 3 pools → candidates have ranks [1, 2, 3].
2. `aggregate` preserves `gt_trending_rank` across a DexScreener+GT pair regardless of merge order.
3. `scorer.score` fires `gt_trending` at rank 1 (default top_n=10) and NOT at rank 11. Signal is in `signals_fired`.
4. `scorer.score` fires `gt_trending` at rank 3 when `GT_TRENDING_TOP_N=3`, does NOT at rank 4 (boundary test).
5. `SCORER_MAX_RAW == 198` asserted by a dedicated pin test.
6. `gt_trending_signal_fired` structlog event emitted exactly once with required fields when signal fires; not emitted below threshold.
7. Integration test: full `run_cycle` with mocked GT/DEX/CG responses produces `gt_trending` in `signals_fired` for the expected token.
8. Full test suite passes (`uv run pytest --tb=short -q`). No regressions in existing BL-051 tests (if PR #34 is merged into master before PR #35 lands) or any other module.
9. `.env.example` contains `GT_TRENDING_TOP_N` entry in its own `# -------- GeckoTerminal Per-Chain Trending (BL-052) --------` section (NOT appended inside the paper-trading block).
10. Scorer module docstring (`scout/scorer.py` top) lists `gt_trending (+15)` in its signals table and the "Max raw:" summation line reads `...+15 = 198` (or `...+15 = 218` if BL-051 has already merged). Implementation pin test asserts this value coherently.
11. `_PRESERVE_FIELDS` in `aggregator.py` has an explicit contract comment above it: `# Preserve first non-None value on merge. Changing this semantics breaks all rank and enrichment signals.`

## 14. Risk & mitigation

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| New signal inflates scores for tokens that were already emitted as candidates by GT (they all get rank ≤ 10 by construction for small trending_pools responses) | Medium | Default `GT_TRENDING_TOP_N=10` restricts to upper half of GT's ~20 response. Tunable down to 3 or 5 if live observation shows grade inflation. |
| Duplicate scoring overlap with `cg_trending_rank` for tokens trending on both platforms | Low | Intentional — that's the confluence signal the spec explicitly targets. The co-occurrence multiplier already exists to reward multi-signal agreement. |
| Changing `SCORER_MAX_RAW` shifts the normalized-score distribution, affecting `MIN_SCORE` and `CONVICTION_THRESHOLD` thresholds | Medium-High | **Actual arithmetic (not "below observation threshold"):** `int(raw*100/183)` vs `int(raw*100/198)` loses ~2 pts mid-range and up to 8 pts at the cap. Examples: raw=100 → 54→50 (-4); raw=150 → 81→75 (-6); raw=180 → 98→90 (-8); raw=183 → 100→92 (-8). **Impact on live thresholds:** Project's live `MIN_SCORE` and `CONVICTION_THRESHOLD` live at 25 and 22 (see backlog.md D1), far below where this shift bites. A token scoring 71 normalized pre-bump now scores 65 — still well above both gates. **Philosophical framing:** Making room for a new signal correctly tightens the threshold for tokens that DON'T fire the new signal; tokens that DO fire `gt_trending` earn back +~8 normalized pts, netting near-zero change at the cap. This is intentional, not a regression. Document this in the scorer docstring comment for future maintainers. |
| Missing rank info due to GT API schema change (order not guaranteed?) | Very low | GT explicitly sorts trending_pools by 24h trade count; docs are stable. If they change, top-N bonus quietly stops firing — fail-open degradation, no pipeline break. |
| `from_geckoterminal` consumers in tests break if the classmethod signature changed | N/A | Classmethod signature is unchanged. Rank is set post-construction via `model_copy`. |

## 15. Follow-ups (deferred, not in this PR)

- Use rank in velocity-tracking (delta between cycles): a token jumping from rank 15 → rank 2 across two cycles is a strong "heating up" signal, separate from the simple threshold-based `gt_trending`. Would require persisting last-cycle ranks.
- Rank-based point curve (1 = 20pts, 2-3 = 15pts, 4-10 = 10pts) instead of flat 15. Defer until data shows whether top-3 genuinely outperforms top-10.
- Extend to GT `/new_pools` endpoint as a "recently-deployed and trending" meta-signal. Separate backlog item.
- Refactor `fetch_trending_pools` to return BOTH a raw rank list (decoration style, like BL-051 BoostInfo) AND emit candidates. Current implementation couples them; teasing apart would be cleaner but is out of scope.

## 16. Summary

Minimal-surface change: one field, one parser tweak, one aggregator line, one scorer block, one setting. No new HTTP calls, no DB migrations, no new dependencies. Pure enhancement of an existing data path that was already flowing through the pipeline but dropping positional info on the floor. Mirrors the `cg_trending_rank` pattern exactly — this spec is essentially its per-chain-DEX twin.
