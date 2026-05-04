# BL-071a': Wire chain_match writers + DexScreener fetch — Implementation Plan (v2 post-review)

**New primitives introduced:** new helper module `scout/chains/mcap_fetcher.py` (single async function `fetch_token_fdv` + protocol type `McapFetcher` for dependency injection in tests); new structured log event `chain_outcome_resolved_via_dexscreener` (per-row INFO); new structured log event `chain_outcome_dexscreener_failed` (per-row **DEBUG** — demoted from WARNING per R1-3, plan v2); new aggregate log events `chain_outcomes_ds_transient_failures` (WARNING, once per LEARN cycle) and `chain_outcome_ds_persistent_failure` (ERROR, when stuck-row age exceeds threshold per R1-1); new aggregate `chain_tracker_session_unhealthy` (ERROR, when DS failure rate > 50% in one cycle per R1-2). No new database columns or migrations — uses the `chain_matches.mcap_at_completion REAL` column shipped in Bundle A.

**v2 changes from plan-review feedback (cross-confirmed by 2 parallel reviewers):**
- **MUST-FIX R2-1:** Task 4 was targeting the wrong file. The actual `update_chain_outcomes` caller is `scout/narrative/learner.py:326`, NOT `scout/main.py`. Without this fix, BL-071a' would be dead-on-arrival (the `session is not None` guard would silently fall through to the legacy path forever — exactly the silent-skip class this PR exists to close). v2 fixes Task 4 + adds **defense-in-depth: hydrator self-creates an aiohttp session if `session is None`**, so even if a future caller forgets to wire the session the new path still fires.
- **MUST-FIX R1-3:** Per-row `chain_outcome_dexscreener_failed` WARNING re-introduced the antipattern Bundle A R2 flagged (permanent log noise). v2 demotes per-row to DEBUG and keeps the aggregate WARNING.
- **MUST-FIX R1-1:** Persistent DS outage made invisible by per-cycle aggregate that resets. v2 adds `chain_outcome_ds_persistent_failure` ERROR when any unresolved-due-to-DS row's age exceeds `2 × CHAIN_CHECK_INTERVAL_SEC`.
- **SHOULD-FIX R1-2:** Long-lived session degradation undetectable. v2 adds `chain_tracker_session_unhealthy` ERROR when DS failure rate exceeds 50% of attempts in one LEARN cycle (with floor of ≥3 attempts to avoid noise from single-row cycles).
- **SHOULD-FIX R2-4:** v2 keeps the superseded `test_hydrator_silent_skip_when_mcap_at_completion_populated` Bundle A test marked `@pytest.mark.skip` (with reason) instead of deleting — preserves the invariant doc that "no session = falls back to legacy" still holds for future maintainers.
- **SHOULD-FIX R2-2:** v2 explicitly notes that the existing `tests/test_chains_learn.py` callers exercise the legacy path (because they pass no session); they don't validate the new BL-071a' code path. The new tests in this plan do.

**Explicitly deferred to BL-071a'' (follow-up):**
- **R2-3** (pre-fetch FDVs OUTSIDE the `check_chains` transaction to avoid 15s SQLite write-lock-hold per memecoin completion): pragmatically acceptable today because chain completions are rare (~0-2 per cycle in prod) and the pipeline is single-process. Documented inline in `_record_completion` as a known scope-limitation. Optimization PR if completion-burst behaviour ever appears.
- **R1-4** (misrouted-pipeline regex detection): orthogonal scope; capture as separate backlog item if it ever happens.
- **R1-6** (pre-Bundle-A backlog backfill of 30 stuck rows): combining a one-shot data backfill with the writer-wiring fix increases blast radius. Ship BL-071a' first, verify it works, then a small follow-up PR backfills the historical rows. Note added to BL-071a' merge-and-deploy step (manual SQL for now).

**Goal:** Close the silent-skip surface that Bundle A intentionally left open: `_record_completion` will populate `mcap_at_completion` at write time (via DexScreener FDV fetch); `update_chain_outcomes` will use the populated value plus a current-time DexScreener fetch to compute hit/miss for memecoin chain_matches. Includes the coupling-guard test BL-071a' acceptance requires.

**Architecture:**
- **Two-snapshot model.** "Completion mcap" captured at `_record_completion` time. "Current mcap" fetched at hydration time (~48h+ later). Hit if `current/completion > 1.5` (default; configurable). Mcap_at_completion remains NULL for `_record_expired_chain` writes — semantically there was no completion, so no anchor mcap exists.
- **Dependency injection** for the mcap fetcher so tests don't hit the network. Production injects the real `fetch_token_fdv`; tests inject a stub.
- **Graceful degradation.** If DexScreener returns no data at write time, `mcap_at_completion` stays NULL and the row falls back to the legacy outcomes path. If DexScreener fails at hydration time, the row stays NULL outcome (re-evaluated next cycle). Neither is a hard failure.
- **Coupling-guard test** asserts that after a hydration cycle, no chain_match exists with `non-NULL mcap_at_completion AND outcome_class IS NULL AND completed_at < now-48h` — exactly the silent-skip surface Bundle A introduced.

**Tech Stack:** Python 3.11, aiohttp, aiosqlite, structlog, pytest-asyncio, aioresponses (mock-network in tests).

**Honest scope-decision note:**
- "Chain-agnostic" DexScreener endpoint chosen (`/latest/dex/tokens/{contract}`) so the writer doesn't need chain metadata it doesn't have. Trade-off: returns pairs from ALL chains the token exists on; we take the first pair (DexScreener orders by liquidity desc by default). For tokens with the same contract address on multiple chains (rare for memecoin contracts), this is approximate but acceptable.
- Hit threshold (`+50% pct change`) is set as a config setting (`CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0`) so it's tunable post-deploy without a code change.
- The aggregate `chain_outcomes_unhydrateable_memecoin` warning from Bundle A is now expected to STOP firing for newly-completed chains. Existing pre-Bundle-A NULL rows (the 154 narrative + 30 memecoin currently-stuck rows) will still potentially trigger it on the first LEARN cycle, then taper as they get hydrated. Verification step in §7 below.

---

## Task 1 — Add `mcap_fetcher` helper module

**Files:**
- Create: `scout/chains/mcap_fetcher.py`
- Test: `tests/test_chain_mcap_fetcher.py` (new)

**Why:** Isolated module makes the dependency injection clean (one function to swap in tests) and avoids polluting `tracker.py` with HTTP code. Mirrors the existing pattern from `scout/safety.py` (single-purpose async helper).

- [ ] **Step 1.1 — Write failing test for happy-path fetch**

Create `tests/test_chain_mcap_fetcher.py`:

```python
"""BL-071a': DexScreener FDV fetcher tests."""
from __future__ import annotations

import os
import sys

import pytest

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason=(
        "Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp/aioresponses tests "
        "to avoid the local OpenSSL DLL conflict."
    ),
)


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_returns_first_pair_fdv():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import fetch_token_fdv

    contract = "0xCB0c224f9382Ca5d09aCFb60141D332A8cA9ce42"
    payload = {
        "pairs": [
            {"fdv": 1_500_000.0, "chainId": "ethereum", "liquidity": {"usd": 50000}},
            {"fdv": 1_200_000.0, "chainId": "base", "liquidity": {"usd": 10000}},
        ]
    }
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            payload=payload, status=200,
        )
        async with aiohttp.ClientSession() as session:
            fdv = await fetch_token_fdv(session, contract)
    assert fdv == 1_500_000.0


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_returns_none_on_empty_pairs():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import fetch_token_fdv

    contract = "0xdeadbeef"
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            payload={"pairs": []}, status=200,
        )
        async with aiohttp.ClientSession() as session:
            fdv = await fetch_token_fdv(session, contract)
    assert fdv is None


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_returns_none_on_404():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import fetch_token_fdv

    contract = "0xnotfound"
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            status=404,
        )
        async with aiohttp.ClientSession() as session:
            fdv = await fetch_token_fdv(session, contract)
    assert fdv is None


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_returns_none_on_timeout():
    import asyncio

    import aiohttp

    from scout.chains.mcap_fetcher import fetch_token_fdv

    contract = "0xtimeout"

    async def _slow_get(*args, **kwargs):
        await asyncio.sleep(60)

    # Use a stub session whose .get raises asyncio.TimeoutError under the hood.
    class _StubResp:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *a):
            return False

    class _StubSession:
        def get(self, *a, **kw):
            return _StubResp()

    fdv = await fetch_token_fdv(_StubSession(), contract)
    assert fdv is None


@_SKIP_AIOHTTP
@pytest.mark.asyncio
async def test_fetch_token_fdv_returns_none_when_pair_lacks_fdv_field():
    import aiohttp
    from aioresponses import aioresponses

    from scout.chains.mcap_fetcher import fetch_token_fdv

    contract = "0xmissingfdv"
    payload = {"pairs": [{"chainId": "ethereum"}]}  # no fdv key
    with aioresponses() as m:
        m.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            payload=payload, status=200,
        )
        async with aiohttp.ClientSession() as session:
            fdv = await fetch_token_fdv(session, contract)
    assert fdv is None
```

- [ ] **Step 1.2 — Run, expect ImportError**

Run: `cd C:/projects/gecko-alpha && uv run pytest tests/test_chain_mcap_fetcher.py -v`
Expected: ImportError on `from scout.chains.mcap_fetcher import fetch_token_fdv`.

- [ ] **Step 1.3 — Implement the helper**

Create `scout/chains/mcap_fetcher.py`:

```python
"""BL-071a': DexScreener FDV fetcher for chain_match outcome hydration.

Used by `scout/chains/tracker.py` at two points:
1. `_record_completion` (write time) — captures `mcap_at_completion`.
2. `update_chain_outcomes` (hydration time) — fetches current FDV to
   compute pct change vs the captured completion FDV.

Uses the chain-agnostic `/latest/dex/tokens/{contract}` endpoint so the
caller does NOT need to know the chain. Returns the FDV of the first
pair (DexScreener orders by liquidity desc by default).

Fail-soft: returns None on any error (404, timeout, malformed response,
missing field). Callers are responsible for graceful degradation —
the writer leaves `mcap_at_completion` NULL, the hydrator skips this
LEARN cycle and re-tries next time.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import aiohttp
import structlog

logger = structlog.get_logger()

DS_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{contract}"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


class McapFetcher(Protocol):
    """Protocol for the FDV-fetcher dependency. Production: fetch_token_fdv.
    Tests: stub returning a fixed value (or None to simulate no-data)."""

    async def __call__(
        self,
        session: aiohttp.ClientSession,
        contract: str,
    ) -> float | None: ...


async def fetch_token_fdv(
    session: aiohttp.ClientSession,
    contract: str,
) -> float | None:
    """Fetch current FDV for a token contract from DexScreener.

    Returns float FDV (USD) of the most-liquid pair, or None on any
    error / missing data. Never raises.
    """
    url = DS_TOKEN_URL.format(contract=contract)
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.debug(
                    "ds_fetch_non_200",
                    contract=contract,
                    status=resp.status,
                )
                return None
            data = await resp.json()
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        logger.debug(
            "ds_fetch_error",
            contract=contract,
            error_type=type(exc).__name__,
        )
        return None

    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not pairs or not isinstance(pairs, list):
        return None

    fdv_raw = pairs[0].get("fdv") if isinstance(pairs[0], dict) else None
    if fdv_raw is None:
        return None
    try:
        fdv = float(fdv_raw)
    except (TypeError, ValueError):
        return None
    return fdv if fdv > 0 else None
```

- [ ] **Step 1.4 — Run, expect 5 pass (1 may skip on Windows)**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_chain_mcap_fetcher.py -v`
Expected: 1 timeout test passes (uses stub session, no network), 4 aiohttp tests skip on Windows. Linux/CI: 5 pass.

- [ ] **Step 1.5 — Commit**

```bash
git add scout/chains/mcap_fetcher.py tests/test_chain_mcap_fetcher.py
git commit -m "feat(BL-071a'): add chain_mcap_fetcher helper

Single-purpose async helper that wraps the DexScreener
/latest/dex/tokens/{contract} endpoint with fail-soft semantics
(returns None on any error). Uses the chain-agnostic endpoint so the
caller doesn't need to know the chain — takes the FDV of the
most-liquid pair (DexScreener orders by liquidity desc).

Will be invoked from _record_completion (write time) and
update_chain_outcomes (hydration time) in the next commit."
```

---

## Task 2 — Wire `_record_completion` to populate `mcap_at_completion`

**Files:**
- Modify: `scout/chains/tracker.py` (`_record_completion` accepts injected fetcher; INSERT statement adds `mcap_at_completion`)
- Modify: `scout/chains/tracker.py` (`run_chain_tracker` creates aiohttp session for the writer to use)
- Test: `tests/test_chain_outcomes_hydration.py` (extend with writer-population tests)

**Why:** Captures the "completion FDV" snapshot at the moment the chain completes. Without this, the hydrator has no baseline to compute pct change against.

- [ ] **Step 2.1 — Add failing tests for writer population**

Append to `tests/test_chain_outcomes_hydration.py`:

```python
@pytest.mark.asyncio
async def test_record_completion_populates_mcap_at_completion(db):
    """BL-071a': _record_completion must capture FDV at write time
    via the injected fetcher, store it in mcap_at_completion."""
    from scout.chains.tracker import _record_completion

    pattern = _stub_pattern()
    chain = _stub_chain("0xtoken1", anchor_offset_hours=2.0)
    chain.completed_at = datetime.now(timezone.utc)

    # Stub fetcher returns a fixed FDV
    async def _stub_fetcher(session, contract):
        assert contract == "0xtoken1"
        return 2_500_000.0

    class _StubSettings:
        CHAIN_ALERT_ON_COMPLETE = False

    await _record_completion(
        db, chain, pattern, _StubSettings(),
        session=None, mcap_fetcher=_stub_fetcher,
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id='0xtoken1'"
    )
    row = await cur.fetchone()
    assert row[0] == 2_500_000.0


@pytest.mark.asyncio
async def test_record_completion_leaves_mcap_null_when_fetcher_returns_none(db):
    """Graceful degradation: if DexScreener has no data, the row
    still writes — mcap_at_completion stays NULL. The chain_match is
    NOT lost just because we couldn't get an FDV snapshot."""
    from scout.chains.tracker import _record_completion

    pattern = _stub_pattern()
    chain = _stub_chain("0xtoken2", anchor_offset_hours=2.0)
    chain.completed_at = datetime.now(timezone.utc)

    async def _none_fetcher(session, contract):
        return None

    class _StubSettings:
        CHAIN_ALERT_ON_COMPLETE = False

    await _record_completion(
        db, chain, pattern, _StubSettings(),
        session=None, mcap_fetcher=_none_fetcher,
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id='0xtoken2'"
    )
    row = await cur.fetchone()
    assert row[0] is None


@pytest.mark.asyncio
async def test_record_completion_skips_fetcher_for_narrative_pipeline(db):
    """Narrative pipeline doesn't use FDV-based outcome — token_id is a
    CoinGecko slug, not a contract address. Fetcher MUST NOT be called
    for narrative chains."""
    from scout.chains.tracker import _record_completion

    pattern = _stub_pattern()
    # Force narrative pipeline
    chain = ActiveChain(
        token_id="boba-network",  # slug, not contract
        pipeline="narrative",
        pattern_id=1,
        pattern_name="test_pattern",
        steps_matched=[1],
        step_events={1: 1},
        anchor_time=datetime.now(timezone.utc) - timedelta(hours=2),
        last_step_time=datetime.now(timezone.utc) - timedelta(hours=1),
        is_complete=True,
        completed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    fetcher_calls = []

    async def _spy_fetcher(session, contract):
        fetcher_calls.append(contract)
        return 999_999.0

    class _StubSettings:
        CHAIN_ALERT_ON_COMPLETE = False

    await _record_completion(
        db, chain, pattern, _StubSettings(),
        session=None, mcap_fetcher=_spy_fetcher,
    )
    await db._conn.commit()
    assert fetcher_calls == [], (
        f"narrative pipeline must NOT call DS fetcher; got {fetcher_calls}"
    )
    cur = await db._conn.execute(
        "SELECT mcap_at_completion FROM chain_matches WHERE token_id='boba-network'"
    )
    row = await cur.fetchone()
    assert row[0] is None
```

- [ ] **Step 2.2 — Run, expect 3 fails**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v -k "record_completion"`
Expected: TypeError ("`_record_completion() got an unexpected keyword argument 'session'`") on all 3.

- [ ] **Step 2.3 — Modify `_record_completion`**

Edit `scout/chains/tracker.py`. At the top of the file, add the import:

```python
from scout.chains.mcap_fetcher import McapFetcher, fetch_token_fdv
```

Replace the `_record_completion` function:

```python
async def _record_completion(
    db: Database,
    chain: ActiveChain,
    pattern: ChainPattern,
    settings: Settings,
    *,
    session: aiohttp.ClientSession | None = None,
    mcap_fetcher: McapFetcher | None = None,
) -> None:
    """Write chain_matches row + emit chain_complete event + optional alert.

    BL-071a' (2026-05-04): for memecoin pipeline, captures a DexScreener
    FDV snapshot in `mcap_at_completion` so the hydrator can later compute
    pct change vs current FDV. Narrative pipeline skips the fetch
    (token_id is a CoinGecko slug, not a contract; FDV lookup would fail).
    Failures are graceful: row writes with mcap_at_completion=NULL.
    """
    duration_h = (chain.last_step_time - chain.anchor_time).total_seconds() / 3600.0

    # BL-071a': fetch FDV snapshot for memecoin chains.
    # SCOPE NOTE (deferred to BL-071a'' per R2-3): this fetch happens
    # INSIDE the check_chains transaction. SQLite write lock is held for
    # up to 15s (DS timeout) per memecoin completion. Acceptable today
    # (single-process pipeline, ~0-2 completions per 60s cycle in prod);
    # if completion bursts ever appear, refactor to pre-fetch in parallel
    # outside the transaction in a follow-up optimization PR.
    mcap_at_completion: float | None = None
    if chain.pipeline == "memecoin" and session is not None:
        fetcher = mcap_fetcher or fetch_token_fdv
        try:
            mcap_at_completion = await fetcher(session, chain.token_id)
        except Exception:
            # Fail-soft — never block chain write on the snapshot
            logger.exception(
                "mcap_at_completion_fetch_unexpected_error",
                token_id=chain.token_id,
            )
            mcap_at_completion = None

    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, mcap_at_completion)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chain.token_id,
            chain.pipeline,
            pattern.id,
            pattern.name,
            len(chain.steps_matched),
            len(pattern.steps),
            chain.anchor_time.isoformat(),
            (chain.completed_at or datetime.now(timezone.utc)).isoformat(),
            round(duration_h, 3),
            pattern.conviction_boost,
            mcap_at_completion,
        ),
    )
    await db._conn.execute(
        "UPDATE chain_patterns SET total_triggers = total_triggers + 1 WHERE id = ?",
        (pattern.id,),
    )

    await safe_emit(
        db,
        token_id=chain.token_id,
        pipeline=chain.pipeline,
        event_type="chain_complete",
        event_data={
            "pattern_name": pattern.name,
            "steps_matched": len(chain.steps_matched),
            "total_steps": len(pattern.steps),
            "conviction_boost": pattern.conviction_boost,
            "chain_duration_hours": round(duration_h, 3),
            "mcap_at_completion": mcap_at_completion,
        },
        source_module="chains.tracker",
    )

    if settings.CHAIN_ALERT_ON_COMPLETE and pattern.alert_priority in (
        "high",
        "medium",
    ):
        try:
            from scout.chains.alerts import send_chain_alert  # lazy import

            await send_chain_alert(db, chain, pattern, settings)
        except Exception:
            logger.exception("chain_alert_failed", pattern=pattern.name)
```

You'll also need to add `import aiohttp` near the top if not present. Verify with `grep "^import aiohttp" scout/chains/tracker.py` first; add if missing.

- [ ] **Step 2.4 — Update `check_chains` caller to pass session**

Edit `scout/chains/tracker.py`. The call site is `await _record_completion(db, chain, pattern, settings)` at line 148. Replace with:

```python
        for chain, pattern in completed_chains:
            await _record_completion(
                db, chain, pattern, settings,
                session=session, mcap_fetcher=mcap_fetcher,
            )
```

This requires `check_chains` to accept `session` and `mcap_fetcher` parameters. Update its signature:

```python
async def check_chains(
    db: Database,
    settings: Settings,
    *,
    session: aiohttp.ClientSession | None = None,
    mcap_fetcher: McapFetcher | None = None,
) -> None:
```

And `run_chain_tracker` creates the session once for the loop:

```python
async def run_chain_tracker(db: Database, settings: Settings) -> None:
    """Main chain tracking loop — runs forever."""
    await seed_built_in_patterns(db)
    logger.info(
        "chain_tracker_started",
        interval_sec=settings.CHAIN_CHECK_INTERVAL_SEC,
    )
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await check_chains(db, settings, session=session)
            except Exception:
                logger.exception("chain_tracker_cycle_error")
            try:
                await asyncio.sleep(settings.CHAIN_CHECK_INTERVAL_SEC)
            except asyncio.CancelledError:
                logger.info("chain_tracker_cancelled")
                raise
```

- [ ] **Step 2.5 — Run, expect 3 pass**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v -k "record_completion"`
Expected: 3 passed.

- [ ] **Step 2.6 — Run full chain regression**

Run: `uv run pytest tests/test_chains_tracker.py tests/test_chains_db.py tests/test_chains_learn.py tests/test_chain_outcomes_hydration.py -v`
Expected: all green (no regressions in existing 30+ chain tests).

- [ ] **Step 2.7 — Commit**

```bash
git add scout/chains/tracker.py tests/test_chain_outcomes_hydration.py
git commit -m "feat(BL-071a'): wire _record_completion to populate mcap_at_completion

For memecoin pipeline, _record_completion now fetches current FDV from
DexScreener via the injected mcap_fetcher (defaults to scout/chains/
mcap_fetcher.py:fetch_token_fdv). Captured value is stored in the
chain_matches.mcap_at_completion column shipped in Bundle A.

Narrative pipeline is intentionally skipped — token_id is a CoinGecko
slug, not a contract address; the DexScreener lookup would always fail.

run_chain_tracker creates a single aiohttp session for the loop and
threads it through check_chains -> _record_completion. Tests inject a
stub mcap_fetcher so they don't hit the network.

Failures are graceful: any exception leaves mcap_at_completion=NULL
and the chain_match is still written. The hydrator then falls back to
the legacy outcomes path."
```

---

## Task 3 — Replace silent skip in `update_chain_outcomes` with DexScreener fetch

**Files:**
- Modify: `scout/chains/tracker.py` (`update_chain_outcomes` — populated branch fetches current FDV, computes pct change, marks hit/miss)
- Modify: `scout/config.py` (add `CHAIN_OUTCOME_HIT_THRESHOLD_PCT: float = 50.0`)
- Test: `tests/test_chain_outcomes_hydration.py` (extend with hydrator+fetcher tests)

**Why:** Closes the silent-skip surface Bundle A intentionally left open. Once writers populate `mcap_at_completion`, the hydrator now does the actual outcome resolution.

- [ ] **Step 3.1 — Add config setting**

Edit `scout/config.py`. Find the chain-related settings block (search `CHAIN_`). Add:

```python
    CHAIN_OUTCOME_HIT_THRESHOLD_PCT: float = 50.0  # BL-071a': memecoin chain hit if (current_fdv/completion_fdv - 1)*100 >= this
```

- [ ] **Step 3.2 — Add failing tests for new hydrator behaviour**

Append to `tests/test_chain_outcomes_hydration.py`:

```python
@pytest.mark.asyncio
async def test_hydrator_resolves_memecoin_via_dexscreener_hit(db, monkeypatch, settings_factory):
    """BL-071a': memecoin chain_match with populated mcap_at_completion
    + current FDV >+50% → outcome_class='hit'."""
    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xwinner','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1000000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    async def _stub_fetcher(session, contract):
        assert contract == "0xwinner"
        return 2_000_000.0  # +100% vs 1M completion mcap

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    updated = await update_chain_outcomes(
        db, settings=s, session=None, mcap_fetcher=_stub_fetcher
    )
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class, outcome_change_pct FROM chain_matches WHERE token_id='0xwinner'"
    )
    row = await cur.fetchone()
    assert row[0] == "hit"
    assert row[1] == pytest.approx(100.0, rel=0.01)


@pytest.mark.asyncio
async def test_hydrator_resolves_memecoin_via_dexscreener_miss(db, monkeypatch, settings_factory):
    """+0% to +50% range → 'miss' (didn't clear the threshold)."""
    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xflat','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1000000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    async def _stub_fetcher(session, contract):
        return 1_200_000.0  # +20% — below 50% threshold

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    updated = await update_chain_outcomes(
        db, settings=s, session=None, mcap_fetcher=_stub_fetcher
    )
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xflat'"
    )
    assert (await cur.fetchone())[0] == "miss"


@pytest.mark.asyncio
async def test_hydrator_skips_on_dexscreener_failure(db, monkeypatch, settings_factory):
    """When DexScreener returns None at hydration time, the row stays
    UNRESOLVED — outcome_class is NOT updated. Re-tries next cycle."""
    captured = _capture_chain_logs(monkeypatch)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES ('0xunavail','memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, 1000000.0)""",
        (long_ago, long_ago),
    )
    await db._conn.commit()

    async def _none_fetcher(session, contract):
        return None  # simulate DS unavailable

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    updated = await update_chain_outcomes(
        db, settings=s, session=None, mcap_fetcher=_none_fetcher
    )
    assert updated == 0
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xunavail'"
    )
    assert (await cur.fetchone())[0] is None
    # WARNING log fired so operators see DS issues
    failed = [c for c in captured if c[1] == "chain_outcome_dexscreener_failed"]
    assert len(failed) >= 1


@pytest.mark.asyncio
async def test_hydrator_coupling_guard(db, monkeypatch, settings_factory):
    """BL-071a' coupling-guard test (per BL-071a' acceptance):
    after a hydration cycle, NO chain_match should exist with
    non-NULL mcap_at_completion AND outcome_class IS NULL AND
    completed_at < now-48h. This is the canary that detects if
    writer-wiring shipped without fetcher-wiring (or vice versa)."""
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    await db._conn.executemany(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost, outcome_class, mcap_at_completion)
           VALUES (?, 'memecoin', 1, 'p', 1, 2, ?, ?, 0.0, 0, NULL, ?)""",
        [
            ("0xpop1", long_ago, long_ago, 1_000_000.0),
            ("0xpop2", long_ago, long_ago, 500_000.0),
        ],
    )
    await db._conn.commit()

    async def _stub_fetcher(session, contract):
        return 2_000_000.0  # +100% / +300%

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    await update_chain_outcomes(
        db, settings=s, session=None, mcap_fetcher=_stub_fetcher
    )

    cur = await db._conn.execute(
        """SELECT COUNT(*) FROM chain_matches
           WHERE pipeline='memecoin'
             AND mcap_at_completion IS NOT NULL
             AND outcome_class IS NULL
             AND completed_at < datetime('now','-48 hours')"""
    )
    leftover = (await cur.fetchone())[0]
    assert leftover == 0, (
        f"BL-071a' coupling-guard FAILED: {leftover} memecoin chain_matches "
        f"have populated mcap_at_completion AND unresolved outcome_class. "
        f"This indicates writer-wiring shipped without DexScreener fetch (or "
        f"the fetcher silently returns None for everything)."
    )
```

- [ ] **Step 3.3 — Run, expect 4 fails**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v -k "dexscreener or coupling"`
Expected: TypeError on all 4 (`update_chain_outcomes` doesn't accept settings/session/mcap_fetcher kwargs yet).

- [ ] **Step 3.4 — Modify `update_chain_outcomes`**

Edit `scout/chains/tracker.py`. Replace the entire `update_chain_outcomes` function:

```python
async def update_chain_outcomes(
    db: Database,
    *,
    settings: Settings | None = None,
    session: aiohttp.ClientSession | None = None,
    mcap_fetcher: McapFetcher | None = None,
) -> int:
    """Hydrate chain_matches.outcome_class from downstream outcome tables.

    For each completed chain_match older than 48h with outcome_class NULL:
    * narrative pipeline → predictions.outcome_class (HIT/MISS/etc.)
    * memecoin pipeline → BL-071a': if mcap_at_completion populated, fetch
      current FDV via DexScreener and compute pct change; hit if change
      >= CHAIN_OUTCOME_HIT_THRESHOLD_PCT, miss otherwise.
      Else fall back to legacy outcomes table for back-compat.

    BL-071a' coupling-guard: populated mcap_at_completion rows MUST be
    resolvable (or surfaced via aggregate WARNING + aging-aware ERROR) —
    never silently skip.

    Defense-in-depth (R2-1, plan v2): if `session is None`, this function
    creates and closes its own aiohttp session for the cycle. Callers that
    don't have a session in scope (e.g., scout/narrative/learner.py:326
    LEARN cycle) get the BL-071a' resolution path automatically without
    needing to thread a session through. The injected-session path is
    preferred where available (avoids per-cycle connector setup overhead).

    Returns the number of rows updated. Designed for once-per-LEARN-cycle.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")

    hit_threshold_pct = (
        settings.CHAIN_OUTCOME_HIT_THRESHOLD_PCT if settings is not None else 50.0
    )
    persistent_failure_age_hours = (
        # 2 × CHAIN_CHECK_INTERVAL_SEC default (300s) → 10 minutes; in prod
        # the LEARN cycle runs daily-ish, so this gates on "stuck across
        # multiple LEARN cycles" not "stuck a single retry."
        max(
            2.0,
            (settings.CHAIN_CHECK_INTERVAL_SEC if settings is not None else 300) * 2 / 3600.0,
        )
    )
    fetcher = mcap_fetcher or fetch_token_fdv

    # Defense-in-depth: self-create session if not injected (R2-1)
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        return await _update_chain_outcomes_inner(
            conn, session, fetcher, hit_threshold_pct,
            persistent_failure_age_hours,
        )
    finally:
        if own_session and session is not None:
            await session.close()


async def _update_chain_outcomes_inner(
    conn,
    session,
    fetcher,
    hit_threshold_pct: float,
    persistent_failure_age_hours: float,
) -> int:
    """Inner body of update_chain_outcomes (split out so the session
    self-create wrapper stays small and the inner logic is testable)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    async with conn.execute(
        """SELECT id, token_id, pipeline, completed_at FROM chain_matches
           WHERE outcome_class IS NULL AND completed_at < ?""",
        (cutoff,),
    ) as cur:
        pending = await cur.fetchall()

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    updated = 0
    memecoin_unhydrateable = 0
    memecoin_ds_failures = 0
    memecoin_ds_attempts = 0
    persistent_stuck_count = 0
    oldest_persistent_age_hours = 0.0
    for row in pending:
        match_id = row["id"]
        token_id = row["token_id"]
        pipeline = row["pipeline"]
        outcome: str | None = None
        outcome_change_pct: float | None = None

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
            async with conn.execute(
                """SELECT mcap_at_completion FROM chain_matches WHERE id = ?""",
                (match_id,),
            ) as cur_m:
                mcap_row = await cur_m.fetchone()
            mcap_at_completion = mcap_row[0] if mcap_row else None

            if mcap_at_completion is not None and mcap_at_completion > 0:
                # BL-071a': active DexScreener resolution path. Note: NO
                # `session is not None` guard — the wrapper self-creates
                # one if needed (R2-1 defense-in-depth, plan v2). Also
                # tracks DS attempts for cycle-level health signal (R1-2).
                memecoin_ds_attempts += 1
                try:
                    current_fdv = await fetcher(session, token_id)
                except Exception:
                    logger.exception(
                        "chain_outcome_dexscreener_unexpected_error",
                        match_id=match_id,
                        token_id=token_id,
                    )
                    current_fdv = None
                if current_fdv is None or current_fdv <= 0:
                    memecoin_ds_failures += 1
                    # R1-3: DEBUG not WARNING — per-row WARNING was the
                    # antipattern Bundle A R2 flagged. Aggregate WARNING
                    # below covers operator visibility; aging-aware ERROR
                    # covers persistent stuck rows.
                    logger.debug(
                        "chain_outcome_dexscreener_failed",
                        match_id=match_id,
                        token_id=token_id,
                        mcap_at_completion=mcap_at_completion,
                    )
                    # R1-1: track persistent stuck rows for aging ERROR
                    completed_at_str = (
                        row["completed_at"] if isinstance(row, dict) else row[3]
                    )
                    try:
                        completed_at = datetime.fromisoformat(
                            completed_at_str.replace("Z", "+00:00")
                        )
                        if completed_at.tzinfo is None:
                            completed_at = completed_at.replace(tzinfo=timezone.utc)
                        age_hours = (now - completed_at).total_seconds() / 3600.0
                        if age_hours > persistent_failure_age_hours:
                            persistent_stuck_count += 1
                            if age_hours > oldest_persistent_age_hours:
                                oldest_persistent_age_hours = age_hours
                    except (ValueError, AttributeError):
                        pass  # malformed timestamp, skip aging check
                    continue  # leave row UNRESOLVED, retry next cycle
                outcome_change_pct = (
                    (current_fdv / mcap_at_completion) - 1.0
                ) * 100.0
                outcome = "hit" if outcome_change_pct >= hit_threshold_pct else "miss"
                logger.info(
                    "chain_outcome_resolved_via_dexscreener",
                    match_id=match_id,
                    token_id=token_id,
                    mcap_at_completion=mcap_at_completion,
                    current_fdv=current_fdv,
                    outcome_change_pct=round(outcome_change_pct, 2),
                    outcome=outcome,
                )
            else:
                # Fall back to legacy outcomes table (covers expired chains
                # and the pre-Bundle-A backlog of NULL-mcap rows)
                async with conn.execute(
                    """SELECT price_change_pct FROM outcomes
                       WHERE contract_address = ? AND price_change_pct IS NOT NULL
                       ORDER BY id DESC LIMIT 1""",
                    (token_id,),
                ) as cur2:
                    orow = await cur2.fetchone()
                if orow is not None and orow[0] is not None:
                    outcome_change_pct = float(orow[0])
                    outcome = "hit" if outcome_change_pct > 0 else "miss"
                else:
                    memecoin_unhydrateable += 1

        if outcome is None:
            continue

        await conn.execute(
            """UPDATE chain_matches
               SET outcome_class = ?, outcome_change_pct = ?, evaluated_at = ?
               WHERE id = ?""",
            (outcome, outcome_change_pct, now_iso, match_id),
        )
        updated += 1

    await conn.commit()
    if updated:
        logger.info("chain_outcomes_hydrated", count=updated)
    # BL-071a': aggregate warnings now distinguishable into two causes:
    # (1) memecoin_unhydrateable = legacy (NULL mcap, no outcomes-table row)
    # (2) memecoin_ds_failures = transient DS errors that may resolve next cycle
    if memecoin_unhydrateable:
        logger.warning(
            "chain_outcomes_unhydrateable_memecoin",
            total_unhydrateable=memecoin_unhydrateable,
            cause="legacy_no_mcap_no_outcomes_row",
            note=(
                "These rows pre-date BL-071a' writer wiring AND have no legacy "
                "outcomes-table data. Backfill follow-up captured in BL-071a' "
                "merge-and-deploy step (manual SQL one-shot)."
            ),
        )
    if memecoin_ds_failures:
        logger.warning(
            "chain_outcomes_ds_transient_failures",
            count=memecoin_ds_failures,
            cause="dexscreener_returned_no_data",
            note="Will retry next LEARN cycle.",
        )
    # R1-1: aging-aware ERROR for rows persistently stuck across cycles.
    # Triggers when a row has been NULL-due-to-DS-failure across multiple
    # LEARN cycles — operators see this and can investigate (DS API key
    # invalid, rate-limited, contract delisted, etc.).
    if persistent_stuck_count:
        logger.error(
            "chain_outcome_ds_persistent_failure",
            stuck_count=persistent_stuck_count,
            oldest_pending_age_hours=round(oldest_persistent_age_hours, 1),
            threshold_hours=round(persistent_failure_age_hours, 2),
            note=(
                "These memecoin chain_matches have populated mcap_at_completion "
                "but DexScreener has returned no FDV for >threshold cycles. "
                "Investigate: API key valid? rate-limited? contract delisted?"
            ),
        )
    # R1-2: cycle-level session health. If >50% of attempts failed
    # (with floor of >=3 attempts to avoid noise from low-volume cycles),
    # the long-lived session may be in a degraded state.
    if memecoin_ds_attempts >= 3:
        failure_rate = memecoin_ds_failures / memecoin_ds_attempts
        if failure_rate > 0.5:
            logger.error(
                "chain_tracker_session_unhealthy",
                attempts=memecoin_ds_attempts,
                failures=memecoin_ds_failures,
                failure_rate_pct=round(failure_rate * 100, 1),
                note=(
                    "DexScreener fetch failure rate exceeds 50% in this cycle. "
                    "Long-lived aiohttp session may be degraded; consider "
                    "service restart to reset connector pool."
                ),
            )
    return updated
```

- [ ] **Step 3.5 — Run, expect 4 pass**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v`
Expected: all green (10+ tests including the 4 new ones).

- [ ] **Step 3.6 — Add the post-BL-071a' resolved-via-fetcher test + keep the superseded Bundle A test as documentation**

The Bundle A test `test_hydrator_silent_skip_when_mcap_at_completion_populated` asserted the populated-column row was SILENTLY SKIPPED. With BL-071a', that row should now be RESOLVED (hit/miss). Per R2-4 plan-review feedback, **keep the superseded test as `@pytest.mark.skip` with reason** (preserves the invariant doc that "no session = falls back to legacy" still holds for future maintainers — defense-in-depth makes that case unreachable in practice but the documented behavior is still meaningful).

In `tests/test_chain_outcomes_hydration.py`, locate the existing `test_hydrator_silent_skip_when_mcap_at_completion_populated`. PRESERVE it but add the skip marker and rename to clarify:

```python
@pytest.mark.skip(
    reason=(
        "Superseded by BL-071a' (commit forthcoming): the silent-skip "
        "semantics are gone — populated mcap_at_completion is now actively "
        "resolved via DexScreener fetch (or self-created session if caller "
        "doesn't provide one). Test preserved as documentation of the "
        "Bundle A intermediate behaviour and as a guard if someone later "
        "removes the defense-in-depth session self-create."
    )
)
@pytest.mark.asyncio
async def test_hydrator_silent_skip_when_mcap_at_completion_populated_BUNDLE_A_BEHAVIOUR(db, monkeypatch):
    # ... existing body unchanged ...
```

Then add the NEW test that asserts the BL-071a' behaviour:

```python
@pytest.mark.asyncio
async def test_hydrator_resolves_populated_mcap_via_fetcher(db, monkeypatch, settings_factory):
    """BL-071a' supersedes the Bundle A 'silent-skip' test: populated
    mcap_at_completion is now actively resolved via DexScreener fetch.
    The silent-skip path is gone."""
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

    async def _stub_fetcher(session, contract):
        return 3_000_000.0  # +100%

    s = settings_factory(CHAIN_OUTCOME_HIT_THRESHOLD_PCT=50.0)
    updated = await update_chain_outcomes(
        db, settings=s, session=None, mcap_fetcher=_stub_fetcher
    )
    assert updated == 1
    cur = await db._conn.execute(
        "SELECT outcome_class FROM chain_matches WHERE token_id='0xdeadbeef'"
    )
    assert (await cur.fetchone())[0] == "hit"
```

Replace the Bundle A test (`test_hydrator_silent_skip_when_mcap_at_completion_populated`) with the above. The "silent skip" semantics no longer exist — the test now asserts the new resolved-via-fetcher behaviour.

- [ ] **Step 3.7 — Run all chain hydration tests**

Run: `uv run pytest tests/test_chain_outcomes_hydration.py -v`
Expected: 10+ pass, none fail.

- [ ] **Step 3.8 — Commit**

```bash
git add scout/chains/tracker.py scout/config.py tests/test_chain_outcomes_hydration.py
git commit -m "feat(BL-071a'): close silent-skip surface — DexScreener fetch in hydrator

update_chain_outcomes now accepts settings + session + mcap_fetcher kwargs.
For memecoin chains with populated mcap_at_completion, fetches current
FDV from DexScreener and computes pct change vs the completion snapshot.
Hit if pct change >= CHAIN_OUTCOME_HIT_THRESHOLD_PCT (default 50.0,
configurable via env).

DexScreener failures are logged as WARNING (chain_outcome_dexscreener_
failed) and the row stays UNRESOLVED for retry next cycle — never
silently swallowed.

The Bundle A 'silent-skip' test is replaced with a 'resolves-via-
fetcher' test (the silent skip semantics are gone). Includes the
BL-071a' acceptance coupling-guard test that asserts no chain_match
has populated mcap AND unresolved outcome AND age > 48h after a
hydration cycle.

Aggregate warning split into two distinguishable causes:
- chain_outcomes_unhydrateable_memecoin (legacy NULL-mcap backlog)
- chain_outcomes_ds_transient_failures (re-tryable)"
```

---

## Task 4 — Wire `update_chain_outcomes` caller (LEARN cycle) to pass settings

**v2 CRITICAL FIX (R2-1):** The original plan v1 targeted `scout/main.py`. **There is no `update_chain_outcomes` caller in main.py.** The actual caller is `scout/narrative/learner.py:326`. v1's grep would have returned zero results and the implementer would have shipped Task 4 as a no-op — leaving BL-071a' dead-on-arrival in production. v2 corrects this AND removes the session-wiring requirement entirely (defense-in-depth in Task 3 means the hydrator self-creates a session if none is passed; Task 4 only needs to pass `settings` for the threshold).

**Files:**
- Modify: `scout/narrative/learner.py:326` (one-line change)

**Why:** Pass `settings` so the hit threshold is honored. Session is no longer required (hydrator self-creates).

- [ ] **Step 4.1 — Verify the call site (sanity check)**

Run: `cd C:/projects/gecko-alpha && grep -n "update_chain_outcomes" scout/narrative/learner.py`
Expected: line 322 (import) and line 326 (await call).

Read context: `scout/narrative/learner.py` lines 315-330. Confirm the function this lives inside has `settings = get_settings()` already in scope at line ~319 (it does — verified during recon).

- [ ] **Step 4.2 — Update the call**

Edit `scout/narrative/learner.py`. Find line 326:

```python
await update_chain_outcomes(db)
```

Replace with:

```python
await update_chain_outcomes(db, settings=settings)
```

The hydrator self-creates an aiohttp session if `session is None` (Task 3 defense-in-depth), so we don't need to thread a session through the learner. Just pass settings for the threshold + persistent-failure timing.

- [ ] **Step 4.3 — Run full chain + heartbeat test suite to catch any regression**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_chains_tracker.py tests/test_chains_db.py tests/test_chains_learn.py tests/test_chain_outcomes_hydration.py tests/test_heartbeat.py tests/test_heartbeat_mcap_missing.py tests/test_chain_mcap_fetcher.py -q --tb=line`
Expected: all green or skipped. Existing `test_chains_learn.py` callers pass `db` only (no kwargs) and exercise the NARRATIVE path (which doesn't depend on session/fetcher), so they keep passing.

- [ ] **Step 4.4 — Commit**

```bash
git add scout/narrative/learner.py
git commit -m "feat(BL-071a'): wire LEARN cycle to pass settings to update_chain_outcomes

Pass settings (for CHAIN_OUTCOME_HIT_THRESHOLD_PCT + persistent-failure
threshold) to the hydrator. Session is NOT threaded through — the
hydrator self-creates an aiohttp session if none is provided (Task 3
defense-in-depth), so callers without a session in scope (like this
LEARN cycle in narrative/learner.py) get the BL-071a' resolution path
without needing to refactor the call chain.

Plan v1 incorrectly targeted scout/main.py for this wiring; the actual
caller lives in scout/narrative/learner.py:326. R2-1 plan-review fix."
```

---

## Final integration

- [ ] **Step F.1 — Full chain + heartbeat test suite**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_chains_tracker.py tests/test_chains_db.py tests/test_chains_learn.py tests/test_chain_outcomes_hydration.py tests/test_heartbeat.py tests/test_heartbeat_mcap_missing.py tests/test_chain_mcap_fetcher.py -q`
Expected: all green or skipped.

- [ ] **Step F.2 — Format**

Run: `uv run black scout/chains/ scout/main.py scout/config.py tests/test_chain_outcomes_hydration.py tests/test_chain_mcap_fetcher.py`

- [ ] **Step F.3 — Commit formatting if any**

```bash
git add -u
git commit -m "style: black formatting"
```

---

## Self-Review (post-v2 edits)

1. **Scope coverage:**
   - Writer wiring → Task 2 ✓
   - DexScreener fetch in hydrator → Task 3 ✓
   - Coupling-guard test → Task 3 Step 3.2 (`test_hydrator_coupling_guard`) ✓
   - Aggregate warning + per-cause + persistent-failure ERROR + session-health ERROR → Task 3 Step 3.4 (v2) ✓
   - Helper module → Task 1 ✓
   - Caller wiring (LEARN cycle, **learner.py not main.py**) → Task 4 (v2) ✓
2. **Placeholder scan:** none — all code shown verbatim ✓
3. **New primitives marker:** present at top (helper module + 5 log events declared, updated in v2) ✓
4. **TDD discipline:** failing-test → minimal-impl → passing-test → commit per task ✓
5. **No cross-task coupling:** Tasks 1 (helper), 2 (writer), 3 (hydrator), 4 (wiring) touch different files; could be reverted independently ✓
6. **Honest scope (v2):** explicitly deferred to BL-071a'' captured at top — pre-fetch FDVs outside transaction (R2-3), misrouted-pipeline detection (R1-4), pre-Bundle-A backlog backfill (R1-6 — handled as manual SQL one-shot during merge-and-deploy instead).
7. **All MUST-FIX from review addressed in v2:**
   - R2-1 ✓ (Task 4 targets learner.py + hydrator self-creates session)
   - R1-3 ✓ (per-row WARNING demoted to DEBUG)
   - R1-1 ✓ (aging-aware `chain_outcome_ds_persistent_failure` ERROR)
8. **All actionable SHOULD-FIX from review addressed in v2:**
   - R1-2 ✓ (`chain_tracker_session_unhealthy` ERROR at >50% failure rate)
   - R2-4 ✓ (superseded test kept with `@pytest.mark.skip`)
   - R2-2 ✓ (doc note in v2 scope-decision: existing learner tests don't validate new path)

## Manual SQL backfill follow-up (post-merge, deferred from R1-6)

After BL-071a' is merged + deployed and verified working on NEW chain completions, one-shot SQL on VPS to clear the pre-Bundle-A backlog of 30 stuck memecoin rows:

```sql
-- Run AFTER BL-071a' is verified clean for ≥1 LEARN cycle
-- Marks pre-existing NULL-mcap memecoin rows as 'expired_no_data' so the
-- aggregate warning stops firing for them. Does NOT generate fake outcomes.
UPDATE chain_matches
   SET outcome_class = 'expired_no_data',
       evaluated_at = datetime('now')
 WHERE pipeline = 'memecoin'
   AND mcap_at_completion IS NULL
   AND outcome_class IS NULL
   AND completed_at < datetime('now', '-7 days');
```

This is a one-shot data fix, NOT part of the BL-071a' code change. Documented here so it doesn't get lost. After this, the only chain_outcomes_unhydrateable_memecoin warnings should be from genuinely-new-but-unfetcheable rows.
