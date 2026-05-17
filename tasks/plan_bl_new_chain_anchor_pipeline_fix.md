**New primitives introduced:** chain-pattern provenance columns (`is_protected_builtin`, `disabled_reason`, `disabled_at`), protected built-in chain-pattern reconciliation, protected-pattern retirement advisory, chain-anchor health watchdog script/timer.

# Plan: BL-NEW-CHAIN-ANCHOR-PIPELINE-FIX

## Runtime Finding

The backlog item is valid, but the current controlling lever is lower than the original `_check_active_chains` hypothesis.

Production check on 2026-05-17:

| Surface | Runtime evidence |
|---|---|
| `signal_events` narrative anchors | LIVE: `category_heating` last `2026-05-17T13:18:09Z` |
| `signal_events` memecoin candidates | LIVE: `candidate_scored` / `conviction_gated` last `2026-05-17T13:46Z+` |
| `active_chains` | DEAD: max `anchor_time=2026-05-11T16:42:03Z` |
| `chain_matches` narrative | DEAD: max `completed_at=2026-05-11T16:43:05Z` |
| `chain_matches` memecoin | DEAD: max `completed_at=2026-05-04T00:51:02Z` |
| `signal_params.chain_completed` | enabled = 1 |
| `chain_patterns` | all three built-ins are `is_active=0`, so `load_active_patterns()` returns empty and `check_chains()` exits before matching |

Conclusion: fix the pattern-lifecycle state path first. Any `_check_active_chains` instrumentation is secondary unless active patterns are restored and anchors still fail to write.

## Drift Check

| Question | Evidence | Decision |
|---|---|---|
| Does chain tracking already exist? | `scout/chains/events.py`, `scout/chains/tracker.py`, `scout/chains/patterns.py`, and tests exist. | Reuse existing chain tracker; no new detector. |
| Does existing seeding revive stale built-ins? | `seed_built_in_patterns()` only inserts missing pattern names; it does not sync or reactivate existing rows. | Add reconciliation to existing seeding path. |
| Does lifecycle already avoid systemic zero-hit retirement? | `run_pattern_lifecycle()` skips only `total_hits_across_all == 0`. Prod now has low but nonzero historical hits, so all three patterns can be retired. | Add protected built-in retirement guard. |
| Does existing monitoring catch active-pattern starvation? | No active-pattern or `active_chains` freshness watchdog found; existing ingest watchdog monitors source ingestion, not chain table writes. | Add small chain-anchor health watchdog. |

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Chain-pattern lifecycle / retirement / reactivation over gecko-alpha SQLite tables | None installed on VPS. Installed relevant skills are `crypto_narrative_scanner`, `xurl`, and inbound `webhook-subscriptions`; none own gecko-alpha `chain_patterns` or `active_chains`. Public bundled catalog also has no DB pattern lifecycle skill: <https://hermes-agent.nousresearch.com/docs/reference/skills-catalog>. | Build inside existing gecko-alpha chain module. |
| On-chain EVM/Solana token data | Optional Hermes [`blockchain/base` and `blockchain/solana`](https://hermes-agent.nousresearch.com/docs/reference/optional-skills-catalog) exist, but they are read-only chain data clients, not pattern-lifecycle or DB-watchdog tools. | Do not use for this fix; track as future enrichment only. |
| Generic webhook/event routing | Installed `webhook-subscriptions` is inbound agent-run plumbing, not a DB freshness watchdog or chain matcher. Public bundled catalog lists devops/productivity/research skills, not a table freshness primitive. | Not applicable. |
| X/KOL narrative signals | Installed `xurl` + `crypto_narrative_scanner` cover X-side ingestion/classification, not chain pattern matching. | Reuse existing path; out of scope. |

Awesome-Hermes ecosystem check: [`0xNyk/awesome-hermes-agent`](https://github.com/0xNyk/awesome-hermes-agent) lists blockchain-adjacent projects such as `ripley-xmr-gateway`, `hermes-blockchain-oracle`, and `mercury`, but these provide on-chain/wallet intelligence, not gecko-alpha's SQLite chain-pattern lifecycle or `active_chains` write-rate monitoring.

One-sentence verdict: Hermes has useful crypto query skills, but no installed or public Hermes capability replaces gecko-alpha's local chain-pattern lifecycle state, so a small in-tree fix is justified.

Sources checked: Hermes bundled skills catalog, Hermes optional blockchain catalog, and `0xNyk/awesome-hermes-agent`.

## Proposed Fix

1. **Add first-class pattern-state provenance.**
   - Migrate `chain_patterns` with `is_protected_builtin INTEGER NOT NULL DEFAULT 0`, `disabled_reason TEXT`, and `disabled_at TEXT`.
   - Backfill built-in rows as `is_protected_builtin=1`.
   - For current inactive built-ins with lifecycle stats (`historical_hit_rate IS NOT NULL OR total_triggers > 0`), set `disabled_reason='legacy_lifecycle_retired'` so the one-time recovery is explicit rather than a blind `is_active` overwrite.
   - Document operator disables as `is_active=0, disabled_reason='operator_disabled'`; startup reconciliation must not reactivate those.

2. **Reconcile built-in chain patterns at startup without overwriting learned state.**
   - Extend `seed_built_in_patterns()` so existing built-in rows are synced with code-owned definitions.
   - Preserve learning fields: `historical_hit_rate`, `total_triggers`, `total_hits`, `created_at`.
   - Preserve lifecycle-owned fields: `alert_priority`, `is_active`, `disabled_reason`, `disabled_at`, except for explicit legacy lifecycle recovery below.
   - Sync structural fields only: `description`, `steps_json`, `min_steps_to_trigger`, `conviction_boost`, `is_protected_builtin`.
   - Reactivate only protected built-ins with `disabled_reason IN ('lifecycle_retired', 'legacy_lifecycle_retired')`.
   - Never reactivate `disabled_reason='operator_disabled'` or `disabled_reason='code_disabled'`.
   - Emit a structured log with inserted/synced/reactivated counts.

3. **Block automated retirement of protected built-ins.**
   - Treat built-in patterns as code-owned protected defaults.
   - If lifecycle math says a protected built-in should retire, leave `is_active=1`, leave `disabled_reason=NULL`, update stats, and emit `chain_pattern_retirement_blocked_protected` with hit rate and trigger count.
   - Keep retirement behavior for future non-built-in / DB-authored patterns.
   - Rationale: automated retirement of all built-in chain patterns is an outage-class state reversal. Quality decisions for Tier 1a built-ins should be explicit operator/code changes, not silent lifecycle writes.

4. **Make empty active-pattern state loud.**
   - If `CHAINS_ENABLED=True` and `load_active_patterns()` returns empty, log `chain_no_active_patterns` at error level instead of returning silently.
   - This covers startup/runtime visibility even before the watchdog fires.

5. **Add chain-anchor health watchdog.**
   - Add `scripts/check_chain_anchor_health.py`.
   - It reads `chain_patterns`, recent anchor events, and `active_chains`.
   - It fails when:
      - protected built-in patterns are all inactive while `CHAINS_ENABLED=True`, or
      - recent anchor events exist but `active_chains` is stale beyond threshold.
   - It exits OK with `status="disabled"` when `.env` has `CHAINS_ENABLED=False`.
   - It does **not** alert merely because `chain_matches` is stale; completions require later step sequences and are too regime-dependent for a simple freshness SLO.
   - Add `scripts/chain-anchor-health-watchdog.sh` to send Telegram using existing `.env` credentials with `parse_mode=` plain text.
   - Add systemd service/timer under `systemd/`.

6. **Deploy note / prod recovery.**
   - After PR merge, deploy code and restart `gecko-pipeline`.
   - Startup reconciliation should reactivate built-ins.
   - Verify:
      - `SELECT name,is_active,alert_priority,is_protected_builtin,disabled_reason FROM chain_patterns;`
      - `journalctl -u gecko-pipeline -g chain_patterns_seeded_or_synced --since "10 minutes ago"`
      - Within one tracker tick, `active_chains` max anchor time should advance if anchor events are present.
      - Run a local synthetic complete-sequence test in the harness to prove `chain_matches` still writes; prod completion is then monitored by follow-up evidence, not immediate freshness alerting.

## Test Plan

1. `tests/test_chains_patterns.py`
   - Existing inactive built-ins are reactivated by seeding.
   - Stale steps/priority are synced while learning stats are preserved.
   - Existing learned `alert_priority` is preserved.
   - `disabled_reason='operator_disabled'` built-ins stay inactive.
   - Seeding remains idempotent.
   - Lifecycle does not retire protected built-ins on low-but-nonzero hit rate; it still updates stats.
   - A synthetic non-built-in pattern can still retire, proving retirement is not globally disabled.

2. `tests/test_chains_tracker.py`
   - `check_chains()` logs/raises no exception when no active patterns, but emits an error-level `chain_no_active_patterns`.

3. `tests/test_chain_anchor_health_watchdog.py`
   - OK when recent anchors and fresh active chains exist.
   - Fails when all patterns inactive and recent anchor events exist.
   - Fails when recent anchors exist but `active_chains` is stale.
   - OK/disabled when `CHAINS_ENABLED=False`.

4. Verification commands:
   - `python -m pytest tests/test_chains_patterns.py tests/test_chains_tracker.py tests/test_chain_anchor_health_watchdog.py -q`
   - Wider chain suite: `python -m pytest tests/test_chains_events.py tests/test_chains_db.py tests/test_chains_patterns.py tests/test_chains_tracker.py tests/test_chains_integration.py tests/test_chain_outcomes_hydration.py tests/test_narrative_chain_coherence.py -q`

## Rollback

- Code rollback restores old lifecycle behavior.
- If reactivation causes unwanted noise, operator can set `CHAINS_ENABLED=False` as the coarse kill switch or set specific patterns to `is_active=0, disabled_reason='operator_disabled'`.
- Watchdog timer can be disabled independently: `systemctl disable --now chain-anchor-health-watchdog.timer`.

## Open Review Questions

1. Should all three built-in patterns be protected, or only narrative `full_conviction` / `narrative_momentum`?
2. Should we add a one-time replay/backfill for the May 11 to May 17 outage window, or keep this PR forward-only?
3. Are watchdog thresholds better as env settings in `Settings`, or shell environment variables in the systemd unit?

## Plan Review Fold

- Reviewer A/B HIGH: do not overwrite lifecycle-owned `alert_priority`; folded by preserving `alert_priority` for existing rows.
- Reviewer A/B HIGH: do not blindly reverse operator disables; folded by adding pattern-state provenance and reactivating only lifecycle-retired protected built-ins.
- Reviewer A/B MEDIUM: `chain_matches` freshness is not equivalent to anchor-writer health; folded by removing raw completion freshness from the watchdog and using `active_chains` as the direct writer-health table.
- Reviewer B MEDIUM: watchdog must respect `CHAINS_ENABLED=False`; folded as explicit disabled behavior.
- Reviewer B LOW: Hermes-first entries need URLs; folded with official catalog and awesome-list links.
