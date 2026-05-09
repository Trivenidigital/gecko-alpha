**New primitives introduced:** Same as `plan_quote_pair_signal.md` — `CandidateToken.quote_symbol`, `CandidateToken.dex_id`, `candidates.quote_symbol` column, `candidates.dex_id` column, scorer signal `stable_paired_liq` (+5pts raw / +2 normalized), Settings `STABLE_PAIRED_LIQ_THRESHOLD_USD` + `STABLE_PAIRED_BONUS` + `STABLE_QUOTE_SYMBOLS`, migration `bl_quote_pair_v1` (writes schema_version=20260512 to existing `schema_version` table).

# Design — BL-NEW-QUOTE-PAIR

Plan: `plan_quote_pair_signal.md` (R1 + R2 reviewer fixes applied).

## Hermes-first analysis

Inherited from plan-stage. No skill match in 18 Hermes domains for crypto-DEX parsing or token-scoring multipliers. Build from scratch.

## File-level diff

### 1. `scout/models.py`

Add to `CandidateToken` (after existing DexScreener tx fields, line 41):

```python
    # Quote-currency awareness (BL-NEW-QUOTE-PAIR)
    quote_symbol: str | None = None
    dex_id: str | None = None
```

Update `from_dexscreener` (after line 120 `txns_h1_sells = ...`):

```python
    quote_token = data.get("quoteToken") or {}
    quote_symbol = quote_token.get("symbol")
    dex_id = data.get("dexId")
```

Pass `quote_symbol=quote_symbol, dex_id=dex_id` into `cls(...)` constructor at end.

Update `from_coingecko` and `from_geckoterminal` (if exists) — explicitly pass `quote_symbol=None, dex_id=None` (default-None makes this implicit, but kept for documentation).

### 2. `scout/config.py`

Add (after existing scoring threshold settings, in pyproject Settings class):

```python
    STABLE_QUOTE_SYMBOLS: tuple[str, ...] = (
        "USDC", "USDT", "DAI", "FDUSD", "USDe",
        "PYUSD", "RLUSD", "sUSDe",
    )
    STABLE_PAIRED_LIQ_THRESHOLD_USD: float = 50_000.0
    STABLE_PAIRED_BONUS: int = 5
```

### 3. `scout/scorer.py`

Insert a new signal block, **inlined** (matches existing 13 signals; R2 NIT). Place before normalization at line 227. Suggested position: after `perp_anomaly` block at line 224 (alphabetical/thematic — pure pair-data signal alongside other parser-derived signals).

```python
    # stable_paired_liq (BL-NEW-QUOTE-PAIR): +5 raw / +2 normalized
    # for tokens paired with a known stablecoin AND liquidity_usd >= threshold.
    # Counts toward co-occurrence multiplier (intended: stable-pair is real evidence).
    if (
        token.quote_symbol in settings.STABLE_QUOTE_SYMBOLS
        and token.liquidity_usd >= settings.STABLE_PAIRED_LIQ_THRESHOLD_USD
    ):
        points += settings.STABLE_PAIRED_BONUS
        signals.append("stable_paired_liq")
```

`SCORER_MAX_RAW` left at 208. Justification: max single-token raw score with all signals firing remains within clamp; +5 doesn't exceed normalization headroom.

### 4. `scout/db.py`

Update `_CANDIDATE_COLUMNS` (line 36-61) — add `"quote_symbol"` and `"dex_id"` to the tuple.

Add new migration method following the canonical `BEGIN EXCLUSIVE` / `try-except-ROLLBACK` / `SCHEMA_DRIFT_DETECTED` / post-assertion pattern (matches `_migrate_high_peak_fade_columns_and_audit_table` at `scout/db.py:1874-1959`). **R4 CRITICAL fix applied** — earlier draft was missing transactional + drift-logging wrapper:

```python
async def _migrate_bl_quote_pair_v1(self) -> None:
    """BL-NEW-QUOTE-PAIR: add quote_symbol + dex_id to candidates table."""
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    conn = self._conn
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        await conn.execute("BEGIN EXCLUSIVE")

        # Defensive — mirrors HPF/Tier-1a pattern; safe in isolation (e.g. tests).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
            """)

        expected_cols = {"quote_symbol": "TEXT", "dex_id": "TEXT"}
        cur_pragma = await conn.execute("PRAGMA table_info(candidates)")
        existing_cols = {row[1] for row in await cur_pragma.fetchall()}
        for col, coltype in expected_cols.items():
            if col in existing_cols:
                _log.info("schema_migration_column_action",
                          migration="bl_quote_pair_v1", col=col, action="skip_exists")
            else:
                await conn.execute(
                    f"ALTER TABLE candidates ADD COLUMN {col} {coltype}"
                )
                _log.info("schema_migration_column_action",
                          migration="bl_quote_pair_v1", col=col, action="added")

        await conn.execute(
            "INSERT OR IGNORE INTO schema_version "
            "(version, applied_at, description) VALUES (?, ?, ?)",
            (20260512, now_iso, "bl_quote_pair_v1_quote_symbol_dex_id"),
        )

        await conn.commit()
    except Exception:
        try:
            await conn.execute("ROLLBACK")
        except Exception as rb_err:
            _log.exception("schema_migration_rollback_failed", err=str(rb_err))
        _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_quote_pair_v1")
        raise

    # Post-assertion — schema_version row must exist after a successful migration.
    cur = await conn.execute(
        "SELECT 1 FROM schema_version WHERE version = ?", (20260512,)
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("bl_quote_pair_v1 schema_version row missing after migration")
```

Note: `schema_version` table has 3 columns (`version`, `applied_at`, `description`) — verified at `scout/db.py:1886-1891` (NOT 2 columns as earlier draft implied).

Wire into `_apply_migrations` chain — must be a new line `await self._migrate_bl_quote_pair_v1()` appended after the existing migration sequence.

Update `upsert_candidate` SQL — add `quote_symbol`, `dex_id` to column list and `?` placeholders; pass values from candidate dict.

### 5. Tests — file-by-file

**`tests/test_models.py`:**

```python
def test_from_dexscreener_extracts_quote_symbol_and_dex_id():
    raw = {
        "baseToken": {"address": "0xabc", "name": "Foo", "symbol": "FOO"},
        "quoteToken": {"symbol": "USDC"},
        "dexId": "raydium",
        "fdv": 1_000_000, "priceUsd": "0.5",
        "liquidity": {"usd": 75_000},
        "volume": {"h24": 10_000},
        "txns": {"h1": {"buys": 30, "sells": 20}},
        "pairCreatedAt": int((datetime.now(timezone.utc).timestamp() - 86400) * 1000),
        "chainId": "solana",
    }
    token = CandidateToken.from_dexscreener(raw)
    assert token.quote_symbol == "USDC"
    assert token.dex_id == "raydium"

def test_from_dexscreener_handles_missing_quote_token():
    raw = {...}  # no quoteToken / dexId fields
    token = CandidateToken.from_dexscreener(raw)
    assert token.quote_symbol is None
    assert token.dex_id is None

def test_from_dexscreener_handles_null_quote_token():
    raw = {..., "quoteToken": None}  # explicit null
    token = CandidateToken.from_dexscreener(raw)
    assert token.quote_symbol is None  # no AttributeError
```

**`tests/test_scorer.py`:**

```python
def test_stable_paired_liq_bonus_fires_for_usdc_above_threshold(
    settings_factory, token_factory
):
    settings = settings_factory()
    token = token_factory(quote_symbol="USDC", liquidity_usd=75_000)
    score, signals = score_quant(token, settings, [])
    assert "stable_paired_liq" in signals

def test_stable_paired_liq_bonus_blocked_below_threshold(
    settings_factory, token_factory
):
    settings = settings_factory()
    token = token_factory(quote_symbol="USDC", liquidity_usd=49_000)
    score, signals = score_quant(token, settings, [])
    assert "stable_paired_liq" not in signals

def test_stable_paired_liq_bonus_blocked_for_non_stable_quote(
    settings_factory, token_factory
):
    settings = settings_factory()
    token = token_factory(quote_symbol="WSOL", liquidity_usd=100_000)
    score, signals = score_quant(token, settings, [])
    assert "stable_paired_liq" not in signals

def test_stable_paired_liq_bonus_handles_none_quote_symbol(
    settings_factory, token_factory
):
    settings = settings_factory()
    token = token_factory(quote_symbol=None, liquidity_usd=100_000)
    score, signals = score_quant(token, settings, [])
    assert "stable_paired_liq" not in signals

def test_stable_paired_liq_counts_toward_cooccurrence_with_score_delta(
    settings_factory, token_factory
):
    """R3 MUST-FIX: assert numeric score delta from 1.15× co-occurrence multiplier.

    A 2-signal token at score X, when stable_paired_liq pushes it to 3 signals,
    must show a measurable post-multiplier score uplift, not just signal-count
    bump. Asserts the dominant mechanical effect identified by R1.
    """
    settings = settings_factory()
    base_token = token_factory(
        quote_symbol=None, liquidity_usd=75_000,
        # ... configure 2 other signals firing (e.g., buy_pressure + age curve)
    )
    score_2sig, signals_2sig = score_quant(base_token, settings, [])
    assert len(signals_2sig) == 2

    stable_token = token_factory(
        quote_symbol="USDC", liquidity_usd=75_000,
        # ... same 2 other signals
    )
    score_3sig, signals_3sig = score_quant(stable_token, settings, [])
    assert len(signals_3sig) == 3
    # The 1.15× multiplier MUST fire — score must exceed naive +2 normalized
    assert score_3sig > score_2sig + 2, (
        f"Co-occurrence multiplier did not fire: 2sig={score_2sig}, 3sig={score_3sig}"
    )

@pytest.mark.parametrize(
    "quote_symbol",
    ["USDC", "USDT", "DAI", "FDUSD", "USDe", "PYUSD", "RLUSD", "sUSDe"],
)
def test_stable_paired_liq_fires_for_all_listed_stables(
    quote_symbol, settings_factory, token_factory
):
    """R3 NIT: parametrize over all 8 stables — config typo would silently break 7/8."""
    settings = settings_factory()
    token = token_factory(quote_symbol=quote_symbol, liquidity_usd=75_000)
    _, signals = score_quant(token, settings, [])
    assert "stable_paired_liq" in signals

@pytest.mark.parametrize(
    "liquidity_usd, should_fire",
    [
        (50_000.0, True),     # exactly threshold — boundary inclusive
        (49_999.99, False),   # one cent under — boundary exclusive
        (50_000.01, True),    # one cent over — boundary inclusive
    ],
)
def test_stable_paired_liq_threshold_boundary(
    liquidity_usd, should_fire, settings_factory, token_factory
):
    """R3 MUST-FIX: catch fence-post errors at the >= 50_000.0 boundary."""
    settings = settings_factory()
    token = token_factory(quote_symbol="USDC", liquidity_usd=liquidity_usd)
    _, signals = score_quant(token, settings, [])
    assert ("stable_paired_liq" in signals) is should_fire

def test_stable_paired_liq_case_sensitivity(
    settings_factory, token_factory
):
    """R3 MUST-FIX: DexScreener may return lowercase/mixed-case symbols.

    Decision: signal fires ONLY for exact-uppercase match against
    STABLE_QUOTE_SYMBOLS. Parser does NOT normalize case (DexScreener returns
    canonical uppercase per their schema). If they ever return lowercase, this
    test will catch the regression and force a parser-side normalization.
    """
    settings = settings_factory()
    for variant in ["usdc", "Usdc", "USDc"]:
        token = token_factory(quote_symbol=variant, liquidity_usd=75_000)
        _, signals = score_quant(token, settings, [])
        assert "stable_paired_liq" not in signals, (
            f"Lowercase/mixed-case quote_symbol={variant!r} unexpectedly fired"
        )
```

**`tests/test_db.py`:**

```python
async def test_upsert_candidate_persists_quote_symbol_and_dex_id(db_with_schema):
    candidate = {..., "quote_symbol": "USDC", "dex_id": "raydium", ...}
    await db_with_schema.upsert_candidate(candidate)
    row = await db_with_schema._fetch_one(
        "SELECT quote_symbol, dex_id FROM candidates WHERE contract_address=?",
        (candidate["contract_address"],),
    )
    assert row["quote_symbol"] == "USDC"
    assert row["dex_id"] == "raydium"

async def test_upsert_candidate_persists_null_quote_symbol(db_with_schema):
    candidate = {..., "quote_symbol": None, "dex_id": None, ...}
    await db_with_schema.upsert_candidate(candidate)
    row = await db_with_schema._fetch_one(
        "SELECT quote_symbol, dex_id FROM candidates WHERE contract_address=?",
        (candidate["contract_address"],),
    )
    assert row["quote_symbol"] is None
```

**`tests/test_migrations.py` — R3 MUST-FIX expanded coverage:**

```python
async def test_bl_quote_pair_v1_columns_added(tmp_path):
    """Columns exist post-initialize (also covers wired-into-_apply_migrations)."""
    db = Database(...)
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(candidates)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "quote_symbol" in cols
    assert "dex_id" in cols

async def test_bl_quote_pair_v1_wired_into_apply_migrations(tmp_path):
    """R3 MUST-FIX (a): orphaned migration would silently succeed first test.

    Use a freshly-created DB (no schema_version row), call ONLY initialize(),
    then assert version 20260512 was written — proves the migration is in chain.
    """
    db = Database(...)
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT 1 FROM schema_version WHERE version = 20260512"
    )
    assert (await cur.fetchone()) is not None, (
        "bl_quote_pair_v1 not wired into _apply_migrations"
    )

async def test_bl_quote_pair_v1_schema_version_row_written(tmp_path):
    """R3 MUST-FIX (b): schema_version row content."""
    db = Database(...)
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT version, applied_at, description FROM schema_version "
        "WHERE version = 20260512"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 20260512
    assert row[2] == "bl_quote_pair_v1_quote_symbol_dex_id"

async def test_bl_quote_pair_v1_idempotent_rerun_does_not_raise(tmp_path):
    """Re-running the migration on already-migrated DB skips with action=skip_exists."""
    db = Database(...)
    await db.initialize()
    await db._migrate_bl_quote_pair_v1()  # second call — must not raise
    cur = await db._conn.execute("PRAGMA table_info(candidates)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "quote_symbol" in cols
    assert "dex_id" in cols

async def test_bl_quote_pair_v1_preserves_pre_existing_rows(tmp_path):
    """R3 MUST-FIX (c): pre-existing candidates rows survive migration with NULL."""
    db = Database(...)
    # Initialize WITHOUT bl_quote_pair_v1 (use older schema state OR migrate to
    # pre-bl_quote_pair_v1 schema_version, then run bl_quote_pair_v1 explicitly).
    # Simulation approach: initialize fresh, insert a candidate row, then verify
    # that re-running the migration leaves the row intact with NULL new fields.
    await db.initialize()
    pre_candidate = {
        "contract_address": "0xpre", "chain": "ethereum", "token_name": "Pre",
        "ticker": "PRE", "first_seen_at": datetime.now(timezone.utc).isoformat(),
        # ... minimum required columns
    }
    await db.upsert_candidate(pre_candidate)
    await db._migrate_bl_quote_pair_v1()  # idempotent re-run
    cur = await db._conn.execute(
        "SELECT contract_address, quote_symbol, dex_id "
        "FROM candidates WHERE contract_address = ?",
        ("0xpre",),
    )
    row = await cur.fetchone()
    assert row[0] == "0xpre"
    assert row[1] is None  # quote_symbol stays NULL on pre-cutover row
    assert row[2] is None  # dex_id stays NULL on pre-cutover row
```

### 6. Documentation

**`CLAUDE.md`** — update "3 New Scoring Signals" table to "4 New Scoring Signals" + new row:

```
| stable_paired_liq | quote_symbol in {stables} AND liquidity_usd >= 50K | +5 raw / +2 normalized | STABLE_PAIRED_BONUS |
```

**`tasks/todo.md`** — add D+7 soak window + D+3 mid-soak verification query.

## Test plan summary

| Layer | New tests | Existing tests touched |
|---|---|---|
| Models (parser) | 3 | 0 |
| Scorer | 5 | 0 (existing 11-signal tests independent) |
| DB (round-trip + migration) | 3 | 0 |
| **Total** | **11 new** | **0 existing modified** |

Existing test count baseline: 1389. Target post-merge: 1400.

## Migration validation + deploy runbook

**R4 MUST-FIX/NIT applied — explicit pycache+restart sequence.**

VPS deploy steps (in order):

```bash
# 1. Pre-deploy: verify journal_mode is WAL (R4 MUST-FIX) — concurrent ALTER safety
sqlite3 /opt/scout/scout.db "PRAGMA journal_mode;"
# Expect: wal — if "delete", proceed with brief pipeline pause during ALTER.

# 2. Pull latest
cd /opt/scout && git pull

# 3. Clear stale __pycache__ (R4 MUST-FIX — feedback_clear_pycache_on_deploy.md
# was elevated to operator memory after BL-066' 14-startup-error incident).
find /opt/scout -name __pycache__ -exec rm -rf {} +

# 4. Restart service — initialize() will apply bl_quote_pair_v1 migration
sudo systemctl restart scout

# 5. Verify migration applied
sqlite3 /opt/scout/scout.db "PRAGMA table_info(candidates);" | grep -E "quote_symbol|dex_id"
# Expect: 2 lines — one for each new column, type TEXT

sqlite3 /opt/scout/scout.db "SELECT * FROM schema_version WHERE version=20260512;"
# Expect: 20260512|<iso-timestamp>|bl_quote_pair_v1_quote_symbol_dex_id

# 6. Verify forward-ingestion populates new fields (D+0, ~5 min after restart)
sqlite3 /opt/scout/scout.db "
  SELECT COUNT(*),
         SUM(CASE WHEN quote_symbol IS NOT NULL THEN 1 ELSE 0 END) AS with_quote
  FROM candidates
  WHERE chain != 'coingecko' AND first_seen_at > datetime('now', '-10 minutes');
"
# Expect: with_quote ≈ count (DexScreener-sourced rows have quote_symbol).

# 7. D+3 mid-soak verification — fraction of candidates that hit the gate
sqlite3 /opt/scout/scout.db "
  SELECT COUNT(*) AS total,
         SUM(CASE WHEN quote_symbol IN
            ('USDC','USDT','DAI','FDUSD','USDe','PYUSD','RLUSD','sUSDe')
            AND liquidity_usd >= 50000 THEN 1 ELSE 0 END) AS stable_gated
  FROM candidates
  WHERE first_seen_at >= datetime('now', '-3 days');
"
# Expect: stable_gated/total < 0.40 — if higher, escalate scrutiny.
```

If migration fails: see "Soak + revert plan" in plan.md. Rollback via `STABLE_PAIRED_BONUS=0` env override + revert PR if necessary.

## Reviewer dispatch — design stage (2 parallel)

- **R3 (test-strategy / TDD discipline):** Are the 11 tests sufficient to lock the contract? Specifically: do they exercise the co-occurrence interaction path that R1 flagged as the dominant mechanical effect? Any boundary case missing? Any mocking pattern off-convention?
- **R4 (operational / deploy-blast-radius):** What happens on first VPS deploy when the migration runs against a multi-GB live `candidates` table? Are there ALTER-TABLE locking concerns on aiosqlite at that volume? What's the rollback if migration partially completes?
