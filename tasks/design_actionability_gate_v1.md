**New primitives introduced:** `scout.trading.actionability.ActionabilityDecision`; `scout.trading.actionability.evaluate_actionability_v1`; `PaperTrader` actionability market-cap enrichment helper; `paper_trades.actionable`; `paper_trades.actionability_reason`; `paper_trades.actionability_version`; migration marker `bl_new_actionability_gate_v1`.

# Actionability Gate v1 Design

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Paper-trade actionability attribution | none found in installed VPS skills or public Skills Hub (`https://hermes-agent.nousresearch.com/docs/skills`) | build in repo; this is gecko-alpha paper-trade DB metadata |
| X/KOL source intelligence | yes - installed `social-media/xurl`, `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher` | reuse as raw telemetry only; outcome linkage is not ready for handle ranking |
| Crypto trading/data skills | crypto/trading-adjacent ecosystem entries exist, no drop-in gecko-alpha paper cohort classifier | keep project-owned DB/scoring/trading boundary |
| Dashboard/reporting | no Hermes primitive for this repo's `paper_trades` fields | defer UI until metadata exists |

Awesome-hermes-agent ecosystem check: no drop-in actionability gate for gecko-alpha paper-trade rows. Verdict: custom in-repo metadata classifier is justified.

## Goal

Add an audit-derived `actionable` cohort marker to paper trades without suppressing exploratory paper trading, raw signal rows, live handoff behavior, or exit policy. `would_be_live` remains live-slot eligibility; Actionability Gate v1 is a separate paper decision-quality label.

## Data Model

Add nullable columns to `paper_trades`:

```sql
actionable INTEGER,
actionability_reason TEXT,
actionability_version TEXT
```

No default is intentional. Historical rows and raw SQL inserts remain `NULL`, so v1 cohorts must filter with:

```sql
actionable = 1 AND actionability_version = 'v1'
```

Migration wiring:

- Add columns to `_create_tables` for fresh DBs.
- Add columns to `_migrate_feedback_loop_schema.expected_cols`.
- Insert `paper_migrations` marker `bl_new_actionability_gate_v1`.
- Add that marker to the existing post-assertion list and `missing_migrations` set.
- Test timestamp preservation on reinitialize.

## Pure Classifier

`scout.trading.actionability.evaluate_actionability_v1(...)` is pure and does not query DB. `actionability_reason` stores the first matching reason as plain `TEXT`; multi-reason JSON is deferred until v1 proves useful enough to need richer audit payloads.

Inputs:

- `signal_type: str`
- `signal_data: dict[str, Any]`
- `signal_combo: str | None`
- `conviction_stack: int = 0`

Output:

```python
ActionabilityDecision(actionable: bool, reason: str, version: str = "v1")
```

Rules:

- `narrative_prediction`, `volume_spike`: pass at mcap `>=10m`; block missing or below `10m`.
- `chain_completed`: pass at mcap `>=10m`; if mcap remains missing after enrichment, pass with `v1_pass_chain_completed_mcap_unknown_exception`.
- `gainers_early`: block `5-10m`; block confluence `>=3`; block `10-50m` as observe-only; pass only `>=50m`.
- `losers_contrarian`, `trending_catch`, `tg_social`: non-actionable by default.
- Unknown signal types: non-actionable.

Confluence is:

```python
max(parsed_signal_combo_part_count, conviction_stack)
```

This avoids trusting `signal_combo` alone, because the current combo builder can cap combinations while the findings used stack-derived confluence.

## Market-Cap Enrichment

The engine does not guarantee mcap in `signal_data`. The implementation uses both source-side carry-forward and edge enrichment before calling the pure classifier.

Priority:

1. Existing `signal_data` keys: `mcap`, `market_cap`, `market_cap_usd`, `mcap_at_sighting`, `alert_market_cap`.
2. `trade_volume_spikes` must carry `VolumeSpike.market_cap` into `signal_data` as `mcap` so the real spike path does not depend on a cache lookup it may bypass with `entry_price`.
3. For `chain_completed`, latest non-null `chain_matches.mcap_at_completion`.
4. Generic `price_cache.market_cap`.

Failure policy:

- Query errors log `actionability_mcap_enrichment_failed`.
- The trade still opens.
- The classifier receives the original payload and returns a deterministic reason.

## Paper Open Integration

`PaperTrader.execute_buy` computes actionability before the insert and writes all three columns in the same `INSERT`.

Actionability is computed even when `settings is None`. `settings` only controls `would_be_live`; actionability is local metadata and should be stamped for the main writer path whenever possible.

Stack integration:

- Compute `conviction_stack` for actionability outside the `settings is not None` `would_be_live` block.
- Reuse that value for `would_be_live` when settings are present.
- If stack computation fails for `gainers_early`, stamp non-actionable metadata with `v1_block_gainers_early_stack_unavailable` and continue opening the paper row. This is a fail-closed metadata decision because stack-derived confluence is required to detect the bad `confluence:3` bucket.
- If stack computation fails for other signals, use stack `0` and continue.

Exception policy:

- Enrichment failures are caught inside enrichment and fall back to the original payload.
- Classifier failures are caught around the classifier and stamp `v1_error`.
- No actionability failure path may raise before the paper-trade insert.

`paper_trade_opened` logs include:

- `actionable`
- `actionability_reason`
- `actionability_version`

The live handoff stays unchanged: a non-actionable paper row can still pass to the injected `LiveEngine` if the existing allowlist says it is eligible. This PR only adds metadata.

## Tests

Add red/green coverage for:

- Pure classifier pass/block reasons, including invalid mcap fallback and conviction-stack confluence.
- Nullable DB columns, marker row, marker timestamp preservation, and legacy row `NULL` preservation.
- Direct `PaperTrader.execute_buy` stamping.
- `TradingEngine.open_trade` stamping non-actionable rows without suppressing opens.
- `trade_volume_spikes` carries `VolumeSpike.market_cap` into `signal_data`.
- `TradingEngine.open_trade` enrichment from `price_cache.market_cap` using a real `volume_spike` signal-data shape without mcap.
- Stack-compute failure for `gainers_early` stamps `v1_block_gainers_early_stack_unavailable` and still opens.
- Enrichment does not mutate persisted `signal_data`.
- Live handoff unchanged for non-actionable rows.

Focused verification:

```powershell
$env:PYTHONPATH=(Get-Location).Path
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_actionability.py tests/test_paper_actionability.py tests/test_live_eligibility.py tests/test_trading_engine.py tests/test_trading_db_migration.py tests/live/test_paper_chokepoint.py --tb=short -q
```

Adjacent verification:

```powershell
$env:PYTHONPATH=(Get-Location).Path
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_trading_*.py tests/live/test_paper_chokepoint.py tests/test_live_eligibility.py tests/test_actionability.py tests/test_paper_actionability.py --tb=short -q
```

## Deferred Work

- X handle and TG channel ranking wait for outcome linkage.
- Peak/no-peak risk handling waits for a separate exit-policy design because `peak_pct` is not known at trade open.
- Dashboard UI waits until metadata exists and tests prove the write path.
- Live-readiness policy changes are out of scope.
