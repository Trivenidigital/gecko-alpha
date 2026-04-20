# BL-050: Paper-trade Edge-Detection — Design Spec (v2)

**Date:** 2026-04-19
**Author:** overnight build
**Status:** v2 — addresses findings from parallel architecture/silent-failure/test-coverage reviews
**Revision log:** v1 had unpinned upsert ordering, weak atomicity claim, naive timestamp SQL, missing boundary/restart/blocked-but-upsert tests, no error policy.

## Problem

The paper-trading engine opens a new `first_signal` trade every time a token is observed with `quant_score > 0` AND non-empty `signals_fired` AND not in the 48h cooldown. This is *current-state* membership, not *transition into* the qualifying set. On every process restart, every currently-qualifying token whose last trade was >48h ago (or never existed) is treated as a fresh signal → restart burst. PR #26 patched the symptom (10-pos cap + 3-min warmup) but the root cause is unfixed.

**Scope of bug:** Only `first_signal` in `scout/trading/signals.py:195-255`. `narrative_prediction` is structurally immune — predictions are created per narrative cycle as fresh `NarrativePrediction` objects from a live API pull, *and* `engine.open_trade`'s DB-persisted 48h cooldown serves as its de-facto transition gate. `first_signal` was designed for continuous qualification monitoring, which is exactly why it lacks an equivalent gate; this spec adds one.

## Goal

A token moving *from* outside the qualifying set *into* it must fire at most one `first_signal` trade. A token continuously qualifying across scans, or qualifying before a restart, must NOT fire. A token that qualified, exited for at least `QUALIFIER_EXIT_GRACE_HOURS`, then re-entered is a fresh transition and may fire again.

## Non-goals

- Does not touch `narrative_prediction`, `volume_spike`, `gainers_early`, `losers_contrarian`, `trending_catch`, `chain_completed`.
- Does not remove warmup, open-position dedup, 48h cooldown, or max-open cap. They become defence-in-depth.
- Does not change trade sizing, TP/SL, or exit rules.
- Does not backfill historical qualifier state. First scan after deploy will see everything as a transition; the existing 48h cooldown catches prior-traded tokens, and the max-open cap bounds the worst case.

## Design

### Schema (`scout/db.py`)

```sql
CREATE TABLE IF NOT EXISTS signal_qualifier_state (
    signal_type         TEXT NOT NULL,
    token_id            TEXT NOT NULL,
    first_qualified_at  TEXT NOT NULL,
    last_qualified_at   TEXT NOT NULL,
    PRIMARY KEY (signal_type, token_id)
);
CREATE INDEX IF NOT EXISTS idx_sqs_last_qualified_at
    ON signal_qualifier_state (last_qualified_at);
```

Timestamps stored as ISO-8601 strings with explicit UTC offset (`datetime.now(timezone.utc).isoformat()`). **Critical: all SQL comparisons against these columns MUST wrap both sides in `datetime(...)` per PR #24 precedent** (`WHERE datetime(last_qualified_at) < datetime(:threshold)`). The index on `last_qualified_at` may or may not be used through the `datetime()` wrapper depending on SQLite version; the table is bounded to ~1 week of retention so a full scan for pruning is acceptable if the index isn't engaged.

### Module (`scout/trading/qualifier_state.py`, new)

Exposes two async functions:

```python
async def classify_transitions(
    db: Database,
    *,
    signal_type: str,
    current_token_ids: set[str],
    now: datetime,
    exit_grace_hours: int,
) -> set[str]:
    """Classify current_token_ids into transitions (return) and continuations (not returned).

    Semantics: upserts ALL current_token_ids with last_qualified_at=now, regardless of
    whether the token is a transition or continuation. For rows that did not previously
    exist, sets first_qualified_at=now. For rows whose prior last_qualified_at is older
    than exit_grace_hours, resets first_qualified_at=now (re-entry). Returns the subset
    that were transitions.

    Boundary convention: a token whose prior last_qualified_at is EXACTLY (now - exit_grace_hours)
    is NOT a transition (the grace window is inclusive of the boundary). A token whose prior
    last_qualified_at is (now - exit_grace_hours - 1 second) IS a transition.

    Early-return: if current_token_ids is empty, returns set() without opening a transaction.

    Atomicity: acquires db._txn_lock for the duration of the read+write. This is the same
    pattern used by scout/trading/suppression.py and scout/trading/combo_refresh.py (PR #29).
    Two concurrent calls with overlapping token sets are serialized; BEGIN IMMEDIATE is NOT
    required because aiosqlite serializes all writes through its worker thread and _txn_lock
    provides the asyncio-layer serialization.

    Error policy: on any aiosqlite error, raises the original exception. Caller is
    REQUIRED to handle failure fail-closed (log + skip trades for this cycle) — see
    integration snippet below. A try/except returning set() would cause a silent data
    loss of every transition; a try/except returning current_token_ids would re-introduce
    the restart-burst bug. Both are explicitly forbidden.
    """

async def prune_stale_qualifiers(
    db: Database,
    *,
    now: datetime,
    retention_hours: int,
) -> int:
    """Delete rows where datetime(last_qualified_at) < datetime(now - retention_hours).

    Returns the number of rows deleted.

    Acquires db._txn_lock. Callers MUST wrap invocation in try/except and log prune
    failures — a prune failure is NOT cycle-fatal (the table is bounded by retention_hours
    only; one failed prune is survivable). Heartbeat is REQUIRED to surface consecutive
    prune-failure count; >5 consecutive failures must page.

    Special cases:
    - retention_hours <= 0: raises ValueError (configuration error).
    - If no rows match, returns 0 without opening a write transaction (read-only SELECT COUNT first).
    """
```

### Integration (`scout/trading/signals.py::trade_first_signals`)

Current flow (from exploration):
```python
for token in all_scored_tokens:
    if token.quant_score > 0 and token.signals_fired:
        await engine.open_trade(...)
```

New flow — **mandatory error handling and token_id dedup**:
```python
qualifying = [t for t in all_scored_tokens if t.quant_score > 0 and t.signals_fired]
current_ids = {t.token_id for t in qualifying}

try:
    transitions = await classify_transitions(
        db,
        signal_type="first_signal",
        current_token_ids=current_ids,
        now=datetime.now(timezone.utc),
        exit_grace_hours=settings.QUALIFIER_EXIT_GRACE_HOURS,
    )
except Exception as exc:
    log.error(
        "qualifier_classify_failed",
        err_id="QUALIFIER_CLASSIFY_FAIL",
        exc_type=type(exc).__name__,
        exc_info=True,
    )
    return  # fail-closed: skip all first_signal trades this cycle

seen: set[str] = set()
for token in qualifying:
    if token.token_id not in transitions:
        continue
    if token.token_id in seen:  # multi-ingestor dup dedup
        continue
    seen.add(token.token_id)
    trade_id = await engine.open_trade(...)
    if trade_id is None:
        log.info(
            "qualifier_transition_skipped",
            signal_type="first_signal",
            token_id=token.token_id,
            reason="open_trade_returned_none",
        )
```

**Ordering decision (pinned):** `classify_transitions` upserts BEFORE `open_trade` is attempted. If `open_trade` returns `None` or raises for a transition token (cooldown block, max-open block, warmup block, no price, stale price, or any other reason), the row is already marked and the trade is lost for the current transition. This is **accepted and explicit** because:
- Paper trades are not financially material; losing one per blocked-transition is bounded.
- The alternative (upsert-on-success) creates a new bug: a token blocked by max-open cap stays "unmarked" and re-fires every scan forever.
- The `qualifier_transition_skipped` log event makes the loss observable.

### Pruning invocation (`scout/main.py`)

In `_pipeline_loop()`, track a counter; every `QUALIFIER_PRUNE_EVERY_CYCLES` iterations call `prune_stale_qualifiers` wrapped in try/except. Failures logged with `err_id="QUALIFIER_PRUNE_FAIL"`; increment a consecutive-failure counter; heartbeat exposes the counter.

### Config (`scout/config.py`)

```python
QUALIFIER_EXIT_GRACE_HOURS: int = 48
QUALIFIER_PRUNE_RETENTION_HOURS: int = 168   # 7 days
QUALIFIER_PRUNE_EVERY_CYCLES: int = 100
```

**Cross-field validation** (Pydantic `model_validator`, REQUIRED to prevent silent prune-vs-classify races):

```python
@model_validator(mode="after")
def _check_retention_gt_grace(self) -> "Settings":
    if self.QUALIFIER_PRUNE_RETENTION_HOURS <= self.QUALIFIER_EXIT_GRACE_HOURS:
        raise ValueError(
            "QUALIFIER_PRUNE_RETENTION_HOURS must be strictly greater than "
            "QUALIFIER_EXIT_GRACE_HOURS to prevent pruning rows that classify "
            "still needs for re-entry detection."
        )
    return self
```

### Why `_txn_lock` (not a fresh `BEGIN IMMEDIATE`)

`_txn_lock` exists on `Database` and is actively used by `scout/trading/combo_refresh.py:29` and `scout/trading/suppression.py:103` for multi-statement atomic sequences. It is the established pattern for read-then-write atomicity in this codebase. Adding `BEGIN IMMEDIATE` on top would work but duplicates protection already provided by `_txn_lock` + aiosqlite's single-connection worker thread.

### Observability

Three structured log events:
- `qualifier_transition_fired` — INFO, emitted per transition before `open_trade` is attempted. Fields: `signal_type`, `token_id`, `prior_last_qualified_at`, `elapsed_since_prior_hours` (or `first_seen=true` if no prior row).
- `qualifier_transition_skipped` — INFO, emitted when `open_trade` returns None for a transition. Fields: `signal_type`, `token_id`, `reason`.
- `qualifier_classify_failed` / `qualifier_prune_failed` — ERROR, err_id set for grep.

Heartbeat (`scout/main.py`) adds per-cycle counters: `qualifier_transitions`, `qualifier_skips`, `qualifier_prune_consecutive_failures`.

## Tests

### Unit tests (`tests/test_trading_qualifier_state.py`, new — 13 tests)

1. `test_classify_returns_all_tokens_on_first_call`
2. `test_classify_returns_empty_when_all_tokens_already_present` — pre-seeded rows, asserts empty return + `last_qualified_at` bumped.
3. `test_classify_returns_only_new_token`
4. `test_re_entry_outside_grace_counts_as_transition` — prior `now - 49h`, grace=48 → transition.
5. `test_re_entry_inside_grace_is_not_transition` — prior `now - 47h`, grace=48 → continuation.
6. `test_re_entry_exactly_at_grace_boundary_is_not_transition` — prior exactly `now - 48h`, grace=48 → continuation (inclusive semantics pinned).
7. `test_re_entry_one_second_past_grace_is_transition` — prior `now - 48h - 1s`, grace=48 → transition.
8. `test_empty_current_ids_returns_empty_without_transaction` — monkey-patch `_txn_lock.acquire` to raise; call with empty set; assert returns `set()` without the lock being acquired.
9. `test_classify_raises_on_aiosqlite_error` — monkey-patch db to raise `OperationalError` inside the transaction; assert the error propagates (does NOT return set()).
10. `test_different_signal_types_do_not_interfere` — same token under `first_signal` and `other_signal`, independent rows, independent classifications.
11. `test_prune_stale_removes_old_rows_only`
12. `test_prune_retention_zero_raises_value_error`
13. `test_prune_returns_zero_when_no_stale_rows_without_write_transaction`

### Integration tests (`tests/test_trading_edge_detection.py`, new — 6 tests)

14. `test_restart_does_not_replay_qualifying_tokens` — cycle N with 5 qualifying tokens, `Database.close()`, re-instantiate Database (same file), cycle N+1 with same 5 tokens → zero `open_trade` calls.
15. `test_fresh_transition_opens_exactly_one_trade` — cycle N: token A qualifying. Cycle N+1: tokens A+B qualifying → exactly one `open_trade` call, for B.
16. `test_restart_with_re_entry_during_downtime` — pre-seed row with `last_qualified_at` aged past grace before restart; first post-restart scan with same token → fires exactly once.
17. `test_transition_blocked_by_cooldown_still_upserts` — seed `paper_trades` row for token A within 48h; next scan A qualifies for first time → classify marks A's row (first_qualified_at=now), but `open_trade` returns None (blocked by cooldown) → next scan A still present: no new trade, no re-classification as transition. Pins the upsert-always semantics.
18. `test_transition_blocked_by_max_open_still_upserts` — fill 10 open positions, new token A qualifies → transition classified + row upserted, `open_trade` returns None (max-open hit), log event `qualifier_transition_skipped` with `reason=max_open_hit`.
19. `test_config_rejects_retention_le_grace` — Settings(QUALIFIER_EXIT_GRACE_HOURS=48, QUALIFIER_PRUNE_RETENTION_HOURS=48) raises ValueError.

### Removed from v1 test list

- `test_classify_is_atomic_against_concurrent_call` — DROPPED. `asyncio.gather` on a single `Database` cannot actually race under aiosqlite's single-connection worker thread; the test would pass trivially and prove nothing. Real concurrency would require two Database instances, which is not a production deployment pattern (the pipeline uses exactly one). The atomicity claim is now explicitly scoped to "serialized by `_txn_lock` + aiosqlite worker thread, not BEGIN IMMEDIATE."

## Rollout

No feature flag. The new gate is strictly more conservative than the existing one. Deploy: DB migration auto-runs on `Database.initialize()`; empty table created on first start; idempotent. No VPS changes beyond merging the PR and restarting the pipeline service.

## Known limitations

1. **Cold-start burst** on fresh deploy: bounded by max-open cap. Acceptable; fresh deploys are rare and the bug this fixes is restart-burst, not fresh-deploy burst.
2. **Trade loss on blocked transitions** — accepted and logged. See "Ordering decision (pinned)" above.
3. **Crash mid-function** — if classify_transitions commits the upsert but the process dies before `open_trade` is invoked, the trade is lost (next scan classifies as continuation). Bounded: paper trades are not financially material; if this moves to live trading, the fix is to invert the write order or use an outbox pattern.
4. **Table growth if prune persistently fails** — heartbeat pages on >5 consecutive failures; retention=168h caps steady-state size at <1MB per signal_type.

## Acceptance criteria

- All 19 tests pass (13 unit + 6 integration).
- Full test suite (`uv run pytest`) passes with no new failures.
- Restart acceptance: with 20 tokens currently qualifying, zero new paper trades open after a restart.
- Transition acceptance: a new token entering the qualifier set opens exactly one trade.
- Re-entry acceptance: a token that was in the set, dropped out for >48h, re-entered → opens exactly one trade.
- Observability: heartbeat shows `qualifier_transitions` and `qualifier_skips` counters per cycle.
- Config validation: misconfigured retention vs grace raises ValueError at startup.
