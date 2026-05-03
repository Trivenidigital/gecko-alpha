# agentskills.io browse — Phase 0 of BL-073 — 2026-05-03

<!-- new-primitives-check: bypass -->

**Acceptance:** ≥1 skill imported and concretely evaluated against gecko-alpha data, OR ≥3 candidate skills documented with specific reject reasons.

**Status:** DONE — concrete starting frameworks identified for Phases 1 and 2; ≥3 skills/repos rejected with specific reasons. Ecosystem turned out to be much larger than the original v1 of this note claimed (see "Honest reflection" at bottom).

## What the Hermes ecosystem actually is

Verified via 4 parallel `WebFetch` calls plus README reads:

- **`hermes-agent.nousresearch.com/docs/skills`** — official skills hub, **671 skills** across 16 categories. Includes Hermes-built-in MLOps skills like `dspy`, `evaluating-llms-harness`, `weights-and-biases`. This is the "marketplace" the original framing assumed didn't exist.
- **`agentskills.io`** — separate site; SKILL.md spec/standard (17.8k stars). Useful for understanding the format, not a catalog.
- **`github.com/0xNyk/awesome-hermes-agent`** — community-curated list pointing at production-grade Hermes apps and skill packs. Where I should have started.
- **`hermeshub.xyz`** — SPA, WebFetch couldn't extract content; not load-bearing for this analysis.

The original v1 of this file ("agentskills.io is spec-only, no marketplace exists") was correct *narrowly* (about agentskills.io itself) but wrong *broadly* — the real Hermes skill ecosystem lives at `hermes-agent.nousresearch.com/docs/skills` and was linked from the Hermes README I had already read. I missed it.

## Concrete starting frameworks per BL-073 phase

### Phase 1 — GEPA on `narrative_prediction` LLM prompt

**Starting framework:** `NousResearch/hermes-agent-self-evolution` (MIT, 2.7k stars).

This is the working DSPy + GEPA pipeline applied to Hermes prompts. Forking it and pointing the optimizer at the 1,274-row `predictions` table eval set (42 HIT / 40 MISS / 566 NEUTRAL / 561 UNRESOLVED) is **substantially less work** than building from scratch with raw `dspy` + `gepa` libraries.

**Supporting Hermes built-in skills (671-skill hub):**
- `dspy` — MLOps category, prompt-program compilation
- `evaluating-llms-harness` — MLOps, eval harness wiring
- `weights-and-biases` — MLOps, run tracking for the eval sweep

**Revised cost:** ~$10 + ~1d (was ~$10 + 2d in original BL-073 entry).

### Phase 2 — Hermes ops agent on VPS (Telegram NL access, scheduled checks, gateway)

**Starting framework:** `JackTheGit/hermes-ai-infrastructure-monitoring-toolkit`.

Near-drop-in for what Phase 2 sketched: Telegram bot interface + cron-driven health checks + cross-platform messaging gateway. Fork, swap the monitored systems for gecko-alpha's systemd services and SQLite paper-trade tables, ship.

**Revised cost:** ~0.5–1d + $5/mo VPS service (was 1–2d + $5/mo in original BL-073 entry).

### Bonus tools worth knowing (not BL-073 phases, but in the toolbox)

- `builderz-labs/mission-control` (3.7k stars) — agent fleet dashboard, SQLite-backed. If we ever spin up multiple Hermes agents (Phase 2 + Phase 3 + Phase 4), this is the visibility layer.
- `roli-lpci/lintlang` — config-file linter for skills. Useful once skill count exceeds ~5.
- `AMAP-ML/SkillClaw` — skill auto-evolution (skills that rewrite themselves based on usage). Speculative; not Phase 1–4 scope.
- `gizdusum/hermes-blockchain-oracle` — Solana-only on-chain analytics. Maybe relevant for a future on-chain signal source; not chain-agnostic so doesn't fit gecko-alpha's current shape.

## Reject reasons (≥3, satisfying the acceptance back-stop)

1. **`Chainlink/chainlink-agent-skills`** — wrong oracle model. Chainlink's price feeds are aggregated/lagged for stable contract use; gecko-alpha needs the noisy, fast, per-DEX feeds CoinGecko/DexScreener already provide. Adding Chainlink would give us slower data at higher cost.
2. **`hxsteric/mercury`** — solves a different problem (high-frequency cross-chain arbitrage routing). gecko-alpha is a *signal* pipeline that produces alerts and paper trades; we don't route execution. Adopting Mercury would mean adopting an execution layer we don't have a use case for.
3. **`KYC-rip/ripley-xmr-gateway`** — Monero-specific. gecko-alpha is chain-agnostic but the actual signal sources (CoinGecko/DexScreener/GeckoTerminal) don't cover XMR; no ingestion path exists. Wrong chain.
4. **No paper-trading-loop skill exists in the 671-skill hub.** The closest matches (`backtest`, `portfolio-tracker`) are positioned for human-in-the-loop investing. Our paper-trade engine has its own ladder/SL/trail/expiry semantics that don't match. Build, don't fork.
5. **No CoinGecko-specific or DexScreener-specific skill exists.** Built-in `http-client` + `rate-limiter` skills could in theory be composed, but our existing `scout/ingestion/coingecko.py` already does the rate-limit math, async session management, and Pydantic validation more tightly than a generic skill would. Replacing would be a downgrade.
6. **No SQLite-audit-log skill exists.** Several `database` skills are Postgres-first. Our `aiosqlite` setup with `_txn_lock` and migration pattern is well-fit; no win in switching.

## Implications for BL-073 sequencing

- **Phase 0 (this note)** — DONE. Real frameworks identified, costs revised down.
- **Phase 1** is now cheaper and faster (1d vs 2d) because `hermes-agent-self-evolution` provides the scaffold. The "operator commits funding + bandwidth" gate still stands, but the bandwidth cost is roughly halved.
- **Phase 2** is now cheaper and faster (0.5–1d vs 1–2d) because `hermes-ai-infrastructure-monitoring-toolkit` is a near-drop-in. The new-VPS-service approval gate still stands.
- **The 90-day cancellation criterion (close BL-073 as won't-fix by 2026-08-03 if Phase 1 hasn't started) now looks pessimistic.** With Phase 1 down to ~1d of work, the activation barrier is mostly operator attention, not engineering risk. Worth revisiting at the +30d check (2026-06-03).
- Phases 3, 4, 5 are unaffected by this note — their gates are independent.

## Honest reflection

The original v1 of this file was lazy in two compounding ways:

1. **I treated the prefix-match "agentskills.io" as the whole ecosystem.** It's the spec site. The actual skill catalog is at `hermes-agent.nousresearch.com/docs/skills`, which was linked from the Hermes README I'd already read at session start. A 30-second WebFetch of that URL — which I now know returns 671 skills — would have surfaced the real ecosystem during the original Phase 0 deferral.
2. **I treated "marketplace doesn't exist" as a closing argument** instead of the question it actually was ("where do production Hermes apps get reusable building blocks from?"). The community curation at `awesome-hermes-agent` answers that question concretely with named repos and stars.

Both errors were structural — pattern-matching on the first URL the user mentioned instead of doing the breadth-first research the acceptance criterion explicitly asked for. The user's correction ("you are becoming lazy these days") was right. This rewrite is the corrected version.
