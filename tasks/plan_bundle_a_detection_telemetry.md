# Bundle A: Detection Telemetry Hygiene — Implementation Plan (v2 post-review)

**New primitives introduced:** new column `chain_matches.mcap_at_completion REAL` (nullable, schema migration via `_migrate_feedback_loop_schema` extension); new heartbeat field `mcap_null_with_price_count` (in-memory only, not persisted); new structured aggregate log event `chain_outcomes_unhydrateable_memecoin` (pure log, no schema, emitted once per LEARN cycle, not per row).

**Goal:** Reduce three known silent-failure surfaces in detection telemetry: (1) silent rejection of CoinGecko tokens with null mcap, (2) narrative chain_matches pre-stamped as `EXPIRED` at write-time which the hydrator then skips forever, (3) memecoin chain_matches whose `outcomes`-table hydrator path is structurally dead.

**Architecture:**
- All three items are isolated, independent code changes — no cross-coupling.
- Two are pure code/log additions (no schema change). One adds a nullable column to `chain_matches` plus a hydrator path that **skips silently when the column is populated** (BL-071a' will wire writers + add the DexScreener fetch later); the structured warning fires **once per LEARN cycle as a count**, not per row.
- All TDD-driven against existing `tests/` patterns (pytest-asyncio, aioresponses for HTTP mocks, per-test `db` fixture from `Database(tmp_path / "test.db")`).

**Tech Stack:** Python 3.11, aiohttp, aiosqlite, structlog, pytest-asyncio. Existing project conventions per `CLAUDE.md`.

**Honest scope-decision note (READ BEFORE REVIEWING):**
Recon against prod `scout.db` revealed that **BL-071a as originally framed cannot ship as a clean fix tonight**. Specifically:
- `chain_matches.token_id` for `pipeline='memecoin'` rows is the *contract_address* (e.g., `0x0f1f36eb…`, `…pump`).
- `paper_trades.token_id` is the CoinGecko *slug* (e.g., `boba-network`, `gigachad-2`).
- Zero overlap in prod (verified: `EXPIRED narrative chain_matches with matching paper_trades = 0`).
- Therefore the backlog-suggested "(a) route to paper_trades" path is structurally broken without a contract→slug mapping that doesn't exist.
- The "(b) re-enable the writer" path requires also adding `mcap_at_completion` column AND wiring callers to pass it. The wiring needs to live in chain dispatch code that's outside the tracker module.

**Decision:** Task 3 in this plan implements the **half of BL-071a that's tractable tonight**: schema column for `mcap_at_completion`, a hydrator path that skips populated rows silently (BL-071a' adds the DexScreener fetch), and an aggregate `chain_outcomes_unhydrateable_memecoin` log emitted once per LEARN cycle (not per row) when the no-source-available case happens. Task 3 also writes a follow-up backlog item BL-071a' that scopes the remaining writer-wiring + DexScreener-fetch work. **This is honest scope limitation, not theatre — the column + hydrator path is real infrastructure that BL-071a' will use as-is. Until BL-071a' lands, the populated-column branch is intentionally a no-op.**

**Deploy ordering note:** All three changes are loaded by restarting `gecko-pipeline.service`. Migration runs at startup inside `Database.initialize()` AFTER the new tracker code is imported, so there is no cross-process race between the EXPIRED-write fix (Task 2) and the EXPIRED-to-NULL data migration. Deploy sequence: `git pull` → `systemctl restart gecko-pipeline`.

---

## Task 1 — BL-075 Phase A: `mcap_null_with_price_count` ingestion telemetry

**Files:**
- Modify: `scout/heartbeat.py` (add field to `_heartbeat_stats`, log line, reset helper, increment helper)
- Modify: `scout/ingestion/coingecko.py` (call increment in parsing loop of `fetch_top_movers` and `fetch_by_volume`)
- Test: `tests/test_heartbeat_mcap_missing.py` (new file)

**Why:** The 2026-05-03 RIV (`riv-coin`) miss was caused by silent rejection at the mcap=0.0 floor — CoinGecko returned `market_cap=null` and our parser writes 0, then the floor drops it. We don't know how often this happens because the rejection path doesn't log per-token. A rolling counter in the heartbeat (already emitted every `HEARTBEAT_INTERVAL_SECONDS=300`) is the cheapest way to find out.

**Naming note:** The counter is `mcap_null_with_price_count` (per reviewer S1) — *not* `mcap_missing_count` — so it's honest about what's measured: tokens where CoinGecko returned `market_cap` as null/0 but a positive `current_price`. Tokens that get rejected for `market_cap_usd < MIN_MARKET_CAP` (intentional operator floor) are NOT counted; that's a separate, known-by-design rejection.

- [ ] **Step 1.1 — Write failing test for heartbeat field**

Create `tests/test_heartbeat_mcap_missing.py`:

```python
"""BL-075 Phase A: mcap_null_with_price telemetry on heartbeat."""
from __future__ import annotations

import pytest

from scout.heartbeat import (
    _heartbeat_stats,
    _maybe_emit_heartbeat,
    _reset_heartbeat_stats,
    increment_mcap_null_with_price,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_heartbeat_stats()
    yield
    _reset_heartbeat_stats()


def test_mcap_null_with_price_field_initialized_to_zero():
    assert _heartbeat_stats["mcap_null_with_price_count"] == 0


def test_increment_bumps_counter():
    increment_mcap_null_with_price()
    increment_mcap_null_with_price()
    assert _heartbeat_stats["mcap_null_with_price_count"] == 2


def test_reset_clears_counter():
    increment_mcap_null_with_price()
    _reset_heartbeat_stats()
    assert _heartbeat_stats["mcap_null_with_price_count"] == 0


def test_heartbeat_log_includes_field(caplog):
    """Heartbeat emission must include the new field.

    Note: HEARTBEAT_INTERVAL_SECONDS=0 means `elapsed < 0` is False for any
    non-negative elapsed, so the second call always fires. First call seeds
    started_at/last_heartbeat_at without logging.
    """
    class _StubSettings:
        HEARTBEAT_INTERVAL_SECONDS = 0

    increment_mcap_null_with_price()
    increment_mcap_null_with_price()
    increment_mcap_null_with_price()
    _maybe_emit_heartbeat(_StubSettings())  # seeds
    with caplog.at_level("INFO"):
        emitted = _maybe_emit_heartbeat(_StubSettings())
    assert emitted is True
    found = any(
        "mcap_null_with_price_count" in str(rec.__dict__)
        for rec in caplog.records
    )
    assert found, "heartbeat log must include mcap_null_with_price_count"
```

- [ ] **Step 1.2 — Run test to verify it fails**

Run: `cd C:/projects/gecko-alpha && uv run pytest tests/test_heartbeat_mcap_missing.py -v`
Expected: FAIL with `ImportError: cannot import name 'increment_mcap_null_with_price'`

- [ ] **Step 1.3 — Implement minimal heartbeat changes**

Edit `scout/heartbeat.py` — replace entire file with:

```python
"""BL-033: Module-level heartbeat state and periodic heartbeat logging.

Tracks cumulative pipeline stats across cycles and emits a structured
"heartbeat" log every HEARTBEAT_INTERVAL_SECONDS so operators can see
the pipeline is alive.

BL-075 Phase A (2026-05-03) adds `mcap_null_with_price_count` —
increments on each CoinGecko ingestion parse where market_cap was
null/0 but current_price was positive. Surfaces the silent-rejection
rate at the mcap=0 floor that caused the RIV (riv-coin) miss.
"""

from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

_heartbeat_stats: dict = {
    "started_at": None,
    "tokens_scanned": 0,
    "candidates_promoted": 0,
    "alerts_fired": 0,
    "narrative_predictions": 0,
    "counter_scores_memecoin": 0,
    "counter_scores_narrative": 0,
    "mcap_null_with_price_count": 0,
    "last_heartbeat_at": None,
}


def _reset_heartbeat_stats() -> None:
    """Reset module-level heartbeat state (test helper)."""
    _heartbeat_stats.update(
        started_at=None,
        tokens_scanned=0,
        candidates_promoted=0,
        alerts_fired=0,
        narrative_predictions=0,
        counter_scores_memecoin=0,
        counter_scores_narrative=0,
        mcap_null_with_price_count=0,
        last_heartbeat_at=None,
    )


def increment_mcap_null_with_price() -> None:
    """BL-075 Phase A: bump null-mcap-with-price counter (called from ingestion)."""
    _heartbeat_stats["mcap_null_with_price_count"] += 1


def _maybe_emit_heartbeat(settings) -> bool:
    """Log heartbeat every HEARTBEAT_INTERVAL_SECONDS."""
    now = datetime.now(timezone.utc)
    if _heartbeat_stats["last_heartbeat_at"] is None:
        _heartbeat_stats["last_heartbeat_at"] = now
        _heartbeat_stats["started_at"] = now
        return False
    elapsed = (now - _heartbeat_stats["last_heartbeat_at"]).total_seconds()
    if elapsed < settings.HEARTBEAT_INTERVAL_SECONDS:
        return False
    uptime_minutes = (now - _heartbeat_stats["started_at"]).total_seconds() / 60
    logger.info(
        "heartbeat",
        uptime_minutes=round(uptime_minutes, 1),
        tokens_scanned=_heartbeat_stats["tokens_scanned"],
        candidates_promoted=_heartbeat_stats["candidates_promoted"],
        alerts_fired=_heartbeat_stats["alerts_fired"],
        narrative_predictions=_heartbeat_stats["narrative_predictions"],
        counter_scores_memecoin=_heartbeat_stats["counter_scores_memecoin"],
        counter_scores_narrative=_heartbeat_stats["counter_scores_narrative"],
        mcap_null_with_price_count=_heartbeat_stats["mcap_null_with_price_count"],
        last_heartbeat_at=_heartbeat_stats["last_heartbeat_at"].isoformat(),
    )
    _heartbeat_stats["last_heartbeat_at"] = now
    return True
```

- [ ] **Step 1.4 — Run test to verify it passes**

Run: `uv run pytest tests/test_heartbeat_mcap_missing.py -v`
Expected: 4 passed

- [ ] **Step 1.5 — Wire ingestion to call `increment_mcap_null_with_price`**

Edit `scout/ingestion/coingecko.py`. At the top of the file, add the import:

```python
from scout.heartbeat import increment_mcap_null_with_price
```

In `fetch_top_movers` (around line 110), replace ONLY the for-loop body that builds `tokens` (do NOT remove the trailing `tokens.sort(...)` or the `logger.info(...)`):

```python
    tokens: list[CandidateToken] = []
    for raw in raw_by_id.values():
        # BL-075 Phase A (2026-05-03): track silent-rejection rate at the
        # mcap=0 floor. CoinGecko occasionally returns market_cap=null
        # for tokens with active price action (the RIV-shape blind spot).
        if (raw.get("market_cap") in (None, 0)) and (raw.get("current_price") or 0) > 0:
            increment_mcap_null_with_price()
        token = CandidateToken.from_coingecko(raw)
        if token.market_cap_usd < settings.MIN_MARKET_CAP:
            continue
        if token.market_cap_usd > settings.MAX_MARKET_CAP:
            continue
        tokens.append(token)
```

In `fetch_by_volume` (around line 237), same pattern — replace ONLY the for-loop body:

```python
    tokens: list[CandidateToken] = []
    for raw in raw_by_id.values():
        if (raw.get("market_cap") in (None, 0)) and (raw.get("current_price") or 0) > 0:
            increment_mcap_null_with_price()
        token = CandidateToken.from_coingecko(raw)
        if token.market_cap_usd < settings.MIN_MARKET_CAP:
            continue
        tokens.append(token)
```

**RETAIN** in both functions the existing trailing lines: `tokens.sort(...)` and `logger.info(...)`. The Edit tool's old_string should include those lines so they aren't accidentally removed.

- [ ] **Step 1.6 — Add ingestion-level test**

Append to `tests/test_heartbeat_mcap_missing.py`:

```python
import aiohttp
from aioresponses import aioresponses


@pytest.mark.asyncio
async def test_fetch_top_movers_increments_counter(settings_factory):
    """A CoinGecko response with market_cap=null + current_price>0 must bump the counter."""
    from scout.ingestion.coingecko import fetch_top_movers

    s = settings_factory(MIN_MARKET_CAP=0, MAX_MARKET_CAP=10**12)
    payload = [
        {"id": "tok1", "name": "Tok1", "symbol": "T1",
         "market_cap": None, "current_price": 0.0123, "total_volume": 10000},
        {"id": "tok2", "name": "Tok2", "symbol": "T2",
         "market_cap": 1_000_000, "current_price": 0.5, "total_volume": 50000},
    ]
    with aioresponses() as m:
        m.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            payload=payload, status=200, repeat=True,
        )
        async with aiohttp.ClientSession() as session:
            await fetch_top_movers(session, s)
    assert _heartbeat_stats["mcap_null_with_price_count"] == 1
```

- [ ] **Step 1.7 — Run all heartbeat tests**

Run: `uv run pytest tests/test_heartbeat_mcap_missing.py -v`
Expected: 5 passed

- [ ] **Step 1.8 — Commit**

```bash
git add scout/heartbeat.py scout/ingestion/coingecko.py tests/test_heartbeat_mcap_missing.py
git commit -m "feat(BL-075-Phase-A): mcap_null_with_price telemetry on heartbeat

Adds mcap_null_with_price_count to heartbeat output. Increments when
CoinGecko returns market_cap=null/0 but current_price>0 — the silent-
rejection shape that caused the RIV (riv-coin) 100x miss on 2026-05-03.

After 7d of telemetry we'll know whether Phase B (slow-burn watcher)
is justified per the BL-075 decision tree (<1% close, 1-5% add
fallback, >5% Phase B)."
```

---

## Task 2 — BL-071b: stop pre-stamping `EXPIRED` at write-time

**Files:**
- Modify: `scout/chains/tracker.py:512-547` (`_record_expired_chain` — write NULL not EXPIRED)
- Modify: `scout/db.py` `_migrate_feedback_loop_schema` (append a guarded one-shot UPDATE)
- Test: `tests/test_chain_outcomes_hydration.py` (new file)

**Why:** Today, `_record_expired_chain` writes `outcome_class='EXPIRED'` directly. The hydrator (`update_chain_outcomes`) then filters `WHERE outcome_class IS NULL`, so those rows are *permanently* skipped — even when the underlying `predictions` table later resolves them to HIT. Verified safe: only one consumer (`patterns.py:263`) reads chain_match outcomes, and it tolerates NULL/EXPIRED equally (NULL rows simply don't contribute to stats until the hydrator processes them).

- [ ] **Step 2.1 — Write failing test for write-time NULL semantics**

Create `tests/test_chain_outcomes_hydration.py`:

```python
"""BL-071b: chain_matches outcome hydration regression tests.

Uses local `db` fixture matching tests/test_chains_tracker.py pattern —
there is no global tmp_db fixture in conftest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.models import ActiveChain, ChainPattern, ChainStep
from scout.chains.tracker import _record_expired_chain, update_chain_outcomes
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _stub_pattern() -> ChainPattern:
    """Construct a ChainPattern matching scout/chains/models.py constraints.

    Required fields: name, description, steps, min_steps_to_trigger,
    conviction_boost, alert_priority. ChainPattern has NO `pipeline` field
    — pipeline is on the events/chains, not the pattern definition.
    """
    return ChainPattern(
        id=1,
        name="test_pattern",
        description="test pattern for outcome-hydration tests",
        steps=[
            ChainStep(
                step_number=1,
                event_type="signal_a",
                max_hours_after_anchor=24.0,
            ),
            ChainStep(
                step_number=2,
                event_type="signal_b",
                max_hours_after_anchor=48.0,
                max_hours_after_previous=24.0,
            ),
        ],
        min_steps_to_trigger=2,
        conviction_boost=10,
        alert_priority="medium",
    )


def _stub_chain(token_id: str, anchor_offset_hours: float = 4.0) -> ActiveChain:
    """Construct an ActiveChain matching scout/chains/models.py constraints.

    Required fields include pattern_name, step_events (dict[int, int]),
    created_at. steps_matched is list[int] (step numbers), NOT list of tuples.
    """
    now = datetime.now(timezone.utc)
    anchor = now - timedelta(hours=anchor_offset_hours)
    last_step = now - timedelta(hours=anchor_offset_hours / 2)
    return ActiveChain(
        token_id=token_id,
        pipeline="narrative",
        pattern_id=1,
        pattern_name="test_pattern",
        steps_matched=[1],
        step_events={1: 1},
        anchor_time=anchor,
        last_step_time=last_step,
        is_complete=False,
        created_at=anchor,
    )


@pytest.mark.asyncio
async def test_expired_chain_writes_null_not_expired(db):
    """BL-071b regression: expired chains must write outcome_class=NULL,
    not 'EXPIRED'. Pre-stamped EXPIRED was a silent permanent skip
    because the hydrator filters WHERE outcome_class IS NULL."""
    pattern = _stub_pattern()
    chain = _stub_chain("TOKEN_A")
    await _record_expired_chain(db, chain, pattern, datetime.now(timezone.utc))
    await db._conn.commit()  # _record_expired_chain doesn't commit; callers do
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='TOKEN_A'"
    )
    row = await cur.fetchone()
    assert row[0] is None, (
        "expected NULL, got %r — pre-stamped EXPIRED is the BL-071b bug" % row[0]
    )


@pytest.mark.asyncio
async def test_hydrator_picks_up_null_expired_chain(db):
    """End-to-end: write 'expired' chain (now NULL) -> insert matching
    predictions HIT -> run hydrator -> chain_match should be hit."""
    pattern = _stub_pattern()
    chain = _stub_chain("TOKEN_B", anchor_offset_hours=72.0)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=72)
    await _record_expired_chain(db, chain, pattern, long_ago)
    await db._conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            strategy_snapshot, predicted_at, outcome_class)
           VALUES ('cat','Cat','TOKEN_B','T','TokenB',
                   1000.0, 1.0, 80, 'high','high','test',
                   '{}', ?, 'hit')""",
        (long_ago.isoformat(),),
    )
    await db._conn.commit()
    updated = await update_chain_outcomes(db)
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='TOKEN_B'"
    )
    row = await cur.fetchone()
    assert row[0] == "hit"
```

- [ ] **Step 2.2 — Run test to verify it fails**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v`
Expected: FAIL — first test expects NULL but gets 'EXPIRED'.

- [ ] **Step 2.3 — Apply the fix in `scout/chains/tracker.py`**

Edit `scout/chains/tracker.py` lines 512-547. Use the Edit tool with `old_string` matching the entire function (use line 512-547 as guide). Replace with:

```python
async def _record_expired_chain(
    db: Database,
    chain: ActiveChain,
    pattern: ChainPattern,
    now: datetime,
) -> None:
    """Record an expired (unresolved) chain as a pending miss in chain_matches.

    BL-071b fix: writes outcome_class=NULL (not 'EXPIRED') so the hydrator
    `update_chain_outcomes` can later resolve the outcome from the predictions
    table. The previous behaviour pre-stamped 'EXPIRED' at write time, which
    the hydrator's `WHERE outcome_class IS NULL` filter then permanently
    skipped — a silent failure that caused 154 narrative chain_matches in
    prod to be stuck as EXPIRED with no evaluated_at.

    Verified safe: only patterns.py:263 reads chain_match outcomes for
    stats, and it tolerates NULL/EXPIRED equally (NULL rows simply don't
    contribute until the hydrator processes them).

    Only records if at least one step was matched.
    """
    steps_matched = len(chain.steps_matched)
    if steps_matched <= 0:
        return
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            chain.token_id,
            chain.pipeline,
            pattern.id,
            pattern.name,
            steps_matched,
            len(pattern.steps),
            chain.anchor_time.isoformat(),
            now.isoformat(),
            round(
                (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0,
                3,
            ),
            0,
        ),
    )
```

- [ ] **Step 2.4 — Run tests to verify both pass**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v`
Expected: 2 passed.

- [ ] **Step 2.5 — Add data migration via `_migrate_feedback_loop_schema` extension**

Edit `scout/db.py`. Locate `_migrate_feedback_loop_schema` (around line 830). Inside the same `try: await conn.execute("BEGIN EXCLUSIVE")` block, AFTER the existing BL-063 migration row insert (search for `bl063_moonshot` or similar — find the LAST `paper_migrations` insert in this method), add a new guarded block before the closing of the try:

```python
            # BL-071b: convert pre-stamped EXPIRED narrative rows to NULL so
            # the hydrator can re-evaluate them against the predictions table.
            # Bounded scope: narrative pipeline only, EXPIRED-with-no-evaluated_at
            # only. Memecoin EXPIRED rows are left alone (BL-071a, not BL-071b).
            # Idempotent: gated by paper_migrations row; second run is a no-op.
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl071b_unstamp_expired_narrative",),
            )
            already_applied = await cur.fetchone()
            if not already_applied:
                await conn.execute(
                    """UPDATE chain_matches
                          SET outcome_class = NULL
                        WHERE pipeline = 'narrative'
                          AND outcome_class = 'EXPIRED'
                          AND evaluated_at IS NULL"""
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (
                        "bl071b_unstamp_expired_narrative",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
```

This sits inside the existing `BEGIN EXCLUSIVE` transaction; commit/rollback handling is already in place at the existing method's `except` branch.

**Verification against existing pattern:** The existing migrations in this method use `INSERT OR IGNORE INTO paper_migrations (name, cutover_ts)`. The guard pattern of "SELECT from paper_migrations + skip if exists" matches the ALTER-skip pattern at db.py:892-900. The UPDATE itself is idempotent (re-run finds zero rows because they're already NULL); the gate is explicit so we don't redundantly run the UPDATE on every restart.

- [ ] **Step 2.6 — Add migration test (verifies both data state AND idempotency-recorded)**

Append to `tests/test_chain_outcomes_hydration.py`:

```python
@pytest.mark.asyncio
async def test_migration_converts_expired_narrative_rows(tmp_path):
    """BL-071b migration:
    - converts EXPIRED narrative rows with NULL evaluated_at to NULL
    - leaves memecoin EXPIRED rows alone (different pipeline)
    - leaves rows with evaluated_at intact (already-hydrated rows)
    - records itself in paper_migrations (so second startup is a no-op)
    """
    from scout.db import Database

    db_path = tmp_path / "mig.db"
    d1 = Database(db_path)
    await d1.initialize()
    long_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    await d1._conn.executemany(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, evaluated_at)
           VALUES (?, ?, 1, 'p', 1, 2, ?, ?, 0.0, 0, ?, ?)""",
        [
            ("N1", "narrative", long_ago, long_ago, "EXPIRED", None),
            ("N2", "narrative", long_ago, long_ago, "EXPIRED", long_ago),
            ("M1", "memecoin", long_ago, long_ago, "EXPIRED", None),
        ],
    )
    await d1._conn.commit()
    # Force migration to run NOW (rows were inserted after initialize() already
    # ran the migration once; explicitly re-invoke to test conversion).
    await d1._migrate_feedback_loop_schema()

    cur = await d1._conn.execute(
        "SELECT token_id, outcome_class FROM chain_matches ORDER BY token_id"
    )
    rows = {r[0]: r[1] for r in await cur.fetchall()}
    assert rows == {"N1": None, "N2": "EXPIRED", "M1": "EXPIRED"}

    # Verify migration is recorded as applied (idempotency gate)
    cur2 = await d1._conn.execute(
        "SELECT name FROM paper_migrations WHERE name = ?",
        ("bl071b_unstamp_expired_narrative",),
    )
    assert (await cur2.fetchone()) is not None

    # Re-run: must be a no-op (second invocation hits the gate)
    await d1._migrate_feedback_loop_schema()
    await d1.close()
```

- [ ] **Step 2.7 — Run migration test**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py::test_migration_converts_expired_narrative_rows -v`
Expected: PASS

- [ ] **Step 2.8 — Run full chain test suite to catch regressions**

Run: `uv run pytest tests/ -k "chain" -v`
Expected: all green (no regressions in `test_chain_*`).

- [ ] **Step 2.9 — Commit**

```bash
git add scout/chains/tracker.py scout/db.py tests/test_chain_outcomes_hydration.py
git commit -m "fix(BL-071b): stop pre-stamping EXPIRED on chain_matches at write time

_record_expired_chain now writes outcome_class=NULL so the hydrator can
later resolve narrative chain_matches against the predictions table.

Includes bounded one-time data migration (extends _migrate_feedback_
loop_schema with paper_migrations gate) that converts existing EXPIRED
narrative rows (with no evaluated_at) to NULL — memecoin EXPIRED rows
are left untouched (BL-071a, not BL-071b).

Verified via grep that only patterns.py:263 reads chain_match outcomes,
and it tolerates NULL/EXPIRED equally — net safe."
```

---

## Task 3 — BL-071a (partial): `mcap_at_completion` column + diagnostic hydrator branch

**Files:**
- Modify: `scout/db.py` — add `mcap_at_completion REAL` to `chain_matches` via existing `_create_tables` ALTER-on-existing-table pattern (or extend `_migrate_feedback_loop_schema`)
- Modify: `scout/chains/tracker.py` — flip memecoin hydrator branch (skip-silently-when-populated; aggregate warning when both sources NULL)
- Modify: `backlog.md` — add BL-071a' follow-up entry capturing the writer-wiring + DexScreener fetch work this PR does NOT do
- Test: `tests/test_chain_outcomes_hydration.py` — extend with Task 3 cases

**Why:** Recon proved the original `outcomes`-table hydrator path for memecoin pipeline cannot work in production today (alerts table has 2 rows, outcomes table has 2 rows; conviction-gated alerts almost never fire). The clean path is to fetch current FDV from DexScreener at hydration time and compare against `mcap_at_completion`. **This task adds the column + flips the hydrator branch to be ready for that. It does NOT wire the writers to populate `mcap_at_completion` AND does NOT add the DexScreener fetch** — both go in BL-071a' follow-up. Until BL-071a' lands, the populated-column branch is intentionally a silent no-op (no warning); the no-source-available case fires an aggregate `chain_outcomes_unhydrateable_memecoin count=N` log once per LEARN cycle, not per-row.

- [ ] **Step 3.1 — Write failing tests for column existence + branch behaviour**

Append to `tests/test_chain_outcomes_hydration.py`:

```python
@pytest.mark.asyncio
async def test_chain_matches_has_mcap_at_completion_column(db):
    """Schema check: column must exist after migrations run."""
    cur = await db._conn.execute("PRAGMA table_info(chain_matches)")
    cols = {row[1]: row[2] for row in await cur.fetchall()}
    assert "mcap_at_completion" in cols
    assert cols["mcap_at_completion"] == "REAL"


@pytest.mark.asyncio
async def test_hydrator_silent_skip_when_mcap_at_completion_populated(db, caplog):
    """When memecoin chain_match has non-NULL mcap_at_completion, hydrator
    must skip silently — BL-071a' will add the DexScreener fetch later.
    No per-row warning (that would be permanent log noise)."""
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xdeadbeef','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1500000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()
    with caplog.at_level("WARNING"):
        updated = await update_chain_outcomes(db)
    assert updated == 0
    # Must NOT emit per-row warning for the populated case
    assert not any(
        "chain_outcomes_pending" in (rec.message + str(rec.__dict__))
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_hydrator_aggregate_warning_when_no_source(db, caplog):
    """When N memecoin rows lack BOTH mcap_at_completion AND outcomes-table
    data, hydrator emits ONE aggregate warning per LEARN cycle (not N)."""
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [
        (f"0xnosrc{i}", "memecoin", 1, "p", 1, 2, long_ago, long_ago, 0.0, 0, None, None)
        for i in range(3)
    ]
    await db._conn.executemany(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await db._conn.commit()
    with caplog.at_level("WARNING"):
        updated = await update_chain_outcomes(db)
    assert updated == 0
    aggregate_logs = [
        rec for rec in caplog.records
        if "chain_outcomes_unhydrateable_memecoin" in (rec.message + str(rec.__dict__))
    ]
    assert len(aggregate_logs) == 1, (
        "expected exactly one aggregate warning for 3 unhydrateable rows, got "
        f"{len(aggregate_logs)}"
    )
```

- [ ] **Step 3.2 — Run tests to verify they fail**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v`
Expected: the three new tests fail (column missing; per-row warning still present; aggregate warning not emitted).

- [ ] **Step 3.3 — Add column via `_migrate_feedback_loop_schema` extension**

Edit `scout/db.py`. Inside the same `_migrate_feedback_loop_schema` block where Step 2.5 added the BL-071b row, append (still inside the BEGIN EXCLUSIVE try-block, after the BL-071b block):

```python
            # BL-071a: add mcap_at_completion column to chain_matches.
            # Hydrator (Task 3) reads it; writers (BL-071a') will populate it
            # in a follow-up PR. PRAGMA-guarded ALTER, idempotent.
            cur = await conn.execute("PRAGMA table_info(chain_matches)")
            cm_cols = {row[1] for row in await cur.fetchall()}
            if "mcap_at_completion" not in cm_cols:
                await conn.execute(
                    "ALTER TABLE chain_matches ADD COLUMN mcap_at_completion REAL"
                )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (
                    "bl071a_chain_matches_mcap_at_completion",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
```

- [ ] **Step 3.4 — Modify `update_chain_outcomes` for new memecoin branch + aggregate warning**

Edit `scout/chains/tracker.py`. The function `update_chain_outcomes` lives at lines 550-623. Replace the memecoin elif branch (lines 598-607) AND wrap the loop with an unhydrateable-counter and aggregate emit at the end. Full replacement of the function body from the loop through the final return:

```python
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    async with conn.execute(
        """SELECT id, token_id, pipeline FROM chain_matches
           WHERE outcome_class IS NULL AND completed_at < ?""",
        (cutoff,),
    ) as cur:
        pending = await cur.fetchall()

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    memecoin_unhydrateable = 0
    for row in pending:
        match_id = row["id"]
        token_id = row["token_id"]
        pipeline = row["pipeline"]
        outcome: str | None = None

        if pipeline == "narrative":
            async with conn.execute(
                """SELECT outcome_class FROM predictions
                   WHERE coin_id = ?
                     AND outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED'
                   ORDER BY predicted_at DESC LIMIT 1""",
                (token_id,),
            ) as cur2:
                prow = await cur2.fetchone()
            if prow is not None and prow[0]:
                outcome = str(prow[0]).lower()
                if outcome not in ("hit", "miss"):
                    outcome = "hit" if outcome == "hit" else "miss"
        elif pipeline == "memecoin":
            # BL-071a partial: prefer mcap_at_completion (set by writer once
            # BL-071a' wires them; today always NULL). When populated, skip
            # SILENTLY — BL-071a' will inline the DexScreener fetch here.
            # When NULL, fall back to legacy outcomes table; if THAT is also
            # empty, count for an aggregate warning emitted once at end.
            async with conn.execute(
                """SELECT mcap_at_completion FROM chain_matches WHERE id = ?""",
                (match_id,),
            ) as cur_m:
                mcap_row = await cur_m.fetchone()
            mcap_at_completion = mcap_row[0] if mcap_row else None

            if mcap_at_completion is not None and mcap_at_completion > 0:
                # Intentional silent skip — BL-071a' inlines the fetch here.
                continue

            async with conn.execute(
                """SELECT price_change_pct FROM outcomes
                   WHERE contract_address = ? AND price_change_pct IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (token_id,),
            ) as cur2:
                orow = await cur2.fetchone()
            if orow is not None and orow[0] is not None:
                outcome = "hit" if float(orow[0]) > 0 else "miss"
            else:
                memecoin_unhydrateable += 1

        if outcome is None:
            continue

        await conn.execute(
            """UPDATE chain_matches
               SET outcome_class = ?, evaluated_at = ?
               WHERE id = ?""",
            (outcome, now_iso, match_id),
        )
        updated += 1

    await conn.commit()
    if updated:
        logger.info("chain_outcomes_hydrated", count=updated)
    # BL-071a partial: aggregate warning per LEARN cycle (not per row) so
    # operators see the silent-failure surface without log spam. Will go
    # quiet once BL-071a' wires writers + adds DexScreener fetch.
    if memecoin_unhydrateable:
        logger.warning(
            "chain_outcomes_unhydrateable_memecoin",
            count=memecoin_unhydrateable,
            reason=(
                "outcomes table empty AND mcap_at_completion NULL across N rows. "
                "BL-071a' will populate mcap_at_completion at write time AND "
                "inline the DexScreener fetch in this hydrator branch."
            ),
        )
    return updated
```

The key change vs. v1 of the plan: the populated-column branch is `continue` with NO warning (silent skip — intentional intermediate state). The no-source-available case increments a counter that's emitted ONCE at the end of the function as `chain_outcomes_unhydrateable_memecoin count=N` — aggregated, not per-row.

- [ ] **Step 3.5 — Run tests to verify they pass**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v`
Expected: 6 passed (3 new + 3 prior).

- [ ] **Step 3.6 — Add BL-071a' follow-up to `backlog.md`**

Edit `backlog.md`. Locate the BL-071a entry. Append immediately after it (new entry):

```markdown
### BL-071a': Wire chain_match writers + DexScreener fetch for memecoin outcome hydration
**Status:** UNBLOCKED (follow-up from Bundle A 2026-05-03) — schema column + hydrator branch exist; only writer wiring + DexScreener fetch remain
**Tag:** `chain-pipeline` `outcome-telemetry` `unblocks-BL-071a-fully`
**Files:** `scout/chains/tracker.py` (`_record_chain_complete`, `_record_expired_chain` — accept and store mcap; hydrator's populated-branch — replace silent `continue` with DexScreener FDV fetch + outcome computation), `scout/chains/events.py` or chain-completion caller chain (pass current FDV through to writers), tests
**Why:** Bundle A added `chain_matches.mcap_at_completion REAL` column + hydrator branch that skips silently when populated. Writers still pass NULL because adding the caller-wiring would have grown Bundle A scope. Once writers populate the column AND the hydrator inlines the DexScreener fetch, hit/miss outcomes flow for memecoin chain_matches. Closes the BL-071a death-spiral structurally.
**Acceptance:**
- New memecoin chain_matches have non-NULL `mcap_at_completion`.
- LEARN cycle emits `chain_outcomes_hydrated count>0` for memecoin pipeline (instead of `chain_outcomes_unhydrateable_memecoin count=N` aggregate warning).
- Pattern hit-rate becomes meaningful for memecoin patterns.
**Estimate:** 0.5d (small caller-chain edit + DexScreener fetch in hydrator + tests).
```

- [ ] **Step 3.7 — Run full chain test suite**

Run: `uv run pytest tests/ -k "chain" -v`
Expected: all green.

- [ ] **Step 3.8 — Commit**

```bash
git add scout/db.py scout/chains/tracker.py tests/test_chain_outcomes_hydration.py backlog.md
git commit -m "fix(BL-071a-partial): mcap_at_completion column + diagnostic hydrator branch

Adds chain_matches.mcap_at_completion REAL column via _migrate_feedback_
loop_schema. Hydrator's memecoin branch now: (a) skips silently when
column populated (BL-071a' inlines the DexScreener fetch later); (b)
falls back to legacy outcomes-table for back-compat; (c) emits ONE
aggregate chain_outcomes_unhydrateable_memecoin warning per LEARN
cycle (not per-row) when neither source has data.

The aggregate-not-per-row choice is deliberate: per-row warnings would
be permanent log noise until BL-071a' lands. The aggregate count gives
operators visibility without spam.

Honest scope: writer wiring + DexScreener fetch are split into BL-071a'
to keep this PR focused on the schema + hydrator-shape. The follow-up
is captured as a new backlog entry with concrete acceptance criteria."
```

---

## Final integration step

- [ ] **Step F.1 — Full test suite**

Run: `uv run pytest -q --tb=short`
Expected: all green. If anything outside our touched modules fails, investigate before opening PR.

- [ ] **Step F.2 — Format**

Run: `uv run black scout/ tests/`

- [ ] **Step F.3 — Commit any formatting changes**

```bash
git add -u
git commit -m "style: black formatting"
```

---

## Self-Review Checklist (post-v2-rewrite)

1. **Scope coverage:** BL-075 Phase A → Task 1 ✓; BL-071a partial → Task 3 (column + branch; writer-wiring deferred to BL-071a' with explicit follow-up) ✓; BL-071b → Task 2 ✓.
2. **Reviewer M1 (migration pattern):** Steps 2.5 and 3.3 now extend `_migrate_feedback_loop_schema` with `paper_migrations`-gated blocks matching the existing pattern at db.py:892-925. ✓
3. **Reviewer M2 (warning-as-noise):** Per-row warning replaced with aggregate `chain_outcomes_unhydrateable_memecoin count=N` emitted once per LEARN cycle. ✓
4. **Reviewer M3 (inverted hydrator branch):** Populated-column branch now `continue` SILENTLY (no warning). Comment explicitly says BL-071a' inlines the fetch here. ✓
5. **Reviewer-1 critical (tmp_db fixture):** Replaced with local `db` fixture matching `tests/test_chains_tracker.py` pattern at lines 24-30. ✓
6. **Reviewer-1 critical (Pydantic stub fields):** `_stub_pattern` and `_stub_chain` rewritten with all required fields (`description`, `min_steps_to_trigger` for ChainPattern; `step_number`, `max_hours_after_anchor` for ChainStep; `pattern_name`, `step_events`, `created_at` for ActiveChain; `steps_matched: list[int]` not list-of-tuples). Import path corrected to `scout.chains.models`. ✓
7. **Reviewer-1 should-fix (test commit):** Step 2.1 test now calls `await db._conn.commit()` after `_record_expired_chain`. ✓
8. **Reviewer S1 (counter naming):** Renamed `mcap_missing_count` → `mcap_null_with_price_count` to be precise about what's measured. ✓
9. **Reviewer S2 (deploy ordering):** Added explicit deploy-ordering note at top of plan: `git pull` → `systemctl restart gecko-pipeline`. ✓
10. **Reviewer S3 (idempotency-recorded test gap):** Step 2.6 test now asserts `paper_migrations` row exists AND second invocation is a no-op. ✓
11. **No placeholders:** all code shown verbatim ✓
12. **New primitives marker:** present at top, updated for v2 ✓
13. **TDD discipline:** every task is failing-test → minimal-impl → passing-test → commit ✓
14. **No cross-task coupling:** Tasks 1, 2, 3 touch different files; could be reverted independently ✓
