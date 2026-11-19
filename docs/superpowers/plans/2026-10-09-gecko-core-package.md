# gecko core package — Implementation Plan (Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** `gecko/` package (settings, store, runs, freshness, notify, collectors for DexScreener/GeckoTerminal/CoinGecko-Demo, features, rank, ledger, backtest, CLI); `gecko.db` 7-table schema; `tests/gecko/`.

**Goal:** Build the deterministic core of the lean watchlist: a 7-table SQLite store fed by three free discovery sources, point-in-time features, the F4-only ranking rule, the forward-return ledger, and a backtest harness anchored to the 2026-09-18 study.

**Architecture:** Every job is `python -m gecko <job>`: it opens the store, does one deterministic pass, writes a `runs` row and exits non-zero on failure (so Hermes cron incidents fire). No LLM anywhere in this plan. Plan 2 adds enrichment (Helius/Blockscout/GoPlus/GMGN) and the Telethon collector; Plan 3 wires Hermes cron, MCP, skills, agent jobs and the cutover.

**Tech Stack:** Python ≥ 3.12, aiohttp, aiosqlite, pydantic v2 + pydantic-settings, structlog, pytest (asyncio_mode=auto), aioresponses, freezegun. Hatchling build (`packages` list in pyproject.toml).

**Spec:** `docs/superpowers/specs/2026-10-09-gecko-lean-hermes-agent-design.md`

## Hermes-first analysis

See spec §"Hermes-first analysis". Every task in this plan is in the spec's net-new set (datastore, deterministic features/ranking, ledger, backtest); scheduling and delivery are Hermes-owned and belong to Plan 3. Verdict: extends-Hermes.

## Global Constraints

- Chains: `solana`, `base`, `ethereum`, `bsc` only (spec §2).
- Universe: liquidity ≥ $250,000 for ≥ 24 h; market cap $500,000–$200,000,000 for listed coins (spec §5).
- No LLM in any data write path; the agent writes only `research_notes` (spec §2, §13).
- `gecko/` ≤ 2,500 LOC excluding tests; `tests/gecko` ≤ 5,000 LOC (spec §1 S1, §15).
- CoinGecko Demo only: base `https://api.coingecko.com/api/v3`, header `x-cg-demo-api-key`, hard cap 8,000 calls/month enforced from `runs.credits_used` (spec §5). Never call `/coins/top_gainers_losers` or `/coins/list/new` (paid-only).
- Token identity = `token_id = f"{chain}:{address.lower()}"`; CoinGecko ids live in `tokens.cg_id` only (spec §4).
- Every Telegram send uses `parse_mode=None` (spec §13).
- Async everywhere; aiohttp session passed as a parameter; no `os.getenv` in business logic; thresholds from `GeckoSettings` (project CLAUDE.md).
- Point-in-time: a feature computed `as_of=T` must never read an observation with `observed_at > T` (spec §6).
- Commit after every task; conventional commit messages ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Run tests with `python -m pytest tests/gecko -q` locally (system Python; `uv run` TLS-fails on this Windows box); CI runs `uv run pytest`.

## Review Focus

1. A DexScreener pair with `priceUsd` missing or `liquidity` null must be skipped, not crash the whole run (Task 5 test `test_pair_missing_fields_skipped`).
2. A token seen with liquidity ≥ $250K once and then < $250K must not be admitted; admission requires two qualifying observations ≥ 24 h apart (Task 4 test `test_admit_requires_24h_survival`).
3. A CoinGecko 429 mid-sweep must back off and resume, and a 401 (deactivated key) must fail the run visibly rather than write an empty snapshot (Task 7 tests `test_markets_429_then_200`, `test_markets_401_fails_run`).
4. The ledger must use the FIRST post-entry observation price, never a later one, and a token with no price for 3 attempts over 3 days is a −100% return (Task 10 tests `test_entry_price_is_first_post_entry`, `test_missing_price_counts_as_total_loss`).
5. `vol_7d_ratio` must be NULL (feature-missing) when fewer than 14 daily candles exist, and F4 must then NOT admit (Task 8 test `test_f4_rejects_when_ratio_missing`).

## File Structure

```
gecko/
  __init__.py          version string only
  __main__.py          CLI: python -m gecko <job> [--as-of ISO] → exit code
  settings.py          GeckoSettings (pydantic-settings, env prefix GECKO_)
  store.py             SCHEMA DDL + Store (aiosqlite) with typed helpers
  runs.py              run_job(): wraps a job, writes runs row, maps exceptions → exit 1
  notify.py            send_telegram(session, token, chat_id, text) plain text
  freshness.py         per-table SLO check + 6 h de-dup → notify
  http.py              get_json(session, url, *, headers, params, retries) with 429/5xx backoff
  universe.py          in_band(), admission state machine (survivor rule)
  collectors/
    __init__.py
    dexscreener.py     boosts + profiles → tokens/pairs → observations
    geckoterminal.py   new_pools + trending_pools + daily OHLCV → observations
    coingecko.py       markets sweep (daily), trending (hourly), simple/price (hourly)
  features.py          compute_features(store, token_id, as_of) → Features
  rank.py              disqualifiers, F4 rule, score, watchlist upsert/exit
  ledger.py            schedule horizons, measure due returns
  backtest/
    __init__.py
    study.py           loads /root/xs_study formats; reproduces the 09-18 cohort
    runner.py          applies a rule as-of each day; reports the 5-criteria battery
tests/gecko/
  conftest.py          store fixture (tmp_path), settings fixture, session fixture
  test_settings.py test_store.py test_runs.py test_freshness.py test_http.py
  test_universe.py test_dexscreener.py test_geckoterminal.py test_coingecko.py
  test_features.py test_rank.py test_ledger.py test_backtest.py test_cli.py
```

---

### Task 1: Package skeleton, settings, CLI dispatcher

**Files:**
- Create: `gecko/__init__.py`, `gecko/settings.py`, `gecko/__main__.py`
- Modify: `pyproject.toml` (`[tool.hatch.build.targets.wheel] packages = ["scout", "dashboard", "gecko"]`)
- Test: `tests/gecko/conftest.py`, `tests/gecko/test_settings.py`, `tests/gecko/test_cli.py`

**Interfaces:**
- Produces: `GeckoSettings` (fields below), `gecko.__main__.main(argv: list[str]) -> int`, `JOBS: dict[str, Callable[[GeckoSettings, argparse.Namespace], Awaitable[int]]]` registry that later tasks append to.

- [ ] **Step 1: Write failing tests**

`tests/gecko/conftest.py`:
```python
import pytest
from gecko.settings import GeckoSettings


@pytest.fixture
def settings(tmp_path) -> GeckoSettings:
    return GeckoSettings(db_path=str(tmp_path / "gecko.db"), _env_file=None)
```

`tests/gecko/test_settings.py`:
```python
from gecko.settings import GeckoSettings


def test_defaults(tmp_path):
    s = GeckoSettings(db_path=str(tmp_path / "g.db"), _env_file=None)
    assert s.chains == ["solana", "base", "ethereum", "bsc"]
    assert s.min_liquidity_usd == 250_000
    assert s.min_mcap_usd == 500_000
    assert s.max_mcap_usd == 200_000_000
    assert s.survivor_hours == 24
    assert s.watchlist_cap == 50
    assert s.f4_max_age_days == 90
    assert s.f4_min_vol_ratio == 2.0
    assert s.coingecko_monthly_cap == 8000
    assert s.coingecko_demo_api_key is None
    assert s.http_timeout_sec == 20


def test_env_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("GECKO_MIN_LIQUIDITY_USD", "100000")
    s = GeckoSettings(db_path=str(tmp_path / "g.db"), _env_file=None)
    assert s.min_liquidity_usd == 100_000


def test_chain_validation(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        GeckoSettings(db_path=str(tmp_path / "g.db"), chains=["tron"], _env_file=None)
```

`tests/gecko/test_cli.py`:
```python
from gecko.__main__ import main


def test_unknown_job_exits_2(capsys):
    assert main(["no-such-job"]) == 2
    assert "unknown job" in capsys.readouterr().err


def test_list_jobs(capsys):
    assert main(["--list"]) == 0
    assert "freshness" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests, expect ImportError**

Run: `python -m pytest tests/gecko -q`
Expected: FAIL `ModuleNotFoundError: No module named 'gecko'`

- [ ] **Step 3: Implement**

`gecko/__init__.py`:
```python
__version__ = "0.1.0"
```

`gecko/settings.py`:
```python
from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_CHAINS = ("solana", "base", "ethereum", "bsc")


class GeckoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GECKO_", env_file=".env", extra="ignore")

    db_path: str = "gecko.db"
    chains: list[str] = list(SUPPORTED_CHAINS)
    min_liquidity_usd: float = 250_000
    min_mcap_usd: float = 500_000
    max_mcap_usd: float = 200_000_000
    survivor_hours: int = 24
    watchlist_cap: int = 50
    watchlist_max_age_days: int = 180
    f4_max_age_days: int = 90
    f4_min_vol_ratio: float = 2.0
    top10_disqualify_pct: float = 60.0
    dead_after_days: int = 7
    http_timeout_sec: float = 20
    coingecko_demo_api_key: SecretStr | None = None
    coingecko_monthly_cap: int = 8000
    telegram_bot_token: SecretStr | None = None
    telegram_health_chat_id: str | None = None
    young_chain_launch_dates: dict[str, str] = {}  # chain -> ISO date

    @field_validator("chains")
    @classmethod
    def _chains_supported(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in SUPPORTED_CHAINS]
        if bad:
            raise ValueError(f"unsupported chains: {bad}")
        return v
```

`gecko/__main__.py`:
```python
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from gecko.settings import GeckoSettings

JobFn = Callable[[GeckoSettings, argparse.Namespace], Awaitable[int]]
JOBS: dict[str, JobFn] = {}


def register(name: str):
    def deco(fn: JobFn) -> JobFn:
        JOBS[name] = fn
        return fn
    return deco


def _load_jobs() -> None:
    # Importing registers jobs; later tasks add modules here.
    import gecko.freshness  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    _load_jobs()
    p = argparse.ArgumentParser(prog="gecko")
    p.add_argument("job", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--as-of", default=None, help="ISO-8601 UTC override for point-in-time jobs")
    p.add_argument("--rule", default="F4")
    p.add_argument("--study-dir", default="/root/xs_study")
    p.add_argument("--history-db", default="/root/gecko-history/scout.db")
    ns = p.parse_args(argv)
    if ns.list:
        print("\n".join(sorted(JOBS)))
        return 0
    if ns.job not in JOBS:
        print(f"unknown job: {ns.job!r}; use --list", file=sys.stderr)
        return 2
    settings = GeckoSettings()
    return asyncio.run(JOBS[ns.job](settings, ns))


if __name__ == "__main__":
    sys.exit(main())
```

Create `gecko/freshness.py` as a stub for now so `_load_jobs` imports (Task 3 fills it):
```python
from gecko.__main__ import register


@register("freshness")
async def freshness_job(settings, ns) -> int:  # replaced in Task 3
    return 0
```

Add `"gecko"` to the hatch packages list in `pyproject.toml`.

- [ ] **Step 4: Run tests, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add gecko tests/gecko pyproject.toml
git commit -m "feat(gecko): package skeleton, settings and CLI dispatcher"
```

---

### Task 2: Store schema and helpers

**Files:**
- Create: `gecko/store.py`
- Test: `tests/gecko/test_store.py`; extend `tests/gecko/conftest.py` with a `store` fixture

**Interfaces:**
- Produces:
  - `SCHEMA: str` (DDL for 7 tables + indexes)
  - `class Store`: `@classmethod async open(path: str) -> Store`; `async close()`; `conn: aiosqlite.Connection`
  - `async upsert_token(token_id, chain, address, symbol, name, first_seen_at, first_source, cg_id=None, pool_created_at=None) -> None` (INSERT OR IGNORE; later calls only fill NULL `cg_id`/`pool_created_at`)
  - `async set_token_status(token_id, status, reason=None)`
  - `async add_observation(token_id, source, observed_at, features: dict) -> int`
  - `async observations(token_id, *, as_of: str, source: str | None = None, limit: int = 500) -> list[Observation]` ordered by observed_at DESC, only `observed_at <= as_of`
  - `async record_run(job, started_at, finished_at, ok, rows_written=0, credits_used=0, error=None) -> int`
  - `async last_write(table) -> str | None` (max of the table's time column)
  - `async watchlist_upsert(token_id, entered_at, rule_id, rank, score, features_at_entry: dict)` (entry row immutable: later calls only update `rank`, `score`, `last_ranked_at`)
  - `async watchlist_exit(token_id, exited_at, reason)`
  - `async active_watchlist() -> list[WatchRow]`
  - `async schedule_returns(token_id, entered_at, horizons=(1,7,30,90,180))`
  - `async due_returns(now) -> list[ReturnRow]` where `measured_at IS NULL AND due_at <= now`
  - `async set_return(token_id, horizon_days, entry_price, price, btc_entry, btc_price, measured_at)`
  - dataclasses `Observation(token_id, source, observed_at, features: dict)`, `WatchRow`, `ReturnRow`.

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_store.py`:
```python
import json
import pytest
from gecko.store import Store, SCHEMA


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "g.db"))
    yield s
    await s.close()


async def test_schema_has_seven_tables(store):
    rows = await (await store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )).fetchall()
    assert {r[0] for r in rows} == {
        "tokens", "observations", "source_calls", "watchlist",
        "forward_returns", "research_notes", "runs",
    }


async def test_upsert_token_is_idempotent_and_fills_nulls(store):
    await store.upsert_token("solana:abc", "solana", "ABC", "ABC", "Abc", "2026-10-01T00:00:00Z", "dexscreener")
    await store.upsert_token("solana:abc", "solana", "ABC", "ABC2", "Abc2", "2026-10-02T00:00:00Z", "geckoterminal", cg_id="abc-coin")
    row = await (await store.conn.execute("SELECT symbol, first_source, cg_id FROM tokens")).fetchone()
    assert row == ("ABC", "dexscreener", "abc-coin")


async def test_observations_respect_as_of(store):
    await store.upsert_token("solana:abc", "solana", "ABC", "ABC", "Abc", "2026-10-01T00:00:00Z", "dexscreener")
    await store.add_observation("solana:abc", "dexscreener", "2026-10-01T00:00:00Z", {"price": 1.0})
    await store.add_observation("solana:abc", "dexscreener", "2026-10-03T00:00:00Z", {"price": 9.0})
    obs = await store.observations("solana:abc", as_of="2026-10-02T00:00:00Z")
    assert [o.features["price"] for o in obs] == [1.0]


async def test_watchlist_entry_row_is_immutable(store):
    await store.upsert_token("solana:abc", "solana", "ABC", "ABC", "Abc", "2026-10-01T00:00:00Z", "dexscreener")
    await store.watchlist_upsert("solana:abc", "2026-10-01T00:00:00Z", "F4", 1, 2.5, {"age_days": 10})
    await store.watchlist_upsert("solana:abc", "2026-10-05T00:00:00Z", "F4", 3, 1.0, {"age_days": 14})
    row = await (await store.conn.execute(
        "SELECT entered_at, rank, score, features_at_entry_json FROM watchlist")).fetchone()
    assert row[0] == "2026-10-01T00:00:00Z" and row[1] == 3 and row[2] == 1.0
    assert json.loads(row[3]) == {"age_days": 10}


async def test_schedule_and_due_returns(store):
    await store.upsert_token("solana:abc", "solana", "ABC", "ABC", "Abc", "2026-10-01T00:00:00Z", "dexscreener")
    await store.schedule_returns("solana:abc", "2026-10-01T00:00:00Z")
    due = await store.due_returns("2026-10-09T00:00:00Z")
    assert [r.horizon_days for r in due] == [1, 7]
    await store.set_return("solana:abc", 1, 1.0, 1.5, 60000.0, 61000.0, "2026-10-02T01:00:00Z")
    due = await store.due_returns("2026-10-09T00:00:00Z")
    assert [r.horizon_days for r in due] == [7]


async def test_last_write(store):
    assert await store.last_write("observations") is None
    await store.record_run("x", "2026-10-01T00:00:00Z", "2026-10-01T00:00:05Z", True)
    assert await store.last_write("runs") == "2026-10-01T00:00:05Z"
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_store.py -q`
Expected: FAIL `ImportError: cannot import name 'Store'`

- [ ] **Step 3: Implement `gecko/store.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  token_id TEXT PRIMARY KEY, chain TEXT NOT NULL, address TEXT NOT NULL,
  symbol TEXT NOT NULL, name TEXT NOT NULL, cg_id TEXT, first_seen_at TEXT NOT NULL,
  first_source TEXT NOT NULL, pool_created_at TEXT, deployer TEXT,
  status TEXT NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate','active','disqualified','dead')),
  disqualify_reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT NOT NULL, source TEXT NOT NULL,
  observed_at TEXT NOT NULL, features_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_obs_token_time ON observations(token_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(observed_at);
CREATE TABLE IF NOT EXISTS source_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT, source_type TEXT NOT NULL
    CHECK (source_type IN ('telegram','x')), source_id TEXT NOT NULL,
  event_id TEXT NOT NULL UNIQUE, posted_at TEXT NOT NULL, text_hash TEXT NOT NULL,
  cashtags_json TEXT NOT NULL DEFAULT '[]', addresses_json TEXT NOT NULL DEFAULT '[]',
  resolved_at TEXT);
CREATE INDEX IF NOT EXISTS idx_calls_token ON source_calls(token_id, posted_at);
CREATE TABLE IF NOT EXISTS watchlist (
  token_id TEXT PRIMARY KEY, entered_at TEXT NOT NULL, rule_id TEXT NOT NULL,
  rank INTEGER NOT NULL, score REAL NOT NULL, features_at_entry_json TEXT NOT NULL,
  last_ranked_at TEXT NOT NULL, exited_at TEXT, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS forward_returns (
  token_id TEXT NOT NULL, horizon_days INTEGER NOT NULL, due_at TEXT NOT NULL,
  entry_price REAL, price REAL, btc_entry REAL, btc_price REAL, ret REAL, ret_vs_btc REAL,
  attempts INTEGER NOT NULL DEFAULT 0, measured_at TEXT,
  PRIMARY KEY (token_id, horizon_days));
CREATE TABLE IF NOT EXISTS research_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT NOT NULL, created_at TEXT NOT NULL,
  model TEXT NOT NULL, schema_version INTEGER NOT NULL, note_json TEXT NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL, ok INTEGER NOT NULL, rows_written INTEGER NOT NULL DEFAULT 0,
  credits_used INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS idx_runs_job_time ON runs(job, finished_at);
"""

TIME_COLUMN = {
    "tokens": "updated_at", "observations": "observed_at", "source_calls": "posted_at",
    "watchlist": "last_ranked_at", "forward_returns": "measured_at",
    "research_notes": "created_at", "runs": "finished_at",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_plus(ts: str, days: int) -> str:
    base = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Observation:
    token_id: str
    source: str
    observed_at: str
    features: dict


@dataclass
class WatchRow:
    token_id: str
    entered_at: str
    rule_id: str
    rank: int
    score: float
    features_at_entry: dict


@dataclass
class ReturnRow:
    token_id: str
    horizon_days: int
    due_at: str
    attempts: int


class Store:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "Store":
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def upsert_token(self, token_id, chain, address, symbol, name, first_seen_at,
                           first_source, cg_id=None, pool_created_at=None) -> None:
        await self.conn.execute(
            "INSERT INTO tokens(token_id,chain,address,symbol,name,cg_id,first_seen_at,"
            "first_source,pool_created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(token_id) DO UPDATE SET "
            "cg_id=COALESCE(tokens.cg_id, excluded.cg_id), "
            "pool_created_at=COALESCE(tokens.pool_created_at, excluded.pool_created_at), "
            "updated_at=excluded.updated_at",
            (token_id, chain, address, symbol, name, cg_id, first_seen_at, first_source,
             pool_created_at, utcnow()))
        await self.conn.commit()

    async def set_token_status(self, token_id, status, reason=None) -> None:
        await self.conn.execute(
            "UPDATE tokens SET status=?, disqualify_reason=?, updated_at=? WHERE token_id=?",
            (status, reason, utcnow(), token_id))
        await self.conn.commit()

    async def add_observation(self, token_id, source, observed_at, features: dict) -> int:
        cur = await self.conn.execute(
            "INSERT INTO observations(token_id,source,observed_at,features_json) VALUES(?,?,?,?)",
            (token_id, source, observed_at, json.dumps(features, sort_keys=True)))
        await self.conn.commit()
        return cur.lastrowid

    async def observations(self, token_id, *, as_of: str, source: str | None = None,
                           limit: int = 500) -> list[Observation]:
        sql = ("SELECT token_id,source,observed_at,features_json FROM observations "
               "WHERE token_id=? AND observed_at<=?")
        args: list = [token_id, as_of]
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        args.append(limit)
        rows = await (await self.conn.execute(sql, args)).fetchall()
        return [Observation(r[0], r[1], r[2], json.loads(r[3])) for r in rows]

    async def record_run(self, job, started_at, finished_at, ok, rows_written=0,
                         credits_used=0, error=None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO runs(job,started_at,finished_at,ok,rows_written,credits_used,error) "
            "VALUES(?,?,?,?,?,?,?)",
            (job, started_at, finished_at, 1 if ok else 0, rows_written, credits_used, error))
        await self.conn.commit()
        return cur.lastrowid

    async def last_write(self, table: str) -> str | None:
        col = TIME_COLUMN[table]
        row = await (await self.conn.execute(f"SELECT MAX({col}) FROM {table}")).fetchone()
        return row[0]

    async def watchlist_upsert(self, token_id, entered_at, rule_id, rank, score,
                               features_at_entry: dict) -> None:
        await self.conn.execute(
            "INSERT INTO watchlist(token_id,entered_at,rule_id,rank,score,"
            "features_at_entry_json,last_ranked_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(token_id) DO UPDATE SET rank=excluded.rank, score=excluded.score, "
            "last_ranked_at=excluded.last_ranked_at",
            (token_id, entered_at, rule_id, rank, score,
             json.dumps(features_at_entry, sort_keys=True), entered_at))
        await self.conn.commit()

    async def watchlist_exit(self, token_id, exited_at, reason) -> None:
        await self.conn.execute(
            "UPDATE watchlist SET exited_at=?, exit_reason=? WHERE token_id=? AND exited_at IS NULL",
            (exited_at, reason, token_id))
        await self.conn.commit()

    async def active_watchlist(self) -> list[WatchRow]:
        rows = await (await self.conn.execute(
            "SELECT token_id,entered_at,rule_id,rank,score,features_at_entry_json "
            "FROM watchlist WHERE exited_at IS NULL ORDER BY rank")).fetchall()
        return [WatchRow(r[0], r[1], r[2], r[3], r[4], json.loads(r[5])) for r in rows]

    async def schedule_returns(self, token_id, entered_at, horizons=(1, 7, 30, 90, 180)) -> None:
        await self.conn.executemany(
            "INSERT OR IGNORE INTO forward_returns(token_id,horizon_days,due_at) VALUES(?,?,?)",
            [(token_id, h, iso_plus(entered_at, h)) for h in horizons])
        await self.conn.commit()

    async def due_returns(self, now: str) -> list[ReturnRow]:
        rows = await (await self.conn.execute(
            "SELECT token_id,horizon_days,due_at,attempts FROM forward_returns "
            "WHERE measured_at IS NULL AND due_at<=? ORDER BY due_at, horizon_days", (now,))).fetchall()
        return [ReturnRow(*r) for r in rows]

    async def set_return(self, token_id, horizon_days, entry_price, price, btc_entry,
                         btc_price, measured_at) -> None:
        ret = (price / entry_price - 1.0) if entry_price and price is not None else None
        btc_ret = (btc_price / btc_entry - 1.0) if btc_entry and btc_price is not None else None
        ret_vs_btc = (ret - btc_ret) if ret is not None and btc_ret is not None else None
        await self.conn.execute(
            "UPDATE forward_returns SET entry_price=?,price=?,btc_entry=?,btc_price=?,ret=?,"
            "ret_vs_btc=?,measured_at=? WHERE token_id=? AND horizon_days=?",
            (entry_price, price, btc_entry, btc_price, ret, ret_vs_btc, measured_at,
             token_id, horizon_days))
        await self.conn.commit()

    async def bump_return_attempt(self, token_id, horizon_days) -> int:
        await self.conn.execute(
            "UPDATE forward_returns SET attempts=attempts+1 WHERE token_id=? AND horizon_days=?",
            (token_id, horizon_days))
        await self.conn.commit()
        row = await (await self.conn.execute(
            "SELECT attempts FROM forward_returns WHERE token_id=? AND horizon_days=?",
            (token_id, horizon_days))).fetchone()
        return row[0]
```

Add to `tests/gecko/conftest.py`:
```python
from gecko.store import Store


@pytest.fixture
async def store(settings):
    s = await Store.open(settings.db_path)
    yield s
    await s.close()
```
(and delete the local `store` fixture from `test_store.py` so the shared one is used).

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add gecko/store.py tests/gecko
git commit -m "feat(gecko): seven-table store with point-in-time observation reads"
```

---

### Task 3: Job runner, Telegram notify, freshness check

**Files:**
- Create: `gecko/runs.py`, `gecko/notify.py`; replace stub `gecko/freshness.py`
- Test: `tests/gecko/test_runs.py`, `tests/gecko/test_freshness.py`

**Interfaces:**
- Consumes: `Store.record_run`, `Store.last_write`, `GeckoSettings`.
- Produces:
  - `async run_job(name: str, settings: GeckoSettings, fn: Callable[[Store, aiohttp.ClientSession], Awaitable[JobResult]]) -> int` — opens store + session, calls `fn`, records run, returns 0 on success / 1 on exception; `JobResult = dataclass(rows_written: int = 0, credits_used: int = 0)`.
  - `async send_telegram(session, token: str, chat_id: str, text: str) -> bool` — POST `https://api.telegram.org/bot{token}/sendMessage` with `{"chat_id", "text"}` and NO `parse_mode`; returns `ok`.
  - `SLO_MINUTES: dict[str, int] = {"observations": 30, "source_calls": 360, "watchlist": 120, "forward_returns": 1560, "research_notes": 1560}`
  - `async check_freshness(store, now: str) -> list[Breach]` with `Breach(table, last_write, slo_minutes)`; a table that has never been written counts as a breach only if `runs` shows a successful run of a job that writes it (otherwise system is bootstrapping).
  - `async freshness_job(settings, ns) -> int` registered as `freshness`: alerts via Telegram once per table per 6 h (de-dup by a `runs` row `job='freshness-alert:<table>'`).

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_runs.py`:
```python
from gecko.runs import run_job, JobResult
from gecko.store import Store


async def test_run_job_records_success(settings):
    async def ok(store, session):
        return JobResult(rows_written=3, credits_used=2)
    assert await run_job("t", settings, ok) == 0
    s = await Store.open(settings.db_path)
    row = await (await s.conn.execute("SELECT job, ok, rows_written, credits_used FROM runs")).fetchone()
    await s.close()
    assert row == ("t", 1, 3, 2)


async def test_run_job_records_failure_and_exits_1(settings):
    async def boom(store, session):
        raise RuntimeError("upstream 401")
    assert await run_job("t", settings, boom) == 1
    s = await Store.open(settings.db_path)
    row = await (await s.conn.execute("SELECT ok, error FROM runs")).fetchone()
    await s.close()
    assert row[0] == 0 and "upstream 401" in row[1]
```

`tests/gecko/test_freshness.py`:
```python
from aioresponses import aioresponses
from gecko.freshness import check_freshness, SLO_MINUTES, alert_breaches


async def test_fresh_table_no_breach(store):
    await store.record_run("dexscreener", "2026-10-09T00:00:00Z", "2026-10-09T00:00:10Z", True, rows_written=1)
    await store.upsert_token("solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener")
    await store.add_observation("solana:a", "dexscreener", "2026-10-09T00:00:00Z", {"price": 1})
    assert await check_freshness(store, "2026-10-09T00:20:00Z") == []


async def test_stale_table_breaches(store):
    await store.record_run("dexscreener", "2026-10-09T00:00:00Z", "2026-10-09T00:00:10Z", True, rows_written=1)
    await store.upsert_token("solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener")
    await store.add_observation("solana:a", "dexscreener", "2026-10-09T00:00:00Z", {"price": 1})
    b = await check_freshness(store, "2026-10-09T01:00:00Z")
    assert [x.table for x in b] == ["observations"]
    assert b[0].slo_minutes == SLO_MINUTES["observations"]


async def test_never_written_table_is_not_breach_while_bootstrapping(store):
    assert await check_freshness(store, "2026-10-09T01:00:00Z") == []


async def test_alert_dedup_6h(store, settings):
    from pydantic import SecretStr
    settings.telegram_bot_token = SecretStr("tok")
    settings.telegram_health_chat_id = "1"
    await store.record_run("dexscreener", "2026-10-09T00:00:00Z", "2026-10-09T00:00:10Z", True, rows_written=1)
    await store.upsert_token("solana:a", "solana", "A", "A", "A", "2026-10-09T00:00:00Z", "dexscreener")
    await store.add_observation("solana:a", "dexscreener", "2026-10-09T00:00:00Z", {"price": 1})
    import aiohttp
    with aioresponses() as m:
        m.post("https://api.telegram.org/bottok/sendMessage", payload={"ok": True})
        m.post("https://api.telegram.org/bottok/sendMessage", payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            n1 = await alert_breaches(store, settings, session, "2026-10-09T01:00:00Z")
            n2 = await alert_breaches(store, settings, session, "2026-10-09T02:00:00Z")
            n3 = await alert_breaches(store, settings, session, "2026-10-09T07:30:00Z")
    assert (n1, n2, n3) == (1, 0, 1)
    sent = [c for c in m.requests if c[0] == "POST"]
    assert len(sent) == 1  # one URL key; check call count below
    assert len(m.requests[sent[0]]) == 2
    body = m.requests[sent[0]][0].kwargs["json"]
    assert "parse_mode" not in body and "observations" in body["text"]
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_runs.py tests/gecko/test_freshness.py -q`
Expected: FAIL `ImportError`

- [ ] **Step 3: Implement**

`gecko/runs.py`:
```python
from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp
import structlog

from gecko.settings import GeckoSettings
from gecko.store import Store, utcnow

log = structlog.get_logger("gecko.runs")


@dataclass
class JobResult:
    rows_written: int = 0
    credits_used: int = 0


JobBody = Callable[[Store, aiohttp.ClientSession], Awaitable[JobResult]]


async def run_job(name: str, settings: GeckoSettings, fn: JobBody) -> int:
    started = utcnow()
    store = await Store.open(settings.db_path)
    timeout = aiohttp.ClientTimeout(total=settings.http_timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                result = await fn(store, session)
            except Exception as exc:  # noqa: BLE001 — recorded, then re-raised as exit 1
                err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
                await store.record_run(name, started, utcnow(), False, error=err)
                log.error("job_failed", job=name, error=str(exc))
                return 1
        await store.record_run(name, started, utcnow(), True,
                               rows_written=result.rows_written, credits_used=result.credits_used)
        log.info("job_ok", job=name, rows=result.rows_written, credits=result.credits_used)
        return 0
    finally:
        await store.close()
```

`gecko/notify.py`:
```python
from __future__ import annotations

import aiohttp
import structlog

log = structlog.get_logger("gecko.notify")


async def send_telegram(session: aiohttp.ClientSession, token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with session.post(url, json={"chat_id": chat_id, "text": text[:4000]}) as resp:
        body = await resp.json(content_type=None)
        ok = bool(body.get("ok")) and resp.status == 200
        log.info("telegram_sent" if ok else "telegram_failed", status=resp.status)
        return ok
```

`gecko/freshness.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from gecko.__main__ import register
from gecko.notify import send_telegram
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store

SLO_MINUTES = {"observations": 30, "source_calls": 360, "watchlist": 120,
               "forward_returns": 1560, "research_notes": 1560}
# which job writes which table; a table is "bootstrapping" until its writer has one ok run
WRITER_JOBS = {"observations": ("dexscreener", "geckoterminal", "coingecko-markets",
                                "coingecko-hourly", "enrich"),
               "source_calls": ("telegram-reader", "xkol"), "watchlist": ("rank",),
               "forward_returns": ("ledger",), "research_notes": ("research",)}
DEDUP_HOURS = 6


@dataclass
class Breach:
    table: str
    last_write: str | None
    slo_minutes: int


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def _writer_has_run(store: Store, table: str) -> bool:
    q = ",".join("?" * len(WRITER_JOBS[table]))
    row = await (await store.conn.execute(
        f"SELECT 1 FROM runs WHERE ok=1 AND job IN ({q}) LIMIT 1", WRITER_JOBS[table])).fetchone()
    return row is not None


async def check_freshness(store: Store, now: str) -> list[Breach]:
    out: list[Breach] = []
    now_dt = _parse(now)
    for table, slo in SLO_MINUTES.items():
        if not await _writer_has_run(store, table):
            continue
        last = await store.last_write(table)
        if last is None or now_dt - _parse(last) > timedelta(minutes=slo):
            out.append(Breach(table, last, slo))
    return out


async def _recently_alerted(store: Store, table: str, now: str) -> bool:
    cutoff = (_parse(now) - timedelta(hours=DEDUP_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = await (await store.conn.execute(
        "SELECT 1 FROM runs WHERE job=? AND finished_at>? LIMIT 1",
        (f"freshness-alert:{table}", cutoff))).fetchone()
    return row is not None


async def alert_breaches(store: Store, settings: GeckoSettings, session: aiohttp.ClientSession,
                         now: str) -> int:
    sent = 0
    for b in await check_freshness(store, now):
        if await _recently_alerted(store, b.table, now):
            continue
        text = (f"gecko freshness breach: table {b.table} last write {b.last_write or 'never'} "
                f"exceeds SLO {b.slo_minutes} min (checked {now})")
        if settings.telegram_bot_token and settings.telegram_health_chat_id:
            await send_telegram(session, settings.telegram_bot_token.get_secret_value(),
                                settings.telegram_health_chat_id, text)
        await store.record_run(f"freshness-alert:{b.table}", now, now, True)
        sent += 1
    return sent


@register("freshness")
async def freshness_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        n = await alert_breaches(store, settings, session, now)
        return JobResult(rows_written=n)

    return await run_job("freshness", settings, body)
```

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add gecko/runs.py gecko/notify.py gecko/freshness.py tests/gecko
git commit -m "feat(gecko): job runner with run ledger, plain-text Telegram notify, per-table freshness SLO"
```

---
### Task 4: HTTP helper with backoff, universe band, survivor admission

**Files:**
- Create: `gecko/http.py`, `gecko/universe.py`
- Test: `tests/gecko/test_http.py`, `tests/gecko/test_universe.py`

**Interfaces:**
- Produces:
  - `class UpstreamError(Exception)` with `.status`.
  - `async get_json(session, url, *, settings, headers=None, params=None, ok_statuses=(200,), retries=3, backoff=(2, 4, 8), sleep=asyncio.sleep) -> Any` — on 429 or 5xx sleeps `backoff[i]` seconds and retries; on any other non-ok status raises `UpstreamError(status)`; after `retries` exhausted raises `UpstreamError(last_status)`; returns parsed JSON (`content_type=None`).
  - `in_band(settings, *, mcap_usd: float | None, liquidity_usd: float | None, chain: str) -> bool` — chain in `settings.chains`; liquidity ≥ `min_liquidity_usd`; when `mcap_usd` is known it must be in `[min_mcap_usd, max_mcap_usd]`; unknown mcap is allowed (DEX-born tokens often lack it).
  - `async admit_survivors(store, settings, now: str) -> int` — promotes `tokens.status='candidate'` → `'active'` when there exist two market observations with `liquidity_usd ≥ min_liquidity_usd` whose `observed_at` differ by ≥ `survivor_hours`, the later one within the last 48 h; returns count promoted. Collectors insert new tokens as `candidate`; `rank` (Task 9) considers only `active`.

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_http.py`:
```python
import aiohttp
import pytest
from aioresponses import aioresponses
from gecko.http import get_json, UpstreamError

URL = "https://example.test/x"


async def test_429_then_200_with_backoff(settings):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    with aioresponses() as m:
        m.get(URL, status=429)
        m.get(URL, payload={"ok": 1})
        async with aiohttp.ClientSession() as s:
            out = await get_json(s, URL, settings=settings, sleep=fake_sleep)
    assert out == {"ok": 1} and slept == [2]


async def test_401_raises_immediately(settings):
    with aioresponses() as m:
        m.get(URL, status=401)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(UpstreamError) as ei:
                await get_json(s, URL, settings=settings, sleep=lambda s: None)
    assert ei.value.status == 401


async def test_retries_exhausted(settings):
    async def nosleep(s):
        pass
    with aioresponses() as m:
        for _ in range(4):
            m.get(URL, status=503)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(UpstreamError) as ei:
                await get_json(s, URL, settings=settings, sleep=nosleep)
    assert ei.value.status == 503


async def test_ok_statuses_passthrough(settings):
    with aioresponses() as m:
        m.get(URL, status=404, payload={"error": "nf"})
        async with aiohttp.ClientSession() as s:
            assert await get_json(s, URL, settings=settings, ok_statuses=(200, 404)) == {"error": "nf"}
```

`tests/gecko/test_universe.py`:
```python
from gecko.universe import in_band, admit_survivors


def test_in_band(settings):
    assert in_band(settings, mcap_usd=1e6, liquidity_usd=300_000, chain="solana")
    assert in_band(settings, mcap_usd=None, liquidity_usd=300_000, chain="base")
    assert not in_band(settings, mcap_usd=1e6, liquidity_usd=100, chain="solana")
    assert not in_band(settings, mcap_usd=1e9, liquidity_usd=300_000, chain="solana")
    assert not in_band(settings, mcap_usd=1e6, liquidity_usd=300_000, chain="tron")


async def seed(store):
    await store.upsert_token("solana:a", "solana", "a", "A", "A", "2026-10-01T00:00:00Z", "dexscreener")


async def test_admit_requires_24h_survival(store, settings):
    await seed(store)
    await store.add_observation("solana:a", "dexscreener", "2026-10-01T00:00:00Z", {"liquidity_usd": 300_000})
    await store.add_observation("solana:a", "dexscreener", "2026-10-01T12:00:00Z", {"liquidity_usd": 300_000})
    assert await admit_survivors(store, settings, "2026-10-01T13:00:00Z") == 0
    await store.add_observation("solana:a", "dexscreener", "2026-10-02T01:00:00Z", {"liquidity_usd": 1_000})
    assert await admit_survivors(store, settings, "2026-10-02T02:00:00Z") == 0  # later obs below floor
    await store.add_observation("solana:a", "dexscreener", "2026-10-02T03:00:00Z", {"liquidity_usd": 300_000})
    assert await admit_survivors(store, settings, "2026-10-02T04:00:00Z") == 1
    row = await (await store.conn.execute("SELECT status FROM tokens WHERE token_id='solana:a'")).fetchone()
    assert row[0] == "active"
    assert await admit_survivors(store, settings, "2026-10-02T05:00:00Z") == 0  # idempotent
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_http.py tests/gecko/test_universe.py -q`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`gecko/http.py`:
```python
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import structlog

from gecko.settings import GeckoSettings

log = structlog.get_logger("gecko.http")
RETRY_STATUSES = {429, 500, 502, 503, 504}


class UpstreamError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"upstream {status} for {url}")
        self.status = status
        self.url = url


async def get_json(session: aiohttp.ClientSession, url: str, *, settings: GeckoSettings,
                   headers: dict | None = None, params: dict | None = None,
                   ok_statuses: tuple[int, ...] = (200,), retries: int = 3,
                   backoff: tuple[float, ...] = (2, 4, 8), sleep=asyncio.sleep) -> Any:
    last = 0
    for attempt in range(retries + 1):
        async with session.get(url, headers=headers, params=params,
                               timeout=aiohttp.ClientTimeout(total=settings.http_timeout_sec)) as resp:
            last = resp.status
            if resp.status in ok_statuses:
                text = await resp.text()
                return json.loads(text) if text.strip() else None
            if resp.status not in RETRY_STATUSES:
                raise UpstreamError(resp.status, url)
        if attempt < retries:
            delay = backoff[min(attempt, len(backoff) - 1)]
            log.warning("http_retry", url=url, status=last, delay=delay)
            await sleep(delay)
    raise UpstreamError(last, url)
```

`gecko/universe.py`:
```python
from __future__ import annotations

import json
from datetime import datetime, timedelta

from gecko.settings import GeckoSettings
from gecko.store import Store

MARKET_SOURCES = ("dexscreener", "geckoterminal", "coingecko")


def in_band(settings: GeckoSettings, *, mcap_usd: float | None, liquidity_usd: float | None,
            chain: str) -> bool:
    if chain not in settings.chains:
        return False
    if liquidity_usd is None or liquidity_usd < settings.min_liquidity_usd:
        return False
    if mcap_usd is not None and not (settings.min_mcap_usd <= mcap_usd <= settings.max_mcap_usd):
        return False
    return True


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def admit_survivors(store: Store, settings: GeckoSettings, now: str) -> int:
    q = ",".join("?" * len(MARKET_SOURCES))
    since = (_dt(now) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = await (await store.conn.execute(
        f"SELECT o.token_id, o.observed_at, o.features_json FROM observations o "
        f"JOIN tokens t ON t.token_id=o.token_id WHERE t.status='candidate' "
        f"AND o.source IN ({q}) AND o.observed_at<=? ORDER BY o.token_id, o.observed_at",
        (*MARKET_SOURCES, now))).fetchall()
    by_token: dict[str, list[tuple[str, float | None]]] = {}
    for tid, obs_at, fj in rows:
        by_token.setdefault(tid, []).append((obs_at, json.loads(fj).get("liquidity_usd")))
    promoted = 0
    for tid, obs in by_token.items():
        latest_at, latest_liq = obs[-1]
        if latest_at < since or latest_liq is None or latest_liq < settings.min_liquidity_usd:
            continue
        cutoff = _dt(latest_at) - timedelta(hours=settings.survivor_hours)
        if any(liq is not None and liq >= settings.min_liquidity_usd and _dt(at) <= cutoff
               for at, liq in obs[:-1]):
            await store.set_token_status(tid, "active")
            promoted += 1
    return promoted
```

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: all passed (http 4, universe 2)

- [ ] **Step 5: Commit**

```bash
git add gecko/http.py gecko/universe.py tests/gecko/test_http.py tests/gecko/test_universe.py
git commit -m "feat(gecko): http backoff helper, universe band and 24h survivor admission"
```

---
### Task 5: DexScreener collector

**Files:**
- Create: `gecko/collectors/__init__.py`, `gecko/collectors/dexscreener.py`
- Modify: `gecko/__main__.py` (`_load_jobs` imports `gecko.collectors.dexscreener`)
- Test: `tests/gecko/test_dexscreener.py`

**API (free, keyless; 60 rpm for boosts/profiles, 300 rpm for tokens):**
- `GET https://api.dexscreener.com/token-boosts/latest/v1` → JSON list of `{chainId, tokenAddress, amount, totalAmount, url, description, links}`
- `GET https://api.dexscreener.com/token-profiles/latest/v1` → JSON list of `{chainId, tokenAddress, url, description, links}`
- `GET https://api.dexscreener.com/tokens/v1/{chainId}/{addr1,addr2,...}` (≤ 30 addresses per call) → JSON list of pairs `{chainId, dexId, pairAddress, baseToken{address,name,symbol}, quoteToken{address,symbol}, priceUsd (string), txns{h24{buys,sells}}, volume{h24}, liquidity{usd}, fdv, marketCap, pairCreatedAt (ms epoch)}`
- `chainId` values used: `solana`, `base`, `ethereum`, `bsc` (identical to `GeckoSettings.chains`).

**Interfaces:**
- Consumes: `get_json`, `in_band`, `admit_survivors`, `Store.upsert_token/add_observation`, `JobResult`, `run_job`.
- Produces:
  - `CHAIN_MAP: dict[str, str] = {"solana": "solana", "base": "base", "ethereum": "ethereum", "bsc": "bsc"}` (DexScreener chainId → gecko chain)
  - `def best_pair(pairs: list[dict], address: str) -> dict | None` — the pair whose `baseToken.address` equals `address` (case-insensitive) with the highest `liquidity.usd`; None if no pair has numeric `priceUsd` and `liquidity.usd`.
  - `def pair_features(pair: dict) -> dict` → `{price_usd, liquidity_usd, fdv_usd, mcap_usd, vol_24h_usd, buys_24h, sells_24h, pair_created_at (ISO Z), pair_address, dex}`; missing numeric fields become `None`.
  - `async collect(store, settings, session, now) -> JobResult` — boosts ∪ profiles → dedupe `(chain, address)` → batches of 30 → `tokens/v1` → for each address with a best pair and `in_band(...)`: `upsert_token(..., first_source="dexscreener", pool_created_at=pair_created_at)` + `add_observation(..., "dexscreener", now, pair_features)`; then `admit_survivors`. Addresses whose pair lacks price/liquidity are skipped and counted in the log, never raised.
  - job `dexscreener` registered.

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_dexscreener.py`:
```python
import aiohttp
from aioresponses import aioresponses
from gecko.collectors.dexscreener import best_pair, pair_features, collect, BOOSTS_URL, PROFILES_URL, TOKENS_URL

NOW = "2026-10-09T00:00:00Z"


def pair(addr="0xAbC", chain="base", price="1.25", liq=400_000, fdv=2e6, mcap=1.8e6, created=1_759_000_000_000):
    return {"chainId": chain, "dexId": "uniswap", "pairAddress": "0xpair",
            "baseToken": {"address": addr, "name": "Abc Coin", "symbol": "ABC"},
            "quoteToken": {"address": "0xweth", "symbol": "WETH"},
            "priceUsd": price, "txns": {"h24": {"buys": 120, "sells": 80}},
            "volume": {"h24": 55_000.5}, "liquidity": {"usd": liq}, "fdv": fdv, "marketCap": mcap,
            "pairCreatedAt": created}


def test_best_pair_picks_deepest_matching_base():
    pairs = [pair(liq=10), pair(liq=500_000), pair(addr="0xother", liq=9e9)]
    assert best_pair(pairs, "0xabc")["liquidity"]["usd"] == 500_000


def test_pair_missing_fields_skipped():
    p = pair(); p["priceUsd"] = None; p["liquidity"] = None
    assert best_pair([p], "0xabc") is None
    q = pair(); del q["marketCap"]; q["volume"] = {}
    f = pair_features(q)
    assert f["mcap_usd"] is None and f["vol_24h_usd"] is None and f["price_usd"] == 1.25


def test_pair_features_iso_created():
    assert pair_features(pair())["pair_created_at"] == "2025-09-27T19:33:20Z"


async def test_collect_writes_tokens_and_observations(store, settings):
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": "0xAbC"},
                                   {"chainId": "tron", "tokenAddress": "Txyz"}])
        m.get(PROFILES_URL, payload=[{"chainId": "base", "tokenAddress": "0xabc"},
                                     {"chainId": "solana", "tokenAddress": "So1ana"}])
        m.get(TOKENS_URL.format(chain="base", addresses="0xabc"), payload=[pair()])
        m.get(TOKENS_URL.format(chain="solana", addresses="so1ana"),
              payload=[pair(addr="So1ana", chain="solana", liq=100)])  # below floor
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 1
    tok = await (await store.conn.execute("SELECT token_id, chain, symbol, status, pool_created_at FROM tokens")).fetchall()
    assert tok == [("base:0xabc", "base", "ABC", "candidate", "2025-09-27T19:33:20Z")]
    obs = await store.observations("base:0xabc", as_of=NOW)
    assert obs[0].features["liquidity_usd"] == 400_000 and obs[0].features["buys_24h"] == 120


async def test_collect_batches_thirty(store, settings):
    addrs = [f"0x{i:040x}" for i in range(31)]
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=[{"chainId": "base", "tokenAddress": a} for a in addrs])
        m.get(PROFILES_URL, payload=[])
        m.get(TOKENS_URL.format(chain="base", addresses=",".join(addrs[:30])), payload=[pair(addr=a) for a in addrs[:30]])
        m.get(TOKENS_URL.format(chain="base", addresses=addrs[30]), payload=[pair(addr=addrs[30])])
        async with aiohttp.ClientSession() as s:
            r = await collect(store, settings, s, NOW)
    assert r.rows_written == 31
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_dexscreener.py -q`
Expected: FAIL `ModuleNotFoundError: gecko.collectors`

- [ ] **Step 3: Implement**

`gecko/collectors/__init__.py`: empty.

`gecko/collectors/dexscreener.py`:
```python
from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
import structlog

from gecko.__main__ import register
from gecko.http import get_json
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store
from gecko.universe import admit_survivors, in_band

log = structlog.get_logger("gecko.dexscreener")
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"
CHAIN_MAP = {"solana": "solana", "base": "base", "ethereum": "ethereum", "bsc": "bsc"}
BATCH = 30


def _num(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _iso_ms(ms) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def best_pair(pairs: list[dict], address: str) -> dict | None:
    best = None
    for p in pairs:
        if str((p.get("baseToken") or {}).get("address", "")).lower() != address.lower():
            continue
        price = _num(p.get("priceUsd"))
        liq = _num((p.get("liquidity") or {}).get("usd"))
        if price is None or liq is None:
            continue
        if best is None or liq > _num((best.get("liquidity") or {}).get("usd")):
            best = p
    return best


def pair_features(p: dict) -> dict:
    tx = (p.get("txns") or {}).get("h24") or {}
    return {
        "price_usd": _num(p.get("priceUsd")),
        "liquidity_usd": _num((p.get("liquidity") or {}).get("usd")),
        "fdv_usd": _num(p.get("fdv")),
        "mcap_usd": _num(p.get("marketCap")),
        "vol_24h_usd": _num((p.get("volume") or {}).get("h24")),
        "buys_24h": tx.get("buys"),
        "sells_24h": tx.get("sells"),
        "pair_created_at": _iso_ms(p.get("pairCreatedAt")),
        "pair_address": p.get("pairAddress"),
        "dex": p.get("dexId"),
    }


async def _discover(session, settings) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for url in (BOOSTS_URL, PROFILES_URL):
        items = await get_json(session, url, settings=settings)
        for it in items or []:
            chain = CHAIN_MAP.get(str(it.get("chainId", "")).lower())
            addr = str(it.get("tokenAddress") or "").lower()
            if chain in settings.chains and addr:
                found.setdefault(chain, set()).add(addr)
    return found


async def collect(store: Store, settings: GeckoSettings, session: aiohttp.ClientSession,
                  now: str) -> JobResult:
    found = await _discover(session, settings)
    written = skipped = 0
    for chain, addrs in found.items():
        ordered = sorted(addrs)
        for i in range(0, len(ordered), BATCH):
            batch = ordered[i:i + BATCH]
            pairs = await get_json(session, TOKENS_URL.format(chain=chain, addresses=",".join(batch)),
                                   settings=settings)
            for addr in batch:
                p = best_pair(pairs or [], addr)
                if p is None:
                    skipped += 1
                    continue
                f = pair_features(p)
                if not in_band(settings, mcap_usd=f["mcap_usd"] or f["fdv_usd"],
                               liquidity_usd=f["liquidity_usd"], chain=chain):
                    skipped += 1
                    continue
                base = p.get("baseToken") or {}
                tid = f"{chain}:{addr}"
                await store.upsert_token(tid, chain, addr, base.get("symbol") or "?", base.get("name") or "?",
                                         now, "dexscreener", pool_created_at=f["pair_created_at"])
                await store.add_observation(tid, "dexscreener", now, f)
                written += 1
    promoted = await admit_survivors(store, settings, now)
    log.info("dexscreener_done", written=written, skipped=skipped, promoted=promoted)
    return JobResult(rows_written=written)


@register("dexscreener")
async def dexscreener_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store, session):
        return await collect(store, settings, session, now)

    return await run_job("dexscreener", settings, body)
```

Add `import gecko.collectors.dexscreener  # noqa: F401` to `_load_jobs()`.

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: all passed (dexscreener 5)

- [ ] **Step 5: Commit**

```bash
git add gecko/collectors gecko/__main__.py tests/gecko/test_dexscreener.py
git commit -m "feat(gecko): DexScreener boosts/profiles collector with deepest-pair selection"
```

---
### Task 6: GeckoTerminal collector (new pools, trending pools, daily OHLCV)

**Files:**
- Create: `gecko/collectors/geckoterminal.py`
- Modify: `gecko/__main__.py` (`_load_jobs` imports it)
- Test: `tests/gecko/test_geckoterminal.py`

**API (keyless, ~30 rpm; header `Accept: application/json;version=20230302`):**
- `GET https://api.geckoterminal.com/api/v2/networks/{network}/new_pools?page=1` and `.../trending_pools?page=1` → `{"data": [{"id": "<network>_<pool>", "attributes": {"address", "name", "base_token_price_usd" (string), "pool_created_at" (ISO), "fdv_usd", "market_cap_usd", "reserve_in_usd", "volume_usd": {"h24": ...}, "transactions": {"h24": {"buys", "sells"}}}, "relationships": {"base_token": {"data": {"id": "<network>_<token_address>"}}}}]}`
- `GET https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/day?limit=30&currency=usd` → `{"data": {"attributes": {"ohlcv_list": [[ts_sec, o, h, l, c, volume_usd], ...]}}}` (descending by time; sort ascending before storing)
- Network slugs: `NETWORK_MAP = {"solana": "solana", "base": "base", "ethereum": "eth", "bsc": "bsc"}` (gecko chain → GT network). The base-token id is `f"{network}_{address}"`; split on the first `_` after the network prefix.

**Interfaces:**
- Consumes: `get_json`, `in_band`, `admit_survivors`, `Store`, `JobResult`, `run_job`.
- Produces:
  - `def pool_features(attrs: dict, list_name: str) -> dict` → `{price_usd, liquidity_usd (reserve_in_usd), fdv_usd, mcap_usd, vol_24h_usd, buys_24h, sells_24h, pool_created_at, pool_address, source_list}`
  - `def token_address_from(rel_id: str, network: str) -> str` → lowercase address.
  - `async collect_pools(store, settings, session, now) -> JobResult` — for every chain in `settings.chains`: both lists → in-band pools → `upsert_token(first_source="geckoterminal", pool_created_at=...)` + observation `geckoterminal`; then `admit_survivors`.
  - `async collect_ohlcv(store, settings, session, now) -> JobResult` — for every token with `status IN ('candidate','active')` and a known `pool_address` in its latest `geckoterminal`/`dexscreener` observation: fetch daily OHLCV, store observation source `geckoterminal_ohlcv` with `{pool_address, candles}` (ascending). Budget: at most `settings.ohlcv_max_per_run = 200` tokens per run, oldest-OHLCV-first.
  - jobs `geckoterminal` (pools, every 15 min) and `geckoterminal-ohlcv` (daily) registered. Add `ohlcv_max_per_run: int = 200` to `GeckoSettings`.

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_geckoterminal.py`:
```python
import aiohttp
from aioresponses import aioresponses
from gecko.collectors.geckoterminal import (pool_features, token_address_from, collect_pools, collect_ohlcv,
                                            POOLS_URL, OHLCV_URL, NETWORK_MAP)

NOW = "2026-10-09T00:00:00Z"


def pool(addr="So1anaTokenAddr", network="solana", reserve="350000.5", pool_addr="PoolAddr1"):
    return {"id": f"{network}_{pool_addr}", "attributes": {
        "address": pool_addr, "name": "ABC / SOL", "base_token_price_usd": "0.0123",
        "pool_created_at": "2026-09-20T10:00:00Z", "fdv_usd": "1500000", "market_cap_usd": None,
        "reserve_in_usd": reserve, "volume_usd": {"h24": "88000.1"},
        "transactions": {"h24": {"buys": 300, "sells": 210}}},
        "relationships": {"base_token": {"data": {"id": f"{network}_{addr}"}}}}


def test_token_address_from():
    assert token_address_from("solana_So1anaTokenAddr", "solana") == "so1anatokenaddr"
    assert token_address_from("eth_0xAbC_weird", "eth") == "0xabc_weird"


def test_pool_features_types():
    f = pool_features(pool()["attributes"], "new_pools")
    assert f["price_usd"] == 0.0123 and f["liquidity_usd"] == 350000.5 and f["mcap_usd"] is None
    assert f["fdv_usd"] == 1500000.0 and f["buys_24h"] == 300 and f["source_list"] == "new_pools"
    assert f["pool_created_at"] == "2026-09-20T10:00:00Z" and f["pool_address"] == "PoolAddr1"


async def test_collect_pools_all_chains_and_band(store, settings):
    settings.chains = ["solana", "ethereum"]
    with aioresponses() as m:
        m.get(POOLS_URL.format(network="solana", list="new_pools"), payload={"data": [pool()]})
        m.get(POOLS_URL.format(network="solana", list="trending_pools"), payload={"data": [pool(reserve="10")]})
        m.get(POOLS_URL.format(network="eth", list="new_pools"), payload={"data": []})
        m.get(POOLS_URL.format(network="eth", list="trending_pools"),
              payload={"data": [pool(addr="0xEEE", network="eth", pool_addr="0xpool")]})
        async with aiohttp.ClientSession() as s:
            r = await collect_pools(store, settings, s, NOW)
    assert r.rows_written == 2
    rows = await (await store.conn.execute("SELECT token_id, chain, pool_created_at FROM tokens ORDER BY token_id")).fetchall()
    assert rows == [("ethereum:0xeee", "ethereum", "2026-09-20T10:00:00Z"),
                    ("solana:so1anatokenaddr", "solana", "2026-09-20T10:00:00Z")]


async def test_collect_ohlcv_sorted_ascending_and_budgeted(store, settings):
    settings.ohlcv_max_per_run = 1
    for tid, pa in (("solana:a", "pa"), ("solana:b", "pb")):
        await store.upsert_token(tid, "solana", tid[-1], "X", "X", NOW, "geckoterminal")
        await store.add_observation(tid, "geckoterminal", NOW, {"pool_address": pa, "liquidity_usd": 3e5})
    with aioresponses() as m:
        m.get(OHLCV_URL.format(network="solana", pool="pa"),
              payload={"data": {"attributes": {"ohlcv_list": [[200, 1, 1, 1, 1, 5.0], [100, 1, 1, 1, 1, 3.0]]}}})
        async with aiohttp.ClientSession() as s:
            r = await collect_ohlcv(store, settings, s, NOW)
    assert r.rows_written == 1
    obs = await store.observations("solana:a", as_of=NOW, source="geckoterminal_ohlcv")
    assert obs[0].features["candles"] == [[100, 1, 1, 1, 1, 3.0], [200, 1, 1, 1, 1, 5.0]]
    assert await store.observations("solana:b", as_of=NOW, source="geckoterminal_ohlcv") == []
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_geckoterminal.py -q`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement `gecko/collectors/geckoterminal.py`**

```python
from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
import structlog

from gecko.__main__ import register
from gecko.http import get_json
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store
from gecko.universe import admit_survivors, in_band

log = structlog.get_logger("gecko.geckoterminal")
BASE = "https://api.geckoterminal.com/api/v2"
POOLS_URL = BASE + "/networks/{network}/{list}?page=1"
OHLCV_URL = BASE + "/networks/{network}/pools/{pool}/ohlcv/day?limit=30&currency=usd"
HEADERS = {"Accept": "application/json;version=20230302"}
NETWORK_MAP = {"solana": "solana", "base": "base", "ethereum": "eth", "bsc": "bsc"}
LISTS = ("new_pools", "trending_pools")


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def token_address_from(rel_id: str, network: str) -> str:
    prefix = network + "_"
    return (rel_id[len(prefix):] if rel_id.startswith(prefix) else rel_id.split("_", 1)[-1]).lower()


def pool_features(attrs: dict, list_name: str) -> dict:
    tx = (attrs.get("transactions") or {}).get("h24") or {}
    return {
        "price_usd": _num(attrs.get("base_token_price_usd")),
        "liquidity_usd": _num(attrs.get("reserve_in_usd")),
        "fdv_usd": _num(attrs.get("fdv_usd")),
        "mcap_usd": _num(attrs.get("market_cap_usd")),
        "vol_24h_usd": _num((attrs.get("volume_usd") or {}).get("h24")),
        "buys_24h": tx.get("buys"),
        "sells_24h": tx.get("sells"),
        "pool_created_at": attrs.get("pool_created_at"),
        "pool_address": attrs.get("address"),
        "source_list": list_name,
    }


async def collect_pools(store: Store, settings: GeckoSettings, session: aiohttp.ClientSession,
                        now: str) -> JobResult:
    written = 0
    for chain in settings.chains:
        network = NETWORK_MAP[chain]
        for list_name in LISTS:
            body = await get_json(session, POOLS_URL.format(network=network, list=list_name),
                                  settings=settings, headers=HEADERS)
            for item in (body or {}).get("data") or []:
                attrs = item.get("attributes") or {}
                f = pool_features(attrs, list_name)
                if not in_band(settings, mcap_usd=f["mcap_usd"] or f["fdv_usd"],
                               liquidity_usd=f["liquidity_usd"], chain=chain):
                    continue
                rel = (((item.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")
                if not rel:
                    continue
                addr = token_address_from(rel, network)
                tid = f"{chain}:{addr}"
                symbol = (attrs.get("name") or "?").split("/")[0].strip() or "?"
                await store.upsert_token(tid, chain, addr, symbol, symbol, now, "geckoterminal",
                                         pool_created_at=f["pool_created_at"])
                await store.add_observation(tid, "geckoterminal", now, f)
                written += 1
    promoted = await admit_survivors(store, settings, now)
    log.info("geckoterminal_pools_done", written=written, promoted=promoted)
    return JobResult(rows_written=written)


async def _pool_for(store: Store, token_id: str, now: str) -> str | None:
    for src in ("geckoterminal", "dexscreener"):
        obs = await store.observations(token_id, as_of=now, source=src, limit=1)
        if obs:
            pa = obs[0].features.get("pool_address") or obs[0].features.get("pair_address")
            if pa:
                return pa
    return None


async def collect_ohlcv(store: Store, settings: GeckoSettings, session: aiohttp.ClientSession,
                        now: str) -> JobResult:
    rows = await (await store.conn.execute(
        "SELECT t.token_id, t.chain, (SELECT MAX(observed_at) FROM observations o "
        "WHERE o.token_id=t.token_id AND o.source='geckoterminal_ohlcv') AS last_ohlcv "
        "FROM tokens t WHERE t.status IN ('candidate','active') "
        "ORDER BY last_ohlcv IS NOT NULL, last_ohlcv, t.token_id LIMIT ?", (settings.ohlcv_max_per_run,))).fetchall()
    written = 0
    for tid, chain, _ in rows:
        pool = await _pool_for(store, tid, now)
        if not pool:
            continue
        body = await get_json(session, OHLCV_URL.format(network=NETWORK_MAP[chain], pool=pool),
                              settings=settings, headers=HEADERS, ok_statuses=(200, 404))
        candles = ((((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        if not candles:
            continue
        candles = sorted(([float(x) for x in c] for c in candles), key=lambda c: c[0])
        await store.add_observation(tid, "geckoterminal_ohlcv", now, {"pool_address": pool, "candles": candles})
        written += 1
    log.info("geckoterminal_ohlcv_done", written=written, considered=len(rows))
    return JobResult(rows_written=written)


@register("geckoterminal")
async def pools_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store, session):
        return await collect_pools(store, settings, session, now)

    return await run_job("geckoterminal", settings, body)


@register("geckoterminal-ohlcv")
async def ohlcv_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store, session):
        return await collect_ohlcv(store, settings, session, now)

    return await run_job("geckoterminal-ohlcv", settings, body)
```

Add `ohlcv_max_per_run: int = 200` to `GeckoSettings`; add `"geckoterminal-ohlcv"` to `WRITER_JOBS["observations"]` in `gecko/freshness.py`; import the module in `_load_jobs()`.

Note the test for `token_address_from("eth_0xAbC_weird", "eth")`: the prefix path strips only the leading `eth_`, so an address containing `_` survives intact.

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: all passed (geckoterminal 4)

- [ ] **Step 5: Commit**

```bash
git add gecko/collectors/geckoterminal.py gecko/settings.py gecko/freshness.py gecko/__main__.py tests/gecko/test_geckoterminal.py
git commit -m "feat(gecko): GeckoTerminal new/trending pools and daily OHLCV collectors"
```

---

### Task 7: CoinGecko Demo collector (listed universe, trending, BTC benchmark) with monthly cap

**Files:**
- Create: `gecko/collectors/coingecko.py`
- Modify: `gecko/__main__.py`
- Test: `tests/gecko/test_coingecko.py`

**API (Demo tier: base `https://api.coingecko.com/api/v3`, header `x-cg-demo-api-key: <key>`, 30 rpm, 10,000 calls/month; every request = 1 call):**
- `GET /coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=N&price_change_percentage=24h,7d` → list of `{id, symbol, name, current_price, market_cap, market_cap_rank, total_volume, price_change_percentage_24h_in_currency, price_change_percentage_7d_in_currency}`. Sweep pages from `settings.cg_first_page` (default 2, skipping the top-250 mega caps) until a page's last `market_cap` < `min_mcap_usd` or `settings.cg_max_pages` (default 14) is hit; rows with `market_cap > max_mcap_usd` are ignored.
- `GET /coins/{id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false` → `{platforms: {"solana": "<addr>", "base": "<addr>", "ethereum": ..., "binance-smart-chain": ...}}`. Platform map: `PLATFORM_MAP = {"solana": "solana", "base": "base", "ethereum": "ethereum", "binance-smart-chain": "bsc"}`. Called once per NEW coin (cached in `tokens.cg_id`); a coin with no supported platform is recorded in a `cg_unmapped` observation on token_id `cg:<id>` and never re-fetched.
- `GET /search/trending` → `{"coins": [{"item": {"id", "symbol", "name", "market_cap_rank", "score"}}]}`
- `GET /simple/price?ids=bitcoin&vs_currencies=usd` → `{"bitcoin": {"usd": 60000.0}}`

**Budget:** `calls_this_month(store) = SUM(credits_used) FROM runs WHERE job LIKE 'coingecko%' AND started_at >= first day of month`. If `calls_this_month + planned > settings.coingecko_monthly_cap`, the job logs `cg_budget_exhausted`, writes nothing and returns `JobResult(credits_used=0)` with exit 0 (a budget stop is not an incident; freshness will page if it persists).

**Interfaces:**
- Produces:
  - `CGClient(session, settings)` with `async get(path, params) -> Any` adding the header and counting `.calls`.
  - `async markets_sweep(store, settings, session, now) -> JobResult` (job `coingecko-markets`, daily): for each in-band row with a supported platform → `upsert_token(cg_id=...)` + observation `coingecko` `{price_usd, mcap_usd, vol_24h_usd, mcap_rank, price_change_24h_pct, price_change_7d_pct}` (no `liquidity_usd`: listed coins get liquidity from DexScreener/GT observations; `in_band` for CG rows is called with `liquidity_usd=settings.min_liquidity_usd` so the mcap band alone decides admission into `tokens`, and `admit_survivors` still requires DEX liquidity observations before `active`).
  - `async hourly(store, settings, session, now) -> JobResult` (job `coingecko-hourly`): trending (observation `coingecko` with `trending_rank` on mapped coins only) + BTC `simple/price` → `upsert_token("benchmark:btc", "benchmark", "btc", "BTC", "Bitcoin", ...)` + observation `{price_usd}`.
  - 401 from CoinGecko raises `UpstreamError(401)` → run recorded failed, exit 1 (Review Focus 3).

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_coingecko.py`:
```python
import re
import aiohttp
import pytest
from aioresponses import aioresponses
from pydantic import SecretStr
from gecko.collectors.coingecko import markets_sweep, hourly, calls_this_month, BASE
from gecko.http import UpstreamError

NOW = "2026-10-09T00:00:00Z"
MARKETS = re.compile(re.escape(BASE + "/coins/markets") + r"\?.*")
COIN = re.compile(re.escape(BASE + "/coins/") + r"(?!markets)[a-z0-9-]+\?.*")


def row(cid, mcap, price=1.0):
    return {"id": cid, "symbol": cid[:3], "name": cid.title(), "current_price": price, "market_cap": mcap,
            "market_cap_rank": 500, "total_volume": 1e5, "price_change_percentage_24h_in_currency": 1.5,
            "price_change_percentage_7d_in_currency": 10.0}


@pytest.fixture
def cg_settings(settings):
    settings.coingecko_demo_api_key = SecretStr("demo")
    settings.cg_first_page = 1
    settings.cg_max_pages = 3
    return settings


async def test_sweep_maps_platforms_and_counts_calls(store, cg_settings):
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("abc-coin", 5e6), row("mega", 9e9), row("nochain", 2e6)])
        m.get(MARKETS, payload=[row("tail", 100.0)])  # last mcap below floor -> stop
        m.get(COIN, payload={"platforms": {"solana": "So1Addr"}})
        m.get(COIN, payload={"platforms": {"tron": "T1"}})
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW)
    assert r.credits_used == 4 and r.rows_written == 1
    tok = await (await store.conn.execute("SELECT token_id, cg_id FROM tokens WHERE chain='solana'")).fetchall()
    assert tok == [("solana:so1addr", "abc-coin")]
    obs = await store.observations("solana:so1addr", as_of=NOW, source="coingecko")
    assert obs[0].features["mcap_usd"] == 5e6 and obs[0].features["price_change_7d_pct"] == 10.0
    unmapped = await store.observations("cg:nochain", as_of=NOW, source="cg_unmapped")
    assert len(unmapped) == 1
    hdr = next(iter(m.requests.values()))[0].kwargs["headers"]
    assert hdr["x-cg-demo-api-key"] == "demo"


async def test_sweep_does_not_refetch_known_platforms(store, cg_settings):
    await store.upsert_token("solana:so1addr", "solana", "so1addr", "ABC", "Abc", NOW, "coingecko", cg_id="abc-coin")
    with aioresponses() as m:
        m.get(MARKETS, payload=[row("abc-coin", 5e6)])
        m.get(MARKETS, payload=[])
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW)
    assert r.credits_used == 2


async def test_markets_429_then_200(store, cg_settings):
    with aioresponses() as m:
        m.get(MARKETS, status=429)
        m.get(MARKETS, payload=[row("tail", 100.0)])
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW, sleep=lambda s: _noop())
    assert r.credits_used == 2


async def _noop():
    return None


async def test_markets_401_fails_run(store, cg_settings):
    with aioresponses() as m:
        m.get(MARKETS, status=401, payload={"status": {"error_code": 10004}})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(UpstreamError) as ei:
                await markets_sweep(store, cg_settings, s, NOW)
    assert ei.value.status == 401


async def test_monthly_cap_stops_before_calling(store, cg_settings):
    cg_settings.coingecko_monthly_cap = 10
    await store.record_run("coingecko-markets", "2026-10-01T00:00:00Z", "2026-10-01T00:01:00Z", True, credits_used=9)
    assert await calls_this_month(store, NOW) == 9
    with aioresponses() as m:
        async with aiohttp.ClientSession() as s:
            r = await markets_sweep(store, cg_settings, s, NOW)
    assert r.credits_used == 0 and r.rows_written == 0 and m.requests == {}


async def test_hourly_btc_and_trending(store, cg_settings):
    await store.upsert_token("solana:so1addr", "solana", "so1addr", "ABC", "Abc", NOW, "coingecko", cg_id="abc-coin")
    with aioresponses() as m:
        m.get(re.compile(re.escape(BASE + "/search/trending")),
              payload={"coins": [{"item": {"id": "abc-coin", "symbol": "abc", "name": "Abc", "market_cap_rank": 700}},
                                 {"item": {"id": "unknown", "symbol": "u", "name": "U", "market_cap_rank": 9}}]})
        m.get(re.compile(re.escape(BASE + "/simple/price") + r"\?.*"), payload={"bitcoin": {"usd": 61234.5}})
        async with aiohttp.ClientSession() as s:
            r = await hourly(store, cg_settings, s, NOW)
    assert r.credits_used == 2
    btc = await store.observations("benchmark:btc", as_of=NOW, source="coingecko")
    assert btc[0].features["price_usd"] == 61234.5
    tr = await store.observations("solana:so1addr", as_of=NOW, source="coingecko")
    assert tr[0].features["trending_rank"] == 1
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_coingecko.py -q`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Add to `GeckoSettings`: `cg_first_page: int = 2`, `cg_max_pages: int = 14`.

`gecko/collectors/coingecko.py`:
```python
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog

from gecko.__main__ import register
from gecko.http import get_json
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store
from gecko.universe import in_band

log = structlog.get_logger("gecko.coingecko")
BASE = "https://api.coingecko.com/api/v3"
PLATFORM_MAP = {"solana": "solana", "base": "base", "ethereum": "ethereum", "binance-smart-chain": "bsc"}
BTC_ID = "benchmark:btc"


class CGClient:
    def __init__(self, session: aiohttp.ClientSession, settings: GeckoSettings, sleep=asyncio.sleep):
        self.session, self.settings, self.sleep, self.calls = session, settings, sleep, 0

    def _headers(self) -> dict:
        key = self.settings.coingecko_demo_api_key
        return {"x-cg-demo-api-key": key.get_secret_value()} if key else {}

    async def get(self, path: str, params: dict | None = None) -> Any:
        self.calls += 1
        return await get_json(self.session, BASE + path, settings=self.settings,
                              headers=self._headers(), params=params, sleep=self.sleep)


async def calls_this_month(store: Store, now: str) -> int:
    month_start = now[:7] + "-01T00:00:00Z"
    row = await (await store.conn.execute(
        "SELECT COALESCE(SUM(credits_used),0) FROM runs WHERE job LIKE 'coingecko%' AND started_at>=?",
        (month_start,))).fetchone()
    return int(row[0])


async def _within_budget(store: Store, settings: GeckoSettings, now: str, planned: int) -> bool:
    used = await calls_this_month(store, now)
    if used + planned > settings.coingecko_monthly_cap:
        log.warning("cg_budget_exhausted", used=used, planned=planned, cap=settings.coingecko_monthly_cap)
        return False
    return True


async def _platform_for(store: Store, cg: CGClient, cid: str, now: str) -> tuple[str, str] | None:
    row = await (await store.conn.execute(
        "SELECT chain, address FROM tokens WHERE cg_id=?", (cid,))).fetchone()
    if row:
        return row[0], row[1]
    seen = await store.observations(f"cg:{cid}", as_of=now, source="cg_unmapped", limit=1)
    if seen:
        return None
    body = await cg.get(f"/coins/{cid}", {"localization": "false", "tickers": "false", "market_data": "false",
                                          "community_data": "false", "developer_data": "false", "sparkline": "false"})
    for platform, addr in ((body or {}).get("platforms") or {}).items():
        chain = PLATFORM_MAP.get(platform)
        if chain and addr:
            return chain, str(addr).lower()
    await store.upsert_token(f"cg:{cid}", "cg", cid, cid, cid, now, "coingecko", cg_id=cid)
    await store.set_token_status(f"cg:{cid}", "disqualified", "no_supported_platform")
    await store.add_observation(f"cg:{cid}", "cg_unmapped", now, {"platforms": list((body or {}).get("platforms") or {})})
    return None


def _market_features(r: dict) -> dict:
    return {"price_usd": r.get("current_price"), "mcap_usd": r.get("market_cap"),
            "vol_24h_usd": r.get("total_volume"), "mcap_rank": r.get("market_cap_rank"),
            "price_change_24h_pct": r.get("price_change_percentage_24h_in_currency"),
            "price_change_7d_pct": r.get("price_change_percentage_7d_in_currency")}


async def markets_sweep(store: Store, settings: GeckoSettings, session: aiohttp.ClientSession,
                        now: str, sleep=asyncio.sleep) -> JobResult:
    planned = settings.cg_max_pages - settings.cg_first_page + 1
    if not await _within_budget(store, settings, now, planned):
        return JobResult()
    cg = CGClient(session, settings, sleep)
    written = 0
    for page in range(settings.cg_first_page, settings.cg_max_pages + 1):
        rows = await cg.get("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc",
                                               "per_page": 250, "page": page,
                                               "price_change_percentage": "24h,7d"})
        if not rows:
            break
        for r in rows:
            mcap = r.get("market_cap")
            if mcap is None or mcap > settings.max_mcap_usd or mcap < settings.min_mcap_usd:
                continue
            if not in_band(settings, mcap_usd=mcap, liquidity_usd=settings.min_liquidity_usd, chain="solana"):
                continue
            mapped = await _platform_for(store, cg, r["id"], now)
            if not mapped:
                continue
            chain, addr = mapped
            if chain not in settings.chains:
                continue
            tid = f"{chain}:{addr}"
            await store.upsert_token(tid, chain, addr, (r.get("symbol") or "?").upper(), r.get("name") or "?",
                                     now, "coingecko", cg_id=r["id"])
            await store.add_observation(tid, "coingecko", now, _market_features(r))
            written += 1
        if (rows[-1].get("market_cap") or 0) < settings.min_mcap_usd:
            break
    log.info("coingecko_markets_done", written=written, calls=cg.calls)
    return JobResult(rows_written=written, credits_used=cg.calls)


async def hourly(store: Store, settings: GeckoSettings, session: aiohttp.ClientSession, now: str) -> JobResult:
    if not await _within_budget(store, settings, now, 2):
        return JobResult()
    cg = CGClient(session, settings)
    written = 0
    trending = await cg.get("/search/trending")
    for rank, entry in enumerate(((trending or {}).get("coins") or []), start=1):
        item = entry.get("item") or {}
        row = await (await store.conn.execute(
            "SELECT token_id FROM tokens WHERE cg_id=? AND chain!='cg'", (item.get("id"),))).fetchone()
        if row:
            await store.add_observation(row[0], "coingecko", now,
                                        {"trending_rank": rank, "mcap_rank": item.get("market_cap_rank")})
            written += 1
    price = await cg.get("/simple/price", {"ids": "bitcoin", "vs_currencies": "usd"})
    btc = ((price or {}).get("bitcoin") or {}).get("usd")
    if btc:
        await store.upsert_token(BTC_ID, "benchmark", "btc", "BTC", "Bitcoin", now, "coingecko", cg_id="bitcoin")
        await store.add_observation(BTC_ID, "coingecko", now, {"price_usd": float(btc)})
        written += 1
    return JobResult(rows_written=written, credits_used=cg.calls)


@register("coingecko-markets")
async def markets_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store, session):
        return await markets_sweep(store, settings, session, now)

    return await run_job("coingecko-markets", settings, body)


@register("coingecko-hourly")
async def hourly_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store, session):
        return await hourly(store, settings, session, now)

    return await run_job("coingecko-hourly", settings, body)
```

Note: `in_band(... chain="solana")` in the sweep only applies the mcap band (any supported chain name works there); the real chain check happens after platform mapping. `tokens.status` CHECK already allows `'disqualified'`; the `cg:` pseudo-token exists only to remember unmapped ids and is excluded from ranking by `chain='cg'` and status.

Import the module in `_load_jobs()`.

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: all passed (coingecko 6)

- [ ] **Step 5: Commit**

```bash
git add gecko/collectors/coingecko.py gecko/settings.py gecko/__main__.py tests/gecko/test_coingecko.py
git commit -m "feat(gecko): CoinGecko Demo collector with platform mapping, trending, BTC benchmark and monthly cap"
```

---
### Task 8: Point-in-time features

**Files:**
- Create: `gecko/features.py`
- Test: `tests/gecko/test_features.py`

**Interfaces:**
- Consumes: `Store.observations(token_id, as_of=..., source=...)`, `tokens.pool_created_at/first_seen_at/chain`, `source_calls`, `forward_returns`, `GeckoSettings.young_chain_launch_dates`.
- Observation feature contract (keys written by collectors in Tasks 5-7 and by Plan 2 enrichment; every key optional):
  - source `dexscreener` / `geckoterminal` / `coingecko`: `price_usd, liquidity_usd, fdv_usd, mcap_usd, vol_24h_usd, buys_24h, sells_24h, pair_created_at|pool_created_at, pair_address|pool_address, mcap_rank, trending_rank`
  - source `geckoterminal_ohlcv`: `pool_address, candles: [[ts_sec, o, h, l, c, vol_usd], ...]` (daily, ascending)
  - source `enrich` (Plan 2): `holders, top10_pct, security_ok (bool), security_flags: [str], dd_score, smart_money_wallets, mint_revoked, freeze_revoked`
  - token_id `benchmark:btc`, source `coingecko`: `price_usd` (hourly; used by the ledger)
- Produces: `@dataclass Features` with fields `token_id, as_of, observed_at (latest market obs), price_usd, mcap_usd, liquidity_usd, vol_24h_usd, age_days, vol_7d_ratio, holders, holder_growth_7d, top10_pct, bundle_flag, smart_money_wallets, callers_7d, caller_quality, narrative_mentions_7d, security_ok, young_chain, momentum_ctx` (all `float | int | bool | None` except ids) and `.as_dict()`; `async compute_features(store, settings, token_id, as_of) -> Features`.

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_features.py`:
```python
import pytest
from gecko.features import compute_features

T0 = "2026-10-01T00:00:00Z"


async def seed(store, *, pool_created="2026-09-10T00:00:00Z"):
    await store.upsert_token("solana:abc", "solana", "abc", "ABC", "Abc", T0, "dexscreener",
                             pool_created_at=pool_created)


def candles(start_ts: int, vols: list[float]):
    return [[start_ts + i * 86400, 1, 1, 1, 1, v] for i, v in enumerate(vols)]


async def test_age_and_market_fields(store, settings):
    await seed(store)
    await store.add_observation("solana:abc", "dexscreener", "2026-10-05T00:00:00Z",
                                {"price_usd": 2.0, "mcap_usd": 1e6, "liquidity_usd": 3e5, "vol_24h_usd": 5e4})
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T12:00:00Z")
    assert f.price_usd == 2.0 and f.mcap_usd == 1e6 and f.liquidity_usd == 3e5
    assert f.age_days == pytest.approx(25.5, abs=0.01)
    assert f.observed_at == "2026-10-05T00:00:00Z"


async def test_no_lookahead(store, settings):
    await seed(store)
    await store.add_observation("solana:abc", "dexscreener", "2026-10-05T00:00:00Z", {"price_usd": 2.0})
    await store.add_observation("solana:abc", "dexscreener", "2026-10-07T00:00:00Z", {"price_usd": 99.0})
    f = await compute_features(store, settings, "solana:abc", "2026-10-06T00:00:00Z")
    assert f.price_usd == 2.0


async def test_vol_ratio_from_ohlcv(store, settings):
    await seed(store)
    # 14 daily candles ending the day before as_of: prior week 100/day, last week 250/day
    import datetime as dt
    as_of = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    start = int((as_of - dt.timedelta(days=14)).timestamp())
    await store.add_observation("solana:abc", "geckoterminal_ohlcv", "2026-10-15T00:00:00Z",
                                {"pool_address": "p", "candles": candles(start, [100] * 7 + [250] * 7)})
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio == pytest.approx(2.5)


async def test_vol_ratio_missing_when_short_history(store, settings):
    await seed(store)
    import datetime as dt
    as_of = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    start = int((as_of - dt.timedelta(days=10)).timestamp())
    await store.add_observation("solana:abc", "geckoterminal_ohlcv", "2026-10-15T00:00:00Z",
                                {"pool_address": "p", "candles": candles(start, [100] * 10)})
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio is None


async def test_vol_ratio_missing_when_prior_zero(store, settings):
    await seed(store)
    import datetime as dt
    as_of = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    start = int((as_of - dt.timedelta(days=14)).timestamp())
    await store.add_observation("solana:abc", "geckoterminal_ohlcv", "2026-10-15T00:00:00Z",
                                {"pool_address": "p", "candles": candles(start, [0] * 7 + [250] * 7)})
    f = await compute_features(store, settings, "solana:abc", "2026-10-15T00:00:00Z")
    assert f.vol_7d_ratio is None


async def test_holder_growth_and_bundle_flag(store, settings):
    await seed(store)
    await store.add_observation("solana:abc", "enrich", "2026-10-01T00:00:00Z", {"holders": 1000, "top10_pct": 20})
    await store.add_observation("solana:abc", "enrich", "2026-10-08T00:00:00Z", {"holders": 1500, "top10_pct": 45, "dd_score": 80})
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T06:00:00Z")
    assert f.holders == 1500 and f.holder_growth_7d == pytest.approx(0.5)
    assert f.top10_pct == 45 and f.bundle_flag is True  # top10 > 40


async def test_security_unknown_when_no_enrich(store, settings):
    await seed(store)
    f = await compute_features(store, settings, "solana:abc", "2026-10-08T00:00:00Z")
    assert f.security_ok is None and f.holders is None and f.bundle_flag is False


async def test_callers_and_quality(store, settings):
    await seed(store)
    await store.upsert_token("solana:old", "solana", "old", "OLD", "Old", "2026-08-01T00:00:00Z", "telegram")
    # caller c1 called OLD on 08-01; OLD's 30d return beat BTC -> hit
    await store.conn.execute(
        "INSERT INTO source_calls(token_id,source_type,source_id,event_id,posted_at,text_hash) VALUES(?,?,?,?,?,?)",
        ("solana:old", "telegram", "c1", "e1", "2026-08-01T00:00:00Z", "h"))
    await store.schedule_returns("solana:old", "2026-08-01T00:00:00Z")
    await store.set_return("solana:old", 30, 1.0, 2.0, 100.0, 110.0, "2026-08-31T00:00:00Z")
    for i, src in enumerate(["c1", "c1", "c2"]):
        await store.conn.execute(
            "INSERT INTO source_calls(token_id,source_type,source_id,event_id,posted_at,text_hash) VALUES(?,?,?,?,?,?)",
            ("solana:abc", "telegram", src, f"e{i+2}", "2026-10-03T00:00:00Z", "h"))
    await store.conn.execute(
        "INSERT INTO source_calls(token_id,source_type,source_id,event_id,posted_at,text_hash) VALUES(?,?,?,?,?,?)",
        ("solana:abc", "x", "kol1", "x1", "2026-10-04T00:00:00Z", "h"))
    await store.conn.commit()
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.callers_7d == 2  # c1, c2 (telegram only)
    assert f.caller_quality == pytest.approx(1.0)  # c1 hit rate 1.0; c2 no history -> excluded
    assert f.narrative_mentions_7d == 1


async def test_young_chain_flag(store, settings):
    settings.young_chain_launch_dates = {"solana": "2026-09-01"}
    await seed(store)
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.young_chain is True
    settings.young_chain_launch_dates = {}
    f = await compute_features(store, settings, "solana:abc", "2026-10-05T00:00:00Z")
    assert f.young_chain is False
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_features.py -q`
Expected: FAIL `ModuleNotFoundError: gecko.features`

- [ ] **Step 3: Implement `gecko/features.py`**

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from gecko.settings import GeckoSettings
from gecko.store import Observation, Store

MARKET_SOURCES = ("dexscreener", "geckoterminal", "coingecko")
YOUNG_CHAIN_DAYS = 180
BUNDLE_DD_BELOW = 60
BUNDLE_TOP10_ABOVE = 40


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Features:
    token_id: str
    as_of: str
    observed_at: str | None = None
    price_usd: float | None = None
    mcap_usd: float | None = None
    liquidity_usd: float | None = None
    vol_24h_usd: float | None = None
    age_days: float | None = None
    vol_7d_ratio: float | None = None
    holders: int | None = None
    holder_growth_7d: float | None = None
    top10_pct: float | None = None
    bundle_flag: bool = False
    smart_money_wallets: int | None = None
    callers_7d: int = 0
    caller_quality: float | None = None
    narrative_mentions_7d: int = 0
    security_ok: bool | None = None
    young_chain: bool = False
    momentum_ctx: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _first(obs: list[Observation], key: str):
    for o in obs:
        v = o.features.get(key)
        if v is not None:
            return v, o
    return None, None


def vol_ratio_from_candles(candles: list[list[float]], as_of: datetime) -> float | None:
    """candles: [[ts_sec,o,h,l,c,vol],...]. Uses only candles with ts < as_of.
    NULL when fewer than 14 usable candles or prior-week volume is zero."""
    t = as_of.timestamp()
    usable = [c for c in candles if c[0] < t]
    if len(usable) < 14:
        return None
    last = sum(c[5] for c in usable if t - 7 * 86400 <= c[0] < t)
    prior = sum(c[5] for c in usable if t - 14 * 86400 <= c[0] < t - 7 * 86400)
    if prior <= 0:
        return None
    return last / prior


async def _market_obs(store: Store, token_id: str, as_of: str) -> list[Observation]:
    obs: list[Observation] = []
    for src in MARKET_SOURCES:
        obs.extend(await store.observations(token_id, as_of=as_of, source=src, limit=60))
    obs.sort(key=lambda o: o.observed_at, reverse=True)
    return obs


async def compute_features(store: Store, settings: GeckoSettings, token_id: str, as_of: str) -> Features:
    f = Features(token_id=token_id, as_of=as_of)
    as_of_dt = _dt(as_of)
    trow = await (await store.conn.execute(
        "SELECT chain, first_seen_at, pool_created_at FROM tokens WHERE token_id=?", (token_id,))).fetchone()
    if trow is None:
        return f
    chain, first_seen, pool_created = trow

    market = await _market_obs(store, token_id, as_of)
    if market:
        f.observed_at = market[0].observed_at
    for key in ("price_usd", "mcap_usd", "liquidity_usd", "vol_24h_usd"):
        v, _ = _first(market, key)
        setattr(f, key, float(v) if v is not None else None)

    birth = min(d for d in (first_seen, pool_created) if d)
    f.age_days = (as_of_dt - _dt(birth)).total_seconds() / 86400

    ohlcv = await store.observations(token_id, as_of=as_of, source="geckoterminal_ohlcv", limit=1)
    if ohlcv:
        f.vol_7d_ratio = vol_ratio_from_candles(ohlcv[0].features.get("candles") or [], as_of_dt)

    week_ago = _iso(as_of_dt - timedelta(days=7))
    week_ago_market = [o for o in market if o.observed_at <= week_ago]
    p_now = f.price_usd
    p_then, _ = _first(week_ago_market, "price_usd")
    if p_now and p_then:
        f.momentum_ctx = p_now / float(p_then) - 1.0

    enrich = await store.observations(token_id, as_of=as_of, source="enrich", limit=30)
    if enrich:
        h, _ = _first(enrich, "holders")
        f.holders = int(h) if h is not None else None
        t10, _ = _first(enrich, "top10_pct")
        f.top10_pct = float(t10) if t10 is not None else None
        sm, _ = _first(enrich, "smart_money_wallets")
        f.smart_money_wallets = int(sm) if sm is not None else None
        sec, _ = _first(enrich, "security_ok")
        f.security_ok = bool(sec) if sec is not None else None
        dd, _ = _first(enrich, "dd_score")
        old = [o for o in enrich if o.observed_at <= week_ago]
        h_then, _ = _first(old, "holders")
        if f.holders is not None and h_then:
            f.holder_growth_7d = (f.holders - int(h_then)) / int(h_then)
        f.bundle_flag = bool((dd is not None and float(dd) < BUNDLE_DD_BELOW)
                             or (f.top10_pct is not None and f.top10_pct > BUNDLE_TOP10_ABOVE))

    rows = await (await store.conn.execute(
        "SELECT source_type, source_id FROM source_calls WHERE token_id=? AND posted_at>? AND posted_at<=?",
        (token_id, week_ago, as_of))).fetchall()
    tg_callers = {sid for st, sid in rows if st == "telegram"}
    f.callers_7d = len(tg_callers)
    f.narrative_mentions_7d = sum(1 for st, _ in rows if st == "x")
    if tg_callers:
        month_ago = _iso(as_of_dt - timedelta(days=30))
        q = ",".join("?" * len(tg_callers))
        hist = await (await store.conn.execute(
            f"SELECT sc.source_id, fr.ret_vs_btc FROM source_calls sc "
            f"JOIN forward_returns fr ON fr.token_id=sc.token_id AND fr.horizon_days=30 "
            f"WHERE sc.source_type='telegram' AND sc.source_id IN ({q}) AND sc.posted_at<=? "
            f"AND fr.measured_at IS NOT NULL AND fr.measured_at<=?",
            (*tg_callers, month_ago, as_of))).fetchall()
        per: dict[str, list[float]] = {}
        for sid, rvb in hist:
            if rvb is not None:
                per.setdefault(sid, []).append(1.0 if rvb > 0 else 0.0)
        rates = [sum(v) / len(v) for v in per.values() if v]
        f.caller_quality = sum(rates) / len(rates) if rates else None

    launch = settings.young_chain_launch_dates.get(chain)
    if launch:
        f.young_chain = (as_of_dt - _dt(launch + "T00:00:00Z")).days <= YOUNG_CHAIN_DAYS
    return f
```

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko/test_features.py -q`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add gecko/features.py tests/gecko/test_features.py
git commit -m "feat(gecko): point-in-time feature computation with no-lookahead guarantee"
```

---

### Task 9: Disqualifiers, F4 rule, score, rank job

**Files:**
- Create: `gecko/rank.py`
- Modify: `gecko/__main__.py` (`_load_jobs` imports `gecko.rank`)
- Test: `tests/gecko/test_rank.py`

**Interfaces:**
- Consumes: `compute_features`, `Store.active_watchlist/watchlist_upsert/watchlist_exit/schedule_returns/set_token_status`, `GeckoSettings`.
- Produces:
  - `disqualify(f: Features, settings) -> str | None` returning one of `"security_fail" | "concentration" | "liquidity" | "dead"` or None, checked in that order.
  - `rule_f4(f: Features, settings) -> bool`
  - `score(f: Features, stats: dict[str, tuple[float, float]]) -> float` where `stats[key] = (mean, std)` over the candidate set for keys `vol_7d_ratio, holder_growth_7d, smart_money_wallets, caller_signal`; `caller_signal = callers_7d * (caller_quality or 0)`; missing feature → 0 contribution; `-1.0` if `bundle_flag`.
  - `async rank_once(store, settings, as_of: str) -> RankReport(entered: list[str], exited: list[str], ranked: int)`
  - job `rank` registered in `JOBS`.
- Spec deviation (recorded): `security_ok is None` is allowed into F4 while enrichment (Plan 2) does not exist; `GeckoSettings.require_security_known: bool = False` is added and Plan 3 sets it `True` at cutover.

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_rank.py`:
```python
import pytest
from gecko.features import Features
from gecko.rank import disqualify, rule_f4, score, rank_once

A = "2026-10-15T00:00:00Z"


def feat(**kw) -> Features:
    base = dict(token_id="solana:x", as_of=A, observed_at=A, liquidity_usd=300_000.0,
                age_days=30.0, vol_7d_ratio=2.5, security_ok=True, bundle_flag=False)
    base.update(kw)
    return Features(**base)


def test_disqualify_which_gate(settings):
    assert disqualify(feat(security_ok=False), settings) == "security_fail"
    assert disqualify(feat(top10_pct=61.0), settings) == "concentration"
    assert disqualify(feat(liquidity_usd=100.0), settings) == "liquidity"
    assert disqualify(feat(observed_at="2026-10-01T00:00:00Z"), settings) == "dead"
    assert disqualify(feat(), settings) is None


def test_f4_boundaries(settings):
    assert rule_f4(feat(age_days=90.0, vol_7d_ratio=2.0), settings) is True
    assert rule_f4(feat(age_days=91.0), settings) is False
    assert rule_f4(feat(vol_7d_ratio=1.9), settings) is False
    assert rule_f4(feat(bundle_flag=True), settings) is False


def test_f4_rejects_when_ratio_missing(settings):
    assert rule_f4(feat(vol_7d_ratio=None), settings) is False


def test_f4_security_unknown_policy(settings):
    assert rule_f4(feat(security_ok=None), settings) is True
    settings.require_security_known = True
    assert rule_f4(feat(security_ok=None), settings) is False


def test_score_missing_contributes_zero_and_bundle_penalty():
    stats = {"vol_7d_ratio": (2.0, 1.0), "holder_growth_7d": (0.1, 0.1),
             "smart_money_wallets": (2.0, 2.0), "caller_signal": (1.0, 1.0)}
    s1 = score(feat(vol_7d_ratio=3.0), stats)          # z=1, rest missing -> 1.0
    s2 = score(feat(vol_7d_ratio=3.0, bundle_flag=True), stats)
    assert s1 == pytest.approx(1.0) and s2 == pytest.approx(0.0)


async def seed_token(store, tid, *, age_days=30, liq=300_000.0, vols=None):
    import datetime as dt
    as_of = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    born = (as_of - dt.timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    chain, addr = tid.split(":")
    await store.upsert_token(tid, chain, addr, addr.upper(), addr, born, "dexscreener", pool_created_at=born)
    await store.set_token_status(tid, "active")
    await store.add_observation(tid, "dexscreener", "2026-10-14T23:00:00Z",
                                {"price_usd": 1.0, "liquidity_usd": liq, "mcap_usd": 2e6, "vol_24h_usd": 1e5})
    start = int((as_of - dt.timedelta(days=14)).timestamp())
    vols = vols or [100] * 7 + [300] * 7
    await store.add_observation(tid, "geckoterminal_ohlcv", "2026-10-15T00:00:00Z",
                                {"pool_address": "p", "candles": [[start + i * 86400, 1, 1, 1, 1, v] for i, v in enumerate(vols)]})


async def test_rank_once_enters_schedules_and_exits(store, settings):
    await seed_token(store, "solana:good")
    await seed_token(store, "solana:flat", vols=[100] * 14)
    await seed_token(store, "solana:thin", liq=1000.0)
    r = await rank_once(store, settings, A)
    assert r.entered == ["solana:good"] and r.exited == []
    wl = await store.active_watchlist()
    assert [w.token_id for w in wl] == ["solana:good"] and wl[0].rank == 1
    due = await store.due_returns("2027-01-01T00:00:00Z")
    assert sorted(d.horizon_days for d in due) == [1, 7, 30, 90, 180]
    row = await (await store.conn.execute("SELECT status, disqualify_reason FROM tokens WHERE token_id='solana:thin'")).fetchone()
    assert row == ("disqualified", "liquidity")
    # later: good token loses liquidity -> exits with reason
    await store.add_observation("solana:good", "dexscreener", "2026-10-20T00:00:00Z",
                                {"price_usd": 1.0, "liquidity_usd": 10.0})
    r2 = await rank_once(store, settings, "2026-10-20T01:00:00Z")
    assert r2.exited == ["solana:good"]
    row = await (await store.conn.execute("SELECT exit_reason FROM watchlist WHERE token_id='solana:good'")).fetchone()
    assert row[0] == "liquidity"


async def test_rank_is_sticky_when_rule_lapses(store, settings):
    await seed_token(store, "solana:good")
    await rank_once(store, settings, A)
    # ratio collapses but no disqualifier: stays on the watchlist
    import datetime as dt
    as_of = dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc)
    start = int((as_of - dt.timedelta(days=14)).timestamp())
    await store.add_observation("solana:good", "geckoterminal_ohlcv", "2026-10-22T00:00:00Z",
                                {"pool_address": "p", "candles": [[start + i * 86400, 1, 1, 1, 1, 100] for i in range(14)]})
    await store.add_observation("solana:good", "dexscreener", "2026-10-21T23:00:00Z",
                                {"price_usd": 1.0, "liquidity_usd": 300_000.0})
    r = await rank_once(store, settings, "2026-10-22T00:00:00Z")
    assert r.exited == [] and [w.token_id for w in await store.active_watchlist()] == ["solana:good"]


async def test_watchlist_cap(store, settings):
    settings.watchlist_cap = 2
    for i in range(4):
        await seed_token(store, f"solana:t{i}", vols=[100] * 7 + [300 + i] * 7)
    r = await rank_once(store, settings, A)
    assert len(r.entered) == 2
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_rank.py -q`
Expected: FAIL `ModuleNotFoundError: gecko.rank`

- [ ] **Step 3: Implement**

Add to `GeckoSettings` in `gecko/settings.py`:
```python
    require_security_known: bool = False  # Plan 3 flips to True at cutover
```

`gecko/rank.py`:
```python
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from gecko.__main__ import register
from gecko.features import Features, compute_features
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store

log = structlog.get_logger("gecko.rank")
RULE_ID = "F4"
SCORE_KEYS = ("vol_7d_ratio", "holder_growth_7d", "smart_money_wallets", "caller_signal")
WEIGHTS = {"vol_7d_ratio": 1.0, "holder_growth_7d": 1.0, "smart_money_wallets": 0.5, "caller_signal": 0.5}


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def disqualify(f: Features, settings: GeckoSettings) -> str | None:
    if f.security_ok is False:
        return "security_fail"
    if f.top10_pct is not None and f.top10_pct > settings.top10_disqualify_pct:
        return "concentration"
    if f.liquidity_usd is None or f.liquidity_usd < settings.min_liquidity_usd:
        return "liquidity"
    if f.observed_at is None or _dt(f.as_of) - _dt(f.observed_at) > timedelta(days=settings.dead_after_days):
        return "dead"
    return None


def rule_f4(f: Features, settings: GeckoSettings) -> bool:
    if f.age_days is None or f.age_days > settings.f4_max_age_days:
        return False
    if f.vol_7d_ratio is None or f.vol_7d_ratio < settings.f4_min_vol_ratio:
        return False
    if f.security_ok is False or (f.security_ok is None and settings.require_security_known):
        return False
    return not f.bundle_flag


def _raw(f: Features) -> dict[str, float | None]:
    return {"vol_7d_ratio": f.vol_7d_ratio, "holder_growth_7d": f.holder_growth_7d,
            "smart_money_wallets": f.smart_money_wallets,
            "caller_signal": (f.callers_7d * (f.caller_quality or 0.0)) if f.callers_7d else None}


def stats_for(feats: list[Features]) -> dict[str, tuple[float, float]]:
    out = {}
    for k in SCORE_KEYS:
        vals = [v for v in (_raw(f)[k] for f in feats) if v is not None]
        mean = statistics.fmean(vals) if vals else 0.0
        std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        out[k] = (mean, std)
    return out


def score(f: Features, stats: dict[str, tuple[float, float]]) -> float:
    s = 0.0
    raw = _raw(f)
    for k in SCORE_KEYS:
        v = raw[k]
        mean, std = stats[k]
        if v is None or std == 0:
            continue
        s += WEIGHTS[k] * (v - mean) / std
    if f.bundle_flag:
        s -= 1.0
    return s


@dataclass
class RankReport:
    entered: list[str]
    exited: list[str]
    ranked: int


async def rank_once(store: Store, settings: GeckoSettings, as_of: str) -> RankReport:
    ids = [r[0] for r in await (await store.conn.execute(
        "SELECT token_id FROM tokens WHERE status='active'")).fetchall()]
    feats: dict[str, Features] = {}
    for tid in ids:
        f = await compute_features(store, settings, tid, as_of)
        why = disqualify(f, settings)
        if why:
            await store.set_token_status(tid, "dead" if why == "dead" else "disqualified", why)
            continue
        feats[tid] = f

    current = {w.token_id: w for w in await store.active_watchlist()}
    exited: list[str] = []
    for tid, w in current.items():
        f = feats.get(tid)
        if f is None:
            row = await (await store.conn.execute(
                "SELECT disqualify_reason FROM tokens WHERE token_id=?", (tid,))).fetchone()
            reason = (row[0] if row and row[0] else "disqualified")
            await store.watchlist_exit(tid, as_of, reason)
            exited.append(tid)
        elif (_dt(as_of) - _dt(w.entered_at)).days > settings.watchlist_max_age_days:
            await store.watchlist_exit(tid, as_of, "max_age")
            exited.append(tid)
    for tid in exited:
        current.pop(tid, None)

    admitted = {tid for tid, f in feats.items() if rule_f4(f, settings)}
    pool = set(current) | admitted
    stats = stats_for([feats[t] for t in pool])
    ordered = sorted(pool, key=lambda t: score(feats[t], stats), reverse=True)
    entered: list[str] = []
    rank = 0
    for tid in ordered:
        is_new = tid not in current
        if is_new and rank >= settings.watchlist_cap:
            break
        rank += 1
        await store.watchlist_upsert(tid, as_of, RULE_ID, rank, score(feats[tid], stats),
                                     feats[tid].as_dict())
        if is_new:
            await store.schedule_returns(tid, as_of)
            entered.append(tid)
    log.info("rank_done", as_of=as_of, ranked=rank, entered=len(entered), exited=len(exited))
    return RankReport(entered, exited, rank)


@register("rank")
async def rank_job(settings: GeckoSettings, ns) -> int:
    as_of = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store, session):
        r = await rank_once(store, settings, as_of)
        return JobResult(rows_written=r.ranked)

    return await run_job("rank", settings, body)
```

Add `import gecko.rank  # noqa: F401` to `_load_jobs()` in `gecko/__main__.py`.

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: all passed (test_rank 8)

- [ ] **Step 5: Commit**

```bash
git add gecko/rank.py gecko/settings.py gecko/__main__.py tests/gecko/test_rank.py
git commit -m "feat(gecko): disqualifiers, pre-registered F4 rule, z-score ordering and sticky watchlist"
```

---
### Task 10: Forward-return ledger

**Files:**
- Create: `gecko/ledger.py`
- Modify: `gecko/__main__.py` (`_load_jobs` imports `gecko.ledger`)
- Test: `tests/gecko/test_ledger.py`

**Interfaces:**
- Consumes: `Store.due_returns/set_return/bump_return_attempt/observations/set_token_status`, `gecko.http.get_json` (Task 5), `benchmark:btc` observations written by Task 7's hourly job.
- Produces:
  - `async entry_price(store, token_id, entered_at) -> float | None` — price of the FIRST market observation with `observed_at >= entered_at` (ascending), never a later one.
  - `async btc_price_at(store, ts) -> float | None` — `benchmark:btc` observation nearest to `ts` within ±6 h (either side), else None.
  - `async fetch_price(session, settings, chain, address) -> float | None` — DexScreener `GET https://api.dexscreener.com/tokens/v1/{chain}/{address}`; returns `priceUsd` of the pair with the highest `liquidity.usd`; None on 404/empty/non-numeric.
  - `async measure_due(store, settings, session, now) -> LedgerReport(measured: int, failed: int, total_loss: int)`
  - job `ledger` registered.
  - Rule: after `MAX_ATTEMPTS = 3` failed fetches, the horizon is recorded with `price=0.0` ⇒ `ret = -1.0`, and the token's status becomes `dead` with reason `no_price`.

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_ledger.py`:
```python
import aiohttp
import pytest
from aioresponses import aioresponses
from gecko.ledger import entry_price, btc_price_at, fetch_price, measure_due, MAX_ATTEMPTS

E = "2026-10-01T00:00:00Z"
DS = "https://api.dexscreener.com/tokens/v1/solana/abc"


async def seed(store):
    await store.upsert_token("solana:abc", "solana", "abc", "ABC", "Abc", "2026-09-20T00:00:00Z", "dexscreener")
    await store.set_token_status("solana:abc", "active")
    await store.add_observation("solana:abc", "dexscreener", "2026-09-30T23:00:00Z", {"price_usd": 0.5})
    await store.add_observation("solana:abc", "dexscreener", "2026-10-01T00:30:00Z", {"price_usd": 1.0})
    await store.add_observation("solana:abc", "dexscreener", "2026-10-01T05:00:00Z", {"price_usd": 4.0})
    await store.upsert_token("benchmark:btc", "benchmark", "btc", "BTC", "Bitcoin", E, "coingecko")
    await store.add_observation("benchmark:btc", "coingecko", "2026-10-01T01:00:00Z", {"price_usd": 60000.0})
    await store.add_observation("benchmark:btc", "coingecko", "2026-10-02T01:00:00Z", {"price_usd": 66000.0})
    await store.watchlist_upsert("solana:abc", E, "F4", 1, 1.0, {})
    await store.schedule_returns("solana:abc", E)


async def test_entry_price_is_first_post_entry(store):
    await seed(store)
    assert await entry_price(store, "solana:abc", E) == 1.0


async def test_btc_nearest_within_window(store):
    await seed(store)
    assert await btc_price_at(store, "2026-10-01T00:00:00Z") == 60000.0
    assert await btc_price_at(store, "2026-10-05T00:00:00Z") is None


async def test_fetch_price_picks_deepest_pair(settings):
    with aioresponses() as m:
        m.get(DS, payload=[
            {"priceUsd": "1.5", "liquidity": {"usd": 1000}},
            {"priceUsd": "1.9", "liquidity": {"usd": 90000}},
            {"priceUsd": None, "liquidity": {"usd": 999999}},
        ])
        async with aiohttp.ClientSession() as s:
            assert await fetch_price(s, settings, "solana", "abc") == 1.9


async def test_fetch_price_none_on_404(settings):
    with aioresponses() as m:
        m.get(DS, status=404)
        async with aiohttp.ClientSession() as s:
            assert await fetch_price(s, settings, "solana", "abc") is None


async def test_measure_due_writes_return_vs_btc(store, settings):
    await seed(store)
    with aioresponses() as m:
        m.get(DS, payload=[{"priceUsd": "2.0", "liquidity": {"usd": 5}}])
        async with aiohttp.ClientSession() as s:
            r = await measure_due(store, settings, s, "2026-10-02T02:00:00Z")
    assert (r.measured, r.failed, r.total_loss) == (1, 0, 0)
    row = await (await store.conn.execute(
        "SELECT entry_price, price, btc_entry, btc_price, ret, ret_vs_btc FROM forward_returns "
        "WHERE horizon_days=1")).fetchone()
    assert row[:4] == (1.0, 2.0, 60000.0, 66000.0)
    assert row[4] == pytest.approx(1.0) and row[5] == pytest.approx(0.9)


async def test_missing_price_counts_as_total_loss(store, settings):
    await seed(store)
    with aioresponses() as m:
        for _ in range(MAX_ATTEMPTS):
            m.get(DS, status=404)
        async with aiohttp.ClientSession() as s:
            reports = [await measure_due(store, settings, s, "2026-10-02T02:00:00Z") for _ in range(MAX_ATTEMPTS)]
    assert [r.failed for r in reports] == [1, 1, 0] and reports[-1].total_loss == 1
    row = await (await store.conn.execute(
        "SELECT ret, price FROM forward_returns WHERE horizon_days=1")).fetchone()
    assert row == (-1.0, 0.0)
    st = await (await store.conn.execute("SELECT status, disqualify_reason FROM tokens WHERE token_id='solana:abc'")).fetchone()
    assert st == ("dead", "no_price")
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_ledger.py -q`
Expected: FAIL `ModuleNotFoundError: gecko.ledger`

- [ ] **Step 3: Implement `gecko/ledger.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp
import structlog

from gecko.__main__ import register
from gecko.features import MARKET_SOURCES
from gecko.http import get_json
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store

log = structlog.get_logger("gecko.ledger")
MAX_ATTEMPTS = 3
BTC_WINDOW_HOURS = 6
DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/{chain}/{address}"


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def entry_price(store: Store, token_id: str, entered_at: str) -> float | None:
    q = ",".join("?" * len(MARKET_SOURCES))
    rows = await (await store.conn.execute(
        f"SELECT features_json FROM observations WHERE token_id=? AND source IN ({q}) "
        f"AND observed_at>=? ORDER BY observed_at ASC LIMIT 20",
        (token_id, *MARKET_SOURCES, entered_at))).fetchall()
    import json
    for (fj,) in rows:
        p = json.loads(fj).get("price_usd")
        if p:
            return float(p)
    return None


async def btc_price_at(store: Store, ts: str) -> float | None:
    t = _dt(ts)
    lo = (t - timedelta(hours=BTC_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (t + timedelta(hours=BTC_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = await (await store.conn.execute(
        "SELECT observed_at, features_json FROM observations WHERE token_id='benchmark:btc' "
        "AND observed_at BETWEEN ? AND ?", (lo, hi))).fetchall()
    import json
    best = None
    for obs_at, fj in rows:
        p = json.loads(fj).get("price_usd")
        if p is None:
            continue
        d = abs((_dt(obs_at) - t).total_seconds())
        if best is None or d < best[0]:
            best = (d, float(p))
    return best[1] if best else None


async def fetch_price(session: aiohttp.ClientSession, settings: GeckoSettings,
                      chain: str, address: str) -> float | None:
    data = await get_json(session, DEXSCREENER_TOKENS.format(chain=chain, address=address),
                          settings=settings, ok_statuses=(200, 404))
    if not isinstance(data, list):
        return None
    best = None
    for pair in data:
        try:
            price = float(pair.get("priceUsd")) if pair.get("priceUsd") is not None else None
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            continue
        if price is None:
            continue
        if best is None or liq > best[0]:
            best = (liq, price)
    return best[1] if best else None


@dataclass
class LedgerReport:
    measured: int = 0
    failed: int = 0
    total_loss: int = 0


async def measure_due(store: Store, settings: GeckoSettings, session: aiohttp.ClientSession,
                      now: str) -> LedgerReport:
    rep = LedgerReport()
    for due in await store.due_returns(now):
        trow = await (await store.conn.execute(
            "SELECT chain, address FROM tokens WHERE token_id=?", (due.token_id,))).fetchone()
        wrow = await (await store.conn.execute(
            "SELECT entered_at FROM watchlist WHERE token_id=?", (due.token_id,))).fetchone()
        if not trow or not wrow:
            continue
        chain, address = trow
        entered_at = wrow[0]
        ep = await entry_price(store, due.token_id, entered_at)
        price = await fetch_price(session, settings, chain, address)
        if price is None:
            attempts = await store.bump_return_attempt(due.token_id, due.horizon_days)
            if attempts >= MAX_ATTEMPTS:
                await store.set_return(due.token_id, due.horizon_days, ep, 0.0,
                                       await btc_price_at(store, entered_at),
                                       await btc_price_at(store, now), now)
                await store.set_token_status(due.token_id, "dead", "no_price")
                rep.total_loss += 1
            else:
                rep.failed += 1
            continue
        await store.set_return(due.token_id, due.horizon_days, ep, price,
                               await btc_price_at(store, entered_at), await btc_price_at(store, now), now)
        rep.measured += 1
    log.info("ledger_done", **rep.__dict__)
    return rep


@register("ledger")
async def ledger_job(settings: GeckoSettings, ns) -> int:
    now = ns.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def body(store, session):
        r = await measure_due(store, settings, session, now)
        return JobResult(rows_written=r.measured + r.total_loss)

    return await run_job("ledger", settings, body)
```

Note: `set_return` with `price=0.0` yields `ret = 0/entry − 1 = −1.0` (Task 2 formula) when `entry_price` is known; if `entry_price` is None, `ret` stays NULL and the row is still marked measured — the scorecard (Plan 3) counts NULL-ret rows as `unmeasurable`.

Add `import gecko.ledger  # noqa: F401` to `_load_jobs()`.

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: all passed (test_ledger 6)

- [ ] **Step 5: Commit**

```bash
git add gecko/ledger.py gecko/__main__.py tests/gecko/test_ledger.py
git commit -m "feat(gecko): forward-return ledger at 1/7/30/90/180d vs BTC with total-loss rule"
```

---

### Task 11: Backtest harness anchored to the 2026-09-18 study

**Files:**
- Create: `gecko/backtest/__init__.py`, `gecko/backtest/study.py`, `gecko/backtest/runner.py`
- Modify: `gecko/__main__.py` (`_load_jobs` imports `gecko.backtest.runner`)
- Test: `tests/gecko/test_backtest.py` with a synthetic study directory built in `tmp_path`

**Study data formats (on the VPS at `/root/xs_study`, copied read-only):**
- `events.json`: `{cg_id: [[iso_ts, surface], ...]}` (1,800 coins). `t0 = min(ts)`.
- `charts/{cg_id}.json`: CoinGecko `market_chart` → `{"prices": [[ms, usd], ...], "market_caps": [...], "total_volumes": [...]}` daily points.
- `btc.json`: same shape for bitcoin.
- `v2/coins/{cg_id}.json`: `{cid, t0, platforms: [..], net, platform, pool, pool_created_at (ISO Z), ohlcv: [[ts_sec,o,h,l,c,v],...] or null, ohlcv_status}`.
- Stored flag-time prices come from the historical `scout.db`: `predictions(coin_id, predicted_at, price_at_prediction)`, `paper_trades(token_id, opened_at, entry_price)`, `volume_spikes(coin_id, detected_at, price)`, `velocity_alerts`, `slow_burn_candidates`, `gainer_acceleration`, `momentum_7d` (`coin_id, detected_at, current_price`) — earliest per coin.
- Study rules reproduced exactly: price at t0 = chart point nearest t0 (dead if > 36 h away or ≤ 0); `x = last_price / p0`; `bx = btc_last / btc_at_t0`; exclude coins whose `p0 / stored_price` is outside `[0.5, 2.0]`; feature-missing coins are excluded from rules that need the feature; five pass criteria (mean > 1 and > BTC; mean ex-best > BTC; mean > same-month R0 mean; both halves split at 2026-07-01 (or median t0 if a half < 20) beat BTC; 20 ≤ n ≤ 200).

**Interfaces:**
- `load_study(study_dir: str, history_db: str | None) -> list[Coin]` with `Coin(cid, t0: str, dead: bool, p0, m0, x, bx, surf: set[str], age_days: float | None, vol_ratio: float | None, chain: str)`.
- `apply_rule(coins, rule_id: str, settings) -> list[Coin]` for `rule_id in {"R0","F1","F3","F4"}` using `gecko.features.vol_ratio_from_candles` for `vol_ratio` and `settings.f4_*` thresholds.
- `battery(selected, all_live, btc_split="2026-07-01") -> Battery(n, mean, median, btc_mean, ex_best, ex_top3, same_month_r0, pct_7x, pct_half, halves, passes: list[bool], passed: bool)`.
- job `backtest` registered: `python -m gecko backtest --rule F4 --study-dir ... --history-db ...` prints a one-table report and exits 0 (1 on load error).

- [ ] **Step 1: Write failing tests**

`tests/gecko/test_backtest.py`:
```python
import json
import sqlite3
import pytest
from gecko.backtest.study import load_study, apply_rule, battery

DAY = 86400


def write_study(tmp_path, coins: dict):
    """coins: cid -> dict(t0, prices:[(day_offset, price)], stored, age_days, vols:list[14])"""
    base_ms = 1_775_001_600_000  # 2026-04-01T00:00:00Z
    (tmp_path / "charts").mkdir()
    (tmp_path / "v2" / "coins").mkdir(parents=True)
    events, btc = {}, {"prices": [[base_ms + d * DAY * 1000, 100.0 + d] for d in range(200)]}
    db = sqlite3.connect(tmp_path / "scout.db")
    db.execute("CREATE TABLE predictions(coin_id TEXT, predicted_at TEXT, price_at_prediction REAL)")
    for cid, c in coins.items():
        events[cid] = [[c["t0"], "prediction"]]
        json.dump({"prices": [[base_ms + d * DAY * 1000, p] for d, p in c["prices"]]},
                  open(tmp_path / "charts" / f"{cid}.json", "w"))
        db.execute("INSERT INTO predictions VALUES(?,?,?)", (cid, c["t0"], c["stored"]))
        t0_ms = base_ms + c["t0_day"] * DAY * 1000
        pc = (t0_ms - c["age_days"] * DAY * 1000) // 1000
        start = t0_ms // 1000 - 14 * DAY
        json.dump({"cid": cid, "t0": c["t0"], "platforms": ["solana"], "net": "solana",
                   "pool_created_at": __import__("datetime").datetime.utcfromtimestamp(pc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "ohlcv": [[start + i * DAY, 1, 1, 1, 1, v] for i, v in enumerate(c["vols"])]},
                  open(tmp_path / "v2" / "coins" / f"{cid}.json", "w"))
    db.commit(); db.close()
    json.dump(events, open(tmp_path / "events.json", "w"))
    json.dump(btc, open(tmp_path / "btc.json", "w"))


def coin(t0_day, p0, x, *, stored=None, age_days=30, vols=None):
    t0 = __import__("datetime").datetime.utcfromtimestamp(1_775_001_600 + t0_day * DAY).strftime("%Y-%m-%dT%H:%M:%S")
    return dict(t0=t0, t0_day=t0_day, prices=[(t0_day, p0), (199, p0 * x)], stored=stored or p0,
                age_days=age_days, vols=vols or [100] * 7 + [300] * 7)


@pytest.fixture
def study(tmp_path, settings):
    write_study(tmp_path, {
        "winner": coin(10, 1.0, 8.0),
        "loser": coin(12, 1.0, 0.4),
        "old": coin(14, 1.0, 3.0, age_days=200),
        "flat": coin(16, 1.0, 2.0, vols=[100] * 14),
        "badchart": coin(18, 1.0, 50.0, stored=0.01),
        "late": coin(120, 1.0, 2.5),
    })
    return load_study(str(tmp_path), str(tmp_path / "scout.db"))


def test_load_excludes_chart_disagreement_and_sets_features(study):
    cids = {c.cid for c in study}
    assert "badchart" not in cids and len(study) == 5
    w = next(c for c in study if c.cid == "winner")
    assert w.x == pytest.approx(8.0) and w.age_days == pytest.approx(30, abs=0.01) and w.vol_ratio == pytest.approx(3.0)
    assert w.bx == pytest.approx((100 + 199) / (100 + 10))


def test_rules(study, settings):
    f4 = {c.cid for c in apply_rule(study, "F4", settings)}
    assert f4 == {"winner", "loser", "late"}
    assert {c.cid for c in apply_rule(study, "F1", settings)} == {"winner", "loser", "flat", "late"}
    assert {c.cid for c in apply_rule(study, "F3", settings)} == {"winner", "loser", "old", "late"}


def test_battery_reports_which_criteria_fail(study, settings):
    sel = apply_rule(study, "F4", settings)
    b = battery(sel, study)
    assert b.n == 3 and b.mean == pytest.approx((8.0 + 0.4 + 2.5) / 3)
    assert b.passes[4] is False  # n < 20
    assert b.passed is False
    assert b.ex_best == pytest.approx((0.4 + 2.5) / 2)
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_backtest.py -q`
Expected: FAIL `ModuleNotFoundError: gecko.backtest`

- [ ] **Step 3: Implement**

`gecko/backtest/__init__.py`: empty.

`gecko/backtest/study.py`:
```python
from __future__ import annotations

import bisect
import json
import os
import sqlite3
import statistics as st
from dataclasses import dataclass, field
from datetime import datetime, timezone

from gecko.features import vol_ratio_from_candles
from gecko.settings import GeckoSettings

STORED_QUERIES = [
    "SELECT coin_id, predicted_at, price_at_prediction FROM predictions",
    "SELECT token_id, opened_at, entry_price FROM paper_trades WHERE opened_at>'2020'",
    "SELECT coin_id, detected_at, current_price FROM velocity_alerts",
    "SELECT coin_id, detected_at, current_price FROM slow_burn_candidates",
    "SELECT coin_id, detected_at, current_price FROM gainer_acceleration",
    "SELECT coin_id, detected_at, current_price FROM momentum_7d",
    "SELECT coin_id, detected_at, price FROM volume_spikes",
]
CHART_TOLERANCE_MS = 36 * 3600 * 1000
STORED_BAND = (0.5, 2.0)


@dataclass
class Coin:
    cid: str
    t0: str
    dead: bool
    p0: float = 0.0
    m0: float = 0.0
    x: float = 0.0
    bx: float = 0.0
    surf: set = field(default_factory=set)
    age_days: float | None = None
    vol_ratio: float | None = None
    chain: str = "none"


def _ms(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "")).replace(tzinfo=timezone.utc).timestamp() * 1000


def _at(series: list, times: list, t: float):
    i = bisect.bisect_left(times, t)
    i = min(max(i, 0), len(series) - 1)
    if i > 0 and abs(times[i - 1] - t) < abs(times[i] - t):
        i -= 1
    return series[i]


def _stored_prices(history_db: str | None) -> dict[str, tuple[str, float]]:
    out: dict[str, tuple[str, float]] = {}
    if not history_db or not os.path.exists(history_db):
        return out
    conn = sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)
    for q in STORED_QUERIES:
        try:
            rows = conn.execute(q).fetchall()
        except sqlite3.OperationalError:
            continue
        for cid, ts, p in rows:
            if cid and p and p > 0:
                ts = str(ts).replace(" ", "T")[:19]
                if cid not in out or ts < out[cid][0]:
                    out[cid] = (ts, float(p))
    conn.close()
    return out


def load_study(study_dir: str, history_db: str | None) -> list[Coin]:
    ev = json.load(open(os.path.join(study_dir, "events.json")))
    btc = json.load(open(os.path.join(study_dir, "btc.json")))["prices"]
    bt = [p[0] for p in btc]
    stored = _stored_prices(history_db)
    coins: list[Coin] = []
    for cid, e in ev.items():
        f = os.path.join(study_dir, "charts", f"{cid}.json")
        if not os.path.exists(f):
            continue
        d = json.load(open(f))
        pr = d.get("prices") or []
        mc = d.get("market_caps") or []
        t0s = min(x[0] for x in e)
        if len(pr) < 2:
            coins.append(Coin(cid, t0s, True))
            continue
        t0 = _ms(t0s)
        pt = [p[0] for p in pr]
        p0 = _at(pr, pt, t0)
        if abs(p0[0] - t0) > CHART_TOLERANCE_MS or p0[1] <= 0:
            coins.append(Coin(cid, t0s, True))
            continue
        if cid in stored and not (STORED_BAND[0] <= p0[1] / stored[cid][1] <= STORED_BAND[1]):
            continue  # chart/stored disagreement > 2x: excluded (study rule)
        m0 = _at(mc, [m[0] for m in mc], t0)[1] if mc else 0.0
        b0 = _at(btc, bt, t0)[1]
        c = Coin(cid, t0s, False, p0=p0[1], m0=m0 or 0.0, x=pr[-1][1] / p0[1],
                 bx=btc[-1][1] / b0, surf={x[1] for x in e})
        v2 = os.path.join(study_dir, "v2", "coins", f"{cid}.json")
        if os.path.exists(v2):
            r = json.load(open(v2))
            pf = r.get("platforms") or []
            c.chain = pf[0] if len(pf) == 1 else ("multi" if pf else "none")
            if r.get("pool_created_at"):
                c.age_days = (t0 - _ms(r["pool_created_at"])) / 86400000
            if r.get("ohlcv"):
                c.vol_ratio = vol_ratio_from_candles(r["ohlcv"], datetime.fromtimestamp(t0 / 1000, tz=timezone.utc))
        coins.append(c)
    return [c for c in coins if not c.dead]


def apply_rule(coins: list[Coin], rule_id: str, settings: GeckoSettings) -> list[Coin]:
    f1 = lambda c: c.age_days is not None and c.age_days <= settings.f4_max_age_days  # noqa: E731
    f3 = lambda c: c.vol_ratio is not None and c.vol_ratio >= settings.f4_min_vol_ratio  # noqa: E731
    rules = {"R0": lambda c: True, "F1": f1, "F3": f3, "F4": lambda c: f1(c) and f3(c)}
    return [c for c in coins if rules[rule_id](c)]


@dataclass
class Battery:
    n: int
    mean: float
    median: float
    btc_mean: float
    ex_best: float
    ex_top3: float
    same_month_r0: float
    pct_7x: float
    pct_half: float
    halves: str
    passes: list[bool]
    passed: bool


def battery(selected: list[Coin], universe: list[Coin], split: str = "2026-07-01") -> Battery:
    if len(selected) < 2:
        return Battery(len(selected), 0, 0, 0, 0, 0, 0, 0, 0, "-", [False] * 5, False)
    xs = sorted(c.x for c in selected)
    m = st.fmean(xs)
    btc = st.fmean(c.bx for c in selected)
    month_mean = {mo: st.fmean(c.x for c in universe if c.t0[:7] == mo) for mo in {c.t0[:7] for c in universe}}
    base = st.fmean(month_mean[c.t0[:7]] for c in selected)
    h1 = [c for c in selected if c.t0 < split]
    h2 = [c for c in selected if c.t0 >= split]
    label = split
    if len(h1) < 20 or len(h2) < 20:
        med = sorted(c.t0 for c in selected)[len(selected) // 2]
        h1 = [c for c in selected if c.t0 < med]
        h2 = [c for c in selected if c.t0 >= med]
        label = "median " + med[:10]
    hm = lambda v: st.fmean(c.x for c in v) if v else 0.0  # noqa: E731
    hb = lambda v: st.fmean(c.bx for c in v) if v else 0.0  # noqa: E731
    passes = [
        m > 1.0 and m > btc,
        st.fmean(xs[:-1]) > btc,
        m > base,
        bool(h1 and h2 and hm(h1) > hb(h1) and hm(h2) > hb(h2)),
        20 <= len(selected) <= 200,
    ]
    halves = f"[{label}] {hm(h1):.2f}/{hb(h1):.2f} n={len(h1)} | {hm(h2):.2f}/{hb(h2):.2f} n={len(h2)}"
    return Battery(len(selected), m, st.median(xs), btc, st.fmean(xs[:-1]),
                   st.fmean(xs[:-3]) if len(xs) > 3 else st.fmean(xs), base,
                   100 * sum(a >= 7 for a in xs) / len(xs), 100 * sum(a <= 0.5 for a in xs) / len(xs),
                   halves, passes, all(passes))
```

`gecko/backtest/runner.py`:
```python
from __future__ import annotations

from gecko.__main__ import register
from gecko.backtest.study import apply_rule, battery, load_study
from gecko.settings import GeckoSettings


@register("backtest")
async def backtest_job(settings: GeckoSettings, ns) -> int:
    try:
        coins = load_study(ns.study_dir, ns.history_db)
    except (OSError, ValueError, KeyError) as exc:
        print(f"backtest: cannot load study: {exc}")
        return 1
    sel = apply_rule(coins, ns.rule, settings)
    b = battery(sel, coins)
    print("rule | n | mean | median | BTC | ex-best | ex-top3 | same-month R0 | %>=7x | %<=0.5x | halves | PASS | failed")
    failed = ",".join(str(i + 1) for i, p in enumerate(b.passes) if not p)
    print(f"{ns.rule} | {b.n} | {b.mean:.2f}x | {b.median:.2f}x | {b.btc_mean:.2f}x | {b.ex_best:.2f}x | "
          f"{b.ex_top3:.2f}x | {b.same_month_r0:.2f}x | {b.pct_7x:.1f}% | {b.pct_half:.0f}% | {b.halves} | "
          f"{'PASS' if b.passed else 'fail'} | {failed}")
    return 0
```

Add `import gecko.backtest.runner  # noqa: F401` to `_load_jobs()`.

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q`
Expected: all passed (test_backtest 3)

- [ ] **Step 5: Regression anchor against the real study (on the VPS, read-only)**

```bash
ssh srilu-vps 'cd /root/gecko-alpha && python3 -m gecko backtest --rule F4 --study-dir /root/xs_study --history-db /root/gecko-alpha/scout.db' > .ssh_out_backtest.txt 2>&1
```
Read the file. Expected within tolerance of the 2026-09-18 result: `n` 101 ± 5, mean 1.91x ± 0.15, BTC 1.07x ± 0.05, ex-top3 ≈ 0.75x, median ≈ 0.53x, PASS. If outside tolerance, the harness is wrong, not the study: diff `load_study` against `/root/xs_study/analyze.py` + `rerun.py` and fix before committing. Record the printed row in `docs/prereg/F4.md` (create: rule text, hash `f7d38fa2`, universe, criteria, this anchor row, the fragility caveats from the spec §7).

- [ ] **Step 6: Commit**

```bash
git add gecko/backtest gecko/__main__.py tests/gecko/test_backtest.py docs/prereg/F4.md
git commit -m "feat(gecko): backtest harness reproducing the 2026-09-18 long-hold study battery"
```

---

### Task 12: Wire CI, line-count guard, and the `tests/gecko` collection check

**Files:**
- Modify: `.github/workflows/test.yml` (add a `gecko` job running `uv run pytest tests/gecko -q` and the guard below)
- Create: `scripts/gecko_loc_guard.py`
- Test: `tests/gecko/test_loc_guard.py`

**Interfaces:**
- `scripts/gecko_loc_guard.py`: exits 1 if `gecko/**/*.py` non-blank, non-comment lines > 2,500 or `tests/gecko/**/*.py` > 5,000; prints both counts. This encodes spec S1 as a runtime contract (anti-scope as CI gate, project lesson).

- [ ] **Step 1: Write failing test**

`tests/gecko/test_loc_guard.py`:
```python
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_guard_passes_on_current_tree():
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "gecko_loc_guard.py")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "gecko=" in p.stdout and "tests=" in p.stdout


def test_guard_fails_when_limit_exceeded(tmp_path):
    big = tmp_path / "gecko"
    big.mkdir()
    (big / "x.py").write_text("\n".join(f"a{i} = {i}" for i in range(2600)))
    (tmp_path / "tests" / "gecko").mkdir(parents=True)
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "gecko_loc_guard.py"), "--root", str(tmp_path)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 1
```

- [ ] **Step 2: Run, expect failure**

Run: `python -m pytest tests/gecko/test_loc_guard.py -q`
Expected: FAIL (script missing → FileNotFoundError / returncode 2)

- [ ] **Step 3: Implement `scripts/gecko_loc_guard.py`**

```python
#!/usr/bin/env python3
"""Spec S1 guard: gecko/ <= 2500 code lines, tests/gecko <= 5000. Exit 1 on breach."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

LIMITS = {"gecko": 2500, "tests/gecko": 5000}


def count(root: Path, rel: str) -> int:
    n = 0
    for f in (root / rel).rglob("*.py"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    root = Path(ap.parse_args().root)
    g, t = count(root, "gecko"), count(root, "tests/gecko")
    print(f"gecko={g}/{LIMITS['gecko']} tests={t}/{LIMITS['tests/gecko']}")
    return 1 if g > LIMITS["gecko"] or t > LIMITS["tests/gecko"] else 0


if __name__ == "__main__":
    sys.exit(main())
```

Add to `.github/workflows/test.yml` a job:
```yaml
  gecko:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv python install 3.12
      - run: uv sync --all-extras
      - run: uv run pytest tests/gecko -q --timeout=120
      - run: uv run python scripts/gecko_loc_guard.py
```

- [ ] **Step 4: Run, expect pass**

Run: `python -m pytest tests/gecko -q && python scripts/gecko_loc_guard.py`
Expected: all passed; guard prints counts, exit 0

- [ ] **Step 5: Commit, push, open PR**

```bash
git add scripts/gecko_loc_guard.py tests/gecko/test_loc_guard.py .github/workflows/test.yml
git commit -m "ci(gecko): tests job and spec-S1 line-count guard"
git push -u origin feat/gecko-lean-agent
gh pr create --title "feat(gecko): lean watchlist core package (Plan 1 of 3)" --body-file docs/superpowers/plans/2026-10-09-gecko-core-package.md
```

Wait for exact-head CI green (`gh run view --json conclusion`) before requesting review; do not read the `gh run watch` stream for X-lines.

---

## Self-review (run after writing; results recorded here)

**Spec coverage:** §4 store → Task 2; §5 DexScreener/GeckoTerminal/CoinGecko → Tasks 5-7; §6 features → Task 8; §7 rule + protocol → Task 9 + docs/prereg/F4.md (Task 11); §9 ledger → Task 10; §10 backtest → Task 11; §12 freshness → Task 3; §15 tests (point-in-time, ledger, which-gate, freshness dedup, 429/401, backtest anchor) → Tasks 8, 10, 9, 3, 7, 11; S1 guard → Task 12. Not in this plan (by design): §5 Helius/Blockscout/GoPlus/GMGN/Telegram/X collectors (Plan 2); §8 agent jobs, §11 Hermes config, §14 cutover (Plan 3).

**Placeholders:** none; every step has code and a run command.

**Type consistency:** `Store.observations(token_id, as_of=, source=, limit=)` used identically in Tasks 8, 10; `Features` field names match `rank._raw` and tests; `JobResult(rows_written, credits_used)` used by Tasks 3, 5-7, 9, 10; `register()` names: `freshness, dexscreener, geckoterminal, coingecko-markets, coingecko-hourly, rank, ledger, backtest` match `WRITER_JOBS` in Task 3 (plus `enrich`, `telegram-reader`, `xkol`, `research` owned by Plans 2-3).

**Review Focus → tests:** 1 → Task 5 `test_pair_missing_fields_skipped`; 2 → Task 4 `test_admit_requires_24h_survival`; 3 → Task 7 `test_markets_429_then_200`, `test_markets_401_fails_run`; 4 → Task 10 `test_entry_price_is_first_post_entry`, `test_missing_price_counts_as_total_loss`; 5 → Task 8 `test_vol_ratio_missing_when_short_history` + Task 9 `test_f4_rejects_when_ratio_missing`.
