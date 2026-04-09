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
**Status:** Blocked by BL-001
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
**Status:** Not started
**Files:** scout/scorer.py, scout/models.py, tests/test_scorer.py
**Why:** Current scorer has no fraud filter. Wash-traded tokens pass easily.
**Changes:**
- Liquidity floor: auto-discard if `liquidity_usd < $15K` (configurable via Settings)
- Run before any scoring — fail fast, return score=0
- Deployer wallet check deferred to Phase 2 (needs Helius/Moralis)
- Wash trade top-3-wallet check deferred to Phase 2 (needs on-chain data)
**Acceptance:** Tokens with < $15K liquidity get score 0 and never reach MiroFish

### BL-011: Add buy pressure ratio signal (Tier 3)
**Status:** Not started
**Files:** scout/models.py, scout/ingestion/dexscreener.py, scout/scorer.py, tests/
**Why:** Best wash-trade discriminator available from existing API data. DexScreener returns `txns.h1.buys` and `txns.h1.sells` — currently unused.
**Changes:**
- Add `txns_h1_buys: int | None = None` and `txns_h1_sells: int | None = None` to CandidateToken (see Decision D2)
- Parse from DexScreener response in `from_dexscreener()`
- Score: buy_ratio > 65% → +15 points
**Acceptance:** Tokens with skewed buy pressure score higher than balanced volume tokens

### BL-012: Replace binary token age with bell curve (Tier 4)
**Status:** Not started
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
**Status:** Not started
**Files:** scout/scorer.py, scout/db.py, scout/main.py, tests/
**Why:** A token whose score is rising across consecutive scans indicates active accumulation in progress — the velocity itself is a signal.
**Changes:**
- Add `score_history` table in db.py (contract_address, score, scanned_at)
- Log each score in main.py after scoring
- In scorer: accept `historical_scores: list[float] | None = None` param, award +10 if strictly increasing over last 3 scans (see Decision D3)
- Scorer remains pure (no I/O) — main.py does the DB read and passes historical scores in
**Acceptance:** Tokens with rising scores get bonus, flat/declining scores get nothing

### BL-014: Add co-occurrence multiplier
**Status:** Not started
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
**Status:** Not started
**Files:** scout/mirofish/seed_builder.py, scout/scorer.py, tests/
**Why:** Enriching the MiroFish seed with signal context improves narrative simulation quality.
**Changes:**
- scorer.py returns additional `confidence: str` (HIGH if 3+ tiers firing, MEDIUM if 2, LOW if 1)
- seed_builder.py includes `signal_confidence` and `signals_fired` list in the seed payload
- Update MiroFish prompt to reference the confidence level
**Acceptance:** MiroFish seed contains signal context, tests verify format

### BL-016: Normalize scoring to 125 base → 100 scale
**Status:** Not started
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
**Status:** Not started — currently dead signal (25 pts never fire)
**Files:** scout/ingestion/holder_enricher.py
**Why:** Code review found holder_growth_1h is never populated. The 25-point holder growth signal is dead in production without this.
**Changes:**
- Store previous holder_count in DB per contract_address
- On next scan, compute delta as holder_growth_1h
- Requires at least 2 scan cycles to produce data
**Blocked by:** Helius/Moralis API key

### BL-021: Add unique buyer wallet count signal (Tier 3)
**Status:** Not started
**Files:** scout/ingestion/holder_enricher.py, scout/models.py, scout/scorer.py
**Why:** Distinguishes organic community buying from bot accumulation.
**Changes:**
- Add `unique_buyers_1h: int = 0` to CandidateToken
- Fetch from Helius (Solana) / Moralis (EVM) transfer history
- Score: high unique_buyers relative to total_txns → +15 pts
**Blocked by:** Helius/Moralis API key

### BL-022: Add wash trade detection (top-3 wallet volume concentration)
**Status:** Not started
**Files:** scout/scorer.py, scout/ingestion/holder_enricher.py
**Why:** Hard disqualifier — if top 3 wallets account for > 40% of volume, it's almost certainly wash trading.
**Changes:**
- Fetch top wallet transaction data from Helius/Moralis
- Compute concentration ratio
- Disqualify (score 0) if > 40%
**Blocked by:** Helius/Moralis API key

### BL-023: Add deployer wallet supply concentration check
**Status:** Not started
**Files:** scout/safety.py or scout/scorer.py
**Why:** Classic rug setup — deployer holds > 20% of supply.
**Note:** Partially covered by GoPlus already. Evaluate overlap before implementing.
**Blocked by:** Helius/Moralis API key

### BL-024: Add transaction size distribution signal
**Status:** Not started
**Files:** scout/ingestion/holder_enricher.py, scout/scorer.py
**Why:** Organic pre-pump = many small txns ($50-$500). Bot wash = fewer large uniform txns.
**Blocked by:** Helius/Moralis API key

---

## P2 — Infrastructure & Reliability

### BL-030: Add Solana chain bonus to scorer (Tier 4)
**Status:** Not started
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Diagram shows +5 pts for Solana chain (meme premium). Solana has disproportionate meme coin activity.
**Changes:** Simple conditional: `if chain == "solana": +5 pts`

### BL-031: Add market cap tier curve (Tier 4)
**Status:** Not started
**Files:** scout/scorer.py, tests/test_scorer.py
**Why:** Current binary $10K-$500K gate misses the sweet spot. Diagram shows $10K-$100K as peak score, tapering to $500K.
**Changes:** Graduated scoring: 8 pts for $10K-$100K, 5 pts for $100K-$250K, 2 pts for $250K-$500K

### BL-032: Populate social_mentions_24h signal
**Status:** Not started — currently dead signal (15 pts never fire)
**Files:** scout/ingestion/, scout/models.py
**Why:** Code review found social_mentions_24h is never populated.
**Note:** PRD defers Twitter/X integration to Phase 5. Could use free Telegram channel monitoring or LunarCrush API as interim source.
**Blocked by:** Social data source decision

### BL-033: Add heartbeat logging every 5 minutes
**Status:** Not started
**Files:** scout/main.py
**Why:** PRD requires heartbeat log showing: tokens scanned, candidates promoted, alerts fired, MiroFish jobs today.
**Changes:** Track cumulative stats, log summary every 5 min (or every N cycles)

### BL-034: Set up MiroFish Docker integration
**Status:** Not started
**Files:** docker-compose.yml, scout/mirofish/client.py
**Why:** MiroFish is the key differentiator but hasn't been tested locally yet. Currently all narrative scoring goes through the fallback.
**Changes:** Clone MiroFish repo, configure LLM keys, test /simulate endpoint, verify seed format compatibility

---

## P3 — Future / Nice-to-have

### BL-040: Add backtesting framework
**Status:** Not started
**Why:** PRD Phase 4 (weeks 4-6). Need 30 days of outcome data first. /backtest slash command exists but needs real data.

### BL-041: Add X/Twitter social monitoring
**Status:** Not started
**Why:** PRD Phase 5. Requires Twitter API or scraping infrastructure.

### BL-042: Refactor test helpers to use conftest.py fixtures
**Status:** Not started
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

## Early Detection Roadmap — Phased Approach

**Goal:** Detect tokens that will appear on [CoinGecko Highlights](https://www.coingecko.com/en/highlights) (Trending Coins + Top Gainers) 1-2 hours before they appear, for manual research and informed buy decisions.

**Architecture:** Parallel early detection layer running alongside existing pipeline in shadow mode. Logs predictions with timestamps, compares against CoinGecko trending snapshots. Existing pipeline unchanged.

**Success metrics:** Hit rate (% of flagged tokens that appeared on Highlights), average lead time (minutes before CoinGecko), misses (tokens that trended without our flag).

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
