**New primitives introduced:** New `signal_params.tg_alert_eligible` BOOLEAN column (default 0); migration `bl_tg_alert_eligible_v1` (schema 20260516, stamps `schema_version` + `paper_migrations` cutover row, mirrors `_migrate_bl_quote_pair_v1` structure). New module `scout/trading/tg_alert_dispatch.py` exposing `notify_paper_trade_opened(db, settings, session, paper_trade_id, signal_type, ...)` with per-signal allowlist + per-token cooldown. New helper `format_paper_trade_alert(...)` producing concise single-line Telegram body with `parse_mode=None` (R1-C1 fold: avoids the silent-400 Markdown class caught in PR #76). New post-open hook in `scout/trading/engine.py` after `execute_buy` returns trade_id, using `asyncio.create_task` + `_tg_alert_tasks: set[asyncio.Task]` ref-holding pattern (R1-C3 fold: mirrors `scout/main.py:91` `_social_restart_tasks`). Engine post-slip entry price re-read after open via `SELECT entry_price FROM paper_trades WHERE id=?` (R1-C2 fold). New Settings `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 6` (R2-I1 fold: per-token-across-signals, default 6h). Default eligibility set in migration: gainers_early=1, narrative_prediction=1, losers_contrarian=1, volume_spike=1. **chain_completed=0 (R2-C2 fold)** — the existing `scout/chains/alerts.py` chain-pattern-completion alert already carries chain_completed; new paper-trade-open dispatch suppresses to avoid duplicate alerts on the same event. First-deploy operator announcement message via `scout/main.py` startup logging (R2-I3+I4 fold). Operator can audit-trail-flip eligibility via `UPDATE signal_params SET tg_alert_eligible=N WHERE signal_type=...`.

# TG Alert Allowlist (Option B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Send real-time Telegram alerts when proven paper-trade signals open, scoped via a per-signal allowlist with a provisional n≥30 gate for new signals. Default eligibility = the 4 statistically-validated signals (gainers_early, narrative_prediction, losers_contrarian, volume_spike) + chain_completed under provisional gate.

**Architecture:** Introduce `signal_params.tg_alert_eligible` flag. After `engine.open_trade` successfully creates a paper_trade row, fire a post-open hook that:
1. Reads `tg_alert_eligible` for the signal_type — if 0, no-op
2. **Per-token cooldown** (R2-I1 fold): don't re-alert the same `token_id` within `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS` (6h default) regardless of signal_type. Operator scenario: bitcoin firing gainers_early at 10am + losers_contrarian at 4pm → 1 alert, not 2. Reduces signal-type-collision noise on a single token.
3. Format message + dispatch via `alerter.send_telegram_message(parse_mode=None)` (R1-C1 fold: avoid Markdown 400-error from underscores in `signal_type.upper()`)
4. Record fire in `tg_alert_log` table for audit + cooldown lookup

Failure modes (network, missing creds) are caught + logged; never block paper-trade dispatch.

**chain_completed exclusion** (R2-C2 fold): existing `scout/chains/alerts.py:59` already fires a Telegram alert when a chain pattern completes — that's the same event a chain_completed paper-trade-open would alert on. To avoid duplicate alerts (operator inbox sees one event = two pings), `chain_completed` is left at `tg_alert_eligible=0` in the default migration. The existing chain-pattern-completion path remains. Operator can opt-in via `UPDATE signal_params SET tg_alert_eligible=1 WHERE signal_type='chain_completed'` if they want both, accepting the duplication.

**Provisional gate not introduced in M1** (R1-I4 fold): the prior plan's `TG_ALERT_PROVISIONAL_MIN_TRADES` gate is removed. With chain_completed excluded, no signal in the M1 default-allow list needs a provisional gate. If a future signal needs it, the gate can be added then; M1 keeps eligibility binary.

**Tech Stack:** Python 3.12, aiosqlite, aiohttp, pydantic v2 BaseSettings, structlog, pytest-asyncio (auto mode), aioresponses (HTTP mock).

**Total scope:** ~50-60 steps across 6 tasks. One schema migration. Zero breaking changes to existing TG dispatch surfaces (`scout/chains/alerts.py`, `scout/main.py:855`, research streams) — additive only.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/db.py` | Modify | Add migration `bl_tg_alert_eligible_v1` (schema 20260516): `ALTER TABLE signal_params ADD COLUMN tg_alert_eligible INTEGER NOT NULL DEFAULT 0`; UPDATE existing rows for default-allow signals; CREATE TABLE `tg_alert_log` (audit + cooldown source) with `outcome IN ('sent','blocked_eligibility','blocked_cooldown','dispatch_failed','announcement_sent')` (R2-C1 fold: 5th value for first-deploy announcement sentinel) |
| `scout/trading/auto_suspend.py` | Modify | `_atomic_suspend` sets `tg_alert_eligible=0` jointly with `enabled=0` (R2-I1 fold: joint flag maintenance); revive helper restores `=1` if signal was in default-allow set |
| `scout/main.py` | Modify | Pass `aiohttp.ClientSession` into `TradingEngine` constructor (R1-I3+I5 fold); add first-deploy announcement call after `Database.initialize()` gated on `'announcement_sent'` sentinel (R2-C1 + R1-I4 fold) |
| `scout/config.py` | Modify | Add `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 6` Settings |
| `scout/trading/tg_alert_dispatch.py` | **Create** | `notify_paper_trade_opened(...)` orchestrator + `format_paper_trade_alert(...)` formatter + `_check_eligibility`, `_check_cooldown` helpers. Pure async, no I/O beyond aiosqlite + alerter. |
| `scout/trading/engine.py` | Modify | After `trade_id = await self._paper_trader.execute_buy(...)` returns non-None, await `notify_paper_trade_opened(...)` (fire-and-forget pattern via task spawn so paper-trade dispatch never blocks on Telegram) |
| `tests/test_tg_alert_dispatch.py` | **Create** | Eligibility allowlist tests + provisional gate tests + per-token cooldown tests + format tests + tg_alert_log writer tests + failure-mode tests |
| `tests/test_engine_post_open_hook.py` | **Create** | Engine integration test — verify `notify_paper_trade_opened` fires on successful open + does NOT fire when allowlist=0 |

**Schema versions reserved:** `20260516` for `bl_tg_alert_eligible_v1`.

---

## Task 0: Setup — branch + Settings + migration

- [ ] **Step 1: Verify branch**

```bash
git branch --show-current
# Expected: feat/tg-alert-allowlist
```

- [ ] **Step 2: Add Settings fields**

In `scout/config.py` near other paper-trade Settings:

```python
    # BL-NEW-TG-ALERT-ALLOWLIST: per-signal Telegram alert dispatch on
    # paper-trade open. Eligibility is tracked per-signal in
    # signal_params.tg_alert_eligible.
    # Per-token cooldown (R2-I1 fold: per-token-across-signals, NOT
    # per-(signal,token), so a single token firing two different signals
    # within the window only alerts once).
    TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 6
```

- [ ] **Step 3: Add migration `bl_tg_alert_eligible_v1` to `scout/db.py`**

R1-I1 fold: mirror the BEGIN EXCLUSIVE + schema_version stamp + paper_migrations cutover row pattern of `_migrate_bl_quote_pair_v1` (db.py:2733-2837). Migration registration MUST be appended AFTER `_migrate_bl_slow_burn_v1` (currently last at db.py:102) so the existing `_migrate_signal_params_schema` (db.py:91) seeds the rows BEFORE the default-allow UPDATE runs (R1-I2 fold).

```python
async def _migrate_tg_alert_eligible_v1(self, conn) -> None:
    """BL-NEW-TG-ALERT-ALLOWLIST: per-signal TG alert eligibility.

    Schema version 20260516. Adds signal_params.tg_alert_eligible
    (default 0). Sets eligibility=1 for the 4 statistically-validated
    signals (gainers_early, narrative_prediction, losers_contrarian,
    volume_spike). chain_completed stays 0 because the existing
    scout/chains/alerts.py path already alerts on chain pattern
    completion; setting it 1 here would duplicate.

    Creates tg_alert_log for audit + cooldown lookup. ON DELETE SET NULL
    on paper_trade_id FK so future paper_trades cleanup doesn't block.
    """
    SCHEMA_VERSION = 20260516
    cur = await conn.execute(
        "SELECT 1 FROM schema_version WHERE version = ?", (SCHEMA_VERSION,)
    )
    if await cur.fetchone():
        return  # already applied
    # R1-C1 design-stage fold: drop _txn_lock to match _migrate_bl_quote_pair_v1
    # precedent. Migration runs at startup-only (single coroutine).
    await conn.execute("BEGIN EXCLUSIVE")
    try:
            cur = await conn.execute("PRAGMA table_info(signal_params)")
            cols = {row[1] for row in await cur.fetchall()}
            if "tg_alert_eligible" not in cols:
                await conn.execute(
                    "ALTER TABLE signal_params ADD COLUMN "
                    "tg_alert_eligible INTEGER NOT NULL DEFAULT 0"
                )
            for sig in (
                "gainers_early", "narrative_prediction",
                "losers_contrarian", "volume_spike",
            ):
                await conn.execute(
                    "UPDATE signal_params SET tg_alert_eligible=1 "
                    "WHERE signal_type = ?",
                    (sig,),
                )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS tg_alert_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_trade_id INTEGER REFERENCES paper_trades(id) ON DELETE SET NULL,
                    signal_type TEXT NOT NULL,
                    token_id    TEXT NOT NULL,
                    alerted_at  TEXT NOT NULL,
                    outcome     TEXT NOT NULL CHECK (outcome IN (
                        'sent','blocked_eligibility',
                        'blocked_cooldown','dispatch_failed',
                        'announcement_sent'
                    )),
                    detail      TEXT
                )"""
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_alert_log_token "
                "ON tg_alert_log(token_id, alerted_at)"
            )
            await conn.execute(
                "INSERT INTO schema_version (version, description, applied_at) "
                "VALUES (?, 'bl_tg_alert_eligible_v1', ?)",
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
            # Post-assertion: per-signal individual checks (R1-C2 design-stage
            # fold — robust to future signal-set expansion vs COUNT(*)=4).
            for sig in DEFAULT_ALLOW_SIGNALS:
                cur = await conn.execute(
                    "SELECT tg_alert_eligible FROM signal_params "
                    "WHERE signal_type = ?",
                    (sig,),
                )
                row = await cur.fetchone()
                assert row and row[0] == 1, (
                    f"bl_tg_alert_eligible_v1 post-assert: {sig} not eligible"
                )
            await conn.commit()
        except Exception:
            await conn.execute("ROLLBACK")
            raise
```

`DEFAULT_ALLOW_SIGNALS` is a module-level constant (`= ("gainers_early", "narrative_prediction", "losers_contrarian", "volume_spike")`). Same constant used by `auto_suspend.py` revive helper to restore `tg_alert_eligible=1` for signals in this set (R2-I1 fold).

- [ ] **Step 4: Failing test for migration**

```python
async def test_tg_alert_eligible_migration_default_allow(tmp_path):
    """Migration sets tg_alert_eligible=1 for the 4 proven + chain_completed."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT signal_type, tg_alert_eligible FROM signal_params "
        "ORDER BY signal_type"
    )
    rows = {r[0]: r[1] for r in await cur.fetchall()}
    assert rows["gainers_early"] == 1
    assert rows["narrative_prediction"] == 1
    assert rows["losers_contrarian"] == 1
    assert rows["volume_spike"] == 1
    assert rows["chain_completed"] == 1
    # Suspended signals stay 0
    assert rows["first_signal"] == 0
    assert rows["trending_catch"] == 0
    assert rows["tg_social"] == 0
    await db.close()
```

- [ ] **Step 5: Commit**

```bash
uv run --native-tls pytest tests/test_db_migrations.py -k tg_alert -q
git add scout/config.py scout/db.py tests/test_db_migrations.py
git commit -m "feat(tg-alert-allowlist): Settings + migration bl_tg_alert_eligible_v1 (Task 0)"
```

---

## Task 1: `tg_alert_dispatch.py` — orchestrator + helpers

**Files:**
- Create: `scout/trading/tg_alert_dispatch.py`
- Test: `tests/test_tg_alert_dispatch.py` (NEW)

- [ ] **Step 1: Failing tests**

```python
"""BL-NEW-TG-ALERT-ALLOWLIST: dispatch + gate tests."""

import pytest
from scout.config import Settings
from scout.db import Database
from scout.trading.tg_alert_dispatch import (
    notify_paper_trade_opened,
    format_paper_trade_alert,
    _check_eligibility,
    _check_cooldown,
)

_REQUIRED = {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "x", "ANTHROPIC_API_KEY": "x"}


@pytest.mark.asyncio
async def test_eligibility_allows_when_flag_is_1(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Migration default: gainers_early=1
    assert await _check_eligibility(db, "gainers_early") is True
    await db.close()


@pytest.mark.asyncio
async def test_eligibility_blocks_when_flag_is_0(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Migration default: first_signal=0 (suspended)
    assert await _check_eligibility(db, "first_signal") is False
    await db.close()


@pytest.mark.asyncio
async def test_eligibility_unknown_signal_blocked(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    assert await _check_eligibility(db, "bogus_signal") is False
    await db.close()


@pytest.mark.asyncio
async def test_eligibility_chain_completed_excluded_by_default(tmp_path):
    """R2-C2 fold: chain_completed defaults to tg_alert_eligible=0
    because the existing scout/chains/alerts.py path already alerts."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    assert await _check_eligibility(db, "chain_completed") is False
    await db.close()


@pytest.mark.asyncio
async def test_cooldown_blocks_within_window(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (now.isoformat(),),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "btc") is True


@pytest.mark.asyncio
async def test_cooldown_blocks_across_signals_for_same_token(tmp_path):
    """R2-I1 fold: per-token cooldown blocks DIFFERENT signal_type for
    the same token within the window."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (now.isoformat(),),
    )
    await db._conn.commit()
    # Different signal_type — should still block via per-token cooldown.
    assert await _check_cooldown(db, settings, "btc") is True


@pytest.mark.asyncio
async def test_cooldown_allows_after_window(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (old,),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "btc") is False


@pytest.mark.asyncio
async def test_cooldown_only_counts_sent_outcome(tmp_path):
    """Failed dispatches and blocked alerts don't count toward cooldown —
    so a transient failure doesn't suppress the next legitimate fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=6)
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'dispatch_failed')",
        (now,),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "btc") is False


@pytest.mark.asyncio
async def test_format_gainers_early_renders_dispatcher_fields(tmp_path):
    """R2-C1 fold: gainers_early dispatcher emits {price_change_24h, mcap}."""
    body = format_paper_trade_alert(
        signal_type="gainers_early", symbol="BTC", coin_id="bitcoin",
        entry_price=50000.0, amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    assert "GAINERS EARLY" in body
    assert "BTC" in body
    assert "+36.9%" in body  # 24h price change
    assert "$5.5M" in body  # mcap
    assert "coingecko.com/en/coins/bitcoin" in body  # one-tap research link


@pytest.mark.asyncio
async def test_format_narrative_prediction_renders_fit_and_category(tmp_path):
    """R2-C1 fold: narrative_prediction dispatcher emits {fit, category, mcap}.
    Earlier plan rendered narrative_score (never emitted) — would have
    shipped blank alerts for ~25% of fires."""
    body = format_paper_trade_alert(
        signal_type="narrative_prediction", symbol="DOGE", coin_id="dogecoin",
        entry_price=0.15, amount_usd=100.0,
        signal_data={"fit": 87, "category": "memecoin", "mcap": 20_000_000_000},
    )
    assert "NARRATIVE PREDICTION" in body
    assert "DOGE" in body
    assert "memecoin" in body
    assert "fit 87" in body
    assert "$20.0B" in body


@pytest.mark.asyncio
async def test_format_volume_spike_renders_spike_ratio(tmp_path):
    """R2-C1 fold: volume_spike dispatcher emits {spike_ratio} only."""
    body = format_paper_trade_alert(
        signal_type="volume_spike", symbol="PEPE", coin_id="pepe",
        entry_price=0.0001, amount_usd=100.0,
        signal_data={"spike_ratio": 8.3},
    )
    assert "VOLUME SPIKE" in body
    assert "vol×8.3" in body


@pytest.mark.asyncio
async def test_format_does_not_use_markdown_specials(tmp_path):
    """R1-C1 fold: the format is dispatched with parse_mode=None. Verify
    the format itself doesn't ACCIDENTALLY use Markdown specials that
    would render badly even in plain-text — sanity check."""
    body = format_paper_trade_alert(
        signal_type="gainers_early", symbol="BTC", coin_id="bitcoin",
        entry_price=50000.0, amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    # signal_type underscores were the silent-400 trigger; the format
    # transforms them to spaces ("GAINERS EARLY") — verify.
    assert "_" not in body.split("\n")[0]


@pytest.mark.asyncio
async def test_notify_writes_log_row_on_success(tmp_path, monkeypatch):
    """Successful TG dispatch writes a 'sent' row to tg_alert_log."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED)
    # Stub send_telegram_message to no-op (success)
    sent = []
    async def _fake_send(text, session, settings):
        sent.append(text)
    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    await notify_paper_trade_opened(
        db, settings, session=None, paper_trade_id=42,
        signal_type="gainers_early", token_id="bitcoin", symbol="BTC",
        entry_price=50000.0, amount_usd=100.0, signal_data={},
    )
    cur = await db._conn.execute(
        "SELECT outcome, signal_type, token_id FROM tg_alert_log "
        "WHERE paper_trade_id=42"
    )
    row = await cur.fetchone()
    assert row[0] == "sent"
    assert row[1] == "gainers_early"
    assert row[2] == "bitcoin"
    assert len(sent) == 1
    await db.close()


@pytest.mark.asyncio
async def test_notify_logs_eligibility_block(tmp_path):
    """When eligibility=0, log the block + don't dispatch."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED)
    await notify_paper_trade_opened(
        db, settings, session=None, paper_trade_id=42,
        signal_type="first_signal",  # suspended, eligibility=0
        token_id="bitcoin", symbol="BTC",
        entry_price=50000.0, amount_usd=100.0, signal_data={},
    )
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE paper_trade_id=42"
    )
    assert (await cur.fetchone())[0] == "blocked_eligibility"
    await db.close()


@pytest.mark.asyncio
async def test_notify_handles_dispatch_failure_gracefully(tmp_path, monkeypatch):
    """Network error during send_telegram_message → log dispatch_failed,
    don't crash the caller."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED)
    async def _fake_send_fail(*args, **kwargs):
        raise aiohttp.ClientError("simulated")
    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send_fail)
    # Should NOT raise
    await notify_paper_trade_opened(
        db, settings, session=None, paper_trade_id=42,
        signal_type="gainers_early", token_id="bitcoin", symbol="BTC",
        entry_price=50000.0, amount_usd=100.0, signal_data={},
    )
    cur = await db._conn.execute(
        "SELECT outcome FROM tg_alert_log WHERE paper_trade_id=42"
    )
    assert (await cur.fetchone())[0] == "dispatch_failed"
    await db.close()
```

- [ ] **Step 2: Implement `scout/trading/tg_alert_dispatch.py`**

```python
"""BL-NEW-TG-ALERT-ALLOWLIST: per-signal Telegram alert dispatch on
paper-trade open.

Architecture (see tasks/plan_tg_alert_allowlist.md):
- _check_eligibility: signal_params.tg_alert_eligible == 1
- _check_cooldown: per-token (across signals) — don't re-alert the same
  token_id within TG_ALERT_PER_TOKEN_COOLDOWN_HOURS (R2-I1 fold)
- format_paper_trade_alert: concise single-line body with per-signal
  field map (R2-C1 fold) + parse_mode=None caller (R1-C1 fold)
- notify_paper_trade_opened: orchestrator; never raises (logs failures)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database

log = structlog.get_logger(__name__)


async def _check_eligibility(db: Database, signal_type: str) -> bool:
    if db._conn is None:
        return False
    cur = await db._conn.execute(
        "SELECT tg_alert_eligible FROM signal_params WHERE signal_type = ?",
        (signal_type,),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _check_cooldown(
    db: Database, settings: Settings, token_id: str
) -> bool:
    """Returns True if cooldown is in effect (block the alert).

    R2-I1 fold: keyed on token_id ONLY (across all signal types) so a
    single token firing two different signals within the window only
    alerts once. Operator scenario: bitcoin firing gainers_early at 10am
    + losers_contrarian at 4pm → 1 alert (the first), not 2.

    Only counts 'sent' outcomes — transient failures don't suppress next
    legitimate fire.
    """
    if db._conn is None:
        return False
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS)
    ).isoformat()
    cur = await db._conn.execute(
        "SELECT 1 FROM tg_alert_log "
        "WHERE token_id = ? AND outcome = 'sent' "
        "AND alerted_at >= ? LIMIT 1",
        (token_id, cutoff),
    )
    return (await cur.fetchone()) is not None


def _fmt_mcap(mcap):
    if mcap is None:
        return "?"
    if mcap >= 1e9:
        return f"${mcap/1e9:.1f}B"
    if mcap >= 1e6:
        return f"${mcap/1e6:.1f}M"
    if mcap >= 1e3:
        return f"${mcap/1e3:.1f}K"
    return f"${mcap:.0f}"


def _fmt_price(p):
    if p is None or p == 0:
        return "$0"
    if p >= 1:
        return f"${p:.2f}"
    if p >= 0.01:
        return f"${p:.4f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"


_SIGNAL_EMOJI = {
    "gainers_early": "📈",
    "losers_contrarian": "📉",
    "volume_spike": "⚡",
    "narrative_prediction": "🪙",
    "chain_completed": "🔗",
}


def format_paper_trade_alert(
    *,
    signal_type: str,
    symbol: str,
    coin_id: str,
    entry_price: float,
    amount_usd: float,
    signal_data: dict | None,
) -> str:
    """Concise single-line + extras Telegram body for a paper-trade open.

    R1-C1 fold: caller MUST dispatch with parse_mode=None — signal_type
    contains underscores that Markdown parses as italic delimiters,
    producing a silent 400 BAD_REQUEST (caught project-wide once in PR
    #76 per memory project_overnight_2026_05_05.md).

    R2-C1 fold: per-signal field maps verified against actual emissions
    in scout/trading/signals.py:
      - volume_spike:        {spike_ratio}
      - gainers_early:       {price_change_24h, mcap}
      - losers_contrarian:   {price_change_24h, mcap}
      - narrative_prediction:{fit, category, mcap}
      - chain_completed:     {pattern, boost, ...} (excluded from
                              default-allow per R2-C2 — alerted via
                              existing scout/chains/alerts.py)

    R2-format fold: header line is single-line glanceable; per-signal
    detail follows; CoinGecko link last for one-tap research.
    """
    sd = signal_data or {}
    emoji = _SIGNAL_EMOJI.get(signal_type, "📊")
    # Header — single glanceable line (phone-screen-friendly)
    header = (
        f"{emoji} {signal_type.upper().replace('_', ' ')} · {symbol} · "
        f"{_fmt_price(entry_price)} · ${amount_usd:.0f}"
    )
    extras = []
    # Per-signal detail (only fields the dispatcher actually emits)
    if signal_type == "gainers_early" or signal_type == "losers_contrarian":
        if "price_change_24h" in sd:
            extras.append(f"24h: {sd['price_change_24h']:+.1f}%")
        if "mcap" in sd:
            extras.append(f"mcap {_fmt_mcap(sd['mcap'])}")
    elif signal_type == "volume_spike":
        if "spike_ratio" in sd:
            extras.append(f"vol×{sd['spike_ratio']:.1f}")
    elif signal_type == "narrative_prediction":
        if "category" in sd:
            extras.append(f"{sd['category']}")
        if "fit" in sd:
            extras.append(f"fit {sd['fit']}")
        if "mcap" in sd:
            extras.append(f"mcap {_fmt_mcap(sd['mcap'])}")
    detail = " · ".join(extras) if extras else None
    link = f"coingecko.com/en/coins/{coin_id}"
    parts = [header]
    if detail:
        parts.append(detail)
    parts.append(link)
    return "\n".join(parts)


async def _log_outcome(
    db: Database,
    *,
    paper_trade_id: int,
    signal_type: str,
    token_id: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    if db._conn is None:
        return
    await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            paper_trade_id,
            signal_type,
            token_id,
            datetime.now(timezone.utc).isoformat(),
            outcome,
            detail,
        ),
    )
    await db._conn.commit()


async def notify_paper_trade_opened(
    db: Database,
    settings: Settings,
    session,
    *,
    paper_trade_id: int,
    signal_type: str,
    token_id: str,
    symbol: str,
    entry_price: float,
    amount_usd: float,
    signal_data: dict | None,
) -> None:
    """Fire a Telegram alert for a paper-trade open (best-effort).

    Never raises. Always writes a tg_alert_log row recording the outcome
    (sent / blocked_eligibility / blocked_cooldown / dispatch_failed) for
    audit.

    R2-C2 design-stage fold: atomic check-then-write. The cooldown check
    AND the pre-write of the 'sent' row happen under a single
    `db._txn_lock` so concurrent tasks for the same token serialize
    cleanly. Without this, sequential `engine.open_trade` calls in
    `scout/main.py` can dispatch 2-N alerts for the same token within
    100ms because each task spawns + returns before the prior
    `tg_alert_log` INSERT lands.

    Flow:
      1. lock + eligibility check
      2. lock + cooldown check + INSERT pre-emptive 'sent' row
         (acts as the atomic claim)
      3. release lock
      4. call send_telegram_message
      5. on dispatch failure, UPDATE the row to 'dispatch_failed'
    """
    try:
        if not await _check_eligibility(db, signal_type):
            await _log_outcome(
                db, paper_trade_id=paper_trade_id, signal_type=signal_type,
                token_id=token_id, outcome="blocked_eligibility",
            )
            return

        # R2-C2 atomic claim: lock, re-check cooldown, pre-INSERT 'sent'
        # row that subsequent tasks see via _check_cooldown's SELECT.
        sent_row_id = None
        if db._conn is None:
            return
        async with db._txn_lock:
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(hours=settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS)
            ).isoformat()
            cur = await db._conn.execute(
                "SELECT 1 FROM tg_alert_log "
                "WHERE token_id = ? AND outcome = 'sent' "
                "AND alerted_at >= ? LIMIT 1",
                (token_id, cutoff),
            )
            if await cur.fetchone():
                # Cooldown active — log + return under lock (no race window)
                await db._conn.execute(
                    "INSERT INTO tg_alert_log "
                    "(paper_trade_id, signal_type, token_id, alerted_at, "
                    " outcome, detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        paper_trade_id, signal_type, token_id,
                        datetime.now(timezone.utc).isoformat(),
                        "blocked_cooldown",
                        f"hours={settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS}",
                    ),
                )
                await db._conn.commit()
                return
            # Win the race: insert pre-emptive 'sent' row
            cur = await db._conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                "VALUES (?, ?, ?, ?, 'sent')",
                (
                    paper_trade_id, signal_type, token_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            sent_row_id = cur.lastrowid
            await db._conn.commit()

        # Dispatch outside the lock
        body = format_paper_trade_alert(
            signal_type=signal_type, symbol=symbol, coin_id=token_id,
            entry_price=entry_price, amount_usd=amount_usd,
            signal_data=signal_data,
        )
        try:
            # R1-C1 fold: parse_mode=None to avoid Markdown 400 silent-fail
            # on signal_type underscores ("GAINERS_EARLY" etc.).
            await alerter.send_telegram_message(
                body, session, settings, parse_mode=None
            )
            # Row is already 'sent' from the atomic claim. No-op.
        except Exception as e:
            log.warning(
                "tg_alert_dispatch_failed",
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
                err=str(e),
            )
            # Demote pre-emptive 'sent' row to 'dispatch_failed'.
            # Note: cooldown query filters on outcome='sent' so this
            # demotion correctly clears the cooldown for the next fire.
            if sent_row_id is not None and db._conn is not None:
                async with db._txn_lock:
                    await db._conn.execute(
                        "UPDATE tg_alert_log SET outcome='dispatch_failed', "
                        "detail=? WHERE id=?",
                        (str(e)[:200], sent_row_id),
                    )
                    await db._conn.commit()
    except Exception:
        # Belt-and-braces: even logging failures must not propagate up
        # to block paper-trade dispatch.
        log.exception(
            "tg_alert_notify_unexpected_error",
            paper_trade_id=paper_trade_id,
            signal_type=signal_type,
        )
```

- [ ] **Step 3: Run + commit**

```bash
uv run --native-tls pytest tests/test_tg_alert_dispatch.py -q
git add scout/trading/tg_alert_dispatch.py tests/test_tg_alert_dispatch.py
git commit -m "feat(tg-alert-allowlist): tg_alert_dispatch + helpers (Task 1)"
```

---

## Task 2: Engine post-open hook

**Files:**
- Modify: `scout/trading/engine.py`
- Test: `tests/test_engine_post_open_hook.py` (NEW)

- [ ] **Step 1: Modify `engine.open_trade` to fire post-open hook**

After `trade_id = await self._paper_trader.execute_buy(...)` returns:

```python
        if self.mode == "paper":
            trade_id = await self._paper_trader.execute_buy(
                db=self.db,
                # ... existing args ...
            )
            # BL-NEW-TG-ALERT-ALLOWLIST: post-open Telegram alert hook.
            # Fire-and-forget — alert dispatch never blocks paper-trade
            # success path. notify_paper_trade_opened never raises.
            if trade_id is not None:
                await self._spawn_tg_alert(
                    trade_id=trade_id,
                    signal_type=signal_type,
                    token_id=token_id,
                    symbol=symbol,
                    amount_usd=trade_amount,
                    signal_data=signal_data,
                )
            return trade_id
```

**`__init__` additions** (R1-C3 + R1-I5 folds):

```python
def __init__(self, ..., session: aiohttp.ClientSession | None = None) -> None:
    # ... existing ...
    self._tg_session = session  # R1-I5: passed from main.py cycle session
    self._tg_alert_tasks: set[asyncio.Task] = set()  # R1-C3: GC-protect
```

**`_spawn_tg_alert` helper method** (R1-C2 + R1-C3 folds):

```python
async def _spawn_tg_alert(
    self,
    *,
    trade_id: int,
    signal_type: str,
    token_id: str,
    symbol: str,
    amount_usd: float,
    signal_data: dict,
) -> None:
    """Spawn the TG alert dispatch as a tracked background task.

    R1-C2 fold: re-read entry_price from paper_trades AFTER execute_buy
    so the alert's price matches the audit row (post-slip). The slipped
    entry price is the operator-facing reality.

    R1-C3 fold: hold task reference in self._tg_alert_tasks to prevent
    GC + dropped exceptions on shutdown. Mirrors scout/main.py:91
    `_social_restart_tasks` pattern.
    """
    from scout.trading.tg_alert_dispatch import notify_paper_trade_opened

    cur = await self.db._conn.execute(
        "SELECT entry_price FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = await cur.fetchone()
    effective_entry = float(row[0]) if row else 0.0

    task = asyncio.create_task(
        notify_paper_trade_opened(
            self.db, self.settings, self._tg_session,
            paper_trade_id=trade_id,
            signal_type=signal_type,
            token_id=token_id,
            symbol=symbol,
            entry_price=effective_entry,  # R1-C2: post-slip
            amount_usd=amount_usd,
            signal_data=signal_data,
        )
    )
    self._tg_alert_tasks.add(task)
    task.add_done_callback(self._tg_alert_tasks.discard)
```

- [ ] **Step 2: Tests in `tests/test_engine_post_open_hook.py`**

```python
@pytest.mark.asyncio
async def test_post_open_hook_fires_on_successful_trade(tmp_path, monkeypatch):
    """Engine.open_trade success → notify_paper_trade_opened fires."""
    calls = []
    async def _fake_notify(db, settings, session, **kw):
        calls.append(kw)
    monkeypatch.setattr(
        "scout.trading.tg_alert_dispatch.notify_paper_trade_opened",
        _fake_notify,
    )
    # ... construct engine with paper mode + seed price_cache + signal_params ...
    trade_id = await engine.open_trade(
        token_id="btc", symbol="BTC", name="Bitcoin", chain="coingecko",
        signal_type="gainers_early", signal_data={"price_change_24h": 30},
    )
    assert trade_id is not None
    # Allow background task to run
    await asyncio.sleep(0.05)
    assert len(calls) == 1
    assert calls[0]["signal_type"] == "gainers_early"
    assert calls[0]["paper_trade_id"] == trade_id


@pytest.mark.asyncio
async def test_post_open_hook_does_not_fire_when_open_fails(tmp_path, monkeypatch):
    """trade_id is None (e.g., max_open_trades hit) → no hook fire."""
    calls = []
    async def _fake_notify(db, settings, session, **kw):
        calls.append(kw)
    monkeypatch.setattr(
        "scout.trading.tg_alert_dispatch.notify_paper_trade_opened",
        _fake_notify,
    )
    # Force max_open=0 so any open returns None
    # ... construct engine with PAPER_MAX_OPEN_TRADES=0 (or seed an existing open) ...
    trade_id = await engine.open_trade(...)
    assert trade_id is None
    await asyncio.sleep(0.05)
    assert len(calls) == 0


@pytest.mark.asyncio
async def test_post_open_hook_failure_does_not_block_open(tmp_path, monkeypatch):
    """notify_paper_trade_opened raising must NOT propagate; trade_id
    must still return successfully."""
    async def _fake_notify_raise(*a, **kw):
        raise RuntimeError("simulated")
    monkeypatch.setattr(
        "scout.trading.tg_alert_dispatch.notify_paper_trade_opened",
        _fake_notify_raise,
    )
    trade_id = await engine.open_trade(...)
    assert trade_id is not None  # caller is unaffected
```

- [ ] **Step 3: Commit**

```bash
git add scout/trading/engine.py tests/test_engine_post_open_hook.py
git commit -m "feat(tg-alert-allowlist): engine post-open hook (Task 2)"
```

---

## Task 2.5: auto_suspend joint flag maintenance (R2-I1 fold)

**Files:**
- Modify: `scout/trading/auto_suspend.py`
- Test: `tests/test_auto_suspend.py` (existing)

- [ ] **Step 1: Modify `_atomic_suspend` to clear `tg_alert_eligible`**

In the existing `UPDATE signal_params SET enabled=0, suspended_at=?, suspended_reason=? WHERE signal_type=?` add `tg_alert_eligible=0`:

```python
await conn.execute(
    """UPDATE signal_params
       SET enabled=0, tg_alert_eligible=0,
           suspended_at=?, suspended_reason=?
       WHERE signal_type=?""",
    (now_iso, reason, signal_type),
)
```

- [ ] **Step 2: Modify `revive_signal_with_baseline` to restore eligibility**

When reviving, restore `tg_alert_eligible=1` IF the signal was in `DEFAULT_ALLOW_SIGNALS`:

```python
from scout.trading.tg_alert_dispatch import DEFAULT_ALLOW_SIGNALS

restore_eligible = 1 if signal_type in DEFAULT_ALLOW_SIGNALS else 0
await conn.execute(
    """UPDATE signal_params
       SET enabled=1, tg_alert_eligible=?,
           suspended_at=NULL, suspended_reason=NULL
       WHERE signal_type=?""",
    (restore_eligible, signal_type),
)
```

- [ ] **Step 3: Add tests** in `tests/test_auto_suspend.py`:

```python
async def test_atomic_suspend_clears_tg_alert_eligible(tmp_path):
    """R2-I1 fold: auto-suspend clears BOTH enabled and tg_alert_eligible."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # gainers_early starts eligible=1 from migration
    await _atomic_suspend(db._conn, "gainers_early", "hard_loss", now_iso)
    cur = await db._conn.execute(
        "SELECT enabled, tg_alert_eligible FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    row = await cur.fetchone()
    assert row[0] == 0  # enabled cleared
    assert row[1] == 0  # tg_alert_eligible also cleared


async def test_revive_restores_default_allow_eligibility(tmp_path):
    """R2-I1 fold: revive restores tg_alert_eligible=1 for default-allow signals."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _atomic_suspend(db._conn, "gainers_early", "hard_loss", now_iso)
    await revive_signal_with_baseline(db, "gainers_early", ...)
    cur = await db._conn.execute(
        "SELECT enabled, tg_alert_eligible FROM signal_params "
        "WHERE signal_type='gainers_early'"
    )
    row = await cur.fetchone()
    assert row[0] == 1
    assert row[1] == 1


async def test_revive_does_not_restore_non_default_allow(tmp_path):
    """trending_catch is not in DEFAULT_ALLOW_SIGNALS → revive sets eligible=0."""
    # ... similar, asserts tg_alert_eligible=0 after revive
```

- [ ] **Step 4: Commit**

```bash
git add scout/trading/auto_suspend.py tests/test_auto_suspend.py
git commit -m "feat(tg-alert-allowlist): auto_suspend joint flag maintenance (Task 2.5, R2-I1)"
```

---

## Task 2.6: scout/main.py wiring — session pass + first-deploy announcement

**Files:**
- Modify: `scout/main.py`
- Test: `tests/test_main_wiring.py` (existing)

- [ ] **Step 1: Pass session into TradingEngine**

In `scout/main.py:1230`, the engine is constructed inside the cycle loop where `aiohttp.ClientSession` is already available. Pass it:

```python
trading_engine = TradingEngine(
    mode=settings.TRADING_MODE,
    db=db,
    settings=settings,
    live_engine=live_engine,
    session=session,  # R1-I3+I5 fold: TG alert dispatch uses this
)
```

- [ ] **Step 2: Add first-deploy announcement**

After `Database.initialize()` (which runs the migration creating tg_alert_log) but before the cycle loop, INSIDE the `aiohttp.ClientSession` block:

```python
# R2-C1 + R1-I4 design-stage fold: first-deploy operator announcement.
# Gated on tg_alert_log 'announcement_sent' sentinel — fires exactly
# once per database lifetime regardless of restart count.
async def _maybe_announce_tg_alerts(db, session, settings):
    cur = await db._conn.execute(
        "SELECT 1 FROM tg_alert_log WHERE outcome='announcement_sent' LIMIT 1"
    )
    if await cur.fetchone():
        return  # already announced
    body = (
        "📢 TG alert allowlist active\n"
        "Allowed signals (paper-trade open): gainers_early, "
        "narrative_prediction, losers_contrarian, volume_spike\n"
        "Open-only — check dashboard for closes\n"
        "chain_completed via existing chain alerter\n"
        "Per-token cooldown: "
        f"{settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS}h "
        "(reduce via .env TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=2 for "
        "second-leg signals)\n"
        "To silence per-signal: UPDATE signal_params SET "
        "tg_alert_eligible=0 WHERE signal_type='...';"
    )
    try:
        await alerter.send_telegram_message(
            body, session, settings, parse_mode=None
        )
        # Sentinel insert — must succeed for idempotency
        async with db._txn_lock:
            await db._conn.execute(
                "INSERT INTO tg_alert_log "
                "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
                "VALUES (NULL, 'announcement', '_system', ?, "
                "'announcement_sent')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await db._conn.commit()
        logger.info("tg_alert_announcement_sent")
    except Exception:
        # Don't block startup on announcement failure; will retry next start
        logger.exception("tg_alert_announcement_failed")

# Call once after engine setup, before cycle loop
await _maybe_announce_tg_alerts(db, session, settings)
```

- [ ] **Step 3: Commit**

```bash
git add scout/main.py tests/test_main_wiring.py
git commit -m "feat(tg-alert-allowlist): main.py session wiring + first-deploy announcement (Task 2.6, R2-C1+R1-I4)"
```

---

## Task 3: Full regression + black

```bash
uv run --native-tls pytest tests/ -q --tb=short
uv run --native-tls black scout/ tests/
git diff --stat scout/ tests/
git commit -am "chore(tg-alert-allowlist): black reformat" 2>&1 | tail -3
```

---

## Task 4: PR + 3-vector reviewers + merge + deploy

Per CLAUDE.md §8 (operator-visible alert change with potential spam blast radius):
- V1 — structural/code: API correctness, signal_params column composition, engine hook composition, async task spawn correctness
- V2 — UX/blast-radius: alert spam risk, cooldown semantics, default eligibility correctness vs operator expectation
- V3 — silent-failure: dispatch failure path, log-only-not-block invariant, race between paper-trade write and alert fire

---

## Done criteria

- 4 default-allow signals (gainers_early, narrative_prediction, losers_contrarian, volume_spike) fire TG alerts on paper-trade open
- chain_completed kept at `tg_alert_eligible=0` (R2-C2 fold) — existing `scout/chains/alerts.py` chain-pattern alert is its TG path; new dispatch suppresses to avoid duplicate alerts. Operator can opt-in via UPDATE if they want both.
- Suspended signals (first_signal, trending_catch, tg_social) explicitly blocked by tg_alert_eligible=0
- Per-token 6h cooldown across signals prevents re-alert spam (R2-I1 fold)
- tg_alert_log records every outcome for audit + dashboard reporting
- TG dispatch failure NEVER blocks paper-trade dispatch path
- Schema migration `bl_tg_alert_eligible_v1` (20260516) applied on prod with schema_version stamp + post-assertion (R1-I1 fold)
- Existing TG dispatch surfaces (`scout/main.py:855`, `scout/chains/alerts.py`, research streams) unchanged
- First-deploy operator announcement message (R2-I3 + R2-I4 folds): on `scout/main.py` startup when `LIVE_MODE='paper'` AND no prior `tg_alert_log` rows exist, send a one-time Telegram message: `📢 TG alert allowlist active: gainers_early, narrative_prediction, losers_contrarian, volume_spike (open-only; check dashboard for closes). chain_completed via existing chain alerter. Per-token 6h cooldown.`

## What this milestone does NOT do

- Does NOT add per-channel routing (single TELEGRAM_CHAT_ID destination; multi-topic routing is M2)
- Does NOT batch alerts into digests (each open fires individually within cooldown)
- Does NOT add a paper-trade-CLOSE alert (only opens; close-alerts are a separate scope)
- Does NOT modify the conviction-gate `send_alert` or research-only streams (velocity, secondwave, gainers tracker)
- Does NOT add a dashboard view for tg_alert_log (operator can SQL it; dashboard is M2)

## Reversibility

Single PR. Fast revert: `UPDATE signal_params SET tg_alert_eligible=0 WHERE signal_type IN (...)` silences all alerts without code change. Slower revert: `git revert <squash>` reverts migration (column add is idempotent + DROP TABLE tg_alert_log via reverse migration if needed).
