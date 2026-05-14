**New primitives introduced:** NONE - findings/report artifact only.

# Top Gainers Gap Report - 2026-05-14

## Question

For the Top Gainers screenshot cohort, which tokens did gecko-alpha catch as
signals, which did it miss, and what is the actual data-path gap?

Scope: signal detection only. Paper-trade policy and Telegram alerts are out of
scope except where paper trades prove a signal existed.

## Drift + Hermes-First

| Domain | Existing/Hermes capability found? | Decision |
|---|---|---|
| Existing CoinGecko signal tables | Yes: `gainers_snapshots`, `trending_snapshots`, `momentum_7d`, `slow_burn_candidates`, `volume_spikes`, `price_cache`, `paper_trades`. | Audit existing rows first; no new detector claim from screenshots alone. |
| CoinGecko breadth/hydration | PR #121 just shipped: trending hydration + configurable top-volume breadth. | Treat as future mitigation for trending-only misses, not retroactive evidence. |
| X/KOL/TG social paths | Existing Hermes X and custom TG social are separate narrative/curator signals. | Not used to explain CoinGecko Top Gainers ingestion gaps. |
| Mid-cap market-rank scan | No installed Hermes runtime skill. CoinGecko Agent SKILL/API docs can guide endpoint use. | Candidate follow-up if we decide to scan rank bands sorted by market cap and compute local 24h gainers. |

One-sentence verdict: the miss pattern is not an X/social gap; it is a
CoinGecko market-breadth gap for mid-cap, low-volume gainers outside both
volume-ranked and trending feeds.

## Exact CoinGecko IDs Used

The audit used exact CoinGecko IDs from `/search`, not fuzzy names:

| Screenshot name | Exact CoinGecko ID | Symbol |
|---|---|---|
| Playnance | `playnance` | GCOIN |
| Kishu Inu | `kishu-inu` | KISHU |
| OpenServ | `openserv` | SERV |
| Gensyn | `gensyn` | AI |
| Bityuan | `bityuan` | BTY |
| Quack AI | `quack-ai` | Q |
| SAFEbit | `safecoin` | SAFE |
| TROLL | `troll-2` | TROLL |

Important correction: fuzzy name search matched `playsout` / PLAY and made
Playnance look caught. That is wrong. The screenshot token is `playnance` /
GCOIN; it has no detector rows.

## Coverage Summary

Production DB: `/root/gecko-alpha/scout.db`, checked 2026-05-14.

| Token | Coverage class | First detector row | Signal/trade evidence |
|---|---|---|---|
| TROLL | Caught | `momentum_7d` 2026-05-03; `gainers_snapshots` 2026-05-07; `trending_snapshots` 2026-05-10 | 6 paper trades; best `gainers_early` +122.1% PnL / +123.2% peak. |
| Quack AI | Caught | `gainers_snapshots` 2026-05-08; `slow_burn` 2026-05-10; `momentum_7d` 2026-05-13 | 6 paper trades; best `gainers_early` +56.9% PnL. |
| Gensyn | Caught | `trending_snapshots` 2026-05-08; `gainers_snapshots` 2026-05-14 | 4 paper trades; one `losers_contrarian` +16.0%; one open `gainers_early`. |
| OpenServ | Caught | `momentum_7d` + `slow_burn` 2026-05-14 06:46; `gainers_snapshots` 09:22 | 1 `gainers_early`, +6.28% closed. |
| Kishu Inu | Partial | `trending_snapshots` 2026-05-11 | No gainer/momentum/slow-burn/paper trade rows. PR #121 should improve future trending-only hydration. |
| Playnance | Missed | none | Exact `playnance` has only one `price_cache` row; no detector rows. |
| Bityuan | Missed | none | No rows in audited detector tables. |
| SAFEbit | Missed | none | No rows in audited detector tables. |

## Current Market Shape of Misses

Current CoinGecko `/coins/markets` snapshot during audit:

| Token | Rank | 24h | 7d | Volume | Why current ingestion misses |
|---|---:|---:|---:|---:|---|
| Playnance | 520 | +96.4% | +407.5% | $0.84M | Mid-cap rank; not top-1000 by volume in sample; not trending. |
| Bityuan | 472 | +18.5% now, +30% in screenshot | +98.5% | $0.55M | Mid-cap rank; not top-1000 by volume in sample; not trending. |
| SAFEbit | 683 | +34.6% | +47.4% | $0.86M | Mid-cap rank; not top-1000 by volume in sample; not trending. |
| Kishu Inu | 680 | +71.9% | +238.9% | $1.14M | Was trending, but pre-PR #121 trending rows did not feed raw-market signal surfaces. |

The misses are not dust and not obscure contract-address-only memes. They are
CoinGecko-listed mid-caps with market-cap rank roughly 470-680 and low volume
relative to top-volume pages. A `volume_desc` scan cannot reliably catch them,
even at top-750. They need either a market-rank-band scan or a direct gainer
endpoint. CoinGecko's direct top-gainers endpoint is paid/prohibited by project
rules, so the free-compatible option is local ranking from market-cap pages.

## Root Causes

### RC1 - Top-volume breadth does not cover low-volume mid-cap gainers

`fetch_by_volume` historically covered top-500 by volume; PR #121 raises the
default to top-750. The three exact misses were not in the top-1000 by volume
during the audit sample. Increasing volume pages alone is the wrong lever.

### RC2 - Trending-only rows were not raw-market rows before PR #121

Kishu Inu had 8 `trending_snapshots` rows beginning 2026-05-11, but no
gainer/momentum/slow-burn/paper-trade rows. Before PR #121, trending candidates
carried rank but did not feed the raw CoinGecko market surfaces. PR #121 should
fix this class going forward by hydrating trending IDs through `/coins/markets`
and including them in raw-market fan-in.

### RC3 - Fuzzy token matching creates false positives in audits

`Playnance` was initially misclassified because fuzzy matching found `playsout`
/ PLAY. Exact CoinGecko IDs are required for any future screenshot cohort audit.

## Recommendation

Add a follow-up backlog item for a free-tier-compatible mid-cap market-rank
scan:

- Fetch CoinGecko `/coins/markets` ordered by `market_cap_desc` for a bounded
  rank band such as pages 2-4 or 2-5.
- Locally sort by `price_change_percentage_24h`.
- Feed only rows passing quality gates into existing raw-market surfaces:
  minimum 24h gain, minimum volume, max market cap, and dedupe by coin ID.
- Use CoinGecko SKILL/API docs as reference; runtime stays in gecko-alpha.
- Keep default disabled or narrow until a backtest estimates added row/trade
  volume. Quality over quantity.

Suggested name: `BL-NEW-COINGECKO-MIDCAP-GAINER-SCAN`.

## Non-Goals

- No TG alert changes.
- No paper-trade threshold changes.
- No custom X/Twitter/LunarCrush work.
- No paid CoinGecko endpoint.
