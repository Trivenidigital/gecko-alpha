**New primitives introduced:** New `signal_params.tg_alert_eligible` BOOLEAN column (default 0); migration `bl_tg_alert_eligible_v1`. New module `scout/trading/tg_alert_dispatch.py` exposing `notify_paper_trade_opened(db, settings, session, paper_trade_id, signal_type, ...)` with per-signal allowlist + provisional n≥30 gate + per-token cooldown. New helper `format_paper_trade_alert(...)` producing concise Telegram body. New post-open hook in `scout/trading/engine.py` after `execute_buy` returns trade_id. New Settings `TG_ALERT_PROVISIONAL_MIN_TRADES: int = 30`, `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 24`. Default eligibility set in migration: gainers_early=1, narrative_prediction=1, losers_contrarian=1, volume_spike=1, chain_completed=1 (BUT chain_completed gates further on n≥30 closed trades count). Existing `scout/chains/alerts.py` chain_completion alert is RETAINED (chain-pattern detection alert; separate from paper-trade-open alert path). Operator can audit-trail-flip eligibility via `UPDATE signal_params SET tg_alert_eligible=N WHERE signal_type=...`.

# TG Alert Allowlist (Option B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Send real-time Telegram alerts when proven paper-trade signals open, scoped via a per-signal allowlist with a provisional n≥30 gate for new signals. Default eligibility = the 4 statistically-validated signals (gainers_early, narrative_prediction, losers_contrarian, volume_spike) + chain_completed under provisional gate.

**Architecture:** Introduce `signal_params.tg_alert_eligible` flag. After `engine.open_trade` successfully creates a paper_trade row, fire a post-open hook that:
1. Reads `tg_alert_eligible` for the signal_type — if 0, no-op
2. If signal is "provisional" (configured per-signal — default chain_completed has eligibility=1 BUT historical n<min_threshold), gate further: only fire if `paper_trades` closed-trade count for this signal_type ≥ `TG_ALERT_PROVISIONAL_MIN_TRADES` (30)
3. Per-token cooldown — don't re-alert the same `(signal_type, token_id)` within `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS` (24h default)
4. Format message + dispatch via `alerter.send_telegram_message`
5. Record fire in `tg_alert_log` table for audit + cooldown lookup

Failure modes (network, missing creds) are caught + logged; never block paper-trade dispatch.

**Tech Stack:** Python 3.12, aiosqlite, aiohttp, pydantic v2 BaseSettings, structlog, pytest-asyncio (auto mode), aioresponses (HTTP mock).

**Total scope:** ~50-60 steps across 6 tasks. One schema migration. Zero breaking changes to existing TG dispatch surfaces (`scout/chains/alerts.py`, `scout/main.py:855`, research streams) — additive only.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/db.py` | Modify | Add migration `bl_tg_alert_eligible_v1` (schema 20260516): `ALTER TABLE signal_params ADD COLUMN tg_alert_eligible INTEGER NOT NULL DEFAULT 0`; UPDATE existing rows for default-allow signals; CREATE TABLE `tg_alert_log` (audit + cooldown source) |
| `scout/config.py` | Modify | Add `TG_ALERT_PROVISIONAL_MIN_TRADES: int = 30` + `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 24` Settings |
| `scout/trading/tg_alert_dispatch.py` | **Create** | `notify_paper_trade_opened(...)` orchestrator + `format_paper_trade_alert(...)` formatter + `_check_eligibility`, `_check_provisional_gate`, `_check_cooldown` helpers. Pure async, no I/O beyond aiosqlite + alerter. |
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
    # signal_params.tg_alert_eligible. Provisional signals (low historical
    # sample size) gate further on closed-trade count.
    TG_ALERT_PROVISIONAL_MIN_TRADES: int = 30
    # Per-(signal_type, token_id) cooldown to prevent spam when the same
    # signal re-fires repeatedly on a single token (multi-snapshot bursts).
    TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 24
```

- [ ] **Step 3: Add migration `bl_tg_alert_eligible_v1` to `scout/db.py`**

Add after the most recent `_migrate_*` function, register in `_apply_migrations` with schema version 20260516:

```python
async def _migrate_tg_alert_eligible_v1(self, conn) -> None:
    """BL-NEW-TG-ALERT-ALLOWLIST: per-signal TG alert eligibility.

    Adds signal_params.tg_alert_eligible (default 0). Sets eligibility=1
    for the 4 statistically-validated signals + chain_completed (which
    is gated additionally by TG_ALERT_PROVISIONAL_MIN_TRADES).

    Creates tg_alert_log for audit + cooldown lookup.
    """
    # Idempotent: skip column add if already present (pragma_table_info)
    cur = await conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1] for row in await cur.fetchall()}
    if "tg_alert_eligible" not in cols:
        await conn.execute(
            "ALTER TABLE signal_params ADD COLUMN "
            "tg_alert_eligible INTEGER NOT NULL DEFAULT 0"
        )
    # Default-allow signals (data-driven assessment 2026-05-10)
    for sig in ("gainers_early", "narrative_prediction", "losers_contrarian",
                "volume_spike", "chain_completed"):
        await conn.execute(
            "UPDATE signal_params SET tg_alert_eligible=1 "
            "WHERE signal_type = ?",
            (sig,),
        )
    # Audit + cooldown source
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS tg_alert_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_trade_id INTEGER REFERENCES paper_trades(id),
            signal_type  TEXT NOT NULL,
            token_id     TEXT NOT NULL,
            alerted_at   TEXT NOT NULL,
            outcome      TEXT NOT NULL CHECK (outcome IN (
                'sent','blocked_eligibility','blocked_provisional',
                'blocked_cooldown','dispatch_failed'
            )),
            detail       TEXT
        )"""
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tg_alert_log_token "
        "ON tg_alert_log(signal_type, token_id, alerted_at)"
    )
    await conn.commit()
```

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
    _check_provisional_gate,
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
async def test_provisional_gate_allows_at_or_above_threshold(tmp_path):
    """chain_completed has eligibility=1 but is gated by closed-trade count."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PROVISIONAL_MIN_TRADES=30)
    # Insert 30 closed chain_completed trades
    await _seed_closed_trades(db, "chain_completed", count=30)
    assert await _check_provisional_gate(db, settings, "chain_completed") is True
    await db.close()


@pytest.mark.asyncio
async def test_provisional_gate_blocks_below_threshold(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PROVISIONAL_MIN_TRADES=30)
    await _seed_closed_trades(db, "chain_completed", count=8)  # current prod
    assert await _check_provisional_gate(db, settings, "chain_completed") is False
    await db.close()


@pytest.mark.asyncio
async def test_provisional_gate_skipped_for_non_provisional(tmp_path):
    """gainers_early is non-provisional → skip the count gate."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PROVISIONAL_MIN_TRADES=30)
    await _seed_closed_trades(db, "gainers_early", count=0)
    # Provisional gate is ONLY applied to chain_completed in M1; others skip.
    assert await _check_provisional_gate(db, settings, "gainers_early") is True
    await db.close()


@pytest.mark.asyncio
async def test_cooldown_blocks_within_window(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=24)
    # Insert recent alert log
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (now.isoformat(),),
    )
    await db._conn.commit()
    in_cd = await _check_cooldown(db, settings, "gainers_early", "btc")
    assert in_cd is True


@pytest.mark.asyncio
async def test_cooldown_allows_after_window(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=24)
    # Insert OLD alert log (25h ago)
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'sent')",
        (old,),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "gainers_early", "btc") is False


@pytest.mark.asyncio
async def test_cooldown_only_counts_sent_outcome(tmp_path):
    """Failed dispatches and blocked alerts don't count toward cooldown —
    so a transient failure doesn't suppress the next legitimate fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = Settings(_env_file=None, **_REQUIRED, TG_ALERT_PER_TOKEN_COOLDOWN_HOURS=24)
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_alert_log (paper_trade_id, signal_type, token_id, "
        "alerted_at, outcome) VALUES (1, 'gainers_early', 'btc', ?, 'dispatch_failed')",
        (now,),
    )
    await db._conn.commit()
    assert await _check_cooldown(db, settings, "gainers_early", "btc") is False


@pytest.mark.asyncio
async def test_format_paper_trade_alert_renders_signal_data(tmp_path):
    """Format is concise + includes signal-specific data."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    assert "GAINERS_EARLY" in body
    assert "BTC" in body
    assert "36.92" in body  # price change
    assert "$5.5M" in body or "5,500,000" in body or "5500000" in body  # mcap


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
- _check_provisional_gate: for chain_completed, require closed-trade
  count >= TG_ALERT_PROVISIONAL_MIN_TRADES
- _check_cooldown: don't re-alert (signal_type, token_id) within
  TG_ALERT_PER_TOKEN_COOLDOWN_HOURS
- format_paper_trade_alert: concise body
- notify_paper_trade_opened: orchestrator; never raises (logs failures)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database

log = structlog.get_logger(__name__)

# Provisional signals — gated on closed-trade count threshold.
# Operator can graduate by setting tg_alert_eligible while count < threshold,
# but the gate enforces n>=threshold regardless.
_PROVISIONAL_SIGNALS = {"chain_completed"}


async def _check_eligibility(db: Database, signal_type: str) -> bool:
    if db._conn is None:
        return False
    cur = await db._conn.execute(
        "SELECT tg_alert_eligible FROM signal_params WHERE signal_type = ?",
        (signal_type,),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _check_provisional_gate(
    db: Database, settings: Settings, signal_type: str
) -> bool:
    """For provisional signals, require >= TG_ALERT_PROVISIONAL_MIN_TRADES
    closed paper trades. Non-provisional signals always pass."""
    if signal_type not in _PROVISIONAL_SIGNALS:
        return True
    if db._conn is None:
        return False
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades "
        "WHERE signal_type = ? AND status != 'open'",
        (signal_type,),
    )
    row = await cur.fetchone()
    n = int(row[0]) if row else 0
    return n >= settings.TG_ALERT_PROVISIONAL_MIN_TRADES


async def _check_cooldown(
    db: Database, settings: Settings, signal_type: str, token_id: str
) -> bool:
    """Returns True if cooldown is in effect (block the alert).

    Only counts 'sent' outcomes — transient failures don't suppress next fire.
    """
    if db._conn is None:
        return False
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS)
    ).isoformat()
    cur = await db._conn.execute(
        "SELECT 1 FROM tg_alert_log "
        "WHERE signal_type = ? AND token_id = ? AND outcome = 'sent' "
        "AND alerted_at >= ? LIMIT 1",
        (signal_type, token_id, cutoff),
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


def format_paper_trade_alert(
    *,
    signal_type: str,
    symbol: str,
    coin_id: str,
    entry_price: float,
    amount_usd: float,
    signal_data: dict | None,
) -> str:
    """Concise Telegram body for a paper-trade open. Single message, no
    parse_mode (avoids Markdown 400-error class per BL-080-style fix).
    """
    sd = signal_data or {}
    lines = [
        f"📈 {signal_type.upper()}",
        f"{symbol} ({coin_id})",
        f"Entry {_fmt_price(entry_price)} | Size ${amount_usd:.0f}",
    ]
    # Per-signal extras
    if "price_change_24h" in sd:
        lines.append(f"24h: {sd['price_change_24h']:+.1f}%")
    if "mcap" in sd:
        lines.append(f"MCap {_fmt_mcap(sd['mcap'])}")
    if "narrative_score" in sd:
        lines.append(f"Narrative score: {sd['narrative_score']}")
    return "\n".join(lines)


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
    (sent / blocked_eligibility / blocked_provisional / blocked_cooldown /
    dispatch_failed) for audit.
    """
    try:
        if not await _check_eligibility(db, signal_type):
            await _log_outcome(
                db, paper_trade_id=paper_trade_id, signal_type=signal_type,
                token_id=token_id, outcome="blocked_eligibility",
            )
            return
        if not await _check_provisional_gate(db, settings, signal_type):
            await _log_outcome(
                db, paper_trade_id=paper_trade_id, signal_type=signal_type,
                token_id=token_id, outcome="blocked_provisional",
                detail=f"min={settings.TG_ALERT_PROVISIONAL_MIN_TRADES}",
            )
            return
        if await _check_cooldown(db, settings, signal_type, token_id):
            await _log_outcome(
                db, paper_trade_id=paper_trade_id, signal_type=signal_type,
                token_id=token_id, outcome="blocked_cooldown",
                detail=f"hours={settings.TG_ALERT_PER_TOKEN_COOLDOWN_HOURS}",
            )
            return
        body = format_paper_trade_alert(
            signal_type=signal_type, symbol=symbol, coin_id=token_id,
            entry_price=entry_price, amount_usd=amount_usd,
            signal_data=signal_data,
        )
        try:
            await alerter.send_telegram_message(body, session, settings)
            await _log_outcome(
                db, paper_trade_id=paper_trade_id, signal_type=signal_type,
                token_id=token_id, outcome="sent",
            )
        except Exception as e:
            log.warning(
                "tg_alert_dispatch_failed",
                paper_trade_id=paper_trade_id,
                signal_type=signal_type,
                token_id=token_id,
                err=str(e),
            )
            await _log_outcome(
                db, paper_trade_id=paper_trade_id, signal_type=signal_type,
                token_id=token_id, outcome="dispatch_failed",
                detail=str(e)[:200],
            )
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
                from scout.trading.tg_alert_dispatch import notify_paper_trade_opened
                # session reused if available on engine; else None (alerter
                # creates a fresh ClientSession internally)
                tg_session = getattr(self, "_tg_session", None)
                # Spawn as background task — paper-trade success returns
                # immediately even if Telegram is slow.
                asyncio.create_task(
                    notify_paper_trade_opened(
                        self.db, self.settings, tg_session,
                        paper_trade_id=trade_id,
                        signal_type=signal_type,
                        token_id=token_id,
                        symbol=symbol,
                        entry_price=current_price,
                        amount_usd=trade_amount,
                        signal_data=signal_data,
                    )
                )
            return trade_id
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
- chain_completed eligibility=1 BUT gated by closed-trade count >= 30 (currently 8 → blocked until 22 more close)
- Suspended signals (first_signal, trending_catch, tg_social) explicitly blocked by tg_alert_eligible=0
- Per-(signal, token) 24h cooldown prevents re-alert spam
- tg_alert_log records every outcome for audit + dashboard reporting
- TG dispatch failure NEVER blocks paper-trade dispatch path
- Schema migration `bl_tg_alert_eligible_v1` (20260516) applied on prod
- Existing TG dispatch surfaces (`scout/main.py:855`, `scout/chains/alerts.py`, research streams) unchanged

## What this milestone does NOT do

- Does NOT add per-channel routing (single TELEGRAM_CHAT_ID destination; multi-topic routing is M2)
- Does NOT batch alerts into digests (each open fires individually within cooldown)
- Does NOT add a paper-trade-CLOSE alert (only opens; close-alerts are a separate scope)
- Does NOT modify the conviction-gate `send_alert` or research-only streams (velocity, secondwave, gainers tracker)
- Does NOT add a dashboard view for tg_alert_log (operator can SQL it; dashboard is M2)

## Reversibility

Single PR. Fast revert: `UPDATE signal_params SET tg_alert_eligible=0 WHERE signal_type IN (...)` silences all alerts without code change. Slower revert: `git revert <squash>` reverts migration (column add is idempotent + DROP TABLE tg_alert_log via reverse migration if needed).
