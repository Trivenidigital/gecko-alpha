**New primitives introduced:** Same set as `tasks/plan_live_eligible_weekly_digest.md` (post V27/V28 fold, commit `da16ebe`) — module `scout/trading/cohort_digest.py` (`build_cohort_digest()` + `send_cohort_digest()` + `_compute_signal_cohort_stats()` + `_classify_verdict()` + `_detect_verdict_flip()`), 7 Settings fields, structured log events `cohort_digest_sent / cohort_digest_empty / cohort_digest_failed / cohort_digest_verdict_flip`, `main.py` weekly-loop hook + `last_cohort_digest_date` + `last_final_block_fired` state.

# Design: BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST

**Plan reference:** `tasks/plan_live_eligible_weekly_digest.md` (`9f3061b` + V27/V28 fold `da16ebe`)
**Pattern source:** `scout/trading/weekly_digest.py` shape — same dispatch surface, same chunk-split helper reuse, same retention story.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Scheduled cohort-comparison digests | None (hermes-agent.nousresearch.com/docs/skills probed 2026-05-17) | Build in-tree. |
| Generic weekly digest dispatch | None (architectural neighbor `scout/trading/weekly_digest.py`) | Reuse pattern. |

awesome-hermes-agent: 404. **Verdict:** custom; mirror `weekly_digest.py`.

## Design decisions

### D1. Single SQL pair per `build_cohort_digest` (NOT per-signal-type)

Query both cohorts once for the entire window:

```sql
-- full cohort (all enumerated types)
SELECT signal_type, COUNT(*) AS n, SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
       COALESCE(SUM(pnl_usd), 0) AS pnl
FROM paper_trades
WHERE status != 'open'
  AND closed_at >= ? AND closed_at < ?
  AND signal_type IN (?, ?, ?)  -- _LIVE_ELIGIBLE_ENUMERATED_TYPES
GROUP BY signal_type;

-- eligible cohort (same + would_be_live=1)
... AND would_be_live = 1 ...
```

For 2 windows (week N + week N-1) × 2 cohorts = **4 queries per digest**, not 4×|signals|. ~ms total at srilu scale.

**Rationale:** `_compute_signal_cohort_stats` as written in plan Task 2 takes one signal at a time — design ratifies the per-signal API but the implementation does ONE pair of queries up-front and dispatches results to the per-signal call. The plan's per-signal API is preserved so unit tests stay tight.

### D2. Module-level structured-log emit (mirror weekly_digest.py)

```python
log = structlog.get_logger()
```

Same package-level call. Events:
- `cohort_digest_sent` INFO with `bytes` field at end
- `cohort_digest_empty` INFO when no enumerated-type activity in window
- `cohort_digest_failed` exception in outer try
- `cohort_digest_verdict_flip` WARNING **once per digest** containing `flips=[(signal, prev, curr), ...]` (V28 SHOULD-FIX rolled-up)
- `cohort_digest_skipped_disabled` DEBUG when `COHORT_DIGEST_ENABLED=False` (V26-class observability symmetry — silent skip is ambiguous)

### D3. `send_cohort_digest` reuses `weekly_digest._split_for_telegram`

Per `scout/trading/weekly_digest.py:352`, the existing helper handles ≤4KB chunk splits. Cohort digest is unlikely to exceed 4KB (3 enumerated signals × 4 lines + footer), but defensive reuse keeps the chunking surface single-sourced.

Import path: `from scout.trading.weekly_digest import _split_for_telegram` — promoting private helper to dual-use. Acceptable since both modules live in `scout/trading/` and the helper is independently tested.

**Alt considered:** copy-paste the helper. Rejected — duplicates the chunking bug-surface (memory `feedback_resilience_layered_failure_modes.md`: every resilience addition extends a visibility surface; here we want LESS surface, not more).

### D4. `main.py` weekly-loop hook lives next to `send_weekly_digest` call

`scout/main.py:327` already has `await _weekly_digest.send_weekly_digest(db, settings)` inside a daily-cron-style block gated by `last_weekly_digest_date` + day-of-week check. The cohort hook mirrors that exactly:

```python
# After the existing weekly_digest dispatch:
if settings.COHORT_DIGEST_ENABLED:
    today = date.today()
    if today.weekday() == settings.COHORT_DIGEST_DAY_OF_WEEK and last_cohort_digest_date != today.isoformat():
        try:
            await _cohort_digest.send_cohort_digest(db, settings)
            last_cohort_digest_date = today.isoformat()
        except Exception:
            logger.exception("cohort_digest_loop_error")
```

Per V28 SHOULD-FIX final-window fallback: the FINAL block logic lives INSIDE `build_cohort_digest` based on `today >= COHORT_DIGEST_FINAL_DATE AND not last_final_block_fired`. State updates after a successful send, not on attempted-send (so a TG-dispatch-failure mid-final doesn't lose the flag).

### D5. Final-window state persistence

`last_cohort_digest_date` + `last_final_block_fired` are stored in the same in-process module-level state that `last_weekly_digest_date` lives in. **Lost on process restart.** That's acceptable for `last_cohort_digest_date` (worst case is a same-day re-fire on restart, which is itself idempotent because the day-of-week gate is already met), BUT `last_final_block_fired` cannot tolerate re-fire (operator gets the final decision-block twice).

**Mitigation:** stamp `last_final_block_fired` to a SQLite row in a tiny `cohort_digest_state` table after first send. Read on weekly-loop entry. One-row table, primary key on a constant marker; no migration risk.

Schema:

```sql
CREATE TABLE IF NOT EXISTS cohort_digest_state (
    marker INTEGER PRIMARY KEY DEFAULT 1,
    last_final_block_fired_at TEXT,
    CHECK (marker = 1)
);
```

CHECK constraint enforces singleton row.

### D6. Test fixtures + Windows-OPENSSL deferral

Existing `tests/conftest.py` has `token_factory`. We need a `paper_trade_factory` for seeding rows with arbitrary `signal_type / status / closed_at / pnl_usd / would_be_live`. Check if one exists; if not, build a minimal one within `tests/test_cohort_digest.py` itself (avoid plumbing through conftest unless ≥2 tests need it).

Per memory `reference_windows_openssl_workaround.md`, local pytest hangs on scout.main imports. Same VPS-deferral pattern as cycles 1-4.

### D7. Log emit timing

`_run_hourly_maintenance` already taught the pattern: emit AFTER the work, with the data payload spread. Same here — `cohort_digest_sent` emits AFTER successful `send_telegram_message`, with `bytes=len(text)` in the payload.

### D8. Telegram parse_mode

Per CLAUDE.md "What NOT To Do" + memory `feedback_class_3_silent_failure_rendering_corruption.md`: digest contains signal names with `_` (gainers_early, volume_spike, chain_completed). MarkdownV1 parses `_` as italics → mangling. **Use `parse_mode=None`** (plain text) for the cohort digest. Existing `send_weekly_digest` already uses `parse_mode=None` — verify and mirror.

---

## Cross-file invariants

| Invariant | Where enforced | Test |
|---|---|---|
| Verdict labels match dashboard verbatim | `_classify_verdict` constants | 9 verdict-classification tests in plan Task 2 |
| Stats query mirrors dashboard | `_compute_signal_cohort_stats` SQL | `test_compute_signal_cohort_stats_uses_status_not_open_and_closed_at` |
| Win uses `pnl_usd > 0` | Same SQL | covered transitively in `test_compute_signal_cohort_stats_basic` |
| Sign-flip emits once per digest, NOT per signal | `_detect_verdict_flip` + `build_cohort_digest` | `test_build_cohort_digest_emits_single_rolled_up_flip_event` |
| Final-window block fires on first eligible run AFTER lock date, not strict == | `build_cohort_digest` date check + `cohort_digest_state` row | `test_final_block_fires_on_first_eligible_run_after_lock_date` |
| `last_final_block_fired` survives process restart | SQLite singleton row | `test_final_block_does_not_re_fire_after_restart` |
| Disabled flag short-circuits cleanly | main.py loop guard | `test_main_weekly_loop_skips_when_disabled` |
| Day-of-week + same-day-re-fire guard | main.py loop guard | `test_main_weekly_loop_doesnt_re_fire_same_day` |
| parse_mode=None | `send_cohort_digest` dispatch | `test_send_cohort_digest_passes_parse_mode_none` |
| Chunking inherits weekly_digest helper | import + dispatch | `test_send_cohort_digest_reuses_split_for_telegram` |

---

## Commit sequence

5 commits, bisect-friendly. Slightly different from plan's 4 — D5 adds a 1-table migration:

1. `feat(config): cohort digest settings (cycle 5 commit 1/5)` — 7 fields + 6 default-value tests
2. `feat(db): cohort_digest_state singleton table for final-block-fired tracking (cycle 5 commit 2/5)` — schema + 2 helpers (read/stamp)
3. `feat(cohort_digest): stats + verdict classification (cycle 5 commit 3/5)` — `_compute_signal_cohort_stats` + `_classify_verdict` + 11 unit tests
4. `feat(cohort_digest): text builder + sign-flip + final-window block (cycle 5 commit 4/5)` — `build_cohort_digest` + `_detect_verdict_flip` + final-window state read/write + 8 tests
5. `feat(main): cohort digest weekly-loop hook + close backlog (cycle 5 commit 5/5)` — main.py wiring + 3 integration tests + `docs(backlog)` flip

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Helper-import coupling (D3 `_split_for_telegram`) | Low | Low | Promote to module-level public if a refactor needs it; current import path stable. Test reuse is the contract. |
| `cohort_digest_state` table migration on existing prod DB | Low | Medium | `CREATE TABLE IF NOT EXISTS` in `Database.initialize()` — idempotent. No data migration needed. |
| Final-block double-fire on first-deploy-AFTER-2026-06-08 | Low | Low | `last_final_block_fired_at IS NULL` on first deploy → block fires once → row stamped → never again. Pre-2026-06-08 deploys see no final block; row stays NULL. |
| Sunday weekly_digest + Monday cohort_digest both reference 7d window | None (refuted) | — | Different windows: weekly_digest reads `[end-7d, end)` ending Sunday; cohort_digest reads `[end-7d, end)` ending Monday — partial overlap is intentional cadence-separation. |
| Dashboard threshold retune after merge | Medium | Medium | Settings constants pulled into `.env`; one operator edit + restart reconciles both surfaces in lockstep. |
| Process restart on Sunday 23:55 loses `last_cohort_digest_date` | Medium | Low | Day-of-week + once-per-day gate handles same-day re-fire idempotently. Worst case: 1 duplicate digest on the day of restart. |

---

## Out of scope

- Backfill mode (`--backfill`) — plan §Out of scope; resurrect if 4-week run requires
- Daily-cadence cohort digest — backlog explicitly says weekly
- Per-cohort-type drill-down beyond enumerated 3 — dashboard handles ad-hoc
- Live-trading promotion automation — BL-055 gates this; design only RECOMMENDS

---

## Deployment verification (autonomous post-3-reviewer-fold)

Identical to plan §Deployment plus:

1. After merge + pull + restart on srilu, verify `CREATE TABLE cohort_digest_state` ran by `sqlite3 prod.db ".schema cohort_digest_state"` returning the row.
2. Smoke: `python -c "from scout.trading.cohort_digest import _classify_verdict; print(_classify_verdict(eN=15, fN=30, wrDelta=20.0, fPnl=300, ePnl=-250, signal_type='gainers_early', n_gate=10))"` returns `strong-pattern (exploratory)`.
3. Wait until next Monday for first digest; operator confirms TG receipt.
4. 2026-06-08 final-window: confirm decision-recommendation block present + `cohort_digest_state.last_final_block_fired_at IS NOT NULL` after.
