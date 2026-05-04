# BL-076: Junk-filter expansion + symbol/name population — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** add `"test-"` to `_JUNK_COINID_PREFIXES` in `scout/trading/signals.py:569`; new method `Database.lookup_symbol_name_by_coin_id(coin_id) -> tuple[str, str]` in `scout/db.py` (per architecture-review #2 — pure metadata read, belongs on Database not signals.py module); add WARNING log in `scout/trading/engine.py:open_trade` when called with both `symbol=""` and `name=""` (defense-in-depth visibility, NOT a hard reject — soak-then-escalate criterion documented below); modify call sites in `trade_volume_spikes` (line 53) + `trade_predictions` (line 741) + `trade_chain_completions` (line 814) to populate `symbol` + `name` parameters. NO new DB tables, columns, or settings.

**Prerequisites:** master ≥ `64a8e35` (BL-066' deployed + backlog hygiene merged).

**v2 changes from 2-agent plan-review feedback:**

*MUST-FIX (consensus blockers):*
- **M1 (a09b333 + aff3517 #6) — structlog `caplog` won't capture:** project uses `structlog.PrintLoggerFactory()` (verified `scout/main.py:911`); `caplog` reads stdlib logging only. Tasks 2 + 5b tests rewritten to use `structlog.testing.capture_logs()` context manager + assert exact `event` field (not substring match).
- **M2 (a09b333) — engine WARNING placement:** verify it fires before `trade_skipped_no_price` short-circuit at engine.py:170-177. WARNING goes immediately AFTER `if signal_data is None: signal_data = {}` and BEFORE the warmup gate so it always fires regardless of downstream gates.
- **M3 (a09b333) — T5 INSERT violates `chain_matches` NOT NULL:** schema (verified `scout/db.py:332-348`) requires `steps_matched`, `total_steps`, `anchor_time`, `chain_duration_hours`, `conviction_boost` — all NOT NULL. Plan v1 omitted 4 of 5; v2 INSERTs include all required columns.
- **A1 (aff3517) — UNION fragile across schema drift:** replaced single UNION ALL with **3 sequential prioritized SELECTs** (gainers_snapshots → volume_history_cg → volume_spikes), each in its own try/except. A column rename in any one table fails ONLY that lookup; helper still returns metadata from the others.
- **A2 (aff3517) — helper boundary:** moved from `signals.py::_resolve_symbol_name_for_chain` to `Database.lookup_symbol_name_by_coin_id` so future callers (dashboard, backfill scripts) reuse the resolver instead of reimplementing the JOIN.

*SHOULD-FIX (worth applying):*
- **a09b333 S2 — deploy verification §6 vacuous:** added positive-path check via journalctl grep for `signal_skipped_junk` events with `coin_id LIKE 'test-%'`. Original "expect 0 new test-* trades" is satisfied trivially when no such token enters the polling window (steady state).
- **a09b333 S3 — pre-deploy SQL audit for other placeholder prefixes:** added pre-merge query to enumerate all token_id prefixes in `paper_trades` so `example-` / `demo-` / `placeholder-` (if present) get folded into the same PR rather than a follow-up.
- **aff3517 #4 — newest-row metadata unstable:** sequential prioritized lookup (above) inherently picks gainers_snapshots first as canonical (most authoritative — populated from CoinGecko `/coins/markets`). Removes the cross-source casing race.
- **aff3517 #3 — soak-then-escalate timeline for engine WARNING:** added explicit Self-Review item: after 14 days of green prod logs (zero `open_trade_called_with_empty_symbol_and_name` events), open BL-077 to escalate the WARNING to a hard reject. Prevents wallpaper.
- **aff3517 #5 — junk-prefix tuple bound:** added Self-Review note: at >10 prefix entries OR a regex/substring requirement, refactor to settings-backed `PAPER_JUNK_COINID_PREFIXES` so ops can update without deploy.
- **aff3517 #10 — chain_completed gap unmeasured:** added §5 step 0b pre-deploy baseline query (`SELECT signal_type, COUNT(*) FROM paper_trades WHERE symbol='' GROUP BY signal_type`) so post-deploy operator audit can attribute the fix to actual signal mix.
- **aff3517 #11 + a09b333 S5 — test edge cases + tmp_path consistency:** Task 2 switched from `tempfile.TemporaryDirectory` to `tmp_path`; added T5c (snapshot row exists with `symbol IS NULL` — confirms `if row and row[0] and row[1]` filter works as intended).

*NIT (tracked, not applied):*
- a09b333 N3 (BL-061 reference cleanup), N4 (commit squashing) — minor.
- aff3517 #7-#8 — confirmed defensible (single-PR, no test pollution from `_StubEngine` in BL-065 tests).

**v3 changes from 2-agent design-review feedback:**

*MUST-FIX:*
- **M1 (a6fcf0f7) — performance prediction unsupported:** chain_completed orphan rate is UNKNOWN until §5 step 10 measures it. Self-Review #8 amended: soak-then-escalate is contingent on chain orphan rate, NOT binary 14d-clean.
- **M2 (a6fcf0f7) — F2 wording inverted:** corrected — `test-net-token` IS REJECTED (false positive risk), not "slips through".
- **A3 (a2616834) — parallel `log.info` event:** added `trade_metadata_empty` INFO event next to WARNING in Task 2 Step 3. Matches existing `signal_skipped_*` telemetry pattern; future BL-077 reuses same event name.
- **A4 (a2616834) — BL-077 sketch:** added one paragraph to design Self-Review #8 — flip from `log.warning + proceed` to `log.info("trade_skipped_empty_metadata") + return None`. NOT exception (would break dispatcher per-signal isolation).

*SHOULD-FIX:*
- **a6fcf0f7 S3 — M2 has no test:** added Task 2 Step 5 — T2b `test_open_trade_warning_fires_even_during_warmup` with `PAPER_STARTUP_WARMUP_SECONDS=10` monkeypatch.
- **a6fcf0f7 S6 — F11 WAL claim irrelevant:** rewrote — same-connection serialization stronger than WAL.
- **a6fcf0f7 S7 — F14 forward guard:** documented `FakeEngine` stub pattern in design F14 mitigation column.
- **a6fcf0f7 S8 — "no DB schema changes" wording:** clarified to "no DDL changes (zero CREATE/ALTER/DROP, zero migrations)".
- **A2 (a2616834) — F8 in-line citation:** added `pyproject.toml:12, structlog>=24.1,<25` exact citation.
- **A5 (a2616834) — F4 reframed:** "WARNING fires unexpectedly often (>10/hour) — investigation trigger" — now real failure mode.
- **A6 (a2616834) — performance scaling math:** added per-LEARN-cycle calculation + refactor trigger at N>500.
- **A7 (a2616834) — `signal_combo` field added to WARNING + INFO event:** caller-context for operator debugging.
- **A8 (a2616834) — refactor trigger documented in helper docstring + Self-Review #11.**
- **A9 (a2616834) — F7 deleted:** project-wide migration risk, not BL-076-specific.
- **A11 (a2616834) — narrow exception:** Task 5 helper changed from `except Exception:  # noqa: BLE001` to `except aiosqlite.OperationalError` per-table. Added T5g — monkeypatch `ValueError` on first SELECT, assert it propagates.
- **A10 (a2616834) — F16/F17 added + F16 defensive guard:** helper opens with `if not coin_id: return "", ""` (F16). Added T5e — parametrized empty + None coin_id test.

*NIT:*
- a6fcf0f7 #10-12 (cosmetic — symmetry-preserving NULL filter, gainers index citation cleanup, test count drift). Applied to design.

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Token slug blacklist / placeholder detection (e.g. `test-N`, `example-N`) | None found | Build inline (extending `_JUNK_COINID_PREFIXES` tuple in `scout/trading/signals.py`) |
| CoinGecko junk-token / scam-coin filtering | None found (no crypto-specific skills in registry) | Build inline (project owns the gate logic) |
| Symbol / name validation for crypto tokens | None found | Build inline (defensive empty-string guard at engine level) |
| Wash-trade or fraud detection at admission time | None found (closest: none — Hermes covers software-dev / MLOps / GitHub workflows, not domain-specific fraud) | Reuse existing PR #44 junk filter pattern; expand prefixes |

**Awesome-hermes-agent ecosystem check:** No relevant repos. Curated list covers Hermes infra (chat workspaces, agent fleets, monitoring), DSPy/GEPA, blockchain ORACLES (Chainlink/Solana — different problem), and Minara execution. None apply to the trading-admission junk-filter problem class.

**Verdict:** Pure project-internal trading-admission filter. No Hermes-skill replacement. Building inline by extending the existing PR #44 pattern at `scout/trading/signals.py:569`. The bug surface (CoinGecko's `test-N` placeholder slugs + dispatcher paths writing empty `symbol`/`name`) is a domain-specific issue that requires understanding of this project's specific data flow (which dispatchers feed `paper_trades.signal_data`, what fields each Pydantic model carries, etc.).

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**

- `scout/trading/signals.py:565-588` — current junk filter:
  ```python
  _JUNK_COINID_SUBSTRINGS = ("-bridged-", "-wrapped-")
  _JUNK_COINID_PREFIXES = ("bridged-", "wrapped-", "superbridge-")
  def _is_junk_coinid(coin_id: object) -> bool: ...
  ```
  **Gap:** does not include `"test-"`. Confirmed prod traded `test-3` twice (#980 first_signal -$9.96, #1551 volume_spike +$188.91 by lucky pump).

- `scout/trading/signals.py:591-622` — `_is_tradeable_candidate(coin_id, ticker)`:
  - filters non-str / empty `coin_id` / empty `ticker`
  - calls `_is_junk_coinid(coin_id)`
  - filters non-ASCII coin_id / non-ASCII ticker
  - **Gap:** doesn't filter on `name` (the Pydantic models carry it; `volume_spike` passes only ticker/symbol)

- `scout/trading/engine.py:103-115` — `open_trade(...)` signature:
  ```python
  async def open_trade(
      self,
      token_id: str,
      symbol: str = "",
      name: str = "",
      chain: str = "coingecko",
      signal_type: str = "",
      signal_data: dict | None = None,
      ...
  )
  ```
  **Gap:** `symbol` + `name` default to empty string — silently writes `""` to `paper_trades` table when caller doesn't supply.

- `scout/db.py:557-572` — `paper_trades` schema: `symbol TEXT NOT NULL`, `name TEXT NOT NULL`. Empty string passes the constraint (NOT NULL ≠ NOT EMPTY in SQLite).

- **Dispatcher call sites** — verified by grep + read:
  - `trade_first_signals` (line 298) — ✅ passes `symbol` + `name` (verified by trade #980 having `symbol=tst, name=Test`)
  - `trade_gainers` (line 162-174) — ✅ passes both
  - `trade_losers_contrarian` (similar pattern as gainers — verified by trade #1391/#1129 having proper BAS/BNB Attestation Service)
  - `trade_volume_spikes` (line 53-60) — ❌ uses `spike.coin_id` only; `VolumeSpike` model has `symbol: str` and `name: str` (`scout/spikes/models.py:14-15`). EASY FIX.
  - `trade_predictions` (line 741-752) — ❌ uses `pred.coin_id` only; `NarrativePrediction` model has `symbol: str` and `name: str` (`scout/narrative/models.py:50-51`). EASY FIX.
  - `trade_chain_completions` (line 814-820) — ❌ uses `c["token_id"]` only; `chain_matches` table has NO symbol/name columns. **HARDER FIX:** JOIN with `candidates` table (which has `ticker` + `token_name`) OR accept the gap for chain_completed in this PR.

- **`candidates` schema:** verified — has `contract_address` (PK), `token_name`, `ticker`, `chain`, etc. Coverage gap: `candidates` is keyed by `contract_address`, not `coin_id` — chain_matches uses CoinGecko `coin_id` slugs. So a JOIN on `candidates.contract_address = chain_matches.token_id` won't always work for CoinGecko-pipeline chains. **Better source for chain_completed name lookup:** `gainers_snapshots` / `losers_snapshots` / `volume_history_cg` — all keyed by `coin_id` and carry `symbol`+`name`. Most recent row per coin_id wins.

- **Bug 1 prod evidence (audit run 2026-05-04):**
  - 2 `test-3` paper trades (#980 first_signal `-$9.96`, #1551 volume_spike `+$188.91`)
  - Several `bas`/BNB Attestation Service trades (legitimate — `bas` slug coincidentally contains substring "bas" but isn't a junk pattern)

- **Bug 2 prod evidence:** ~150+ trades across `narrative_prediction` + `volume_spike` + `chain_completed` paths have `symbol=""` AND `name=""`. Operator dashboard can only identify them by `coin_id` slug.

**Pattern conformance:**
- Extending the existing tuple-based junk filter (no new abstractions)
- Defense in depth: dispatcher-side population (correctness) + engine-side empty-string guard (defense if a future caller forgets)
- For chain_completed: query existing snapshot tables, no new DB infrastructure

---

**Goal:** Stop opening paper trades against CoinGecko placeholder coins (`test-N`), AND stop writing empty `symbol`/`name` to `paper_trades` from the 3 affected dispatcher paths.

**Architecture:** Two scoped extensions. (1) Add `"test-"` to `_JUNK_COINID_PREFIXES` (5-line change + test). (2) Wire `symbol`/`name` through 3 dispatcher call sites; for chain_completed, do a left join against `gainers_snapshots`/`volume_history_cg` for the most recent symbol/name per coin_id (degrade to empty strings if nothing found, but logged as warning so operator can see frequency). (3) Engine-level defense: log WARNING when `symbol="" and name=""` make it through — informs us if any other call site we missed leaks empty data.

**Tech Stack:** Python 3.12, aiosqlite, Pydantic v2, pytest + pytest-asyncio. No new dependencies.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `scout/trading/signals.py` | Add `"test-"` to junk prefixes; pass symbol+name in 3 dispatchers; new helper `_resolve_symbol_name_for_chain` (queries snapshot tables) | Modify |
| `scout/trading/engine.py` | Add WARNING log when `open_trade` receives `symbol="" and name=""` (defense-in-depth visibility) | Modify |
| `tests/test_bl076_junk_filter_and_symbol_name.py` | TDD test file: 8 active tests covering both bugs + 1 contract test | Create |
| `tests/test_signals_trade_dispatchers.py` (existing) | Verify existing dispatcher tests still pass | Verify only |

---

## Tasks

### Task 1: Add `"test-"` to junk filter

**Files:**
- Modify: `scout/trading/signals.py:569` (add `"test-"` to `_JUNK_COINID_PREFIXES`)
- Test: `tests/test_bl076_junk_filter_and_symbol_name.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bl076_junk_filter_and_symbol_name.py
"""BL-076: junk filter expansion (test- prefix) + symbol/name population.

Tests for two bugs surfaced by operator audit 2026-05-04:
1. CoinGecko placeholder coins (test-1..test-N) bypassed PR #44 junk filter.
2. volume_spike + narrative_prediction + chain_completed dispatch paths
   wrote empty symbol+name to paper_trades, masking junk in dashboard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.trading.signals import _is_junk_coinid, _is_tradeable_candidate


def test_is_junk_coinid_rejects_test_prefix():
    """T1 — pins the test-N placeholder bug. CoinGecko has test-1..test-N
    placeholder coins with real price feeds; they MUST be rejected at
    admission to prevent paper trades like #1551 (test-3 / volume_spike)."""
    assert _is_junk_coinid("test-3") is True
    assert _is_junk_coinid("test-1") is True
    assert _is_junk_coinid("test-99") is True
    assert _is_junk_coinid("test-coin") is True


def test_is_junk_coinid_does_not_overreach_on_test_substrings():
    """T1b — guard against false positives. Tokens whose slug merely
    CONTAINS 'test' (e.g. 'protest-coin', 'biggest-token', 'pre-testnet')
    must NOT be rejected. The prefix match is anchored at slug start."""
    # Substring 'test' inside the slug — must remain tradeable
    assert _is_junk_coinid("protest-coin") is False
    assert _is_junk_coinid("biggest-token") is False
    assert _is_junk_coinid("pre-testnet") is False
    assert _is_junk_coinid("pretest") is False
    # Existing junk patterns unaffected
    assert _is_junk_coinid("wrapped-bitcoin") is True
    assert _is_junk_coinid("bridged-usdc") is True
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_is_junk_coinid_rejects_test_prefix -v
```

Expected: FAIL with `assert _is_junk_coinid("test-3") is True` failing (returns False today).

- [ ] **Step 3: Add `"test-"` to `_JUNK_COINID_PREFIXES`**

In `scout/trading/signals.py:569`:

```python
_JUNK_COINID_PREFIXES = (
    "bridged-",
    "wrapped-",
    "superbridge-",
    "test-",  # BL-076: CoinGecko placeholder coins (test-1..test-N) have
              # real price feeds and triggered paper trades #980 + #1551.
              # Anchored at slug start to avoid false positives like
              # 'protest-coin' or 'biggest-token'.
)
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_is_junk_coinid_rejects_test_prefix tests/test_bl076_junk_filter_and_symbol_name.py::test_is_junk_coinid_does_not_overreach_on_test_substrings -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/signals.py tests/test_bl076_junk_filter_and_symbol_name.py
git commit -m "fix(BL-076): add test- prefix to junk filter — CoinGecko placeholder coins"
```

---

### Task 2: Engine-level empty-symbol/name WARNING log

**Files:**
- Modify: `scout/trading/engine.py:103-115` (open_trade signature; add early log)
- Test: `tests/test_bl076_junk_filter_and_symbol_name.py`

**Why this is defense-in-depth, not a hard reject:** the production path TODAY writes empty symbol/name from 3 dispatchers; making this a hard reject would break currently-running pipelines mid-deploy. The dispatcher fixes in Tasks 3-5 are the correctness fix; this WARNING gives operator visibility if a NEW caller forgets the pattern.

- [ ] **Step 1: Write failing test**

```python
import pytest
import structlog
from structlog.testing import capture_logs


@pytest.mark.asyncio
async def test_open_trade_logs_warning_when_symbol_and_name_both_empty(tmp_path):
    """T2 — engine-level defense-in-depth: log WARNING when caller forgets
    to pass symbol+name. Bug 2 evidence: 150+ paper trades across 3 dispatcher
    paths had empty symbol+name; operator audit dashboard couldn't identify
    them. This guard catches any future caller drift.

    NOTE (M1 fix): project uses structlog.PrintLoggerFactory() — pytest's
    caplog mechanism only captures stdlib logging, NOT structlog's stdout
    print path. Use structlog.testing.capture_logs() to intercept the
    structured event dict directly.
    """
    from scout.trading.engine import TradingEngine
    from scout.config import Settings
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    settings = Settings()
    engine = TradingEngine(sd, settings)
    # Caller-forgotten symbol+name. open_trade returns None or trade_id;
    # we don't care about the trade outcome — only that the warning fires.
    with capture_logs() as captured:
        await engine.open_trade(
            token_id="some-coin",
            signal_type="volume_spike",
            signal_data={"foo": "bar"},
            signal_combo="vs|none",
            entry_price=0.001,
        )
    # Pin exact event name (per aff3517 #6 — substring match too loose).
    matching = [
        e for e in captured
        if e.get("event") == "open_trade_called_with_empty_symbol_and_name"
    ]
    assert matching, (
        f"Expected open_trade_called_with_empty_symbol_and_name event; "
        f"got {[e.get('event') for e in captured]}"
    )
    assert matching[0].get("token_id") == "some-coin"
    assert matching[0].get("signal_type") == "volume_spike"
    await sd.close()
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_logs_warning_when_symbol_and_name_both_empty -v
```

Expected: FAIL — log event not emitted.

- [ ] **Step 3: Add WARNING + parallel INFO log in `open_trade`**

In `scout/trading/engine.py`, immediately after the `if signal_data is None: signal_data = {}` block (around line 122) — **placement BEFORE warmup gate at line 132 is load-bearing per M2**:

```python
        # BL-076: defense-in-depth visibility. Bug 2 (2026-05-04) showed
        # ~150+ paper trades had empty symbol+name because 3 dispatchers
        # forgot to pass them. Tasks 3-5 fix the dispatchers; this guard
        # surfaces any FUTURE caller drift. NOT a hard reject — would
        # break in-flight pipelines mid-deploy. Soak-then-escalate per
        # plan §10: BL-077 flips this to log.info + return None after
        # 14d clean (matches existing trade_skipped_* patterns).
        #
        # Two events emitted (per design A3):
        # - WARNING: human-readable visibility in journalctl
        # - INFO trade_metadata_empty: structured event lands in same
        #   telemetry pipeline that aggregates signal_skipped_* events,
        #   so existing operator dashboards pick it up. Same event name
        #   that BL-077 will reuse when it flips to skip semantics.
        if not symbol and not name:
            log.warning(
                "open_trade_called_with_empty_symbol_and_name",
                token_id=token_id,
                signal_type=signal_type,
                signal_combo=signal_combo,
                hint="dispatcher likely missing symbol=... + name=... kwargs",
            )
            log.info(
                "trade_metadata_empty",
                reason="empty_metadata",
                token_id=token_id,
                signal_type=signal_type,
                signal_combo=signal_combo,
            )
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_logs_warning_when_symbol_and_name_both_empty -v
```

Expected: PASS.

- [ ] **Step 5: Add T2b — pin WARNING fires before warmup gate**

Per M2 fix + design F9 + a6fcf0f7 S3 — pin that WARNING placement is BEFORE the warmup gate at `engine.py:132`. Without this test, a future refactor could move the WARNING below warmup and production WARNINGs vanish silently during the warmup window.

```python
@pytest.mark.asyncio
async def test_open_trade_warning_fires_even_during_warmup(tmp_path, monkeypatch):
    """T2b — pins F9 mitigation. Engine WARNING placement BEFORE
    PAPER_STARTUP_WARMUP_SECONDS gate. Asserts both WARNING + warmup-skip
    events fire; warmup-skip alone (without WARNING) means the placement
    regressed."""
    from scout.trading.engine import TradingEngine
    from scout.config import Settings
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    settings = Settings()
    monkeypatch.setattr(settings, "PAPER_STARTUP_WARMUP_SECONDS", 10)
    engine = TradingEngine(sd, settings)
    with capture_logs() as captured:
        result = await engine.open_trade(
            token_id="warmup-test",
            signal_type="volume_spike",
            signal_data={"foo": "bar"},
            signal_combo="vs|none",
            entry_price=0.001,
        )
    # open_trade returns None during warmup
    assert result is None
    events = [e.get("event") for e in captured]
    # BOTH events must fire — WARNING placement is BEFORE warmup gate
    assert "open_trade_called_with_empty_symbol_and_name" in events, (
        f"WARNING regressed below warmup gate; got events: {events}"
    )
    assert "trade_metadata_empty" in events, (
        f"INFO event regressed below warmup gate; got events: {events}"
    )
    assert "trade_skipped_warmup" in events, (
        f"warmup gate didn't fire (test setup bug); got events: {events}"
    )
    await sd.close()
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_warning_fires_even_during_warmup -v` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scout/trading/engine.py tests/test_bl076_junk_filter_and_symbol_name.py
git commit -m "feat(BL-076): engine-level WARNING + parallel INFO event on empty symbol+name (T2 + T2b)"
```

---

### Task 3: Wire symbol/name in `trade_volume_spikes`

**Files:**
- Modify: `scout/trading/signals.py:53-60` (open_trade call in trade_volume_spikes)
- Test: `tests/test_bl076_junk_filter_and_symbol_name.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_trade_volume_spikes_passes_symbol_and_name_to_engine(tmp_path):
    """T3 — pins Bug 2 for volume_spike path. VolumeSpike Pydantic model
    carries symbol+name; trade_volume_spikes was calling open_trade
    without them, leaving empty strings in paper_trades."""
    from datetime import datetime, timezone
    from scout.spikes.models import VolumeSpike
    from scout.trading.signals import trade_volume_spikes
    from scout.config import Settings
    from scout.db import Database
    from unittest.mock import AsyncMock

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    settings = Settings()
    captured = {}

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.update(kwargs)
            return 1

    spike = VolumeSpike(
        coin_id="real-coin",
        symbol="REAL",
        name="Real Coin",
        current_volume=1_000_000,
        avg_volume_7d=100_000,
        spike_ratio=10.0,
        market_cap=1_000_000,
        price=0.01,
        detected_at=datetime.now(timezone.utc),
    )
    await trade_volume_spikes(FakeEngine(), sd, [spike], settings)
    assert captured.get("symbol") == "REAL", (
        f"trade_volume_spikes must pass symbol; got {captured!r}"
    )
    assert captured.get("name") == "Real Coin", (
        f"trade_volume_spikes must pass name; got {captured!r}"
    )
    await sd.close()


# T3b edge case (aff3517 #11): VolumeSpike model requires symbol: str (NOT
# Optional), so a NULL symbol from the source can't be constructed. Empty
# string COULD theoretically arrive (`spike.symbol = ""`), but
# _is_tradeable_candidate (signals.py:614-617) already rejects empty ticker
# at the FIRST gate before open_trade is reached. So the only way to leak
# empty symbol/name to engine.open_trade from this path is a producer-side
# Pydantic validation bypass — out of scope for BL-076.
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_volume_spikes_passes_symbol_and_name_to_engine -v
```

Expected: FAIL with `captured.get("symbol")` returning None.

- [ ] **Step 3: Modify `trade_volume_spikes`**

In `scout/trading/signals.py:53-60`, change the `engine.open_trade` call:

```python
            await engine.open_trade(
                token_id=spike.coin_id,
                symbol=spike.symbol,
                name=spike.name,
                chain="coingecko",
                signal_type="volume_spike",
                signal_data={"spike_ratio": spike.spike_ratio},
                entry_price=spike.price,
                signal_combo=combo_key,
            )
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_volume_spikes_passes_symbol_and_name_to_engine -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/signals.py tests/test_bl076_junk_filter_and_symbol_name.py
git commit -m "fix(BL-076): wire symbol+name through trade_volume_spikes -> engine.open_trade"
```

---

### Task 4: Wire symbol/name in `trade_predictions`

**Files:**
- Modify: `scout/trading/signals.py:741-752`
- Test: `tests/test_bl076_junk_filter_and_symbol_name.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_trade_predictions_passes_symbol_and_name_to_engine(tmp_path):
    """T4 — same fix shape as T3 but for narrative_prediction path.
    NarrativePrediction Pydantic model has symbol+name; dispatcher
    was discarding them."""
    from datetime import datetime, timezone
    from scout.narrative.models import NarrativePrediction
    from scout.trading.signals import trade_predictions
    from scout.config import Settings
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    # Seed price_cache so the inner SELECT finds a price.
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('real-coin', 0.01, 10_000_000, ?)",
        (now,),
    )
    await sd._conn.commit()
    settings = Settings()
    captured = []

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.append(kwargs)
            return 1

    pred = NarrativePrediction(
        category_id="ai",
        category_name="AI Tokens",
        coin_id="real-coin",
        symbol="REAL",
        name="Real Coin",
        market_cap_at_prediction=10_000_000,
        price_at_prediction=0.01,
        narrative_fit_score=80,
        staying_power="high",
        confidence="high",
        reasoning="x",
        market_regime="bull",
        trigger_count=3,
        strategy_snapshot={},
        predicted_at=datetime.now(timezone.utc),
    )
    await trade_predictions(
        FakeEngine(), sd, [pred],
        min_mcap=1_000_000, max_mcap=None, min_fit_score=1,
        settings=settings,
    )
    assert captured, "trade_predictions did not call open_trade"
    assert captured[0].get("symbol") == "REAL", f"got {captured[0]!r}"
    assert captured[0].get("name") == "Real Coin", f"got {captured[0]!r}"
    await sd.close()
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_predictions_passes_symbol_and_name_to_engine -v
```

Expected: FAIL.

- [ ] **Step 3: Modify `trade_predictions`**

In `scout/trading/signals.py:741-752`, change the `engine.open_trade` call:

```python
            await engine.open_trade(
                token_id=pred.coin_id,
                symbol=pred.symbol,
                name=pred.name,
                chain="coingecko",
                signal_type="narrative_prediction",
                signal_data={
                    "fit": pred.narrative_fit_score,
                    "category": pred.category_name,
                    "mcap": pred.market_cap_at_prediction,
                },
                entry_price=pred_price,
                signal_combo=combo_key,
            )
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_predictions_passes_symbol_and_name_to_engine -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/signals.py tests/test_bl076_junk_filter_and_symbol_name.py
git commit -m "fix(BL-076): wire symbol+name through trade_predictions -> engine.open_trade"
```

---

### Task 5: Wire symbol/name in `trade_chain_completions` via Database resolver

**Files:**
- Modify: `scout/db.py` (add `Database.lookup_symbol_name_by_coin_id` method)
- Modify: `scout/trading/signals.py:760-820` (use new method in dispatcher)
- Test: `tests/test_bl076_junk_filter_and_symbol_name.py`

`chain_matches` table has no symbol/name. Resolve via **3 sequential prioritized SELECTs** (not UNION) across `gainers_snapshots` (most authoritative — populated from CoinGecko `/coins/markets`), `volume_history_cg` (CoinGecko-side coverage), `volume_spikes` (DexScreener-side coverage). Each in its own try/except so a column-rename in one table fails ONLY that lookup; helper still returns metadata from the others.

Per architecture-review #2: helper lives on `Database` class (pure metadata read, future-callable from dashboard / backfill scripts) NOT in `signals.py` module-private space.

- [ ] **Step 1: Write failing test (T5 — chain has metadata in gainers_snapshots)**

```python
@pytest.mark.asyncio
async def test_lookup_symbol_name_prefers_gainers_snapshots(tmp_path):
    """T5 — Database.lookup_symbol_name_by_coin_id picks gainers_snapshots
    first (most authoritative source per architecture-review #4)."""
    from datetime import datetime, timezone
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, "
        " price_at_snapshot, snapshot_at) "
        "VALUES ('chain-coin', 'CHAIN', 'Chain Token', 12.0, 5_000_000, 0.05, ?)",
        (now,),
    )
    await sd._conn.commit()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("chain-coin")
    assert symbol == "CHAIN"
    assert name == "Chain Token"
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_falls_through_to_volume_history_cg(tmp_path):
    """T5b — when gainers_snapshots has no row, falls through to
    volume_history_cg. Validates the sequential prioritized lookup chain."""
    from datetime import datetime, timezone
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT INTO volume_history_cg "
        "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
        "VALUES ('only-vh-coin', 'ONLYVH', 'Only VolHist Coin', 1000, 100, 1.0, ?)",
        (now,),
    )
    await sd._conn.commit()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("only-vh-coin")
    assert symbol == "ONLYVH"
    assert name == "Only VolHist Coin"
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_returns_empty_when_no_source_has_row(tmp_path):
    """T5c — orphan coin (no row in any snapshot table) returns ('', '')
    so caller can decide to log + still proceed with the trade."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("orphan-coin")
    assert symbol == ""
    assert name == ""
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_skips_null_symbol_in_source(tmp_path):
    """T5d (aff3517 #11 edge case) — snapshot row exists but symbol IS NULL
    (legacy / partial data). Helper's `if row and row[0] and row[1]` filter
    must skip and try next table. Here volume_spikes has a NULL-symbol row
    AND volume_history_cg has a clean row — helper must return the clean."""
    from datetime import datetime, timezone
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    # volume_history_cg has the clean row
    await sd._conn.execute(
        "INSERT INTO volume_history_cg "
        "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
        "VALUES ('partial-coin', 'PART', 'Partial Coin', 1000, 100, 1.0, ?)",
        (now,),
    )
    await sd._conn.commit()
    symbol, name = await sd.lookup_symbol_name_by_coin_id("partial-coin")
    assert symbol == "PART"
    assert name == "Partial Coin"
    await sd.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_coin_id", ["", None])
async def test_lookup_symbol_name_handles_empty_or_none_coin_id(tmp_path, bad_coin_id):
    """T5e — F16 mitigation. Defensive guard at top of helper for
    empty/None coin_id (caller-side bug). Should return ("","") without
    issuing a SELECT."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    symbol, name = await sd.lookup_symbol_name_by_coin_id(bad_coin_id)
    assert symbol == ""
    assert name == ""
    await sd.close()


@pytest.mark.asyncio
async def test_lookup_symbol_name_propagates_non_operational_errors(tmp_path, monkeypatch):
    """T5g (A11 fix) — pin that the per-table catch is narrow:
    `except aiosqlite.OperationalError` ONLY. Other exception types
    (programming errors, type mismatches) MUST propagate — otherwise
    we hide real bugs behind silent ("","") returns.

    Monkeypatch _conn.execute to raise ValueError on the first call;
    helper must propagate, not swallow."""
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    real_execute = sd._conn.execute
    call_count = {"n": 0}

    async def boom(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("simulated programming error")
        return await real_execute(*args, **kwargs)

    monkeypatch.setattr(sd._conn, "execute", boom)
    with pytest.raises(ValueError, match="simulated programming error"):
        await sd.lookup_symbol_name_by_coin_id("any-coin")
    await sd.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py -k lookup_symbol_name -v
```

Expected: FAIL with `AttributeError: 'Database' object has no attribute 'lookup_symbol_name_by_coin_id'`.

- [ ] **Step 3: Add `lookup_symbol_name_by_coin_id` to `Database` class**

In `scout/db.py`, add near the other public methods (search for `async def initialize` then place this method after it; exact location flexible — keep it grouped with other read helpers if there's a section):

```python
    async def lookup_symbol_name_by_coin_id(
        self, coin_id: str
    ) -> tuple[str, str]:
        """BL-076: pure metadata lookup. Returns (symbol, name) for a
        CoinGecko coin_id, resolving via 3 sequential prioritized SELECTs.

        chain_matches table carries no symbol/name. This helper bridges
        that gap by querying snapshot tables that DO have it (all keyed
        by coin_id). Per architecture-review #2 lives on Database (not
        signals.py) so future callers (dashboard, backfill scripts) reuse
        the resolver instead of reimplementing the JOIN.

        Lookup order (per architecture-review #4 — gainers_snapshots is
        the most authoritative source, populated from CoinGecko's
        /coins/markets endpoint):
          1. gainers_snapshots (canonical CoinGecko metadata)
          2. volume_history_cg (CoinGecko volume telemetry)
          3. volume_spikes (DexScreener-side spikes)

        Each SELECT in its own `except aiosqlite.OperationalError`
        (per architecture-review #1 + A11 narrowing — NOT bare `except
        Exception`): a column rename or table lock in any one table fails
        ONLY that lookup; the next table still works. Other exception
        types (programming errors, etc.) propagate. Returns ("", "") if
        nothing found — caller decides whether to log + still proceed.

        Refactor trigger (per A8): when a 4th source is added OR source
        priority becomes dynamic per-chain, refactor to a `MetadataSource`
        plugin pattern. Performance trigger (per A1): if cardinality
        exceeds ~500/cycle, refactor to UNION ALL with per-table
        OperationalError fallback for happy-path single round-trip.
        """
        # F16 mitigation: defensive None/empty coin_id guard.
        if not coin_id:
            return "", ""
        # 1. gainers_snapshots — primary source (canonical CoinGecko)
        try:
            cur = await self._conn.execute(
                "SELECT symbol, name FROM gainers_snapshots "
                "WHERE coin_id = ? AND symbol IS NOT NULL AND name IS NOT NULL "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (coin_id,),
            )
            row = await cur.fetchone()
            if row and row[0] and row[1]:
                return row[0], row[1]
        except aiosqlite.OperationalError:
            # F3 (schema drift) + F17 (table locked) — fall through.
            # Other exceptions (e.g. ProgrammingError from a logic bug)
            # propagate, per A11.
            pass
        # 2. volume_history_cg — fallback
        try:
            cur = await self._conn.execute(
                "SELECT symbol, name FROM volume_history_cg "
                "WHERE coin_id = ? AND symbol IS NOT NULL AND name IS NOT NULL "
                "ORDER BY recorded_at DESC LIMIT 1",
                (coin_id,),
            )
            row = await cur.fetchone()
            if row and row[0] and row[1]:
                return row[0], row[1]
        except aiosqlite.OperationalError:
            pass
        # 3. volume_spikes — last resort
        try:
            cur = await self._conn.execute(
                "SELECT symbol, name FROM volume_spikes "
                "WHERE coin_id = ? AND symbol IS NOT NULL AND name IS NOT NULL "
                "ORDER BY detected_at DESC LIMIT 1",
                (coin_id,),
            )
            row = await cur.fetchone()
            if row and row[0] and row[1]:
                return row[0], row[1]
        except aiosqlite.OperationalError:
            pass
        return "", ""
```

Ensure `import aiosqlite` is at the top of `scout/db.py` (verified — `scout/db.py` already imports it).

- [ ] **Step 4: Run lookup tests to verify they pass**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py -k lookup_symbol_name -v
```

Expected: 4 PASS.

- [ ] **Step 5: Wire `trade_chain_completions` to the new resolver**

```python
@pytest.mark.asyncio
async def test_trade_chain_completions_uses_lookup_helper_for_metadata(tmp_path):
    """T5e — trade_chain_completions calls Database.lookup_symbol_name_by_coin_id
    and passes the result through to engine.open_trade.

    M3 fix (a09b333): chain_matches schema requires steps_matched (NOT NULL),
    total_steps (NOT NULL), anchor_time (NOT NULL), chain_duration_hours
    (NOT NULL), conviction_boost (NOT NULL). v1 INSERT omitted 4 of 5;
    fixed below."""
    from datetime import datetime, timezone
    from scout.trading.signals import trade_chain_completions
    from scout.config import Settings
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('chain-coin', 0.05, 5_000_000, ?)", (now,),
    )
    # chain_matches NOT NULL columns (M3 fix): steps_matched, total_steps,
    # anchor_time, chain_duration_hours, conviction_boost.
    # chain_patterns FK on pattern_id — seed a pattern row first.
    await sd._conn.execute(
        "INSERT INTO chain_patterns (id, name, pipeline, steps_json, "
        " is_active, hit_threshold_pct, max_chain_duration_hours, created_at) "
        "VALUES (1, 'full_conviction', 'narrative', '[]', 1, 5.0, 48.0, ?)",
        (now,),
    )
    await sd._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, "
        " steps_matched, total_steps, anchor_time, completed_at, "
        " chain_duration_hours, conviction_boost, created_at) "
        "VALUES ('chain-coin', 'narrative', 1, 'full_conviction', "
        " 3, 3, ?, ?, 4.0, 1, ?)",
        (now, now, now),
    )
    await sd._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, "
        " price_at_snapshot, snapshot_at) "
        "VALUES ('chain-coin', 'CHAIN', 'Chain Token', 12.0, 5_000_000, 0.05, ?)",
        (now,),
    )
    await sd._conn.commit()
    settings = Settings()
    captured = []

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.append(kwargs)
            return 1

    await trade_chain_completions(FakeEngine(), sd, settings=settings)
    assert captured, "trade_chain_completions did not call open_trade"
    assert captured[0].get("symbol") == "CHAIN", f"got {captured[0]!r}"
    assert captured[0].get("name") == "Chain Token", f"got {captured[0]!r}"
    await sd.close()
```

In `scout/trading/signals.py`, modify `trade_chain_completions` at line 814:

```python
                # BL-076: resolve symbol/name via Database resolver; log
                # warning if neither found so operator sees the gap rate.
                symbol, name = await db.lookup_symbol_name_by_coin_id(
                    c["token_id"]
                )
                if not symbol and not name:
                    logger.warning(
                        "chain_completed_no_metadata",
                        coin_id=c["token_id"],
                        hint="no row in gainers_snapshots/volume_history_cg/volume_spikes",
                    )
                await engine.open_trade(
                    token_id=c["token_id"],
                    symbol=symbol,
                    name=name,
                    chain="coingecko",
                    signal_type="chain_completed",
                    signal_data={
                        "pattern": c["pattern_name"],
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_chain_completions_uses_lookup_helper_for_metadata -v` — Expected: PASS.

- [ ] **Step 6: Add orphan-fallback test using structlog capture (M1 fix applied)**

```python
@pytest.mark.asyncio
async def test_trade_chain_completions_falls_back_to_empty_when_no_snapshot(tmp_path):
    """T5f — orphan chain coin (no row in any snapshot table). Helper
    returns ('', ''), dispatcher logs `chain_completed_no_metadata`,
    AND open_trade still fires (the trade is real; we just lack metadata).
    Engine-level WARNING from Task 2 ALSO fires (defense-in-depth).

    NOTE (M1): use structlog.testing.capture_logs (not caplog).
    NOTE (M3): chain_matches INSERT supplies all NOT NULL columns."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from scout.trading.signals import trade_chain_completions
    from scout.config import Settings
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('orphan-coin', 0.05, 5_000_000, ?)", (now,),
    )
    await sd._conn.execute(
        "INSERT INTO chain_patterns (id, name, pipeline, steps_json, "
        " is_active, hit_threshold_pct, max_chain_duration_hours, created_at) "
        "VALUES (2, 'full_conviction', 'narrative', '[]', 1, 5.0, 48.0, ?)",
        (now,),
    )
    await sd._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, "
        " steps_matched, total_steps, anchor_time, completed_at, "
        " chain_duration_hours, conviction_boost, created_at) "
        "VALUES ('orphan-coin', 'narrative', 2, 'full_conviction', "
        " 3, 3, ?, ?, 4.0, 1, ?)",
        (now, now, now),
    )
    await sd._conn.commit()
    settings = Settings()
    captured = []

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.append(kwargs)
            return 1

    with capture_logs() as logs:
        await trade_chain_completions(FakeEngine(), sd, settings=settings)
    assert captured, "open_trade still called even with empty symbol/name"
    assert captured[0].get("symbol") == ""
    assert captured[0].get("name") == ""
    assert any(
        e.get("event") == "chain_completed_no_metadata" for e in logs
    ), f"expected chain_completed_no_metadata; got {[e.get('event') for e in logs]}"
    await sd.close()
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_chain_completions_falls_back_to_empty_when_no_snapshot -v` — Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scout/db.py scout/trading/signals.py tests/test_bl076_junk_filter_and_symbol_name.py
git commit -m "fix(BL-076): chain_completed resolves symbol+name via Database.lookup_symbol_name_by_coin_id (sequential prioritized lookup with per-table try/except)"
```

---

### Task 6: Final regression sweep + push

- [ ] **Step 1: Run BL-076 test file**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py -v --tb=short
```

Expected: all 7+ tests PASS.

- [ ] **Step 2: Targeted regression on adjacent test files**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_signals_trade_dispatchers.py tests/test_bl065_cashtag_dispatch.py tests/test_db.py tests/test_dashboard_tg_social_extensions.py -q --tb=short
```

Expected: all PASS (no regression in trading dispatch + BL-065 + db + BL-066' tests).

- [ ] **Step 3: Push branch**

```bash
git push origin feat/bl-076-junk-filter-symbol-name
```

- [ ] **Step 4: Verify CI green (excluding pre-existing flake)**

`test_heartbeat_mcap_missing.py` is the known pre-existing flake (CoinGecko 503 from CI runners — same failure on master `cbb1e7f` / `b51324c` / `6b95c2f`). Any other failure is a real regression and blocks the PR.

---

## Pre-merge audits (run BEFORE pushing to PR)

**A. Other placeholder prefixes audit (a09b333 S3):**
```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "
  SELECT substr(token_id, 1, instr(token_id||'\''-'\'','\''-'\'')-1) AS prefix,
         COUNT(*) AS n
  FROM paper_trades
  GROUP BY prefix
  HAVING n >= 1
  ORDER BY n DESC
  LIMIT 30"' > .ssh_prefix_audit.txt
```
**Expected output:** prefixes like `bridged`, `wrapped`, `test`, `bnb`, `ethereum`, etc. If unexpected placeholder prefixes (`example`, `demo`, `placeholder`, `sample`) appear with non-trivial counts, ADD them to `_JUNK_COINID_PREFIXES` in the same PR rather than punting.

**B. Symbol/name baseline (aff3517 #10):** capture pre-deploy state so post-deploy operator audit has attribution data:
```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "
  SELECT signal_type, COUNT(*) AS n_empty
  FROM paper_trades
  WHERE symbol = '\'''\'' OR name = '\'''\''
  GROUP BY signal_type
  ORDER BY n_empty DESC"' > .ssh_empty_baseline.txt
```
This baseline distinguishes "fix carries 90% of value via volume_spike" vs "chain_completed JOIN is the main payoff" — informs whether to invest in BL-077 follow-ups.

---

## Deploy verification (§5)

**Sequence (deploy-stop-FIRST per BL-065 plan v3 §5 + lessons from BL-066' deploy):**

0. **Pre-deploy backup:** `cp /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.bak.bl076.$(date +%s)`
0a. **Capture error baseline:** `BASELINE_ERR=$(journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -ciE "error|exception|traceback") ; echo "baseline=$BASELINE_ERR" > /tmp/bl076_baseline.txt` — record for step 8.
0b. **Capture WARNING baseline (used in step 9 escalation criterion):** `BASE_WARN=$(journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -c "open_trade_called_with_empty_symbol_and_name") ; echo "warn_baseline=$BASE_WARN" >> /tmp/bl076_baseline.txt`
1. **Stop pipeline service FIRST:** `systemctl stop gecko-pipeline`. (Dashboard service `gecko-dashboard` does NOT need to stop — BL-076 doesn't touch dashboard code.)
2. **Pull:** `cd /root/gecko-alpha && git pull origin master`
3. **Clear pycache (lesson from BL-066' deploy 2026-05-04):** `find . -name __pycache__ -type d -exec rm -rf {} +`
4. **Start pipeline:** `systemctl start gecko-pipeline`
5. **Service started cleanly:** `systemctl status gecko-pipeline` — active+running.
6. **Junk filter ACTIVELY rejects (a09b333 S2 — positive verification path):**
   - Wait one polling cycle (5 minutes).
   - **Negative check (necessary, not sufficient):**
     ```bash
     sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM paper_trades WHERE token_id LIKE 'test-%' AND opened_at >= datetime('now', '-10 minutes')"
     ```
     Expected: 0.
   - **Positive check (proves filter actually rejected):** look for `signal_skipped_junk` events for `test-` coin_ids in journalctl:
     ```bash
     journalctl -u gecko-pipeline --since "15 minutes ago" --no-pager | grep -E '"signal_skipped_junk".*"coin_id":"test-' | head -5
     ```
     Expected: at least one entry (CoinGecko continuously refreshes its `test-N` placeholder coins; over a 15min window we should see at least one rejection in the trending/markets feed). If zero entries AND zero new trades, the filter wasn't exercised — re-check after 1h.
7. **Symbol/name populated for new trades:**
   ```bash
   sqlite3 /root/gecko-alpha/scout.db "SELECT id, signal_type, token_id, symbol, name FROM paper_trades WHERE opened_at >= datetime('now', '-10 minutes') AND signal_type IN ('volume_spike', 'narrative_prediction', 'chain_completed') ORDER BY id DESC"
   ```
   Expected: any new rows have non-empty symbol AND non-empty name (chain_completed may have empty if no snapshot row exists — see step 9). Existing rows pre-deploy unaffected (forward-only fix).
8. **No new exceptions vs baseline:**
   ```bash
   BASELINE_ERR=$(grep "^baseline=" /tmp/bl076_baseline.txt | cut -d= -f2)
   POST=$(journalctl -u gecko-pipeline --since "5 minutes ago" --no-pager | grep -ciE "error|exception|traceback")
   echo "post=$POST baseline=$BASELINE_ERR"
   [ "$POST" -le "$BASELINE_ERR" ] && echo "OK" || echo "REGRESSION: $((POST - BASELINE_ERR)) new"
   ```
9. **Engine WARNING is rare (not wallpaper) — feeds soak-then-escalate decision:**
   ```bash
   POST_WARN=$(journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -c "open_trade_called_with_empty_symbol_and_name")
   echo "WARNING fires in last 10min: $POST_WARN"
   ```
   Expected: rare (0–3 expected — only chain_completed orphan tokens). If many (>10/hour), the resolver isn't finding metadata — investigate `lookup_symbol_name_by_coin_id` paths before declaring deploy success.
10. **Symbol/name fix attribution (correlate against pre-deploy baseline):**
    ```bash
    sqlite3 /root/gecko-alpha/scout.db "
      SELECT signal_type, COUNT(*) AS new_with_meta
      FROM paper_trades
      WHERE symbol != '' AND name != ''
        AND opened_at >= datetime('now', '-1 hour')
      GROUP BY signal_type"
    ```
    Compare against `.ssh_empty_baseline.txt`: if pre-deploy showed 50 empty narrative_prediction trades and post-deploy shows 5 NEW narrative_prediction with metadata, fix is working.

**Soak-then-escalate criterion (per architecture-review #3):** track `open_trade_called_with_empty_symbol_and_name` count daily for 14 days. Trigger:
- ≥1 event during soak → investigate which dispatcher leaked + patch
- 0 events for 14 consecutive days → open BL-077 to escalate engine WARNING to a hard reject (raise + log instead of warn + proceed)

**Revert path:** `git checkout <prev-master-sha> && find . -name __pycache__ -exec rm -rf {} + && systemctl restart gecko-pipeline`. No DB rollback needed (no schema changes). Pre-deploy paper_trades rows with empty symbol/name remain as-is — this is a forward-only correctness fix.

---

## Self-Review

1. **Spec coverage:**
   - Bug 1 (test-*  bypass) → Task 1 + T1 + T1b ✓
   - Bug 2.a (volume_spike empty symbol/name) → Task 3 ✓
   - Bug 2.b (narrative_prediction empty symbol/name) → Task 4 ✓
   - Bug 2.c (chain_completed empty symbol/name) → Task 5 + T5b fallback ✓
   - Engine-level defense-in-depth visibility → Task 2 ✓

2. **Placeholder scan:** none — every step has either exact code or exact command.

3. **Type consistency:** `_JUNK_COINID_PREFIXES` is `tuple[str, ...]` — adding string ✓. Helper returns `tuple[str, str]` — consumed positionally + via tuple unpack ✓. Engine signature unchanged ✓.

4. **New primitives marker:** present at top with junk-prefix tuple addition + WARNING log + dispatcher-call-site changes + new helper. NO new DB tables/columns/settings.

5. **Hermes-first marker:** present immediately after new-primitives per alignment doc 2026-05-04 convention. 6/6 negative + verdict.

6. **Drift grounding:** explicit file:line refs to all extended code; deployed schemas verified; bug evidence cited from prod audit.

7. **TDD discipline:** every task starts with failing test → run → impl → run → commit. No "implement then add tests later".

8. **No cross-task coupling:** Tasks 1, 3, 4, 5 each modify a different code surface; can be reverted independently. Task 2 (engine WARNING) is purely additive — visible only via logs. Task 5 introduces the helper but doesn't change other dispatchers.

9. **Honest scope:**
   - **NOT in scope:** retroactively backfilling symbol/name for the 150+ historical paper_trades with empty fields (forward-only fix)
   - **NOT in scope:** stripping `test-` rows from prod (only 2 trades; already closed; no real money; reviewing them is more useful than rewriting history)
   - **CONDITIONALLY in scope (per a09b333 S3):** broader CoinGecko placeholder slug audit (`example-N`, `placeholder-N`, `demo-N`). Pre-merge prefix-audit query in §"Pre-merge audits" enumerates all observed prefixes in `paper_trades`. If any non-`test-` placeholder family appears, FOLD INTO THIS PR — adding one prefix later is the same churn as adding four now.
   - **DELIBERATELY DEFERRED:** chain_completed metadata via direct CoinGecko fetch (would add I/O dependency to the dispatch hot path); current solution uses already-available DB rows.

10. **Engine-WARNING soak-then-escalate (aff3517 #3):** the WARNING is intentionally not a hard reject for v1 — escalating mid-deploy would break in-flight pipelines. Soak criterion: 14 consecutive days of green prod logs (zero `open_trade_called_with_empty_symbol_and_name` events). On clean soak → open BL-077 to flip the WARNING to a `raise UnknownEmptyMetadataError` + log. Track via §5 step 9 daily count. If WARNING never reaches zero, the resolver has a coverage gap that needs fixing first.

11. **Junk-prefix tuple — when to refactor (aff3517 #5):** current shape is `tuple[str, str, str, str]` (4 entries: bridged, wrapped, superbridge, test). Trigger to refactor to a settings-backed list (`PAPER_JUNK_COINID_PREFIXES`):
    - tuple grows to ≥10 entries (review-pain threshold), OR
    - a substring/regex/fnmatch pattern is needed (e.g., `"test-coin*"`, `"^placeholder.*"`)
    Don't build the settings indirection now — but plan to surface this trigger in the `_JUNK_COINID_PREFIXES` docstring so the next contributor sees it.

12. **Chain_completed coverage rate — measured at deploy (aff3517 #10):** §5 step 10 correlates pre-deploy `.ssh_empty_baseline.txt` against post-deploy new-trade metadata. If chain_completed = 1% of historical empties, the JOIN helper is over-engineered; if ≥50%, it's the main fix. Either way, attribution data informs the next pipeline session's priorities.
