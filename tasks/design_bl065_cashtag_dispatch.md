# BL-065: Cashtag Dispatch — Design Document

**New primitives introduced:** None new beyond what `tasks/plan_bl065_cashtag_dispatch.md` (v2) declares. This design adds test matrix, edge case analysis, performance, rollback, and operational verification — no new schema, no new modules, no new log events beyond plan v2.

**Companion to:** `tasks/plan_bl065_cashtag_dispatch.md` v2.

---

## 1. Test coverage matrix

| Behaviour | Test | File |
|---|---|---|
| Schema column exists with NOT NULL DEFAULT 0 | `test_cashtag_trade_eligible_column_exists` | `tests/test_bl065_cashtag_dispatch.py` |
| New channels default to `cashtag_trade_eligible=0` | `test_cashtag_trade_eligible_default_zero_for_new_channel` | same |
| Migration records itself in `paper_migrations` | `test_cashtag_trade_eligible_migration_paper_migrations_row` | same |
| `_channel_cashtag_trade_eligible` helper (true/false/missing fail-closed) | `test_channel_cashtag_eligible_helper` | same |
| Gate A: blocked when `cashtag_trade_eligible=0` | `test_evaluate_cashtag_blocked_when_channel_disabled` | same |
| Gate B: blocked when `top.mcap < floor` | `test_evaluate_cashtag_blocked_when_below_floor` | same |
| Gate C: blocked when `top.mcap < 2× second.mcap` (ambiguous) | `test_evaluate_cashtag_blocked_when_ambiguous` | same |
| Gate C passes when only 1 candidate | `test_evaluate_cashtag_passes_when_only_one_candidate` | same |
| Gate C passes when top clearly dominates | `test_evaluate_cashtag_passes_when_clearly_dominant` | same |
| Gate D: blocked by `dedup_open` (shared with CA path) | `test_evaluate_cashtag_blocked_when_dedup_open` | same |
| **Gate F (R1#5 v2): blocked when channel hits daily cap** | **gap T1** | — |
| **Gate F: passes when channel under cap** | **gap T2** | — |
| **R2#3: empty candidates returns `cashtag_no_candidates` (not `cashtag_disabled`)** | **gap T3** | — |
| End-to-end: cashtag dispatch opens paper_trade with correct signal_data | `test_dispatch_cashtag_end_to_end_opens_paper_trade` | same |
| **R1#1 v2: DLQ-write failure does NOT kill listener** | **gap T4** | — |
| **R1#2 v2: CancelledError re-raises (not swallowed)** | **gap T5** | — |
| **R1#2 v2: Other Exception → `cashtag_dispatch_exception` gate** | **gap T6** | — |
| **R1#10 v2: `_persist_signal_row` failure does NOT kill listener** | **gap T7** | — |
| **R1#4 v2: resolver returns candidates in [search-rank-then-mcap] order** | `test_resolver_candidates_top3_order_contract` (skipped, build-phase: implement) | `tests/test_tg_social_resolver_ordering.py` |
| **R1#6 v2: cashtag dispatch with same SYMBOL but different token_id logs WARNING** | `test_dispatch_cashtag_logs_symbol_collision` | `tests/test_bl065_cashtag_dispatch.py` |

**Test gaps T1–T7 — build-phase action required:**

- **T1 (Gate F passing):** seed channel with `cashtag_trade_eligible=1`, insert `PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY` cashtag-resolution paper_trades opened today for that channel; assert next dispatch returns `blocked_gate="cashtag_channel_rate_limited"`.
- **T2 (Gate F under cap):** seed N-1 trades (one below cap); assert next dispatch passes.
- **T3 (`cashtag_no_candidates`):** call `_evaluate_cashtag` with `candidates=[]`; assert `blocked_gate="cashtag_no_candidates"` (NOT `cashtag_disabled`).
- **T4 (DLQ-write failure):** monkeypatch `_append_dlq` to raise; assert listener loop continues (no exception propagates), `tg_social_dlq_write_failed` log fires, original error context captured BEFORE the DLQ attempt.
- **T5 (CancelledError):** monkeypatch `dispatch_cashtag_to_engine` to raise `asyncio.CancelledError`; assert it propagates (NOT swallowed into `cashtag_dispatch_exception`).
- **T6 (other Exception):** monkeypatch `dispatch_cashtag_to_engine` to raise `RuntimeError`; assert listener catches, sets `blocked_gate="cashtag_dispatch_exception"` (distinct from `engine_rejected`), logs `tg_social_cashtag_dispatch_exception`, listener continues.
- **T7 (`_persist_signal_row` failure):** monkeypatch `_persist_signal_row` to raise; assert listener catches, logs `tg_social_persist_signal_failed`, does NOT propagate (trade is already opened, lifecycle owns it).

Build phase MUST add T1–T7. Without them, the v2 fixes are documented but unenforced — exactly the silent-degradation pattern the plan was rewritten to avoid.

---

## 2. Edge case + failure mode analysis

### Schema migration (Task 1)

**F1.1 — Migration runs while listener processes a message.** R1#3 v2 fix: `systemctl stop` BEFORE `git pull` closes the window. Documented in §0a of operational verification.

**F1.2 — Existing channels with `trade_eligible=1` immediately dispatch on next message.** No — `cashtag_trade_eligible` defaults to 0 (fail-closed). No traffic change for any channel until operator explicitly UPDATEs. Documented in §5 step 5.

**F1.3 — Operator forgets `removed_at IS NULL` in their UPDATE.** Helper `_channel_cashtag_trade_eligible` filters `removed_at IS NULL` — removed channels never dispatch even if cashtag flag is set. Test `test_channel_cashtag_eligible_helper` covers missing channel returning False; should add an explicit test for the `removed_at IS NOT NULL` case in build phase as a NIT.

### Dispatcher (Task 2)

**F2.1 — Gate F (rate cap) JSON1 query performance.** `json_extract` on every cashtag dispatch evaluation. **R2#6 v3 honesty correction (refined per PR #65 R1#1 4th-pass review):** JSON1 expressions are NOT index-able by SQLite without an explicit expression index. The actual query plan picks `SEARCH paper_trades USING INDEX idx_paper_trades_signal (signal_type=?)` — NOT the composite `idx_paper_trades_combo_opened` (since the query doesn't reference `signal_combo`, the leftmost column). Effect is equivalent — narrows to same-day tg_social rows; json_extract is then per-row Python within that narrowed scan. At current cardinality (5–50 same-day tg_social rows in prod) this is sub-millisecond. v3 does NOT add an expression index because cardinality doesn't justify the maintenance cost; revisit if message volume grows 100×.

**F2.2 — Gate F counter is wall-clock-day, not 24h rolling.** A noisy curator at 23:59 UTC can dispatch 5; at 00:01 UTC dispatch another 5 = 10 in 2 minutes. Acceptable v1 tradeoff (simpler SQL, predictable boundary). Operator can lower the cap if abuse seen. Documented in v2 R1#5 plan note.

**F2.3 — Gate C (disambiguity ratio) divide-by-zero edge case.** Guard `if second_mcap > 0` already in plan code (Step 2.5); test `test_evaluate_cashtag_blocked_when_ambiguous` exercises non-zero second_mcap. **Build-phase add:** test for `second_mcap == 0` (passes top through? blocks? — design says passes through because the `if second_mcap > 0` guard skips the comparison; verify behaviour matches intent).

**F2.4 — Symbol-collision warning fires on every cashtag dispatch even when no collision.** Helper queries first; only logs if collisions found. NIT — minor extra DB query but bounded.

**F2.5 — `_check_potential_symbol_duplicate` runs AFTER `engine.open_trade` succeeded.** If the helper itself raises (DB locked etc.), the trade is already opened. Wrap the helper call in try/except + log. **Build phase action:** add inner try/except around `_check_potential_symbol_duplicate` call.

### Listener (Task 3)

**F3.1 — `dispatch_cashtag_to_engine` raises `OSError` from underlying aiosqlite.** Caught by broad `except Exception`; gate=`cashtag_dispatch_exception`; logged with `error_type="OSError"`. Listener continues. Correct.

**F3.2 — `_append_dlq` itself raises `OSError`.** Inner nested try/except logs `tg_social_dlq_write_failed`; outer doesn't crash. Listener continues. Correct (R1#1 v2 fix).

**F3.3 — `format_candidates_alert` raises after dispatch succeeded.** No try/except wraps the formatter call; would propagate up through the listener loop. **Build phase action:** wrap formatter in try/except too. Failure mode: alert not sent, but trade is open — provenance partially captured (paper_trade_id known from successful dispatch).

**F3.4 — `send_telegram` raises after formatter succeeded.** Already wrapped in try/except at plan code: logs `tg_social_alert_send_failed`, listener continues. Correct.

**F3.5 — Two cashtag messages arrive within milliseconds for the SAME channel near the cap.** Both pass `_channel_cashtag_trades_today_count(N-1) < cap`; both dispatch. Gate F is racy. **R1-S1 v3 honesty correction:** under burst conditions (N+1 messages within <100ms), Gate F can be exceeded by ~2× in the worst case (e.g., cap=5, two messages at the same instant both observe count=4, both dispatch → 6 trades). The HARD cap that ALWAYS holds is `TG_SOCIAL_MAX_OPEN_TRADES` (currently 20 globally — per-channel can exceed cap as long as total stays under 20). Why this is acceptable in v1: (a) Telegram listener processes messages sequentially per channel (Telethon's MessageEvent ordering is sync per dialog), so true within-100ms races require multi-channel collision; (b) paper-trade scope means worst-case cost is a few extra paper trades, not real money; (c) folding the count + insert into a transaction is the proper fix but adds complexity not justified at v1 scale. If real-world data shows the race fires, BL-065' upgrade to transactional rate-cap.

### Cross-cutting

**F4.1 — Cashtag dispatched, then curator posts CA on same channel for same token.** CA path's `_has_open_tg_social_exposure(token_id)` blocks the second trade (same coin_id resolves the same way). Plan claim verified.

**F4.2 — Cashtag dispatched on `pepe`, CA dispatched on `pepe-bsc` (different chain, same memecoin, different coin_id).** Dedup misses. R1#6 v2 mitigation: WARNING fires. No automatic block. Documented as v1 tradeoff.

**F4.3 — Test environment doesn't have `json_extract` (older SQLite).** SQLite 3.38+ ships JSON1 by default. Project's existing BL-064 already uses `json_extract` for signal_data. Safe.

---

## 3. Performance considerations

| Change | Per-message cost | Per-cycle cost | Notes |
|---|---|---|---|
| Schema migration (one-shot at startup) | n/a | n/a | Single ALTER, paper_migrations gate |
| Gate A (channel lookup) | 1 DB SELECT (indexed) | n/a | ~0.1ms |
| Gate B/C (mcap floor + disambiguity) | Pure Python | n/a | Negligible |
| Gate D (dedup) | 1 DB SELECT JOIN | n/a | Existing query, indexed |
| Gate E (open count) | 1 DB SELECT COUNT | n/a | Existing |
| Gate F (rate cap, NEW) | 1 DB SELECT COUNT with json_extract | n/a | Sub-ms with same-day cardinality (~5-50 rows) |
| Symbol-collision check | 1 DB SELECT JOIN with UPPER | n/a | ~0.5ms; only logs if collisions found |
| Listener nested try/except | Negligible | n/a | Pure Python control flow |

**Total added per cashtag message:** ~3 extra DB SELECTs (Gate F + symbol-check + json_extract). At BL-064's current 1,019 messages/week (~150/day) cadence, this is negligible. If message volume grows 100×, profile json_extract first.

---

## 4. Rollback strategy

| Item | Rollback method | Side effects |
|---|---|---|
| Task 1 (column) | `git revert` + restart | Column stays in `tg_social_channels` (SQLite ALTER DROP requires table rebuild). Unused column = harmless. |
| Task 2 (dispatcher) | `git revert` + restart | Listener falls back to ImportError on `dispatch_cashtag_to_engine` — would break unless listener is also reverted. Revert in this order: Task 3 (listener) first, then Task 2. |
| Task 3 (listener) | `git revert` + restart | Reverts to early-return at line 249-276 (Bundle B-pre behaviour). Existing alert-only flow restored. No data loss. |
| Task 4 (resolver test + symbol warning) | `git revert` + restart | Loses observability for symbol collisions. No correctness impact. |

**Combined rollback (full BL-065 rollback):** revert in order Task 3 → Task 2 → Task 4 → Task 1. Or simpler: revert all 4 commits via `git revert --no-commit COMMIT1 COMMIT2 COMMIT3 COMMIT4 && git commit`. Either way: no data loss, schema column persists (harmless), listener returns to pre-BL-065 alert-only behaviour.

**Operational kill-switch (no code change):** SQL `UPDATE tg_social_channels SET cashtag_trade_eligible = 0 WHERE channel_handle = '@misbehaving'` instantly stops dispatch for one channel. `UPDATE tg_social_channels SET cashtag_trade_eligible = 0` instantly stops cashtag dispatch globally.

**R1-S3 v3 — kill-switch caching assumption (DO NOT VIOLATE):** kill-switch instantness depends on `_channel_cashtag_trade_eligible` querying the DB on every message (no module-level cache). Verified true today; helper does a fresh `SELECT cashtag_trade_eligible FROM tg_social_channels WHERE channel_handle=? AND removed_at IS NULL` per call. **A future performance optimization that adds caching to this helper MUST also revise the kill-switch RTO promise** (or add a cache-invalidation channel). If this gets refactored without that consideration, the kill-switch silently degrades from instant to "instant + cache TTL." Documented here so a future PR doesn't break this contract by accident.

---

## 5. Operational verification post-deploy

(See plan v3 §5 for the deploy-order sequence — `systemctl stop` BEFORE `git pull` per R1#3 v2.)

**R1-N1 v3 confirmation: not a zero-downtime regression.** gecko-alpha has no zero-downtime deploy practice today (single VPS, single systemd unit, every PR causes a full restart per memory `project_bl062_deployed_2026_04_24.md`). Adding `systemctl stop` before `git pull` is not a downgrade — it's making the implicit ~10-second downtime explicit and ordered. If we ever adopt blue-green or rolling deploys, this convention needs updating; today it's the right shape.

**Post-restart verification:**
1. Service active+running: `systemctl status gecko-pipeline`
2. Migration applied: `sqlite3 scout.db "SELECT name FROM paper_migrations WHERE name='bl065_cashtag_trade_eligible'"` returns row
3. Column exists: `sqlite3 scout.db "PRAGMA table_info(tg_social_channels)"` lists `cashtag_trade_eligible INTEGER`
4. Default fail-closed: all existing channels show `cashtag_trade_eligible=0`
5. No new exceptions in 5 min post-restart: `journalctl -u gecko-pipeline --since '5 min ago' | grep -iE 'error|exception|traceback'` returns 0 or only pre-existing known-noise
6. **Operator-driven enable** (post-verify): `sqlite3 scout.db "UPDATE tg_social_channels SET cashtag_trade_eligible = 1 WHERE channel_handle = '@thanos_mind'"` (or whichever curator is the experiment target)
7. **First cashtag dispatch verification** (when curator posts a cashtag-only signal): journalctl shows `tg_social_cashtag_trade_dispatched` event with `paper_trade_id=N`, `cashtag=$X`, `candidates_total=N`. Cross-check with `sqlite3 scout.db "SELECT id, signal_data FROM paper_trades WHERE signal_type='tg_social' AND signal_data LIKE '%cashtag%' ORDER BY id DESC LIMIT 1"`.
8. **Gate behaviour spot-check:** for cashtag-disabled channels, journalctl shows `tg_social_cashtag_admission_blocked gate_name=cashtag_disabled` events. For ambiguous candidates, `gate_name=cashtag_ambiguous`. For dust mcap, `gate_name=cashtag_below_floor`.
9. **R1#5 cap verification:** if a curator dispatches 5 cashtag trades in one day, the 6th attempt logs `gate_name=cashtag_channel_rate_limited`.

---

## 6. Open questions / non-blocking items

1. **Disambiguity ratio empirical default 2.0** — chosen as v1 baseline. Revisit after 2 weeks of cashtag dispatch data: are operators seeing `cashtag_ambiguous` blocks on legitimate top candidates? Tune via `.env` if so.
2. **Per-channel daily cap default 5** — same v1 baseline rationale. Tune in `.env` once we see real cadence per curator.
3. **Per-channel daily $loss circuit breaker (R1#7 deferral)** — not in scope. If R1#5 rate cap proves insufficient (e.g., curator dispatches 5 honeypots → 5 wipeouts), add `PAPER_TG_SOCIAL_CASHTAG_DAILY_LOSS_USD` setting in BL-065'.
4. **Per-symbol dedup option (R1#6 deferral)** — current mitigation is observability (WARNING). If operators see frequent cross-listing collisions, BL-065' adds an opt-in setting `PAPER_TG_SOCIAL_DEDUP_BY_SYMBOL: bool = False`.

---

## 7. Self-review

- [x] Test gaps T1–T7 explicitly named with build-phase actions
- [x] All failure modes analyzed for each task (Schema: 3, Dispatcher: 5, Listener: 5, Cross-cutting: 3)
- [x] Performance impact quantified (§3)
- [x] Rollback strategy per task + combined + kill-switch (§4)
- [x] Operational verification with R1#3 deploy-order fix referenced
- [x] Open questions for v1 defaults (disambiguity ratio, daily cap)
- [x] No new primitives beyond plan v2
- [x] All v2 plan-review fixes have a corresponding test gap or existing test reference
