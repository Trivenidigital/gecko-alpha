**New primitives introduced:** `scout.trading.actionability.ActionabilityDecision`; `scout.trading.actionability.evaluate_actionability_v1`; `PaperTrader` actionability market-cap enrichment helper; three `paper_trades` columns (`actionable`, `actionability_reason`, `actionability_version`); migration marker `bl_new_actionability_gate_v1`; follow-up backlog entries for X/TG outcome linkage and no-peak risk handling.

# Actionability Gate v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tested `actionable=false` layer to paper trades so discovery remains broad while decision-bearing cohorts exclude known junk patterns.

**Architecture:** Implement a pure Actionability Gate v1 classifier, stamp its decision onto `paper_trades` at paper-trade open, and leave all signal ingestion/exploratory paper recording intact. This is observability/control metadata, not an exit-policy change and not a live-trading dispatch change.

**Tech Stack:** Python 3.12, async SQLite via `aiosqlite`, Pydantic `Settings`, pytest/pytest-asyncio, existing `TradingEngine`/`PaperTrader` open path.

---

## Drift Check

- Existing `would_be_live` primitive exists in `scout/trading/live_eligibility.py`; it answers live-slot/tier eligibility and stamps `paper_trades.would_be_live`.
- Existing paper-trade open path:
  - `scout/trading/engine.py::TradingEngine.open_trade` receives `signal_type`, `signal_data`, `signal_combo`, and optional `entry_price`.
  - `scout/trading/paper.py::PaperTrader.execute_buy` computes `would_be_live`, inserts `paper_trades`, then optionally hands off to live/shadow systems.
- Existing signal-data contract is inconsistent for market cap:
  - `trade_gainers`, `trade_losers`, `trade_predictions`, and `tg_social` already pass market cap in `signal_data`.
  - `trade_volume_spikes` currently passes only `{"spike_ratio": ...}` even though `VolumeSpike`/DB-side data has market-cap context elsewhere.
  - `trade_chain_completions` currently passes only pattern/boost; chain matches may have `mcap_at_completion`, and `price_cache.market_cap` may also be available.
  - Therefore the implementation must enrich market cap at the paper-trade edge from existing DB sources before evaluating v1; direct `signal_data` extraction alone is not enough.
- Existing schema migration hook: `scout/db.py::_migrate_feedback_loop_schema` additively manages `paper_trades` columns and indexes.
- No existing `actionable`, `actionability_reason`, or `actionability_version` columns/functions were found.
- `peak_pct` is not available at trade-open time; it is populated by evaluator after monitoring, so no-peak/peak-giveback changes are a separate exit/risk follow-up, not part of Actionability Gate v1.
- `signal_combo` is not a reliable raw-confluence source by itself. `build_combo_key` caps combinations at `signal_type + one extra signal`, and `gainers_early` currently calls that helper with `signals=None`. The audit's `confluence:3` bucket used `max(parsed_combo_count, conviction_locked_stack)`, so v1 must pass open-time `conviction_stack` into the classifier and gate on `max(parsed_combo_count, conviction_stack)`.
- `x_handle`, `tg_channel`, and liquidity are not reliable closed-trade segmentation fields yet per `tasks/findings_profit_patterns_2026_05_19.md`; v1 must not rank by those fields.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Paper-trade actionability attribution | none found in installed VPS skills or public Skills Hub (`https://hermes-agent.nousresearch.com/docs/skills`) | build from scratch; this is gecko-alpha DB row metadata and cohort logic |
| X/KOL signal source | yes - installed `social-media/xurl`, `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher` | reuse as raw signal source only; do not add social ingestion or rank handles until outcome linkage exists |
| Crypto market data/trading skills | public ecosystem has crypto/trading-adjacent skills/tools, but no gecko-alpha paper-trade actionability gate | do not replace project DB/scoring/trading surfaces |
| Dashboard/reporting | no Hermes primitive for this repo's `paper_trades` dashboard columns | defer dashboard UI unless implementation finishes cleanly |

Awesome-hermes-agent ecosystem check: `0xNyk/awesome-hermes-agent` is a curated ecosystem list; search found crypto/trading-adjacent resources but no drop-in actionability gate for gecko-alpha paper-trade DB rows. Verdict: custom in-repo classifier is justified; keep Hermes X/KOL as upstream telemetry.

## Scope Boundaries

In scope:
- Bring audit artifacts forward on this branch.
- Backlog/todo fold for BL-NEW-ACTIONABILITY-GATE and child follow-ups.
- Pure gate function with unit tests.
- Minimal DB columns and migration tests.
- Stamp actionability metadata at paper-trade open.
- Keep exploratory paper trades opening.

Out of scope:
- X/TG handle/channel ranking.
- Dashboard redesign.
- Exit-policy changes for `peak_pct < 5%`.
- Live execution changes.
- Suppressing raw signal rows.

## Gate Rules

Actionability Gate v1 returns:

```python
ActionabilityDecision(
    actionable: bool,
    reason: str,
    version: "v1",
)
```

Rules:
- `narrative_prediction`: actionable when enriched entry market cap is usable and not below $10M.
- `chain_completed`: actionable when enriched entry market cap is usable and not below $10M; if market cap remains missing after DB fallback, v1 permits it with explicit `v1_pass_chain_completed_mcap_unknown_exception` because the total current-regime chain-completed cohort is strongly positive and this source has a known metadata gap. This exception must be reviewed once chain-completed mcap buckets are available.
- `volume_spike`: actionable when enriched entry market cap is usable and not below $10M.
- `gainers_early`: non-actionable for `5m <= mcap < 10m`; non-actionable when source confluence count is at least 3; non-actionable for `10m <= mcap < 50m` as `v1_block_gainers_early_mcap_10_50m_observe`; actionable only when `mcap >= 50m`.
- `losers_contrarian`: non-actionable by default.
- `trending_catch`: non-actionable by default.
- `tg_social`: non-actionable by default.
- Unknown/missing market cap: non-actionable with explicit reason, except the explicit `chain_completed` carve-out above.
- Unknown signal types: non-actionable with explicit reason.
- Core-signal market cap below $10M: non-actionable by conservative v1 policy. The audit strongly supports `10-50m` and current-regime `>50m`; sub-$10M cells are too thin/unstable for a first actionability pass.

Market-cap buckets are computed from enriched open-time metadata:
- first, `signal_data` keys already present in production data (`mcap`, `market_cap`, `market_cap_usd`, `mcap_at_sighting`, `alert_market_cap`);
- `trade_volume_spikes` must carry `VolumeSpike.market_cap` into `signal_data` as `mcap`;
- for `chain_completed`, latest `chain_matches.mcap_at_completion` before generic cache fallback;
- then `price_cache.market_cap` for the token.

Source confluence count is `max(parsed signal_combo parts, conviction_stack)`. Combo parts are derived from `signal_combo` split on `+`, `|`, `/`, `,`, semicolon, or whitespace; if empty, count is 1.

Actionability is separate from existing live eligibility:
- `would_be_live=1` remains the live-evaluable/live-slot cohort.
- `actionable=1 AND actionability_version='v1'` means the row passed this audit-derived paper actionability classifier.
- A future live-readiness predicate, if needed, must combine both: `would_be_live=1 AND actionable=1 AND actionability_version='v1'`.
- `narrative_prediction` can be actionability-positive while `would_be_live=0`; that is intentional because it is profitable in paper but structurally not live-eligible under current Tier 1/2 rules.

Schema cohort predicate:
- Legacy/raw/unclassified rows must not silently enter the v1 cohort.
- `paper_trades.actionable` will be nullable with no default.
- Queries must use `actionable = 1 AND actionability_version = 'v1'` for the v1 actionable cohort.

## Plan Review Fold

Two independent plan reviewers completed on 2026-05-19.

- Statistical/product review verdict: `APPROVE_WITH_CHANGES`. Folded changes: clarified `gainers_early` only passes at `>=50m`; added the chain-completed missing-mcap exception because total chain-completed edge is strong but bucket coverage is incomplete; kept X/TG/no-peak deferrals.
- Structural/API review verdict: `BLOCK`. Folded changes: added DB-side market-cap enrichment before classification; changed schema from `NOT NULL DEFAULT 1` to nullable/no default so historical rows are not falsely evaluated; added required migration-marker post-assertion and timestamp-preservation tests; added engine-path tests with real signal-data shapes.
- Design review verdicts: both `APPROVE_WITH_CHANGES`. Folded changes: carry volume-spike mcap at dispatch; make enrichment catch query failures internally; fail closed for `gainers_early` stack-compute failures without suppressing paper opens; document `actionability_reason` as first matching `TEXT` reason; add upgrade, stack-failure, and persisted-signal-data immutability tests.

## Task 1: Backlog And Todo Fold

**Files:**
- Modify: `backlog.md`
- Modify: `tasks/todo.md`

- [ ] **Step 1: Add Actionability Gate active work to todo**

Insert near the top of `tasks/todo.md`:

```markdown
## Active Work: BL-NEW-ACTIONABILITY-GATE-V1

- [x] Isolated worktree created on `codex/actionability-gate-v1`
- [x] Audit artifacts cherry-picked: `tasks/findings_profit_patterns_2026_05_19.md`, `scripts/analyze_profit_patterns.py`
- [x] Drift check: existing `would_be_live` is live-slot eligibility, not actionability
- [x] Hermes-first check: no actionability-gate primitive; reuse Hermes X/KOL only as raw telemetry
- [ ] Plan drafted and reviewed
- [ ] Design drafted and reviewed
- [ ] TDD implementation
- [ ] PR opened and reviewed by 3 agents
```

- [ ] **Step 2: Add backlog entries**

Add a `BL-NEW-ACTIONABILITY-GATE` entry if absent, otherwise update the existing entry to `FINDINGS-READY`, pointing at `tasks/findings_profit_patterns_2026_05_19.md`. Add children:

```markdown
### BL-NEW-ACTIONABILITY-GATE-V1-IMPLEMENT
**Status:** PLANNED
**Why:** Current paper trades mix actionable and exploratory cohorts. Recent findings show profitable and junk patterns differ sharply; paper decision-quality cohorts need an explicit actionability flag. This is separate from `would_be_live` live-slot eligibility.
**Scope:** Add `paper_trades.actionable`, `actionability_reason`, and `actionability_version`; stamp via a pure classifier at open time; keep exploratory paper rows.
```

```markdown
### BL-NEW-X-OUTCOME-LINKAGE
**Status:** PROPOSED
**Why:** X handle ranking is blocked: 215 X alerts had 0 priced outcomes because `resolved_coin_id`/pricing linkage is missing.
**Scope:** Persist `resolved_coin_id`, `x_handle`, outcome status, entry/current price, and $300 notional P&L.
```

```markdown
### BL-NEW-TG-OUTCOME-LINKAGE
**Status:** PROPOSED
**Why:** TG channel ranking is blocked: only 2 current-regime closed linked trades, both low-n losses.
**Scope:** Persist and dashboard `tg_channel`, `resolution_state`, `posted_at`, `paper_trade_id`, and `mcap_at_sighting`.
```

```markdown
### BL-NEW-NO-PEAK-RISK-HANDLING
**Status:** PROPOSED
**Why:** `no_peak_<5` current-regime bucket is deeply negative (-$6,090.86 / n=99). Exit/risk handling needs separate design.
**Scope:** Design a peak<5 early-exit or hard-risk policy; do not mix into Actionability Gate v1.
```

- [ ] **Step 3: Commit docs fold**

Run:

```bash
git add backlog.md tasks/todo.md
git commit -m "docs: fold actionability gate backlog"
```

## Task 2: TDD Pure Gate

**Files:**
- Create: `scout/trading/actionability.py`
- Create: `tests/test_actionability.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_actionability.py` with tests for:

```python
from scout.trading.actionability import evaluate_actionability_v1


def _decision(signal_type, *, signal_data=None, signal_combo=None):
    return evaluate_actionability_v1(
        signal_type=signal_type,
        signal_data=signal_data or {},
        signal_combo=signal_combo or signal_type,
        conviction_stack=0,
    )


def test_narrative_prediction_passes_with_10_50m_mcap():
    d = _decision("narrative_prediction", signal_data={"mcap": 20_000_000})
    assert d.actionable is True
    assert d.reason == "v1_pass_core_signal_mcap_10_50m"
    assert d.version == "v1"


def test_chain_completed_passes_with_over_50m_mcap():
    d = _decision("chain_completed", signal_data={"market_cap": 75_000_000})
    assert d.actionable is True
    assert d.reason == "v1_pass_core_signal_mcap_50m_plus"


def test_chain_completed_missing_mcap_uses_explicit_exception():
    d = _decision("chain_completed", signal_data={})
    assert d.actionable is True
    assert d.reason == "v1_pass_chain_completed_mcap_unknown_exception"


def test_volume_spike_passes_with_10_50m_mcap():
    d = _decision("volume_spike", signal_data={"market_cap_usd": 12_000_000})
    assert d.actionable is True


def test_losers_contrarian_is_non_actionable_by_default():
    d = _decision("losers_contrarian", signal_data={"mcap": 8_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_losers_contrarian_exploratory"


def test_trending_catch_is_non_actionable_by_default():
    d = _decision("trending_catch", signal_data={"mcap": 80_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_trending_catch_low_n"


def test_gainers_early_blocks_5_to_10m():
    d = _decision("gainers_early", signal_data={"mcap": 7_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_mcap_5_10m"


def test_gainers_early_blocks_confluence_3():
    d = _decision(
        "gainers_early",
        signal_data={"mcap": 80_000_000},
        signal_combo="gainers_early+cg_trending_rank+momentum_ratio",
    )
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_confluence_3"


def test_gainers_early_blocks_conviction_stack_3_when_combo_is_pair_capped():
    d = evaluate_actionability_v1(
        signal_type="gainers_early",
        signal_data={"mcap": 80_000_000},
        signal_combo="gainers_early+momentum_ratio",
        conviction_stack=3,
    )
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_confluence_3"


def test_gainers_early_over_50m_passes_when_confluence_below_3():
    d = _decision("gainers_early", signal_data={"mcap": 80_000_000})
    assert d.actionable is True
    assert d.reason == "v1_pass_gainers_early_mcap_50m_plus"


def test_gainers_early_10_to_50m_blocks_as_observe():
    d = _decision("gainers_early", signal_data={"mcap": 20_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_gainers_early_mcap_10_50m_observe"


def test_unknown_mcap_blocks_explicitly_for_non_chain_signal():
    d = _decision("narrative_prediction", signal_data={})
    assert d.actionable is False
    assert d.reason == "v1_block_missing_mcap"


def test_mcap_extraction_continues_after_invalid_candidate_key():
    d = _decision(
        "volume_spike",
        signal_data={"mcap": "unknown", "market_cap_usd": 12_000_000},
    )
    assert d.actionable is True
    assert d.reason == "v1_pass_core_signal_mcap_10_50m"


def test_unknown_signal_blocks_explicitly():
    d = _decision("new_signal", signal_data={"mcap": 20_000_000})
    assert d.actionable is False
    assert d.reason == "v1_block_unknown_signal_type"
```

- [ ] **Step 2: Verify red**

Run:

```bash
python -m pytest tests/test_actionability.py --tb=short -q
```

Expected: import/module failure because `scout.trading.actionability` does not exist.

- [ ] **Step 3: Implement minimal pure function**

Create `scout/trading/actionability.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionabilityDecision:
    actionable: bool
    reason: str
    version: str = "v1"


def evaluate_actionability_v1(
    *,
    signal_type: str,
    signal_data: dict[str, Any],
    signal_combo: str | None,
    conviction_stack: int = 0,
) -> ActionabilityDecision:
    mcap = _extract_mcap(signal_data)
    if mcap is None or mcap <= 0:
        if signal_type == "chain_completed":
            return ActionabilityDecision(
                True, "v1_pass_chain_completed_mcap_unknown_exception"
            )
        return ActionabilityDecision(False, "v1_block_missing_mcap")

    confluence = max(
        _source_confluence_count(signal_combo or signal_type),
        int(conviction_stack or 0),
    )

    if signal_type in {"narrative_prediction", "chain_completed", "volume_spike"}:
        if 10_000_000 <= mcap < 50_000_000:
            return ActionabilityDecision(True, "v1_pass_core_signal_mcap_10_50m")
        if mcap >= 50_000_000:
            return ActionabilityDecision(True, "v1_pass_core_signal_mcap_50m_plus")
        return ActionabilityDecision(False, "v1_block_core_signal_mcap_below_10m")

    if signal_type == "gainers_early":
        if 5_000_000 <= mcap < 10_000_000:
            return ActionabilityDecision(False, "v1_block_gainers_early_mcap_5_10m")
        if confluence >= 3:
            return ActionabilityDecision(False, "v1_block_gainers_early_confluence_3")
        if mcap >= 50_000_000:
            return ActionabilityDecision(True, "v1_pass_gainers_early_mcap_50m_plus")
        if mcap >= 10_000_000:
            return ActionabilityDecision(
                False, "v1_block_gainers_early_mcap_10_50m_observe"
            )
        return ActionabilityDecision(False, "v1_block_gainers_early_not_50m_plus")

    if signal_type == "losers_contrarian":
        return ActionabilityDecision(False, "v1_block_losers_contrarian_exploratory")
    if signal_type == "trending_catch":
        return ActionabilityDecision(False, "v1_block_trending_catch_low_n")
    if signal_type == "tg_social":
        return ActionabilityDecision(False, "v1_block_tg_social_low_n")

    return ActionabilityDecision(False, "v1_block_unknown_signal_type")


def _extract_mcap(signal_data: dict[str, Any]) -> float | None:
    for key in ("mcap", "market_cap", "market_cap_usd", "mcap_at_sighting", "alert_market_cap"):
        value = signal_data.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _source_confluence_count(signal_combo: str) -> int:
    import re

    parts = [p for p in re.split(r"[+,|;/\s]+", signal_combo) if p]
    return max(1, len(set(parts)))
```

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_actionability.py --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/actionability.py tests/test_actionability.py
git commit -m "feat: add actionability gate classifier"
```

## Task 3: TDD Schema Migration

**Files:**
- Modify: `scout/db.py`
- Modify: `tests/test_trading_db_migration.py`

- [ ] **Step 1: Write failing migration tests**

Append tests asserting:

```python
async def test_migration_adds_actionability_columns(tmp_path):
    db = Database(tmp_path / "actionability.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1]: row for row in await cur.fetchall()}
    assert "actionable" in cols
    assert "actionability_reason" in cols
    assert "actionability_version" in cols
    assert cols["actionable"][2] == "INTEGER"
    assert cols["actionable"][3] == 0
    assert cols["actionable"][4] is None
    await db.close()


async def test_migration_records_actionability_marker(tmp_path):
    db = Database(tmp_path / "actionability_marker.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name=?",
        ("bl_new_actionability_gate_v1",),
    )
    assert await cur.fetchone() is not None
    await db.close()


async def test_actionability_marker_timestamp_preserved_on_reinitialize(tmp_path):
    db_path = tmp_path / "actionability_marker_idempotent.db"
    db = Database(db_path)
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name=?",
        ("bl_new_actionability_gate_v1",),
    )
    first = (await cur.fetchone())[0]
    await db.close()

    db2 = Database(db_path)
    await db2.initialize()
    cur = await db2._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name=?",
        ("bl_new_actionability_gate_v1",),
    )
    second = (await cur.fetchone())[0]
    assert second == first
    await db2.close()


async def test_actionability_columns_preserve_pre_cutover_nulls(tmp_path):
    db = Database(tmp_path / "actionability_precutover.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        "entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy",
            "LEG",
            "Legacy",
            "coingecko",
            "gainers_early",
            "{}",
            1.0,
            300.0,
            300.0,
            20.0,
            10.0,
            1.2,
            0.9,
            "open",
            "2026-05-01T00:00:00+00:00",
        ),
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version "
        "FROM paper_trades WHERE token_id='legacy'"
    )
    row = await cur.fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
    await db.close()


async def test_initialize_upgrades_pre_actionability_db(tmp_path):
    db_path = tmp_path / "pre_actionability.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript("""
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                chain TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_data TEXT NOT NULL,
                entry_price REAL NOT NULL,
                amount_usd REAL NOT NULL,
                quantity REAL NOT NULL,
                tp_pct REAL NOT NULL DEFAULT 20.0,
                sl_pct REAL NOT NULL DEFAULT 10.0,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                opened_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                would_be_live INTEGER
            );
        """)
        await conn.commit()

    db = Database(db_path)
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert {"actionable", "actionability_reason", "actionability_version"} <= cols
    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name=?",
        ("bl_new_actionability_gate_v1",),
    )
    assert await cur.fetchone() is not None
    await db.close()
```

- [ ] **Step 2: Verify red**

Run:

```bash
python -m pytest tests/test_trading_db_migration.py::test_migration_adds_actionability_columns tests/test_trading_db_migration.py::test_migration_records_actionability_marker tests/test_trading_db_migration.py::test_actionability_marker_timestamp_preserved_on_reinitialize tests/test_trading_db_migration.py::test_actionability_columns_preserve_pre_cutover_nulls tests/test_trading_db_migration.py::test_initialize_upgrades_pre_actionability_db --tb=short -q
```

Expected: columns/marker missing.

- [ ] **Step 3: Add schema columns**

Update `scout/db.py::_create_tables` `paper_trades` schema with:

```sql
actionable INTEGER,
actionability_reason TEXT,
actionability_version TEXT,
```

Update `_migrate_feedback_loop_schema.expected_cols` with:

```python
"actionable": "INTEGER",
"actionability_reason": "TEXT",
"actionability_version": "TEXT",
```

Add `paper_migrations` marker:

```python
await conn.execute(
    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
    ("bl_new_actionability_gate_v1", datetime.now(timezone.utc).isoformat()),
)
```

Also add `bl_new_actionability_gate_v1` to the hardcoded migration post-assertion list:

```python
"'bl_new_actionability_gate_v1')"
```

and to the `missing_migrations` set. This keeps the migration consistent with the existing defensive marker assertion pattern.

- [ ] **Step 4: Verify green**

Run the five migration tests again. Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add scout/db.py tests/test_trading_db_migration.py
git commit -m "feat: add actionability paper-trade columns"
```

## Task 4: TDD Stamp Paper Trades

**Files:**
- Modify: `scout/trading/paper.py`
- Modify: `scout/trading/signals.py`
- Modify: `tests/test_trading_signals.py`
- Modify: `tests/test_live_eligibility.py` or create `tests/test_paper_actionability.py`

- [ ] **Step 1: Write failing paper-stamp tests**

Create `tests/test_paper_actionability.py`:

```python
import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.paper import PaperTrader


def _settings(**overrides):
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="x",
        ANTHROPIC_API_KEY="x",
        **overrides,
    )


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "paper_actionability.db")
    await d.initialize()
    yield d
    await d.close()


async def _open(trader, db, *, signal_type, signal_data, signal_combo=None):
    trade_id = await trader.execute_buy(
        db=db,
        token_id=f"{signal_type}-tok",
        symbol="TOK",
        name="Token",
        chain="coingecko",
        signal_type=signal_type,
        signal_data=signal_data,
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo=signal_combo or signal_type,
        settings=_settings(),
    )
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    return await cur.fetchone()


@pytest.mark.asyncio
async def test_execute_buy_stamps_actionable_true_for_narrative_prediction(db):
    row = await _open(
        PaperTrader(),
        db,
        signal_type="narrative_prediction",
        signal_data={"mcap": 20_000_000},
    )
    assert row["actionable"] == 1
    assert row["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_execute_buy_stamps_actionable_false_for_gainers_early_5_10m(db):
    row = await _open(
        PaperTrader(),
        db,
        signal_type="gainers_early",
        signal_data={"mcap": 7_000_000},
    )
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_gainers_early_mcap_5_10m"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_execute_buy_without_settings_still_classifies_actionability(db):
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="compat",
        symbol="CMP",
        name="Compat",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"mcap": 20_000_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="narrative_prediction",
    )
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row["actionable"] == 1
    assert row["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_execute_buy_without_settings_still_classifies_long_hold_non_actionable(db):
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="long-hold",
        symbol="LH",
        name="Long Hold",
        chain="coingecko",
        signal_type="long_hold",
        signal_data={"mcap": 20_000_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="long_hold",
    )
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_unknown_signal_type"
    assert row["actionability_version"] == "v1"


@pytest.mark.asyncio
async def test_actionability_enrichment_does_not_mutate_persisted_signal_data(db):
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) VALUES (?, ?, ?, ?)",
        ("immut", 1.0, 20_000_000, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="immut",
        symbol="IMM",
        name="Immutable",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="volume_spike",
    )
    cur = await db._conn.execute(
        "SELECT signal_data, actionable FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert json.loads(row["signal_data"]) == {"spike_ratio": 12.3}
    assert row["actionable"] == 1
```

Add a signal dispatch test in `tests/test_trading_signals.py`:

```python
async def test_trade_volume_spikes_passes_mcap_to_actionability_signal_data(db, settings):
    captured = {}

    class EngineSpy:
        async def open_trade(self, **kwargs):
            captured.update(kwargs)
            return 1

    spike = VolumeSpike(
        coin_id="vol-mcap",
        symbol="VM",
        name="VolMcap",
        current_volume=600_000,
        avg_volume_7d=100_000,
        spike_ratio=6.0,
        market_cap=20_000_000,
        price=1.0,
        detected_at=datetime.now(timezone.utc),
    )
    await trade_volume_spikes(EngineSpy(), db, [spike], settings)
    assert captured["signal_data"]["mcap"] == 20_000_000
```

Add an engine-level test in `tests/test_trading_engine.py` proving actionability metadata does not block exploratory paper:

```python
async def test_open_trade_stamps_non_actionable_but_still_opens(engine, db):
    trade_id = await engine.open_trade(
        token_id="loser-probe",
        symbol="LP",
        name="LoserProbe",
        chain="coingecko",
        signal_type="losers_contrarian",
        signal_data={"mcap": 20_000_000},
        entry_price=1.0,
        signal_combo="losers_contrarian",
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_losers_contrarian_exploratory"
    assert row["actionability_version"] == "v1"
```

Add an engine-level DB-fallback test in `tests/test_trading_engine.py` proving real signal-data shapes are enriched before classification:

```python
async def test_open_trade_enriches_actionability_mcap_from_price_cache(engine, db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) VALUES (?, ?, ?, ?)",
        ("vol-no-mcap", 1.0, 20_000_000, now),
    )
    await db._conn.commit()

    trade_id = await engine.open_trade(
        token_id="vol-no-mcap",
        symbol="VNM",
        name="VolumeNoMcap",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        entry_price=1.0,
        signal_combo="volume_spike",
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT actionable, actionability_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row["actionable"] == 1
    assert row["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"
```

Add a stack-failure regression in `tests/test_paper_actionability.py`:

```python
@pytest.mark.asyncio
async def test_gainers_early_stack_failure_fails_closed_but_opens(db, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("forced stack failure")

    monkeypatch.setattr("scout.trading.paper.compute_stack", boom)
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="stack-fail",
        symbol="SF",
        name="Stack Fail",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={"mcap": 80_000_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="gainers_early",
    )
    assert trade_id is not None
    cur = await db._conn.execute(
        "SELECT actionable, actionability_reason FROM paper_trades WHERE id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_gainers_early_stack_unavailable"
```

Add a live-handoff regression in `tests/live/test_paper_chokepoint.py`:

```python
class _LiveEngineSpy:
    def __init__(self):
        self.handoffs = []

    def is_eligible(self, signal_type):
        return True

    async def on_paper_trade_opened(self, handoff):
        self.handoffs.append(handoff)


async def test_actionability_stamp_does_not_change_live_handoff(db):
    live = _LiveEngineSpy()
    trader = PaperTrader(live_engine=live)
    trade_id = await trader.execute_buy(
        db=db,
        token_id="non-actionable-live-handoff",
        symbol="NAL",
        name="Non Actionable Live Handoff",
        chain="coingecko",
        signal_type="losers_contrarian",
        signal_data={"mcap": 20_000_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="losers_contrarian",
        settings=_settings(),
    )
    await asyncio.sleep(0)
    assert trade_id is not None
    assert [h.id for h in live.handoffs] == [trade_id]
```

This locks the invariant: v1 actionability metadata does not suppress exploratory paper opens or alter the existing live handoff allowlist behavior.

- [ ] **Step 2: Verify red**

Run:

```bash
python -m pytest tests/test_paper_actionability.py --tb=short -q
```

Expected: insert does not populate expected columns or columns missing before Task 3 is complete.

- [ ] **Step 3: Wire classifier into `PaperTrader.execute_buy`**

Import:

```python
from scout.trading.actionability import ActionabilityDecision, evaluate_actionability_v1
```

Before `INSERT_SQL`, compute actionability unconditionally. Reuse the same `stack` already needed by `compute_would_be_live`; if `settings is None`, still compute stack for actionability because the classifier is pure and production has a direct `PaperTrader.execute_buy` caller for `long_hold` without settings.

Add a private async helper near `execute_buy` to enrich market cap without changing the pure classifier:

```python
async def _enrich_actionability_signal_data(
    db: Database,
    *,
    token_id: str,
    signal_type: str,
    signal_data: dict,
) -> dict:
    enriched = dict(signal_data)
    if _has_mcap(enriched):
        return enriched

    conn = db._conn
    if conn is None:
        return enriched

    try:
        if signal_type == "chain_completed":
            cur = await conn.execute(
                "SELECT mcap_at_completion FROM chain_matches "
                "WHERE token_id=? AND mcap_at_completion IS NOT NULL "
                "ORDER BY datetime(completed_at) DESC LIMIT 1",
                (token_id,),
            )
            row = await cur.fetchone()
            if row and row[0] not in (None, ""):
                enriched["mcap"] = row[0]
                return enriched

        cur = await conn.execute(
            "SELECT market_cap FROM price_cache WHERE coin_id=?",
            (token_id,),
        )
        row = await cur.fetchone()
        if row and row[0] not in (None, ""):
            enriched["mcap"] = row[0]
    except Exception:
        log.exception(
            "actionability_mcap_enrichment_failed",
            token_id=token_id,
            signal_type=signal_type,
        )
    return enriched
```

The helper catches query failures internally, logs `actionability_mcap_enrichment_failed`, and returns the original payload. This keeps paper opening non-blocking and preserves the `chain_completed` missing-mcap exception.

```python
stack_for_actionability = 0
try:
    if signal_type not in ("chain_completed", "volume_spike"):
        stack_for_actionability = await compute_stack(db, token_id, now)
except Exception:
    log.exception(
        "actionability_stack_compute_failed",
        token_id=token_id,
        signal_type=signal_type,
    )
    if signal_type == "gainers_early":
        actionability = ActionabilityDecision(
            False, "v1_block_gainers_early_stack_unavailable", "v1"
        )
    stack_for_actionability = 0

try:
    if actionability is None:
        actionability_signal_data = await _enrich_actionability_signal_data(
            db,
            token_id=token_id,
            signal_type=signal_type,
            signal_data=signal_data,
        )
        actionability = evaluate_actionability_v1(
            signal_type=signal_type,
            signal_data=actionability_signal_data,
            signal_combo=signal_combo,
            conviction_stack=stack_for_actionability,
        )
except Exception:
    log.exception(
        "actionability_gate_failed",
        token_id=token_id,
        signal_type=signal_type,
    )
    actionability = ActionabilityDecision(False, "v1_error", "v1")
```

Initialize `actionability: ActionabilityDecision | None = None` before stack computation. Any actionability exception must be caught and converted to metadata; it must never prevent the paper row insert.

Then compute `would_be_live` using `stack_for_actionability` when needed so stack is not recomputed:

```python
if settings is not None:
    try:
        would_be_live = await compute_would_be_live(
            db,
            signal_type=signal_type,
            signal_data=signal_data,
            conviction_stack=stack_for_actionability,
            settings=settings,
        )
    except Exception:
        log.exception(
            "would_be_live_stamp_failed",
            token_id=token_id,
            signal_type=signal_type,
        )
        would_be_live = 0
```

Extend insert columns/values:

```sql
actionable, actionability_reason, actionability_version
```

with values:

```python
actionable_value = 1 if actionability.actionable else 0
actionability_reason = actionability.reason
actionability_version = actionability.version
```

There is no `settings is None` bypass for actionability. Raw SQL/backward-compat rows can remain `NULL`, but the main `PaperTrader` writer must stamp explicit `0/1`, reason, and version.

Add structured log fields to `paper_trade_opened`:

```python
actionable=actionable_value,
actionability_reason=actionability_reason,
actionability_version=actionability_version,
```

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_actionability.py tests/test_paper_actionability.py tests/test_live_eligibility.py tests/test_trading_signals.py::test_trade_volume_spikes_passes_mcap_to_actionability_signal_data tests/test_trading_engine.py::test_open_trade_stamps_non_actionable_but_still_opens tests/test_trading_engine.py::test_open_trade_enriches_actionability_mcap_from_price_cache tests/live/test_paper_chokepoint.py::test_actionability_stamp_does_not_change_live_handoff --tb=short -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/paper.py scout/trading/signals.py tests/test_paper_actionability.py tests/test_trading_signals.py tests/test_trading_engine.py tests/live/test_paper_chokepoint.py
git commit -m "feat: stamp paper-trade actionability"
```

## Task 5: Verification And PR

**Files:**
- Modify: `tasks/todo.md`
- Optional memory file: `C:\Users\srini\.claude\projects\C--projects-gecko-alpha\memory\project_actionability_gate_v1_<date>.md`

- [ ] **Step 1: Run focused verification**

Use the provisioned project venv if `uv` cannot build in this worktree:

```powershell
$env:PYTHONPATH=(Get-Location).Path
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_actionability.py tests/test_paper_actionability.py tests/test_live_eligibility.py tests/test_trading_engine.py tests/test_trading_db_migration.py tests/live/test_paper_chokepoint.py --tb=short -q
```

Expected: relevant tests pass. Record exact counts.

- [ ] **Step 2: Run adjacent trading tests**

```powershell
$env:PYTHONPATH=(Get-Location).Path
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_trading_*.py tests/live/test_paper_chokepoint.py tests/test_live_eligibility.py tests/test_actionability.py tests/test_paper_actionability.py --tb=short -q
```

Expected: pass or document unrelated baseline failures with evidence.

- [ ] **Step 3: Update todo review**

Add verification counts, PR link, reviewer status, and deferrals:
- X/TG ranking deferred until outcome linkage exists.
- Peak<5 exit changes deferred to separate design.
- Dashboard deferred unless separate PR.
- Live trading unchanged.

- [ ] **Step 4: Create PR**

Run:

```bash
git status --short
gh pr create --base master --head codex/actionability-gate-v1 --title "feat: add Actionability Gate v1 paper-trade metadata" --body-file tasks/pr_actionability_gate_v1.md
```

If `gh` is unavailable/auth-blocked, record that and provide branch/commit for manual PR creation.

## Plan Self-Review

- Spec coverage: covers Priority 0, 1, and 2; explicitly defers Priority 3 dashboard and Priority 4 exit logic.
- Placeholder scan: no TODO/TBD placeholders; all implementation steps name exact files and commands.
- Type consistency: `ActionabilityDecision(actionable, reason, version)` is consistent across tests, module, and insert wiring.
- Risk note: v1 requires market cap for core pass. If plan reviewers decide `chain_completed` should pass despite missing mcap because its n=16 cohort was strong with some unknown rows, fold before design/build.
