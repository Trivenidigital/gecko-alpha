# BL-065: Dispatch paper trades from cashtag-only resolutions — Implementation Plan

**New primitives introduced:** new column `tg_social_channels.cashtag_trade_eligible INTEGER NOT NULL DEFAULT 0` (added via `_migrate_feedback_loop_schema` extension, gated by `paper_migrations` row); new function `scout/social/telegram/dispatcher.py:dispatch_cashtag_to_engine` (sibling of existing `dispatch_to_engine`, with cashtag-specific gate set); new helpers `_channel_cashtag_trade_eligible`, `_evaluate_cashtag` (cashtag-specific gates); new Settings fields `PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD: float = 300.0` (defaults to same as CA path), `PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD: float = 100_000.0`, `PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO: float = 2.0`; new `BlockedGate` literal values `cashtag_disabled`, `cashtag_below_floor`, `cashtag_ambiguous`; new structured log events `tg_social_cashtag_admission_blocked`, `tg_social_cashtag_trade_dispatched`. No new models — reuses existing `ResolvedToken` (`candidates_top3` is already `list[ResolvedToken]`).

**Goal:** Today, when a curator posts only `$EITHER` (cashtag) without a contract address, the listener at `scout/social/telegram/listener.py:249-276` sends a Telegram alert with top-3 CoinGecko candidates and **returns before** `dispatch_to_engine`. Trade-eligible curators (`@thanos_mind`, `@detecter_calls`) currently posting cashtag-only signals have produced **zero** paper trades despite the listener being healthy. BL-065 extends the cashtag path to dispatch a paper trade when the channel has the new `cashtag_trade_eligible=1` flag set, picks top-1 candidate (subject to floor + disambiguity gates), and reuses `_has_open_tg_social_exposure` for dedup with the CA path.

**Architecture:**
- **New dispatcher function `dispatch_cashtag_to_engine`** — sibling of `dispatch_to_engine`. Shares `_channel_*` helpers and `engine.open_trade`. Cashtag-specific gate set: skips `no_ca` (by design — that's the whole point) and `safety_*` (no CA = no GoPlus); adds `cashtag_disabled`, `cashtag_below_floor`, `cashtag_ambiguous`, plus the existing `dedup_open` and `tg_social_quota`.
- **Per-channel opt-in column** `cashtag_trade_eligible INTEGER NOT NULL DEFAULT 0` — fail-closed default. Operators explicitly enable per-channel via SQL UPDATE post-deploy.
- **Top-1 candidate selection** with two filter gates: (a) mcap >= floor (default $100K, skips dust); (b) disambiguity ratio: only dispatch if `len(candidates) == 1` OR `candidates[0].mcap >= candidates[1].mcap * 2.0` (top candidate clearly stands out).
- **Dedup with CA path:** identical `_has_open_tg_social_exposure(token_id)` check. If curator later posts CA for the same token, dedup blocks the second trade because both paths share `token_id` (CoinGecko coin_id).

**Tech Stack:** Python 3.11, aiosqlite, pytest-asyncio. Existing project conventions per CLAUDE.md.

**Honest scope-decision note:**
- Picked option (b) from BL-065 backlog "Safety" question — separate `cashtag_trade_eligible` column. Rejected option (c) "require both `trade_eligible=1 AND safety_required=0`" because coupling those two flags conflates concerns: an operator may want a channel to dispatch CAs (safety-checked) AND cashtags (no safety) WITHOUT relaxing the CA-path safety. Independent flags give correct semantics.
- Cashtag dispatch is **inherently no-safety-check** — the cashtag→token_id resolution doesn't yield a CA to check with GoPlus. Operators opt into this risk explicitly per channel by setting `cashtag_trade_eligible=1`. This is documented in the column comment.
- Trade size defaults to same as CA path ($300) for v1; the operator can tune via the new `PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD` setting if cashtag confidence proves lower in real data.
- `candidates_top3[0]` selection assumes the resolver returns them in a meaningful order. **Verified** in `scout/social/telegram/resolver.py:292` — order is by CoinGecko search-rank-then-mcap (existing behaviour). Top-1 = "best CoinGecko match for this cashtag." Disambiguity gate (top.mcap >= 2× second.mcap) protects against the "look-alike token at similar mcap" failure mode.

---

## Task 1 — Schema migration: add `cashtag_trade_eligible` column

**Files:**
- Modify: `scout/db.py` — extend `_migrate_feedback_loop_schema` with PRAGMA-guarded ALTER + paper_migrations gate; extend POST-ASSERTION set
- Test: `tests/test_chain_outcomes_hydration.py` is the wrong file — use `tests/test_tg_social_db.py` if present, else create `tests/test_bl065_cashtag_dispatch.py`

**Why:** Per-channel opt-in lets operators enable cashtag dispatch on a known-good curator (e.g. `@thanos_mind`) without auto-enabling on every alert-only channel. Default 0 = fail-closed.

- [ ] **Step 1.1 — Locate the migration insertion point**

Run: `grep -n "bl071a_chain_matches_mcap_at_completion\|bl071b_unstamp_expired_narrative" scout/db.py`
The new migration goes immediately AFTER the BL-071a' block (after the `if "mcap_at_completion" not in cm_cols:` else-branch closes), and BEFORE the `CREATE INDEX` block that follows.

- [ ] **Step 1.2 — Write failing schema test**

Create or extend `tests/test_bl065_cashtag_dispatch.py`:

```python
"""BL-065: cashtag dispatch tests — schema, gate evaluation, end-to-end."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_cashtag_trade_eligible_column_exists(db):
    """BL-065: schema migration adds column with NOT NULL DEFAULT 0."""
    cur = await db._conn.execute("PRAGMA table_info(tg_social_channels)")
    cols = {row[1]: (row[2], row[3], row[4]) for row in await cur.fetchall()}
    # (type, notnull, dflt_value)
    assert "cashtag_trade_eligible" in cols
    coltype, notnull, default = cols["cashtag_trade_eligible"]
    assert coltype == "INTEGER"
    assert notnull == 1
    assert default == "0"


@pytest.mark.asyncio
async def test_cashtag_trade_eligible_default_zero_for_new_channel(db):
    """New rows default to fail-closed (cashtag dispatch off)."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, safety_required, added_at) "
        "VALUES (?, ?, 1, 1, ?)",
        ("@test", "Test", now),
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT cashtag_trade_eligible FROM tg_social_channels WHERE channel_handle='@test'"
    )
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_cashtag_trade_eligible_migration_paper_migrations_row(tmp_path):
    """Migration records bl065_cashtag_trade_eligible in paper_migrations
    (idempotency gate; second startup is a no-op)."""
    db = Database(tmp_path / "mig.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM paper_migrations WHERE name = ?",
        ("bl065_cashtag_trade_eligible",),
    )
    assert (await cur.fetchone()) is not None
    await db.close()
```

- [ ] **Step 1.3 — Run, expect 3 fails**

Run: `cd C:/projects/gecko-alpha && SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl065_cashtag_dispatch.py -v`
Expected: column missing on `PRAGMA table_info`; 3 FAIL.

- [ ] **Step 1.4 — Add migration to `_migrate_feedback_loop_schema`**

Edit `scout/db.py`. Find the BL-071a' block (search `bl071a_chain_matches_mcap_at_completion`). Append after that block, BEFORE the `CREATE INDEX IF NOT EXISTS idx_paper_trades_combo_opened` line:

```python
            # BL-065 (Bundle B 2026-05-04): per-channel cashtag dispatch
            # opt-in. Default 0 = fail-closed; operators explicitly UPDATE
            # to 1 per known-good curator. Independent of trade_eligible
            # (the CA-path flag) and safety_required (the no-record-pass
            # flag) — three flags = three independent concerns.
            cur = await conn.execute("PRAGMA table_info(tg_social_channels)")
            tg_chan_cols2 = {row[1] for row in await cur.fetchall()}
            if "cashtag_trade_eligible" not in tg_chan_cols2:
                await conn.execute(
                    "ALTER TABLE tg_social_channels "
                    "ADD COLUMN cashtag_trade_eligible INTEGER NOT NULL DEFAULT 0"
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (
                        "bl065_cashtag_trade_eligible",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            else:
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (
                        "bl065_cashtag_trade_eligible",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
```

- [ ] **Step 1.5 — Extend POST-ASSERTION set**

In the same `_migrate_feedback_loop_schema` body, locate the post-assertion `recorded` set check (search `bl071a_chain_matches_mcap_at_completion`). Add `bl065_cashtag_trade_eligible` to both the SELECT and the missing-set:

```python
            cur = await conn.execute(
                "SELECT name FROM paper_migrations WHERE name IN "
                "('bl061_ladder', 'bl062_peak_fade', 'bl063_moonshot', "
                "'bl064_tg_social', 'bl064_safety_required_per_channel', "
                "'bl071b_unstamp_expired_narrative', "
                "'bl071a_chain_matches_mcap_at_completion', "
                "'bl065_cashtag_trade_eligible')"
            )
            recorded = {row[0] for row in await cur.fetchall()}
            missing_migrations = {
                "bl061_ladder",
                "bl062_peak_fade",
                "bl063_moonshot",
                "bl064_tg_social",
                "bl064_safety_required_per_channel",
                "bl071b_unstamp_expired_narrative",
                "bl071a_chain_matches_mcap_at_completion",
                "bl065_cashtag_trade_eligible",
            } - recorded
```

- [ ] **Step 1.6 — Run tests, expect 3 pass**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl065_cashtag_dispatch.py -v`
Expected: 3 passed.

- [ ] **Step 1.7 — Commit**

```bash
git add scout/db.py tests/test_bl065_cashtag_dispatch.py
git commit -m "feat(BL-065): add cashtag_trade_eligible column to tg_social_channels

Per-channel opt-in for cashtag dispatch. Default 0 (fail-closed).
Independent of trade_eligible (CA-path flag) and safety_required
(no-record-pass flag) — three flags = three independent concerns.

Migration appended to _migrate_feedback_loop_schema; extends
POST-ASSERTION set with bl065_cashtag_trade_eligible."
```

---

## Task 2 — Cashtag-specific dispatcher

**Files:**
- Modify: `scout/social/telegram/dispatcher.py` — add `_channel_cashtag_trade_eligible`, `_evaluate_cashtag`, `dispatch_cashtag_to_engine`
- Modify: `scout/social/telegram/models.py` — extend `BlockedGate` literal with `cashtag_disabled`, `cashtag_below_floor`, `cashtag_ambiguous`
- Modify: `scout/config.py` — add 3 settings (trade amount, mcap floor, disambiguity ratio)
- Test: `tests/test_bl065_cashtag_dispatch.py` (extend with gate tests)

**Why:** Sibling dispatcher (vs. branching the existing `evaluate()` on caller intent) keeps gate semantics clean. Existing `dispatch_to_engine` is unchanged. Existing `_channel_trade_eligible`, `_has_open_tg_social_exposure`, `_tg_social_open_count` helpers are reused as-is.

- [ ] **Step 2.1 — Add Settings fields**

Edit `scout/config.py`. Find `PAPER_TG_SOCIAL_TRADE_AMOUNT_USD` (existing setting for CA path). Add three siblings nearby:

```python
    # BL-065 (Bundle B 2026-05-04): cashtag-only dispatch tunables
    PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD: float = 300.0  # default = same as CA path; tune lower if confidence is empirically lower
    PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD: float = 100_000.0  # skip dust candidates
    PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO: float = 2.0  # top.mcap >= 2× second.mcap, else reject as ambiguous
```

(Verify the existing `PAPER_TG_SOCIAL_TRADE_AMOUNT_USD` field by grep first — adjust placement to keep logically-grouped settings adjacent.)

- [ ] **Step 2.2 — Extend BlockedGate literal**

Edit `scout/social/telegram/models.py`. Find `BlockedGate = Literal[...]` (around line 86-94). Add three new values:

```python
BlockedGate = Literal[
    "no_ca",
    "safety_unknown",
    "safety_failed",
    "channel_disabled",
    "dedup_open",
    "tg_social_quota",
    "engine_rejected",
    # BL-065 cashtag-path gates
    "cashtag_disabled",
    "cashtag_below_floor",
    "cashtag_ambiguous",
]
```

- [ ] **Step 2.3 — Write failing dispatcher tests**

Append to `tests/test_bl065_cashtag_dispatch.py`:

```python
from scout.social.telegram.models import ResolvedToken
from scout.social.telegram.dispatcher import (
    _channel_cashtag_trade_eligible,
    _evaluate_cashtag,
    dispatch_cashtag_to_engine,
)


def _candidate(token_id: str, symbol: str, mcap: float, price: float = 1.0) -> ResolvedToken:
    """Build a cashtag-resolution candidate (no CA, safety_skipped_no_ca=True)."""
    return ResolvedToken(
        token_id=token_id,
        symbol=symbol,
        chain=None,
        contract_address=None,
        mcap=mcap,
        price_usd=price,
        safety_pass=False,
        safety_check_completed=False,
        safety_skipped_no_ca=True,
    )


async def _seed_channel(db, handle: str, *, trade_eligible=1, safety_required=1, cashtag=0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, safety_required, "
        "cashtag_trade_eligible, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (handle, handle, trade_eligible, safety_required, cashtag, now),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_channel_cashtag_eligible_helper(db):
    await _seed_channel(db, "@on", cashtag=1)
    await _seed_channel(db, "@off", cashtag=0)
    assert await _channel_cashtag_trade_eligible(db, "@on") is True
    assert await _channel_cashtag_trade_eligible(db, "@off") is False
    assert await _channel_cashtag_trade_eligible(db, "@missing") is False  # fail-closed


@pytest.mark.asyncio
async def test_evaluate_cashtag_blocked_when_channel_disabled(db, settings_factory):
    await _seed_channel(db, "@off", cashtag=0)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-a", "A", 5_000_000)],
        channel_handle="@off",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_disabled"


@pytest.mark.asyncio
async def test_evaluate_cashtag_blocked_when_below_floor(db, settings_factory):
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-dust", "D", 50_000)],  # below 100K floor
        channel_handle="@on",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_below_floor"


@pytest.mark.asyncio
async def test_evaluate_cashtag_blocked_when_ambiguous(db, settings_factory):
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    # Top is 5M, second is 4M — ratio 1.25 < 2.0
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[
            _candidate("token-top", "TOP", 5_000_000),
            _candidate("token-look", "LOOK", 4_000_000),
        ],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "cashtag_ambiguous"


@pytest.mark.asyncio
async def test_evaluate_cashtag_passes_when_clearly_dominant(db, settings_factory):
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    # Top 5M vs second 1M — ratio 5.0 >= 2.0 → pass
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[
            _candidate("token-clear", "CLR", 5_000_000),
            _candidate("token-other", "OTH", 1_000_000),
        ],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is True
    assert decision.blocked_gate is None


@pytest.mark.asyncio
async def test_evaluate_cashtag_passes_when_only_one_candidate(db, settings_factory):
    """Single-candidate case: no disambiguity check needed."""
    await _seed_channel(db, "@on", cashtag=1)
    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-only", "ONLY", 1_000_000)],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is True


@pytest.mark.asyncio
async def test_evaluate_cashtag_blocked_when_dedup_open(db, settings_factory):
    """Per-OPEN-exposure dedup: shared with CA path."""
    await _seed_channel(db, "@on", cashtag=1)
    # Seed an open paper_trade + matching tg_social_signals row
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('token-dup','DUP','Dup','coingecko','tg_social','{}',
                   1.0, 300, 300, 1.2, 0.9, 'open', ?)""",
        (now,),
    )
    pt_cur = await db._conn.execute("SELECT last_insert_rowid()")
    pt_id = (await pt_cur.fetchone())[0]
    await db._conn.execute(
        """INSERT INTO tg_social_messages
           (channel_handle, msg_id, posted_at, sender, text, cashtags,
            contracts, urls, parsed_at)
           VALUES ('@on', 1, ?, 'tester', 'test', '[]', '[]', '[]', ?)""",
        (now, now),
    )
    msg_cur = await db._conn.execute("SELECT last_insert_rowid()")
    msg_pk = (await msg_cur.fetchone())[0]
    await db._conn.execute(
        """INSERT INTO tg_social_signals
           (message_pk, token_id, symbol, contract_address, chain,
            mcap_at_sighting, resolution_state, source_channel_handle,
            paper_trade_id, created_at)
           VALUES (?, 'token-dup', 'DUP', NULL, NULL, 1000000.0,
                   'cashtag', '@on', ?, ?)""",
        (msg_pk, pt_id, now),
    )
    await db._conn.commit()

    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
    )
    decision = await _evaluate_cashtag(
        db=db,
        settings=s,
        candidates=[_candidate("token-dup", "DUP", 5_000_000)],
        channel_handle="@on",
    )
    assert decision.dispatch_trade is False
    assert decision.blocked_gate == "dedup_open"
```

- [ ] **Step 2.4 — Run, expect 7 fails**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl065_cashtag_dispatch.py -v -k "cashtag"`
Expected: ImportError or AttributeError on `_channel_cashtag_trade_eligible` / `_evaluate_cashtag` / `dispatch_cashtag_to_engine`.

- [ ] **Step 2.5 — Implement helpers + dispatcher**

Edit `scout/social/telegram/dispatcher.py`. Append after `dispatch_to_engine`:

```python
async def _channel_cashtag_trade_eligible(db: Database, channel_handle: str) -> bool:
    """BL-065: per-channel opt-in for cashtag dispatch. Fail-closed default
    (returns False on missing row, NULL, or 0 — explicit 1 required).

    Independent of trade_eligible (the CA-path flag) — operator may want
    a channel to dispatch CAs without dispatching cashtags, or vice versa.
    """
    cur = await db._conn.execute(
        "SELECT cashtag_trade_eligible FROM tg_social_channels "
        "WHERE channel_handle = ? AND removed_at IS NULL",
        (channel_handle,),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return False
    return bool(row[0])


async def _evaluate_cashtag(
    *,
    db: Database,
    settings: Settings,
    candidates: list[ResolvedToken],
    channel_handle: str,
) -> AdmissionDecision:
    """BL-065: cashtag-specific gates.

    Skipped (vs. CA path):
      * Gate 2 no_ca — by definition no CA; skipping is the whole point
      * Gate 4 safety — no CA = no GoPlus; operator opts into this risk
        explicitly via cashtag_trade_eligible=1

    Added:
      * cashtag_disabled — channel.cashtag_trade_eligible=0
      * cashtag_below_floor — top candidate mcap < PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD
      * cashtag_ambiguous — len>1 AND top.mcap < second.mcap × DISAMBIGUITY_RATIO

    Reused (from CA path):
      * dedup_open — per-OPEN-exposure dedup by token_id (shared semantic)
      * tg_social_quota — same TG_SOCIAL_MAX_OPEN_TRADES global cap
    """
    # Gate A: channel cashtag opt-in
    if not await _channel_cashtag_trade_eligible(db, channel_handle):
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="cashtag_disabled",
            reason="tg_social_channels.cashtag_trade_eligible=0 (default)",
        )

    if not candidates:
        # Defensive — caller should have filtered, but explicit here.
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="cashtag_disabled",
            reason="no candidates (caller bug)",
        )
    top = candidates[0]

    # Gate B: mcap floor (skip dust)
    min_mcap = settings.PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD
    if (top.mcap or 0) < min_mcap:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="cashtag_below_floor",
            reason=(
                f"top candidate mcap {top.mcap} < floor {min_mcap}"
            ),
        )

    # Gate C: disambiguity (top must clearly dominate #2)
    if len(candidates) > 1:
        second_mcap = candidates[1].mcap or 0
        ratio_required = settings.PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO
        if second_mcap > 0 and (top.mcap or 0) < second_mcap * ratio_required:
            return AdmissionDecision(
                dispatch_trade=False,
                blocked_gate="cashtag_ambiguous",
                reason=(
                    f"top mcap {top.mcap} < {ratio_required}× second mcap "
                    f"{second_mcap} — possible look-alike token"
                ),
            )

    # Gate D: per-OPEN-exposure dedup (shared with CA path)
    if await _has_open_tg_social_exposure(db, top.token_id):
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="dedup_open",
            reason="another tg_social trade is currently open on this token",
        )

    # Gate E: tg_social slot quota
    open_count = await _tg_social_open_count(db)
    if open_count >= settings.TG_SOCIAL_MAX_OPEN_TRADES:
        return AdmissionDecision(
            dispatch_trade=False,
            blocked_gate="tg_social_quota",
            reason=(
                f"tg_social open trades {open_count} "
                f">= TG_SOCIAL_MAX_OPEN_TRADES {settings.TG_SOCIAL_MAX_OPEN_TRADES}"
            ),
        )

    return AdmissionDecision(dispatch_trade=True)


async def dispatch_cashtag_to_engine(
    *,
    db: Database,
    settings: Settings,
    engine: TradingEngine,
    candidates: list[ResolvedToken],
    cashtag: str,  # e.g. "EITHER" — already normalized (no '$')
    channel_handle: str,
) -> tuple[int | None, str | None]:
    """BL-065: dispatch top-1 cashtag candidate to TradingEngine.open_trade.

    Returns (paper_trade_id, blocked_gate). signal_data carries the
    cashtag-resolution provenance fields per BL-065 acceptance:
    {"resolution": "cashtag", "cashtag": "$X", "candidate_rank": 1,
     "candidates_total": N}.

    On any rejection, returns (None, gate_name). On engine-side rejection,
    gate is 'engine_rejected' (engine logs specific reason).
    """
    decision = await _evaluate_cashtag(
        db=db, settings=settings, candidates=candidates, channel_handle=channel_handle
    )
    if not decision.dispatch_trade:
        log.info(
            "tg_social_cashtag_admission_blocked",
            cashtag=cashtag,
            candidates_total=len(candidates),
            channel_handle=channel_handle,
            gate_name=decision.blocked_gate,
            reason=decision.reason,
        )
        return (None, decision.blocked_gate)

    top = candidates[0]
    trade_id = await engine.open_trade(
        token_id=top.token_id,
        symbol=top.symbol,
        name=top.symbol,
        chain=top.chain or "coingecko",
        signal_type="tg_social",
        signal_data={
            "channel_handle": channel_handle,
            "resolution": "cashtag",
            "cashtag": f"${cashtag}",
            "candidate_rank": 1,
            "candidates_total": len(candidates),
            "mcap_at_sighting": top.mcap,
        },
        amount_usd=settings.PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD,
        entry_price=top.price_usd,
        signal_combo="tg_social",
    )
    if trade_id is not None:
        log.info(
            "tg_social_cashtag_trade_dispatched",
            paper_trade_id=trade_id,
            token_id=top.token_id,
            symbol=top.symbol,
            cashtag=f"${cashtag}",
            candidates_total=len(candidates),
            amount_usd=settings.PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD,
            channel_handle=channel_handle,
        )
        return (trade_id, None)

    log.info(
        "tg_social_cashtag_admission_blocked_engine",
        token_id=top.token_id,
        symbol=top.symbol,
        cashtag=f"${cashtag}",
        channel_handle=channel_handle,
        note="see engine log for specific gate",
    )
    return (None, "engine_rejected")
```

- [ ] **Step 2.6 — Run, expect all dispatcher tests pass**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl065_cashtag_dispatch.py -v -k "cashtag"`
Expected: 7 (or however many we wrote) passed.

- [ ] **Step 2.7 — Commit**

```bash
git add scout/social/telegram/dispatcher.py scout/social/telegram/models.py scout/config.py tests/test_bl065_cashtag_dispatch.py
git commit -m "feat(BL-065): cashtag-specific dispatcher (sibling of CA path)

dispatch_cashtag_to_engine + _evaluate_cashtag + _channel_cashtag_
trade_eligible. Cashtag-specific gates: cashtag_disabled,
cashtag_below_floor, cashtag_ambiguous (len>1 AND top<2× second).
Reuses dedup_open + tg_social_quota from CA path.

3 new Settings fields (CASHTAG_TRADE_AMOUNT_USD/MIN_MCAP_USD/
DISAMBIGUITY_RATIO). 3 new BlockedGate literals."
```

---

## Task 3 — Wire listener cashtag-only branch to dispatch

**Files:**
- Modify: `scout/social/telegram/listener.py` lines ~249-276 — replace early-return with dispatch + alert
- Test: `tests/test_bl065_cashtag_dispatch.py` — end-to-end test

**Why:** This is the line of code BL-065 exists to fix. Today: `return` before `dispatch_to_engine`. After: call `dispatch_cashtag_to_engine`, capture `paper_trade_id`, format alert body, persist signal row with `paper_trade_id` populated.

- [ ] **Step 3.1 — Write failing end-to-end test**

Append to `tests/test_bl065_cashtag_dispatch.py`:

```python
@pytest.mark.asyncio
async def test_dispatch_cashtag_end_to_end_opens_paper_trade(db, settings_factory, monkeypatch):
    """BL-065 acceptance test: posting $CASHTAG to a cashtag_trade_eligible=1
    channel opens a paper_trade with signal_type='tg_social' and signal_data
    carrying {resolution: cashtag, cashtag: $X, candidate_rank: 1,
    candidates_total: N}."""
    from scout.social.telegram.dispatcher import dispatch_cashtag_to_engine

    await _seed_channel(db, "@trusted", cashtag=1)

    s = settings_factory(
        PAPER_TG_SOCIAL_CASHTAG_TRADE_AMOUNT_USD=300.0,
        PAPER_TG_SOCIAL_CASHTAG_MIN_MCAP_USD=100_000.0,
        PAPER_TG_SOCIAL_CASHTAG_DISAMBIGUITY_RATIO=2.0,
        TG_SOCIAL_MAX_OPEN_TRADES=20,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_MAX_OPEN_TRADES=50,
    )

    # Stub TradingEngine to record the call + return a fake trade_id
    captured_calls = []

    class _StubEngine:
        async def open_trade(self, **kwargs):
            captured_calls.append(kwargs)
            return 42  # fake paper_trade_id

    candidates = [
        _candidate("either-coin", "EITHER", 5_000_000),
        _candidate("either-token", "EITHER", 1_000_000),  # 5× ratio gap, not ambiguous
    ]

    paper_trade_id, blocked = await dispatch_cashtag_to_engine(
        db=db,
        settings=s,
        engine=_StubEngine(),
        candidates=candidates,
        cashtag="EITHER",
        channel_handle="@trusted",
    )

    assert paper_trade_id == 42
    assert blocked is None
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["signal_type"] == "tg_social"
    assert call["amount_usd"] == 300.0
    sd = call["signal_data"]
    assert sd["resolution"] == "cashtag"
    assert sd["cashtag"] == "$EITHER"
    assert sd["candidate_rank"] == 1
    assert sd["candidates_total"] == 2
    assert sd["channel_handle"] == "@trusted"
```

- [ ] **Step 3.2 — Run, expect pass (Task 2 already implemented dispatcher)**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl065_cashtag_dispatch.py::test_dispatch_cashtag_end_to_end_opens_paper_trade -v`
Expected: PASS.

- [ ] **Step 3.3 — Modify listener cashtag-only branch**

Edit `scout/social/telegram/listener.py`. Find the cashtag-only branch (search `# Cashtag-only candidates path`). Replace lines 249-276 with:

```python
    # Cashtag-only candidates path
    if not result.tokens and result.candidates_top3:
        # BL-065 (Bundle B 2026-05-04): dispatch top-1 candidate to engine
        # if channel has cashtag_trade_eligible=1. Otherwise alert-only
        # (existing behaviour).
        from scout.social.telegram.dispatcher import dispatch_cashtag_to_engine

        cashtag_normalized = (
            parsed.cashtags[0] if parsed.cashtags else ""
        )  # already upper, no '$' (per parser contract)
        try:
            paper_trade_id, blocked_gate = await dispatch_cashtag_to_engine(
                db=db,
                settings=settings,
                engine=engine,
                candidates=result.candidates_top3,
                cashtag=cashtag_normalized,
                channel_handle=channel_handle,
            )
        except Exception as e:
            await _append_dlq(db, channel_handle, msg_id or 0, text, e)
            paper_trade_id = None
            blocked_gate = "engine_rejected"

        body = format_candidates_alert(
            channel_handle=channel_handle,
            cashtags=parsed.cashtags,
            candidates=result.candidates_top3,
            msg_link=msg_link,
            paper_trade_id=paper_trade_id,  # NEW — None if not dispatched
            blocked_gate=blocked_gate,  # NEW — None if dispatched
        )
        try:
            await send_telegram(
                http_session, telegram_bot_token, telegram_chat_id, body
            )
        except Exception as e:
            log.warning("tg_social_alert_send_failed", error=str(e))
        top = result.candidates_top3[0]
        await _persist_signal_row(
            db=db,
            message_pk=message_pk,
            token_id=top.token_id,
            symbol=top.symbol,
            contract_address=None,
            chain=None,
            mcap=top.mcap,
            resolution_state=result.state.value,
            channel_handle=channel_handle,
            paper_trade_id=paper_trade_id,  # NEW — was hard-coded None
        )
        return
```

- [ ] **Step 3.4 — Update `format_candidates_alert` to accept new fields**

Find `format_candidates_alert` in `scout/social/telegram/alerter.py`. Add `paper_trade_id` and `blocked_gate` kwargs (default None each, since not all callers pass them — but this listener will). When `paper_trade_id is not None`, append a "📍 paper_trade_id=N (cashtag dispatch)" line to the body. When `blocked_gate is not None`, append "🚫 blocked: {gate}" line.

(Read the existing `format_candidates_alert` first; mirror the style used in `format_resolved_alert` for `paper_trade_id`/`blocked_gate` rendering.)

- [ ] **Step 3.5 — Run full chain + telegram + heartbeat regression**

Run:
```bash
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl065_cashtag_dispatch.py tests/test_chains_tracker.py tests/test_chains_db.py tests/test_chains_learn.py tests/test_chain_outcomes_hydration.py tests/test_chain_mcap_fetcher.py tests/test_heartbeat.py tests/test_heartbeat_mcap_missing.py tests/test_tg_social_resolver.py
```
Expected: all green or cleanly skipped.

- [ ] **Step 3.6 — Commit**

```bash
git add scout/social/telegram/listener.py scout/social/telegram/alerter.py tests/test_bl065_cashtag_dispatch.py
git commit -m "feat(BL-065): listener cashtag branch dispatches to engine

Replaces the early-return at listener.py:249-276 with a call to
dispatch_cashtag_to_engine. Top-1 candidate is dispatched if the
channel has cashtag_trade_eligible=1, candidate mcap >= floor, and
top is at least 2× the second's mcap (or only one candidate).

Alert body now includes paper_trade_id (when dispatched) or
blocked_gate (when admission denied) so curators see the outcome.

format_candidates_alert extended with paper_trade_id + blocked_gate
kwargs (default None — preserves existing alert-only call shape)."
```

---

## Final integration

- [ ] **Step F.1 — Full chain + heartbeat + telegram regression**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/ -k "chain or heartbeat or tg or bl065" -q`
Expected: all green or cleanly skipped.

- [ ] **Step F.2 — Format**

Run: `uv run black scout/db.py scout/social/telegram/ scout/config.py tests/test_bl065_cashtag_dispatch.py`

- [ ] **Step F.3 — Commit any formatting changes**

```bash
git add -u
git commit -m "style: black formatting"
```

---

## Operational verification post-deploy

After `git pull` + `systemctl restart gecko-pipeline`:

1. **Pre-deploy backup:** `cp /root/gecko-alpha/scout.db /root/gecko-alpha/scout.db.bak.$(date +%s)`
2. **Service started cleanly:** `systemctl status gecko-pipeline` — active+running.
3. **Migration applied:** `sqlite3 scout.db "SELECT name FROM paper_migrations WHERE name='bl065_cashtag_trade_eligible'"` returns the row.
4. **Column exists:** `sqlite3 scout.db "PRAGMA table_info(tg_social_channels)"` lists `cashtag_trade_eligible INTEGER`.
5. **Default fail-closed:** `sqlite3 scout.db "SELECT channel_handle, cashtag_trade_eligible FROM tg_social_channels"` shows all existing channels with `cashtag_trade_eligible=0`. No traffic change for any channel until operator explicitly enables.
6. **Enable on a known curator (operator-driven, post-verify):**
   ```sql
   UPDATE tg_social_channels SET cashtag_trade_eligible = 1
    WHERE channel_handle = '@thanos_mind';
   ```
7. **First cashtag dispatch:** when `@thanos_mind` posts a cashtag-only signal that resolves to top-1 candidate clearly dominating + above floor, look in journalctl for `tg_social_cashtag_trade_dispatched` event with `paper_trade_id=N`, `cashtag=$X`, `candidates_total=N`. Cross-check with `sqlite3 scout.db "SELECT id, signal_data FROM paper_trades WHERE signal_type='tg_social' AND signal_data LIKE '%cashtag%' ORDER BY id DESC LIMIT 1"`.
8. **Admission-blocked path:** for cashtag-disabled channels, look for `tg_social_cashtag_admission_blocked gate_name=cashtag_disabled` events.

---

## Self-Review

1. **Scope coverage:**
   - Schema column → Task 1 ✓
   - Cashtag-specific dispatcher → Task 2 ✓
   - Listener wiring → Task 3 ✓
   - Acceptance test (signal_data carries cashtag/rank/total) → Task 3 Step 3.1 ✓
2. **Placeholder scan:** none — all code shown verbatim ✓
3. **New primitives marker:** present at top with all new column/functions/settings/log events ✓
4. **TDD discipline:** failing-test → minimal-impl → passing-test → commit per task ✓
5. **No cross-task coupling:** Tasks 1 (schema), 2 (dispatcher), 3 (listener) touch different modules; could be reverted independently ✓
6. **Honest scope:** picked option (b) per-channel flag (vs (c) coupled flags); cashtag dispatch inherently no-safety (operator opts into risk); top-1 with floor + disambiguity gates documented at top.
