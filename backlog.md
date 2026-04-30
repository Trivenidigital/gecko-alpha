# CoinPump Scout — Backlog

## Priority Legend
- **P0** — Blocking: must complete before first live run
- **P1** — High: significantly improves signal quality or production readiness
- **P2** — Medium: valuable enhancement, not blocking
- **P3** — Low: nice-to-have, future phase

---

## Design Decisions (Locked In)

These decisions were reviewed and approved. Reference them when implementing P1 items.

**D1 — Scoring normalization (UPDATED):** MIN_SCORE lowered to 25 and CONVICTION_THRESHOLD to 22 after observing that the CoinGecko micro-cap universe produces lower raw scores than originally modelled. The 178-point normalization base compresses scores: typical top tokens score 25-35 quant. The original target of MIN_SCORE=60 would require 4+ signals firing simultaneously which rarely happens in current market conditions. Thresholds will be raised as vol_acceleration signal accumulates history (requires 3+ scan cycles) and more data sources come online.

**D2 — Buy pressure fields:** Add `txns_h1_buys: int | None = None` and `txns_h1_sells: int | None = None` to CandidateToken as Optional fields. Parser populates where available (DexScreener `txns.h1.buys` / `txns.h1.sells`). Scorer treats `None` as 0 points for buy pressure — graceful degradation if the field is missing from the API response.

**D3 — Score velocity parameter injection:** Pass `historical_scores: list[float] | None = None` into the scorer as a parameter. Keeps scorer a pure function — no DB access, fully testable. The caller (main.py) does the DB read and passes historical scores in. This is the correct pattern: I/O at the edges, pure logic in the core.

**D4 — Qwen migration order (SUPERSEDED):** BL-001 (Qwen migration) was cancelled — Claude haiku-4-5 via Anthropic SDK retained as fallback scorer. Rationale: user has $200/month Anthropic plan, no need for additional DashScope account. The narrative scoring prompt has been calibrated with a detailed rubric and quantitative context for Claude haiku-4-5 specifically.

**D5 — Implementation order for enhanced scorer:** Execute P1 items in this sequence:
1. BL-011 (buy pressure) — new CandidateToken fields + parser + signal
2. BL-012 (age bell curve) — replace existing signal, no new fields
3. BL-010 (hard disqualifiers) — liquidity floor pre-filter
4. BL-014 (co-occurrence multiplier) — structural scoring change
5. BL-013 (score velocity) — DB table + parameter injection
6. BL-016 (normalization) — adjust scale after all signals added
7. BL-015 (confidence tag) — enriches MiroFish seed last

---

## P0 — Blocking

### BL-001: Migrate fallback scorer from Anthropic to Qwen (OpenAI-compatible)
**Status:** CANCELLED — see D4 (SUPERSEDED)
**Files:** scout/mirofish/fallback.py, scout/config.py, .env.example, tests/test_fallback.py, pyproject.toml
**Why:** User wants Qwen (qwen-plus via DashScope) instead of Claude haiku for the narrative fallback scorer. DashScope uses OpenAI-compatible API.
**Changes needed:**
- Replace `anthropic` SDK with `openai` SDK (async client) in fallback.py
- Add config fields: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_NAME` (replace `ANTHROPIC_API_KEY`)
- Update seed prompt for Qwen's response style
- Update tests to mock OpenAI client instead of Anthropic
- Remove `anthropic` from pyproject.toml dependencies, add `openai`
**Acceptance:** `uv run pytest tests/test_fallback.py -v` passes, dry-run produces narrative scores

### BL-002: Create .env with real API keys and run first live dry-run
**Status:** DONE — live dry-run completed, real API keys configured on VPS
**Files:** .env
**Why:** Pipeline has never been tested against real APIs. Need to verify DexScreener/GeckoTerminal response parsing, Telegram delivery, and end-to-end flow.
**Keys needed:**
- TELEGRAM_BOT_TOKEN: (stored in .env)
- TELEGRAM_CHAT_ID: (stored in .env)
- ANTHROPIC_API_KEY: (stored in .env)
**Acceptance:** `uv run python -m scout.main --dry-run --cycles 1` completes with real tokens fetched, scored, and logged

---

## P1 — Enhanced Scorer (Phase 1: DexScreener-only data)

### BL-010: Add hard disqualifiers (Tier 1 pre-filter)
**Status:** DONE — implemented by offshore devs (liquidity floor in scorer.py:55-57)
**Files:** scout/scorer.py, scout/models.py, tests/test_scorer.py
**Why:** Current scorer has no fraud filter. Wash-traded tokens pass easily.
**Changes:**
- Liquidity floor: auto-discard if `liquidity_usd < $15K` (configurable via Settings)
- Run before any scoring — fail fast, return score=0
- Deployer wallet check deferred to Phase 2 (needs Helius/Moralis)
- Wash trade top-3-wallet check deferred to Phase 2 (needs on-chain data)
**Acceptance:** Tokens with < $15K liquidity get score 0 and never reach MiroFish

### BL-011: Add buy pressure ratio signal (Tier 3)
**Status:** DONE — implemented by offshore devs (buy pressure in scorer.py:104-114)
**Files:** scout/models.py, scout/ingestion/dexscreener.py, scout/scorer.py, tests/
**Why:** Best wash-trade discriminator available from existing API data. DexScreener returns `txns.h1.buys` and `txns.h1.sells` — currently unused.
**Changes:**
- Add `txns_h1_buys: int | None = None` and `txns_h1_sells: int | None = None` to CandidateToken (see Decision D2)
- Parse from DexScreener response in `from_dexscreener()`
- Score: buy_ratio > 65% → +15 points
**Acceptance:** Tokens with skewed buy pressure score higher than balanced volume tokens

### BL-012: Replace binary token age with bell curve (Tier 4)
**Status:** DONE — implemented by offshore devs (age bell curve in scorer.py:83-97)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Current binary `< 7 days = 10 pts` misses the optimal 1-3 day window.
**Changes:**
- 0 pts for < 12h (too early, no liquidity)
- 5 pts for 12-24h
- 10 pts for 1-3 days (peak window)
- 5 pts for 3-5 days
- 0 pts for > 5 days (likely dead)
**Acceptance:** Scoring curve matches spec, existing tests updated

### BL-013: Add score velocity bonus (Tier 2)
**Status:** DONE — implemented by offshore devs (score velocity in scorer.py:149-154)
**Files:** scout/scorer.py, scout/db.py, scout/main.py, tests/
**Why:** A token whose score is rising across consecutive scans indicates active accumulation in progress — the velocity itself is a signal.
**Changes:**
- Add `score_history` table in db.py (contract_address, score, scanned_at)
- Log each score in main.py after scoring
- In scorer: accept `historical_scores: list[float] | None = None` param, award +10 if strictly increasing over last 3 scans (see Decision D3)
- Scorer remains pure (no I/O) — main.py does the DB read and passes historical scores in
**Acceptance:** Tokens with rising scores get bonus, flat/declining scores get nothing

### BL-014: Add co-occurrence multiplier
**Status:** DONE — implemented by offshore devs (co-occurrence multiplier in scorer.py:159-161)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Vol/liq ratio alone is the most commonly gamed signal. Penalize isolated vol/liq without holder growth; bonus when both fire together.
**Changes:**
- After summing all signal points:
  - If `vol_liq_ratio` fired AND `holder_growth` fired → multiply by 1.2×
  - If `vol_liq_ratio` fired WITHOUT `holder_growth` → multiply by 0.8×
- Apply multiplier before returning final score
- Cap final score at 100
**Acceptance:** Wash-traded tokens (high vol, no holder growth) score 20% lower

### BL-015: Add signal confidence tag to MiroFish seed
**Status:** DONE — implemented by offshore devs (signal_confidence function in scorer.py:167-177)
**Files:** scout/mirofish/seed_builder.py, scout/scorer.py, tests/
**Why:** Enriching the MiroFish seed with signal context improves narrative simulation quality.
**Changes:**
- scorer.py returns additional `confidence: str` (HIGH if 3+ tiers firing, MEDIUM if 2, LOW if 1)
- seed_builder.py includes `signal_confidence` and `signals_fired` list in the seed payload
- Update MiroFish prompt to reference the confidence level
**Acceptance:** MiroFish seed contains signal context, tests verify format

### BL-016: Normalize scoring to 125 base → 100 scale
**Status:** DONE — implemented by offshore devs (normalization in scorer.py:156-157, SCORER_MAX_RAW=183)
**Files:** scout/scorer.py, scout/config.py, tests/test_scorer.py
**Why:** New signals (buy pressure +15, velocity +10, revised age curve) push max above 100. Need normalization.
**Changes:**
- Calculate raw sum from all signals (max 125 base)
- Normalize: `final = min(100, int(raw_sum * 100 / 125))`
- Apply co-occurrence multiplier after normalization
- Update MIN_SCORE semantics if needed
**Acceptance:** All scores remain 0-100, tests verify edge cases

---

## P2 — Phase 2 Enhancements (Helius/Moralis required)

### BL-020: Populate holder_growth_1h from enricher
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/ingestion/holder_enricher.py
**Why:** Code review found holder_growth_1h is never populated. The 25-point holder growth signal is dead in production without this.
**Changes:**
- Store previous holder_count in DB per contract_address
- On next scan, compute delta as holder_growth_1h
- Requires at least 2 scan cycles to produce data
~~**Blocked by:** Helius/Moralis API key~~

### BL-021: Add unique buyer wallet count signal (Tier 3)
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/ingestion/holder_enricher.py, scout/models.py, scout/scorer.py
**Why:** Distinguishes organic community buying from bot accumulation.
**Changes:**
- Add `unique_buyers_1h: int = 0` to CandidateToken
- Fetch from Helius (Solana) / Moralis (EVM) transfer history
- Score: high unique_buyers relative to total_txns → +15 pts
~~**Blocked by:** Helius/Moralis API key~~

### BL-022: Add wash trade detection (top-3 wallet volume concentration)
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/scorer.py, scout/ingestion/holder_enricher.py
**Why:** Hard disqualifier — if top 3 wallets account for > 40% of volume, it's almost certainly wash trading.
**Changes:**
- Fetch top wallet transaction data from Helius/Moralis
- Compute concentration ratio
- Disqualify (score 0) if > 40%
~~**Blocked by:** Helius/Moralis API key~~

### BL-023: Add deployer wallet supply concentration check
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/safety.py or scout/scorer.py
**Why:** Classic rug setup — deployer holds > 20% of supply.
**Note:** Partially covered by GoPlus already. Evaluate overlap before implementing.
~~**Blocked by:** Helius/Moralis API key~~

### BL-024: Add transaction size distribution signal
**Status:** DROPPED — user confirmed system is not meme-concentrated, on-chain holder data is for DEX memes which is not the focus
**Files:** scout/ingestion/holder_enricher.py, scout/scorer.py
**Why:** Organic pre-pump = many small txns ($50-$500). Bot wash = fewer large uniform txns.
~~**Blocked by:** Helius/Moralis API key~~

---

## P2 — Infrastructure & Reliability

### BL-030: Add Solana chain bonus to scorer (Tier 4)
**Status:** DONE — implemented by offshore devs (Solana bonus in scorer.py)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Diagram shows +5 pts for Solana chain (meme premium). Solana has disproportionate meme coin activity.
**Changes:** Simple conditional: `if chain == "solana": +5 pts`

### BL-031: Add market cap tier curve (Tier 4)
**Status:** DONE — implemented by offshore devs (mcap tier curve in scorer.py)
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Current binary $10K-$500K gate misses the sweet spot. Diagram shows $10K-$100K as peak score, tapering to $500K.
**Changes:** Graduated scoring: 8 pts for $10K-$100K, 5 pts for $100K-$250K, 2 pts for $250K-$500K

### BL-032: Populate social_mentions_24h signal
**Status:** Not started — currently dead signal (15 pts never fire)
**Files:** scout/ingestion/, scout/models.py
**Why:** Code review found social_mentions_24h is never populated.
**Note:** PRD defers Twitter/X integration to Phase 5. Could use free Telegram channel monitoring or LunarCrush API as interim source.
**Blocked by:** Social data source decision (LunarCrush is an option at $24/mo)

### BL-033: Add heartbeat logging every 5 minutes
**Status:** DONE — heartbeat logging implemented (PR #7)
**Files:** scout/main.py
**Why:** PRD requires heartbeat log showing: tokens scanned, candidates promoted, alerts fired, MiroFish jobs today.
**Changes:** Track cumulative stats, log summary every 5 min (or every N cycles)

### BL-034: Set up MiroFish Docker integration
**Status:** DROPPED — Claude Haiku fallback is sufficient, gate lowered to MIN_SCORE=25
**Files:** docker-compose.yml, scout/mirofish/client.py
**Why:** MiroFish is the key differentiator but hasn't been tested locally yet. Currently all narrative scoring goes through the fallback.
**Changes:** Clone MiroFish repo, configure LLM keys, test /simulate endpoint, verify seed format compatibility

---

## P2 — BL-064 follow-ups (TG social signals deployed 2026-04-27)

### BL-065: Dispatch paper trades from cashtag-only resolutions
**Status:** Not started — flagged 2026-04-29
**Files:** `scout/social/telegram/listener.py` (cashtag-only branch ~L249-276), `scout/social/telegram/dispatcher.py`, `scout/social/telegram/resolver.py` (search-top-3 path), schema (`tg_social_channels` add column), tests
**Why:** Today, when a curator posts only `$EITHER` (cashtag) without a contract address, BL-064 sends a Telegram alert with top-3 CoinGecko candidates but **never** dispatches a paper trade — `listener.py:249` returns before `dispatch_to_engine`. With the active trade-eligible curators (`@thanos_mind`, `@detecter_calls`) currently posting cashtag-only hype, this means BL-064 has dispatched zero trades despite the listener being healthy. Extending dispatch to cashtags would unlock the bulk of curator activity.
**Design decisions to make:**
- **Candidate selection** — top-1 by mcap? Top-1 with minimum mcap floor (e.g. $1M to skip dead tickers)? Top-1 with confidence-margin gap over #2? Reject if top-3 is ambiguous (small mcap spread)?
- **Safety** — current path skips GoPlus on cashtags (no CA to query). Either: (a) fetch CA from CoinGecko candidate before safety check, (b) allow cashtag dispatches per-channel via new `cashtag_trade_eligible` flag (mirrors `safety_required`), (c) require both `trade_eligible=1 AND safety_required=0` to opt in.
- **Per-channel opt-in** — separate column `cashtag_trade_eligible` on `tg_social_channels` so we can enable for the trusted-curator subset without auto-enabling the alert-only ones.
- **Trade size** — same `PAPER_TG_SOCIAL_TRADE_AMOUNT_USD=300` as CA path, or smaller given lower confidence (e.g. $150)?
- **Dedup with CA path** — if the same curator later posts the CA for the same token (cashtag→CA upgrade path), do we open a second trade? Probably no — same `_has_open_tg_social_exposure` check already covers it once we resolve the cashtag to a coin_id.
**Acceptance:** Post a `$<CASHTAG>` message in a channel marked `cashtag_trade_eligible=1`, verify a paper trade opens with `signal_type=tg_social`, `signal_data` carries `{"resolution": "cashtag", "cashtag": "$X", "candidate_rank": 1, "candidates_total": 3}`, and the existing alert-only channels remain alert-only.
**Estimate:** 0.5-1 day with tests.

### BL-066: Dashboard view for BL-064 activity (channels, messages, alerts)
**Status:** Not started — flagged 2026-04-29
**Files:** `dashboard/api.py` (new `/api/tg_social/*` endpoints), `dashboard/db.py`, `dashboard/frontend/components/` (new TGSocial section), `dashboard/frontend/main.jsx` (add tab or section)
**Why:** BL-064 has been live since 2026-04-27 with 1,019 messages ingested, 487 signals parsed, 395 in DLQ — and there is currently **zero dashboard visibility** into any of it. Operators have to SSH to the VPS and run sqlite queries to see channel activity. The Telegram alert channel that was supposed to be the primary visibility surface is non-functional because the bot token is a placeholder. Until the token is fixed, the dashboard is the only realistic visibility surface.
**Endpoints to add:**
- `GET /api/tg_social/channels` — list configured channels with `trade_eligible`, `safety_required`, last_seen_msg_id, last_message_at, listener_state
- `GET /api/tg_social/messages?limit=20` — recent messages with cashtags/contracts extracted, has_ca flag
- `GET /api/tg_social/signals?limit=20` — recent resolved signals + which message they came from, dispatch outcome
- `GET /api/tg_social/dlq?limit=20` — recent DLQ entries with error, channel, message preview
- `GET /api/tg_social/stats` — totals: messages last 24h by channel, resolution success rate, dispatch rate, DLQ rate
**Frontend:** new "Social" tab (or section in Health tab) showing channel health, recent messages, recent signals, DLQ count, link to full DLQ detail.
**Acceptance:** Operator can open dashboard, see at a glance: are listeners running? are messages flowing? what's in DLQ? did a trade dispatch?
**Estimate:** 0.5-1 day backend + 0.5 day frontend.

### BL-067: Conviction-locked hold — extend exit gates when independent signals stack on the same token
**Status:** **RESEARCH-GATED — DO NOT IMPLEMENT YET.** Requires backtest + design decisions documented below before any production code lands.
**Tag:** `research-gated` `strategy` `multi-signal` `requires-backtest`
**Files (when implementation starts):** `scout/trading/evaluator.py`, `scout/trading/conviction.py` (new), `scout/db.py` (signal-stack lookup), `scout/trading/params.py` (per-signal opt-in column), tests.
**Why — the BIO case study (2026-04-30):** BIO (`bio-protocol`) was caught across **5 independent signal surfaces over 7 days** (`first_signal` → `gainers_snapshots` → `trending_snapshots` → `losers_contrarian` on dip → `narrative_prediction` → `trending_catch` → `gainers_early` + DEX-side wrapper). Each fired a *separate* paper trade that exited within 2.5h–25h on trailing-stop / peak-fade / expiry. Net captured: **+$63 across 5 trades**. If the FIRST trade (`#869 first_signal`, opened 2026-04-23 01:10 at $0.0349) had been held continuously, the single position would now sit at **+16.3% / ~$49 unrealized** with a 7-day peak near +37.8%. **One position held through the multi-signal confirmation beats five positions churned in 12-hour windows.** The system correctly identified high conviction; the exit logic ignored that context. This is a structural, not BIO-specific, gap.
**Concept:** When a paper trade is open AND `N >= 2` *distinct* independent signals fire on the same `token_id` AFTER `opened_at`, the trade enters "conviction-locked" mode with extended exit gates:

| Stacked signals | max_duration_hours | trail_pct | sl_pct |
|---|---|---|---|
| 1 (default) | from `signal_params` | from `signal_params` | from `signal_params` |
| 2 | +72h | +5pp (cap 35%) | +5pp (cap 35%) |
| 3 | +168h | +10pp (cap 35%) | +10pp (cap 40%) |
| ≥4 | +336h | +15pp (cap 35%) | +15pp (cap 40%) |

**Definition of "distinct independent signal":**
- Different `signal_type` from the trade's own `signal_type`
- Fired *after* the trade's `opened_at`
- Not a duplicate of a `signal_type` already counted in the stack
- Sources to query: `gainers_snapshots`, `losers_snapshots`, `trending_snapshots`, `velocity_alerts`, `volume_spikes`, `narrative_predictions`, `chain_matches`, `tg_social_signals` — all already populated in scout.db.

**Open design questions (must resolve BEFORE coding):**
1. **Lookback window** — count only signals fired in last 7d, or full open-life?
2. **Per-signal-type opt-in** — does `narrative_prediction` (slow, multi-day window) benefit, or does conviction-lock only apply to fast signals (`gainers_early`, `first_signal`, `volume_spike`)?
3. **Interaction with PR #59 adaptive trail (low-peak tightening)** — does conviction-lock OVERRIDE the low-peak threshold (peak<20% → trail to 8%)? Or compose? They pull opposite directions.
4. **Interaction with BL-063 moonshot trail (peak ≥ 40% → 30% trail)** — moonshot is peak-driven, conviction is signal-count-driven. Likely compose (whichever is wider wins), but verify.
5. **Cap on stack count** — count up to 4? 6? Diminishing returns past N=3?
6. **Storage** — compute stack on-the-fly each evaluator pass (cheap, ~10 row DB hit), or persist `conviction_stack_count` column on `paper_trades`?
7. **Per-signal `conviction_lock_enabled` boolean on `signal_params`** — same calibration table controls which signal-types respect the multiplier.
8. **TG social interaction** — should `tg_social_signals` count as a stacked signal? It's a separate detection surface but the same trade would already be open under that signal_type if dispatched.
9. **Conviction stack downgrade** — once locked, does the lock stay regardless of subsequent activity? Or expire if no new signals fire for X days?

**Required research before implementing:**
1. **Backtest script** (`scripts/backtest_conviction_lock.py`) replaying last 30-90d of paper trades:
   - For each open trade, compute the stack count at every evaluator tick
   - Simulate exits under conviction-locked params vs the actual exits
   - Output: number of trades that would have been locked, simulated PnL delta vs actual, win-rate change, max-hold change, expired-pct change
2. **Survey of "BIO-like" plays in the existing data** — count how many tokens hit `N≥3` stacked signals over a 7d window in 2026-04 paper trades. If the answer is "BIO is unique," the feature is a poor ROI investment.
3. **Document edge cases:** what happens to a conviction-locked trade when the original `signal_type` gets auto-suspended (Tier 1b)? When an additional signal of an excluded-from-calibration type (`narrative_prediction`) fires?
4. **Compare to existing PR #6 (Multi-Signal Conviction Chains)** — that ships an *alert-time* convicition concept; this is *exit-time*. Verify they're orthogonal, not duplicating logic.

**Acceptance (when implementation eventually lands):**
- `backtest_conviction_lock.py` shows ≥10% PnL lift on simulated 30d window vs actual
- BIO replay demonstrates the trade staying open ≥5 days vs current ≤26h exits
- All 5 design questions above resolved in PR description
- Per-signal opt-in via `signal_params.conviction_lock_enabled` column (default OFF — deploy as no-op, flip per signal after observation)
- Tests: stack counts correctly, conviction-lock + adaptive-trail compose correctly, conviction-lock + moonshot compose correctly, suspended source signal does NOT block conviction lock from staying active
- Dashboard surfaces: badge on open positions showing current stack count + "conviction-locked" status

**Estimate (post-research):** 1.5–2 days code + tests + dashboard surface. Backtest script is a separate ~0.5 day deliverable that gates everything else.

**Resume protocol:** When user says "let's work on conviction-locked hold" or "BL-067", FIRST step is the backtest script. Do not write `scout/trading/conviction.py` until the backtest output justifies it.

---

## P3 — Future / Nice-to-have

### BL-040: Add backtesting framework
**Status:** DONE — backtest CLI implemented (PR #8, `python -m scout.backtest`)
**Why:** PRD Phase 4 (weeks 4-6). Need 30 days of outcome data first. /backtest slash command exists but needs real data.

### BL-041: Add X/Twitter social monitoring
**Status:** Not started
**Why:** PRD Phase 5. Requires Twitter API or scraping infrastructure.

### BL-042: Refactor test helpers to use conftest.py fixtures
**Status:** DONE — 17 test files migrated to shared conftest.py fixtures
**Why:** Code review M5 — shared fixtures added to conftest.py but existing tests still use local helpers. Low priority cleanup.

### BL-043: Add Prometheus/Grafana monitoring
**Status:** Not started
**Why:** Production observability. Export scan rates, alert rates, MiroFish latency as metrics.

### BL-044: VPS deployment with systemd service
**Status:** DONE — deployed to Srilu VPS (89.167.116.187)
**Why:** Production deployment. Run scanner as a persistent service with auto-restart.
**Services:** `gecko-pipeline.service`, `gecko-dashboard.service` (systemd, enabled on boot)
**Dashboard:** http://89.167.116.187:8000

---

## Virality Detection Roadmap — Multi-Source (Apr 2026)

**Context:** ASTEROID (+114775% / +50036.5%) exposed the limit of CoinGecko-only detection. Price/volume is the *symptom* of virality, not the cause. No amount of ML on price history predicts a Musk tweet. Detection scales with **data sources**, not with model training. Each new source unlocks a distinct virality trigger class.

**Trigger taxonomy → data source → lead time:**

| Trigger class | Example | Source | Lead time |
|---|---|---|---|
| Celebrity/influencer endorsement | Musk reply | Twitter/X API + LunarCrush influencer list | seconds–minutes |
| Exchange listing / rumor | Binance, Coinbase | Twitter CEX accounts + announcement bots | minutes (rumor) / same-second (official) |
| News / macro event | ETF approval, SEC ruling | CryptoPanic / CoinDesk / Bloomberg | minutes |
| Cultural moment | Polaris Dawn, elections, viral TikTok | Twitter trending + Google Trends + Reddit | hours–days |
| Coordinated degen campaigns | Telegram pumps, CT thread waves | Telegram/Discord scraping + X reply-velocity | minutes |
| Copycat mania | ASTEROID → instant SHIBA-2 / ORBIT | pump.fun new-deploy watcher + fuzzy-match | seconds |
| Whale / smart money | Labeled wallet accumulation | Nansen / Arkham / Dune | minutes |
| Narrative rotation | AI / RWA / DePIN sector pumps | Our category_snapshots + LunarCrush topics | tens of minutes |
| Perp / funding anomaly | Funding flip, OI spike on perps | Binance / Bybit / OKX WebSockets | seconds |
| Developer / project news | GitHub teasers, team posts | GitHub webhook + project Twitter | hours |

**Ranked rollout (ROI = coverage × lead-time ÷ effort × cost):**

| # | Source | Classes covered | Lead time | Effort | Cost |
|---|---|---|---|---|---|
| 1 | DexScreener `/token-boosts/top` + GeckoTerminal per-chain trending | paid-promo, copycat, rotation | seconds–min | 1–2 d | free |
| 2 | CryptoPanic news feed | news/macro | min (free) / sec (paid) | 2 d | free basic |
| 3 | Binance/Bybit perp WebSocket (funding + OI anomaly) | perp/funding | seconds | 2–3 d | free |
| 4 | LunarCrush Discover | influencer, cultural, rotation | minutes | 4–5 d | $24/mo |
| 5 | pump.fun new-deploy watcher (Solana) | copycat | seconds | 5–6 d | $0–49/mo (Helius) |
| 6 | Dune Analytics smart-money queries | whale accumulation | minutes | 3–4 d | free–$390/mo |
| 7 | Nansen Smart Money API (upgrade from #6) | whale | sec–min | 4–5 d | $150–1,500/mo |
| 8 | Twitter/X API direct (only if LunarCrush insufficient) | influencer | seconds | 5–7 d | $200–5,000/mo |

**Skip list (negative ROI):** Telegram/Discord scraping (legal gray, noisy), GitHub webhooks (too niche), Reddit velocity (hours of lag), Arkham scraping (TOS risk).

**Sprint plan:**

- **Sprint 1 — free sources, quick wins (1 week):**
  - PR #28 — DexScreener boosts + GeckoTerminal trending → `velocity_boost` tier
  - PR #29 — CryptoPanic news-tag watcher → `news_watch` tier
  - PR #30 — Binance/Bybit perp WebSocket anomaly detector → `perp_anomaly` tier
- **Sprint 2 — paid social, Musk-class catch:**
  - PR #31 — LunarCrush Discover integration → `social_velocity` tier ($24/mo)
- **Sprint 3 — on-chain upstream signal:**
  - PR #32 — Dune smart-money queries, cron-scheduled → `smart_money` tier
  - PR #33 — pump.fun new-deploy watcher with fuzzy-match → `copycat_launch` tier
- **Sprint 4 — meta-layer:**
  - PR #34 — Ensemble virality classifier. Requires ≥3 tiers live + ~2 weeks of labeled data. Tags each alert: `influencer-driven | whale-accumulation | rotation | copycat | news | perp-driven`. Telegram messages gain virality-class badges; exit logic diverges by class (influencer dies in hours, whale runs for days).

**First action:** PR #28 (DexScreener boosts + GeckoTerminal trending) — free, 2 days, proves the paid-promo hypothesis before committing to LunarCrush subscription. Execute after PR #27 velocity alerter stabilizes with ~48h of live Telegram traffic.

**What learning CAN do on existing data (no new sources):**
- Retrospective virality classifier: label past alerts (virality vs organic) using already-collected features — wallet concentration, holder_growth_1h curve, vol/mcap slope across 3+ cycles. Virality has narrow wallet sets + vertical-then-vertical curves; organic has broader accumulation.
- Ensemble on existing signals: velocity alert + extreme holder growth + rising vol/mcap for 3 cycles → "suspected virality" tag even before Sprint 4.

**Realistic expectation:** LunarCrush + DexScreener boosts gets us 5–15 minutes faster on narrative-driven pumps. We will never beat Musk-timed institutional trades (they have co-located Twitter feeds). Target: beat retail discovery by a meaningful window.

---

## Early Detection Roadmap — Phased Approach

**Goal:** Detect tokens that will appear on [CoinGecko Highlights](https://www.coingecko.com/en/highlights) (Trending Coins + Top Gainers) 1-2 hours before they appear, for manual research and informed buy decisions.

**Architecture:** Parallel early detection layer running alongside existing pipeline in shadow mode. Logs predictions with timestamps, compares against CoinGecko trending snapshots. Existing pipeline unchanged.

**Success metrics:** Hit rate (% of flagged tokens that appeared on Highlights), average lead time (minutes before CoinGecko), misses (tokens that trended without our flag).

> **NOTE (Apr 2026):** The CoinGecko Trending Tracker (PR #12) + Volume Spike Detector (PR #15) now serve as the primary early detection layer, using FREE CoinGecko data. LunarCrush/Santiment/Nansen phases are DEFERRED — the free approach achieved 56/61 (91.8%) trending hit rate with 62.4h avg lead time.

### Phase 1: LunarCrush Social Velocity ($24/mo) — CURRENT
**Status:** In design
**Rationale:** Social mention velocity is the #1 input to CoinGecko's trending algorithm. LunarCrush aggregates Twitter + Reddit + Telegram into a single API. Cheapest way to validate the thesis.
**Modules:**
- `scout/early/lunarcrush.py` — API client, fetch Galaxy Score + social volume
- `scout/early/tracker.py` — Spike detection, comparison vs CoinGecko trending
- `scout/early/models.py` — EarlySignal, TrendingSnapshot models
- DB tables: `early_signals`, `trending_snapshots`
- Dashboard: "Early Detection" tab with live signals, hit rate, lead time
**Config:** `LUNARCRUSH_API_KEY`, `LUNARCRUSH_POLL_INTERVAL=300`, `SOCIAL_VOLUME_SPIKE_RATIO=2.0`, `GALAXY_SCORE_JUMP_THRESHOLD=10`
**Validation:** After 2-4 weeks of shadow data, measure hit rate + lead time. If >50% hit rate with >30 min avg lead time, thesis is validated.

### Phase 2: Santiment Cross-Validation ($49/mo)
**Status:** Future — contingent on Phase 1 validation
**Rationale:** Second independent social signal source. Santiment's "emerging trends" and social volume divergence metric provides cross-validation against LunarCrush. Reduces false positives.
**Integration:** GraphQL API via `sanpy` Python client. Add as second signal source in `scout/early/`. Boost confidence when both LunarCrush AND Santiment flag the same token.
**Trigger:** Proceed if Phase 1 hit rate is promising but false positive rate is >40%.

### Phase 3: Nansen Smart Money ($49/mo + API credits)
**Status:** Future — strongest signal but most expensive
**Rationale:** Smart money (whale/fund wallets) accumulating a token typically precedes social buzz by hours. This catches a different phase of the pump lifecycle — accumulation before attention.
**Integration:** REST API. Track labeled wallet inflows for tokens in our candidate pool. When smart money + social spike align, highest confidence signal.
**Trigger:** Proceed if Phases 1-2 show social signals alone miss tokens that pump from whale accumulation without initial social buzz.

### Alternative Sources (if LunarCrush doesn't validate)
- **Dune Analytics** — Custom SQL queries on on-chain social/volume data
- **Defined.fi** — Real-time new pair discovery across 40+ chains (free tier)
- **Birdeye** — Solana-specific trending (free-$49/mo)
- **CoinGecko unused endpoints** — `watchlist_portfolio_users` spikes, category momentum, `is_anomaly` flags (free, but may be circular)
- **CoinMarketCap** — Cross-reference trending from different algorithm (free 333 req/day)

### Reviewer Notes (preserved for context)
> The lead time numbers from social APIs mean "before CoinGecko page updates", not "before price moves." For automated trading this is insufficient edge. However, for manual research (our use case), even minutes of lead time is valuable for investigating WHY a token is gaining attention before the retail crowd sees it on Highlights.
>
> If pivoting to automated trading in the future, the architecture changes significantly: need execution engine, risk management, MEV awareness, and sub-second latency. That is a separate project.

---

## Completed Features (April 2026 Session)

### Narrative Rotation Agent (PR #1)
**Status:** DONE — live on VPS
Autonomous 5-phase agent: OBSERVE → PREDICT → ALERT → EVALUATE → LEARN
Self-improving via agent_strategy table. 26+ predictions, LEARN phase active.

### Counter-Narrative Scoring (PR #3)
**Status:** DONE — live on VPS
Adversarial risk analysis for both pipelines. Deterministic flags + LLM synthesis.

### Shared CoinGecko Rate Limiter (PR #4)
**Status:** DONE — live on VPS
Token bucket limiter (25/min) shared across all CoinGecko callers. Closes issue #2.

### Second-Wave Detection (PR #5)
**Status:** DONE — live on VPS
Detects tokens that pumped 3-14 days ago and are re-accumulating.

### Multi-Signal Conviction Chains (PR #6)
**Status:** DONE — live on VPS
Event store + temporal pattern matching. 3 built-in patterns with LEARN lifecycle.

### Heartbeat + LEARN Counter Integration (PR #7)
**Status:** DONE — live on VPS

### CoinGecko Watchlist Signal + Backtest CLI (PR #8)
**Status:** DONE — live on VPS

### Dashboard Expansion (PRs #9-11 + fixes)
**Status:** DONE — live on VPS
5 tabs: Pipeline, Narrative Rotation, Chains, Second Wave, Health
TokenLink component with CoinGecko/DexScreener routing.

### CoinGecko Trending Snapshot Tracker (PR #12)
**Status:** DONE — live on VPS. 15/15 trending tokens caught (100% hit rate), avg 25.6h lead time.
Validates core goal — snapshots trending page, measures if we caught tokens before they trended.

### Personalized Narrative Matching (PR #13)
**Status:** DONE — live on VPS. 3 alert modes: all/whitelist/blacklist.
Category + mcap preferences for alert filtering. 3 modes: all/whitelist/blacklist.

### Test Fixture Refactor + Backlog Cleanup (PR #14)
**Status:** DONE
Test fixture refactor (BL-042) + backlog cleanup.

### Volume Spike Detector + Top Gainers Tracker (PR #15)
**Status:** DONE — live on VPS. Detects individual token breakouts via 5x+ volume surges. Top gainers validation same pattern as trending tracker.

### Top Losers Tracker + Volume-Sorted Scan (PR #17)
**Status:** DONE — live on VPS

### Comprehensive Code Review — 26 Fixes (PR #18)
**Status:** DONE

### Peak Gain Tracking (PR #19)
**Status:** DONE — live on VPS

### main.py Refactoring + UNRESOLVED Fix (PR #20)
**Status:** DONE — 1513 to 668 lines

### Market Briefing Agent (PR #21)
**Status:** DONE — live on VPS

### Dashboard Improvements
**Status:** DONE — sortable columns, missed gainers section, heating lead time

### 7d Momentum Scanner
**Status:** DONE — live on VPS

### Volume Spike Detector Broadened
**Status:** DONE — expanded to 250 tokens

### Dashboard Redesign
**Status:** DONE
3-tab layout (Signals/Pipeline/Health), Early Catches validation, quality signals, price cache, Narrative vs Meme separation.

### Price Cache System
**Status:** DONE
Stores prices from pipeline fetches, dashboard reads from DB (zero extra CoinGecko calls).

### SQLite datetime string-comparison fix (PR #24)
**Status:** DONE — live on VPS
38 queries across 10 modules wrap stored columns with `datetime()` to force parsing on both sides. `datetime.isoformat()` writes `T`-separator; SQLite `datetime('now')` returns space-separator; `'T' > ' '` produced false-stale comparisons. Max price-divergence dropped from 16.99% to 4.07%, avg to 0.71%. VANA 1.75 stale-peak entries cleared.

### Momentum_ratio 24h floor (PR #25)
**Status:** DONE — live on VPS
`momentum_ratio` signal now requires 24h change ≥ `MOMENTUM_MIN_24H_CHANGE_PCT` (default 3.0%). Previously stablecoin peg wobble (0.05% / 0.08% = ratio 0.625 > 0.6) was triggering the +20-point signal, polluting paper trades with USDC/DAI/PYUSD showing uniform -0.5% losses. Zero stablecoins in paper book post-deploy.

### Paper trade hard cap + startup warmup (PR #26)
**Status:** DONE — live on VPS
Two gates on `scout/trading/engine.py`: Step 0 warmup (`PAPER_STARTUP_WARMUP_SECONDS=180`, `time.monotonic()`-based, immune to wall-clock jumps) refuses new trades for 3 min after startup. Step 2c cap (`PAPER_MAX_OPEN_TRADES=10`) caps concurrent opens. Fixes restart-burst behavior: every process restart was replaying every currently-qualifying token as a fresh signal, filling the book with 45+ positions. Verified: exactly 10 open post-restart, warmup skip logs fire at elapsed=138.6s, max-open skip logs fire at overflow.

### CoinGecko velocity alerter (PR #27)
**Status:** DONE — live on VPS (`VELOCITY_ALERTS_ENABLED=true`)
New `scout/velocity/detector.py` tier for catching asteroid-class pumps (ASTEROID +60087%) earlier than gainers / 7d-momentum trackers. Filters: 1h ≥ 30%, mcap $500K–$50M, vol/mcap ≥ 0.2, top-10 by 1h change, dedup 4h per coin-id via new `velocity_alerts` table. **Research-only — no paper trade dispatch.** Zero extra CoinGecko API calls (reuses `_raw_markets_combined` cache). 616 tests passing. Planned: meta-tier in Sprint 4 of Virality Roadmap.

### Open follow-ups noted during session
- **Edge detection for paper trades:** only open on *transition* into qualifier set, not current-state membership (prevents restart-bursts at root). Requires persisting previous cycle's qualifier set per signal type. Noted in PR #26 body.
- **DexScreener boosts + GeckoTerminal per-chain trending** as additional velocity sources. See Virality Roadmap PR #28.

---

## Trading Engine Roadmap

**Goal:** Autonomous DEX trading — detect signals, execute trades on-chain, manage positions.

**Approach:** Paper trading first (2 weeks) to prove edge with PnL data, then graduate to live trading with small positions ($50-100/trade).

### Architecture Decisions (Locked In)

**D1 — Pluggable engine:** The trading engine is an independent common component (`scout/trading/`) that any signal source can call. Interface: `engine.buy(token_id, chain, amount_usd)` / `engine.sell(...)`. Mode switchable: paper or live.

**D2 — Signal triggering:** Paper mode trades ALL signals (Option C) — volume spikes, narrative picks, trending catches, chain completions. Maximizes data collection. Live mode will use multi-signal confirmation (multi-layer agreement before executing).

**D3 — Chain support:** Chain-agnostic paper trading with chain metadata stored. Live execution targets BSC (PancakeSwap), Solana (Raydium/Jupiter), Ethereum/Base (Uniswap).

**D4 — Exit strategy:** Multi-checkpoint tracking (1h, 6h, 24h, 48h) for analysis + simulated take-profit (+20%) and stop-loss (-10%) for realistic PnL. Both run in parallel per trade.

**D5 — Libraries chosen:**
- EVM chains: `web3-ethereum-defi` (MIT, pip install, pure Python, 800+ stars)
- Solana: `raydium_py` or solana-py based library
- Paper trading shim: custom (~50 lines)
- NOT using Hummingbot (too heavy) or Freqtrade (no DEX support)

### Phase A: Paper Trading Engine (Current)
**Status:** LIVE — running on VPS since Apr 15. Currently on Iteration 4 (first_signal + narrative_prediction). Collecting data for 48h undisturbed.
**Module:** `scout/trading/`
```
scout/trading/
  engine.py        # Pluggable interface — buy/sell/get_positions/get_pnl
  paper.py         # Paper trading — simulate fills at current price, log to DB
  models.py        # PaperTrade, Position, PnL models
```
**DB tables:** `paper_trades`, `paper_positions`
**Dashboard:** Paper PnL section on Signals tab — per-signal-type performance
**Config:** `TRADING_ENABLED=true`, `TRADING_MODE=paper`, `PAPER_TRADE_AMOUNT_USD=50`
**Success criteria:** 2 weeks of paper trades with positive PnL after simulated fees → graduate to Phase B

#### Paper Trading Iterations

| Iteration | Signals Used | Result |
|-----------|-------------|--------|
| 1 | All 7 signals | All losing — bought at the top every time |
| 2 | Removed lagging signals | Still micro-cap junk |
| 3 | Added $5M mcap filter | momentum_7d still producing late entries |
| **4 (current)** | **first_signal + narrative_prediction only** | **Collecting data — 48h undisturbed run** |

### Phase B: Live Execution Engine (Future — after paper validates)
**Status:** Not started — blocked by Phase A validation
**Module extensions:**
```
scout/trading/
  live_evm.py      # web3-ethereum-defi — PancakeSwap, Uniswap swaps
  live_solana.py   # raydium_py — Raydium, Jupiter swaps
  risk.py          # Position sizing, max exposure, stop-loss enforcement
  wallet.py        # Encrypted private key management (NEVER in .env)
```
**Requirements before going live:**
- [ ] 2+ weeks of paper trading data showing positive PnL
- [ ] Risk management: max $50/trade, max $500 total exposure, automatic stop-loss
- [ ] Kill switch: `TRADING_KILL_SWITCH=true` instantly stops all trading
- [ ] Private key encryption (keyring or encrypted file, never plaintext)
- [ ] Manual approval for first 10 live trades (dashboard queue)
- [ ] Gas estimation + slippage protection per chain
- [ ] MEV protection (private RPC for Ethereum, Jito for Solana)

### Phase C: Advanced Trading (Future)
- Partial position scaling (enter 50%, add if signal strengthens)
- Dynamic position sizing based on signal quality score
- Cross-chain arbitrage (same token on multiple DEXes)
- Portfolio rebalancing
- PnL-based signal weighting (auto-increase position size for profitable signal types)

### RCA Results (Apr 19, 2026) — Validates the Trading Thesis
- 12/14 CoinGecko Highlights tokens caught (86%)
- 15/15 CoinGecko Trending tokens caught (100% hit rate)
- BLESS: caught 16h early, currently +216% 7d
- GENIUS: caught 10.5h early
- MEZO: caught 8h early
- RaveDAO: caught 33h early, +5333% 7d
- Gap: 2 tokens missed (aPriori, Bedrock) — individual breakouts without category momentum. Volume Spike Detector (PR #15) designed to catch these going forward.
