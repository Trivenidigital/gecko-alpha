**New primitives introduced:** Same set as `tasks/plan_live_eligible_weekly_digest.md` (post V27/V28 fold, commit `da16ebe`) PLUS V29/V30 design-fold additions — `scout/trading/cohort_digest.py` adds `_compute_all_cohorts_stats()` (V29 SHOULD-FIX) + `stamp_last_digest_date()` + `stamp_final_block_fired()` + `read_state()`; 8 Settings fields total (added `COHORT_DIGEST_HOUR: int = 9` per V29 MUST-FIX); `_run_feedback_schedulers` signature extends to `tuple[str, str, str]` with new `last_cohort_digest_date` parameter; new SQLite table `cohort_digest_state` (singleton, marker INTEGER PK + CHECK, seeded NULL+NULL); new SQLite partial index `idx_paper_trades_closed_at` (V30 MUST-FIX); structured log events identical except `cohort_digest_skipped_disabled` DEBUG added for observability symmetry.

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

### D1. Two SQL pairs per `build_cohort_digest` (V29 SHOULD-FIX fold — explicit two-window + helper naming)

`build_cohort_digest(db, end_date, settings)` runs the **TWO non-overlapping windows** to support sign-flip detection across week-N vs week-N-1:

- Window N: `[end_date - 7d, end_date)`
- Window N-1: `[end_date - 14d, end_date - 7d)`

For each window, query both cohorts. Total = **4 queries per digest**, not 4×|signals|.

Implementation factoring:

```python
# Public per-signal API (preserved for testability):
async def _compute_signal_cohort_stats(
    db, *, signal_type, start, end
) -> dict: ...

# NEW private helper (V29 SHOULD-FIX): one SQL pair for the whole window
async def _compute_all_cohorts_stats(
    db, *, start, end
) -> dict[str, dict]:
    """Returns {signal_type: stats_dict} for all _LIVE_ELIGIBLE_ENUMERATED_TYPES,
    using 2 SQL queries total (full + eligible). _compute_signal_cohort_stats
    is the test-only seam — production path uses _compute_all_cohorts_stats."""
```

Query shape (same for full + eligible — only the `AND would_be_live = 1` differs):

```sql
SELECT signal_type, COUNT(*) AS n,
       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
       COALESCE(SUM(pnl_usd), 0) AS pnl
FROM paper_trades
WHERE status != 'open'
  AND closed_at >= ? AND closed_at < ?
  AND signal_type IN (?, ?, ?)  -- _LIVE_ELIGIBLE_ENUMERATED_TYPES
GROUP BY signal_type;
```

`~ms total at srilu scale` claim depends on the new `idx_paper_trades_closed_at` partial index (V30 MUST-FIX, see D5b).

**New primitives updated:** `_compute_all_cohorts_stats` added.

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

### D4. `main.py` weekly-loop hook — extend `_run_feedback_schedulers` tuple-return (V29 MUST-FIX fold)

V29 caught the wiring mismatch: the loop is `_run_feedback_schedulers(db, settings, last_refresh_date, last_digest_date, now_local) -> tuple[str, str]` at `scout/main.py:249-330`. Three gates: `weekday == ... AND hour == ... AND last_digest_date != today_iso`. State threaded via `nonlocal` at main.py:1779/1852-1857. Uses NAIVE-LOCAL `now_local`, not `date.today()`.

**Revised hook integration** — extend the helper's signature to `tuple[str, str, str]`:

```python
async def _run_feedback_schedulers(
    db,
    settings,
    last_refresh_date: str,
    last_digest_date: str,
    last_cohort_digest_date: str,   # NEW
    now_local: datetime,
) -> tuple[str, str, str]:
    ...
    today_iso = now_local.strftime("%Y-%m-%d")
    ...
    # Cohort digest (COHORT_DIGEST_DAY_OF_WEEK + _HOUR local, mirrors weekly_digest)
    if (
        settings.COHORT_DIGEST_ENABLED
        and now_local.weekday() == settings.COHORT_DIGEST_DAY_OF_WEEK
        and now_local.hour == settings.COHORT_DIGEST_HOUR     # NEW Settings field
        and last_cohort_digest_date != today_iso
    ):
        try:
            await _cohort_digest.send_cohort_digest(db, settings)
            last_cohort_digest_date = today_iso
            await _cohort_digest.stamp_last_digest_date(db, today_iso)  # V29#2 fold (D5 update)
        except Exception:
            logger.exception("cohort_digest_loop_error")
    return last_refresh_date, last_digest_date, last_cohort_digest_date
```

Caller at main.py:1779 gets new `nonlocal last_cohort_digest_date = ""` declared at module top (init from `cohort_digest_state.last_digest_date` if present so a same-day restart doesn't re-fire — V29 MUST-FIX #2). Tuple-unpack:

```python
last_combo_refresh_date, last_weekly_digest_date, last_cohort_digest_date = (
    await _run_feedback_schedulers(
        db, settings,
        last_combo_refresh_date,
        last_weekly_digest_date,
        last_cohort_digest_date,
        now_local,
    )
)
```

New Settings field added: `COHORT_DIGEST_HOUR: int = 9` (default 9am local; operator-tunable). Now 8 Settings fields total, not 7 — primitives list updated.

Per V28 SHOULD-FIX final-window fallback: FINAL-block logic lives INSIDE `build_cohort_digest`. State updates after a successful TG dispatch, not on attempted-send.

### D5. Persistent state — `cohort_digest_state` singleton (V29 MUST-FIX + V30 MUST-FIX folds)

V29 walk-through proved in-process `last_cohort_digest_date` re-fires same-day on restart (gate is what causes re-fire, not what prevents it). V30 caught the missing initial-row seed: a plain `UPDATE` on an empty singleton table silently no-ops. **Both fold into ONE row carrying BOTH sentinels.**

Schema:

```sql
CREATE TABLE IF NOT EXISTS cohort_digest_state (
    marker INTEGER PRIMARY KEY DEFAULT 1,
    last_digest_date TEXT,                  -- V29 fold: persisted, survives restart
    last_final_block_fired_at TEXT,
    CHECK (marker = 1)
);
-- Seed empty NULL row immediately so write paths can UPDATE safely:
INSERT OR IGNORE INTO cohort_digest_state (marker, last_digest_date, last_final_block_fired_at)
VALUES (1, NULL, NULL);
```

Both the CREATE and the seed-INSERT run in the same migration step (commit 2/5). CHECK enforces singleton row; if a buggy writer attempts `INSERT (marker, ...) VALUES (2, ...)` it raises `IntegrityError`. V30 SHOULD-FIX format consistency: writers and the digest use `datetime.now(timezone.utc).isoformat()`; format invariant locked in by `test_window_string_format_matches_writer_format`.

**Write SQL (V30 MUST-FIX explicit):**

```python
async def stamp_last_digest_date(db: Database, date_iso: str) -> None:
    await db._conn.execute(
        "INSERT OR REPLACE INTO cohort_digest_state "
        "(marker, last_digest_date, last_final_block_fired_at) "
        "VALUES (1, ?, (SELECT last_final_block_fired_at FROM cohort_digest_state WHERE marker = 1))",
        (date_iso,),
    )
    await db._conn.commit()

async def stamp_final_block_fired(db: Database, ts_iso: str) -> None:
    await db._conn.execute(
        "INSERT OR REPLACE INTO cohort_digest_state "
        "(marker, last_digest_date, last_final_block_fired_at) "
        "VALUES (1, (SELECT last_digest_date FROM cohort_digest_state WHERE marker = 1), ?)",
        (ts_iso,),
    )
    await db._conn.commit()

async def read_state(db: Database) -> dict:
    cur = await db._conn.execute(
        "SELECT last_digest_date, last_final_block_fired_at FROM cohort_digest_state WHERE marker = 1"
    )
    row = await cur.fetchone()
    if row is None:
        return {"last_digest_date": None, "last_final_block_fired_at": None}
    return {"last_digest_date": row[0], "last_final_block_fired_at": row[1]}
```

`INSERT OR REPLACE` semantics: with `marker INTEGER PRIMARY KEY`, the singleton row is preserved; the sub-SELECTs read the other field so the unrelated column isn't clobbered to NULL on partial stamp. Test: `test_stamp_last_digest_date_preserves_final_block_field`.

On main.py startup, `last_cohort_digest_date` is initialized from `read_state(db)["last_digest_date"] or ""` so a restart on the SAME day does NOT re-fire.

### D5b. New paper_trades index migration (V30 MUST-FIX fold)

V30 caught the missing index: `paper_trades` has indexes on `status`, `opened_at`, `signal_type`, and `(would_be_live, status)` but NOT `closed_at`. The digest's query plan would table-scan-filter under `idx_paper_trades_signal`. Per `feedback_ddl_before_alter.md` precedent, the index lives in a MIGRATION step (not `_create_tables` — `CREATE TABLE IF NOT EXISTS` is a no-op for existing tables, so an index defined there wouldn't apply to prod):

```sql
-- Migration step (commit 2/5, runs alongside cohort_digest_state CREATE):
CREATE INDEX IF NOT EXISTS idx_paper_trades_closed_at
  ON paper_trades(closed_at)
  WHERE closed_at IS NOT NULL;
```

Partial-index filter on `closed_at IS NOT NULL` skips open trades (which have `closed_at = NULL`); reduces index size by ~60-80% at production scale. Test (commit 2 of 5): `test_paper_trades_closed_at_index_exists_post_migration` queries `sqlite_master`.

### D6. Test fixtures + Windows-OPENSSL deferral

Existing `tests/conftest.py` has `token_factory`. We need a `paper_trade_factory` for seeding rows with arbitrary `signal_type / status / closed_at / pnl_usd / would_be_live`. Check if one exists; if not, build a minimal one within `tests/test_cohort_digest.py` itself (avoid plumbing through conftest unless ≥2 tests need it).

Per memory `reference_windows_openssl_workaround.md`, local pytest hangs on scout.main imports. Same VPS-deferral pattern as cycles 1-4.

### D7. Log emit timing

`_run_hourly_maintenance` already taught the pattern: emit AFTER the work, with the data payload spread. Same here — `cohort_digest_sent` emits AFTER successful `send_telegram_message`, with `bytes=len(text)` in the payload.

### D8. Telegram parse_mode (V29 SHOULD-FIX fold — verified, not assumed)

Per CLAUDE.md "What NOT To Do" + memory `feedback_class_3_silent_failure_rendering_corruption.md`: digest contains signal names with `_` (gainers_early, volume_spike, chain_completed). MarkdownV1 parses `_` as italics → mangling.

**Confirmed at `scout/trading/weekly_digest.py:336`:** `await alerter.send_telegram_message(chunk, session, settings, parse_mode=None)` — passed EXPLICITLY at the call site. `send_cohort_digest` mirrors verbatim: every call to `alerter.send_telegram_message` carries `parse_mode=None` keyword arg, locked-in by `test_send_cohort_digest_passes_parse_mode_none`.

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

5 commits, bisect-friendly:

1. `feat(config): cohort digest settings (cycle 5 commit 1/5)` — 8 fields + 7 default-value tests
2. `feat(db): cohort_digest_state singleton + paper_trades.closed_at index migration (cycle 5 commit 2/5)` — schema + seed-INSERT-OR-IGNORE + idx_paper_trades_closed_at partial index + read_state/stamp_last_digest_date/stamp_final_block_fired helpers + 5 tests (state read on empty, INSERT-OR-REPLACE preserves other field, idx exists, IntegrityError on marker=2, NULL initial state)
3. `feat(cohort_digest): stats + verdict classification (cycle 5 commit 3/5)` — `_compute_signal_cohort_stats` + `_compute_all_cohorts_stats` + `_classify_verdict` + 13 unit tests
4. `feat(cohort_digest): text builder + sign-flip + final-window block (cycle 5 commit 4/5)` — `build_cohort_digest` (2-window math + final-block) + `send_cohort_digest` + `_detect_verdict_flip` (rolled-up + n-gate-guard) + 10 tests including `test_window_string_format_matches_writer_format`
5. `feat(main): cohort digest weekly-loop hook + close backlog (cycle 5 commit 5/5)` — `_run_feedback_schedulers` signature extension + main.py:1779 tuple-unpack + init-from-cohort_digest_state on startup + 4 integration tests + `docs(backlog)` flip + memory checkpoint

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Helper-import coupling (D3 `_split_for_telegram`) | Low | Low | Promote to module-level public if a refactor needs it; current import path stable. Test reuse is the contract. |
| `cohort_digest_state` table migration on existing prod DB | Low | Medium | `CREATE TABLE IF NOT EXISTS` in `Database.initialize()` — idempotent. No data migration needed. |
| Final-block double-fire on first-deploy-AFTER-2026-06-08 | Low | Low | `last_final_block_fired_at IS NULL` on first deploy → block fires once → row stamped → never again. Pre-2026-06-08 deploys see no final block; row stays NULL. |
| Sunday weekly_digest + Monday cohort_digest both reference 7d window | None (refuted) | — | Different windows: weekly_digest reads `[end-7d, end)` ending Sunday; cohort_digest reads `[end-7d, end)` ending Monday — partial overlap is intentional cadence-separation. |
| Dashboard threshold retune after merge | Medium | Medium | Settings constants pulled into `.env`; one operator edit + restart reconciles both surfaces in lockstep. |
| Process restart same day as digest firing → re-fire | Refuted (V29 MUST-FIX fold) | — | `last_cohort_digest_date` is now persisted to `cohort_digest_state.last_digest_date`. main.py:1779 init reads from DB on startup. Same-day restart sees `last_cohort_digest_date == today_iso` and skips. |
| Singleton write SQL silently no-ops on empty table (V30 MUST-FIX) | Refuted | — | Migration step (commit 2/5) seeds the row via `INSERT OR IGNORE`; `stamp_*` helpers use `INSERT OR REPLACE` with sub-SELECTs to preserve the other column. |
| Missing `closed_at` index → query plan table-scans (V30 MUST-FIX) | Refuted | — | Commit 2/5 adds partial index `idx_paper_trades_closed_at WHERE closed_at IS NOT NULL`. |
| Lexicographic `closed_at` compare fails on bare format (V30 SHOULD-FIX) | Low | Medium | Writers at `scout/trading/paper.py:438` + `scout/live/reconciliation.py:62` + `scout/live/shadow_evaluator.py:57` all use `datetime.now(timezone.utc).isoformat()` → format-stable. Test `test_window_string_format_matches_writer_format` locks the invariant byte-for-byte. |

---

## Out of scope

- Backfill mode (`--backfill`) — plan §Out of scope; resurrect if 4-week run requires
- Daily-cadence cohort digest — backlog explicitly says weekly
- Per-cohort-type drill-down beyond enumerated 3 — dashboard handles ad-hoc
- Live-trading promotion automation — BL-055 gates this; design only RECOMMENDS

---

## Deployment verification (autonomous post-3-reviewer-fold)

### Pre-ship prod-state checks on srilu (V30 PROD-STATE-CHECKLIST fold):

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/prod.db <<EOF
SELECT COUNT(*) FROM paper_trades;
SELECT MIN(closed_at), MAX(closed_at) FROM paper_trades WHERE status != \"open\";
.indexes paper_trades
EOF'
```

Confirm: row count reasonable; `closed_at` values are TZ-suffixed ISO across the corpus (no bare `YYYY-MM-DD HH:MM:SS` from a legacy writer); no pre-existing `idx_paper_trades_closed_at`.

### Post-ship verification (autonomous):

1. `find . -name __pycache__ -exec rm -rf {} +` + restart + `systemctl is-active gecko-pipeline`.
2. `sqlite3 prod.db ".schema cohort_digest_state"` returns the row with CHECK constraint.
3. `sqlite3 prod.db "SELECT * FROM cohort_digest_state"` shows ONE seeded row with NULL+NULL.
4. `sqlite3 prod.db ".indexes paper_trades"` includes `idx_paper_trades_closed_at`.
5. Smoke: `python -c "from scout.trading.cohort_digest import _classify_verdict; print(_classify_verdict(eN=15, fN=30, wrDelta=20.0, fPnl=300, ePnl=-250, signal_type='gainers_early', n_gate=10, strong_wr_gap_pp=15.0, strong_pnl_floor_usd=200.0, moderate_wr_gap_pp=5.0))"` → `strong-pattern (exploratory)`.

### Soak / final-window:

6. First firing: next Monday at `COHORT_DIGEST_HOUR` local; operator confirms TG receipt.
7. After firing, `SELECT last_digest_date FROM cohort_digest_state` returns today's date.
8. Restart service same Monday — verify NO duplicate digest fires (`last_digest_date` re-read from DB on startup).
9. Monday 2026-06-08 (or first Monday after, per V28 fallback): confirm final decision-recommendation block + `last_final_block_fired_at IS NOT NULL`.
10. V30 WAL-pressure follow-up: during first Monday firing, `journalctl -u gecko-pipeline --since "10 min ago" -p debug | grep sqlite_wal_bloat_observed` — if it fires, file follow-up to add `PRAGMA wal_checkpoint(PASSIVE)` post-send.
