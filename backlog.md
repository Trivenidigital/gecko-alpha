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

### BL-032: Social signal source decision (consolidates old BL-032 + BL-041)
**Status:** DECISION-PENDING — three options below; pick one before any code lands
**Tag:** `decision-pending` `dead-signal` `consolidates-BL-041`
**Files (eventual):** `scout/ingestion/`, `scout/models.py`, `scout/scorer.py`
**Why:** `social_mentions_24h` is a 15-point signal that has never fired in production — code review found it's never populated. Hermes findings 2026-05-03 confirmed the 671-skill hub has **no** Twitter/X, social-volume, or LunarCrush-equivalent skill, so the Hermes route is closed. Three real options remain:
- **(a) Reuse BL-064 listener data.** Compute `mentions_24h` per `coin_id` from the existing `tg_social_messages` table (1,019+ messages already ingested). Cheapest path; data is already on disk; chain-agnostic; no new dependency. Limitation: Telegram-only, not full social graph.
- **(b) Third-party API.** LunarCrush ($24/mo) was the leading candidate but is in the "do not propose" list (see memory `feedback_lunarcrush_dropped.md`). Other options: Defined.fi (free tier), CryptoPanic social signal (already integrated for news). Cost vs. coverage trade-off; another monthly subscription.
- **(c) Honest delete.** Remove the `social_mentions_24h` field from `CandidateToken`, drop the 15 points from the scorer, lower `SCORER_MAX_RAW` accordingly, document as won't-fix. Cleanest if no source ever ships.
**Acceptance:** Either (a) or (b) ships and the signal fires non-zero on real tokens, OR (c) ships and the dead-signal surface is removed from the codebase.
**Estimate:** (a) 0.5–1d, (b) depends on source, (c) 0.25d.
**Note on consolidation:** This entry replaces the prior separate BL-032 + BL-041 (X/Twitter monitoring). They were two pending entries for the same dead-signal problem; merged 2026-05-03.

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

## P2 — Operational hygiene + agent-framework integrations

### BL-072: Operational alignment doc + new-primitives convention + pre-write hook
**Status:** SHIPPED 2026-05-03 (this PR)
**Tag:** `convention` `tooling` `enforcement`
**What shipped:**
- `docs/gecko-alpha-alignment.md` — 4-part operational hygiene reference (deployed patterns / drift checklist / working agreement / explicit limits)
- `.claude/hooks/check-new-primitives.py` — PreToolUse hook gating `tasks/(plan|design|spec)_*.md` on the `**New primitives introduced:** [list or NONE]` line
- `.claude/settings.json` — hook registered as 4th PreToolUse entry (matcher `Write|Edit|MultiEdit|NotebookEdit`); preserves existing 3 PreToolUse hooks + 2 PostToolUse blocks + Stop hook
- `CLAUDE.md` — "Plan/Design Document Conventions" sub-heading under existing "Coding Conventions" (no new top-level)
**Why:** chain_patterns auto-retired silently for 17d (2026-04-14 → 2026-05-01) because no convention surfaced the silent-failure surface at proposal time. This PR codifies the surface so future plans declare what infrastructure they add, and the hook prevents drift mechanically (not by discipline alone).
**No production code modified.** No DB migration. Existing tests still pass.
**Honest limitation:** the hook checks the marker EXISTS — does NOT validate that the listed primitives are truthful. Human PR review verifies accuracy. Documented in `docs/gecko-alpha-alignment.md` Part 4 and `CLAUDE.md`.

### BL-073: Hermes Agent integration roadmap
**Status:** RESEARCH-GATED — Phase 0 DONE 2026-05-03; Phase 1 unfunded
**Tag:** `research-gated` `hermes` `cost-gated` `90-day-cancellation`
**Realistic outlook:** Phase 1 (GEPA on `narrative_prediction` LLM prompt) is the one Hermes capability with concrete projected value for gecko-alpha. Cost gate revised down to ~$10 + ~1 day work after Phase 0 identified `NousResearch/hermes-agent-self-evolution` as a near-complete starting framework. Trigger to start: operator commits funding + bandwidth.

**Phases:**

| # | What | Cost | Starting framework | Trigger | Status |
|---|---|---|---|---|---|
| 0 | Browse Hermes skills hub + ecosystem for relevant skills | 1h | — | operator-driven | DONE 2026-05-03 — 671 skills hub identified, frameworks chosen for Phases 1+2, ≥3 honest rejects logged. See `tasks/notes_agentskills_browse_2026_05_03.md` |
| 1 | GEPA evolve `narrative_prediction` LLM prompt against the 1,274-row `predictions` table eval set (42 HIT / 40 MISS / 566 NEUTRAL / 561 UNRESOLVED) | $10 + ~1d (was 2d before Phase 0) | `NousResearch/hermes-agent-self-evolution` (MIT, 2.7k stars, DSPy+GEPA pipeline). Hermes built-ins: `dspy`, `evaluating-llms-harness`, `weights-and-biases` from the 671-skill hub | operator commits | unfunded |
| 2 | Hermes ops agent on VPS — Telegram NL access to gecko-alpha state, scheduled cron checks, cross-platform messaging gateway | ~0.5–1d (was 1-2d before Phase 0) + $5/mo | `JackTheGit/hermes-ai-infrastructure-monitoring-toolkit` (near-drop-in: Telegram bot + cron + monitoring). Optional fleet view: `builderz-labs/mission-control` (3.7k stars) | operator approves new VPS service | unfunded |
| 3 | Model routing for narrative LLM via OpenRouter (200+ models, ensemble, A/B against the Phase 1 eval harness) | 2-3d + variable per-model cost | reuse Phase 1 eval harness | Phase 1 eval harness exists | gated on Phase 1 |
| 4 | BL-064 cross-platform expansion via Hermes gateway (Discord/Slack curator channels in addition to Telegram) | 2-3d | reuse Phase 2 gateway | BL-064 14d soak (2026-05-11) shows curator-side trade dispatch works on Telegram first | gated on BL-064 soak |
| 5 | Atropos RL infrastructure for tool-calling model training | n/a now | — | ≥1000 trades/signal stable for 30d (per memory `feedback_ml_not_yet.md`) | gated on data volume — months out |

**Honest cancellation criterion (REVISED post-Phase 0):** Original criterion was "close as won't-fix by 2026-08-03 if Phase 1 hasn't started". With Phase 1 work halved by `hermes-agent-self-evolution`, the activation barrier is mostly operator attention rather than engineering risk. Re-evaluate this criterion at the +30d check (2026-06-03) — if it still looks like the right call, keep it; if Phase 1 looks like a no-brainer, drop the cancellation criterion. Status checks: +30d (2026-06-03), +60d (2026-07-03), +90d (2026-08-03).

**Realistic outcome 4 weeks from now:** Phase 0 done (it is), Phase 1 may now be cheap enough to attempt opportunistically.
**Realistic outcome 90 days from now:** Phase 1 + Phase 2 shipped (positive case, more plausible after Phase 0) OR still unfunded (worst case — re-evaluate cancellation).

**Honest reject reasons logged in Phase 0:** `chainlink-agent-skills` (wrong oracle model), `hxsteric/mercury` (wrong problem — execution routing, not signals), `ripley-xmr-gateway` (wrong chain), no paper-trade skill exists, no CoinGecko/DexScreener-specific skill, no SQLite-audit-log skill.

**Adapted from `Trivenidigital/shift-agent` analysis:** the inspiration for BL-072 + BL-073 was `shift-agent`'s `docs/hermes-alignment.md` + `CLAUDE.md` "Hermes-first" rules. shift-agent **runs on Hermes** as its production runtime; gecko-alpha does NOT (vanilla async Python pipeline). The adaptation is structural — we kept the 4-part doc shape and the read-deployed-code rule, dropped the Hermes-specific drift-tag vocabulary as cargo-cult, and replaced it with the more answerable single-line `**New primitives introduced:** [list or NONE]` declaration.

### BL-074: Minara as live-execution layer (post-BL-055 unlock)
**Status:** VISION — gated on BL-055 unlock. Captured 2026-05-03.
**Tag:** `gated-on-BL-055` `live-execution` `minara` `hermes-ecosystem`
**Vision:** gecko-alpha alerts in → Minara executes out. gecko-alpha continues to own signal generation, conviction gating, and observability; Minara owns wallet custody, venue routing (EVM + Solana + Hyperliquid perps), order placement, and on-ramp. Two-layer architecture, clean separation.

**Why this is BL-074, not Phase N of BL-073:** BL-073 is about Hermes building blocks for gecko-alpha's *narrative LLM and ops agent*. Minara is a *live-execution skill pack* — different problem class. Lumping them would muddle the dependency graph (BL-073 phases are independent of BL-055; this work absolutely is not).

**Hard prerequisites (all from BL-055 unlock criteria, copied here so they don't drift):**
1. BL-055 shadow soak passes 7d clean (per memory `project_bl055_deployed_2026_04_23.md`).
2. `scout/live/balance_gate.py` implemented (currently the live path raises `NotImplementedError`; verified 2026-05-03 — file does not exist).
3. `would_be_live` paper-trade subset has been validated against actual outcomes (per `feedback_paper_mirrors_live.md` — capital-constrained FCFS-20-slots subset must show positive PnL before risking real capital).
4. Operator writes a live-execution policy: capital allocation rules, per-trade size limits, daily loss limits, kill-switch escalation, custody approach (hot wallet vs. external signer), regulatory posture.
5. Operator explicit go-ahead.

**Architectural choices to revisit when the gate opens (do NOT pre-decide now):**
- **Adapter shape.** Minara is an *agent skill* (NL commands like "Buy 100 USDC of ETH"), not a CCXT-style REST client. Existing `scout/live/adapter_base.py` + `binance_adapter.py` pattern assumes the latter. Either: (a) write `MinaraAdapter` that shells out to `minara` CLI (npm package `minara@latest`) translating intents to NL commands — bypasses Hermes entirely, treats Minara as a thin executor; (b) gecko-alpha publishes structured trade intents to a queue (Redis/SQLite outbox), separate Hermes+Minara process subscribes — preserves agent UX, adds infra; (c) keep alerts → Telegram → operator → Hermes+Minara as today, no integration. Decision belongs in a future spec, not in this entry.
- **Custody.** Minara wallet is a hot wallet on the same host as gecko-alpha → blast radius if VPS compromised. Mitigations to evaluate: per-trade size limit, separate signing host, hardware key, withdraw-only kill switch.
- **Failure semantics.** What does gecko-alpha do if Minara is down at the moment of a high-conviction alert? Queue and retry, or fail-closed and alert operator? (Default fail-closed — execution layer outage should not silently drop signals.)
- **Reconciliation.** Minara executions need to flow back into `paper_trades`/`live_trades` tables for the existing PnL/audit/dashboard surfaces, otherwise we lose end-to-end traceability.

**Reference:** `Minara-AI/skills` (MIT, 263⭐ as of 2026-05-03, last push 2026-04-21). 88/100 self-reported on `Minara-AI/crypto-skill-benchmark` (Sonnet 4.6, 76 scenarios — note self-reported). Multi-chain: Ethereum, Base, Arbitrum, Optimism, Polygon, Avalanche, Solana, BSC, Berachain, Blast, Manta, Mode, Sonic, Conflux, Merlin, Monad, Polymarket, XLayer, Hyperliquid (perps).

**Honest reality check:** Until items 1–4 of the prerequisites above are real, this entry is a vision artifact, not an actionable backlog item. Re-evaluate when BL-055 reaches the unlock checkpoint. Don't let it accrete into a spec prematurely — premature spec for a system whose upstream gate hasn't opened is exactly the BL-073-style theatre we just argued against.

**Operator-side evaluation worth doing now (zero gecko-alpha code change):** install Hermes + Minara on a terminal you control, manually execute a small number of trades on alerts gecko-alpha currently surfaces to Telegram. This is the cheapest way to assess Minara's execution quality on signals you already trust. Outcome of that trial directly informs adapter-shape choice (a) vs. (b) above.

---

## P2 — BL-064 follow-ups (TG social signals deployed 2026-04-27)

### BL-065: Dispatch paper trades from cashtag-only resolutions
**Status:** SHIPPED 2026-05-04 — PR #65 squash-merged as `835ce7f`, deployed VPS 2026-05-04T05:08:30Z. Default fail-closed (`cashtag_trade_eligible=0` on all 8 channels). Operator must `UPDATE tg_social_channels SET cashtag_trade_eligible=1 WHERE channel_handle='@<curator>'` to enable. 6 new BlockedGate values + 4 Settings + 6 log events. 18 active tests + 6 cleanly-skipped placeholders. Closes BL-064 zero-trade gap. See memory `project_bl065_deployed_2026_05_04.md`.

**Original spec — flagged 2026-04-29, now historical:**
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
**Status:** SHIPPED-VARIANT 2026-05-04 — original 5-endpoint scope reduced after drift check found `/api/tg_social/alerts` (composite) + `TGAlertsTab.jsx` already deployed. BL-066' (gap-fill) PR #66 squash-merged as `6b95c2f`, deployed VPS 2026-05-04T06:09:04Z: added `/api/tg_social/dlq` + extended composite endpoint with `cashtag_dispatched_24h` + per-channel cashtag fields (`cashtag_trade_eligible`, `cashtag_dispatched_today`, `cashtag_cap_per_day`) + new `TGDLQPanel.jsx`. 12 active tests + 3 cleanly-skipped. **Lesson learned:** `find . -name __pycache__ -exec rm -rf {} +` mandatory on VPS after any `git pull` touching `dashboard/` Python (stale .pyc caused 14 startup 500s). See memory `project_bl066_deployed_2026_05_04.md` + `feedback_clear_pycache_on_deploy.md`. **Remaining gap (low priority):** original spec proposed 5 separate endpoints; composite covers 95% of need, defer split unless operator finds it limiting.

**Original spec — flagged 2026-04-29, now historical:**
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

### BL-071a': Wire chain_match writers + DexScreener fetch for memecoin outcome hydration
**Status:** SHIPPED 2026-05-04 — PR #64 squash-merged as `cbb1e7f`, deployed VPS. Closes the silent-skip surface for DexScreener-resolved memecoin chain outcomes. See memory `project_chain_revival_2026_05_03.md` (related context).

**Original spec (post-Bundle A 2026-05-03), now historical:**
**Tag:** `chain-pipeline` `outcome-telemetry` `unblocks-BL-071a-fully`
**Files:** `scout/chains/tracker.py` (`_record_chain_complete`, `_record_expired_chain` — accept and store mcap; hydrator's populated-branch — replace silent `continue` with DexScreener FDV fetch + outcome computation), `scout/chains/events.py` or chain-completion caller chain (pass current FDV through to writers), tests
**Why:** Bundle A added `chain_matches.mcap_at_completion REAL` column + hydrator branch that skips silently when populated. Writers still pass NULL because adding the caller-wiring would have grown Bundle A scope. Once writers populate the column AND the hydrator inlines the DexScreener fetch, hit/miss outcomes flow for memecoin chain_matches. Closes the BL-071a death-spiral structurally.
**Acceptance:**
- New memecoin chain_matches have non-NULL `mcap_at_completion`.
- LEARN cycle emits `chain_outcomes_hydrated count>0` for memecoin pipeline (instead of `chain_outcomes_unhydrateable_memecoin total_unhydrateable=N` aggregate warning).
- Pattern hit-rate becomes meaningful for memecoin patterns.
- Remove the `chain_outcomes_unhydrateable_memecoin` warning OR downgrade to INFO (per Bundle A design-doc §6 Q1).
- **Coupling guard (per Bundle A PR-review R2 S2):** writer-wiring + DexScreener fetch MUST land in the same PR. Splitting them would re-introduce the silent-skip path on populated rows (hydrator skips silently when `mcap_at_completion` is set; if writers wire the column without the fetch landing, every populated row is silently dropped from outcome resolution). Add a test that fails if `chain_matches` has any row with non-NULL `mcap_at_completion` AND `outcome_class IS NULL` AND `completed_at < now-48h` after a LEARN cycle — that's the canary.
- **Re-introduce per-cause counters in the aggregate warning** when the failure modes are actually distinguishable (today they aren't; Bundle A intentionally collapsed `mcap_at_completion_null_count` + `outcomes_table_empty_count` into just `total_unhydrateable` to avoid misleading log fields).
**Estimate:** 0.5d (small caller-chain edit + DexScreener fetch in hydrator + tests + coupling-guard test).

### BL-071a: Investigate why memecoin `outcomes` table is empty
**Status:** Not started — flagged 2026-05-03 during BL-071 investigation
**Tag:** `research-gated` `chain-pipeline` `outcome-telemetry`
**Files:** likely `scout/chains/tracker.py`, `scout/memecoin/`, wherever outcomes are supposed to be written for pump.fun / dexscreener tokens
**Why:** `chain_matches.update_chain_outcomes` queries `outcomes WHERE contract_address = ? AND price_change_pct IS NOT NULL` for `pipeline='memecoin'` rows. The `outcomes` table has **0 rows** in prod. So memecoin chain_matches can NEVER get hydrated — they all stay `outcome_class=NULL` or get marked `EXPIRED` by the miss-recorder. That's half the cause of the BL-071 auto-retirement death spiral.
**Investigation:** trace which writer is supposed to insert into `outcomes` for memecoin tokens. Possibilities: (a) writer never existed (intentional — memecoin pipeline never had outcome tracking), (b) writer exists but is gated behind a disabled config flag, (c) writer exists but is silently failing.
**Acceptance:** Either (a) confirm `outcomes` is dead by design and route memecoin chain_matches to a different outcome source (e.g. `paper_trades` outcomes), OR (b) re-enable the writer + verify rows start appearing.
**Estimate:** 0.5–1 day investigation + fix.

### BL-071b: narrative `chain_matches` start at `outcome_class='EXPIRED'`, hydrator skips them
**Status:** Not started — flagged 2026-05-03 during BL-071 investigation
**Tag:** `research-gated` `chain-pipeline` `outcome-telemetry`
**Files:** `scout/chains/tracker.py:518` (`_record_chain_miss` writer), `scout/chains/tracker.py:550` (`update_chain_outcomes` hydrator)
**Why:** All 154 narrative `chain_matches` in prod have `outcome_class='EXPIRED'` with NO `evaluated_at` timestamp — meaning they were marked EXPIRED at write-time by `_record_chain_miss`, not by the hydrator. The hydrator's `WHERE outcome_class IS NULL` clause then skips them entirely, even though the `predictions` table has 42 actual `'HIT'` outcomes that should propagate. Net effect: narrative pattern hit-rate is permanently 0% even when patterns succeed. Other half of the BL-071 death spiral.
**Two design choices to evaluate:**
- (a) Change `_record_chain_miss` to write `outcome_class=NULL` (let the hydrator decide later), OR
- (b) Widen the hydrator's WHERE clause to include `outcome_class='EXPIRED'` (re-evaluate marked-expired matches against the predictions table).
- Option (a) is cleaner semantically — EXPIRED should mean "we waited and nothing happened", not "we wrote it as EXPIRED on first encounter". But may break other consumers expecting EXPIRED-at-write-time semantics.
**Acceptance:** narrative chain_matches start producing `outcome_class='hit'` for tokens whose predictions resolved as HIT. Pattern hit-rate becomes meaningful (non-zero for real winners).
**Estimate:** 0.5 day investigation + 0.5 day fix + tests.

### BL-070: Entry stack gate — refuse trades with insufficient signal confirmation
**Status:** **SHELVED — re-evaluate when system net is clearly negative again, OR if 30d data still shows large stack=1 bleed after 2026-05-15 checkpoint.**
**Tag:** `research-gated` `strategy` `entry-filter` `requires-backtest`
**Plan:** `tasks/plan_bl070_entry_stack_gate.md`
**Why shelved (history):** v1 backtest (`scripts/backtest_v1_signal_stacking.py`) showed stack≥2 trades net +$722 vs stack=1 trades net −$1,243 over 30d. Plan proposed entry-time gate filtering stack=1 trades. Adversarial reviewer's Q10 prompted a baseline check that showed Tier 1a `enabled=0` for `gainers_early` + `trending_catch` would capture $933 of the $1,243 swing with zero new code, so we executed the Tier 1a kill 2026-05-01 instead of building BL-070. **However:** the kill of `gainers_early` was reversed 2026-05-03 when the post-PR-#59 data showed it had become profitable (+$8.61/trade across 59 closes). PR #59 + chain dispatch revival + Tier 1a infrastructure together appear to be enough to swing the system net positive without BL-070.
**Resume protocol:** Only revisit if (a) the 2026-05-15 checkpoint shows 14d net materially negative, OR (b) a future targeted backtest (point-in-time entry replay, paper_trades source removed, index audit, lookback sensitivity sweep) shows entry-time stack-gate lift > $200/30d on top of the current state. If neither — BL-070 is structurally unneeded; close as won't-fix.

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

### BL-075: Slow-burn miss diagnostic + watcher (RIV-shape blind spot)
**Status:** RESEARCH-GATED — Phase A (diagnostic) is cheap and unblocks Phase B; Phase B (watcher) is shadow-only and gated on Phase A telemetry
**Tag:** `research-gated` `detection-blind-spot` `silent-failure-surface`
**Motivating evidence (2026-05-03):** RIV (`riv-coin`) ran $2M → $200M mcap over 30 days — exactly the asymmetric move the system exists to surface. SSH audit against prod `scout.db` returned **zero rows** for RIV across `gainers_snapshots`, `trending_snapshots`, `velocity_alerts`, `volume_spikes`, `momentum_7d`, `second_wave_candidates`, `predictions`, `narrative_signals`, `chain_matches`, `tg_social_signals`, `candidates`, `paper_trades`, `alerts`. Only trace: one row in `price_cache` from 2026-05-01T00:08Z with `market_cap=0.0` (CoinGecko returned null mcap, our parser writes 0). For context: gainers polling captured 90,002 rows in last 30d; trending captured 5,655. Polling is healthy. RIV simply never appeared in either.
**Best-fit hypothesis (three compounding causes):**
1. **CoinGecko `/coins/markets` 1h-change top-50 cut.** A 100x distributed over 30 days averages ~16%/day; individual 1h windows may rarely hit the top-50 cut. We catch concentrated short pumps (BLESS 16h, GENIUS 10.5h, MEZO 8h) — slow-burn marathons fall through.
2. **`market_cap=0.0` silent rejection.** BL-010 hard-rejects `liquidity_usd < $15K` and the predictions agent floors `market_cap_at_prediction`. CoinGecko returning null mcap → our parser writes 0 → multiple downstream gates auto-drop without logging a rejection. No "rescue" path for tokens with strong price action but missing mcap data.
3. **Trending-list miss.** Our trending poller catches 91.8% of CoinGecko Highlights tokens — but RIV apparently never made that tab during a poll cycle (or pumped between polls). We can't distinguish from the data.
**Honest scope caveat:** n=1. RIV alone doesn't justify a major detection rebuild. The point of Phase A is to find out **how often this is actually happening** before building anything heavyweight.

**Phase A — Cheap diagnostic (1h):**
- Add a `mcap_missing_count` counter to ingestion telemetry (`scout/ingestion/coingecko.py` + heartbeat log).
- Increment when CoinGecko returns a token with `market_cap` null/0 but `current_price > 0`.
- Log to existing heartbeat output every 5min (matches BL-033 pattern).
- **Acceptance:** After 7d of telemetry, we know the rate of mcap-missing silent rejections. Decision tree:
  - If < 1% of unique tokens scanned → silent-rejection is a corner case; close BL-075 as won't-fix on this axis.
  - If 1–5% → worth a fallback (estimate mcap from `volume_24h × ratio` or pull from DexScreener); tractable scope expansion.
  - If > 5% → significant blind spot; Phase B is justified.

**Phase B — Slow-burn watcher (shadow-only, ~0.5d after Phase A):**
- New module `scout/early/slow_burn.py` — separate from existing detection layer.
- Filter: `price_change_7d > 50%` AND `price_change_1h < 5%` (the inverse of velocity_alerter — slow accumulation, not concentrated pump).
- Write to new `slow_burn_candidates` table with snapshot history.
- **No paper trade dispatch.** Research-only, like the original PR #27 velocity_alerter pattern. Shadow soak ≥ 14d before any signal-routing decision.
- **Acceptance:** After 14d shadow soak, count tokens that flagged → became 5x+ runners. If hit-rate matches or beats existing velocity_alerter (~zero false-negative cost; the test is whether the new signal catches misses the existing layer doesn't), promote to a real signal type with paper trade dispatch behind a flag (`SLOW_BURN_DISPATCH_ENABLED=False` default).

**Cross-references (do NOT pre-couple, but worth knowing):**
- BL-073 Phase 1 GEPA on `narrative_prediction` could plausibly evolve a slow-burn classifier as a downstream consumer of the same eval set. Worth re-checking after Phase 1 ships (if it ships).
- BL-032 social signal source decision — slow-burn tokens often have organic social mentions before the price move; a working `social_mentions_24h` signal could complement the slow-burn watcher.
- BL-067 conviction-locked hold — slow-burn signal would be one more independent surface that could stack into conviction-lock once both ship.

**Estimate:** Phase A: 1h. Phase B: 0.5d code + 14d shadow soak before any acceptance read.

**Resume protocol:** Operator says "BL-075" or "RIV miss" or "slow-burn watcher" → start with Phase A. Do not skip to Phase B; the diagnostic data informs whether B is worth building.

---

## P3 — Future / Nice-to-have

### BL-040: Add backtesting framework
**Status:** DONE — backtest CLI implemented (PR #8, `python -m scout.backtest`)
**Why:** PRD Phase 4 (weeks 4-6). Need 30 days of outcome data first. /backtest slash command exists but needs real data.

### BL-041: Add X/Twitter social monitoring
**Status:** MERGED INTO BL-032 (2026-05-03) — see "Social signal source decision". X/Twitter is one of several possible sources; the actual question is "which source fills the dead `social_mentions_24h` signal", not "must we use Twitter specifically". Resolved at the decision level.

### BL-042: Refactor test helpers to use conftest.py fixtures
**Status:** DONE — 17 test files migrated to shared conftest.py fixtures
**Why:** Code review M5 — shared fixtures added to conftest.py but existing tests still use local helpers. Low priority cleanup.

### BL-043: Add Prometheus/Grafana monitoring
**Status:** DEFER UNTIL BL-073 PHASE 2 DECIDED (tagged 2026-05-03)
**Tag:** `defer-until-BL-073-Phase-2` `observability` `parallel-work-risk`
**Why:** Production observability — export scan rates, alert rates, MiroFish latency as metrics.
**Why deferred:** BL-073 Phase 2 (`JackTheGit/hermes-ai-infrastructure-monitoring-toolkit`, 0.5–1d) provides Telegram bot + cron + monitoring as a near-drop-in. If that ships, much of this work is redundant. Decision tree:
- If BL-073 Phase 2 ships → re-scope BL-043 to "Prometheus exporters for what the Hermes monitoring toolkit doesn't cover" (likely much smaller).
- If BL-073 Phase 2 is rejected (operator declines new VPS service) → BL-043 returns to its original full scope.
- If BL-073 Phase 2 is still unfunded at 2026-06-03 (+30d check) → re-evaluate independently.
**Do not parallel-work** with BL-073 Phase 2 — risk of building Prometheus scaffolding that the Hermes toolkit replaces.

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
  - PR #34 — Ensemble virality classifier. Requires ≥3 tiers live + ~2 weeks of labeled data. Tags each alert: `influencer-driven | whale-accumulation | rotation | copycat | news | perp-driven`. Telegram messages gain virality-class badges; exit logic diverges by class (influencer dies in hours, whale runs for days). **Cross-ref (2026-05-03):** the BL-073 Phase 1 framework (`NousResearch/hermes-agent-self-evolution`, DSPy + GEPA) is structurally compatible with this classification problem — if Phase 1 ships and works on `narrative_prediction`, PR #34 becomes a downstream consumer of the same pipeline rather than a separate build. Worth checking before building from scratch.

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
