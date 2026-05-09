**New primitives introduced:** Three new `Settings` fields — `LIVE_TRADING_ENABLED: bool = False` (master kill switch), `LIVE_MAX_TRADE_NOTIONAL_USD: float = 100.0` (per-trade cap), `LIVE_MAX_OPEN_EXPOSURE_USD: float = 1000.0` (aggregate cap). One signature change — `VenueResolver.resolve()` gains `chain: str` parameter; returned `ResolvedVenue.venue` becomes `f"minara_{chain}"` for DEX rows (was hardcoded `"binance"`). New SQL view `cross_venue_exposure` aggregating `live_trades` + chain-native `paper_trades`. Updated `gates.py` Gate 7 to query the view. New module `scout/live/minara_health.py` with `is_minara_alive()` + circuit-breaker state. Idempotency contract: Binance `client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"` for retry-safety; Minara-side `intent_uuid + minara_invocation_attempts_count` persisted to `live_trades` (no auto-retry on Minara timeout — gated on operator-in-loop). Pre-registered approval-removal criteria (6 gates, including new idempotency-clean). Pre-registered Phase 3 design-trigger criteria. New `live_trades_skipped_*` counter family (`master_kill`, `mode_paper`, `signal_disabled`, `kill_switch`, `exposure_cap`, `minara_down`, `dual_signal_aggregate`). Phase-2-only `live_position_aggregator` check (deferred from M1). Operator-side `venue_overrides` schema simplified to `(symbol, primary_chain)` only — no venue-internal pair format leakage. Tier-2 gap: `wallet_snapshots` table for true total-exposure (Phase 2+).

# Live Trading — Hybrid Execution Architecture Design

**Date:** 2026-05-06
**Status:** DESIGN — pending operator review
**Companion:** `tasks/findings_minara_verification_2026_05_06.md` (verification findings)
**Decision-of-record for:** BL-055 (CEX live) + BL-074 (DEX live via Minara) + their hybrid orchestration

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Multi-chain DEX execution + wallet custody | **Yes — Minara (`Minara-AI/skills`, MIT, ~265★)** | **Use Minara via CLI shell-out**. Verified 2026-05-06 — supports Solana + 17 EVM chains + Hyperliquid perps. |
| Binance USDT-margined perp execution | **No** — Minara does NOT support Binance perps | Keep BL-055 / `scout/live/binance_adapter.py`. **Permanent infrastructure**, not wind-down code. |
| Operator-in-loop confirmation gate for live trades | **Yes — Minara's built-in CLI confirmation flow** ("balance + summary + Confirm/Abort then STOP") | Phase 0/1 = call Minara without `--yes`. Operator gate comes free. |
| Multi-strategy autopilot for Hyperliquid perps | **Yes — Minara `perps-autopilot`** | Out of scope for milestones 1 + 2. Future evaluation if we go HL-perps. |
| Pre-registered approval-removal criteria | None found — operational discipline pattern | Build from scratch (lives in this design + audit telemetry). |
| Cross-venue exposure tracking | None found — gecko-side orchestration | Build from scratch (Tier-1 SQL view; Tier-2 wallet-snapshots table). |
| Multi-CEX adapters (Bybit/OKX/MEXC) | None — Minara doesn't cover; we'd build per-venue | **Out of scope.** Single-CEX (Binance) is sufficient for milestone 1. |

**Awesome-hermes-agent ecosystem check:** ecosystem covers DSPy/GEPA, Hermes infra monitoring, blockchain oracles (Chainlink/Solana — different problem), and Minara execution. Minara is the relevant entry; no other DEX/CEX execution layer applies.

**Drift-check (per global CLAUDE.md §7a):**
- `scout/live/` already shipped: `LIVE_MODE` config, `ExchangeAdapter` ABC, `binance_adapter.py`, `VenueResolver`, `OverrideStore`, `gates.py`, `kill_switch.py`, `reconciliation.py`, `shadow_evaluator.py`. ~1500 LOC committed.
- BL-074 (Minara integration) explicitly captured-not-actionable per `~/.claude/projects/C--projects-gecko-alpha/memory/project_bl074_minara_vision_2026_05_03.md`. Adapter shape sketched 3 ways, none decided. **This design picks the shape and unlocks BL-074.**
- `paper_trades.chain` column already encodes the routing key (`coingecko` for CEX-sourced, `solana`/`base`/`ethereum`/etc. for chain-native). No new routing primitive needed.

**Verdict:** building from scratch is wrong; using Minara verbatim is wrong. The right shape is BL-055 (CEX) + Minara CLI (DEX) + gecko-side orchestration over both. All three layers' homes already exist.

---

## Goal

Resolve the open architectural question raised in this session: when a signal fires on token BILL (available across multiple exchanges), how does live execution route the trade?

Answer: **Hybrid — chain-field routing**. Signals stamped `chain="coingecko"` route to BL-055/Binance. Signals stamped `chain="solana"`/`"base"`/etc. route to Minara CLI shell-out. Cross-listed tokens like BILL are handled via the existing `OverrideStore` (per-symbol primary-venue override).

## Verified architecture (post-Minara verification 2026-05-06)

```
                     gecko-alpha (signal + strategy + exit logic)
                                     │
                  ┌──────────────────┴──────────────────┐
                  │                                     │
        chain="coingecko" signals          chain="solana"/"base"/etc. signals
        (gainers_early, losers_,           (chain_completed, tg_social,
         narrative_, volume_spike)          BL-076 chain dispatches)
                  │                                     │
                  ▼                                     ▼
        scout/live/                              Minara CLI shell-out
        binance_adapter.py                       `minara swap ...`
        (BL-055 — owned by us,                   `minara perps order ...`
         permanent infrastructure)              (BL-074 — closed CLI we
                  │                              shell-out to; ops layer
                  │                              opaque)
                  │                                     │
                  └──────────────────┬──────────────────┘
                                     │
                                     ▼
                     gecko-side reconciliation
                     (paper_trades + live_trades + live_fills)
                     ↓
                     Cross-venue exposure SQL view (NEW)
                     ↓
                     Heartbeat + Telegram alerts
```

**Key contracts:**
- gecko-side **owns**: signal generation, strategy logic (BL-067 conviction-lock, BL-NEW-HPF, BL-NEW-AUTOSUSPEND-FIX, peak/trail/leg cascade), exit timing, reconciliation, observability, kill-switches at the policy layer.
- BL-055/binance_adapter **owns**: Binance USDT-margined perp + spot order placement, depth fetching, balance, order-state polling.
- Minara CLI **owns**: multi-chain wallet custody, DEX routing (Jupiter/etc. underneath), Hyperliquid perp placement (if used), gas estimation, slippage handling, RPC pool — **all opaque inside the closed CLI binary**.
- gecko-alpha never sees Minara's internals; it shell-outs and parses `--json` output.

## Routing key (Q2 decision)

`paper_trades.chain` is the primary routing key:
- `chain == "coingecko"` → BL-055 / Binance
- `chain in {"solana","base","ethereum","arbitrum",...}` → Minara CLI

Cross-listing override path (the BILL case) — **decoupled per architectural-reviewer MUST-FIX**:
- `OverrideStore` reads `venue_overrides` table — schema simplified from the original draft.
- Operator stamps `(symbol="BILL", primary_chain="solana")` — just `(symbol, chain)`. **No `primary_pair="BILL/USDC@raydium"` venue-internal hint.** The original draft over-coupled gecko-alpha to Minara's DEX routing decisions; that mapping belongs in the Minara adapter, not the gecko-side override table.
- For DEX routes, Minara CLI receives `--symbol BILL --chain solana --json` and resolves the actual pair (BILL/USDC vs BILL/SOL) + DEX (Raydium vs Orca) internally.
- For CEX routes, BL-055's `binance_adapter` resolves the pair (`BILL` symbol → `BILLUSDT` perp) via existing `fetch_exchange_info_row` logic.

**Override semantics:** override fires BEFORE chain-field routing. If chain="coingecko" but override says primary_chain="solana", the trade goes DEX (Minara). The override expresses "ignore this signal's chain field; use my primary_chain instead."

### VenueResolver chain-aware extension (per architectural-reviewer MUST-FIX)

The existing `VenueResolver` in `scout/live/resolver.py` hardcodes `venue="binance"` on every `ResolvedVenue` it returns. This is correct for the BL-055 single-venue era but breaks the hybrid: DEX-routed trades would silently produce `ResolvedVenue.venue="binance"` rows, polluting the cross_venue_exposure_view that groups by venue.

**Fix:** extend `VenueResolver.resolve()` signature to accept `chain: str` (already in paper_trades), and return:
- `chain == "coingecko"` → `ResolvedVenue.venue="binance", pair=<resolved Binance USDT pair>`
- `chain in {"solana","base","ethereum",...}` → `ResolvedVenue.venue=f"minara_{chain}", pair=<symbol>` (Minara handles the rest internally)

**Implementation note:** this is a single-method signature change + branch on `chain`. The `binance_adapter` is unchanged. The `MinaraAdapter` (new — built in BL-074 implementation) takes `(symbol, chain)` and shells out to `minara swap --symbol --chain --json`. The gecko-side resolver does NOT have to know about Jupiter/Raydium/etc.

### Dual-signal handling (per architectural-reviewer RECOMMEND)

Concrete edge case: BILL fires `gainers_early` (chain="coingecko", routes to override → solana DEX). 5 minutes later BILL fires `chain_completed` (chain="solana", routes natively to Minara). Two paper_trade rows on different signal_types both want to dispatch to Minara. Without aggregation, gecko-alpha opens TWO positions on the same underlying token.

**Phase 0/1 policy:** allowed — operator sees both confirmation prompts and decides whether to approve the second. Reasonable signal-conviction stacking pattern.

**Phase 2+ policy:** introduce a `live_position_aggregator` check at engine entry: if a live_trades row already exists for `(symbol, venue)` with status='open' AND the new intent's signal_type is different, refuse with `live_trades_skipped_dual_signal_aggregate`. Operator can override per-trade by passing `--allow-stack`.

**Note:** this is documented here as a Phase-2 design requirement. The Phase-0/1 implementation plan does NOT need this gate. It's a follow-up before autonomy.

## Integration shape — 2×2 (corrected per advisor)

The two axes are **independent**: integration mechanism × approval surface.

| Integration ↓ / Approval → | Operator-in-loop (default) | Autonomous |
|---|---|---|
| **CLI shell-out** (subprocess `minara swap ...` or BL-055 sync call) | **Phase 0/1** — Minara's built-in confirmation prompt fires; operator approves each trade in the terminal where Minara is logged in. BL-055 calls run synchronously and gate via `LIVE_MODE=shadow` → `live`. | **Phase 2 (interim)** — gecko spawns subprocess with `--yes` flag (Minara) or LIVE_MODE=live (BL-055). Approval-removal criteria pre-registered (below). |
| **Queue + Hermes process** (gecko writes `live_order_intents` rows; long-running Hermes process polls + executes) | Possible but unusual (queue-with-operator-gate-at-consume) | **Phase 3 (endpoint)** — fully decoupled. **Conditional on Minara verification outcome (see Phase 3 caveat below).** |

**Phase progression:** 0 → 1 → 2 → 3, but the **integration mechanism** and the **approval surface** advance INDEPENDENTLY:

- Phase 0 = CLI shell-out + operator-in-loop (Minara's built-in default; BL-055 in `shadow` mode then `live` with manual flag-flip per trade).
- Phase 1 = same integration, harden + accumulate evidence toward removal criteria.
- Phase 2 = CLI shell-out + autonomous (criteria met; operator gate removed; integration mechanism unchanged).
- Phase 3 = Queue+Hermes + autonomous. Migration of integration mechanism happens AFTER autonomy is proven, not bundled with it.

**Phase 3 conditional (per advisor):** Phase 3's integration design is conditional on Minara verification outcome. Verification 2026-05-06 confirmed Minara is **partial savings, not clean fit** (closed CLI; opaque slippage / RPC / gas / partial-fill handling). If, during Phase 1/2, we discover the CLI's operational layer is insufficient (e.g., gas-spike fills outside expected slippage), Phase 3 design is revisited with a build-our-own gap analysis. Phase 0/1/2 are unaffected — they work the same regardless.

## Layered kill switches (defense-in-depth)

Live execution is gated by **four independent layers**. ALL must allow execution; ANY one set to off short-circuits live trading. Order is from outermost (master) to innermost (per-trade).

### Layer 1 — Master kill switch: `LIVE_TRADING_ENABLED`

```python
# scout/config.py
LIVE_TRADING_ENABLED: bool = False  # MASTER kill — operator-controlled via .env
```

**Semantics:** when `False`, `scout/live/engine.py` short-circuits at the entry point: emits `live_execution_skipped_master_kill` log event, increments `live_trades_skipped_master_kill` counter, leaves the paper_trade row untouched. When `True`, execution proceeds to layer 2. Default `False`. Operator-controlled by editing `/root/gecko-alpha/.env` and restarting the pipeline.

**Why this exists in addition to `LIVE_MODE`:** `LIVE_MODE` is a graduation-state knob (paper → shadow → live). `LIVE_TRADING_ENABLED` is a hard kill — orthogonal to LIVE_MODE. Even with `LIVE_MODE="live"` AND per-signal opt-in AND no kill_switch active, if `LIVE_TRADING_ENABLED=False`, nothing fires. This means the operator can flip live trading off in seconds via an .env edit + restart, regardless of the rest of the config state. Mirrors the BL-NEW-AUTOSUSPEND-FIX `SIGNAL_PARAMS_ENABLED` flag's role for auto-suspend, and the `PAPER_HIGH_PEAK_FADE_ENABLED` flag's role for HPF.

**Telegram notification:** when the engine starts up, if `LIVE_TRADING_ENABLED=True`, send a Telegram notification "🔴 LIVE TRADING ENABLED — pipeline started with master kill switch ON." So the operator can never forget the state.

### Layer 2 — Mode state: `LIVE_MODE`

Existing config: `LIVE_MODE: Literal["paper", "shadow", "live"] = "paper"`. Determines whether trades dispatch as paper-only (`paper`), shadow-evaluated only (`shadow`), or live-executed (`live`). Controlled per-rollout.

### Layer 3 — Per-signal opt-in

Existing infrastructure: `signal_params.live_eligible` column (or equivalent — verify against current scout/live/engine.py). Per-signal_type opt-in to live execution. Default off. Mirrors `high_peak_fade_enabled` per-signal opt-in pattern.

### Layer 4 — Runtime kill_switch

Existing: `scout/live/kill_switch.py`. Triggered programmatically by alarm conditions (consecutive losses, exposure breach, reconciliation drift). Halts execution venue-by-venue with explicit operator-acknowledgment to clear.

### Visual

```
.env LIVE_TRADING_ENABLED=True ──┐
                                  ├── (AND) ── live execution proceeds ──→
LIVE_MODE in {"shadow","live"} ──┤
signal_params.live_eligible=1 ──┤
kill_switch_active=False ────────┘
```

ANY one False → `live_trades_skipped_<reason>` counter increments + log event + paper_trade unaffected.

## Pre-registered approval-removal criteria (Phase 1 → Phase 2)

Pre-registered HERE in this document. Tolerances NOT adjustable post-Phase-0-start.

> **Operator gate is removed from a (signal_type × venue) pair when ALL of the following hold:**
>
> 1. **Trade-count gate:** ≥ 30 live trades on this (signal_type × venue) pair WITHOUT a "correction" (defined below). The trades need not be strictly consecutive — a correction RESETS the running counter to 0.
>
> 2. **Duration floor (per risk-reviewer MUST-FIX):** ≥ 14 calendar days have elapsed since Phase 0 activated for this (signal_type × venue) pair. 14 days covers two full weekly cycles + at least one weekend liquidity regime + a possible Binance maintenance window. This pairs with the 30-trade count above; per the existing data, gainers_early can hit 30 trades in 2-3 days, which is too short to observe regime variance. BOTH gates must pass independently.
>
> 3. **Slippage-fit gate (per risk-reviewer MUST-FIX, expressed in basis points):** Across the most recent 30 live trades for this (signal_type × venue) pair, ≥ 80% of fills land within the per-venue tolerance band:
>    - **Binance perp / spot:** within **±100 bps** (1.0%) of orderbook mid-price at order-submit time
>    - **Minara DEX (Solana, EVM):** within **±600 bps** (6.0%) of quoted price at order-submit time
>
>    Per-venue thresholds reflect the actual liquidity-class differences (Binance perps have deep books; DEX micro-cap tokens routinely run 200-500 bps actual slippage). Thresholds are absolute and NOT adjustable post-Phase-0-start. The per-(signal_type × venue) breakdown is load-bearing — a global aggregate could hide route-specific bias.
>
> 4. **Reconciliation-clean gate:** No unresolved reconciliation discrepancies (paper_trades / live_trades state mismatches) for this (signal_type × venue) pair in the past 14 days.
>
> 5. **Idempotency-clean gate (per architectural-reviewer RECOMMEND):** No double-fill incidents in the past 14 days. A double-fill is defined as: gecko-alpha records 2+ live_fills rows for the same `intent_id` (Binance: `client_order_id`; Minara: gecko-side intent UUID). This gate is a Phase 2 blocker because operator-in-loop catches double-confirms; autonomous mode does not.
>
> 6. **Minara-uptime gate (DEX side only):** Minara liveness probe stayed green ≥ 99% over the past 14 days (DEX-routed signals only — N/A for BL-055/Binance).

### Definition of "correction" (per risk-reviewer MUST-FIX)

A trade counts as a CORRECTION (resets the trade-count gate counter) if EITHER:
- **(a)** The operator aborts the Minara/BL-055 confirmation prompt (rejects the trade before execution), OR
- **(b)** The operator manually unwinds (sells/closes) the live fill within 24 hours of execution.

Approving a trade that the operator privately judged as "off but probably OK" does NOT reset the counter — the gate measures observed corrections, not reservations. This rules out the psychological-anchoring failure mode where an operator wanting autonomy reinterprets ambiguously-bad trades as "not a correction."

**Note on pre-registration discipline:** these criteria live in this document and are not adjustable via runtime config. The thresholds (30, 14d, 100bps, 600bps, 99%) are committed numbers, not parameter knobs. Otherwise the "is it time" decision becomes subjective — which is the SQL-UPDATE-phantom failure mode that prod-state-check feedback memory exists to prevent.

### Phase 3 trigger pre-registration (per architectural-reviewer RECOMMEND)

To prevent the same drift problem on Phase 3 (queue+Hermes+autonomous):

> **Phase 3 design begins when EITHER condition holds:**
>
> - **(a) Operational-layer-insufficient trigger:** ≥ 5 unexpected partial-fills OR ≥ 3 gas-spike fills exceeding the slippage-fit gate's per-venue tolerance, observed during Phase 1/2 soak. "Unexpected" = not flagged by Minara CLI confirmation, not anticipated by paper-trade slippage model.
> - **(b) Phase-2-clean trigger:** ≥ 100 autonomous (Phase 2) trades have completed cleanly across at least 2 (signal_type × venue) pairs without an idempotency-clean-gate breach. This represents enough autonomous data to scope queue+Hermes integration without the build-our-own gap analysis.
>
> Whichever fires first determines Phase 3 scope. (a) means "build operational layer ourselves alongside queue"; (b) means "build queue thinly on top of working CLI."

## Serial milestones (D-disciplined per advisor)

**Milestone 1: CEX-live (Binance via BL-055)** — first.

Trigger to activate Phase 0 of milestone 1:
- BL-055 prerequisites met (`balance_gate.py` implemented; 7d clean shadow soak; written live policy; operator go-ahead — per existing BL-055 spec).
- This design doc + writing-plans output reviewed and approved.
- **Implementation plan landed** (next step after this design).

**Milestone 2: DEX-live (Minara via CLI shell-out)** — second.

Pre-registered trigger to activate Milestone 2:

> **Milestone 2 (DEX-live) activates when ALL hold:**
>
> 1. Milestone 1 has been live ≥ 30 days (operator-in-loop OR autonomous).
> 2. Approval-removal criteria (1)–(6) above have been met for ≥ 1 (signal_type × `binance` venue) pair (i.e., at least one signal is autonomous on CEX).
> 3. **Net live PnL on milestone 1 is at least 70% of paper-projected PnL** across the same period (one-sided gate — fail on shortfall only, per risk-reviewer MUST-FIX). Outperformance does NOT block; only a > 30% PnL shortfall vs paper does. Symmetric gates would block on positive surprises (e.g., paper underestimating fills), which is the wrong policy.
> 4. Minara verification has been re-confirmed if any major Minara version has shipped since 2026-05-06 (catch breaking-API drift before the dependency lands in prod).
> 5. No unresolved reconciliation issues from milestone 1 in the past 14 days.

**This is what "discrete second milestone" means** — a gate, not a calendar marker. The middle state (some signals live on CEX, others still paper on DEX) is intentional; paper has been running alongside live in shadow mode by design throughout BL-055 anyway.

## Hard capital caps (per risk-reviewer MUST-FIX, BLOCKING for Phase 0)

The original draft left capital sizing to operator discretion. Risk reviewer correctly flagged: in autonomous Phase 2, a burst of simultaneous signals with no per-trade or aggregate ceiling can deploy unbounded capital. **Pre-registered caps go in `.env` BEFORE Phase 0 activates.** Two new `Settings` primitives:

```python
# scout/config.py — added for BL-NEW-LIVE-HYBRID
LIVE_MAX_TRADE_NOTIONAL_USD: float = 100.0
"""Hard ceiling on the notional USD size of any single live trade.
The engine MUST refuse to execute a single intent above this. Sized to
match the existing paper-trade default ($100). Operator increases per
.env edit + restart only after Phase 1 demonstrates fills behave as
expected."""

LIVE_MAX_OPEN_EXPOSURE_USD: float = 1000.0
"""Hard ceiling on the SUM of open live-position notionals across ALL
venues (Binance + Minara DEX). Engine refuses to open a new position if
post-trade aggregate exposure would exceed this. Sized 10x single-trade
default — allows ~10 concurrent positions at default size. Operator
controls via .env."""
```

**Enforcement point:** Layer 4 of the kill-switch stack — checked AFTER LIVE_TRADING_ENABLED + LIVE_MODE + per-signal opt-in but BEFORE the venue adapter is called. Engine queries `cross_venue_exposure_view` (defined below) for current total open exposure, adds the proposed trade's notional, refuses if over ceiling. Logs `live_trades_skipped_exposure_cap` + Telegram alert on breach (alerts because this means signals are firing faster than the operator's intended deployment rate, which is itself a meaningful alert).

**Why the ceiling lives in `.env` and not `signal_params`:** the operator wants to flip caps in seconds without DB migration. `.env` change + restart takes 30s. SQL UPDATE on signal_params requires more thought (which signal? which value?) and can't be a true "halt all expansion" lever.

## Tier-1 infra additions (must exist before milestone 1 goes live)

### 1. `cross_venue_exposure_view` (SQL view, gecko-side) + Gate 7 update

Aggregates exposure across CEX + DEX into a single read. Initial Phase 0/1 scope:

```sql
CREATE VIEW cross_venue_exposure AS
  SELECT
    'binance' AS venue,
    COALESCE(SUM(size_usd), 0) AS open_exposure_usd,
    COUNT(*) AS open_count
  FROM live_trades
  WHERE status = 'open'
UNION ALL
  SELECT
    'minara_' || COALESCE(chain, 'unknown') AS venue,
    COALESCE(SUM(amount_usd), 0) AS open_exposure_usd,
    COUNT(*) AS open_count
  FROM paper_trades
  WHERE status = 'open' AND chain != 'coingecko'
  GROUP BY chain;
```

**Gate 7 update (per architectural-reviewer MUST-FIX):** the existing exposure-cap gate at `scout/live/gates.py` lines 207-231 queries `shadow_trades` only. **It must be updated to query `cross_venue_exposure_view` (sum the `open_exposure_usd` column) BEFORE milestone 1 goes live.** Without this, exposure cap silently allows DEX positions that breach the cap because the gate is blind to non-Binance venues. The implementation plan must list this as an explicit task; the design-level callout here prevents it being missed during build.

**Documented gap (Phase 1 vs Phase 2+):** The above view shows OPEN POSITIONS. It does NOT show IDLE CAPITAL. Total exposure = open + idle. Idle capital lives in:
- Binance account balance (USDT-margined perp wallet) — not in any gecko-alpha table
- On-chain wallet (Solana SPL tokens, EVM tokens) — not in any gecko-alpha table

For Phase 0/1 the operator manually reconciles via `minara balance --json` + Binance dashboard. **This is intentional Phase-0/1 scope.** The Tier-2 follow-up below addresses Phase 2+.

### 2. Minara liveness probe (`scout/live/minara_health.py` — NEW)

Health-check before any DEX-routed trade commits. Implementation:

```python
async def is_minara_alive(timeout_sec: float = 5.0) -> bool:
    """Run `minara account --json` with a short timeout. Return True if
    exit 0 + parseable JSON; False otherwise. Does NOT cache — every
    DEX-routed trade revalidates. Cheap (single API call to local CLI).
    """
```

Failure mode: signal misses (skip the trade with `live_trades_skipped_minara_down` counter increment). **NO fallback.** The advisor's framing: fallback is the trap option (silent execution somewhere unintended is worse than no execution).

Heartbeat surfaces 24h skip count + last-success timestamp.

**False-positive circuit breaker (per risk-reviewer RECOMMEND):** the liveness probe confirms `minara account` responds. It does NOT confirm `minara swap` execution. A CLI that returns account data instantly but hangs on swap calls is false-positive on the probe. Mitigation:

- Track `live_trades_skipped_minara_down` AND `live_swap_calls_timed_out` counters in a rolling 1-hour window.
- If `live_swap_calls_timed_out` ≥ 3 in any 1h window despite probe-green, **halt all DEX execution** (set a process-local `_minara_circuit_open=True` flag) AND fire Telegram alert: `"⚠ Minara circuit breaker tripped: 3 swap timeouts despite liveness-probe green. DEX execution paused. Operator: investigate or run /minara-circuit-reset."`
- Operator-acknowledgment (Telegram command or .env flag flip) clears the breaker.

This is the design-level guard against the liveness-probe-deceives-us scenario; closes the gap risk reviewer flagged at Scenario C.

### 3. Idempotency on retry (per architectural-reviewer + risk-reviewer RECOMMEND)

**Binance side (BL-055 adapter):** every order submission sets a `client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"` (intent_uuid is gecko-generated, persisted to `live_trades` row at submit-time). Before retrying any Binance order on timeout/transient error, the adapter MUST query open orders + recent fills by `client_order_id` and confirm no existing fill before submitting a second request. If an existing matching `client_order_id` is found, the second submit is suppressed and the adapter waits for the original's terminal state instead.

**Minara side (BL-074 adapter):** Minara CLI doesn't expose an obvious idempotency key in the public skill. Compensating control: gecko-side adapter persists `intent_uuid + minara_invocation_attempts_count` to `live_trades` row; if an attempt ends in subprocess timeout (15s built-in or our 30s outer wrapper), gecko marks the intent `status='timed_out_unknown'` and DOES NOT auto-retry. Operator-in-loop mode (Phase 0/1) handles this by surfacing the row in a Telegram alert; operator manually checks `minara balance --json` to determine whether the swap actually filled and updates the row. Phase 2 (autonomous) is GATED on idempotency-clean criterion #5 above — if double-fills occur, autonomy is removed.

**Why no auto-retry on Minara:** without a CLI-side idempotency key, automated retry risks double-fill. Operator-in-loop is the failsafe.

## Tier-2 gap (Phase 2+, not blocking milestone 1)

### `wallet_snapshots` table

Periodic balance fetch from Minara CLI + Binance API → row per wallet/account per snapshot. Drives true total-exposure reconciliation. Schema sketch:

```sql
CREATE TABLE wallet_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    venue           TEXT NOT NULL,        -- 'binance', 'minara_solana', etc.
    asset           TEXT NOT NULL,        -- 'USDT', 'USDC', 'SOL', ...
    balance         REAL NOT NULL,
    balance_usd     REAL,                 -- NULL for non-USD assets if price lookup fails
    snapshot_at     TEXT NOT NULL
);
```

**Explicitly out of scope for milestone 1.** Documented here so it isn't forgotten when Phase 2 lands.

## Phase 0/1 operator gate — leveraging Minara's built-in confirmation

**Verified during Minara survey 2026-05-06:** Minara's CLI structurally requires confirmation flow: *"balance check + summary table + ask 'Confirm or Abort?' — then STOP."* This means **Phase 0/1 (operator-in-loop) is what Minara does by default** when called without `--yes`. We don't build the approval gateway; we just shell out.

Autonomy (Phase 2+) comes from passing `--yes` flags or using `MINARA_API_KEY` for non-interactive auth.

**SMB-Agents Telegram approval gateway pattern** referenced by advisor was searched 2026-05-06; no matching repo found in `gh search repos "smb-agents"`. Possibilities: misremembered name, private project, or generic pattern descriptor. **Not relied on in this design.** Phase 2+ Telegram-based approval flow (if needed) will be designed from scratch (~1 day work) when the operator decides to keep operator-in-loop AFTER the autonomy criteria fire (e.g., for high-conviction trades).

**Action:** ask advisor for the specific repo URL if SMB-Agents pattern reuse remains a goal.

## Operational maturity caveats (Minara closed CLI)

The CLI is closed-source. Documented in the verification but worth restating:

- **Slippage tolerance config:** not exposed in the public skill. We trust the CLI's defaults. If a fill comes back outside expected slippage, we observe via reconciliation but cannot tune the CLI.
- **Partial-fill handling:** not documented. We treat each `minara` call as atomic — if it returns success, we trust the fill amount in the JSON output.
- **RPC pool health / rotation:** not exposed. We rely on Minara's CLI to handle RPC failures internally.
- **Gas / priority-fee estimation:** not exposed. CLI sets it; we observe via fill receipts.
- **Tx simulation:** not documented in the public skill. Whether the CLI simulates before broadcast is unverifiable from the repo we surveyed.

**Risk class:** if Minara's CLI has a bug in any of the above, we can't fix it in our code — we report and wait for upstream. Mitigations:
1. Phase 0/1 operator-in-loop catches obvious anomalies (operator sees the summary before approving).
2. Reconciliation comparing expected vs actual fill price flags slippage drift over time.
3. Approval-removal criterion (ii) requires fills within ±20% of model — this catches systematic CLI misbehavior.
4. Skip-on-Minara-down via liveness probe catches outages.

## What's explicitly NOT in this scope

- **Multi-CEX support (Bybit, OKX, MEXC, Gate.io)** — Binance is sufficient for milestone 1. Adding more CEX adapters is per-venue duplication of BL-055; defer until/unless a specific signal is consistently profitable on a non-Binance CEX.
- **Smart routing / best-execution router** — operator can override per-symbol via `OverrideStore`. Runtime liquidity-aware routing across venues is rejected (latency cost + complexity).
- **CoinGecko platforms-field auto-routing** — too unreliable; chain-field + override is sufficient.
- **Hyperliquid perps via Minara `perps-autopilot`** — out of scope for milestones 1+2. Future evaluation if/when we want non-Binance perp exposure.
- **Withdrawals / deposits / cross-chain bridging** — Minara CLI supports these, but execution layer assumes funded venues. Operator handles funding manually.
- **Live trading kill-switch automation** — `kill_switch.py` exists in BL-055 for venue-level halt. Cross-venue kill-switch (halt ALL execution everywhere) is a Tier-2 follow-up, not blocking milestone 1.

## Open questions left for the operator

1. **Confirm advisor's SMB-Agents reference.** Repo URL or "skip; build from scratch if Phase 2 needs it"?
2. **Confirm BL-055 prerequisites timeline.** `balance_gate.py` is per memory marked as "missing 2026-05-03." Has it landed? If not, that's the immediate blocker for milestone 1.
3. **Confirm milestone 1 first-signal subset.** Phase 0/1 should NOT enable all signals at once. Recommend starting with one signal (e.g., `narrative_prediction` — workhorse, +$579 net 30d, signal type CALIBRATION-EXCLUDED so already operator-judgment-driven). Operator picks.
4. **Operator funding decision.** Milestone 1 needs Binance USDT margin funded. Milestone 2 (later) needs on-chain wallet funded via Minara `deposit` or `transfer`. Capital allocation between the two — operator policy.

## References

- `~/.claude/projects/C--projects-gecko-alpha/memory/project_bl074_minara_vision_2026_05_03.md` — original Minara vision capture
- `tasks/findings_minara_verification_2026_05_06.md` — verification details
- `scout/live/` — BL-055 existing implementation
- BL-055 spec — already approved 2026-04-22
- BL-074 backlog entry — captured-not-actionable; this design unlocks it
