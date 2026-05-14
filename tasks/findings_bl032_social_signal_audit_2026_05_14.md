**New primitives introduced:** NONE - audit/findings artifact only.

# BL-032 Social Signal Audit - 2026-05-14

## Question

What should replace the dead `social_mentions_24h` scorer input, and should
gecko-alpha build custom Twitter/LunarCrush code for it?

## Drift Check

| Surface | Existing in tree? | Decision |
|---|---|---|
| Dead scorer input | Yes: `CandidateToken.social_mentions_24h` and `score()` adds 15 points when it is `>50`. | Do not assume it works; verify prod rows before building a source. |
| Telegram curator ingestion | Yes: `tg_social_messages`, `tg_social_signals`, listener, dispatcher, dashboard, and conviction-stack integration. | Keep as project-owned curated Telegram path. |
| X/KOL narrative ingestion | Yes: Hermes dispatcher writes `narrative_alerts_inbound`; dashboard reads X Alerts. | Reuse Hermes path, but do not conflate it with market-wide social volume. |
| LunarCrush custom path | Yes: code/config/tables exist, but default disabled and runtime tables empty. | Do not revive without residual-gap proof and cost review. |

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| X/KOL monitoring | Yes - installed VPS skills include `social-media/xurl`, `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, and `crypto_narrative_scanner`. | Reuse existing Hermes path; do not build custom X/Twitter ingestion. |
| Telegram curated-channel monitoring | No Hermes skill replaces the existing project-specific Telethon DB writer. HermesHub has Telegram-adjacent communication skills, but not this gecko-alpha channel ingestion/dispatch contract. | Keep custom `tg_social_*` path. |
| LunarCrush / generic social-volume feed | None installed on VPS; no credible Hermes skill found that replaces paid social-volume API ingestion. | Defer; third-party social API only after a new residual-gap design. |
| Social confirmation scoring | No skill can directly replace gecko-alpha scoring/normalization. | Bridge existing Hermes/TG rows only after enough production evidence; otherwise remove/recalibrate the dead scorer input. |

Awesome/evolving ecosystem check: the May 14 crypto-skills sweep checked the
official Hermes skill hub, `0xNyk/awesome-hermes-agent`, HermesHub,
CoinGecko skills, GoldRush skills, and PRB agent-skills. Current useful social
coverage is X/KOL via installed Hermes skills; no installed or public skill is
a drop-in market-wide social-volume counter for this scorer field.

One-sentence verdict: BL-032 should not build custom Twitter/LunarCrush now;
the only real sources today are custom Telegram curator rows and Hermes X/KOL
narrative rows, and neither should be mislabeled as full-market social volume.

## Runtime Verification

Production DB: `/root/gecko-alpha/scout.db`, checked 2026-05-14.

| Check | Result |
|---|---|
| `candidates.social_mentions_24h` | 1,543 candidate rows, 0 rows > 0, max 0. |
| `social_signals` legacy LunarCrush table | 0 rows. |
| `social_baselines` | 0 rows. |
| `social_credit_ledger` | 0 rows. |
| `paper_trades` social-ish signals | `narrative_prediction`: 201 rows; `tg_social`: 3 rows. |
| `tg_social_messages` | 1,645 total; 49 in 24h; 421 in 7d; latest 2026-05-14T06:01:20Z. |
| `tg_social_signals` | 743 total; 24 in 24h; 164 in 7d; 3 with paper trades. |
| `tg_social_signals` resolution | 531 `UNRESOLVED_TRANSIENT`, 211 `RESOLVED`, 1 `UNRESOLVED_TERMINAL`. |
| `narrative_alerts_inbound` | 6 total, all in last 24h; 0 resolved so far. |
| X narrative authors | FrostxXBT 3, gem_insider 1, _Shadow36 1, CrashiusClay69 1. |
| X narrative themes | token_launch 3, price_action 1, meme coin 1, meme 1. |
| `.env` state | TG social enabled; narrative scanner secret configured; no LunarCrush env keys found. |

## Findings

### F1 - `social_mentions_24h` is not a weak signal; it is an unwired signal

The field is always zero in production. The 15-point scorer branch has never
had a live writer feeding it. Adding a new paid API would be a custom-code
expansion without first using the live Hermes X path.

Decision: do not build custom social-volume ingestion for BL-032.

### F2 - Telegram rows are active, but they represent curated calls, not volume

Telegram is producing real data: 421 messages and 164 signals in 7 days.
However, those rows mean "curator mentioned/resolved a token", not "market-wide
social mentions exceeded 50". They already have the right first-class identity:
`tg_social` signal, dashboard, dispatcher, and conviction-stack source.

Decision: keep Telegram as `tg_social`; do not backfill it into
`social_mentions_24h`.

### F3 - Hermes X rows are active but not mature enough for scoring

`narrative_alerts_inbound` has 6 rows, all recent, and none resolved. That is
excellent proof the X-side path is alive; it is not enough evidence to change
the quantitative scorer or to claim a reliable social-confirmation feature.

Decision: keep X rows in X Alerts and use them for review/evidence collection
until they reach a data threshold.

### F4 - The dead scorer denominator is now the real residual gap

Since `social_mentions_24h` never fires but is included in `SCORER_MAX_RAW`,
scores are normalized against a 15-point feature that production cannot earn.
Removing it is not behavior-neutral: it raises normalized scores for every
candidate, so it needs backtest/calibration rather than a casual cleanup.

Decision: create a scoped follow-up to backtest removing or replacing the dead
feature before changing scorer behavior.

## Recommendation

1. Mark BL-032 audited and close the "build social API" direction.
2. Keep `tg_social` and X Alerts as separate first-class signals.
3. Add a new follow-up: backtest scorer denominator cleanup for dead
   `social_mentions_24h` (and separately inspect `holder_growth_1h`, which the
   debt audit also flagged as weakly populated).
4. Set an evidence gate before any X/TG bridge into scoring:
   - at least 50 `narrative_alerts_inbound` rows,
   - at least 20 resolved rows OR an explicit symbol-resolution design,
   - and an attribution analysis showing X/TG rows improve early signal quality
     versus existing gainers/trending/momentum surfaces.

## Non-Goals

- No scorer code change in this audit.
- No LunarCrush/Santiment/Twitter API build.
- No deletion of existing LunarCrush code yet.
- No TG alert changes.
