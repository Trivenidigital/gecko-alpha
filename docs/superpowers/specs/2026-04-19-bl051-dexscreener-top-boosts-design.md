# BL-051 — DexScreener `/token-boosts/top` Poller → `velocity_boost` Signal

**Date:** 2026-04-19
**Status:** Design
**Branch:** `feat/bl-051-dexscreener-boosts-poller`
**Backlog entry:** BL-051 — DexScreener `/token-boosts/top` poller → `velocity_boost` tier. Detects paid-promo, copycat, rotation plays. Lead time: seconds-to-minutes. Free API, 1–2 days effort.

---

## 1. Goal

Add a new ingestion poller that queries DexScreener's `/token-boosts/top/v1` endpoint (tokens ranked by cumulative paid-promotion spend) and decorates existing pipeline candidates with two new fields — `boost_total_amount` and `boost_rank`. The scorer then fires a new `velocity_boost` signal worth +20 points when a candidate's cumulative boost spend clears the `MIN_BOOST_TOTAL_AMOUNT` threshold (default $500 USD-equivalent). This gives gecko-alpha a direct line of sight into paid-promo momentum — a high-signal precursor for copycat/rotation plays on memecoins — without adding new tokens to the pipeline, keeping MiroFish budget and dedup invariants unchanged.

## 2. Rationale

**Why paid-promo correlates with early pumps.** DexScreener "boosts" are paid promotional credits applied by a token's team/community to pin it to the top of DexScreener's discovery feeds. On Solana and Base memecoin markets we observe two recurring patterns: (a) a team boosts aggressively *before* the pump to seed awareness (copycat plays where a new token is riding an existing narrative), and (b) boosts ramp up *during* the pump as holders pile in to keep the token visible. The `/token-boosts/latest/v1` endpoint (already polled by `scout/ingestion/dexscreener.py`) captures individual boost events as they happen, but a single $100 boost is noise. The `/token-boosts/top/v1` endpoint surfaces the *cumulative* spend across all boosts on a token — so $1,500 of stacked promotion is a strong statement of intent, while a one-off $100 boost is not. The `velocity_boost` signal intentionally keys off cumulative total, not latest, to filter ephemeral boost spam.

**Expected lead time.** Seconds-to-minutes ahead of CoinGecko Trending (the current benchmark). Paid-boost signals are operator-driven and therefore precede organic discovery by definition; the operator pays the fee before crowd attention arrives. In practice we expect `velocity_boost` to fire on tokens already present in the pipeline via DexScreener's `/token-boosts/latest/v1` or GeckoTerminal trending pools, but *before* they earn enough organic volume to trigger `vol_acceleration` or `cg_trending_rank`. The signal is deliberately small (+20 pts) so it contributes to conviction confluence rather than firing an alert on its own — alert-grade conviction still requires the co-occurrence multiplier (3+ signals).

## 3. Non-Goals

- **Not a new token source.** The top-boosts endpoint returns only `tokenAddress`, `chainId`, and amounts — no market-cap, volume, liquidity, or holder data. We will NOT hydrate new candidates from it; it decorates existing ones only. Tokens visible on `/token-boosts/top` but absent from the pipeline are ignored this cycle (they will enter via DexScreener/GeckoTerminal pathways if they are trading).
- **No historical boost trend.** We do not persist boost history, slope, or ranking deltas. If the signal proves valuable we can revisit in a follow-up; for v1 we use point-in-time totals only.
- **No boost alerting in isolation.** `velocity_boost` does not bypass the existing gate; it contributes +20 to the quant score and must combine with other signals to reach `CONVICTION_THRESHOLD`.
- **No schema migration for DB persistence.** `boost_total_amount` and `boost_rank` are in-memory only on `CandidateToken`. The existing `candidates` row will NOT be extended with new columns in v1 (keeps migration risk at zero — signal appearance in `signals_fired` is the persisted artifact).
- **No paid-tier endpoints.** `/token-boosts/top/v1` is free. We never call any Pro/paid DexScreener endpoint.

## 4. Architecture

The poller slots into Stage 1 of the existing 6-stage pipeline as a sibling of the existing DexScreener, GeckoTerminal, and CoinGecko fetchers. Because it is decorator-only, its output is passed through the aggregator as a second-class list that merges onto the primary candidate set rather than extending it.

### Pipeline diagram (Stage 1–3)

```
  Stage 1: ingestion (parallel, asyncio.gather)
  -----------------------------------------------------------------
   fetch_trending (DexScreener /token-boosts/latest + tokens/v1)
   fetch_trending_pools (GeckoTerminal)
   cg_fetch_top_movers (CoinGecko markets)
   cg_fetch_trending (CoinGecko search/trending)
   cg_fetch_by_volume (CoinGecko markets by volume)
   fetch_top_boosts (DexScreener /token-boosts/top) <-- NEW
  -----------------------------------------------------------------
                          |
                          v
  Stage 2a: aggregate() — dedup by contract_address
                          |
                          v
  Stage 2b: apply_boost_decorations(candidates, boosts) <-- NEW
                          |
                          v
  Stage 3: score() — reads boost_total_amount, may fire velocity_boost
```

### Sequence (textual)

1. **Fetch** (`scout/ingestion/dexscreener.py::fetch_top_boosts`): single GET to `https://api.dexscreener.com/token-boosts/top/v1`. Uses the existing `_get_json` helper for retry/backoff. Parses each entry into a lightweight `BoostInfo` dataclass (not a full `CandidateToken`). Returns `list[BoostInfo]`.
2. **Gather** (`scout/main.py::run_cycle`): the call is added as a 6th positional arg to the existing `asyncio.gather(..., return_exceptions=True)` in Stage 1. Argument position matters — the returned tuple is destructured by position in the current code, so the new fetch is appended to the end (position 6) and receives its own exception-handling branch below the five existing ones.
3. **Aggregate** (`scout/aggregator.py::aggregate`): existing logic is unchanged; dedups the five primary source lists by `contract_address`.
4. **Decorate** (`scout/aggregator.py::apply_boost_decorations`): called immediately after `aggregate()`. Builds a dict keyed on `(chain, normalized_address)` from the `BoostInfo` list. Walks the deduped candidates and, for matches, issues `model_copy(update={boost_total_amount: ..., boost_rank: ...})`. Non-matches are left untouched; boost entries with no candidate match are silently ignored (they are not eligible pipeline tokens — no market data to score against).
5. **Score** (`scout/scorer.py`): the scoring pass already runs after aggregation. The new signal reads the decorated field and fires as described in Section 6. No call-site changes — the field is already attached.
6. **Downstream stages unchanged**: MiroFish, gate, safety, alerter consume `signals_fired` exactly as they do today. `velocity_boost` simply appears as another entry in the string list. `CandidateToken.boost_total_amount`/`boost_rank` are not serialized to the DB in v1 (see Non-Goals §3), so `scout/db.py` does not change.

### Why "decorator" and not "sixth source"

The five existing Stage-1 pollers each produce fully-populated `CandidateToken` instances: market-cap, volume, liquidity, age. The top-boosts endpoint cannot — its response is three useful fields (`chainId`, `tokenAddress`, `totalAmount`). Treating it as a sixth source would either (a) create `CandidateToken` stubs with `liquidity_usd=0`, `market_cap_usd=0` etc., which would then fail the scorer's hard liquidity disqualifier, or (b) require an N-call hydration loop through `/tokens/v1` that duplicates `fetch_trending`. The decorator model avoids both: we only enrich tokens already validated by other sources, and we never inflate the pipeline population with data-empty stubs.

### Module-level state

One new module-level cache in `scout/ingestion/dexscreener.py`: `last_raw_top_boosts: list[dict] = []`. Populated on every successful fetch, consumed by no one in v1 (keeps parity with `_cg_module.last_raw_markets` pattern for optional dashboard surfacing later; does NOT add a dashboard endpoint in this spec). If the fetch fails (retries exhausted) the cache is left untouched — stale data is preferred over empty data for any future observability consumer.

## 5. Data Model Changes

File: `scout/models.py::CandidateToken`. Two new optional fields added to the existing Pydantic model:

```python
# Populated by DexScreener top-boosts decorator
boost_total_amount: float | None = None
boost_rank: int | None = None
```

**Decision:** both default to `None`. A present `boost_total_amount` means the token appeared on the top-boosts list; `boost_rank` encodes its position (1 = most-boosted). `None` means "not present on the list this cycle" — this is the common case for the majority of candidates.

**Decision:** no new `from_top_boosts()` classmethod. The decorator path mutates via `model_copy(update=...)` in the aggregator, consistent with how `_PRESERVE_FIELDS` is already merged.

## 6. Scorer Change

File: `scout/scorer.py`.

**New signal: `velocity_boost` — +20 points.**

Condition: `token.boost_total_amount is not None and token.boost_total_amount >= settings.MIN_BOOST_TOTAL_AMOUNT`.

Placement: inserted as Signal 10 (before `solana_bonus`, so the chain bonus and velocity bonus remain at the end of the scorer). The signal counter `SCORER_MAX_RAW` is updated from **183** to **203** (30 + 8 + 25 + 15 + 15 + 15 + 20 + 25 + 15 + **20** + 5 + 10 = 203).

Docstring header updated to list the new signal alongside the other DexScreener signals.

**Decision — weight +20:** middle of the existing weight distribution (signals in the codebase use 5/8/10/15/20/25/30). Paid-boost data is operator-intent signal — strong but not as definitive as organic volume acceleration (+25) or vol/liq ratio (+30). Equivalent in weight to `momentum_ratio` (also +20), which is intentional: both are second-tier momentum signals meant to confirm rather than trigger.

**Decision — `boost_rank` not directly used in scoring v1:** rank is stored for observability/follow-up tuning but does not currently adjust points. A future iteration could scale (+20 for rank 1–5, +10 for rank 6–30) but that requires real data to calibrate — out of scope here.

## 7. Config Settings

File: `scout/config.py`. Add to a new block labeled `# -------- DexScreener Top Boosts --------`:

| Key | Default | Rationale |
|---|---|---|
| `DEXSCREENER_TOP_BOOSTS_POLL_EVERY_CYCLES` | `1` | Endpoint is free, no auth, no documented rate cap. Poll every cycle (≈60s) for freshest rank data. Knob exists so we can throttle if we see 429s in production. |
| `MIN_BOOST_TOTAL_AMOUNT` | `500.0` | Minimum cumulative `totalAmount` (USD-equivalent) to trigger the signal. $500 ≈ 5 stacked $100 boosts — enough to clearly exceed accidental/test spend while remaining below typical pump-team commitment (often $2k–$10k). Tunable via env var. |

No other config changes. No kill-switch — the feature is gated by threshold. If we need to fully disable the poller we can set `MIN_BOOST_TOTAL_AMOUNT` to a very large number (e.g. `1_000_000`), which blocks signal firing but still pulls data for observability.

**Decision — no `ENABLED` flag:** the feature is small, deterministic, and has no new external dependencies. Adding a flag would be cargo-culted scope. If the endpoint breaks we log and continue; the pipeline degrades gracefully on its own.

## 8. API Integration Details

**Endpoint.** `GET https://api.dexscreener.com/token-boosts/top/v1`. No auth headers.

**Response shape.** A bare JSON array (no envelope). Each element follows:

```
{
  "url": "https://dexscreener.com/solana/ADDR",
  "chainId": "solana",
  "tokenAddress": "ADDR",
  "amount": 100,           # most-recent boost amount (ignored by us)
  "totalAmount": 1500,     # cumulative spend — THIS is what we key on
  "icon": "...",           # ignored
  "description": "...",    # ignored
  "links": []              # ignored
}
```

**Rate limiting / retry.** Reuse the existing `_get_json(session, url, retries=MAX_RETRIES)` helper in `scout/ingestion/dexscreener.py`. It already implements: 3 retries, exponential backoff (2s/4s/8s) on 429 or 5xx, single 30s connect/read timeout via `REQUEST_TIMEOUT`, `None` return on non-retryable 4xx. The top-boosts poller gets these semantics for free — no new retry logic.

**Error handling.** On `None` return (all retries exhausted or non-retryable error), the poller returns an empty list. The gather handler in `main.py` treats exceptions the same way. A dropped cycle is invisible to users — next cycle refreshes.

**Schema drift safety.** For each entry, we parse defensively:
- Skip if `tokenAddress` is missing or empty.
- Skip if `chainId` is missing or empty.
- Skip if `totalAmount` is missing or not a number (log once at `warning` with the raw entry, continue).
- Coerce `totalAmount` through `float()` inside a try/except; on `TypeError`/`ValueError`, skip.

**Decision — hydrate via `/tokens/v1` like existing `fetch_trending`? No.** Hydrating each top-boosted address would cost N extra calls per cycle and duplicates work the existing DexScreener/GeckoTerminal pollers already do for the same address space. The design intent is decorate-only.

**Decision — response size bound.** The endpoint is documented to return up to 30 entries. We do not slice or cap further; the loop is trivially fast and we want full coverage for rank accuracy. If the API expands response size, our code continues to work — `boost_rank` remains monotonically increasing.

**Decision — no caching of the HTTP response.** Unlike CoinGecko markets (where `last_raw_markets` is consumed downstream for price caching) the top-boosts payload has no secondary consumer in v1. The `last_raw_top_boosts` module cache exists only for future dashboard wiring and is not part of any hot path.

## 9. Aggregator Semantics

File: `scout/aggregator.py`.

**New function:** `apply_boost_decorations(candidates: list[CandidateToken], boosts: list[BoostInfo]) -> list[CandidateToken]`.

Called from `main.py::run_cycle` after the existing `aggregate()` call and before scoring. Returns a new list (or mutates via `model_copy`, consistent with the preservation pattern already in use).

**Join key: normalized `contract_address`.**

`CandidateToken.contract_address` stores addresses case-sensitive on chains where that matters (all EVM chains are case-insensitive; Solana/Sui are case-sensitive base58). DexScreener returns addresses in canonical form (checksummed for EVM, native for others). We normalize both sides by `.lower()` for EVM chains only; Solana addresses are used as-is (lower-casing would corrupt them).

**Chain normalization (chainId → chain slug):** DexScreener's `chainId` values match our internal slugs for the chains we care about (`solana`, `base`, `ethereum`, `arbitrum`, `bsc`, `polygon`, `avalanche`). For completeness we define a small mapping dict in `dexscreener.py`; unknown chainIds are passed through unchanged and simply fail to join on the aggregator side (a no-op, not an error).

**Rank assignment.** `boost_rank` = `index + 1` over the raw API response order. The API already returns the list sorted by `totalAmount` desc; we trust that ordering and do not re-sort. If two tokens tie on `totalAmount` the API's ordering is stable — we reflect it.

**Merge rules.**
1. Build `boost_map: dict[str, BoostInfo]` keyed by `(chain, address_normalized)`.
2. For each candidate in the deduped list, look up `(candidate.chain, candidate.contract_address_normalized)`. If found, attach `boost_total_amount` and `boost_rank`.
3. If a candidate already has `boost_total_amount` set (e.g. pre-decorated by a previous sub-merge — doesn't happen in v1 but future-proof), prefer the new value (last-write-wins, matches existing aggregator semantics).

**Decision — `_PRESERVE_FIELDS`:** the two new fields are NOT added to `_PRESERVE_FIELDS`. That list covers fields that earlier-in-cycle sources populate and later ones might null out. Boost decoration runs once at the end of ingestion, so the preservation mechanic does not apply.

### BoostInfo dataclass

`scout/ingestion/dexscreener.py` defines a new lightweight container (module-level `@dataclass(frozen=True, slots=True)`) with three fields:

- `chain: str` — normalized internal chain slug (post-mapping).
- `address: str` — token contract address as returned by API (un-normalized; normalization applied at join time in the aggregator).
- `total_amount: float` — cumulative boost total.

Rank is not stored on `BoostInfo`; it is derived positionally by the aggregator from list order. This keeps the dataclass immutable and re-orderable without stale rank values.

**Decision — dataclass not Pydantic.** `BoostInfo` is internal plumbing between poller and aggregator, never persisted, never serialized. A stdlib dataclass is lighter and more honest about scope. Consistent with how existing internal helpers in the ingestion layer are written (see `_fetch_one` in `fetch_trending`).

### Normalization helper

`_normalize_chain_id(chain_id: str) -> str` in `scout/ingestion/dexscreener.py`. Takes the raw `chainId` string from DexScreener and returns our canonical chain slug. Implementation is a plain dict lookup with a `.lower()` default for unknowns:

```python
_CHAIN_ID_MAP = {"solana": "solana", "base": "base", "ethereum": "ethereum",
                 "arbitrum": "arbitrum", "bsc": "bsc", "polygon": "polygon",
                 "avalanche": "avalanche"}
```

All entries match 1:1 today — the dict exists for explicitness and as a choke point for future rebrands. Unknown chain IDs lower-case and pass through; the join in the aggregator simply fails to match a candidate (correct behavior: we do not track tokens on chains we do not ingest).

`_normalize_address(chain: str, address: str) -> str` — returns `address.lower()` for EVM chains (ethereum, base, arbitrum, bsc, polygon, avalanche, optimism, fantom); returns the address unchanged for non-EVM chains (solana, sui, aptos, tron). EVM addresses are case-insensitive by spec; mixed case (EIP-55 checksum) must match lowercase canonical form for the join.

## 10. Observability

Two new structured log events (via structlog, matching existing conventions):

- `dex_top_boosts_fetched` — emitted once per successful fetch by the poller. Fields: `count` (int, number of entries received), `top_amount` (float, `totalAmount` of rank-1 entry, or 0 if list empty).
- `velocity_boost_signal_fired` — emitted by the scorer each time the signal is added. Fields: `token` (ticker), `contract_address`, `chain`, `boost_total` (float), `boost_rank` (int).

**Decision — no new heartbeat counters.** The existing heartbeat already aggregates `signals_fired` counts; `velocity_boost` will appear there automatically. Adding a bespoke counter is scope-creep.

**Decision — no dashboard API.** v1 ships with log observability only. If the signal proves its worth, a follow-up can expose `/api/boosts/top` reading `last_raw_top_boosts`.

## 11. Error Handling

- **Upstream failure (endpoint down / network / all retries exhausted).** `_get_json` returns `None`, poller returns `[]`. `asyncio.gather(return_exceptions=True)` receives the empty list, downstream stages run normally. No alert, no cycle failure. Log line: the existing `_get_json` `warning` entries cover this; the poller itself does not re-log.
- **Partial schema drift.** Per-entry try/except on `totalAmount` parse. Log once per cycle with `warning`, skip the entry. Empty/unknown `chainId` entries are also skipped.
- **Mapping miss.** If a boost entry's `(chain, address)` does not match any candidate, it is silently dropped. The poller is decorator-only — unknown addresses are not errors.
- **`asyncio.gather` exception propagation.** Already handled: the new arg gets a sibling `isinstance(X, Exception)` guard in `main.py` identical to the other five ingestion sources.
- **Budget/MiroFish fallout.** `velocity_boost` does not alter MiroFish dispatch criteria — the feature is score-side only. No risk to the 50 jobs/day cap.

## 12. Testing

All tests live under `tests/` and follow the existing aioresponses + pytest-asyncio conventions.

1. **`tests/test_dexscreener.py` (extend).**
   - `test_fetch_top_boosts_happy_path` — mock the endpoint with 3 entries, assert poller returns 3 `BoostInfo` with correct `totalAmount`/`chain`/`address`/rank.
   - `test_fetch_top_boosts_empty_response` — mock `[]` response, assert empty list.
   - `test_fetch_top_boosts_missing_total_amount` — entry without `totalAmount` is skipped; one warning logged; valid entries pass through.
   - `test_fetch_top_boosts_upstream_500` — mock 500 on all retries, assert empty list, no exception raised.
   - `test_fetch_top_boosts_invalid_json` — mock 200 with non-JSON body, assert empty list.

2. **`tests/test_aggregator.py` (extend).**
   - `test_apply_boost_decorations_match` — candidate at address X; boost list contains X; resulting candidate has `boost_total_amount` and `boost_rank` set.
   - `test_apply_boost_decorations_no_match` — candidate at Y; boost list is for X only; candidate unchanged (`boost_total_amount is None`).
   - `test_apply_boost_decorations_evm_case_insensitive` — candidate at `0xABCD...` (upper); boost at `0xabcd...` (lower); join succeeds.
   - `test_apply_boost_decorations_solana_case_sensitive` — Solana address; verify no lowercasing applied.
   - `test_apply_boost_decorations_rank_order` — three boosts in a specific order; verify rank is index+1.

3. **`tests/test_scorer.py` (extend).**
   - `test_velocity_boost_fires_when_above_threshold` — token with `boost_total_amount=1500`; assert `velocity_boost` in `signals_fired` and score contribution is +20 (verify by scoring the same token with and without the field).
   - `test_velocity_boost_silent_below_threshold` — `boost_total_amount=100` (< default 500); signal absent.
   - `test_velocity_boost_silent_when_none` — `boost_total_amount is None`; signal absent.
   - `test_scorer_max_raw_constant` — assert `SCORER_MAX_RAW == 203`.

4. **`tests/test_main.py` or new `tests/test_pipeline_top_boosts.py` (integration).**
   - Mock all 6 ingestion endpoints including `/token-boosts/top/v1` with an entry matching one of the DexScreener pair fixtures. Drive `run_cycle()` once with `dry_run=True`. Assert: (a) the matching candidate's `signals_fired` contains `velocity_boost`; (b) its quant score is strictly higher than the same run with an empty top-boosts response.

5. **Regression.** Full existing suite passes. `SCORER_MAX_RAW = 203` normalization shift is absorbed by `min(100, int(points * 100 / SCORER_MAX_RAW))` — existing tests that assert specific score values must be audited; update golden values where the normalization shift moves results by 1–2 points.

**Decision — no live API test.** DexScreener's free endpoint is stable but we do not want CI dependent on third-party availability. All HTTP is mocked.

### Fixture reuse

Reuse existing fixtures where possible:
- `token_factory()` from `tests/conftest.py` produces `CandidateToken` instances with overrideable fields. Pass `contract_address=`, `chain=`, `boost_total_amount=` to drive scorer and aggregator tests.
- `settings_factory()` provides a `Settings` instance with defaults and overrides. Use `MIN_BOOST_TOTAL_AMOUNT=500.0` as the baseline and override to 1000/100 for threshold-boundary tests.
- `aioresponses` is the HTTP mock. Register `https://api.dexscreener.com/token-boosts/top/v1` with the exact payload structures tested; parse via JSON to catch content-type and status regressions together.

### Test data notes

- Use realistic Solana addresses (base58, 32-44 chars) and realistic EVM addresses (`0x` + 40 hex) in fixtures — not sentinel strings like `"addr1"`. Helps catch case-sensitivity bugs at review time.
- For `test_apply_boost_decorations_evm_case_insensitive`, mint the candidate with an EIP-55 checksum form (e.g. `0xAbC...`) and the boost entry with the all-lower form; assert match. Then reverse and assert match again.
- For Solana sensitivity, pick two addresses that differ only in case (one byte) and verify they do NOT match. (In practice this is vanishingly rare on Solana but the test documents the intent.)
- For the integration test, the DexScreener `/tokens/v1/<chain>/<addr>` mock and the `/token-boosts/top/v1` mock must return the same address so the decoration joins. Verify by asserting the resulting token's `signals_fired` contains both an existing DexScreener-sourced signal (e.g. `vol_liq_ratio`) AND `velocity_boost`.

## 13. Acceptance Criteria

A PR implementing this spec is complete when all of the following are verifiable:

1. **Endpoint fetch.** A unit test drives `fetch_top_boosts` against a mocked `/token-boosts/top/v1` response containing 3 entries and asserts `len(result) == 3` with correct `totalAmount`/`rank`/`chain`/`address` values.
2. **Decoration merge.** A unit test for `apply_boost_decorations` with (i) a candidate at address X and a boost entry at X, (ii) a candidate at Y with no matching boost. After decoration: X has `boost_total_amount` populated and `boost_rank == 1`; Y has both fields still `None`.
3. **Signal fires.** A unit test in `test_scorer.py` passes a `CandidateToken` with `boost_total_amount = 1500` (≥ default threshold 500) through `score()` and asserts `"velocity_boost"` ∈ returned `signals_fired` and the numeric score exceeds the same token scored with `boost_total_amount = None` by exactly the +20 point contribution (pre-normalization; compare pre-normalization via a helper or by computing the diff modulo normalization/co-occurrence exactly).
4. **Signal silent below threshold.** A token with `boost_total_amount = 100` does NOT fire the signal. A token with `boost_total_amount = None` does NOT fire the signal.
5. **Constant updated.** `SCORER_MAX_RAW == 203` asserted by a test; the value is used in `scorer.py`'s normalization call.
6. **Integration.** An end-to-end test driving `run_cycle()` with all 6 ingestion sources mocked produces a candidate whose `signals_fired` contains `velocity_boost` and whose stored quant score reflects the decoration.
7. **Regression.** `uv run pytest --tb=short -q` passes with zero regressions. Any golden-value test impacted by the `SCORER_MAX_RAW` shift is updated with an accompanying comment noting the BL-051 normalization change.
8. **Observability.** Grepping logs from a dry-run cycle shows at least one `dex_top_boosts_fetched` event and (when the fixture triggers it) at least one `velocity_boost_signal_fired` event.
9. **No `.env` / secrets leak.** No new env vars require credentials. `.env.example` updated with `MIN_BOOST_TOTAL_AMOUNT=500` and `DEXSCREENER_TOP_BOOSTS_POLL_EVERY_CYCLES=1` documented.

## 14. Risks / Open Questions

- **Chain-ID normalization drift.** DexScreener could introduce a new `chainId` value (e.g. a rebrand or a new chain we already track under a different slug). *Mitigation:* the mapping dict is explicit; unknown chainIds no-op on the join. *Detection:* a low-frequency `warning` log when `boost_map` size is non-trivial but zero candidates match in a cycle (deferred to a follow-up — not instrumented in v1 to keep scope small).
- **`totalAmount` currency unit.** DexScreener does not document the unit of `totalAmount`. From observation in-market the value tracks the USD-equivalent cost of the booster's purchased promo credit (boost packages are priced in USD). *Decision:* treat as USD. If this later proves to be SOL/ETH-denominated in some response variant, we update the docstring and threshold in a follow-up — the code is unaffected because the threshold is a float.
- **Boost spam / wash-boosting.** Nothing prevents a coordinated team from self-boosting to cross the $500 threshold cheaply. *Mitigation:* the signal is +20 of a possible 203 raw points, and the co-occurrence gate requires ≥3 signals for the multiplier. Boosts alone cannot alert; they must combine with real on-chain momentum. Revisit if we see false-positive clusters in production.
- **Persistence of boost history.** v1 does NOT store history; only point-in-time decorations. *Decision:* ship without history. If the signal proves valuable, a follow-up spec can add `boost_history_cg` (analogous to `volume_history_cg`) for slope/rank-delta signals.
- **Interaction with existing `fetch_trending`.** Both pollers hit DexScreener. Combined they are ≤~60 requests/cycle (top-boosts is 1 call; existing boosts-latest is 1 + N hydration calls). No documented rate cap exists for `/token-boosts/*`; the paid docs mention "reasonable rate limits" without numbers. *Mitigation:* reuse `_get_json` backoff; monitor 429 log volume after deploy and tighten `MAX_CONCURRENT` or `DEXSCREENER_TOP_BOOSTS_POLL_EVERY_CYCLES` if needed.

## 15. Rollout / Ops

**Deploy target.** Srilu VPS (`89.167.116.187`), existing `gecko-alpha.service` systemd unit. No new services or ports.

**Rollout steps.**
1. Merge PR to `master` after CI green and local `run_cycle --dry-run --cycles 1` smoke.
2. `systemctl restart gecko-alpha.service` on VPS. New poller comes online immediately on next cycle.
3. Tail journal for 10 minutes to confirm: (a) `dex_top_boosts_fetched` appears every cycle, (b) no new warning/error patterns, (c) existing signal counts in heartbeat are unchanged except for new `velocity_boost` entries.

**Rollback.** One-line revert of `main.py` gather arg + `scorer.py` signal block; `SCORER_MAX_RAW` reverts to 183. Models/config fields can stay in place (they default to `None` / documented values and have no live effect without the scorer/main changes).

**Post-deploy validation window.** 24 hours. Watch for:
- Heartbeat delta: how many cycles fire `velocity_boost`? Expect low double-digits/day based on typical top-boosts traffic.
- False-positive audit: any tokens alerted solely because `velocity_boost` pushed them across the conviction threshold? (Should not happen — alerting still needs quant+narrative composite — but verify.)
- Rate-limit response: any 429s from DexScreener? Historical deploy pattern shows none, but a new endpoint may differ.

**Tuning guidance.** If `velocity_boost` fires on <3 tokens/day, lower `MIN_BOOST_TOTAL_AMOUNT` to 250 to widen the funnel. If it fires on >50/day, raise to 1000. Tune from data, not speculation.

## 16. Implementation Checklist (for the executor)

1. `scout/models.py` — add `boost_total_amount: float | None = None` and `boost_rank: int | None = None` to `CandidateToken`.
2. `scout/config.py` — add the two new settings in a `# -------- DexScreener Top Boosts --------` block. Update `.env.example`.
3. `scout/ingestion/dexscreener.py` — add `TOP_BOOSTS_URL`, `BoostInfo` dataclass, `last_raw_top_boosts` module-level list, `fetch_top_boosts()` function, and chain-ID normalization helper.
4. `scout/aggregator.py` — add `apply_boost_decorations()` function.
5. `scout/scorer.py` — add signal 10 block; bump `SCORER_MAX_RAW` to 203; update module docstring.
6. `scout/main.py` — extend the Stage 1 `asyncio.gather` with the new fetch; add exception branch; call `apply_boost_decorations()` after `aggregate()`.
7. Tests per Section 12.
8. `uv run pytest --tb=short -q` clean; `uv run black scout/ tests/` clean; `uv run python -m scout.main --dry-run --cycles 1` smoke.
