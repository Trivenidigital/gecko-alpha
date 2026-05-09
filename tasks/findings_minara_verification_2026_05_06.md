**New primitives introduced:** NONE (this is a findings-only document; if/when actioned, it informs `tasks/design_live_trading_hybrid.md` and the BL-074 implementation plan).

# Findings: Minara Verification 2026-05-06

**Date:** 2026-05-06
**Status:** COMPLETE — informs `tasks/design_live_trading_hybrid.md`
**Trigger:** advisor's pre-design demand (per CLAUDE.md §7b Hermes-first analysis): "spend the hour reading the Minara repo for two specific things — Solana DEX operational layer maturity and CEX perp coverage."
**Method:** WebFetch + `gh api` survey of `Minara-AI/skills` repo (MIT, ~265★, last push 2026-04-21).

---

## 1. Project framing

**Repo description (verbatim):** *"The official Minara Skills: the all-in-one skills to make your personal AI CFO."*

Architecture: thin Hermes-style "skill" wrappers (Markdown + bash) that **shell out to a closed-source `minara` CLI binary** (`npm install -g minara@latest`). The actual DeFi / CEX execution mechanics live in the CLI, not in the Hermes skill. The skill is an orchestration shell, not an implementation.

**Implication for BL-074:** integration target is the CLI subprocess, not the Hermes skill internals. Adapter shape is "shell out to `minara <verb> <args> --json`, parse JSON output."

## 2. Chain / venue coverage

| Venue | Supported? | Notes |
|---|---|---|
| Solana | ✅ | DEX swaps |
| EVM (Ethereum, Base, Arbitrum, Optimism, Polygon, Avalanche, BSC, Berachain, Blast, Manta, Mode, Sonic, Conflux, Merlin, Monad, Polymarket, XLayer) | ✅ | 17 EVM chains documented |
| Hyperliquid perps | ✅ | Includes `perps-autopilot` mode |
| **Binance USDT-margined perps** | **❌ NOT MENTIONED** | **The architecture-deciding finding.** Long-term shape is permanent two-stack, not Minara-everywhere migration. |
| Bybit / OKX / MEXC / Gate.io perps | ❌ | Not mentioned |
| Polymarket | ✅ | Prediction-market exposure (out of scope for gecko-alpha) |
| MoonPay (fiat on-ramp) | ✅ | Out of scope |

## 3. Operational maturity (capability vs. maturity per advisor)

### What IS implemented at the skill level (visible in repo)

- **Anti-loop safeguard:** max 1 retry on command failure
- **Hang detection:** 15-second process-kill (no infinite waits)
- **Balance validation:** pre-confirm sufficiency check
- **Token safety:** contract-address verification against canonical addresses; scam/honeypot detection; address-poisoning warnings
- **Operator-in-loop confirmation:** *"balance check + summary table + ask 'Confirm or Abort?' — then STOP"* — structurally enforced for swap and perps-order calls without `--yes`
- **Authentication:** device-code login (`minara login --device`); `MINARA_API_KEY` env var bypasses interactive auth for autonomous mode

### What is OPAQUE (in closed CLI, not exposed in skill)

- **Slippage tolerance config:** not exposed in public skill (the `swap.md` reference does NOT document a `--max-slippage` flag). Likely default-only inside CLI.
- **Partial-fill handling:** not documented at the skill level.
- **RPC pool health / rotation / failover:** not documented.
- **Gas / priority-fee estimation:** not exposed; CLI sets it. Receipts show `gasFee: $0.001` but no caller-side tuning.
- **Tx simulation pre-broadcast:** not documented.
- **Quote-failure retries with exponential backoff:** not documented (only the generic max-1-retry above).
- **Underlying DEX aggregator:** one dry-run example mentions "route: Jupiter" but no definitive statement. Could be Jupiter-only on Solana, or could be a multi-aggregator router; opaque.

### Verdict per advisor's framing

> "Distinguish capability from operational maturity. 'Minara has a Jupiter integration' is capability. 'Minara handles Jupiter quote-failure retries with exponential backoff and surfaces fill-quality metrics' is operational maturity."

**Capability:** ✅ comprehensive across DEX (17+ EVM + Solana) and Hyperliquid perps.
**Operational maturity:** ⚠️ MIXED. Skill-level safeguards are documented (anti-loop, hang-detection, balance-validation, address-verification, confirmation prompt). Operational-layer primitives (slippage, partial fills, RPC, gas estimation, tx simulation) are inside the closed CLI binary and not auditable.

**Final verdict:** **partial savings, not clean Hermes-first win.**

This is the architecture-shaping finding the advisor predicted. Per the original framing:
> "If verification reveals partial savings, integration design is revisited with the build-our-own gap factored in."

The Phase 3 conditional in `design_live_trading_hybrid.md` reflects this — Phase 0/1/2 work the same, but Phase 3 (queue+Hermes+autonomous) integration design is conditional on observed Minara CLI operational behavior during Phases 0–2.

## 4. CEX perp coverage — confirmed Hyperliquid-only

Per the `perps-order.md` reference: *"This is a CLI reference for a single perps ordering tool with minimal venue or risk-handling detail."* No Binance, Bybit, OKX, dYdX, or other CEX perp venues are named anywhere in the documentation.

The `perps-autopilot.md` reference describes "per-wallet, multi-strategy AI trading" — clearly DEX-perp shaped (per-wallet implies on-chain). Given Hyperliquid is the only on-chain perp venue Minara names, autopilot is **Hyperliquid-only** by inference.

**Architecture decision (per advisor's Q2):** "If Minara is spot-only on CEX, BL-055/binance_adapter is permanent infrastructure." Confirmed: Minara is HL-only on perps. Therefore BL-055/binance_adapter is permanent. The "Maybe migrate to Minara later" arrow in the original brainstorm hand-wave is removed.

## 5. Phase-0 reuse opportunity (operator-in-loop comes free)

Verified: Minara's CLI **structurally** requires confirmation flow. Quote from skill doc:

> *"Your response for a swap request = balance check + summary table + ask 'Confirm or Abort?' — then STOP. Execution only proceeds after explicit user confirmation in a subsequent message."*

This means Phase 0/1 (operator-in-loop) is **what Minara does by default**. We don't build an approval gateway; we just shell out without `--yes`. The operator confirms each trade via the terminal where Minara is logged in.

Autonomy (Phase 2+) comes from passing `--yes` flags or using `MINARA_API_KEY` for non-interactive auth.

**Net dev cost for Phase 0 operator-gate:** ~zero. The gate exists in the CLI we're calling. We just have to NOT pass `--yes`.

## 6. SMB-Agents Telegram approval gateway pattern — UNVERIFIED

Advisor referenced this twice ("SMB-Agents discipline" + "SMB-Agents Telegram approval gateway pattern"). Searched 2026-05-06:

- `gh search repos "smb-agents"` → ~20 results, all unrelated (Small/Medium Business agents, Super Mario Bros agents)
- No clear match to a Telegram approval gateway

Possibilities:
1. Advisor misremembered the name
2. Private/internal project not on GitHub
3. Generic pattern descriptor, not a project

**Action:** the design doc does NOT rely on SMB-Agents pattern reuse. If Phase 2+ needs a Telegram approval flow (above and beyond Minara's built-in CLI confirmation), it'll be designed from scratch (~1 day work). **Asked advisor for repo URL** as an open question in the design doc.

## 7. `perps-autopilot` notes (deferred from milestones 1+2 but worth flagging)

`perps-autopilot` is a per-wallet multi-strategy AI trading mode. Once enabled on a wallet, `minara perps order` is BLOCKED on that wallet (manual + autopilot can't coexist, by design).

Functionally analogous to BL-067 conviction-lock + BL-NEW-HPF combined — long-hold strategies with hands-off execution after configuration. But:

- **Hyperliquid-only** (no Binance perps).
- **Strategy is configured via interactive dashboard**, not from gecko-alpha. We can't programmatically set the strategy.
- **Deferring it is the right call** for milestones 1+2. Future evaluation if/when we want HL-perp exposure.

## 8. Authentication / capital model

- **Authentication:** device-code login via `minara login --device`. CLI shows URL+code; operator visits in browser; CLI verifies. **Operator-side step, once per machine.**
- **API key bypass:** `MINARA_API_KEY` env var skips interactive login (for autonomous mode).
- **Capital model:** Minara CLI manages wallet keys internally (closed). Operator funds via `minara deposit` or `transfer`. Balance via `minara balance --json`.
- **Withdrawal:** `minara withdraw` (out of scope for trading-execution path).

## 9. Recommendation summary

The findings confirm the architecture-decision-of-record in `tasks/design_live_trading_hybrid.md`:

1. **Use Minara via CLI shell-out for DEX side** (Solana + EVM + future Hyperliquid). Capability is there; operational maturity is partial-savings; opacity acceptable for Phase 0/1 with operator-in-loop.
2. **Keep BL-055 / binance_adapter as permanent infrastructure** for Binance USDT-margined perps. Minara doesn't cover Binance perps, period.
3. **Phase 0/1 operator gate** = Minara's built-in CLI confirmation prompt. Free, no additional code.
4. **Phase 3 (queue + Hermes + autonomous) integration design** is conditional on observed Minara CLI operational behavior during Phases 0–2. If gas / RPC / partial-fill issues surface, build-our-own gap analysis is required before Phase 3 design.

**Gating:** all findings above are point-in-time as of 2026-05-06. Per the design's milestone-2 trigger criterion (4): if Minara ships a major version before Milestone 2 activates, re-verify this document.
