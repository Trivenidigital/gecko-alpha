**New primitives introduced:** Design companion to `tasks/plan_m1_5c_minara_alert.md`. No new primitives beyond those declared in the plan. Documents architectural rationale, alert-volume + onboarding decisions, failure-isolation invariants, and reversibility.

# M1.5c Design — Minara DEX-Eligibility Alert Extension (Phase 0 Option A)

## 1. Goals

Operator gets a copy-pasteable `minara swap` command in the TG alert body when a paper-trade-open fires for a Solana-listed token. gecko-alpha never executes — the alert is pure decision-support that the operator chooses to act on (or not) by pasting into a Minara CLI logged into their wallet.

**Primary outcomes:**
1. Detection: CoinGecko `/coins/{id}` → `platforms.solana` field check
2. Format: `Run: minara swap --from USDC --to <addr> --amount-usd 10` line in alert body
3. Single-flag disable: `MINARA_ALERT_ENABLED=False` in `.env` → short-circuit, no cost
4. Onboarding: first-deploy announcement covers Minara install + login + funding

**Non-goals:**
- Trade execution (Phase 0 Option A excludes execution by definition)
- Minara CLI on VPS (operator runs locally)
- EVM chains (Solana-first per operator direction; EVM is M1.5d/M2)
- Telegram approval buttons / inline keyboards (M1.5d/M2)
- Slippage / partial-fill / RPC config (closed Minara CLI internals)
- Execution-outcome reconciliation (operator's local Minara handles)

## 2. Architectural choices

### 2.1 Phase 0 Option A vs Option B

**Chosen:** Option A (alert-with-command, no execution).

**Why:** Option B (Minara on VPS + TG approval gateway + subprocess management) is M2-class scope per the original `design_live_trading_hybrid.md` (~2-3 weeks). Option A ships in ~1 week, zero blast radius, validates the DEX-eligibility detection + Minara command shape against 30-50 real-world tokens before committing to Option B's infrastructure. Option A is also reversibility-clean: a single Settings flag disables.

**Tradeoff:** operator must be at a terminal to act on alerts. Phone-to-laptop friction (~30s) is real but acceptable for the 24h+ research-signal horizon; bad for the 10-minute early-pump-detection horizon. Acceptable per "for now" framing.

### 2.2 Detection source: CoinGecko `/coins/{id}` platforms field

**Chosen:** existing `scout.counter.detail.fetch_coin_detail` (30-min in-memory cache, soft-fails to None).

**Why:**
- Already integrated, tested, rate-limited (30 req/min CG free tier)
- 30-min cache fits the use-case: same token rarely re-fires within 6h cooldown, so cache hit rate ~30%
- Returns `platforms: {chain: contract_address}` — single source of truth for on-chain identity
- Empty api_key handled (free tier works)

**Alternative considered (rejected):** DexScreener pool query. Would require chain detection upstream + symbol-to-address matching, more fragile. The CG approach uses coin_id (already populated in paper_trades).

**Tradeoff:** 1 extra CG call per alert (~25/day at observed prod rate). Well under 30/min limit. Cache reduces this further.

### 2.3 Settings-sourced amount, not caller-passed (R2-C1 fold)

**Chosen:** `MINARA_ALERT_AMOUNT_USD: float = 10.0` Settings field, NOT a per-call argument derived from `paper_trade.amount_usd`.

**Why:**
- Prod `PAPER_TRADE_AMOUNT_USD=300`; default $1000. Either way, dramatically higher than M1.5a V3-M3 first-24h discipline of $10.
- An operator pasting a `$300` swap on a 50%-slippage memecoin loses ~$150 per swap. Multiply by 20 alerts/day → $3000/day at-risk by accidental copy-paste.
- Hardcoded $10 default forces explicit operator override (`MINARA_ALERT_AMOUNT_USD=N` in `.env`) for larger sizes — friction matches risk.

**Tradeoff:** alert-suggested size diverges from paper-trade simulated size. The DEX execution is operator-authorized real money; the paper-trade is risk-free simulation. Different posture → different size; correct decoupling.

### 2.4 Helper placement: in dispatch path, after cooldown gate

**Chosen:** `maybe_minara_command` called inside `notify_paper_trade_opened` after the cooldown-claim succeeds, before format runs.

**Why:**
- After cooldown: only successful alerts pay the CG fetch cost. Blocked-cooldown alerts get the cheap-path (no fetch). Aligns spend with operator-visible impact.
- Before format: the command is woven into the alert body via a kwarg, not appended after. Cleaner than mutating the body string post-format.
- Inside the existing outer try/except: the helper's "never raises" contract is belt-and-braces — outer except still demotes 'sent' row to 'dispatch_failed' if helper ever does propagate.

**Alternative considered (rejected):** call helper INSIDE the cooldown lock. Rejected — CG fetch can be 100-500ms; holding the lock that long blocks concurrent dispatches. Lock window should stay short.

### 2.5 Onboarding announcement strategy (R2-C2 fold)

**Chosen:** new sentinel `'m1_5c_announcement_sent'` (separate from M1.5b's `'announcement_sent'`); schema 20260517 migration extends `tg_alert_log.outcome` CHECK enum.

**Why:**
- M1.5b's sentinel is already set on prod (delivered 2026-05-11 00:28Z). Reusing it would either re-fire M1.5b's content OR require complex sentinel-AND logic.
- Separate sentinel = independent idempotency: M1.5c announcement fires exactly once on next deploy, regardless of M1.5b state.
- Body documents: Minara install command, device login, deposit guidance, the default $10 size, the disable flag, the no-execution safety contract.

**Tradeoff:** one more sentinel row + CHECK constraint value. Negligible storage; clean operational model.

### 2.6 Failure isolation

**Three layers of defense:**
1. `MINARA_ALERT_ENABLED=False` → immediate None, no fetch
2. `session is None` → short-circuit before fetch (R1-I1 fold; avoid wasting rate-limiter)
3. fetch_coin_detail outer try/except → None on any exception (CG 404 / 429 / network / parse)
4. format inner try/except → None on any unexpected error (caught + logged + None)

**Plus**: the helper return value is consumed by `notify_paper_trade_opened` via the new `minara_command` kwarg. If `None`, format simply omits the `Run:` line. The TG alert dispatch path is unchanged; no failure mode propagates.

## 3. Alert volume + operator activation experience

**Estimated Solana-listed fraction** of the 4 default-allow signals at observed prod rate (~28/d combined):
- `gainers_early` (12/d): heavily Solana — per `feedback_trading_lessons.md`, ~75% Solana
- `narrative_prediction` (8/d): mixed — ~50% Solana
- `losers_contrarian` (6/d): mixed — ~50% Solana
- `volume_spike` (1.6/d): mixed — ~50% Solana

**Combined estimate:** ~15-18 Run: lines/day (out of ~25 alerts after 6h cooldown). Operator gets ~1 actionable swap suggestion per hour during active periods.

**First-deploy operator activation flow:**
1. Operator pulls master + restarts gecko-pipeline
2. Migration auto-applies (schema 20260517)
3. Next cycle fires `_maybe_announce_tg_alerts` → M1.5b sentinel already set, M1.5c sentinel missing → M1.5c announcement sent
4. Operator reads Telegram: install command, login flow, deposit instructions, default size, kill switch
5. Operator does one-time setup: `npm install -g minara@latest`, `minara login --device`, `minara deposit USDC + SOL`
6. First Solana-eligible signal fires → operator sees `Run:` line → copy-paste → Minara prompts → confirm
7. Trade lands on Solana DEX via Minara's chosen aggregator (Jupiter per verification doc)

## 4. Reversibility

**Fast revert (no code, no deploy):**
```bash
# In .env on VPS
MINARA_ALERT_ENABLED=False
systemctl restart gecko-pipeline
```
Settings read fresh per dispatch — no cache. Next alert fires without `Run:` line.

**Slower revert (git):** `git revert <PR squash>` removes:
- Settings field
- `minara_alert.py` module
- format kwarg + integration call
- M1.5c onboarding announcement function (sentinel row remains; harmless dead data)
- Migration (column add idempotent; CHECK constraint extension reverted via reverse table-rename)

**No execution risk during revert:** since gecko-alpha never executes Minara commands, revert is alert-format-only. Operator's local Minara wallet + commands continue working independently.

## 5. Test strategy

**Unit tests** (`tests/test_minara_alert.py`):
- Solana-listed token → command returned
- Non-Solana token → None
- Empty platforms.solana → None
- fetch_coin_detail returns None (CG outage) → None
- MINARA_ALERT_ENABLED=False → None + no fetch (rate-limiter not consumed)
- Unexpected exception in fetch → None (caught)
- Settings-sourced amount (NOT caller's amount_usd) wins
- Default $10 when no override
- session=None → None + no fetch (R1-I1)
- amount clamp to ≥1 (R1-I2)
- amount_usd=None doesn't crash (R1-I3)

**Integration tests** (`tests/test_tg_alert_dispatch.py`):
- format with minara_command includes `Run:` line BEFORE coingecko link
- format without minara_command unchanged from M1.5b
- end-to-end Solana token → Run: line in dispatched body
- end-to-end EVM-only token → no Run: line

**Onboarding announcement tests** (`tests/test_main_wiring.py` or similar):
- M1.5c announcement fires when M1.5b sentinel exists + M1.5c sentinel absent + MINARA_ALERT_ENABLED
- M1.5c announcement skipped when MINARA_ALERT_ENABLED=False
- Sentinel prevents re-fire on subsequent restarts

**Total: ~15 new test cases.**

## 6. Open questions — resolved

| Q | Resolution |
|---|---|
| Default trade size source | Settings field `MINARA_ALERT_AMOUNT_USD=10.0` (R2-C1) |
| First-deploy operator onboarding | Separate sentinel + announcement (R2-C2) |
| session=None handling | Short-circuit before fetch (R1-I1) |
| Amount rounding edge | `max(1, int(round(...)))` (R1-I2) |
| amount_usd=None | Settings-sourced, not caller-passed (R1-I3) |
| Phone-screen rendering | Accepted — 4-line body, expand to copy |
| Alert volume validation | Runbook-side 7-day soak query (deferred) |
| Heartbeat counter | Deferred to M1.5d / dashboard panel |
| EVM chains | M1.5d/M2 scope (out) |
| Slippage hint | Not exposed by Minara CLI (out) |
| Telegram inline buttons | Phase 0 Option B / M2 (out) |

## 7. Plan-stage reviewer-fold summary

| Finding | Reviewer | Severity | Status |
|---|---|---|---|
| Default amount = $300 paper-trade size → $150-loss-per-swap risk | R2 | C1 | **Folded — MINARA_ALERT_AMOUNT_USD=10.0 default** |
| Operator onboarding gap (no Minara install/login guidance) | R2 | C2 | **Folded — Task 2.5 M1.5c announcement + sentinel + migration** |
| session=None wastes rate-limiter | R1 | I1 | **Folded — short-circuit before fetch** |
| Amount rounding 0.4 → 0 (invalid `--amount-usd 0`) | R1 | I2 | **Folded — `max(1, int(round(...)))` clamp** |
| amount_usd=None TypeError | R1 | I3 | **Folded — Settings-sourced size** |
| Phone-screen Run: below fold | R2 | I1 | Accepted — operator expands |
| Alert volume validation deferred | R2 | I2 | Accepted — runbook-side soak query |
| Copy-paste selection UX | R2 | I3 | Accepted — minor |
| Cache hit test bypassed by monkeypatch | R1 | M1 | Accepted — behavioral test |
| No heartbeat counter | R1 | M2 | Deferred to M1.5d |
| Amount rounding documentation | R2 | M | Inline comment |

## 8. Approval checklist

- [x] Plan-stage 2-reviewer pass complete (folded at `775cb6c`)
- [ ] Design-stage 2-reviewer pass complete (this commit)
- [ ] All folds applied to plan + test coverage verified
- [ ] Build → PR → 3-vector reviewer pass → merge → deploy
