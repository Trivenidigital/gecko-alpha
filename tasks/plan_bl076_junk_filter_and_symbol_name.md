# BL-076: Junk-filter expansion + symbol/name population — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** add `"test-"` to `_JUNK_COINID_PREFIXES` in `scout/trading/signals.py:569`; add empty-string check on `symbol` and `name` to engine-level guard in `scout/trading/engine.py:open_trade` (reject when both are empty after BL-076 dispatcher patches land — defense in depth, fails loud not silent); modify call sites in `trade_volume_spikes` (line 53) + `trade_predictions` (line 741) + `trade_chain_completions` (line 814) to populate `symbol` + `name` parameters; new `dashboard/db.py` query NOT introduced (existing surfaces use the corrected fields directly). NO new DB tables, columns, or settings.

**Prerequisites:** master ≥ `64a8e35` (BL-066' deployed + backlog hygiene merged).

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
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_open_trade_logs_warning_when_symbol_and_name_both_empty(caplog):
    """T2 — engine-level defense-in-depth: log WARNING when caller forgets
    to pass symbol+name. Bug 2 evidence: 150+ paper trades across 3 dispatcher
    paths had empty symbol+name; operator audit dashboard couldn't identify
    them. This guard catches any future caller drift."""
    from scout.trading.engine import TradingEngine
    from scout.config import Settings
    import logging
    from scout.db import Database
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "t.db")
        sd = Database(db_path)
        await sd.initialize()
        settings = Settings()
        engine = TradingEngine(sd, settings)
        # Caller-forgotten symbol+name. open_trade returns None or trade_id;
        # we don't care about the trade outcome — only that the warning fires.
        with caplog.at_level(logging.WARNING):
            await engine.open_trade(
                token_id="some-coin",
                signal_type="volume_spike",
                signal_data={"foo": "bar"},
                signal_combo="vs|none",
                entry_price=0.001,
            )
        # Search structlog output via caplog.text (structlog routes through stdlib)
        assert "open_trade_called_with_empty_symbol_and_name" in caplog.text or any(
            "empty_symbol" in str(r.msg) for r in caplog.records
        )
        await sd.close()
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_logs_warning_when_symbol_and_name_both_empty -v
```

Expected: FAIL — log event not emitted.

- [ ] **Step 3: Add WARNING log in `open_trade`**

In `scout/trading/engine.py`, immediately after the `if signal_data is None: signal_data = {}` block (around line 122):

```python
        # BL-076: defense-in-depth visibility. Bug 2 (2026-05-04) showed
        # ~150+ paper trades had empty symbol+name because 3 dispatchers
        # forgot to pass them. Tasks 3-5 fix the dispatchers; this WARNING
        # surfaces any FUTURE caller drift. NOT a hard reject — that would
        # break in-flight pipelines mid-deploy.
        if not symbol and not name:
            log.warning(
                "open_trade_called_with_empty_symbol_and_name",
                token_id=token_id,
                signal_type=signal_type,
                hint="dispatcher likely missing symbol=... + name=... kwargs",
            )
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_logs_warning_when_symbol_and_name_both_empty -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/engine.py tests/test_bl076_junk_filter_and_symbol_name.py
git commit -m "feat(BL-076): engine-level WARNING on empty symbol+name (defense-in-depth)"
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

### Task 5: Wire symbol/name in `trade_chain_completions` via JOIN

**Files:**
- Modify: `scout/trading/signals.py:760-820` (add helper + use it in dispatcher)
- Test: `tests/test_bl076_junk_filter_and_symbol_name.py`

`chain_matches` table has no symbol/name. Resolve via newest-row lookup across `gainers_snapshots` ∪ `volume_history_cg` ∪ `volume_spikes` (all keyed by `coin_id` and carry symbol+name). Falls back to empty string + WARNING log if nothing found (the engine-level WARNING from Task 2 then fires too — surfaces visibility).

- [ ] **Step 1: Write failing test (chain has metadata in gainers_snapshots)**

```python
@pytest.mark.asyncio
async def test_trade_chain_completions_resolves_symbol_name_from_snapshot_tables(tmp_path):
    """T5 — chain_matches has no symbol/name. Helper queries
    gainers_snapshots / volume_history_cg / volume_spikes (all keyed by
    coin_id) for newest row's metadata. Pass through to open_trade."""
    from datetime import datetime, timezone
    from scout.trading.signals import trade_chain_completions
    from scout.config import Settings
    from scout.db import Database

    db_path = str(tmp_path / "t.db")
    sd = Database(db_path)
    await sd.initialize()
    now = datetime.now(timezone.utc).isoformat()
    # Seed price_cache + a chain_matches row + a gainers_snapshots row
    # with the symbol+name we expect to flow through.
    await sd._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) "
        "VALUES ('chain-coin', 0.05, 5_000_000, ?)", (now,))
    await sd._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, conviction_boost, "
        " completed_at, created_at) "
        "VALUES ('chain-coin', 'narrative', 1, 'full_conviction', 1.5, ?, ?)",
        (now, now),
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

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_chain_completions_resolves_symbol_name_from_snapshot_tables -v
```

Expected: FAIL.

- [ ] **Step 3: Add helper `_resolve_symbol_name_for_chain` and use it**

In `scout/trading/signals.py`, add the helper before `trade_chain_completions`:

```python
async def _resolve_symbol_name_for_chain(
    db: Database, coin_id: str
) -> tuple[str, str]:
    """BL-076: chain_matches table carries no symbol/name. Resolve
    metadata by querying snapshot tables that DO have it (all keyed
    by coin_id). Returns ("", "") if nothing found — caller logs a
    warning so operator can see the gap rate.

    Lookup order is newest-first across three tables:
    1. gainers_snapshots (most likely to have a recent row for the
       same token that triggered the chain completion)
    2. volume_history_cg (CoinGecko-side coverage)
    3. volume_spikes (DexScreener-side coverage)

    UNION ALL preserves source rows; we ORDER BY recency and take 1.
    """
    cur = await db._conn.execute(
        """SELECT symbol, name, recorded_at FROM (
             SELECT symbol, name, snapshot_at AS recorded_at
             FROM gainers_snapshots WHERE coin_id = ?
             UNION ALL
             SELECT symbol, name, recorded_at
             FROM volume_history_cg WHERE coin_id = ?
             UNION ALL
             SELECT symbol, name, detected_at AS recorded_at
             FROM volume_spikes WHERE coin_id = ?
           )
           ORDER BY recorded_at DESC
           LIMIT 1""",
        (coin_id, coin_id, coin_id),
    )
    row = await cur.fetchone()
    if row and row[0] and row[1]:
        return row[0], row[1]
    return "", ""
```

In `trade_chain_completions` at line 814 (the open_trade call), modify to:

```python
                # BL-076: resolve symbol/name from snapshot tables; log
                # warning if neither found so operator can see the gap.
                symbol, name = await _resolve_symbol_name_for_chain(
                    db, c["token_id"]
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

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_chain_completions_resolves_symbol_name_from_snapshot_tables -v
```

Expected: PASS.

- [ ] **Step 5: Add fallback test (no snapshot row → empty + warning)**

```python
@pytest.mark.asyncio
async def test_trade_chain_completions_falls_back_to_empty_when_no_snapshot(tmp_path, caplog):
    """T5b — when no snapshot table has the coin_id, fall back to
    empty symbol/name + log a warning. Engine-level WARNING from T2
    then ALSO fires (defense-in-depth)."""
    import logging
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
        "VALUES ('orphan-coin', 0.05, 5_000_000, ?)", (now,))
    await sd._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, conviction_boost, "
        " completed_at, created_at) "
        "VALUES ('orphan-coin', 'narrative', 1, 'full_conviction', 1.5, ?, ?)",
        (now, now),
    )
    await sd._conn.commit()
    settings = Settings()
    captured = []

    class FakeEngine:
        async def open_trade(self, **kwargs):
            captured.append(kwargs)
            return 1

    with caplog.at_level(logging.WARNING):
        await trade_chain_completions(FakeEngine(), sd, settings=settings)
    assert captured, "open_trade still called with empty symbol/name"
    assert captured[0].get("symbol") == ""
    assert captured[0].get("name") == ""
    assert "chain_completed_no_metadata" in caplog.text
    await sd.close()
```

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_chain_completions_falls_back_to_empty_when_no_snapshot -v` — Expected: PASS (helper returns empty + warning logged + open_trade still called for chain_completed because trade is real).

- [ ] **Step 6: Commit**

```bash
git add scout/trading/signals.py tests/test_bl076_junk_filter_and_symbol_name.py
git commit -m "fix(BL-076): chain_completed resolves symbol+name via snapshot-table JOIN with empty fallback"
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

## Deploy verification (§5)

**Sequence (deploy-stop-FIRST per BL-065 plan v3 §5 + lessons from BL-066' deploy):**

0. **Pre-deploy backup:** `cp /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.bak.bl076.$(date +%s)`
0a. **Capture error baseline:** `BASELINE_ERR=$(journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -ciE "error|exception|traceback") ; echo "baseline=$BASELINE_ERR"` — record for step 7.
1. **Stop pipeline service FIRST:** `systemctl stop gecko-pipeline`. (Dashboard service `gecko-dashboard` does NOT need to stop — BL-076 doesn't touch dashboard code.)
2. **Pull:** `cd /root/gecko-alpha && git pull origin master`
3. **Clear pycache (lesson from BL-066' deploy 2026-05-04):** `find . -name __pycache__ -type d -exec rm -rf {} +`
4. **Start pipeline:** `systemctl start gecko-pipeline`
5. **Service started cleanly:** `systemctl status gecko-pipeline` — active+running.
6. **Junk filter active — no NEW test-* trades:** wait one polling cycle (5 minutes), then:
   ```bash
   sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM paper_trades WHERE token_id LIKE 'test-%' AND opened_at >= datetime('now', '-10 minutes')"
   ```
   Expected: 0.
7. **Symbol/name populated for new trades:**
   ```bash
   sqlite3 /root/gecko-alpha/scout.db "SELECT id, signal_type, token_id, symbol, name FROM paper_trades WHERE opened_at >= datetime('now', '-10 minutes') ORDER BY id DESC"
   ```
   Expected: any new rows from `volume_spike` / `narrative_prediction` / `chain_completed` paths have non-empty symbol AND non-empty name. (Existing rows pre-deploy are unaffected — this is a forward-only fix.)
8. **No new exceptions vs baseline:**
   ```bash
   POST=$(journalctl -u gecko-pipeline --since "5 minutes ago" --no-pager | grep -ciE "error|exception|traceback"); echo "post=$POST baseline=$BASELINE_ERR"
   [ "$POST" -le "$BASELINE_ERR" ] && echo "OK" || echo "REGRESSION: $((POST - BASELINE_ERR)) new"
   ```
9. **Optional — verify engine WARNING fires only for truly orphan tokens:**
   ```bash
   journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep "open_trade_called_with_empty_symbol_and_name" | head -5
   ```
   Expected: at most a few entries from chain_completed paths where the chain token has no row in any snapshot table (genuinely orphan). If many, the helper resolution is failing — investigate.

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
   - **NOT in scope:** broader CoinGecko placeholder slug audit (e.g. `example-N`, `placeholder-N`) — `test-` is the only one observed in prod over 30+ days. Add others reactively if they appear.
   - **DELIBERATELY DEFERRED:** chain_completed metadata via direct CoinGecko fetch (would add I/O dependency to the dispatch hot path); current solution uses already-available DB rows.
