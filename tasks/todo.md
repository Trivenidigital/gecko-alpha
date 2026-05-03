# Backlog — gecko-alpha

Last updated: 2026-05-03 (chain dispatch alive + Tier 1a reversal)

## Pending verifications (time-gated)

- [ ] **2026-05-04 ~01:09Z+ — BL-071 guard verification (24h check).** First daily LEARN tick after the systemic-zero-hits guard deployed (2026-05-03 ~15:13Z). Sequence: chain_patterns reactivated 2026-05-03 15:13Z (3rd time) → BL-071 guard merged commit 2a45263 → patterns reactivated again 2026-05-03 15:13Z. The next LEARN tick at ~01:09Z UTC will run `run_pattern_lifecycle`. With the guard in place, it should hit `total_hits_across_all == 0`, log `chain_pattern_retirement_skipped_systemwide_zero_hits` warning, and short-circuit before any UPDATE.
  - Verify via:
    ```
    ssh srilu-vps "sqlite3 /root/gecko-alpha/scout.db 'SELECT id, name, is_active, updated_at FROM chain_patterns ORDER BY id'"
    ssh srilu-vps "journalctl -u gecko-pipeline --since '01:00' --no-pager | grep retirement_skipped_systemwide"
    ssh srilu-vps "sqlite3 /root/gecko-alpha/scout.db \"SELECT COUNT(*), MAX(opened_at) FROM paper_trades WHERE signal_type='chain_completed'\""
    ```
  - Pass criteria: all 3 patterns still `is_active=1`, warning visible in logs, chain_completed paper_trades count growing past current 7.
  - Fail recovery: if `is_active=0`, the guard didn't fire (different bug); investigate immediately.
- [ ] **2026-05-04 13:58Z — BL-063 moonshot soak ends.** Sneak-peek already strong (+$1,420 net on moonshot path vs +$473 on regular trail at peak ≥30%). Decision: keep on.
- [ ] **2026-05-04 22:24Z — Paper-lifecycle widening soak ends.** Sneak-peek +$1,234 net / 91 closes. Decision: keep on.
- [ ] **2026-05-05 22:58Z — PR #59 strategy tuning soak ends.** Sneak-peek +$1,994 net / 135 closes / 67.4% win / 20% expired. Decision: keep on permanently.
- [ ] **2026-05-10 15:53Z — gainers_early reversal re-soak (7d).** Watch for performance vs the +$190/day sneak-peek that justified reversal. If actuals < +$100/day for 7d, re-evaluate.

## Active soaks (don't disturb)

- [x] **Tier 1a flip — gainers_early kill REVERSED 2026-05-03T15:53Z** — original kill was based on pre-PR-#59 30d data. Sneak-peek of post-#59 data (4.7d window) showed gainers_early at +$508 / 59 closes / +$8.61/trade / 67.8% win — clearly profitable under the new adaptive trail. PR #59 fixed gainers_early; the kill was forfeiting ~$190/day. SQL reversal + restart verified: 5 new gainers_early trades opened at 15:58:29Z, zero `trade_skipped_signal_disabled` events. Tier 1a `SIGNAL_PARAMS_ENABLED=true` flag stays on for the other 7 signals (per-signal params still honored). Audit row in signal_params_audit. Backup: `scout.db.bak.gainers_revive_20260503_155322`.
- [ ] **PR #58 BL-064 lenient-safety soak** — flag flipped 2026-04-28T15:17Z. Re-check window: 2026-05-12.
  - Decision gate: ≥40% win rate + avg pnl_pct >0 → keep on. As of 2026-04-29T12:25Z: 0 trades dispatched yet (curators haven't posted CA-bearing messages since flag flipped). Operational gap, not code.
- [ ] **PR #59 strategy tuning soak** — deployed 2026-04-28T22:58Z. Re-check window: 2026-05-05.
  - Early signal at 13.5h: 23 closes, +$650 net, ~70% win rate, 0 expired closes. 9× improvement in $/trade vs historical −$3.05. Letting it ride.
- [ ] **BL-063 moonshot soak** — flag flipped 2026-04-27T13:58Z. Soak ends 2026-05-04T13:58Z.
- [ ] **BL-064 14d TG social soak** — ends 2026-05-11T22:10Z.
- [ ] **Paper-lifecycle widening soak** — .env tweaks deployed 2026-04-27T22:24Z. Soak ends ~2026-05-04T22:24Z.

## Pending operator action (blocked on user)

- [ ] **Set real `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in prod `.env`.** Both are literal placeholders. Every alert path (BL-063 moonshot, BL-064 social, channel-silence, kill-switch, paper fills) is silently 404-ing. Get a token from BotFather, `/start` the bot in the target chat, then `https://api.telegram.org/bot<TOKEN>/getUpdates` shows the chat_id.

## Next deliverables (in priority order)

### 1. Self-learning Tier 1a + 1b (proposed, awaiting user go-ahead)

The user asked "why isn't the agent self-learning". My response (deferred decision): scope a single PR for **per-signal parameter table** + **auto-suspension of dud signals**. Roughly:

- New `signal_params` DB table — per-signal-type LEG_1_PCT / TRAIL_PCT / SL_PCT / etc. Defaults seeded from current global Settings.
- Weekly calibration script that reads `combo_performance` rolling 30d, writes recalibrated params back to `signal_params`. Operator approves before write goes live (dry-run flag default).
- Evaluator reads per-signal params instead of global Settings.
- Auto-suspension: rolling 30d net P&L < threshold → set signal's `enabled=False` in DB + Telegram alert. One-way switch (manual re-enable).
- Tests + 1-2 day estimate.

This is NOT ML — just data-driven static rules with self-resetting parameters. Real ML (outcome model, RL exit timing) gated on ≥1000 trades/signal stable for 30d (not yet).

**User has not approved scope yet.** Resume by asking.

### 2. Watchlist for next strategy-tuning re-check

When user asks "how is strategy tuning going" tomorrow:
- Re-run `.ssh_recheck.txt` queries (commands documented in conversation)
- Compare 36h post-deploy vs 13.5h baseline
- Look for: BL-064 first dispatched trade (depends on curator activity), trail/leg-1 fire rate stabilizing, gainers_early per-trade P&L sign

### 3. Open optional follow-ups (not urgent)

- [ ] Channel-list reload task in BL-064 listener — currently each new channel requires pipeline restart. Long-pending.
- [ ] `narrative_prediction` token_id divergence fix — 32 of 56 stale-young open trades have empty/synthetic token_ids that don't appear in `price_cache`. Separate upstream fix.
- [ ] Verify @s1mple_s1mple / t.me/s1mplegod123 ownership before adding (long-pending).
- [ ] Audit fix #4 (24h hard-exit if peak<5%) deferred — accumulate more data first.

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
