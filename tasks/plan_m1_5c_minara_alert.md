**New primitives introduced:** New module `scout/trading/minara_alert.py` exposing `maybe_minara_command(session, settings, coin_id, amount_usd) -> str | None` — fetches CoinGecko `/coins/{id}` via existing `scout.counter.detail.fetch_coin_detail` (30-min in-memory cache), reads `platforms.solana`; returns a formatted `minara swap` shell-command string when the token is Solana-listed with a non-empty SPL address, else `None`. New Settings: `MINARA_ALERT_ENABLED: bool = True`, `MINARA_ALERT_FROM_TOKEN: str = "USDC"`, `MINARA_ALERT_AMOUNT_USD_OVERRIDE: float | None = None`. Extends `notify_paper_trade_opened` to await the helper after the cooldown check passes; passes the resolved command string into `format_paper_trade_alert` via a new `minara_command: str | None = None` kwarg; format appends a 4th line `Run: <command>` when supplied. Zero new schema, zero new dispatch tables, zero execution code. Read-only detection + format-extension. Default-on flag with single-knob disable.

# M1.5c — Minara DEX-Eligibility Alert Extension (Phase 0 Option A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** When a paper-trade-open TG alert is about to fire for a Solana-listed token, append a one-line, copy-paste-able `minara swap` command to the alert body. Operator copy-pastes into their local terminal where Minara is logged in to execute. gecko-alpha never executes — pure decision-support.

**Architecture:** Phase 0 Option A per the brainstorm. Cleanest minimal scope:
1. After TG allowlist + cooldown gates pass, but BEFORE format runs, call `maybe_minara_command(session, settings, coin_id, amount_usd)`
2. Helper uses existing `fetch_coin_detail` (CoinGecko `/coins/{id}`, 30-min cache) to read `platforms.solana`
3. If present + non-empty: return formatted command. Else return None.
4. `format_paper_trade_alert` receives the command via new optional kwarg; appends `Run: <cmd>` as last line BEFORE the coingecko.com link
5. Failure modes (CG 404 / 429 / network error): return None; alert still fires with normal content

**Tech Stack:** Python 3.12, aiohttp, aiosqlite, pydantic v2 BaseSettings, structlog, pytest-asyncio (auto mode).

**Total scope:** ~20-25 steps across 4 tasks. Zero schema migrations. Zero new dispatch paths. Composes cleanly with M1.5b TG alert allowlist (`scout/trading/tg_alert_dispatch.py`).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/config.py` | Modify | Add 3 Settings: `MINARA_ALERT_ENABLED`, `MINARA_ALERT_FROM_TOKEN`, `MINARA_ALERT_AMOUNT_USD_OVERRIDE` |
| `scout/trading/minara_alert.py` | **Create** | `maybe_minara_command(session, settings, coin_id, amount_usd) -> str \| None`. Pure async, calls `fetch_coin_detail`, parses `platforms.solana`. Never raises. |
| `scout/trading/tg_alert_dispatch.py` | Modify | `notify_paper_trade_opened` awaits `maybe_minara_command` after cooldown gate passes; passes result into `format_paper_trade_alert(minara_command=...)`. `format_paper_trade_alert` signature gains `minara_command: str \| None = None`; appends `Run: <cmd>` before coingecko link. |
| `tests/test_minara_alert.py` | **Create** | Detection unit tests + format integration tests + failure-mode tests |

**Schema versions reserved:** none. M1.5c is migration-free.

---

## Task 0: Setup — branch + Settings

- [ ] **Step 1: Verify branch**

```bash
git branch --show-current
# Expected: feat/m1-5c-minara-alert
```

- [ ] **Step 2: Add Settings fields to `scout/config.py`**

Near the M1.5b `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS` block:

```python
    # BL-NEW-M1.5C: Minara DEX-eligibility alert extension (Phase 0 Option A).
    # When a TG paper-trade-open alert is about to fire for a Solana-listed
    # token, append a `minara swap` shell command to the alert body for
    # operator copy-paste. gecko-alpha does NOT execute — pure decision-
    # support. Solana-only in M1.5c; EVM chains are M1.5d/M2.
    MINARA_ALERT_ENABLED: bool = True
    # Quote token for the swap command. Default USDC matches Minara wallet
    # operational norm; operator can override via .env.
    MINARA_ALERT_FROM_TOKEN: str = "USDC"
    # Override the default trade size. None → use settings.LIVE_TRADE_AMOUNT_USD
    # (same as live-trading defaults). Set explicitly (e.g., 10.0) to decouple
    # alert-suggested size from live-trading size.
    MINARA_ALERT_AMOUNT_USD_OVERRIDE: float | None = None
```

- [ ] **Step 3: Commit**

```bash
git add scout/config.py
git commit -m "feat(m1.5c): MINARA_ALERT_* Settings — Phase 0 Option A scaffold"
```

---

## Task 1: `minara_alert.py` — detection + command formatter

**Files:**
- Create: `scout/trading/minara_alert.py`
- Test: `tests/test_minara_alert.py` (NEW)

- [ ] **Step 1: Failing tests**

```python
"""BL-NEW-M1.5C: Minara DEX-eligibility alert extension tests."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from scout.config import Settings
from scout.trading.minara_alert import maybe_minara_command


_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


@pytest.mark.asyncio
async def test_returns_command_for_solana_token(monkeypatch):
    """Token with platforms.solana set → formatted command returned."""
    async def _fake_detail(session, coin_id, api_key=""):
        return {
            "platforms": {
                "solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
            }
        }
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )
    settings = _settings(MINARA_ALERT_FROM_TOKEN="USDC")
    cmd = await maybe_minara_command(
        session=None, settings=settings,
        coin_id="bonk", amount_usd=10.0,
    )
    assert cmd is not None
    assert "minara swap" in cmd
    assert "USDC" in cmd
    assert "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" in cmd
    assert "10" in cmd  # amount


@pytest.mark.asyncio
async def test_returns_none_when_no_solana_platform(monkeypatch):
    """Token without platforms.solana → None."""
    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"ethereum": "0xabc"}}
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )
    cmd = await maybe_minara_command(
        session=None, settings=_settings(),
        coin_id="random", amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_returns_none_when_solana_platform_empty(monkeypatch):
    """Empty/None SPL address → None (e.g. mainnet SOL itself has empty platform value)."""
    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": ""}}
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )
    cmd = await maybe_minara_command(
        session=None, settings=_settings(),
        coin_id="solana", amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_returns_none_when_fetch_detail_fails(monkeypatch):
    """CG 404 / 429 / network error → None (never raises)."""
    async def _fake_detail(session, coin_id, api_key=""):
        return None  # fetch_coin_detail soft-fails to None
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )
    cmd = await maybe_minara_command(
        session=None, settings=_settings(),
        coin_id="missing", amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_returns_none_when_disabled(monkeypatch):
    """MINARA_ALERT_ENABLED=False → no fetch, immediate None."""
    fetch_count = [0]
    async def _fake_detail(*args, **kwargs):
        fetch_count[0] += 1
        return {"platforms": {"solana": "SOLADDR"}}
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )
    cmd = await maybe_minara_command(
        session=None,
        settings=_settings(MINARA_ALERT_ENABLED=False),
        coin_id="bonk", amount_usd=10.0,
    )
    assert cmd is None
    assert fetch_count[0] == 0, "should short-circuit before fetch"


@pytest.mark.asyncio
async def test_handles_unexpected_exception(monkeypatch):
    """Even if fetch_coin_detail raises unexpectedly, return None."""
    async def _fake_detail_raise(*args, **kwargs):
        raise RuntimeError("simulated CG outage")
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail_raise
    )
    cmd = await maybe_minara_command(
        session=None, settings=_settings(),
        coin_id="bonk", amount_usd=10.0,
    )
    assert cmd is None  # never raises


@pytest.mark.asyncio
async def test_uses_amount_override_when_set(monkeypatch):
    """MINARA_ALERT_AMOUNT_USD_OVERRIDE decouples alert from live size."""
    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "SOLADDR"}}
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )
    cmd = await maybe_minara_command(
        session=None,
        settings=_settings(MINARA_ALERT_AMOUNT_USD_OVERRIDE=5.0),
        coin_id="bonk", amount_usd=100.0,
    )
    assert cmd is not None
    assert "5" in cmd  # override wins
    assert "100" not in cmd  # caller amount ignored
```

- [ ] **Step 2: Implement `scout/trading/minara_alert.py`**

```python
"""BL-NEW-M1.5C: Minara DEX-eligibility alert extension (Phase 0 Option A).

When a TG paper-trade-open alert is about to fire for a Solana-listed
token, this module returns a formatted `minara swap` shell command that
the operator copy-pastes into their local terminal where Minara is
logged in. gecko-alpha does NOT execute the command — pure decision-
support.

Architecture:
- `maybe_minara_command(session, settings, coin_id, amount_usd) -> str | None`
- Reads CoinGecko `/coins/{id}` via existing `scout.counter.detail.fetch_coin_detail`
  (30-min in-memory cache; soft-fails to None on 404/429/error)
- Detects Solana eligibility via `platforms.solana` field (non-empty SPL address)
- Returns formatted command string OR None (never raises)

Failure modes:
- MINARA_ALERT_ENABLED=False → immediate None (short-circuit, no fetch)
- fetch_coin_detail returns None (CG outage, 404, rate-limit) → None
- platforms.solana missing or empty → None
- Any other exception → caught, logged, return None

Future extensions (M1.5d / M2):
- EVM chains (Base, Arbitrum, etc.) via additional platforms.* checks
- Per-chain quote token selection (USDT on BSC, USDC on Solana/Base, etc.)
- Slippage hint via `--max-slippage` flag (currently not exposed by Minara CLI)
"""

from __future__ import annotations

import structlog

from scout.config import Settings
from scout.counter.detail import fetch_coin_detail

log = structlog.get_logger(__name__)


async def maybe_minara_command(
    session,
    settings: Settings,
    coin_id: str,
    amount_usd: float,
) -> str | None:
    """Return a Minara swap shell command for the operator if the token
    is Solana-listed. Returns None for any other case (not listed,
    fetch failed, feature disabled).

    Never raises.
    """
    if not getattr(settings, "MINARA_ALERT_ENABLED", True):
        return None
    try:
        detail = await fetch_coin_detail(
            session=session,
            coin_id=coin_id,
            api_key=getattr(settings, "COINGECKO_API_KEY", "") or "",
        )
    except Exception:
        log.exception(
            "minara_alert_detail_fetch_failed", coin_id=coin_id
        )
        return None
    if not detail:
        return None
    try:
        platforms = detail.get("platforms") or {}
        spl_address = platforms.get("solana")
        if not spl_address or not isinstance(spl_address, str):
            return None
        from_token = getattr(settings, "MINARA_ALERT_FROM_TOKEN", "USDC")
        override = getattr(settings, "MINARA_ALERT_AMOUNT_USD_OVERRIDE", None)
        size = override if override is not None else amount_usd
        # Round to whole dollars for clean copy-paste UX
        size_int = int(round(float(size)))
        return (
            f"minara swap --from {from_token} --to {spl_address} "
            f"--amount-usd {size_int}"
        )
    except Exception:
        log.exception(
            "minara_alert_format_failed", coin_id=coin_id
        )
        return None
```

- [ ] **Step 3: Run + commit**

```bash
uv run --native-tls pytest tests/test_minara_alert.py -q
git add scout/trading/minara_alert.py tests/test_minara_alert.py
git commit -m "feat(m1.5c): maybe_minara_command detection + Solana platforms.solana lookup (Task 1)"
```

---

## Task 2: TG alert dispatch integration

**Files:**
- Modify: `scout/trading/tg_alert_dispatch.py`
- Test: `tests/test_tg_alert_dispatch.py` (existing — add integration cases)

- [ ] **Step 1: Update `format_paper_trade_alert` signature + body**

```python
def format_paper_trade_alert(
    *,
    signal_type: str,
    symbol: str,
    coin_id: str,
    entry_price: float,
    amount_usd: float,
    signal_data: dict | None,
    minara_command: str | None = None,  # M1.5c addition
) -> str:
    # ... existing header + extras + link logic unchanged ...

    parts = [header]
    if detail:
        parts.append(detail)
    if minara_command:
        # M1.5c: copy-paste shell command for Solana DEX-eligible tokens.
        # Inserted BEFORE the coingecko link so it's prominent.
        parts.append(f"Run: {minara_command}")
    parts.append(link)
    return "\n".join(parts)
```

- [ ] **Step 2: Update `notify_paper_trade_opened` to call helper**

Inside the existing try block, AFTER the cooldown gate succeeds (pre-emptive 'sent' INSERT) but BEFORE the format call:

```python
        # M1.5c BL-NEW-M1.5C: Minara DEX-eligibility check.
        # Sits between the cooldown claim and format/dispatch so the
        # alert body is complete before send. Never raises.
        from scout.trading.minara_alert import maybe_minara_command
        minara_cmd = await maybe_minara_command(
            session, settings, coin_id=token_id, amount_usd=amount_usd,
        )

        # V3-C1 PR-stage fold: format + dispatch BOTH inside the try.
        try:
            body = format_paper_trade_alert(
                signal_type=signal_type,
                symbol=symbol,
                coin_id=token_id,
                entry_price=entry_price,
                amount_usd=amount_usd,
                signal_data=signal_data,
                minara_command=minara_cmd,  # M1.5c addition
            )
            # R1-C1 fold: parse_mode=None to avoid Markdown 400 silent-fail
            await alerter.send_telegram_message(
                body, session, settings, parse_mode=None
            )
        except Exception as e:
            # ... existing demote-to-dispatch_failed logic unchanged ...
```

- [ ] **Step 3: Integration tests in `tests/test_tg_alert_dispatch.py`**

```python
@pytest.mark.asyncio
async def test_format_with_minara_command_includes_run_line():
    """M1.5c: when minara_command is provided, body has 'Run: minara swap...' line."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BONK",
        coin_id="bonk",
        entry_price=0.0001,
        amount_usd=10.0,
        signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
        minara_command="minara swap --from USDC --to ABC123 --amount-usd 10",
    )
    assert "Run: minara swap --from USDC --to ABC123 --amount-usd 10" in body
    # Run: line appears BEFORE coingecko link
    lines = body.split("\n")
    run_idx = next(i for i, l in enumerate(lines) if l.startswith("Run:"))
    link_idx = next(i for i, l in enumerate(lines) if "coingecko.com" in l)
    assert run_idx < link_idx


@pytest.mark.asyncio
async def test_format_without_minara_command_unchanged():
    """M1.5c: when minara_command is None, format matches pre-M1.5c output."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 30.0, "mcap": 1_000_000_000_000},
        minara_command=None,
    )
    assert "Run:" not in body  # no shell command line


@pytest.mark.asyncio
async def test_notify_includes_minara_command_for_solana_token(tmp_path, monkeypatch):
    """End-to-end: Solana token paper-trade-open alert includes the Run: line."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None):
        sent.append(text)
    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "BONKADDR123"}}
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )

    await notify_paper_trade_opened(
        db, settings, session=None,
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="bonk",
        symbol="BONK",
        entry_price=0.0001,
        amount_usd=10.0,
        signal_data={"price_change_24h": 50.0, "mcap": 2_000_000},
    )
    assert len(sent) == 1
    assert "Run: minara swap --from USDC --to BONKADDR123" in sent[0]
    await db.close()


@pytest.mark.asyncio
async def test_notify_no_minara_command_for_evm_only_token(tmp_path, monkeypatch):
    """Token with platforms.ethereum but no platforms.solana → no Run: line."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade(db, trade_id=42)
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None):
        sent.append(text)
    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"ethereum": "0xabc"}}
    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail
    )

    await notify_paper_trade_opened(
        db, settings, session=None,
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="random",
        symbol="RND",
        entry_price=1.0,
        amount_usd=10.0,
        signal_data={"price_change_24h": 30.0, "mcap": 5_000_000},
    )
    assert len(sent) == 1
    assert "Run:" not in sent[0]
    await db.close()
```

- [ ] **Step 4: Commit**

```bash
uv run --native-tls pytest tests/test_minara_alert.py tests/test_tg_alert_dispatch.py -q
git add scout/trading/tg_alert_dispatch.py tests/test_tg_alert_dispatch.py
git commit -m "feat(m1.5c): tg_alert_dispatch integration — format_paper_trade_alert minara_command kwarg + notify_paper_trade_opened helper call (Task 2)"
```

---

## Task 3: Full regression + black

```bash
uv run --native-tls pytest tests/test_minara_alert.py tests/test_tg_alert_dispatch.py tests/test_trading_dashboard.py -q
uv run --native-tls black scout/ tests/
git commit -am "chore(m1.5c): black reformat (Task 3)"
```

---

## Task 4: PR + 3-vector reviewers + merge + deploy

Per CLAUDE.md §8 (operator-visible alert change, low blast radius — no execution):
- V1 — structural: helper composition, kwarg threading, failure isolation
- V2 — UX: command format readability, cooldown interaction, walkaway exposure
- V3 — silent-failure: CG fetch failure paths, malformed platforms field, format injection

---

## Done criteria

- Solana-listed tokens fire TG alerts with a copy-pasteable `minara swap` command
- Non-Solana tokens fire normal alerts (no `Run:` line)
- CG fetch failure / 404 / rate-limit → alert still fires, just without `Run:` line
- `MINARA_ALERT_ENABLED=False` → no fetch at all (zero-overhead disable)
- Existing TG alert allowlist behavior (eligibility + cooldown + auto_suspend joint flag) unchanged
- M1.5b dispatch atomic check-then-write semantics preserved
- Zero schema migrations

## What this milestone does NOT do

- Does NOT execute trades (Phase 0 Option A; pure decision-support)
- Does NOT install Minara CLI on VPS (operator runs locally)
- Does NOT support EVM chains (Solana-first per operator direction; EVM is M1.5d/M2)
- Does NOT add Telegram approval buttons / inline keyboards (M1.5d/M2)
- Does NOT add per-chain quote token routing (always USDC for now)
- Does NOT add slippage hint to the command (Minara CLI doesn't expose `--max-slippage`)
- Does NOT track command-execution outcomes (operator's local Minara handles that; gecko-alpha has no visibility)
- Does NOT integrate with M1.5b's routing layer (alert-only path; routing is for live execution)

## Reversibility

**Fast revert (no code, no deploy):** `.env` flip `MINARA_ALERT_ENABLED=False` + `systemctl restart gecko-pipeline`. Settings read fresh per dispatch — no cache to invalidate.

**Slower revert (git):** `git revert <PR squash>` removes Settings + helper + format kwarg + integration call. Backward-compatible: removed kwarg defaulted to None.

**Operator copy-paste safety:** the `Run:` line is plain text — Telegram doesn't auto-execute. Operator must explicitly copy + paste + confirm in Minara CLI (Minara's own confirmation prompt fires on swap when called without `--yes`). Three-layer safety: gecko-alpha doesn't execute, Telegram doesn't execute, Minara prompts before executing.
