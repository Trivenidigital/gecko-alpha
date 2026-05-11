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

## 9. Design-stage reviewer folds (round 2)

### CRITICAL folds

**R2-C1: First-deploy announcement re-fires on every engine restart before first eligible trade.**
The earlier "empty `tg_alert_log` → fire" idempotency is wrong: between deploy and first eligible paper-trade open (minutes-hours at observed rate), every engine restart re-fires the announcement.

**Fix:** anchor on a sentinel row inserted at announcement-send time. Extend the `tg_alert_log.outcome` CHECK constraint to admit `'announcement_sent'`; insert that row immediately on send; gate the announcement on `SELECT 1 FROM tg_alert_log WHERE outcome='announcement_sent' LIMIT 1`. Migration adds `'announcement_sent'` to the CHECK enum.

**R2-C2: Concurrent dispatch race writes 2-N alerts for the same token within 100ms — defeats per-token cooldown.**
`scout/main.py:539,559,584,604,794` calls dispatcher functions sequentially. Each iterates candidates and calls `engine.open_trade` → `_spawn_tg_alert` → `asyncio.create_task`. Tasks are NOT awaited; the next dispatcher call can fire for the same token before task #1's `tg_alert_log` INSERT lands. Both tasks see empty cooldown query → both fire.

**Fix:** atomic check-then-write inside the spawned task. New flow:
1. Acquire `db._txn_lock`
2. Re-check cooldown
3. Pre-INSERT a tentative `'sent'` row (or the appropriate `blocked_*` outcome)
4. Release lock
5. If outcome was `'sent'` (i.e., we won the race), call `send_telegram_message`
6. If dispatch fails, `UPDATE tg_alert_log SET outcome='dispatch_failed' WHERE id=?`

This ordering — write-then-dispatch — means concurrent tasks see each other's pending sends through `_check_cooldown`, and only one proceeds. New test: spawn 3 concurrent `notify_paper_trade_opened` for same token → assert exactly 1 outcome='sent'.

**R1-C2: Post-assertion `COUNT(*)=4` is fragile.**
If `_migrate_signal_params_schema` ever drops one of the 4 default-allow signals from the seed list (rename / retire), the post-assertion fails on every startup, blocking init.

**Fix:** assert each signal individually:
```python
for sig in DEFAULT_ALLOW_SIGNALS:
    cur = await conn.execute(
        "SELECT tg_alert_eligible FROM signal_params WHERE signal_type=?",
        (sig,),
    )
    row = await cur.fetchone()
    assert row and row[0] == 1, f"bl_tg_alert_eligible_v1: {sig} not eligible"
```

**R1-C1 (cosmetic): drop `async with self._txn_lock:` to match `_migrate_bl_quote_pair_v1` precedent.** Migration runs at startup-only (single coroutine) — lock is harmless but inconsistent. Fold cosmetic.

### Important folds

**R1-I2 — engine `_tg_alert_tasks` shutdown handling:**
acceptable to lose mid-flight tasks (paper-trade row already written; only the alert + log is lost). Document explicitly in `_spawn_tg_alert` docstring: "Mid-flight task loss on shutdown is acceptable — the paper_trades row is already committed; the only loss is the TG alert + tg_alert_log row." No code-side drain in M1.

**R1-I3 + R1-I4 — `scout/main.py` wiring spec:**
- Pass `aiohttp.ClientSession` into `TradingEngine` constructor at `scout/main.py:1230` (need to refactor to construct session before engine OR pass an async-context-manager factory). Engine stores as `self._tg_session`; `_spawn_tg_alert` uses it.
- Announcement call site: in `scout/main.py` after `Database.initialize()` runs but BEFORE first cycle. Plan must specify this is in the cycle setup section after migration runs (so `tg_alert_log` table exists). Concrete site: just before the cycle while-loop, inside the `aiohttp.ClientSession` block.

**R1-I5 — test coverage gaps:**
Add tests:
- `test_migration_idempotent`: run `_migrate_tg_alert_eligible_v1` twice → no error, row count stable
- `test_post_slip_price_in_alert_body`: insert paper_trade with effective_entry differing from current_price; assert alert body contains effective_entry
- `test_engine_constructor_accepts_session_kwarg`: minimal smoke
- `test_concurrent_dispatch_only_one_sent` (per R2-C2): spawn 3 tasks for same token, assert 1 sent + 2 blocked_cooldown

**R2-I1 — auto_suspend joint flag maintenance:**
Modify `scout/trading/auto_suspend.py:_atomic_suspend` to also set `tg_alert_eligible=0` when suspending a signal. Audit row tracks both flag changes. On `revive_signal_with_baseline`, restore `tg_alert_eligible=1` if the signal was originally in the default-allow list.

**R2-I2 — operator opt-out path in announcement:**
Add to first-deploy announcement body: `"To silence per-signal: UPDATE signal_params SET tg_alert_eligible=0 WHERE signal_type='...';"`

**R2-I3 — second-leg tradeoff documentation:**
The 6h cooldown is exposed as `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS` env-tunable. Document in announcement body: "Cooldown 6h; reduce via .env TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=2 to receive second-leg signals."

**R2-I4 — chain_completed dual-session documentation:**
Document in §2.2 that flipping chain_completed eligibility=1 produces 2 simultaneous TG sends (existing chains/alerts.py + new dispatch). Each opens its own ClientSession briefly (low chain rate makes this acceptable).

### Reviewer-fold table (design-stage)

| Finding | Reviewer | Severity | Status |
|---|---|---|---|
| Migration `async with self._txn_lock` deviation | R1 | C1 | **Folded — drop lock to match precedent** |
| Post-assertion `COUNT(*)=4` fragility | R1 | C2 | **Folded — per-signal assertions** |
| Announcement re-fires on restart before first trade | R2 | C1 | **Folded — `'announcement_sent'` sentinel row in tg_alert_log** |
| Concurrent dispatch race defeats per-token cooldown | R2 | C2 | **Folded — atomic check-then-write under `_txn_lock`** |
| `_tg_alert_tasks` shutdown drain | R1 | I2 | **Folded — documented acceptable loss** |
| `session=None` runtime path broken | R1 | I3 | **Folded — main.py wiring spec'd in plan** |
| Announcement call site unspecified | R1 | I4 | **Folded — main.py:1262-area** |
| Test coverage gaps | R1 | I5 | **Folded — 4 new tests** |
| auto_suspend doesn't clear tg_alert_eligible | R2 | I1 | **Folded — joint flag maintenance** |
| Opt-out path missing from announcement | R2 | I2 | **Folded — added to body** |
| 6h vs second-leg tradeoff | R2 | I3 | **Folded — env-tunable + announcement note** |
| chain_completed dual-session | R2 | I4 | **Folded — documented in §2.2** |
| db._conn private access | R1 | I1 | Documented (project pattern) |
| Emoji choice / format safety | R2 | M | Verified |

## 10. Approval checklist

- [x] Plan-stage 2-reviewer pass complete (folded at `53f63e2`)
- [x] Design-stage 2-reviewer pass complete (folds in this commit)
- [ ] All folds applied to plan + test coverage verified
- [ ] Build → PR → 3-vector reviewer pass → merge → deploy
