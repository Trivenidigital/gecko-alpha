# Backlog — gecko-alpha

Last updated: 2026-05-09 (autonomous build: BL-NEW-QUOTE-PAIR plan + design + impl + tests; PR pending)

## Active Work: Overnight gecko-alpha repo review

- [x] Isolated worktree created: `C:\Users\srini\.config\superpowers\worktrees\gecko-alpha\codex-overnight-repo-review` on `codex/overnight-repo-review`
- [x] Baseline verification: full suite via main venv -> 16 failed, 2049 passed, 39 skipped; fresh `uv` setup blocked by local PyPI certificate issue
- [x] Static scan: inspect suspicious patterns with `rg` and focused file reads
- [x] Runtime-boundary scan: reviewed external-service, DB, alerting/live-order, and test-contract paths for silent-failure risks
- [x] Drift/Hermes-first gate: fixes reuse in-tree primitives; no new external/Hermes-replaceable primitive introduced
- [x] Create review findings file: `tasks/review_gecko_alpha_overnight_2026_05_14.md`
- [x] Fix high-confidence issues that are scoped enough to repair safely
  - [x] CoinGecko stale raw globals can fresh-stamp old prices
  - [x] `trade_predictions` unexpected resolution DB errors do not fail closed
  - [x] Paper-trade race-lost DML paths leave transactions open
  - [x] Final ladder close computes PnL on original quantity instead of remaining quantity + realized legs
  - [x] Stale tests instantiate bare `Settings()` without required env
  - [x] Live Binance `signal_type` attribution blank in pending rows
  - [x] `signal_params` revive audit hardcodes old `tg_alert_eligible`
  - [x] Reviewer fold: make resolver contract drift fail loud
  - [x] Reviewer fold: make live `OrderRequest.signal_type` mandatory and assert adapter persistence
  - [x] Reviewer fold: add trending stale-cache, scorer point-value, transaction-closure, and config override tests
- [x] Run targeted and full verification after fixes: targeted 13 passed after reviewer folds; full suite 2072 passed, 39 skipped
- [x] Request reviewer pass before PR if fixes land: two reviewers returned, findings folded
- [ ] Create PR if the branch contains code/docs changes worth merging

## BL-NEW-QUOTE-PAIR soak (post-deploy)

- [ ] **D+3 mid-soak verification** — query `candidates` table for fraction satisfying `quote_symbol ∈ stables AND liquidity_usd >= 50K`. Threshold: < 40% to keep current bonus magnitude. Query in `docs/runbook_high_peak_fade.md`-adjacent runbook if needed.
- [ ] **D+7 soak end** — alert volume must not exceed +10% baseline. Revert via `STABLE_PAIRED_BONUS=0` env override if breached.

## Pending verifications (time-gated)

- [x] **2026-05-04 ~01:09Z+ — BL-071 guard verification (24h check).** **PASS (with caveat).** Verified 2026-05-04T15:35Z. `full_conviction` + `narrative_momentum` still `is_active=1` ✓. `volume_breakout` retired 2026-05-04T01:01:48Z via the `chain_pattern_retired` path (hit_rate=1.82%, 1 hit in 55 attempts) — legitimate individual underperformance, NOT a guard failure. The guard only short-circuits on `total_hits_across_all == 0`; with non-zero hits on at least one pattern, individual retirement is allowed (correct behavior). chain_completed paper_trades count: 7 → 10 in 24h (+3 new). Chain dispatch alive. No action needed.
- [x] **2026-05-04 13:58Z — BL-063 moonshot soak ends. DECISION: keep on permanently.** Verified 2026-05-04T15:35Z. Moonshot path: **19 closes / +$2,232.86 net / +$117.52/trade / 100% win**. Regular-trail comparison (peak ≥30, no moonshot armed): 13 closes / +$773.52 net / +$59.50/trade / 100% win. Moonshot delta = +$1,459.34 net — exceeds the +$1,420 sneak-peek prediction by ~3% and ~3× the regular-trail per-trade. Permanent.
- [ ] **2026-05-04 22:24Z — Paper-lifecycle widening soak ends.** Sneak-peek +$1,234 net / 91 closes. Decision: keep on.
- [ ] **2026-05-05 22:58Z — PR #59 strategy tuning soak ends.** Sneak-peek +$1,994 net / 135 closes / 67.4% win / 20% expired. Decision: keep on permanently.
- [ ] **2026-05-10 15:53Z — gainers_early reversal re-soak (7d).** Watch for performance vs the +$190/day sneak-peek that justified reversal. If actuals < +$100/day for 7d, re-evaluate.
- [x] **2026-05-13 02:13Z — losers_contrarian post-BL-NEW-AUTOSUSPEND-FIX revival 7d soak.** **KEEP ON (permanent).** Closed 2026-05-13T04:05Z. n=55, net +$826.68, per_trade +$15.03, win 69.1%. Both gate clauses cleared by ~4×. Zero auto-suspend fires during soak. Drivers: `peak_fade` n=26 +$1,688; `stop_loss` n=11 −$917 drag. Audit row id=23.
- [x] **2026-05-13 02:15Z — gainers_early post-BL-NEW-AUTOSUSPEND-FIX revival 7d soak.** **KEEP ON (permanent).** Closed 2026-05-13T04:05Z. n=128, net +$1,894.37, per_trade +$14.80, win 72.7%. Both gate clauses cleared. Zero auto-suspend fires during soak. `conviction_lock_enabled=1` stays armed. Drivers: `peak_fade` n=38 +$2,499 + `trailing_stop` n=54 +$888; `stop_loss` n=13 −$1,059 drag. Audit row id=24.
- [x] **2026-05-13 02:18Z — HPF dry-run 7d soak (BL-NEW-HPF Phase 1).** **KEEP DRY-RUN. Do NOT flip the flag.** Closed 2026-05-13T04:05Z. n=7 would-fires (6 gainers_early + 1 losers_contrarian). Aggregate counterfactual: HPF +$1,078.15 vs actual +$1,123.63 — **delta −$45.48 (negative)**. Subset reading (structural §9c): HPF beats `moonshot_trail` 3/3 (+$238) but loses to existing `peak_fade` 3/4 (−$285). Re-evaluate at n≥20 scoped to `moonshot_trail`-subset only (filed BL-NEW-HPF-RE-EVALUATION). Audit row id=25.
- [ ] **2026-05-13+ — Deploy PR #82 BL-NEW-MOONSHOT-OPT-OUT (held overnight 2026-05-06).** Migration adds `signal_params.moonshot_enabled INTEGER NOT NULL DEFAULT 1` — no behavior change on deploy (default opt-IN preserves existing floor). Per-signal opt-out via `UPDATE signal_params SET moonshot_enabled=0 WHERE signal_type='X'`. Backtest applicability caveat: `findings_high_peak_giveback.md` PnL projection used floored regime; opted-out signal must re-run backtest with floor removed before projecting impact.
- [ ] **2026-05-13+ — chain_complete fire-rate observation post-PR #80.** Pre-fix: 2 chain_complete events in entire history despite 2,770 anchor candidates (token_id keying bug). Post-fix should produce ≥1 chain_complete per week from full_conviction or narrative_momentum patterns. If yes, can revert `alert_priority` back to "low" in chains/patterns.py (currently temporarily bumped to "medium" for first-fire observability).

## Active soaks (don't disturb)

- [x] **Tier 1a flip — gainers_early kill REVERSED 2026-05-03T15:53Z** — original kill was based on pre-PR-#59 30d data. Sneak-peek of post-#59 data (4.7d window) showed gainers_early at +$508 / 59 closes / +$8.61/trade / 67.8% win — clearly profitable under the new adaptive trail. PR #59 fixed gainers_early; the kill was forfeiting ~$190/day. SQL reversal + restart verified: 5 new gainers_early trades opened at 15:58:29Z, zero `trade_skipped_signal_disabled` events. Tier 1a `SIGNAL_PARAMS_ENABLED=true` flag stays on for the other 7 signals (per-signal params still honored). Audit row in signal_params_audit. Backup: `scout.db.bak.gainers_revive_20260503_155322`.

- [ ] **2026-05-15 14:06Z — RE-SCOPED system health checkpoint (was: "Tier 1a kill 14d soak").** The original A/B (kill gainers_early, see net swing) was invalidated 2026-05-03 when we reversed the kill based on post-PR-#59 data. New scope: 2-week strategic checkpoint after a flurry of changes (Tier 1a flag on, per-signal params live, chain_completed dispatch wired + long-hold tuned, BL-071 guard live). Three concrete questions:
  1. **System P&L re-baseline.** Compute 14d rolling net (2026-05-01 → 2026-05-15) and compare to the −$506 baseline that motivated all the recent changes. Decision gate: ≥ +$1,000 net = strategy stack worked; +$0–$1,000 = mixed; < $0 = something else is bleeding, dig in.
  2. **Tier 1a infrastructure health.** Did Tier 1b auto-suspend fire on anything (shouldn't have, since all signals trended profitable in the 4.7d sneak-peek)? Did anyone run `calibrate.py`? Are signal_params_audit rows clean and traceable? Any latency regression from per-signal lookup vs Settings reads?
  3. **Next-best-next decision.** With 2 weeks of cleaner data and chain_completed actually producing trades, decide what's next: BL-067 (conviction-locked hold), BL-071a/b (outcome plumbing fixes), or "leave the system alone, monitor for another 30d, then revisit". Optionally also: do we re-evaluate BL-070 (entry stack gate) given the data actually shows we're net positive without it?
  - Verify queries (paste into VPS sqlite):
    ```
    -- (1) 14d rolling net since Tier 1a flip
    SELECT COUNT(*), ROUND(SUM(pnl_usd),2), ROUND(AVG(pnl_usd),2),
      ROUND(100.0*SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS win_pct
    FROM paper_trades WHERE status LIKE 'closed_%'
      AND datetime(closed_at) >= datetime('2026-05-01 14:06:00');
    -- (2) per-signal breakdown including chain_completed
    SELECT signal_type, COUNT(*) AS n, ROUND(SUM(pnl_usd),2) AS net,
      ROUND(AVG(pnl_usd),2) AS per_trade,
      ROUND(100.0*SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS win_pct
    FROM paper_trades WHERE status LIKE 'closed_%'
      AND datetime(closed_at) >= datetime('2026-05-01 14:06:00')
    GROUP BY signal_type ORDER BY net DESC;
    -- (3) auto-suspend events (Tier 1b should NOT have fired)
    SELECT * FROM signal_params_audit WHERE applied_by = 'auto_suspend';
    -- (4) all operator/calibration changes since Tier 1a went on
    SELECT * FROM signal_params_audit
    WHERE datetime(applied_at) >= datetime('2026-05-01 14:06:00')
    ORDER BY applied_at;
    ```
  - This is no longer an A/B test — just a 2-week strategic checkpoint. No automatic action; user-driven decision.
- [ ] **PR #58 BL-064 lenient-safety soak** — flag flipped 2026-04-28T15:17Z. Re-check window: 2026-05-12.
  - Decision gate: ≥40% win rate + avg pnl_pct >0 → keep on. As of 2026-04-29T12:25Z: 0 trades dispatched yet (curators haven't posted CA-bearing messages since flag flipped). Operational gap, not code.
- [ ] **PR #59 strategy tuning soak** — deployed 2026-04-28T22:58Z. Re-check window: 2026-05-05.
  - Early signal at 13.5h: 23 closes, +$650 net, ~70% win rate, 0 expired closes. 9× improvement in $/trade vs historical −$3.05. Letting it ride.
- [ ] **BL-063 moonshot soak** — flag flipped 2026-04-27T13:58Z. Soak ends 2026-05-04T13:58Z.
- [ ] **BL-064 14d TG social soak** — ends 2026-05-11T22:10Z.
- [ ] **Paper-lifecycle widening soak** — .env tweaks deployed 2026-04-27T22:24Z. Soak ends ~2026-05-04T22:24Z.

## Pending operator action (blocked on user)

- [x] **2026-05-06 02:40Z — Telegram credentials wired up.** Bot @Srini_gecko_bot (id 8427551586) DM'd to chat_id 6337722878 (operator's @LowCapHunt account). Test message via `alerter.send_telegram_message` confirmed end-to-end delivery. .env backup at `.env.bak.tg_<timestamp>`. Unblocks: BL-063 moonshot alerts, BL-064 social dispatches, channel-silence heartbeat, auto_suspend kill-switch (incl. new combined-gate paths), paper fills, calibrate weekly --dry-run alert (PR #76), future BL-NEW-HPF would-fire alerts.

## Next deliverables (in priority order)

### 1. Self-learning Tier 1a + 1b (proposed, awaiting user go-ahead)

The user asked "why isn't the agent self-learning". My response (deferred decision): scope a single PR for **per-signal parameter table** + **auto-suspension of dud signals**. Roughly:

- New `signal_params` DB table — per-signal-type LEG_1_PCT / TRAIL_PCT / SL_PCT / etc. Defaults seeded from current global Settings.
- Weekly calibration script that reads `combo_performance` rolling 30d, writes recalibrated params back to `signal_params`. Operator approves before write goes live (dry-run flag default).
- Evaluator reads per-signal params instead of global Settings.
- Auto-suspension: rolling 30d net P&L < threshold → set signal's `enabled=False` in DB + Telegram alert. One-way switch (manual re-enable).
- Tests + 1-2 day estimate.

This is NOT ML — just data-driven static rules with self-resetting parameters. Real ML (outcome model, RL exit timing) gated on ≥1000 trades/signal stable for 30d (not yet).

**~~User has not approved scope yet. Resume by asking.~~ CLOSED 2026-05-04 — already shipped.**

Drift research 2026-05-04 confirmed every component is in tree and operating in production:

- ✅ `signal_params` table + `signal_params_audit` (`scout/db.py:1578-1679`)
- ✅ `SignalParams` dataclass + `get_params` + cache (`scout/trading/params.py`)
- ✅ `SIGNAL_PARAMS_ENABLED=true` on prod
- ✅ **`scout/trading/calibrate.py`** (557 lines) — `--apply` / `--dry-run` / `--since-deploy` / `--force-no-alert`
- ✅ **`scout/trading/auto_suspend.py`** (268 lines) — hard_loss + pnl_threshold triggers
- ✅ Auto-suspend wired in `_run_feedback_schedulers` at `scout/main.py:163-170`
- ✅ Dashboard endpoint at `dashboard/api.py:953`
- ✅ Plan/design at `tasks/plan_tier_1a_1b.md` (544 lines, 5-reviewer signed off)

**Production evidence Tier 1b is firing daily** (3 audit rows by `applied_by='auto_suspend'`):
- 2026-05-02T01:00:18Z — first_signal + losers_contrarian (hard_loss)
- 2026-05-04T01:01:02Z — gainers_early (hard_loss)

**Real residual gaps (small, NOT blocking):**
- Calibrator never run in production (0 audit rows with `applied_by='calibration'`); operator-manual-by-design. Optional follow-up: weekly cron `--dry-run` + Telegram diff alert (no auto-apply).
- BL-067 opt-in 2026-05-04T15:31Z flipped `conviction_lock_enabled=1` for first_signal + gainers_early, both currently `enabled=0` (auto-suspended). Lock works on existing open trades only. Strategy decision pending: re-enable for new entries, or stay suspended-with-locked-existing.

### 2. Watchlist for next strategy-tuning re-check

When user asks "how is strategy tuning going" tomorrow:
- Re-run `.ssh_recheck.txt` queries (commands documented in conversation)
- Compare 36h post-deploy vs 13.5h baseline
- Look for: BL-064 first dispatched trade (depends on curator activity), trail/leg-1 fire rate stabilizing, gainers_early per-trade P&L sign

### 3. Open optional follow-ups (not urgent)

- [x] **2026-05-06 Channel-list reload task in BL-064 listener** — CLOSED-AS-SHIPPED. Drift-check finds: PR #73 (`a12603f`, 2026-05-04) shipped channel hot-reload via `_channel_reload_once` (`scout/social/telegram/listener.py:1252-1325`), heartbeat factory `_make_channel_reload_heartbeat` at line 1327, and structural-typed channels_holder TypedDict refactor in PR #75 (`8e54578`). Listener swaps handlers on reload without pipeline restart. todo.md item was stale.
- [ ] `narrative_prediction` token_id divergence fix — 32 of 56 stale-young open trades have empty/synthetic token_ids that don't appear in `price_cache`. Separate upstream fix.
- [x] **2026-05-06 @s1mple_s1mple verdict — DO-NOT-ADD (off-thesis).** Background investigation 2026-05-06: `@s1mple_s1mple` doesn't resolve via Bot API (likely user account, not channel — incompatible with Telethon listener). `@s1mplegod123` resolves as Russian-language esports diary "Дневник Симпла" (Counter-Strike pro s1mple of NaVi), 256K subscribers, ZERO crypto content across t.me sample + 1,220 cross-channel mention rows. No DB references in 5 tables. Operator can still add as `trade_eligible=0, cashtag_trade_eligible=0` watch-only with 30-day re-eligibility check if desired despite fit, but default action is no-add. See investigation notes inline; no separate findings file written.
- [ ] Audit fix #4 (24h hard-exit if peak<5%) deferred — accumulate more data first.
- [x] **BL-NEW-REVIVAL-COOLOFF — SHIPPED 2026-05-06** (PR #81 / `57192cb`). 7-day default cool-off on `revive_signal_with_baseline` with `force=True` bypass. Plan-stage MUST-FIX: positive `applied_by='operator'` filter. Design-stage MUST-FIX: settings DI. PR-stage CRITICAL: caplog→capture_logs. All applied. Smoke-tested on VPS: cool-off correctly blocks losers_contrarian re-revival.
- [x] **#3 Channel-list reload — CLOSED-AS-SHIPPED 2026-05-06.** Drift-check: PR #73 (`a12603f`, 2026-05-04) shipped channel hot-reload via `_channel_reload_once` + heartbeat factory + channels_holder TypedDict. todo.md item was stale.
- [x] **narrative_prediction token_id divergence — UPSTREAM FIX SHIPPED 2026-05-06** (PR #80 / `eaf3523`). Original symptom (32/56 stale-young opens) resolved by PR #72 + zombie cleanup. Real upstream cause was agent.py emitting `category_heating` with `token_id=accel.category_id`, breaking chain pattern matching. Pre-fix: 2,770 anchors → 2 chain_completes. Post-fix: per-laggard emission with `token.coin_id`.
- [x] **#5 @s1mple_s1mple verdict — DO-NOT-ADD 2026-05-06.** Esports diary, no crypto.
- [x] **moonshot floor nullification — UPSTREAM FIX MERGED 2026-05-06** (PR #82, deploy held until 2026-05-13). Per-signal `moonshot_enabled INTEGER NOT NULL DEFAULT 1` opt-out flag.
- [ ] **first_signal revival decision** — under combined-gate rule, first_signal would NOT auto-fire (-$132 30d net is borderline). Operator decision: revive for soak, or leave suspended. Note: revival now subject to 7-day cool-off (PR #81); first revival ever bypasses cool-off cleanly.

## What shipped this session (2026-04-28 → 2026-04-29)

| PR | Commit | Topic |
|---|---|---|
| #55 | 4c057e3 | BL-064 listener resilience (bad-handle / crash-state / txn-lock) — 3 fixes + 13 tests |
| #56 | 9127959 | Drop explicit BEGIN IMMEDIATE — match project _txn_lock pattern |
| #57 | adf1a32 | Dashboard reconcile open-trade PnL$ and PnL% on partial-fill ladders |
| #58 | 2061675 | BL-064 per-channel `safety_required` flag — unblocks fresh memecoins |
| #59 | 3c83fb7 | Strategy tuning — adaptive trail + per-signal kill switches |

Test count: 1354 → 1389 passing (+35 across the PRs).

Prod .env current state (relevant flags):
```
PAPER_MAX_DURATION_HOURS=168
PAPER_SL_PCT=25
PAPER_LADDER_TRAIL_PCT=20
PAPER_LADDER_LEG_1_PCT=10.0           # PR #59 — was 25 default
PAPER_LADDER_LEG_1_QTY_FRAC=0.50
PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=false
PAPER_SIGNAL_TRENDING_CATCH_ENABLED=false
TG_SOCIAL_ENABLED=True
TELEGRAM_BOT_TOKEN=placeholder        # ⚠️ not real
TELEGRAM_CHAT_ID=placeholder          # ⚠️ not real
```

Active TG channels (7):
- `@detecter_calls` (trade_eligible, safety_required=0)
- `@thanos_mind` (trade_eligible, safety_required=0)
- `@cryptoyeezuscalls` `@Alt_Crypto_Gems` `@nebukadnaza` `@alohcooks` `@CallerFiona1` (alert-only, strict)
- `@gem_detecter` (retired — typo, doesn't exist on Telegram)

## Resume hook

When the user comes back, the obvious next move is one of:
1. Approve the Tier 1a + 1b self-learning PR scope and start that work
2. Re-run the post-deploy strategy check-in (24-36h window now)
3. Set the real Telegram bot token + chat_id

Default suggestion if user opens with a generic "what's up": run the post-deploy check-in (option 2) — it's quick and gives them fresh data.
