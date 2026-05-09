**New primitives introduced:** `CandidateToken.quote_symbol`, `CandidateToken.dex_id`, `candidates.quote_symbol` column, `candidates.dex_id` column, scorer signal `stable_paired_liq` (+5pts raw), Settings `STABLE_PAIRED_LIQ_THRESHOLD_USD` + `STABLE_PAIRED_BONUS` + `STABLE_QUOTE_SYMBOLS`, migration `bl_quote_pair_v1` (schema_version 20260512). Migration writes a row to existing `schema_version` table (singular, per `scout/db.py:1030`).

# Plan — BL-NEW-QUOTE-PAIR: Quote-currency liquidity-quality signal

## Hermes-first analysis

Mandatory per CLAUDE.md §7b. Checked the Hermes skill hub at
`hermes-agent.nousresearch.com/docs/skills` (2026-05-09).

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Crypto DEX parsing (DexScreener `quoteToken` extraction) | None found in 684-skill catalog under Finance / Software Dev | Build from scratch — pure parser addition, ~6 LOC |
| Token scoring / multipliers | None found | Build from scratch — scorer is gecko-alpha-internal abstraction |
| Stable-currency classification | None found | Build from scratch — hardcoded set of {USDC, USDT, DAI, FDUSD, USDe} |

awesome-hermes-agent ecosystem check: no community skill listed for crypto pair-data parsing or aggregator-API quote-token handling. Verdict: **build from scratch is correct**; this is gecko-alpha-internal scoring logic with no domain match in Hermes.

## Drift-check (per CLAUDE.md §7a)

Verified 2026-05-09 against current tree (commit `084bdd7`):

1. `grep -rn "quote_symbol\|quote_token\|quoteToken" scout/models.py scout/ingestion/dexscreener.py` → **no matches** in models.py or DS parser.
2. `CandidateToken.from_dexscreener` (`scout/models.py:104-`) extracts `baseToken` only, ignores `quoteToken` field returned by DexScreener API.
3. `scorer.py` has 11 signals (liquidity floor, buy pressure, age curve, score velocity, co-occurrence, signal_confidence, mcap_tier_curve, solana_bonus, momentum_ratio, vol_acceleration, cg_trending_rank); none differentiate by quote currency.
4. `backlog.md` BL-010 (liquidity floor) shipped; BL-011 (buy pressure) shipped. No item in backlog scopes quote-currency awareness — confirmed not previously proposed.

Drift verdict: **net-new**, no in-tree match.

## Why this matters

The user's stated trading goal (memory `user_trading_goals.md`): *manual research, chain-agnostic, beat CoinGecko Highlights by minutes*. A token paired only with WETH or SOL has materially different exit dynamics than one paired with USDC/USDT — exit slippage on a $50K WETH-only pair during a vol-spike forces the holder to take secondary slippage on the WETH→USD leg.

Industry precedent (Birdeye filters, GMGN.ai): "stable-pair preferred" is a standard liquidity-quality discriminator. The pipeline currently throws away the `quoteToken` field DexScreener returns for free.

## Magnitude analysis (post R1 reviewer correction)

`SCORER_MAX_RAW = 208` (verified `scout/scorer.py:37`). +5 raw points → `int(5 * 100 / 208) = +2` normalized points (not +5 — earlier draft misstated). `MIN_SCORE = 60`, `CONVICTION_THRESHOLD = 70` (verified `scout/config.py:21-22`; backlog.md D1's "25/22" is stale documentation).

**Co-occurrence interaction (R1 CRITICAL).** Co-occurrence multiplier fires AFTER normalization at `scout/scorer.py:230-231` when `len(signals) >= CO_OCCURRENCE_MIN_SIGNALS` (= 3), multiplying by `CO_OCCURRENCE_MULTIPLIER` (= 1.15). `stable_paired_liq` is appended to `signals`, so the signal IS counted toward co-occurrence. Worst-case path: a token at 2 signals + score 60 → adding stable_paired_liq pushes to 3 signals → 60 + 2 = 62 → × 1.15 = 71 → past `CONVICTION_THRESHOLD=70`. This is the **dominant** mechanical effect, not the +2pt direct bonus.

**Design decision: keep stable_paired_liq in `signals`** (it IS evidence — stable-paired tokens are materially distinct from non-stable-paired). The 1.15× co-occurrence amplification on tokens already showing 2 other independent signals is *intended* behavior — that's exactly what co-occurrence is designed to reward. Tokens with 0-1 other signals get only the +2 normalized direct bonus, which is a true tiebreaker.

**Predicted alert-volume uplift:** ≤15% (revised down from blind <10% claim). Bound: only tokens currently at 2 signals AND stable-paired AND ≥$50K liquidity AND scoring 58-67 normalized would flip past the gate. Verification step required mid-soak: query `candidates` post-deploy for distribution.

## What's in scope

1. New `CandidateToken` fields (Pydantic, optional, nullable):
   - `quote_symbol: str | None` — DexScreener `quoteToken.symbol` (e.g., `"WSOL"`, `"USDC"`)
   - `dex_id: str | None` — DexScreener `dexId` (e.g., `"raydium"`, `"uniswap"`) — captured opportunistically since we're touching the parser; useful for downstream attribution + future per-DEX scoring.

2. Parser update: `CandidateToken.from_dexscreener` extracts both fields.
   - Use `(data.get("quoteToken") or {}).get("symbol")` and `data.get("dexId")` — the `or {}` guard handles `"quoteToken": null` API responses (R2 NIT, matches existing `priceChange` pattern at `scout/models.py:123`).

3. DB migration `bl_quote_pair_v1`:
   - Pattern: follow `_migrate_feedback_loop_schema` at `scout/db.py:1077` — `PRAGMA table_info(candidates)` → set existing → conditional `ALTER TABLE candidates ADD COLUMN <col> TEXT` for each missing.
   - Idempotent: re-running the migration on an already-migrated DB skips with `schema_migration_column_action action=skip_exists` log.
   - Schema-version write: existing table is `schema_version` (singular, NOT `schema_versions`) — confirmed at `scout/db.py:1030`. Insert via project's `_record_schema_version` helper if present, otherwise raw `INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (20260512, ...)`.
   - Both columns nullable (pre-cutover rows = NULL, per `feedback_mid_flight_flag_migration.md` discipline).

4. Persistence: `db.upsert_candidate` writes both fields when present.

5. Settings additions (`scout/config.py`):
   - `STABLE_QUOTE_SYMBOLS: tuple[str, ...] = ("USDC", "USDT", "DAI", "FDUSD", "USDe", "PYUSD", "RLUSD", "sUSDe")` — R1 stable list update for 2026: PYUSD (PayPal, top-5 ETH stable), RLUSD (Ripple, active on DEXes since Q4 2025), sUSDe (synthetic yield stable). BUSD/TUSD intentionally excluded (BUSD redemption-only since Feb 2024; TUSD repeated depegs).
   - `STABLE_PAIRED_LIQ_THRESHOLD_USD: float = 50_000.0`
   - `STABLE_PAIRED_BONUS: int = 5` (raw — translates to +2 normalized post `SCORER_MAX_RAW=208` divisor)

6. Scorer signal (`scout/scorer.py`):
   - **Inlined** alongside existing signals (matches `scorer.py:85, 102, 132`-style pattern — R2 NIT). NO helper function — every existing signal is inlined; introducing a helper for one signal creates a copy-this-pattern-incorrectly hazard.
   - Wire as: `if token.quote_symbol in settings.STABLE_QUOTE_SYMBOLS and token.liquidity_usd >= settings.STABLE_PAIRED_LIQ_THRESHOLD_USD: points += settings.STABLE_PAIRED_BONUS; signals.append("stable_paired_liq")`.
   - `SCORER_MAX_RAW` stays at 208 — adding +5 to ceiling is unnecessary (a single signal cannot dominate; the normalization clamp at line 227 still works correctly). Existing 30d calibration preserved.

7. Tests:
   - `tests/test_models.py::test_from_dexscreener_extracts_quote_symbol_and_dex_id` (positive)
   - `tests/test_models.py::test_from_dexscreener_handles_missing_quote_token` (graceful degradation)
   - `tests/test_scorer.py::test_stable_paired_liq_bonus_fires_for_usdc_above_threshold`
   - `tests/test_scorer.py::test_stable_paired_liq_bonus_blocked_below_threshold`
   - `tests/test_scorer.py::test_stable_paired_liq_bonus_blocked_for_non_stable_quote`
   - `tests/test_scorer.py::test_stable_paired_liq_bonus_handles_none_quote_symbol`
   - `tests/test_db.py::test_upsert_candidate_persists_quote_symbol_and_dex_id` (round-trip)
   - `tests/test_migrations.py::test_bl_quote_pair_v1_idempotent` (re-run)

8. Docs:
   - Update `CLAUDE.md` "3 New Scoring Signals" table → 4 signals + new row for `stable_paired_liq`.
   - Update `docs/gecko-alpha-alignment.md` if it lists scoring signals.

## What's out of scope

- **Per-DEX scoring** — `dex_id` captured but not scored. Future work.
- **Quote-currency aggregation across pools** — DexScreener already returns multiple pairs per token; we use first-pair-only today. Aggregation across pools is BL-NEW-MULTIPOOL-LIQ (separate proposal, not blocking).
- **Scoring re-normalization** — SCORER_MAX_RAW stays at 183. Changing it is BL-016 territory and invalidates current 30d calibration.
- **Backfill of `quote_symbol`/`dex_id` for pre-cutover rows** — they stay NULL by design (mid-flight flag migration discipline).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Score inflation: +5pts could push borderline tokens past `MIN_SCORE=25` and cause alert flood | Backtest on 30d history of `candidates` post-deploy; verify alert volume change < 10%. |
| DexScreener pairs without `quoteToken.symbol` field | Parser uses `.get("symbol")` → graceful None; scorer tolerates None. |
| Stable list out-of-date (e.g., new USD stable launches) | Configurable via Settings, env-overridable. |
| Backfill burden on operator | Skipped by design — pre-cutover rows = NULL is fine for forward-looking analysis. |
| Calibration drift: +5pts shifts per-signal kill-switch baselines | Not a blocker — BL-NEW-AUTOSUSPEND-FIX combined-gate works on absolute P&L not score. Per-signal `SignalParams` are independent. |

## Tasks

1. **Add Pydantic fields + parser update** (`scout/models.py`)
   - Add `quote_symbol: str | None = None` and `dex_id: str | None = None` to CandidateToken
   - Update `from_dexscreener` to extract both
   - Tests in `tests/test_models.py`

2. **Add Settings fields** (`scout/config.py`)
   - `STABLE_QUOTE_SYMBOLS`, `STABLE_PAIRED_LIQ_THRESHOLD_USD`, `STABLE_PAIRED_BONUS`
   - Test presence in `tests/test_config.py`

3. **DB migration `bl_quote_pair_v1`** (`scout/db.py`)
   - Add `_migrate_bl_quote_pair_v1` method following pattern of existing `_migrate_*` helpers
   - Add ALTER TABLE statements; idempotency check via `PRAGMA table_info(candidates)`
   - Increment schema_version to 20260512
   - Wire in `_apply_migrations`
   - Update `upsert_candidate` SQL to include new columns
   - Tests in `tests/test_migrations.py` + `tests/test_db.py`

4. **Scorer signal** (`scout/scorer.py`)
   - `_score_stable_paired` helper
   - Wire into `score()` aggregation
   - Tests in `tests/test_scorer.py` (4 cases)

5. **Docs update**
   - `CLAUDE.md` scoring-signals table
   - `tasks/todo.md` — mark soak window 7d post-deploy

## Acceptance criteria

- All new tests pass (≥7 new test cases)
- Full regression suite passes (`uv run pytest --tb=short -q`) — current count 1389+
- `black scout/ tests/` clean
- Migration applied on prod DB without errors
- Forward-looking: rows in `candidates` post-cutover have `quote_symbol`/`dex_id` populated for DexScreener-sourced tokens (CG/GT-only sources stay NULL, expected)
- Soak: 7-day observation window after deploy. Alert volume should not increase by > 10%; predicted ≤15% with active monitoring on D+3 verification query (R1 MUST-FIX correction). Per memory `project_session_2026_04_28_strategy_tuning.md`, weekly cadence is hundreds of paper closes — n≈350 weekly events gives 7d soak well-powered for 10% shift detection (z ≈ 2.8, p < 0.005).

## Soak + revert plan

- Default-on (additive +2 normalized pts via +5 raw is low-risk; no kill switch needed for deploy).
- **Mid-soak verification (D+3):** query `SELECT COUNT(*), SUM(CASE WHEN quote_symbol IN ('USDC','USDT','DAI','FDUSD','USDe','PYUSD','RLUSD','sUSDe') AND liquidity_usd>=50000 THEN 1 ELSE 0 END) FROM candidates WHERE first_seen_at >= '2026-05-09'` to confirm fraction of candidates that satisfy the gate. If fraction > 40%, escalate scrutiny.
- **Revert threshold matches acceptance threshold:** if alert volume increases > 10% in 7d → revert via `STABLE_PAIRED_BONUS=0` env override (no code rollback). Earlier draft set revert at 20% — closed gap to 10% per R1 NIT.
- If migration fails on VPS → revert via column-removal migration (safe because `quote_symbol`/`dex_id` are nullable + only newly-added).

## Estimate

~2-3 hours coding + tests. ~30 min reviewer dispatch + fix cycles. ~30 min PR + reviewers + merge + deploy.

## Reviewer dispatch (per workflow)

- **Plan-stage reviewers (2 parallel):**
  - R1 (statistical/data): is +5pts the right magnitude? Does the stable list cover ≥95% of stable-paired liquidity? Will this cause alert-flood regression?
  - R2 (code-structural): is the migration shape correct (idempotent, rollback-safe)? Does Pydantic field default-None preserve backward compat?
