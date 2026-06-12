**New primitives introduced:** `scout/conviction/` module + `cross_surface_conviction()` pure scorer + `ConvictionResult` dataclass; `/api/conviction/shortlist` read-only endpoint; `CONVICTION_*` config keys. (Phase 2 dashboard panel + Phase 3 alert are deferred follow-ups, NOT in this PR.)

# Cross-Surface Conviction Score (BL-NEW-CROSS-SURFACE-CONVICTION-SCORE) — Design 2026-06-12

Turns the noisy gainers firehose into a ranked winner shortlist by scoring how many **independent detectors confirmed a coin EARLY** (≥24h before it crossed +20%/24h). Read-only, observe-first.

## Hermes-first analysis (§7b)
| Domain | Hermes skill found? | Decision |
|---|---|---|
| cross-signal confirmation / ensemble ranking | none (skill hub is durable-memory/scheduling/routing, not detection-scoring) | build custom |
| crypto winner ranking / conviction scoring | none found | build custom |

awesome-hermes-agent ecosystem: no signal-ensemble / conviction-ranking capability. Per `backlog.md` Hermes-first stance, detection/scoring over our own DB is the custom layer. **Verdict: build custom** (pure function over existing `gainers_comparisons` flags; no external lib fits).

## Evidence (srilu, full history 2026-04-15 → 2026-06-12, 723 tracked gainers)
The system is **not** missing winners — it ranks them poorly. Recall is high (chains caught 40/42 three-baggers = 95%, slow_burn 31, momentum 29); precision is the problem (slow_burn fires on 286 coins to catch 31; 42 winners hide among 723 = 5.8%).

**The dominant, validated discriminator is the count of independent surfaces that confirmed a coin ≥24h BEFORE the move** (predictive, not coincident — for the 42 winners, 3.26 of 3.52 confirming surfaces fired ≥24h early):

| # surfaces confirmed ≥24h early | tracked | 3x+ | win-rate |
|---|---|---|---|
| 0–1 | 316 | 2 | ~1% |
| 2–3 | 276 | 12 | ~5% |
| ≥4 | 131 | 28 | **21%** |

A `≥2-early-surface` filter keeps 85/93 (91%) of 2x winners and 18/19 of 5x at 21% precision; a `≥4-early` shortlist is 131 names (~2/day), 21.4% precision, 67% recall of 3x — a **3.7× precision lift** over the firehose with no recall loss vs single-signal tightening (which would drop winners that share per-signal thresholds).

## Scope (Phase 1 — this PR): pure scorer + read-only endpoint
### `scout/conviction/cross_surface.py`
- `SURFACE_LEAD_COLUMNS`: maps the 8 surfaces → their `*_lead_minutes` column.
- `cross_surface_conviction(row, settings) -> ConvictionResult` — **pure** (no DB/IO). Counts surfaces where `detected_by_<s>` is truthy AND `<s>_lead_minutes` is non-null AND `>= CONVICTION_EARLY_LEAD_MINUTES`. `score` = Σ per-surface weight (equal 1.0 in v1; weight hook reserved). `tier` = `high` (≥`CONVICTION_HIGH_TIER_MIN_SURFACES`) / `watch` (≥`CONVICTION_WATCH_TIER_MIN_SURFACES`) / `low`. Returns `early_count`, `score`, `tier`, `contributing` surfaces.
- Null/missing leads and missing columns degrade to "not early" (never raise). Uses a safe `_row_get` (KeyError/IndexError → None) so it works on sqlite `Row` and `dict`.

### `/api/conviction/shortlist` (dashboard, read-only, additive)
Reuses `get_gainers_comparisons()` (already returns the 8 flags + leads), scores each row, returns rows sorted by `(score desc, peak_gain_pct desc)` with `early_count`/`score`/`tier`/`contributing`. Query params `limit` (1–500), `min_tier` (low|watch|high). No new DB read, no write, no schema change.

### Config (`scout/config.py`)
- `CONVICTION_SCORE_ENABLED: bool = True` — read-only surface, safe-on (no alerts/trades).
- `CONVICTION_EARLY_LEAD_MINUTES: int = 1440` — the "early" threshold (24h).
- `CONVICTION_HIGH_TIER_MIN_SURFACES: int = 4` / `CONVICTION_WATCH_TIER_MIN_SURFACES: int = 2` — tier gates (validated boundaries).

### Tests
Scorer: 0/1/2/4/8 early surfaces → correct count/tier; lead exactly at threshold (inclusive); null lead with `detected=1` does NOT count; missing column degrades; `dict` and `Row` inputs; weight/tier config overrides. Endpoint: shape, sort order, `min_tier` filter, empty result, contributing list.

## Deferred (separate follow-up BLs, NOT this PR)
- **Phase 2 `BL-NEW-CONVICTION-DASHBOARD-PANEL`** — a Today's-Focus "Conviction Shortlist" panel over the endpoint (frontend; dist-commit discipline applies).
- **Phase 3 `BL-NEW-CONVICTION-HIGH-TIER-ALERT`** — a high-tier TG alert, gated + observe-first, only after the forward-outcome measurement confirms the live precision matches the backtest.

## Anti-scope
No new detector; no change to any signal's thresholds (precision via ranking, NOT noise-cutting — proven to lose recall); no paper-trade/alert in this PR; no schema change; no write path. Orthogonal to the deep-volume coverage lane (#358) which addresses recall/coverage — this addresses precision/ranking.

## Honest limits
n is modest (42 3x / 93 2x); treat the *ordering* as solid, exact rates ±. Measures only the **caught** set (coins that entered our universe). `peak_gain_pct` is forward-PEAK, not captured PnL — the score surfaces candidates; capture needs entry/exit discipline. The score must be measured against forward outcomes in observe mode before any alert (Phase 3) is enabled.
