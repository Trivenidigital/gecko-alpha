**New primitives introduced:** `meta.market_benchmarks` optional object on `/api/todays_focus` payload (`{btc_4h_pct: float, sol_4h_pct: float}` with per-benchmark omission when unavailable); `meta.market_benchmarks_is_visual_context_only: true` flag (presence-iff-any-benchmark-present, strict-True identity); contract-firewall validation for the new meta keys (added to `OPTIONAL_META_KEYS` per the absence-vs-null semantic); `dashboard/frontend/components/MarketContextStrip.jsx` thin factual benchmark renderer; CSS class `.todays-focus-market-context`.

# Today's Focus PR-D: Market Context Strip (Factual Benchmarks)

**Goal:** Add a thin factual benchmark strip below the panel header so the trader can read BTC and SOL 4h deltas without leaving the dashboard. Pure scan-aid; no regime labels, no directional advice, no cohort aggregates.

**Operator-pinned scope (2026-05-28):**
1. `BTC 4h: ±X.X%`
2. `SOL 4h: ±X.X%`
3. Optionally surface existing meta counts (rows_returned / source_rows_considered) — already present in meta, no new query needed
4. **NO** regime labels (`risk-on`, `risk-off`, `quiet`, `high vol`)
5. **NO** directional advice (`size up`, `sit out`, `take profit`)
6. **NO** cohort averages over the Today's Focus rows themselves (`Focus rows avg 24h +X%` smuggles cohort-coherence inference)

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Market benchmark display | Hermes dashboards target Hermes, not gecko-alpha React. | Build in-repo. |
| BTC/SOL price-delta query | No Hermes skill owns gecko-alpha's `volume_history_cg`. | Reuse PR-C's `_ro_db` + per-coin price-point query pattern. |
| Regime / sentiment labels | Explicitly out of scope. | N/A. |

Awesome-hermes ecosystem: no diagnostic plugin owns gecko-alpha's row contract or data sources. Custom repo-local work warranted.

## Drift / Runtime Findings (master @ `25d15fee`)

- Sparkline source `volume_history_cg` (scout/db.py:863-875) has `price` and `recorded_at`. Already verified by PR #312 audit (writer cadence sufficient).
- `bitcoin` and `solana` coin_ids are CoinGecko slugs; the CG markets-watcher polls them as part of the universal coverage. Live audit snapshot (2026-05-28T22:30:15Z) confirmed 547 points/24h for non-blue-chip slugs, so blue-chips should have ≥same density.
- `/api/todays_focus` has NO `response_model` (PR #317 removed it). The dict from `db.get_todays_focus()` passes through to JSON wire as-is. Contract firewall is the source of truth for shape validation.
- Existing meta already has `rows_returned`, `source_rows_considered`, `source_group_counts` — useful operator context, but currently surfaced via the existing `todays-focus-meta` span in the heading. No new query needed for those.

## Universe Pin

- **Benchmark coins:** `bitcoin` and `solana` only. No ETH, no SPX, no DOGE — each additional benchmark adds a "why this not that" interpretive surface. Operator-pinned to two.
- **Lookback window:** `4h` exactly (configurable via `--lookback-hours` is NOT exposed; this is a fixed operator-chosen interval).
- **Delta computation:** `(latest_price_in_window - oldest_price_in_window) / oldest_price_in_window * 100`, rounded to 2 decimals.
- **Validity guard:** both oldest and latest prices must satisfy `0 < price < 1e308` (Infinity guard, defensive). If either fails or `volume_history_cg` returns fewer than 2 points in the window for that coin, the benchmark is OMITTED.
- **Server-side cutoff timestamp:** captured ONCE per `/api/todays_focus` request via `datetime.now(timezone.utc)`; reused for both benchmark queries AND PR-C's sparkline query (single clock-source pin).
- **ISO cutoff format:** matches the writer at `scout/spikes/detector.py:26` — `(now - timedelta(hours=4)).isoformat()`.
- **Wire shape:**
  - When both benchmarks computable: `meta.market_benchmarks = {"btc_4h_pct": -0.5, "sol_4h_pct": 1.2}` AND `meta.market_benchmarks_is_visual_context_only = True`.
  - When one benchmark computable: only the present key is included in `market_benchmarks`; flag still set.
  - When neither computable: BOTH `market_benchmarks` AND the flag are absent from meta entirely.
- **Numeric type:** `float`. Both fields strict-checked as `isinstance(v, (int, float))` AND `not isinstance(v, bool)` in the contract firewall.

## Scope

Build:

1. **Server-side (`dashboard/db.py`)**:
   - Add helper `_fetch_benchmark_delta_pct(db, coin_id, cutoff_iso, now_iso) -> float | None`. Returns rounded delta when both oldest+latest valid; None otherwise.
   - In `get_todays_focus`, after the existing PR-C `_fetch_price_path_points` loop, also fetch benchmarks for `bitcoin` and `solana` using the SAME `_ro_db` connection (already open).
   - Compose `meta["market_benchmarks"] = {...}` only when at least one benchmark resolves; set `meta["market_benchmarks_is_visual_context_only"] = True` iff the dict is included.
2. **Contract firewall (`scripts/check_todays_focus_contract.py`)**:
   - Add `market_benchmarks` and `market_benchmarks_is_visual_context_only` to `OPTIONAL_META_KEYS`.
   - Add validator `_check_market_benchmarks(meta, result)`:
     - When `market_benchmarks` present: must be a `dict`; allowed keys are `{btc_4h_pct, sol_4h_pct}`; each value must be `int|float` (not bool), finite, in range `[-1e308, 1e308]`. At least one key present.
     - When `market_benchmarks` absent: flag must be absent (same absence semantic as PR-C sparkline flag).
     - When `market_benchmarks` present: flag must be exactly `True` (identity check).
   - No new BANNED_PATTERNS entries — the data is purely numeric; no copy enters the wire.
3. **BtcSolBenchmarkStrip component (`dashboard/frontend/components/BtcSolBenchmarkStrip.jsx`)** — renamed from `MarketContextStrip` per Reviewer A N7 fold to encode purpose in name and foreclose generic reuse for sentiment/regime indicators. Pure functional renderer:
   - Props: `{ benchmarks: {btc_4h_pct?: number, sol_4h_pct?: number} | undefined }`.
   - Returns `null` if `benchmarks` is null/undefined OR if no key present.
   - Renders inline `<span>` elements (NOT a `<div>` banner) styled as siblings of the existing `todays-focus-meta` chips. Per Reviewer A B1 fold: placement is **WITHIN the existing `.todays-focus-heading` meta-chip flex container**, NOT as a separate banner row above or below. Visual prominence matches the existing `{rows_returned} rows from {source} candidates` chip.
   - Per-benchmark format: `BTC 4h: -0.5%` or `SOL 4h: +1.2%`. Sign is explicit (`+` for non-negative, `-` for negative).
   - `aria-label="BTC and SOL 4-hour deltas"` (strict-pinned via layout test; no other extension permitted).
   - No interaction, no tooltip, no color severity, no animation, no banner wrapper.
   - Component source is statically scanned for: (a) the same banned SVG substrings as Sparkline (no `<text|<title|<circle|...`), and (b) per Reviewer A N8 fold, the regime/advice vocabulary substrings: `risk-on`, `risk-off`, `quiet`, `high vol`, `low vol`, `range-bound`, `trending`, `choppy`, `size up`, `sit out`, `take profit`. Static-scan test asserts each substring absent from `BtcSolBenchmarkStrip.jsx` source.
4. **TodayFocusPanel wiring**: render `<BtcSolBenchmarkStrip benchmarks={meta.market_benchmarks} />` as the LAST child of `<div className="todays-focus-heading">` (alongside the existing `rows / refreshed` meta chips). Per Reviewer A B1 fold: this places the strip in the meta-chip row, NOT above/below the row list. Component returns `null` when absent — no placeholder text.
5. **CSS (`dashboard/frontend/style.css`)**:
   - `.todays-focus-benchmark`: the per-chip span style. `color: var(--color-text-secondary)` (uniform — same as the existing meta chips). `font-size: 12px`. NO color-coding for sign (uniform color regardless of `+` or `-`).
   - 480px portrait: chips wrap naturally on `flex-wrap` of the parent `.todays-focus-heading` (existing rule); no new media-query rules required for the benchmark chips themselves.
   - Per Reviewer A B2 fold: a concrete CSS-uniformity test asserts that the `.todays-focus-benchmark` rule contains exactly ONE `color:` declaration AND no `[data-sign=]`, `:nth-child`, `:has(`, conditional selectors that could differ color by sign or position. The factual-copy scan on JSX further forbids `className={... ? "pos" : "neg"}` patterns.
6. **Tests**:
   - Server-side endpoint tests (`tests/test_todays_focus_endpoint.py`):
     - Both benchmarks computable → both keys present + flag True.
     - One benchmark missing → only the present key + flag True.
     - Both missing → `market_benchmarks` absent + flag absent.
     - Wire-shape regression: assert via `payload["meta"].get(...)` exactly the expected presence-vs-absence semantics. **Per `feedback_fastapi_wire_shape_reviewer_pattern.md`, this test catches the response_model envelope drift PR-C's hotfix series surfaced.**
   - Contract firewall tests (`tests/test_check_todays_focus_contract.py`):
     - Valid payload with both benchmarks + flag passes.
     - Payload with benchmarks but no flag fails critical.
     - Payload with flag `False` fails (strict True identity).
     - Payload with flag `1` (truthy non-bool) fails.
     - Payload with `market_benchmarks` containing string value fails.
     - Payload with `market_benchmarks` empty dict fails (at least one key required).
     - Payload with unknown key in `market_benchmarks` fails.
     - Payload with flag set but no `market_benchmarks` field fails.
     - **Per Reviewer A B3 fold**: payload with `market_benchmarks = {"btc_4h_pct": -0.5, "focus_rows_avg_24h_pct": 1.0}` fails critical (specific cohort-average key name; ensures the future implementer reads a failure message naming exactly what they tried to smuggle).
     - **Per Reviewer A N3 fold**: payload with `len(market_benchmarks) > 2` (e.g., `btc_4h_pct` + `sol_4h_pct` + `eth_4h_pct`) fails critical (defends the 2-benchmark pin against silent expansion).
     - **Per Reviewer B N6 fold**: payload with `btc_4h_pct = 0.0` and `sol_4h_pct = -0.0` passes (defensive: pin `0` vs `0.0` JSON behavior).
   - Layout tests (`tests/test_dashboard_frontend_layout.py`):
     - JSX imports `MarketContextStrip` and renders between panel-header and todays-focus-status.
     - CSS contains `.todays-focus-market-context`.
     - Dist bundle contains `BTC 4h` and `SOL 4h` literals.
     - Banned SVG substrings absent from MarketContextStrip.jsx.
     - Factual-copy scan extended to include MarketContextStrip.jsx.
   - Sparkline component test (PR-C residual): no change required; this PR's strip is a separate component.

## Anti-Scope (firewall-equivalent at plan level)

1. **No regime / sentiment / volatility labels.** No `risk-on`, `risk-off`, `quiet`, `high vol`, `low vol`, `range-bound`, `trending`, `choppy`, etc. The strip emits only `BTC 4h: ±X.X%` / `SOL 4h: ±X.X%`. BANNED_PATTERNS-style enforcement is unnecessary because the data is purely numeric; no copy enters the JSON wire.
2. **No directional advice copy.** No `size up`, `sit out`, `take profit`, `be careful`, `wait`, `now is a good time`.
3. **No cohort aggregates.** No `Focus rows avg 24h +X%`, no `cohort heating up`, no `today's batch is hot/cold` framing. Cohort coherence is inference.
4. **No additional benchmarks beyond BTC + SOL.** Adding ETH / DOGE / SPX / BNB / etc. would introduce per-benchmark "why this" interpretive surface. Operator pin holds.
5. **No color severity.** Sign `+` and `-` use the same uniform `color: var(--color-text-secondary)`. No green-for-up / red-for-down.
6. **No interaction.** No hover tooltip, no click-to-expand, no time-range selector. View-only.
7. **No row ordering / styling derived from benchmark direction.** Frontend MUST NOT reorder rows when `btc_4h_pct < 0` (e.g., promote "shorts" rows). No conditional row CSS.
8. **No alerts / Telegram dispatch.** The strip is dashboard-visibility only.
9. **No interpretive `aria-label` on the strip.** `aria-label="Market context"` is the only permitted value; layout test pins.
10. **No response_model usage on `/api/todays_focus`** (PR #317 already removed it; this PR does not re-add it).
11. **Numeric type fidelity.** Server emits `float`; contract checker accepts `int|float`; Pydantic envelope is absent so no coercion can break the wire shape. Endpoint integration test asserts numeric type at the wire layer per the new wire-shape memory.
12. **No backfill, no new tables, no migration, no schema changes** beyond the optional meta-key additions handled by OPTIONAL_META_KEYS.
13. **Per Reviewer B B1 fold — TodaysFocusMeta Pydantic model receives NO update.** The current `dashboard/models.py:TodaysFocusMeta` is dead code (since PR #317 removed `response_model=TodaysFocusResponse` from the route). DO NOT add `market_benchmarks` or `market_benchmarks_is_visual_context_only` fields to the Pydantic model. The dict-as-wire path is the source of truth. A future PR adding these fields AND re-decorating the route with `response_model=...` would silently reintroduce the PR-C-style absence-vs-null tension. Anti-scope §10 already bans re-adding response_model; this §13 explicitly bans the parallel model-edit path.

## FastAPI Wire-Shape Check (per `feedback_fastapi_wire_shape_reviewer_pattern.md`)

This PR's reviewer prompts MUST include the trace:

```
db.get_todays_focus() return dict
  -> no response_model (PR #317 cleared the envelope)
    -> FastAPI default JSON serialization
      -> JSON wire
        -> scripts/check_todays_focus_contract.py
```

Concrete assertions in tests (per Reviewer B B2 fold — correct `isinstance` argument order):
- Endpoint integration test fetches the live response via httpx, asserts `payload["meta"]["market_benchmarks"]["btc_4h_pct"]` exists AND `isinstance(payload["meta"]["market_benchmarks"]["btc_4h_pct"], (int, float)) and not isinstance(..., bool)` (numeric, not string-coerced, not bool).
- Endpoint integration test asserts `payload["meta"].get("market_benchmarks_is_visual_context_only") is True` (identity check, matching contract firewall).
- Endpoint integration test asserts absence behavior: when `volume_history_cg` empty for both bitcoin and solana, both `market_benchmarks` and the flag are KEY-ABSENT from `payload["meta"]` (NOT serialized as null). Specifically: `"market_benchmarks" not in payload["meta"]` AND `"market_benchmarks_is_visual_context_only" not in payload["meta"]`.
- Cutoff timestamp test (per Reviewer A N5 + B N1 folds): server-side unit test on the cutoff helper asserts `cutoff_iso = (now - timedelta(hours=4)).isoformat()` exactly. Plus: the benchmark fetch happens INSIDE the existing `_ro_db` `async with` block in `get_todays_focus` — verified by reading the code flow before implementation.

## Merge Gate

PR merges only when ALL three hold:
1. CI green.
2. Both PR reviewers (anti-scope + wire-shape; correctness + tests) return zero findings OR only non-blocking findings.
3. Every blocking finding folded.

Smoke after deploy:
1. Render Today's Focus; strip shows `BTC 4h: ±X.X% | SOL 4h: ±X.X%` (or omitted when data unavailable).
2. Aggregate contract checker returns 0 criticals.
3. `curl /api/todays_focus` returns numeric `btc_4h_pct` / `sol_4h_pct` values (not strings; not null when present).
4. 375px portrait: strip fits without horizontal overflow.
5. Visual inspection: no color severity on the sign; uniform text color.
