**New primitives introduced:** NONE — read-only search endpoint + frontend SearchBar component over existing tables. No schema changes, no new Settings, no new background workers, no new writes. One new FastAPI GET route + one new React component + one new helper module.

# Plan: Dashboard Global Search

## Context

Operator workflow problem: when a KOL mentions a token by ticker (e.g. "CHIP"), operator currently has no way to ask the dashboard "what do we know about this token across all our data sources?" without `sqlite3` shelling into `scout.db` and writing manual `UNION ALL` queries. The motivating instance is the 2026-05-14 conversation: operator wanted to locate a token called CHIP across candidates, paper_trades, TG messages, X-narrative alerts, and signal snapshots and had to ask Claude to do it via SSH.

## Goal

Add a single global search box to the dashboard that, given a free-text query (ticker, name fragment, contract address, or coin slug), returns a grouped, ranked summary of every place that token appears in `scout.db`: ingestion snapshots, scored candidates, paper trades, Telegram social signals, X-narrative inbound alerts, and detection-signal tables (momentum_7d / slow_burn / velocity / volume_spikes / chain_matches / predictions). Each row gives the operator a one-click TokenLink to the external chart.

## Drift-check (§7a)

Grepped `dashboard/` + `scout/` for `/search`, `search?`, `q=` patterns 2026-05-14. Findings:

| File:line | What it is | Closes proposal? |
|---|---|---|
| `dashboard/api.py:444` | Fallback `for pcid, pdata in symbol_map.items(): if pcid.startswith(sym)` — substring lookup inside `gainers_comparisons` enrichment | NO — internal helper, not exposed, only searches `price_cache`. Closes 0% of proposal scope. |
| `dashboard/frontend/components/TokenLink.jsx:32` | DexScreener external URL `dexscreener.com/search?q=...` | NO — outbound link, not local search. |
| `scout/resolver/*` | `symbol_aliases` resolver (current row count: **0**) | NO — different domain (venue-symbol normalization), table is empty. |

No existing global-search primitive in tree. Proposal proceeds.

## Hermes-first analysis (§7b)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Cross-table SQLite full-text search | None on hermes-agent.nousresearch.com/docs/skills — Hermes skill hub is oriented at on-chain detection / narrative classification / KOL ingestion, not internal-dashboard search | Build from scratch (rationale: this is an internal dashboard-UX feature reading a local SQLite DB; no external service or LLM call involved). |
| Fuzzy string match / ranking | Library-level (`rapidfuzz`, sqlite `LIKE`/`GLOB`) — not a Hermes skill territory | Use stdlib `sqlite3 LIKE` with case-insensitive `COLLATE NOCASE` — no new dependency. Fuzzy ranking deferred unless review flags it. |
| Awesome-hermes-agent ecosystem | No cross-table search primitives | N/A |

**Verdict:** Hermes ecosystem does not cover internal-dashboard SQL search. Custom implementation justified. Pure-stdlib (no new pip dep).

## Scope

### In scope

1. **New backend endpoint** `GET /api/search?q=<query>&limit=<n>` (default limit=50, max 200).
2. **New module** `dashboard/search.py` — pure-SQL search service. One function per source table; orchestrator aggregates results.
3. **New Pydantic models** in `dashboard/models.py` — `SearchHit`, `SearchResponse`.
4. **New frontend component** `dashboard/frontend/components/GlobalSearch.jsx` — header-mounted search box, debounced input (300ms), keyboard shortcut `Ctrl+K` / `Cmd+K` to focus, `Esc` to clear, dropdown result panel.
5. **Tests** — `tests/test_dashboard_search.py` covering: empty query, exact symbol match, substring name match, contract-address match, no results, SQL-injection attempt (single quote in query), result ranking, limit enforcement.

### Searched sources (16 tables / view types)

| Table | Rows on prod (2026-05-14) | Search columns | Role |
|---|---|---|---|
| `candidates` | 1,617 | `token_name`, `ticker`, `contract_address` | scored token |
| `alerts` | 2 | `token_name`, `ticker`, `contract_address` | TG conviction alert |
| `paper_trades` | 1,382 | `symbol`, `name`, `token_id` | sim trade outcome |
| `gainers_snapshots` | 87,145 | `name`, `symbol`, `coin_id` | CG top gainers polling |
| `trending_snapshots` | 5,355 | `name`, `symbol`, `coin_id` | CG trending polling |
| `tg_social_messages` | 1,661 | `text` (LIKE `%$Q%` OR `%Q%`), `cashtags`, `contracts` | KOL TG mention |
| `tg_social_signals` | 747 | `symbol`, `token_id`, `contract_address` | resolved KOL signal |
| `narrative_alerts_inbound` | 6 | `extracted_cashtag`, `resolved_coin_id`, `tweet_text` (LIKE) | X/KOL inbound |
| `momentum_7d` | 455 | `symbol`, `name`, `coin_id` | 7d momentum signal |
| `slow_burn_candidates` | 105 | `symbol`, `name`, `coin_id` | slow-burn signal |
| `velocity_alerts` | 215 | `symbol`, `name`, `coin_id` | velocity signal |
| `volume_spikes` | 149 | `symbol`, `name`, `coin_id` | vol-spike signal |
| `predictions` | (varies) | `symbol`, `name`, `coin_id` | narrative prediction |
| `chain_matches` | 603 | `token_id` | chain pattern hit |
| `signal_events` | 7,502,712 | `token_id` only (NOT searched — too large for fan-out; included only if token_id resolves elsewhere) | per-token event log |
| `price_cache` | 8,181 | `coin_id` | quotation fallback |

### Out of scope

- **Full-text indexing (FTS5)** — current row counts (largest searched table = 87k gainers_snapshots) are small enough for indexed `LIKE` to stay sub-50ms. FTS5 deferred unless review or perf-test flags it.
- **Cross-VPS search** (shift-agent, Hermes side data) — separate concern.
- **Auto-complete suggestions** — deferred. Plain debounced free-text only.
- **Saved searches / search history** — deferred.
- **Authorization / rate-limiting on `/api/search`** — dashboard is already operator-only (localhost / WireGuard reachable). Same threat model as existing GET endpoints.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Frontend: GlobalSearch.jsx (in App.jsx header)                  │
│  - debounced input (300ms)                                      │
│  - Ctrl+K focus, Esc clear                                      │
│  - fetch('/api/search?q=' + encodeURIComponent(q))              │
│  - render grouped result list, each row = TokenLink + meta      │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ Backend: GET /api/search?q=&limit=                              │
│  dashboard/api.py — thin route handler, calls search.run_search │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ dashboard/search.py — orchestrator                              │
│  run_search(db_path, q, limit) -> SearchResponse                │
│   1. Normalize q (strip, lower, reject empty/whitespace-only)   │
│   2. Open read-only conn (db._ro_db pattern)                    │
│   3. Parallel `asyncio.gather()` over per-table search fns      │
│   4. Each fn returns list[SearchHit]                            │
│   5. Group by (canonical_id = contract_address or coin_id),     │
│      collapse multiple hits per entity into one SearchHit with  │
│      `sources: list[str]` and `first_seen / last_seen` window   │
│   6. Rank: exact symbol match > prefix match > substring        │
│   7. Apply limit, return SearchResponse                         │
└─────────────────────────────────────────────────────────────────┘
```

**SQL safety invariant:** every query in `search.py` uses parameterized `?` placeholders. The query string is interpolated ONLY into the `?`-bound LIKE pattern after being wrapped as `f"%{q}%"` (FastAPI/Pydantic validates length); never concatenated into raw SQL.

## File structure

- **Create**: `dashboard/search.py` (~250 LOC, async)
- **Create**: `dashboard/frontend/components/GlobalSearch.jsx` (~150 LOC)
- **Create**: `tests/test_dashboard_search.py` (~400 LOC)
- **Modify**: `dashboard/api.py` — add `@app.get("/api/search")` route (~10 LOC)
- **Modify**: `dashboard/models.py` — add `SearchHit` + `SearchResponse` models (~30 LOC)
- **Modify**: `dashboard/frontend/App.jsx` — mount `<GlobalSearch />` in header (~3 LOC)
- **Modify**: `dashboard/frontend/style.css` — search box + dropdown styling (~80 LOC)

## Tasks

### Task 1: Pydantic models

**Files:**
- Modify: `dashboard/models.py` (append)
- Test: `tests/test_dashboard_search.py` (created)

- [ ] **Step 1.1: Write the failing test**

```python
# tests/test_dashboard_search.py
from dashboard.models import SearchHit, SearchResponse


def test_search_hit_minimal():
    hit = SearchHit(
        canonical_id="0xabc",
        symbol="CHIP",
        name="ChipCoin",
        chain="solana",
        sources=["candidates"],
        first_seen_at="2026-05-14T10:00:00+00:00",
        last_seen_at="2026-05-14T10:00:00+00:00",
        match_quality="exact_symbol",
    )
    assert hit.symbol == "CHIP"
    assert hit.sources == ["candidates"]


def test_search_response_shape():
    resp = SearchResponse(query="CHIP", total_hits=0, hits=[])
    assert resp.query == "CHIP"
    assert resp.hits == []
```

- [ ] **Step 1.2: Run test — expect ImportError**

```
uv run pytest tests/test_dashboard_search.py -v
```
Expected: `ImportError: cannot import name 'SearchHit' from 'dashboard.models'`

- [ ] **Step 1.3: Implement minimal models**

```python
# Append to dashboard/models.py
class SearchHit(BaseModel):
    canonical_id: str
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None
    contract_address: str | None = None
    sources: list[str] = []
    source_counts: dict[str, int] = {}
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    match_quality: str  # "exact_symbol" | "exact_contract" | "prefix" | "substring" | "text"
    best_paper_trade_pnl_pct: float | None = None


class SearchResponse(BaseModel):
    query: str
    total_hits: int = 0
    hits: list[SearchHit] = []
    truncated: bool = False
```

- [ ] **Step 1.4: Run tests — expect PASS**

```
uv run pytest tests/test_dashboard_search.py -v
```

- [ ] **Step 1.5: Commit**

```bash
git add dashboard/models.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): add SearchHit + SearchResponse Pydantic models"
```

---

### Task 2: Query normalization + empty-query handling

**Files:**
- Create: `dashboard/search.py`
- Test: `tests/test_dashboard_search.py` (append)

- [ ] **Step 2.1: Write failing tests**

```python
# Append to tests/test_dashboard_search.py
import pytest
from dashboard.search import normalize_query, QueryTooShortError


def test_normalize_strips_whitespace():
    assert normalize_query("  CHIP  ") == "chip"


def test_normalize_strips_dollar_prefix():
    assert normalize_query("$CHIP") == "chip"


def test_normalize_keeps_contract_address_case():
    # Solana / EVM addresses are case-sensitive for display but we lowercase
    # for matching (we'll case-insensitively compare).
    assert normalize_query("0xAbC") == "0xabc"


def test_normalize_rejects_empty():
    with pytest.raises(QueryTooShortError):
        normalize_query("")


def test_normalize_rejects_single_char():
    with pytest.raises(QueryTooShortError):
        normalize_query("a")


def test_normalize_rejects_whitespace_only():
    with pytest.raises(QueryTooShortError):
        normalize_query("   ")
```

- [ ] **Step 2.2: Run — expect failure (module missing)**

```
uv run pytest tests/test_dashboard_search.py::test_normalize_strips_whitespace -v
```

- [ ] **Step 2.3: Implement**

```python
# dashboard/search.py
"""Global cross-table search over scout.db.

Read-only. All queries are parameterized — query string NEVER concatenated
into SQL (only ever bound via `?` placeholders inside `%q%` LIKE patterns).
"""

from __future__ import annotations


class QueryTooShortError(ValueError):
    """Query must be at least 2 non-whitespace characters."""


def normalize_query(raw: str) -> str:
    """Strip whitespace, lowercase, strip leading $/# ticker sigils.

    Raises QueryTooShortError for queries < 2 chars after normalization.
    """
    if raw is None:
        raise QueryTooShortError("query required")
    q = raw.strip()
    if q.startswith("$") or q.startswith("#"):
        q = q[1:]
    q = q.lower()
    if len(q) < 2:
        raise QueryTooShortError(f"query too short: {raw!r}")
    return q
```

- [ ] **Step 2.4: Run — expect PASS**

```
uv run pytest tests/test_dashboard_search.py -v
```

- [ ] **Step 2.5: Commit**

```bash
git add dashboard/search.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): add normalize_query with rejection of short queries"
```

---

### Task 3: Per-table search functions (candidates)

**Files:**
- Modify: `dashboard/search.py`
- Test: `tests/test_dashboard_search.py` (append)

- [ ] **Step 3.1: Write failing test using tmp_path SQLite fixture**

```python
# Append to tests/test_dashboard_search.py
import aiosqlite
import asyncio


async def _seed_candidates(db_path):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_name TEXT NOT NULL,
                ticker TEXT NOT NULL,
                market_cap_usd REAL,
                first_seen_at TEXT NOT NULL,
                alerted_at TEXT
            )
        """)
        await conn.executemany(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
            [
                ("0xCHIP01", "solana", "ChipCoin", "CHIP", 1000.0, "2026-05-14T10:00:00+00:00", None),
                ("0xCHIP02", "base", "ChipperCoin", "CHIPR", 2000.0, "2026-05-14T11:00:00+00:00", None),
                ("0xOTHER", "solana", "Other", "OTH", 500.0, "2026-05-14T12:00:00+00:00", None),
            ],
        )
        await conn.commit()


async def test_search_candidates_exact_symbol(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import search_candidates

    hits = await search_candidates(db_path, "chip", limit=10)
    # CHIP exact match should score higher than CHIPR
    assert len(hits) == 2
    symbols = [h.symbol for h in hits]
    assert "CHIP" in symbols
    assert "CHIPR" in symbols
    # Exact symbol match should be ranked first
    assert hits[0].symbol == "CHIP"
    assert hits[0].match_quality == "exact_symbol"


async def test_search_candidates_contract_address(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import search_candidates

    hits = await search_candidates(db_path, "0xchip01", limit=10)
    assert len(hits) == 1
    assert hits[0].canonical_id == "0xCHIP01"
    assert hits[0].match_quality == "exact_contract"


async def test_search_candidates_no_match(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import search_candidates

    hits = await search_candidates(db_path, "nosuchtoken", limit=10)
    assert hits == []
```

- [ ] **Step 3.2: Run — expect failure**

```
uv run pytest tests/test_dashboard_search.py::test_search_candidates_exact_symbol -v
```

- [ ] **Step 3.3: Implement search_candidates**

```python
# Append to dashboard/search.py
from dashboard.models import SearchHit
import aiosqlite


def _classify_match(q: str, *fields: str | None) -> str:
    """Return 'exact_symbol', 'exact_contract', 'prefix', or 'substring'."""
    q_lower = q.lower()
    for f in fields:
        if f is None:
            continue
        f_lower = f.lower()
        if f_lower == q_lower:
            # Contract addresses are typically long; symbols are short
            if f_lower.startswith("0x") or len(f_lower) > 20:
                return "exact_contract"
            return "exact_symbol"
    for f in fields:
        if f is None:
            continue
        if f.lower().startswith(q_lower):
            return "prefix"
    return "substring"


_MATCH_QUALITY_RANK = {
    "exact_symbol": 0,
    "exact_contract": 1,
    "prefix": 2,
    "substring": 3,
    "text": 4,
}


async def search_candidates(db_path: str, q: str, limit: int) -> list[SearchHit]:
    """Search candidates table on contract_address, token_name, ticker."""
    pattern = f"%{q}%"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT contract_address, chain, token_name, ticker,
                      market_cap_usd, first_seen_at, alerted_at
               FROM candidates
               WHERE lower(contract_address) LIKE ?
                  OR lower(token_name) LIKE ?
                  OR lower(ticker) LIKE ?
               ORDER BY first_seen_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["contract_address"], r["token_name"], r["ticker"])
        hits.append(SearchHit(
            canonical_id=r["contract_address"],
            symbol=r["ticker"],
            name=r["token_name"],
            chain=r["chain"],
            contract_address=r["contract_address"],
            sources=["candidates"],
            source_counts={"candidates": 1},
            first_seen_at=r["first_seen_at"],
            last_seen_at=r["first_seen_at"],
            match_quality=mq,
        ))
    hits.sort(key=lambda h: _MATCH_QUALITY_RANK[h.match_quality])
    return hits
```

- [ ] **Step 3.4: Run tests — expect PASS**

```
uv run pytest tests/test_dashboard_search.py -v
```

- [ ] **Step 3.5: Commit**

```bash
git add dashboard/search.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): add search_candidates with match-quality ranking"
```

---

### Task 4: Per-table search for paper_trades, alerts

**Files:**
- Modify: `dashboard/search.py`
- Test: `tests/test_dashboard_search.py` (append)

- [ ] **Step 4.1: Write failing tests**

```python
async def test_search_paper_trades_by_symbol(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                chain TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                pnl_pct REAL,
                peak_pct REAL
            )
        """)
        await conn.execute(
            "INSERT INTO paper_trades(token_id,symbol,name,chain,signal_type,opened_at,status,pnl_pct,peak_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("chip-coin", "CHIP", "ChipCoin", "coingecko", "gainers_early",
             "2026-05-10T10:00:00+00:00", "closed_tp", 25.4, 30.1),
        )
        await conn.commit()

    from dashboard.search import search_paper_trades

    hits = await search_paper_trades(db_path, "chip", limit=10)
    assert len(hits) == 1
    assert hits[0].symbol == "CHIP"
    assert hits[0].best_paper_trade_pnl_pct == 25.4
    assert "paper_trades" in hits[0].sources


async def test_search_alerts(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                chain TEXT NOT NULL,
                conviction_score REAL NOT NULL,
                alert_market_cap REAL,
                alerted_at TEXT NOT NULL,
                token_name TEXT,
                ticker TEXT
            )
        """)
        await conn.execute(
            "INSERT INTO alerts(contract_address,chain,conviction_score,alerted_at,token_name,ticker) "
            "VALUES (?,?,?,?,?,?)",
            ("0xchip", "solana", 72.0, "2026-05-13T10:00:00+00:00", "ChipCoin", "CHIP"),
        )
        await conn.commit()

    from dashboard.search import search_alerts

    hits = await search_alerts(db_path, "chip", limit=10)
    assert len(hits) == 1
    assert hits[0].symbol == "CHIP"
    assert "alerts" in hits[0].sources
```

- [ ] **Step 4.2: Run — expect failure**

- [ ] **Step 4.3: Implement search_paper_trades and search_alerts**

```python
# Append to dashboard/search.py
async def search_paper_trades(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT token_id, symbol, name, chain,
                      MIN(opened_at) AS first_seen,
                      MAX(opened_at) AS last_seen,
                      COUNT(*) AS n,
                      MAX(pnl_pct) AS best_pnl
               FROM paper_trades
               WHERE lower(symbol) LIKE ?
                  OR lower(name) LIKE ?
                  OR lower(token_id) LIKE ?
               GROUP BY token_id, symbol, name, chain
               ORDER BY last_seen DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["symbol"], r["name"], r["token_id"])
        hits.append(SearchHit(
            canonical_id=r["token_id"],
            symbol=r["symbol"],
            name=r["name"],
            chain=r["chain"],
            sources=["paper_trades"],
            source_counts={"paper_trades": r["n"]},
            first_seen_at=r["first_seen"],
            last_seen_at=r["last_seen"],
            match_quality=mq,
            best_paper_trade_pnl_pct=r["best_pnl"],
        ))
    return hits


async def search_alerts(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT contract_address, chain, conviction_score, alert_market_cap,
                      alerted_at, token_name, ticker
               FROM alerts
               WHERE lower(contract_address) LIKE ?
                  OR lower(token_name) LIKE ?
                  OR lower(ticker) LIKE ?
               ORDER BY alerted_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["contract_address"], r["token_name"], r["ticker"])
        hits.append(SearchHit(
            canonical_id=r["contract_address"],
            symbol=r["ticker"],
            name=r["token_name"],
            chain=r["chain"],
            contract_address=r["contract_address"],
            sources=["alerts"],
            source_counts={"alerts": 1},
            first_seen_at=r["alerted_at"],
            last_seen_at=r["alerted_at"],
            match_quality=mq,
        ))
    return hits
```

- [ ] **Step 4.4: Run — PASS**

- [ ] **Step 4.5: Commit**

```bash
git add dashboard/search.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): add search_paper_trades + search_alerts"
```

---

### Task 5: Snapshots tables (gainers, trending) — aggregate hits

**Files:**
- Modify: `dashboard/search.py`
- Test: `tests/test_dashboard_search.py` (append)

- [ ] **Step 5.1: Write failing test**

```python
async def test_search_gainers_snapshots_aggregates(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE gainers_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_24h REAL NOT NULL,
                market_cap REAL,
                volume_24h REAL,
                snapshot_at TEXT NOT NULL
            )
        """)
        await conn.executemany(
            "INSERT INTO gainers_snapshots(coin_id,symbol,name,price_change_24h,snapshot_at) "
            "VALUES (?,?,?,?,?)",
            [
                ("chip-coin", "CHIP", "ChipCoin", 65.0, "2026-05-14T10:00:00+00:00"),
                ("chip-coin", "CHIP", "ChipCoin", 72.0, "2026-05-14T11:00:00+00:00"),
                ("chip-coin", "CHIP", "ChipCoin", 80.0, "2026-05-14T12:00:00+00:00"),
            ],
        )
        await conn.commit()

    from dashboard.search import search_snapshots

    hits = await search_snapshots(db_path, "chip", limit=10, table="gainers_snapshots")
    assert len(hits) == 1
    assert hits[0].source_counts["gainers_snapshots"] == 3
    assert hits[0].first_seen_at == "2026-05-14T10:00:00+00:00"
    assert hits[0].last_seen_at == "2026-05-14T12:00:00+00:00"
```

- [ ] **Step 5.2: Run — expect failure**

- [ ] **Step 5.3: Implement search_snapshots (generic)**

```python
# Append to dashboard/search.py
_SNAPSHOT_TABLES = {
    "gainers_snapshots", "trending_snapshots",
    "momentum_7d", "slow_burn_candidates", "velocity_alerts", "volume_spikes",
    "predictions",
}

_TIME_COL = {
    "gainers_snapshots": "snapshot_at",
    "trending_snapshots": "snapshot_at",
    "momentum_7d": "detected_at",
    "slow_burn_candidates": "detected_at",
    "velocity_alerts": "detected_at",
    "volume_spikes": "detected_at",
    "predictions": "predicted_at",
}


async def search_snapshots(
    db_path: str, q: str, limit: int, table: str
) -> list[SearchHit]:
    if table not in _SNAPSHOT_TABLES:
        raise ValueError(f"unknown snapshot table: {table}")
    time_col = _TIME_COL[table]
    pattern = f"%{q}%"
    # Table name is a hard-coded whitelist member — safe to interpolate.
    sql = f"""
        SELECT coin_id, symbol, name,
               MIN({time_col}) AS first_seen,
               MAX({time_col}) AS last_seen,
               COUNT(*) AS n
        FROM {table}
        WHERE lower(coin_id) LIKE ?
           OR lower(symbol) LIKE ?
           OR lower(name) LIKE ?
        GROUP BY coin_id, symbol, name
        ORDER BY last_seen DESC
        LIMIT ?
    """
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, (pattern, pattern, pattern, limit))
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["symbol"], r["name"], r["coin_id"])
        hits.append(SearchHit(
            canonical_id=r["coin_id"],
            symbol=r["symbol"],
            name=r["name"],
            chain="coingecko",
            sources=[table],
            source_counts={table: r["n"]},
            first_seen_at=r["first_seen"],
            last_seen_at=r["last_seen"],
            match_quality=mq,
        ))
    return hits
```

- [ ] **Step 5.4: Run — PASS**

- [ ] **Step 5.5: Commit**

```bash
git add dashboard/search.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): add generic search_snapshots for 7 detection tables"
```

---

### Task 6: TG social messages + signals + narrative inbound

**Files:**
- Modify: `dashboard/search.py`
- Test: `tests/test_dashboard_search.py` (append)

- [ ] **Step 6.1: Write failing tests**

```python
async def test_search_tg_messages_finds_cashtag(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE tg_social_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_handle TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                posted_at TEXT NOT NULL,
                sender TEXT,
                text TEXT,
                cashtags TEXT,
                contracts TEXT,
                parsed_at TEXT NOT NULL
            )
        """)
        await conn.execute(
            "INSERT INTO tg_social_messages(channel_handle,msg_id,posted_at,sender,text,cashtags,contracts,parsed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("@kol", 1, "2026-05-14T08:00:00+00:00", "alice",
             "Watch $CHIP about to break out", '["$CHIP"]', "[]",
             "2026-05-14T08:00:01+00:00"),
        )
        await conn.commit()

    from dashboard.search import search_tg_messages

    hits = await search_tg_messages(db_path, "chip", limit=10)
    assert len(hits) == 1
    assert "tg_social_messages" in hits[0].sources


async def test_search_narrative_inbound_finds_tweet(tmp_path):
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE narrative_alerts_inbound (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                tweet_id TEXT NOT NULL,
                tweet_author TEXT NOT NULL,
                tweet_ts TEXT NOT NULL,
                tweet_text TEXT NOT NULL,
                tweet_text_hash TEXT NOT NULL,
                extracted_cashtag TEXT,
                extracted_ca TEXT,
                extracted_chain TEXT,
                resolved_coin_id TEXT,
                classifier_version TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
        """)
        await conn.execute(
            """INSERT INTO narrative_alerts_inbound(
                event_id,tweet_id,tweet_author,tweet_ts,tweet_text,tweet_text_hash,
                extracted_cashtag,resolved_coin_id,classifier_version,received_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("e1", "t1", "kol_jim", "2026-05-14T07:00:00+00:00",
             "$CHIP looks heated", "h1", "$CHIP", "chip-coin", "v1",
             "2026-05-14T07:00:05+00:00"),
        )
        await conn.commit()

    from dashboard.search import search_narrative_inbound

    hits = await search_narrative_inbound(db_path, "chip", limit=10)
    assert len(hits) == 1
    assert "narrative_alerts_inbound" in hits[0].sources
```

- [ ] **Step 6.2: Run — expect failure**

- [ ] **Step 6.3: Implement**

```python
# Append to dashboard/search.py
async def search_tg_messages(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT id, channel_handle, posted_at, sender, text, cashtags, contracts
               FROM tg_social_messages
               WHERE lower(text) LIKE ?
                  OR lower(cashtags) LIKE ?
                  OR lower(contracts) LIKE ?
               ORDER BY posted_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        # tg_social_messages doesn't have a canonical token id — use msg id
        # so dedup at orchestrator level won't collapse distinct messages
        hits.append(SearchHit(
            canonical_id=f"tg_msg:{r['id']}",
            symbol=None,
            name=f"{r['channel_handle']} #{r['id']}",
            chain=None,
            sources=["tg_social_messages"],
            source_counts={"tg_social_messages": 1},
            first_seen_at=r["posted_at"],
            last_seen_at=r["posted_at"],
            match_quality="text",
        ))
    return hits


async def search_tg_signals(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT token_id, symbol, contract_address, chain,
                      MIN(created_at) AS first_seen,
                      MAX(created_at) AS last_seen,
                      COUNT(*) AS n
               FROM tg_social_signals
               WHERE lower(symbol) LIKE ?
                  OR lower(token_id) LIKE ?
                  OR lower(contract_address) LIKE ?
               GROUP BY token_id, symbol, contract_address, chain
               ORDER BY last_seen DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["symbol"], r["token_id"], r["contract_address"])
        hits.append(SearchHit(
            canonical_id=r["token_id"],
            symbol=r["symbol"],
            chain=r["chain"],
            contract_address=r["contract_address"],
            sources=["tg_social_signals"],
            source_counts={"tg_social_signals": r["n"]},
            first_seen_at=r["first_seen"],
            last_seen_at=r["last_seen"],
            match_quality=mq,
        ))
    return hits


async def search_narrative_inbound(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT id, tweet_author, tweet_ts, tweet_text,
                      extracted_cashtag, resolved_coin_id, received_at
               FROM narrative_alerts_inbound
               WHERE lower(tweet_text) LIKE ?
                  OR lower(extracted_cashtag) LIKE ?
                  OR lower(resolved_coin_id) LIKE ?
                  OR lower(tweet_author) LIKE ?
               ORDER BY received_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        canonical = r["resolved_coin_id"] or f"x_alert:{r['id']}"
        hits.append(SearchHit(
            canonical_id=canonical,
            symbol=(r["extracted_cashtag"] or "").lstrip("$") or None,
            name=f"@{r['tweet_author']} (X)",
            chain="coingecko" if r["resolved_coin_id"] else None,
            sources=["narrative_alerts_inbound"],
            source_counts={"narrative_alerts_inbound": 1},
            first_seen_at=r["received_at"],
            last_seen_at=r["received_at"],
            match_quality="text",
        ))
    return hits
```

- [ ] **Step 6.4: Run — PASS**

- [ ] **Step 6.5: Commit**

```bash
git add dashboard/search.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): add tg_messages, tg_signals, narrative_inbound searchers"
```

---

### Task 7: Orchestrator with dedup + ranking

**Files:**
- Modify: `dashboard/search.py`
- Test: `tests/test_dashboard_search.py` (append)

- [ ] **Step 7.1: Write failing tests**

```python
async def test_run_search_aggregates_across_tables(tmp_path):
    """A token in candidates + gainers_snapshots should collapse into one hit
    with sources=['candidates','gainers_snapshots']."""
    db_path = str(tmp_path / "scout.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY, chain TEXT, token_name TEXT,
                ticker TEXT, market_cap_usd REAL, first_seen_at TEXT, alerted_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE gainers_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT, symbol TEXT,
                name TEXT, price_change_24h REAL, snapshot_at TEXT
            )
        """)
        await conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
            ("chip-coin", "coingecko", "ChipCoin", "CHIP", 50000.0,
             "2026-05-14T09:00:00+00:00", None),
        )
        await conn.execute(
            "INSERT INTO gainers_snapshots(coin_id,symbol,name,price_change_24h,snapshot_at) VALUES (?,?,?,?,?)",
            ("chip-coin", "CHIP", "ChipCoin", 65.0, "2026-05-14T10:00:00+00:00"),
        )
        await conn.commit()

    from dashboard.search import run_search

    resp = await run_search(db_path, "chip", limit=50)
    assert resp.total_hits == 1
    h = resp.hits[0]
    assert h.canonical_id == "chip-coin"
    assert set(h.sources) == {"candidates", "gainers_snapshots"}


async def test_run_search_empty_query():
    from dashboard.search import run_search, QueryTooShortError

    with pytest.raises(QueryTooShortError):
        await run_search("/tmp/no.db", "", limit=50)


async def test_run_search_sql_injection_attempt(tmp_path):
    """A query containing SQL meta chars must be treated as text, not SQL."""
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.search import run_search

    # If injection succeeded, this would drop the table or return everything
    resp = await run_search(db_path, "'; DROP TABLE candidates; --", limit=50)
    assert resp.total_hits == 0
    # Table still exists
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM candidates")
        cnt = (await cur.fetchone())[0]
        assert cnt == 3  # original seed
```

- [ ] **Step 7.2: Run — expect failure**

- [ ] **Step 7.3: Implement orchestrator**

```python
# Append to dashboard/search.py
import asyncio
from dashboard.models import SearchResponse


async def _safe_call(coro):
    """Wrap a search coroutine so a missing table or other DB error returns []."""
    try:
        return await coro
    except aiosqlite.OperationalError:
        return []


async def run_search(db_path: str, raw_q: str, limit: int = 50) -> SearchResponse:
    """Orchestrate all per-table searches in parallel, dedup by canonical_id."""
    q = normalize_query(raw_q)
    # Each per-table search pulls up to `limit` so aggregate dedup still
    # gives `limit` distinct entities most of the time.
    coros = [
        _safe_call(search_candidates(db_path, q, limit)),
        _safe_call(search_paper_trades(db_path, q, limit)),
        _safe_call(search_alerts(db_path, q, limit)),
        _safe_call(search_snapshots(db_path, q, limit, "gainers_snapshots")),
        _safe_call(search_snapshots(db_path, q, limit, "trending_snapshots")),
        _safe_call(search_snapshots(db_path, q, limit, "momentum_7d")),
        _safe_call(search_snapshots(db_path, q, limit, "slow_burn_candidates")),
        _safe_call(search_snapshots(db_path, q, limit, "velocity_alerts")),
        _safe_call(search_snapshots(db_path, q, limit, "volume_spikes")),
        _safe_call(search_snapshots(db_path, q, limit, "predictions")),
        _safe_call(search_tg_messages(db_path, q, limit)),
        _safe_call(search_tg_signals(db_path, q, limit)),
        _safe_call(search_narrative_inbound(db_path, q, limit)),
    ]
    results = await asyncio.gather(*coros, return_exceptions=False)
    # Flatten + dedup
    by_id: dict[str, SearchHit] = {}
    for hits in results:
        for h in hits:
            cid = h.canonical_id
            if cid not in by_id:
                by_id[cid] = h
                continue
            # Merge: union sources, take min/max timestamps, best match_quality
            existing = by_id[cid]
            existing.sources = sorted(set(existing.sources) | set(h.sources))
            for src, n in h.source_counts.items():
                existing.source_counts[src] = existing.source_counts.get(src, 0) + n
            if h.first_seen_at and (
                not existing.first_seen_at or h.first_seen_at < existing.first_seen_at
            ):
                existing.first_seen_at = h.first_seen_at
            if h.last_seen_at and (
                not existing.last_seen_at or h.last_seen_at > existing.last_seen_at
            ):
                existing.last_seen_at = h.last_seen_at
            if _MATCH_QUALITY_RANK[h.match_quality] < _MATCH_QUALITY_RANK[existing.match_quality]:
                existing.match_quality = h.match_quality
            existing.symbol = existing.symbol or h.symbol
            existing.name = existing.name or h.name
            existing.chain = existing.chain or h.chain
            existing.contract_address = existing.contract_address or h.contract_address
            if h.best_paper_trade_pnl_pct is not None and (
                existing.best_paper_trade_pnl_pct is None
                or h.best_paper_trade_pnl_pct > existing.best_paper_trade_pnl_pct
            ):
                existing.best_paper_trade_pnl_pct = h.best_paper_trade_pnl_pct
    merged = list(by_id.values())
    merged.sort(key=lambda h: (_MATCH_QUALITY_RANK[h.match_quality],
                                -(len(h.sources)),
                                h.last_seen_at or ""))
    truncated = len(merged) > limit
    merged = merged[:limit]
    return SearchResponse(
        query=q,
        total_hits=len(merged),
        hits=merged,
        truncated=truncated,
    )
```

- [ ] **Step 7.4: Run — PASS**

- [ ] **Step 7.5: Commit**

```bash
git add dashboard/search.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): orchestrator with dedup, ranking, SQL-injection-safe params"
```

---

### Task 8: API route

**Files:**
- Modify: `dashboard/api.py`
- Test: `tests/test_dashboard_search.py` (append integration test)

- [ ] **Step 8.1: Write failing integration test**

```python
from fastapi.testclient import TestClient


async def test_search_endpoint_returns_results(tmp_path):
    db_path = str(tmp_path / "scout.db")
    await _seed_candidates(db_path)

    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    client = TestClient(app)
    r = client.get("/api/search?q=chip")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "chip"
    assert body["total_hits"] >= 1
    assert any(h["symbol"] == "CHIP" for h in body["hits"])


async def test_search_endpoint_rejects_short_query(tmp_path):
    db_path = str(tmp_path / "scout.db")
    from dashboard.api import create_app
    app = create_app(db_path=db_path)
    client = TestClient(app)
    r = client.get("/api/search?q=a")
    assert r.status_code == 400
```

- [ ] **Step 8.2: Run — expect 404 (route missing)**

- [ ] **Step 8.3: Add route to `dashboard/api.py`**

Insert after the `/api/win-rate` endpoint (around line 114):

```python
    @app.get("/api/search", response_model=None)
    async def get_search(
        q: str = Query(..., min_length=1, max_length=128),
        limit: int = Query(50, ge=1, le=200),
    ):
        from fastapi.responses import JSONResponse
        from dashboard.search import run_search, QueryTooShortError

        try:
            resp = await run_search(_db_path, q, limit=limit)
        except QueryTooShortError as e:
            return JSONResponse(status_code=400, content={"detail": str(e)})
        return resp.model_dump()
```

- [ ] **Step 8.4: Run — PASS**

- [ ] **Step 8.5: Commit**

```bash
git add dashboard/api.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): wire /api/search route"
```

---

### Task 9: Frontend GlobalSearch component

**Files:**
- Create: `dashboard/frontend/components/GlobalSearch.jsx`
- Modify: `dashboard/frontend/App.jsx`
- Modify: `dashboard/frontend/style.css`

- [ ] **Step 9.1: Create GlobalSearch.jsx**

```jsx
import React, { useState, useEffect, useRef, useCallback } from 'react'
import TokenLink from './TokenLink'

const SOURCE_LABELS = {
  candidates: 'Cand',
  alerts: 'Alert',
  paper_trades: 'Paper',
  gainers_snapshots: 'Gain',
  trending_snapshots: 'Trend',
  momentum_7d: 'Mom7',
  slow_burn_candidates: 'Slow',
  velocity_alerts: 'Vel',
  volume_spikes: 'VolSpk',
  predictions: 'Pred',
  tg_social_messages: 'TG-Msg',
  tg_social_signals: 'TG-Sig',
  narrative_alerts_inbound: 'X',
}

export default function GlobalSearch() {
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const inputRef = useRef(null)
  const abortRef = useRef(null)

  const doSearch = useCallback(async (query) => {
    if (!query || query.trim().length < 2) {
      setResults(null)
      return
    }
    if (abortRef.current) abortRef.current.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setLoading(true)
    try {
      const r = await fetch(`/api/search?q=${encodeURIComponent(query)}`, { signal: ctrl.signal })
      if (r.ok) {
        const body = await r.json()
        setResults(body)
      } else if (r.status === 400) {
        setResults({ query, total_hits: 0, hits: [] })
      }
    } catch (e) {
      if (e.name !== 'AbortError') setResults(null)
    } finally {
      setLoading(false)
    }
  }, [])

  // Debounced search on q change
  useEffect(() => {
    const t = setTimeout(() => doSearch(q), 300)
    return () => clearTimeout(t)
  }, [q, doSearch])

  // Ctrl+K / Cmd+K to focus
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
        setOpen(true)
      } else if (e.key === 'Escape') {
        setOpen(false)
        inputRef.current?.blur()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="global-search">
      <input
        ref={inputRef}
        className="global-search-input"
        type="text"
        placeholder="Search tokens, alerts, KOL msgs... (Ctrl+K)"
        value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
      />
      {open && q.length >= 2 && (
        <div className="global-search-dropdown">
          {loading && <div className="gs-state">Searching...</div>}
          {!loading && results && results.total_hits === 0 && (
            <div className="gs-state">No results for "{q}"</div>
          )}
          {!loading && results && results.hits.map((h) => (
            <div key={h.canonical_id} className="gs-hit">
              <div className="gs-hit-main">
                <TokenLink
                  tokenId={h.contract_address || h.canonical_id}
                  symbol={h.symbol || h.name}
                  chain={h.chain}
                />
                <span className="gs-hit-name">{h.name}</span>
                <span className={`gs-hit-quality gs-quality-${h.match_quality}`}>{h.match_quality}</span>
              </div>
              <div className="gs-hit-sources">
                {h.sources.map((src) => (
                  <span key={src} className="gs-source-badge" title={`${src}: ${h.source_counts[src] || 1}`}>
                    {SOURCE_LABELS[src] || src} {h.source_counts[src] > 1 ? `×${h.source_counts[src]}` : ''}
                  </span>
                ))}
                {h.best_paper_trade_pnl_pct != null && (
                  <span className="gs-pnl-badge" title="best paper-trade pnl%">
                    PnL {h.best_paper_trade_pnl_pct >= 0 ? '+' : ''}{h.best_paper_trade_pnl_pct.toFixed(1)}%
                  </span>
                )}
              </div>
              <div className="gs-hit-meta">
                {h.first_seen_at && <span>first: {h.first_seen_at.slice(0, 16).replace('T', ' ')}</span>}
                {h.last_seen_at && h.last_seen_at !== h.first_seen_at &&
                  <span> · last: {h.last_seen_at.slice(0, 16).replace('T', ' ')}</span>}
              </div>
            </div>
          ))}
          {results && results.truncated && (
            <div className="gs-state gs-truncated">Showing first {results.hits.length} results — refine query for more.</div>
          )}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 9.2: Mount in App.jsx header**

In `dashboard/frontend/App.jsx`, after the import block add:

```jsx
import GlobalSearch from './components/GlobalSearch.jsx'
```

And modify the header (line 113-120) to:

```jsx
<div className="header">
  <h1>Gecko-Alpha Dashboard</h1>
  <GlobalSearch />
  <div className="live-indicator">
    <div className={`live-dot ${connected ? '' : 'disconnected'}`} />
    <span>{connected ? 'Live' : 'Reconnecting...'}</span>
  </div>
</div>
```

- [ ] **Step 9.3: Add CSS to style.css**

Append to `dashboard/frontend/style.css`:

```css
/* --- Global Search --- */
.global-search { position: relative; flex: 0 1 380px; margin: 0 16px; }
.global-search-input {
  width: 100%; box-sizing: border-box; padding: 8px 12px;
  background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15);
  border-radius: 6px; color: var(--color-text); font-size: 13px;
}
.global-search-input:focus { outline: 1px solid #4fc3f7; }
.global-search-dropdown {
  position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px;
  background: #161b22; border: 1px solid rgba(255,255,255,0.15);
  border-radius: 6px; max-height: 480px; overflow-y: auto; z-index: 100;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.gs-state { padding: 12px; color: var(--color-text-secondary); font-size: 12px; }
.gs-hit { padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.06); }
.gs-hit:last-child { border-bottom: none; }
.gs-hit-main { display: flex; gap: 8px; align-items: baseline; }
.gs-hit-name { color: var(--color-text-secondary); font-size: 12px; flex: 1; }
.gs-hit-quality { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(255,255,255,0.08); color: var(--color-text-secondary); }
.gs-quality-exact_symbol, .gs-quality-exact_contract { background: rgba(79,195,247,0.2); color: #4fc3f7; }
.gs-hit-sources { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.gs-source-badge { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(255,255,255,0.08); color: var(--color-text-secondary); }
.gs-pnl-badge { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(76,217,100,0.15); color: #4cd964; }
.gs-hit-meta { font-size: 10px; color: var(--color-text-secondary); margin-top: 4px; }
.gs-truncated { font-style: italic; }
```

- [ ] **Step 9.4: Verify visually with `npm run dev`**

```bash
cd dashboard/frontend && npm run dev
```

Open browser, type "chip" — expect debounced dropdown with at least the candidates seeded above. **If you cannot test the UI** (Windows OpenSSL workaround per memory `reference_windows_openssl_workaround.md`): explicitly say so in the PR description; let srilu-vps validate on deploy.

- [ ] **Step 9.5: Commit**

```bash
git add dashboard/frontend/components/GlobalSearch.jsx dashboard/frontend/App.jsx dashboard/frontend/style.css
git commit -m "feat(dashboard-search): add GlobalSearch React component with Ctrl+K shortcut"
```

---

### Task 10: Build frontend + verify

**Files:**
- Modify: `dashboard/frontend/dist/index.html` + `dashboard/frontend/dist/assets/index-*.js`

- [ ] **Step 10.1: Build frontend**

```bash
cd dashboard/frontend && npm run build
```

Expected: `dist/index.html` and `dist/assets/index-<hash>.js` regenerated.

Per memory `feedback_vite_dist_index_html_commit_discipline.md`: ALWAYS commit `dist/index.html` together with the regenerated `dist/assets/*.js`. Vite rewrites the script src on every build.

- [ ] **Step 10.2: Verify tests still pass**

```bash
uv run pytest tests/test_dashboard_search.py -v --tb=short
```

- [ ] **Step 10.3: Run full test suite (regression guard)**

```bash
uv run pytest --tb=short -q
```

Expected: no new failures relative to master baseline.

- [ ] **Step 10.4: Commit built dist**

```bash
git add dashboard/frontend/dist/index.html dashboard/frontend/dist/assets/
git commit -m "build(dashboard): regenerate dist for GlobalSearch component"
```

---

## Verification before PR

- [ ] All new tests pass locally OR explicitly note "Windows OpenSSL prevents local run — validated on VPS"
- [ ] No regressions in existing test suite
- [ ] Search returns expected hits for: 'chip' (no match), 'troll' (hits paper_trades + gainers + trending), 'quack' (hits candidates + paper_trades + gainers)
- [ ] Ctrl+K focuses the search box
- [ ] SQL-injection test passes (test_run_search_sql_injection_attempt)
- [ ] Frontend `dist/index.html` regenerated and committed
- [ ] Pre-flight against prod DB read-only: `ssh root@89.167.116.187 'curl -s "http://localhost:8000/api/search?q=troll&limit=5"'` after deploy — expect ≥1 hit with paper_trades source

## Deploy steps

1. PR review (3 parallel reviewers — see workflow above)
2. Merge to master
3. `ssh root@89.167.116.187 'cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} +; systemctl restart gecko-dashboard'`
4. Verify: `curl http://localhost:8000/api/search?q=troll&limit=5`
5. Operator UAT: open dashboard, Ctrl+K, search 'chip'

## Rollback

`git revert <merge-sha> && git push origin master && ssh root@srilu 'cd /root/gecko-alpha && git pull && systemctl restart gecko-dashboard'`. Zero state change to roll back — read-only feature.
