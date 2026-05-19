**New primitives introduced:** `scout.trading.actionability.ActionabilityDecision`; `scout.trading.actionability.evaluate_actionability_v1`; three `paper_trades` columns (`actionable`, `actionability_reason`, `actionability_version`); migration marker `bl_new_actionability_gate_v1`; follow-up backlog entries for X/TG outcome linkage and no-peak risk handling.

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
- Existing schema migration hook: `scout/db.py::_migrate_feedback_loop_schema` additively manages `paper_trades` columns and indexes.
- No existing `actionable`, `actionability_reason`, or `actionability_version` columns/functions were found.
- `peak_pct` is not available at trade-open time; it is populated by evaluator after monitoring, so no-peak/peak-giveback changes are a separate exit/risk follow-up, not part of Actionability Gate v1.
- `x_handle`, `tg_channel`, and liquidity are not reliable closed-trade segmentation fields yet per `tasks/findings_profit_patterns_2026_05_19.md`; v1 must not rank by those fields.

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Paper-trade actionability attribution | none found in installed VPS skills or public Skills Hub | build from scratch; this is gecko-alpha DB row metadata and cohort logic |
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
- `narrative_prediction`: actionable when entry market cap is usable and not below floor.
- `chain_completed`: actionable when entry market cap is usable and not below floor.
- `volume_spike`: actionable when entry market cap is usable and not below floor.
- `gainers_early`: non-actionable for `5m <= mcap < 10m`; non-actionable when source confluence count is at least 3; actionable only when `mcap >= 50m`.
- `losers_contrarian`: non-actionable by default.
- `trending_catch`: non-actionable by default.
- `tg_social`: non-actionable by default.
- Unknown/missing market cap: non-actionable with explicit reason.
- Unknown signal types: non-actionable with explicit reason.

Market-cap buckets are computed from `signal_data` using keys already present in production data (`mcap`, `market_cap`, `market_cap_usd`, `mcap_at_sighting`, `alert_market_cap`).

Source confluence count is derived from `signal_combo` split on `+`, `|`, `/`, `,`, semicolon, or whitespace; if empty, count is 1.

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
**Why:** Current paper trades mix actionable and exploratory cohorts. Recent findings show profitable and junk patterns differ sharply; paper/live-readiness decisions need an explicit actionability flag.
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


def test_gainers_early_over_50m_passes_when_confluence_below_3():
    d = _decision("gainers_early", signal_data={"mcap": 80_000_000})
    assert d.actionable is True
    assert d.reason == "v1_pass_gainers_early_mcap_50m_plus"


def test_unknown_mcap_blocks_explicitly():
    d = _decision("narrative_prediction", signal_data={})
    assert d.actionable is False
    assert d.reason == "v1_block_missing_mcap"


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
) -> ActionabilityDecision:
    mcap = _extract_mcap(signal_data)
    if mcap is None or mcap <= 0:
        return ActionabilityDecision(False, "v1_block_missing_mcap")

    confluence = _source_confluence_count(signal_combo or signal_type)

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
            return None
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
    assert cols["actionable"][3] == 1
    assert cols["actionable"][4] == "1"
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
```

- [ ] **Step 2: Verify red**

Run:

```bash
python -m pytest tests/test_trading_db_migration.py::test_migration_adds_actionability_columns tests/test_trading_db_migration.py::test_migration_records_actionability_marker --tb=short -q
```

Expected: columns/marker missing.

- [ ] **Step 3: Add schema columns**

Update `scout/db.py::_create_tables` `paper_trades` schema with:

```sql
actionable INTEGER NOT NULL DEFAULT 1,
actionability_reason TEXT,
actionability_version TEXT,
```

Update `_migrate_feedback_loop_schema.expected_cols` with:

```python
"actionable": "INTEGER NOT NULL DEFAULT 1",
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

- [ ] **Step 4: Verify green**

Run the two migration tests again. Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add scout/db.py tests/test_trading_db_migration.py
git commit -m "feat: add actionability paper-trade columns"
```

## Task 4: TDD Stamp Paper Trades

**Files:**
- Modify: `scout/trading/paper.py`
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
async def test_execute_buy_without_settings_defaults_existing_rows_actionable(db):
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
    assert row["actionability_reason"] is None
    assert row["actionability_version"] is None
```

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

Before `INSERT_SQL`, compute:

```python
actionability = ActionabilityDecision(True, "v1_not_evaluated", "v1")
if settings is not None:
    try:
        actionability = evaluate_actionability_v1(
            signal_type=signal_type,
            signal_data=signal_data,
            signal_combo=signal_combo,
        )
    except Exception:
        log.exception(
            "actionability_gate_failed",
            token_id=token_id,
            signal_type=signal_type,
        )
        actionability = ActionabilityDecision(False, "v1_error", "v1")
```

Extend insert columns/values:

```sql
actionable, actionability_reason, actionability_version
```

with values:

```python
1 if actionability.actionable else 0,
actionability.reason,
actionability.version,
```

For `settings is None`, use database defaults by setting:

```python
actionable_value = 1
actionability_reason = None
actionability_version = None
```

Add structured log fields to `paper_trade_opened`:

```python
actionable=actionable_value,
actionability_reason=actionability_reason,
actionability_version=actionability_version,
```

- [ ] **Step 4: Verify green**

Run:

```bash
python -m pytest tests/test_actionability.py tests/test_paper_actionability.py tests/test_live_eligibility.py --tb=short -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add scout/trading/paper.py tests/test_paper_actionability.py
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
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_actionability.py tests/test_paper_actionability.py tests/test_live_eligibility.py tests/test_trading_engine.py tests/test_trading_db_migration.py --tb=short -q
```

Expected: relevant tests pass. Record exact counts.

- [ ] **Step 2: Run adjacent trading tests**

```powershell
$env:PYTHONPATH=(Get-Location).Path
C:\projects\gecko-alpha\.venv\Scripts\python.exe -m pytest tests/test_trading_*.py tests/test_live_eligibility.py tests/test_actionability.py tests/test_paper_actionability.py --tb=short -q
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
