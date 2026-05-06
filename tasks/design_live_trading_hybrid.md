**New primitives introduced:** `tasks/design_live_trading_hybrid.md` itself (this document — the architecture-decision-of-record for live execution). No new schema or code primitives in this design — all referenced infra components (`signal_params.chain` field, `OverrideStore`, `ExchangeAdapter` ABC, `LIVE_MODE` Settings) already exist in tree. Design adds: a routing-key contract (chain → venue stack), a 2×2 integration matrix (CLI shell-out × queue+Hermes / operator-in-loop × autonomous), pre-registered approval-removal criteria (≥30 trades + per-(signal × venue) slippage tolerance + reconciliation-clean + Minara-uptime), milestone-2 trigger gates, two new Tier-1 infra components (`cross_venue_exposure_view` SQL view + Minara liveness probe), one Tier-2 gap (`wallet_snapshots` table), one Phase-3 integration-design conditional on Minara verification outcome.

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
                     (paper_trades + live_orders + live_fills)
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

Cross-listing override path (the BILL case):
- `OverrideStore` (`scout/live/resolver.py`) reads `venue_overrides` table.
- Operator can stamp `(symbol="BILL", primary_venue="solana_dex", primary_pair="BILL/USDC@raydium")` for any token where the chain-field default is wrong.
- Override fires BEFORE chain-field routing — overrides win.
- This is the existing infrastructure; **no new code**, just operator-side SQL inserts.

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

## Pre-registered approval-removal criteria (Phase 1 → Phase 2)

Pre-registered HERE in this document. Tolerances NOT adjustable post-Phase-0-start.

> **Operator gate is removed from a (signal_type × venue) pair when ALL of the following hold:**
>
> 1. **Trade-count gate:** ≥ 30 consecutive live trades on this (signal_type × venue) pair approved without operator override or correction.
> 2. **Slippage-fit gate:** Across the most recent 30 live trades for this (signal_type × venue) pair, **≥ 80% of fills land within ±20% of the empirical model's predicted fill price.** Tolerance is 20% absolute and not adjustable post-Phase-0-start.
> 3. **Reconciliation-clean gate:** No unresolved reconciliation discrepancies (paper_trades / live_orders state mismatches) for this (signal_type × venue) pair in the past 14 days.
> 4. **Minara-uptime gate (DEX side only):** Minara liveness probe stayed green ≥ 99% over the past 14 days (DEX-routed signals only — N/A for BL-055/Binance).

**Note on (2):** the per-(signal_type × venue) breakdown is load-bearing. A global aggregate ±20% can hide a single signal_type with systematic bias on one venue — removing the operator gate would mask a route-specific issue. Each pair clears independently.

**Note on pre-registration discipline:** this rule is a `tasks/findings_*.md` document, not a `combo_performance` style runtime knob. It's pre-registered as text BEFORE Phase 0 starts. Otherwise the "is it time" decision becomes subjective — which is the SQL-UPDATE-phantom failure mode the prod-state-check feedback memory exists to prevent.

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
> 2. Approval-removal criteria (i)–(iv) above have been met for ≥ 1 (signal_type × `binance` venue) pair (i.e., at least one signal is autonomous on CEX).
> 3. Net live PnL on milestone 1 is within ±30% of the paper-projected PnL across the same period (sanity check that paper-trade modeling roughly matches live).
> 4. Minara verification has been re-confirmed if any major Minara version has shipped since 2026-05-06 (catch breaking-API drift before the dependency lands in prod).
> 5. No unresolved reconciliation issues from milestone 1 in the past 14 days.

**This is what "discrete second milestone" means** — a gate, not a calendar marker. The middle state (some signals live on CEX, others still paper on DEX) is intentional; paper has been running alongside live in shadow mode by design throughout BL-055 anyway.

## Tier-1 infra additions (must exist before milestone 1 goes live)

### 1. `cross_venue_exposure_view` (SQL view, gecko-side)

Aggregates exposure across CEX + DEX into a single read. Initial Phase 0/1 scope:

```sql
CREATE VIEW cross_venue_exposure AS
  SELECT
    'binance' AS venue,
    COALESCE(SUM(size_usd), 0) AS open_exposure_usd,
    COUNT(*) AS open_count
  FROM live_orders
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

Failure mode: signal misses (skip the trade with `live_orders_skipped_minara_down` counter increment). **NO fallback.** The advisor's framing: fallback is the trap option (silent execution somewhere unintended is worse than no execution).

Heartbeat surfaces 24h skip count + last-success timestamp.

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
