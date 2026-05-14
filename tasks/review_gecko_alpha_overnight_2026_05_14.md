**New primitives introduced:** NONE

# Overnight gecko-alpha repo review - 2026-05-14

Branch: `codex/overnight-repo-review`

Scope: broad static/runtime-boundary pass over ingestion, trading, live-order,
DB-audit, and test-contract surfaces. This review prioritized silent failures,
stale runtime state, attribution drift, and tests that had fallen behind the
current configuration contract.

## Drift and Hermes-first analysis

No new external primitive or new integration capability was introduced in this
PR. The fixes below repair existing in-tree primitives: CoinGecko ingestion raw
payload caches, paper-trade lifecycle accounting, synthetic-token rejection,
live-order idempotency attribution, and signal-parameter audit history.

| Domain | In-tree / Hermes capability found? | Decision |
|---|---|---|
| CoinGecko current-cycle raw payload handling | In-tree mutable globals `last_raw_markets`, `last_raw_trending`, `last_raw_by_volume` | Reuse and repair in-tree primitive; no custom external capability needed |
| Paper-trade ladder accounting | In-tree `remaining_qty` + `realized_pnl_usd` columns and partial-sell logic | Reuse existing lifecycle fields for final close PnL |
| Trade prediction token-id resolution guard | In-tree `coin_id_resolves` and existing fail-closed synthetic-token skip logs | Extend existing guard to operational resolver errors while preserving helper-drift propagation |
| Live-order idempotency attribution | In-tree `OrderRequest` + `record_pending_order(signal_type=...)` | Wire existing accepted field through the adapter; no new order primitive |
| Signal-params audit old-value tracking | In-tree `signal_params_audit` | Record actual prior value instead of hardcoded old value |

Hermes verdict: no Hermes replacement was warranted because no new capability
was scoped. If the deferred live-order terminal-state repair is implemented, it
should run the full section 7 flow first: drift-check existing `live_trades`,
`lookup_existing_order_id`, reconciliation, and review-status patterns, then
Hermes/VPS skills/plugins only if a genuinely new exchange-state reconciliation
primitive is being proposed.

## Findings fixed

### P1 - CoinGecko raw globals could fresh-stamp stale prices

Evidence: `scout/main.py` consumes `scout.ingestion.coingecko.last_raw_*`
globals after a cycle. Before this patch, `fetch_top_movers()` and
`fetch_by_volume()` returned `[]` on outage/no-data paths without clearing the
previous cycle's raw payloads, so the price-cache writer could stamp stale
prices with a fresh `updated_at`.

Fix: clear `last_raw_markets`, `last_raw_trending`, and `last_raw_by_volume`
before each current-cycle fetch in `scout/ingestion/coingecko.py`.

Regression tests:
- `tests/test_coingecko.py::test_fetch_top_movers_outage_clears_previous_raw_cache`
- `tests/test_coingecko.py::test_fetch_by_volume_outage_clears_previous_raw_cache`
- `tests/test_coingecko.py::test_fetch_trending_outage_clears_previous_raw_cache`

### P1 - Trade prediction resolution errors could crash the signal loop

Evidence: `trade_predictions()` already failed closed for documented
synthetic-token resolution exceptions, but a runtime DB/probe error from
`coin_id_resolves()` propagated and aborted processing instead of skipping the
bad prediction.

Fix: documented operational resolver failures emit
`signal_skipped_synthetic_token_id` with `reason="resolution_check_error"` and
continue. Programmer/contract drift still fails loud: `TypeError`,
`AssertionError`, and `AttributeError` are not downgraded to skips, and
`asyncio.CancelledError` is not caught.

Regression test:
- `tests/test_narrative_prediction_token_id.py::test_resolution_check_error_fails_closed`
- `tests/test_narrative_prediction_token_id.py::test_resolution_contract_drift_fails_loud`

### P2 - Final paper close overstated ladder-trade PnL after partial exits

Evidence: partial exits update `remaining_qty` and `realized_pnl_usd`, but
`execute_sell()` closed the final leg using the original `quantity` only. That
double-counted already-sold quantity and ignored previously realized PnL.

Fix: final close now computes total PnL as
`realized_pnl_usd + remaining_qty * (effective_exit - entry_price)`, falls back
to original quantity for legacy rows with `remaining_qty IS NULL`, and sets
`remaining_qty=0` for lifecycle-managed rows.

Regression test:
- `tests/test_paper_trader.py::test_execute_sell_after_partial_uses_remaining_qty_and_realized_pnl`

### P2 - Race-lost paper DML paths left implicit SQLite transactions open

Evidence: `execute_partial_sell()` and `execute_sell()` returned `False` on
zero-row updates after DML without closing the implicit SQLite transaction.
That can amplify concurrent evaluator/dispatcher lock contention.

Fix: both rowcount-zero paths now commit before returning, mirroring the
existing `arm_moonshot()` transaction-closure pattern.

Regression coverage:
- `tests/test_paper_trader.py::test_execute_partial_sell_idempotent_on_double_call`
- `tests/test_paper_trader.py::test_execute_sell_after_partial_uses_remaining_qty_and_realized_pnl`

### P2 - Live Binance rows lost `signal_type` attribution

Evidence: `record_pending_order()` already accepts `signal_type`, but the
Binance adapter passed an empty string, so live rows could not feed per-signal
approval thresholds or attribution.

Fix: `OrderRequest` now requires `signal_type`; `LiveEngine` sets it from the
paper trade; `BinanceSpotAdapter` passes it into `record_pending_order()`.
Adapter-level persistence coverage asserts the actual `live_trades.signal_type`
row value so the test does not stop at a mocked engine boundary.

Regression test:
- `tests/live/test_live_engine.py::test_live_dispatch_preserves_signal_type_in_order_request`
- `tests/test_live_binance_adapter_signed.py::test_place_order_request_first_attempt_records_then_submits`

### P3 - Signal-params revive audit hardcoded old `tg_alert_eligible`

Evidence: `revive_signal_with_baseline()` restored `tg_alert_eligible`, but the
audit row always recorded old value as `"0"` even when the actual old value was
already `1`.

Fix: select the current `tg_alert_eligible` value and record it as the audit
old value.

Regression coverage:
- `tests/test_signal_params_auto_suspend.py::test_revive_signal_with_baseline_stamps_baseline_and_audit`
- `tests/test_signal_params_auto_suspend.py::test_revive_signal_with_baseline_on_already_enabled_signal`

### P3 - Tests had drifted from current contracts

Fixed stale test contracts that were hiding the real suite status:
- `tests/test_bl076_junk_filter_and_symbol_name.py` now supplies required
  `Settings` secrets via a local helper.
- `tests/test_calibration_dryrun_scheduler.py` fake Telegram sender accepts
  `**kwargs` so `parse_mode=None` plumbing stays testable.
- `tests/test_heartbeat_mcap_missing.py` aioresponses URLs now tolerate query
  parameters.
- `tests/test_bl064_channel_reload.py` now asserts interval-zero heartbeat
  stays alive until cancellation instead of timing out.
- `tests/test_signal_params_auto_suspend.py` audit queries now filter the
  intended `field_name='enabled'` row after joint audit writes.
- `tests/test_scorer.py` now asserts exact normalized point values for
  CoinGecko momentum, volume acceleration, and trending-rank signals.
- `tests/test_config.py` now asserts environment overrides for the CoinGecko
  scoring knobs.

## Deferred finding

### P1 - Live order rows can remain open after ambiguous submit/fill failures

Evidence: `record_pending_order()` writes `live_trades.status='open'` before
venue submit. If the signed POST fails after that point, or if fill
confirmation returns a terminal non-filled status that is not handled by the
engine, the row can remain open and distort exposure accounting.

Why not fixed here: the safe fix is not a one-line status update. Ambiguous
network failures after submit may mean Binance accepted the order but the local
process missed the response. Correct repair should query by `origClientOrderId`
or reuse reconciliation before marking a row terminal. A naive "mark rejected on
exception" could create false negatives for real live orders.

Recommended follow-up: design a small live-order terminal-state repair using
existing idempotency/reconciliation primitives:
- After post-pending exceptions, reconcile by `client_order_id`.
- Mark unresolvable ambiguous states as `needs_manual_review`, not silently
  `open`.
- Handle non-filled confirmations (`rejected`, `timeout`, `partial`) with
  explicit status/reason transitions.
- Add freshness/watchdog coverage for stale `live_trades.status='open'` rows.

## Verification

Baseline before fixes:
- `C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest --tb=short -q`
- Result: 16 failed, 2049 passed, 39 skipped.
- Fresh `uv` environment setup in the isolated worktree was blocked by local
  PyPI certificate validation while fetching `hatchling`, so verification used
  the existing project venv.

Targeted after fixes:
- Initial fixed-path target: 33 passed.
- Post-review targeted follow-up: 13 passed.

Full suite after fixes:
- `C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest --tb=short -q`
- Result: 2072 passed, 39 skipped, 8 warnings in 342.65s.

Dry-run note:
- `python -m scout.main --dry-run --cycles 1` with dummy required env and a
  temporary DB exited 0. DNS failures prevented external candidates from
  appearing in the log, so this is a boot/schema/no-crash smoke, not
  live-network candidate evidence.

Warnings are pre-existing/no-fail suite hygiene: unknown `slow` mark, a few
unawaited `AsyncMock` warnings in existing tests, and one aiosqlite thread
warning in a schema-negative test.
