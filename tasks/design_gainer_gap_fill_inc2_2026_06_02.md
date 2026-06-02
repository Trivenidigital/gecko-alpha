**New primitives introduced:** `fetch_deep_volume` CoinGecko ingestion lane (rotating deep volume_desc page, $500K-$10M focus) reallocated from the disabled midcap lane; `COINGECKO_DEEP_VOLUME_*` config keys.

# Gainer Gap-Fill Increment 2 — proactive $500K-$10M coverage (2026-06-02)

Fills the 32 TRUE coverage gaps left after Increment 1 (PR #356/#357). Evidence:
`tasks/findings_missed_gainers_gap_2026_06_02.md`. Operator directive: fill the gaps, build the
infra, $200M ceiling, autonomous, coordinate with Codex (xhigh).

## Hermes-first analysis (Section 7b)
| Domain | Hermes skill found? | Decision |
|---|---|---|
| crypto market scanning / CoinGecko ingestion | none (CoinGecko Agent SKILL is API reference, not a runtime breadth writer) | build custom (retune existing lane) |
| budget-constrained API page scheduling | none found | build custom |

awesome-hermes-agent + Hermes optional-skills catalog: blockchain helpers but no CoinGecko breadth /
ingestion path. Verdict (Codex-confirmed): retune the existing custom CG ingestion — the volume-scan
primitive already exists; this reallocates + deepens it. Residual gap, not a rebuild.

## Problem (prod-grounded)
The 32 residual gaps are $500K-$10M tokens — the HOLE between the scorer corpus (`MAX_MARKET_CAP`
$500K, DEX new-pools) and the midcap lane (`COINGECKO_MIDCAP_SCAN_MIN_MCAP` $10M, CG). They pump
+20%/24h with ZERO pre-pump `volume_history_cg` because:
- `fetch_by_volume` (`coingecko.py:288`) covers top-750 by volume (any mcap >= `MIN_MARKET_CAP`
  $10K) — but the gaps' PRE-pump volume is not top-750, so they are never fetched early.
- The midcap lane (`coingecko.py:397`, `market_cap_desc` ranks 251-1000, requires `+25%/24h`,
  starts at $10M) is REACTIVE, excludes $500K-$10M, and is low-yield (prod: 6 returns / 6h).
- BINDING CONSTRAINT: CG budget saturated — prod shows **123 429/backoff events / 30 min**, cycles
  78-145s, ingestion-bound. Lanes break early on backoff. So Increment 2 CANNOT add net pages.

## Solution — page-NEUTRAL reallocation (Codex xhigh, folded)
Replace the reactive midcap lane with a PROACTIVE rotating deep-volume page:
- **`fetch_deep_volume`** (`coingecko.py`): `order=volume_desc`, **ONE extra page per cycle rotating
  START..END (4 -> 5 -> 6)** — ranks ~750-1500 by volume. Budget `+1/cycle`, funded by disabling the
  midcap lane (`3 pages / 3 cycles = -1/cycle avg`) -> **net 0/cycle**, and SMOOTHER than a 3-page
  burst (which is what a "pages 4-6 every cycle" or "3 pages every 3rd cycle" design would do —
  rejected on Codex's budget math: pages-4-6-every-cycle is +3/cycle, not neutral).
- Accepted rows feed BOTH `volume_history_cg` (raw rows -> `_combine_coin_market_rows`
  (`main.py:815`) -> `record_volume` -> the gainer_acceleration detector + the gainers tracker get
  pre-pump history) AND **candidates** (the tracker credits pipeline-surface lead via
  `candidates.first_seen_at`, `tracker.py:215`). Tight filters bound blast radius — every accepted
  token also reaches scoring/upsert, but CG-listed micro-caps in this band score ~0.
- **Disable the midcap lane** (`COINGECKO_MIDCAP_SCAN_ENABLED=False`) — the paired reallocation. Per
  Codex: ship both together so the gap-fill flag can default ON; if midcap is NOT disabled, the new
  flag must default OFF.

## Why volume_desc-deep, not mcap_desc-deep
A $500K-$10M token ranks ~2000-8000 by MCAP -> `market_cap_desc` would need pages 8-30 (infeasible).
But a token about to pump shows RISING VOLUME first, so it climbs the VOLUME ranking into pages 4-6
BEFORE it pumps. (Caveat from the May-14 audit: some $10M+ low-volume midcaps are not top-1000 by
volume, so this is not a universal gainer fix — it is the right first probe for the $500K-$10M
zero-history gap.)

## Config (`COINGECKO_DEEP_VOLUME_*`)
ENABLED (default True), START_PAGE (4), END_PAGE (6) [rotates START..END, +1 page/cycle], MIN_MCAP
($500K), MAX_MCAP ($10M gap-fill target; configurable up to the $200M hard cap), MIN_VOLUME ($100K),
MIN_VOL_MCAP_RATIO (0.03), MIN_24H_CHANGE (3.0 — proactive, NOT 20/25 which is already reactive),
MAX_TOKENS_PER_CYCLE (75, sorted by vol/mcap then 1h then 24h). `COINGECKO_MIDCAP_SCAN_ENABLED` ->
False (paired reallocation).

## Soak (Section 11a — data-bound, Codex gate)
Promotion gate as n=X fires, not days: **n=20** qualifying post-deploy top-gainer fires in the
$500K-$10M band. **Success: >= 6/20** have STRICT pre-gainer detection — `first_seen_at <
appeared_on_gainers_at` with NO +5min tolerance, and no earlier non-gapfill surface already explains
the catch. **Budget guard:** `cg_429_backoff/hour` and `ingestion_s` p95 not worse than pre-deploy by
> 10-15%. Measure with `scripts/audit_missed_gainers.py` + the gainers tracker. Revert if missed:
`COINGECKO_DEEP_VOLUME_ENABLED=False` + re-enable midcap.

## Risks / rollback
Page-neutral (no budget increase; smoother burst profile). Reversible per-flag (the paired profile).
Precision: deeper volume adds lower-quality tokens that reach scoring, but the tight band/volume/
ratio/change filters + 75/cycle cap bound it, and CG micro-caps score ~0 (no MiroFish/alert/paper).
**Honest ceiling:** gaps whose pre-pump volume ranks below ~1500 still need DEX new-pools / social —
not reachable from CG within budget. The deep lane is observability-first; the budget-regression
guard in the soak gate is the kill-switch trigger.

## Codex xhigh review: FOLDED (2026-06-02)
Rotating-single-page (not pages-4-6-every-cycle) + candidates-feed + the filter set + the n=20 / >=6
soak gate are all Codex dispositions, accepted. Drift + Hermes-first negative confirmed by Codex.
