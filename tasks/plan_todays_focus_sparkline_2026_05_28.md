**New primitives introduced:** `/api/todays_focus` row field `price_path_points: list[[unix_ts: int, price: float]]`; payload meta flag `sparkline_is_visual_price_history_only: true`; `dashboard/frontend/components/Sparkline.jsx` pure-functional SVG renderer; CSS classes `.todays-focus-sparkline` and `.todays-focus-sparkline-unavailable`; contract-firewall extension allowing `price_path_points` with array-of-numeric-pairs validation.

# Today's Focus PR-C: Sparkline (Visual 24h Price Path)

**Goal:** Reduce per-candidate external chart-tab opens by rendering a small inline 24h price-path sparkline on the main row. Coverage audit (PR #312 / #313) confirmed all 5 current rows have 125-547 points in 24h — well above any reasonable density floor — so this feature ships without a backfill prerequisite.

**Operator-approved guardrails (2026-05-28):**
1. Render only normalized 24h price path. No annotations.
2. Show factual "Sparkline unavailable" fallback ONLY if coverage falls below pinned density.
3. No labels: no "still pumping", "fading", "breakout", "good setup", "trend up/down".
4. Payload meta flag: `sparkline_is_visual_price_history_only: true`.
5. Mobile 375px portrait must render 5 rows without horizontal overflow.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Inline SVG sparkline renderer | Hermes dashboard skills target Hermes dashboards, not gecko-alpha React. | Build pure-functional in-repo component. |
| Price-path API field | No Hermes skill owns gecko-alpha's `/api/todays_focus` row contract. | Extend in-repo with PRAGMA-runtime contract validation. |
| Density-floor decision | No reusable primitive. | Pin in this plan, enforce server-side. |

Awesome-hermes ecosystem: no diagnostic plugin owns gecko-alpha's row shape. Custom repo-local work warranted.

## Drift / Runtime Findings (master @ `d0c7451b`)

- Source data: `volume_history_cg(coin_id, price, recorded_at)` per `scout/db.py:863-875`. Writer prunes >7d at `scout/spikes/detector.py:55-57`. Watcher cadence produces 5-22 points/hr per token (audit snapshot 2026-05-28T22:30:15Z).
- Audit snapshot: 100% join_rate (paper + tracker), 125-547 points / 24h per row. All 5 alternate price+timestamp tables present but excluded from this PR's scope (consistent with audit's source-of-truth pin).
- Existing /api/todays_focus contract firewall: `scripts/check_todays_focus_contract.py` has `EXPECTED_ROW_KEYS`, `FORBIDDEN_KEYS`, `FORBIDDEN_FIELD_PATTERNS`, `BANNED_PATTERNS`. PR-C adds `price_path_points` to expected keys + adds a numeric-pair structural check + requires the meta flag.
- Existing dashboard helpers: `dashboard/frontend/todayFocusFacts.js`, `todayFocusAge.js`, `todayFocusLinks.js`, `todayFocusStorage.js`. No existing sparkline primitive; new `Sparkline.jsx` is the only new frontend module.

## Universe Pin (pinned discipline; no implementer drift)

- **Density floor:** `len(points) >= 12` (inclusive at 12) valid price points in the last 24h. Rationale: below 12, the polyline interpolation gap exceeds 2h and the visual misrepresents price continuity (structural justification, not coverage judgment).
- **Lookback window:** exactly 24h relative to server-side `now`. Configurable via query param `sparkline_lookback_hours` (default 24, max 168 to match writer retention; rejection on out-of-range, same as audit).
- **Valid point:** `volume_history_cg` row with matching `coin_id` AND `recorded_at >= cutoff` AND `price IS NOT NULL` AND `price > 0` AND `price < 1e308` (Infinity guard).
- **Server-side cutoff:** captured ONCE per `/api/todays_focus` request at the top-level caller (NOT per row); threaded into `_today_focus_row` as a parameter. Reviewer B N4 fold. Matches audit's clock-source-pin discipline.
- **ISO → unix timestamp conversion:** `int(datetime.fromisoformat(recorded_at).timestamp())`. Unparseable ISO → skip the point silently (do NOT abort the row). Matches audit's defensive behavior at `audit_price_path_coverage.py:92-96`. Reviewer B N5 fold.
- **Sparkline rendering edge cases** (Reviewer B N8 fold):
  - Empty points / missing prop → component returns `null` (parent renders fallback).
  - Single point (n=1) → returns `null` (cannot draw a line with one point; parent renders fallback).
  - All-same-price (degenerate y-range) → horizontal `<polyline>` at `y = height / 2`. Uniform stroke.
  - Exactly 12 points at floor → renders normally.
- **Mobile breakpoint** (Reviewer B N10 fold): at `@media (max-width: 480px)`, sparkline cell stays inline (80px × 24px is small enough to fit alongside other chips on 375px portrait). Mobile-fallback "collapse to text" is NOT used; the SVG fits. Verified by layout test asserting no horizontal overflow with 5 rows.
- **Per-row field semantics:**
  - When density ≥ 12: `price_path_points: [[unix_ts: int, price: float], ...]` ordered by `recorded_at` ascending. Always a list of 2-element numeric pairs.
  - When density < 12: field is **omitted entirely** from the row (NOT empty array — absence signals "unavailable"). Client renders factual fallback.
  - When source data join fails (paper rows with contract-address token_id): field omitted, same fallback.
- **Meta flag:** `meta.sparkline_is_visual_price_history_only: true` MUST be present on any payload that includes at least one row with `price_path_points`. Contract firewall enforces this invariant. Flag value never `false`; if no sparklines, flag is omitted entirely (no payload assertion).

## Scope

Build:

1. **Server-side (`dashboard/db.py`)**: extend `_today_focus_row` to:
   - Query `volume_history_cg` for last 24h points per row (coin_id-keyed; paper rows attempt direct token_id match same as the audit).
   - Apply density floor: only include `price_path_points` if count ≥ 12.
   - Set `meta.sparkline_is_visual_price_history_only = True` when any row has the field.
2. **Sparkline component (`dashboard/frontend/components/Sparkline.jsx`)**: pure functional SVG renderer:
   - Props: `{ points: [[ts, price], ...], width: 80, height: 24 }`.
   - Normalizes y-axis to point range; x-axis to time range.
   - Renders single `<polyline>` with uniform stroke color (no green-for-up / red-for-down).
   - No tooltip, no axis labels, no fills, no gradients.
   - Returns `null` if `points` array empty/missing (parent handles fallback render).
3. **TodayFocusPanel wiring (`dashboard/frontend/components/TodayFocusPanel.jsx`)**: render `<Sparkline>` after `.todays-focus-detected` cell. If `row.price_path_points` is missing/empty, render `<span className="todays-focus-sparkline-unavailable">Sparkline unavailable</span>` instead.
4. **CSS (`dashboard/frontend/style.css`)**:
   - `.todays-focus-sparkline`: inline-block, fixed 80px × 24px, no fill, uniform stroke via CSS variable.
   - `.todays-focus-sparkline-unavailable`: same font-size + color as other meta chips (uniform style, no italics, no warning color).
   - 480px portrait: sparkline stays inline OR collapses to one-line text fallback (whichever fits; no overflow).
5. **Contract firewall (`scripts/check_todays_focus_contract.py`) — Reviewer B B1 + B2 folds**:
   - Introduce `OPTIONAL_ROW_KEYS: frozenset[str] = frozenset({"price_path_points"})`. The existing `_check_exact_keys` (or its caller) must be modified to subtract `OPTIONAL_ROW_KEYS` from the `missing` set before reporting critical. `unknown` set unchanged: keys not in `EXPECTED_ROW_KEYS ∪ OPTIONAL_ROW_KEYS` still fail.
   - Introduce `OPTIONAL_META_KEYS: frozenset[str] = frozenset({"sparkline_is_visual_price_history_only"})`. Same treatment for `_check_meta`.
   - Post-walk conditional: after row+meta validation, if ANY row has `price_path_points`, assert `meta["sparkline_is_visual_price_history_only"] is True` (identity check, not truthiness). Failure: critical, message includes which rows had the field.
   - Validate `price_path_points` shape when present: must be a list; each element must be a 2-element list `[int_ts, float_price]`; `int_ts` must be `isinstance(int)` AND positive; `price` must be `isinstance(int | float)` AND `> 0` AND `< 1e308`. Reject any other type. Error message pinned: `f"{path}.price_path_points[{i}] must be [int_ts, positive_finite_number]; got {pair!r}"`.
   - Add to BANNED_PATTERNS: `r"Sparkline unavailable[:\-]"` (rejects suffixed variants of the fallback string).
6. **Tests** (folds A-B1/B2/B3/B4, A-N4, B-N3/N4/N5/N8):
   - Server-side: row with sufficient density → `price_path_points` present; below floor → field omitted entirely; mixed cohort → meta flag set; all-rows-below-floor → meta flag absent.
   - Contract firewall:
     - Payload with valid pairs + meta flag `True` passes.
     - Payload with sparkline pairs but missing meta flag fails as critical.
     - Payload with meta flag set to `False` fails (strict True required).
     - Payload with meta flag set to `1` (truthy non-bool) fails — identity check, not truthiness.
     - Payload with meta flag set to `"true"` (string) fails.
     - Payload with string inside `price_path_points` element fails.
     - Payload with 3-element list in `price_path_points` fails (must be exactly 2 elements).
     - Payload with negative price in pair fails.
     - Payload with `int_ts = 0` or negative fails (must be positive).
     - "Sparkline unavailable: data thin" anywhere fails BANNED_PATTERNS check.
   - Sparkline component (using JSDOM or similar via `_run_node`):
     - Renders SVG with `<polyline>` and correct points string for 12+ valid points.
     - Returns null for `points=[]`, `points=undefined`, `points=null`, `points=[[t,p]]` (single point).
     - All-same-price renders horizontal line at `y=height/2`.
     - SVG source contains NO `<text`, `<tspan`, `<title`, `<desc`, `<foreignObject`, `<circle`, `<rect`, `<ellipse`, `<marker`, `<path` substrings (strict bans).
     - `aria-label` attribute equals exactly `"Sparkline"` (no other extension permitted).
   - Fallback component:
     - `textContent === "Sparkline unavailable"` (strict equality).
     - `aria-label === "Sparkline unavailable"` (strict equality).
   - Layout: JSX layout test asserts new import + className markers (`todays-focus-sparkline`, `todays-focus-sparkline-unavailable`); CSS contains new classes + 480px media query handles them without horizontal overflow.
   - Dist freshness: bundle contains `Sparkline` component literal + className strings + `Sparkline unavailable` literal.

## Anti-Scope (firewall-equivalent at plan level)

1. **No trend labels in copy or aria-text.** "Sparkline unavailable" is the only string the user-facing sparkline area can produce.
2. **`aria-label` strict-pinned to exactly two literal strings** — `"Sparkline"` (when SVG renders) OR `"Sparkline unavailable"` (when fallback renders). Test asserts strict equality (Reviewer A B3 fold). No extension permitted.
3. **"Sparkline unavailable" string strict-pinned** — test asserts `textContent === "Sparkline unavailable"` (Reviewer A B4 fold). Contract firewall BANNED_PATTERNS adds `r"Sparkline unavailable[:\-]"` to reject suffixed variants (`Sparkline unavailable: data thin`, `Sparkline unavailable - low density`).
4. **SVG geometry strictly limited to `<polyline>`** (Reviewer A B1 + B2 fold). Explicit bans:
   - No `<text>`, `<tspan>`, `<title>`, `<desc>`, `<foreignObject>` (text injection)
   - No `<circle>`, `<rect>`, `<ellipse>`, `<marker>`, `<path>` (endpoint markers / additional geometry)
   - Test asserts `Sparkline.jsx` source contains NONE of: `<text`, `<tspan`, `<title`, `<desc`, `<foreignObject`, `<circle`, `<rect`, `<ellipse`, `<marker`, `<path` substrings.
5. **No directional color coding.** Single uniform stroke color regardless of up/down. No green-for-up / red-for-down.
6. **No fill / no gradient / no animation.** Polyline-only, static SVG.
7. **No ranking by sparkline shape.** Frontend MUST NOT re-sort rows by slope, volatility, or any shape-derived metric.
8. **No row-CSS-class modifier derived from sparkline values** (Reviewer A N3 fold). No `.has-sparkline`, `.sparkline-rising`, `.sparkline-falling` row modifier classes. The presence/absence of the sparkline cell does not change the parent row's class list.
9. **No interaction with sparkline beyond viewing.** No click-to-expand-larger-chart, no hover-tooltip-with-data.
10. **Contract firewall enforces strict-boolean-true on meta flag** (Reviewer A N4 fold). `sparkline_is_visual_price_history_only` must be Python literal `True` — NOT `1`, `"true"`, `1.0`, or any truthy non-boolean. Firewall asserts `value is True` (identity check), not `bool(value)`.
11. **No backend schema changes** beyond `/api/todays_focus` row contract extension (adding `price_path_points` field, optional).
12. **No new tables / no migration / no backfill** (audit confirmed coverage).
13. **No alert dispatch / no Telegram / no TG sparkline.**
14. **No mobile-specific data shape.** Same payload; CSS handles 375px responsively.

## Merge Gate

PR merges only when ALL three hold:
1. CI green.
2. All three orthogonal PR reviewers (product/anti-scope, frontend/SVG/mobile, contract/test/dist-drift) return zero findings OR only non-blocking findings.
3. Every blocking finding folded and reviewer re-confirms or new-commit verification passes.

Smoke after deploy:
1. Render Today's Focus on srilu dashboard.
2. Verify each row shows either inline sparkline OR factual `Sparkline unavailable` fallback (no row shows nothing in the sparkline cell).
3. Aggregate dashboard contract firewall returns 0 criticals: `scripts/check_dashboard_contracts.py --url http://127.0.0.1:8000 --window-hours 36 --verbose`.
4. 375px portrait: 5 rows with sparklines render without horizontal overflow.
5. Inspect HTML: each `<svg>` has `<polyline>` with at least 12 points (matches density floor).
6. Re-run `scripts/audit_price_path_coverage.py` and verify coverage rates unchanged.
