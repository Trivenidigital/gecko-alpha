**New primitives introduced:** Design companion to `tasks/plan_tg_alert_allowlist.md`. No new primitives beyond those declared in the plan. Documents architectural rationale, format decisions, async safety, alert volume math, and reversibility.

# TG Alert Allowlist — Design Document

## 1. Goals

When a paper-trade opens for a statistically-validated signal, send a concise Telegram alert. Defaults match the data-driven assessment: 4 default-allow signals (gainers_early, narrative_prediction, losers_contrarian, volume_spike); chain_completed delegated to the existing pattern-completion alerter. Per-token cooldown prevents inbox fatigue.

**Primary outcomes:**
1. `signal_params.tg_alert_eligible` → operator-controllable allowlist
2. `notify_paper_trade_opened` post-open hook → fires under eligibility + cooldown
3. `tg_alert_log` → audit + cooldown source
4. Failure isolation: TG dispatch never blocks paper-trade write

**Non-goals:**
- Multi-channel routing (single TELEGRAM_CHAT_ID destination)
- Alert digests / batching
- CLOSE alerts (TP / SL / expired)
- Modifying conviction-gated `send_alert` or research-only streams

## 2. Architectural choices

### 2.1 Sibling dispatcher vs unified rewrite

**Chosen:** new `scout/trading/tg_alert_dispatch.py`. Existing dispatchers (`scout/main.py:855`, `scout/chains/alerts.py`, research streams in `secondwave/`, `velocity/`, `social/`) untouched.

**Why:** smallest blast radius. The existing surfaces have their own quality gates and aren't paper-trade-bound. Refactoring them into one allowlist is a bigger architecture move that doesn't serve the operator's "for now" framing.

**Tradeoff:** operator inbox still has heterogeneous TG sources. Future work could unify; not in scope for M1.

### 2.2 chain_completed exclusion (R2-C2 fold)

**Chosen:** `chain_completed` defaults to `tg_alert_eligible=0`.

**Why:** `scout/chains/alerts.py:59` already fires a Telegram alert when a chain pattern completes. Adding a paper-trade-open dispatch for chain_completed means **2 alerts per chain event** (the pattern-completion + the paper-trade-open). Operator inbox sees duplication.

The user's allowlist listed chain_completed as #5 provisional. Translation in this design: "chain_completed already alerts via existing path; new dispatch is unnecessary." If operator wants both: `UPDATE signal_params SET tg_alert_eligible=1 WHERE signal_type='chain_completed'`.

**Tradeoff:** if the existing chain_completed alert ever breaks (eg. `chains/alerts.py` regression), chain_completed would silently miss alerts. Mitigation: existing alerter has its own tests; not adding redundancy is fine.

### 2.3 Provisional gate removed (R1-I4 fold)

**Chosen:** no `TG_ALERT_PROVISIONAL_MIN_TRADES` gate in M1.

**Why:** the prior plan's provisional gate was for chain_completed (n=8 < 30 → block). With chain_completed dropped from default-allow, no signal in the M1 default list needs a count-based gate. The 4 default-allow signals all have n ≫ 30 in the 14d post-tuning window.

If a future signal needs provisional gating, the gate can be added then.

**Tradeoff:** trending_catch (currently in re-enable soak — see `project_trending_catch_soak_2026_05_10.md`) won't be auto-graduated to TG when n=50 lands. Operator promotes manually via `UPDATE signal_params SET tg_alert_eligible=1`. Acceptable — small operator action, deliberate decision point.

### 2.4 Per-token cooldown (across signals) — R2-I1 fold

**Chosen:** cooldown keyed on `token_id` only, not `(signal_type, token_id)`. Default 6h.

**Why:** operator scenario: bitcoin fires gainers_early at 10am, then losers_contrarian at 4pm. With per-(signal, token) cooldown, operator gets 2 alerts for the same token in 6h — duplicative noise. With per-token, 1 alert (the first; the second is suppressed via cooldown).

**Why 6h (not 24h):** long enough to suppress the multi-snapshot bursts that motivate the cooldown; short enough that legitimate second-leg pumps re-alert. At observed ~20-25 alerts/day across 4 signals, 6h cooldown still effective.

**Tradeoff:** operator misses the second-leg pump if same token re-fires within 6h. Acceptable — first-leg already dispatched a paper trade; operator already knows the token is interesting.

### 2.5 Format: single-line header + extras + link (R2-format fold)

**Chosen:**
```
📈 GAINERS EARLY · BTC · $50000.00 · $100
24h: +36.9% · mcap $5.5M
coingecko.com/en/coins/bitcoin
```

**Why:** phone-screen norms favor 2-3 lines visible without expanding. Single-line header carries the must-read data (signal, symbol, entry, size). Per-signal extras line includes only fields the dispatcher actually emits. Link enables one-tap research.

**Why no Markdown:** R1-C1 fold — signal_type contains underscores ("GAINERS_EARLY"). Telegram default `parse_mode=Markdown` parses underscores as italic delimiters; closing-delimiter mismatch produces a silent 400 BAD_REQUEST. The format normalizes to spaces ("GAINERS EARLY") and the caller dispatches with `parse_mode=None`. This is the same bug class caught pre-merge in PR #76 (memory: `project_overnight_2026_05_05.md`).

**Per-signal field map (R2-C1 fold):** verified against `scout/trading/signals.py` actual emissions. Earlier plan's format included `narrative_score` which the dispatcher never produces — narrative_prediction alerts (≈25% of fires) would have shipped blank.

### 2.6 Engine post-open hook with task ref-holding (R1-C3 fold)

**Chosen:** `engine._spawn_tg_alert(...)` helper:
1. Re-reads `entry_price` from `paper_trades` (post-slip; matches audit row per R1-C2)
2. Spawns `notify_paper_trade_opened` as `asyncio.create_task`
3. Adds task to `self._tg_alert_tasks: set[asyncio.Task]`
4. Registers `task.add_done_callback(self._tg_alert_tasks.discard)` so completed tasks are gc'd

**Why ref-holding:** `asyncio.create_task` returns a Task object; if no reference is held, Python's garbage collector may collect the awaitable mid-flight, producing `RuntimeWarning: coroutine was never awaited` and dropped exceptions. The project already has this pattern at `scout/main.py:91` (`_social_restart_tasks`). Mirroring it here.

**Why post-slip price re-read:** `paper.execute_buy` writes `effective_entry = current_price * (1 + slippage_bps / 10000)` to the row. The TG alert showing pre-slip would silently disagree with `paper_trades.entry_price` for every fire — auditable defect. Re-reading after open ensures alert matches DB.

### 2.7 Migration with full schema_version contract (R1-I1 + R1-I2 fold)

**Chosen:** mirror `_migrate_bl_quote_pair_v1` (db.py:2733):
- `schema_version` row with version 20260516
- `BEGIN EXCLUSIVE` + `ROLLBACK` on error
- `paper_migrations` cutover row (audit)
- Post-assertion that 4 default-allow rows exist
- Idempotent: skips if `schema_version` row already present

**Registration order** (R1-I2 fold): the migration MUST be appended AFTER `_migrate_bl_slow_burn_v1` (currently last at `db.py:102`) so that `_migrate_signal_params_schema` (db.py:91) seeds the `signal_params` rows BEFORE the default-allow `UPDATE` runs. If reordered earlier, the UPDATE is a no-op and post-assertion fails.

## 3. Alert volume + operator activation experience

**Observed paper-trade rates last 14d (post-tuning):**
- gainers_early: 171 trades / 14d ≈ 12/d
- narrative_prediction: 112 / 14d ≈ 8/d
- losers_contrarian: 84 / 14d ≈ 6/d
- volume_spike: 23 / 14d ≈ 1.6/d
- **Combined: ~28 trades/d**

**With per-token 6h cooldown:** assume ~15-25% of fires hit the cooldown (multi-snapshot bursts on the same token; cross-signal duplicates). Effective alert volume: **~20-25/day**.

**On busy news days** (sector rotation, major event): up to ~40/day. That's ~1 alert every 35-70 minutes — borderline phone-fatigue for a manual-research workflow but tolerable.

**First-deploy operator announcement** (R2-I3 + R2-I4 fold): one-time TG message on first cycle when `tg_alert_log` is empty:
```
📢 TG alert allowlist active: gainers_early, narrative_prediction,
   losers_contrarian, volume_spike (open-only; check dashboard for
   closes). chain_completed via existing chain alerter.
   Per-token 6h cooldown.
```
Sets expectations + makes the open-only scope explicit.

## 4. Failure modes + isolation invariant

**Invariant:** TG dispatch failure NEVER blocks paper-trade write.

**Three layers of defense:**
1. `notify_paper_trade_opened` outer try/except — even logging failures don't propagate
2. Inner try/except around `send_telegram_message` — network errors logged as `dispatch_failed`
3. Engine spawns dispatch as `asyncio.create_task` — caller (paper-trade success path) returns immediately even if dispatch hangs

**Cooldown counts only `outcome='sent'`** — transient failures don't suppress next legitimate fire. Operator scenario: Telegram bot has a 30s outage, alert N is logged as `dispatch_failed`. Alert N+1 (same token, 5min later) is NOT blocked by cooldown; it gets a fresh attempt.

**Failure observability:** every outcome (sent / blocked_eligibility / blocked_cooldown / dispatch_failed) writes a `tg_alert_log` row. Operator can SQL: `SELECT outcome, COUNT(*) FROM tg_alert_log WHERE alerted_at >= datetime('now', '-1 day') GROUP BY outcome;` to triage.

## 5. Reversibility

**Fast revert (no code, no deploy):** `UPDATE signal_params SET tg_alert_eligible=0 WHERE signal_type IN (...)` silences all alerts in one SQL statement. Single command per signal; revertible per-signal.

**Slower revert (git):** `git revert <PR squash>` reverts:
- Migration (column add is left in place; harmless because column is unused; or a follow-up migration drops it)
- Engine hook (no behavior change without dispatcher)
- Settings field
- New module + tests

**In-flight tasks during revert:** if engine restart happens mid-task, the `asyncio.create_task` is cancelled; `notify_paper_trade_opened`'s outer except catches `CancelledError`. No orphan rows.

## 6. Test strategy

**Unit tests (`tests/test_tg_alert_dispatch.py`):**
- Eligibility allow / block / unknown-signal
- chain_completed default block (R2-C2)
- Cooldown within / after window
- Cooldown across signals for same token (R2-I1)
- Cooldown only-counts-sent
- Format per-signal (gainers_early, narrative_prediction, volume_spike) — verify field map
- Format no-Markdown-specials sanity (R1-C1)
- `notify_*` writes log row on success / eligibility-block / cooldown-block / dispatch-failure

**Integration tests (`tests/test_engine_post_open_hook.py`):**
- Hook fires on successful open
- Hook does NOT fire when `trade_id is None`
- Hook failure does NOT block engine return (R1-C3 invariant)
- Engine `_tg_alert_tasks` set is populated then drained

**Migration test:**
- Default-allow signals have `tg_alert_eligible=1`
- chain_completed has `tg_alert_eligible=0`
- Migration is idempotent (run twice → same state)

**Total: ~16 new test cases.**

## 7. Open questions — resolved

**Q1 (R1):** Migration ordering?
- **Resolved (§2.7):** append AFTER `_migrate_bl_slow_burn_v1`.

**Q2 (R1):** Async task ref-holding?
- **Resolved (§2.6):** `_tg_alert_tasks` set + add_done_callback.

**Q3 (R1):** Pre-slip vs post-slip entry price?
- **Resolved (§2.6):** post-slip via re-read.

**Q4 (R1):** parse_mode handling?
- **Resolved (§2.5):** `parse_mode=None` + format normalizes underscores.

**Q5 (R2):** chain_completed double-alert?
- **Resolved (§2.2):** chain_completed defaults to off; existing chain alerter is the path.

**Q6 (R2):** Cooldown granularity?
- **Resolved (§2.4):** per-token-across-signals at 6h.

**Q7 (R2):** First-deploy operator message?
- **Resolved (§3):** one-time announcement on empty `tg_alert_log`.

## 8. Reviewer-fold summary (plan-stage)

| Finding | Reviewer | Severity | Status |
|---|---|---|---|
| Markdown 400 silent-fail (signal_type underscore) | R1 | C1 | **Folded — parse_mode=None + format normalizes** |
| Pre-slip vs post-slip price divergence | R1 | C2 | **Folded — engine re-reads entry_price post-open** |
| Orphan asyncio.create_task | R1 | C3 | **Folded — `_tg_alert_tasks` set + done_callback** |
| signal_data field map mismatch (blank alerts) | R2 | C1 | **Folded — per-signal field maps verified against signals.py** |
| chain_completed duplicate alert | R2 | C2 | **Folded — chain_completed defaults to off** |
| Migration BEGIN EXCLUSIVE + schema_version stamp missing | R1 | I1 | Folded |
| Migration registration order | R1 | I2 | Folded |
| `db._conn` private access | R1 | I3 | Documented (project pattern) |
| "graduate" language vs hardcoded list | R1 | I4 | Folded — provisional gate removed entirely |
| Engine session wiring | R1 | I5 | Folded — constructor accepts session, main.py passes |
| Test helper `_seed_closed_trades` undefined | R1 | I6 | Folded — direct INSERT in tests; provisional tests removed |
| Cooldown spam — per-(signal, token) | R2 | I1 | **Folded — per-token-across-signals at 6h** |
| chain_completed double alert (overlap with C2) | R2 | I2 | Folded |
| Provisional state invisible to operator | R2 | I3 | Folded — first-deploy announcement |
| Open-only scope undocumented | R2 | I4 | Folded — first-deploy announcement |

## 9. Approval checklist

- [x] Plan-stage 2-reviewer pass complete (folded at `53f63e2`)
- [ ] Design-stage 2-reviewer pass complete (this commit)
- [ ] All folds applied + test coverage verified
- [ ] Build → PR → 3-vector reviewer pass → merge → deploy
