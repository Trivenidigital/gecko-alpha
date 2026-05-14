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
| `scout/api/narrative.py` (drift-check addendum 2026-05-14 post plan-review-1) | Inbound HMAC-gated webhook from Hermes that writes to `narrative_alerts_inbound` only. Grep hits on `search` are unrelated (canonical-query-string for HMAC, journalctl-searchability comment). | NO — write-side webhook, not a read-side search endpoint. |
| `dashboard/db.py` | All existing per-feature query helpers (`get_candidates`, `get_recent_alerts`, etc.) — no cross-table search shape. | NO — single-table reads. |

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
│  - debounced input (300ms, skipped on q<2)                      │
│  - Ctrl+K / "/" focus (guarded vs other editable elements)      │
│  - Esc clears + blurs                                           │
│  - Arrow keys navigate results; Enter opens hit                 │
│  - ARIA combobox/listbox semantics                              │
│  - fetch('/api/search?q=' + encodeURIComponent(q))              │
│  - render grouped result list                                   │
│  - Renders TokenLink ONLY when canonical_id is a real token     │
│    identifier (NOT tg_msg:* or x_alert:*)                       │
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
│   2. Each per-table search fn opens its own read-only conn via  │
│      `dashboard.db._ro_db(db_path)` — same pattern as every     │
│      other dashboard query. URI mode=ro at the driver level     │
│      makes accidental writes impossible.                        │
│   3. Parallel `asyncio.gather()` over per-table search fns      │
│   4. Each fn returns list[SearchHit]                            │
│   5. Group by (canonical_id = contract_address or coin_id),     │
│      collapse multiple hits per entity into one SearchHit with  │
│      `sources: list[str]` and `first_seen / last_seen` window   │
│   6. Rank: exact symbol > exact contract > prefix > substring   │
│   7. Apply limit, return SearchResponse                         │
└─────────────────────────────────────────────────────────────────┘
```

**SQL safety invariant:** every query in `search.py` uses parameterized `?` placeholders. The query string is interpolated ONLY into the `?`-bound LIKE pattern after being wrapped as `f"%{q}%"` (FastAPI/Pydantic validates length 1-128); never concatenated into raw SQL. Plus every connection opens in **URI read-only mode** (`?mode=ro`) — even if a SQL-injection bypass were found, the driver rejects writes at the kernel level.

**Performance budget (revised after plan-review-1 measurement):** one LIKE query on prod `gainers_snapshots` (87k rows) measured at 61ms. SQLite serializes 13 parallel connections through the file lock, so realistic warm fan-out is **150-300ms**, cold ~300-500ms. Acceptable for a 300ms-debounced interactive search but the original "<50ms" estimate was wrong. Mitigation: per-source `LIMIT limit` keeps per-query cost bounded; `lower(symbol) = ?` exact-match fast path runs first (uses no index but is O(N) without LIKE meta-char overhead); add no new indexes (would require migration; 87k rows is already small).

**Deferred refactor (plan-review-1 SHOULD-FIX #9 — explicitly NOT in this PR):** shared single read-only connection passed to all per-table functions would save ~13× connection-open overhead (~5-10ms each warm). Deferred because (a) it changes every per-table function signature, expanding test surface; (b) the saved ~65-130ms is below the 300ms debounce floor and operator-imperceptible; (c) it's a clean follow-up if perf monitoring shows actual cost.

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
    # entity_kind: "token" for real on-chain tokens / coingecko coins (renders as TokenLink);
    # "tg_msg" for a single TG message (canonical_id like tg_msg:<id>);
    # "x_alert" for an unresolved X alert (canonical_id like x_alert:<id>).
    # Frontend uses this to decide whether to render TokenLink — avoids broken URLs
    # like coingecko.com/en/coins/tg_msg:123 (plan-review-1 frontend MUST-FIX #4).
    entity_kind: str = "token"
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
All connections open in URI read-only mode (`?mode=ro`) via
`dashboard.db._ro_db` for defense-in-depth (plan-review-1 backend MUST-FIX #2).
"""

from __future__ import annotations

import aiosqlite

from dashboard.db import _ro_db
from dashboard.models import SearchHit, SearchResponse


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
    async with _ro_db(db_path) as conn:
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
            entity_kind="token",
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

> **Note: test fixture must seed candidates schema with `aiosqlite.connect`-write (NOT `_ro_db`).** The `_ro_db` helper exists in `dashboard/db.py` and only opens connections; tests use the standard `aiosqlite.connect` to seed test fixtures. The plan's test code at Step 3.1 already does this correctly.

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
    async with _ro_db(db_path) as conn:
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
            entity_kind="token",
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
    """Search alerts. LEFT JOIN candidates so older alert rows (which have NULL
    ticker/token_name from the pre-migration era) still match queries against
    the joined candidates row's ticker/token_name. Per plan-review-1 backend
    SHOULD-FIX #7 — without this join, prod's 2/2 alerts rows are unsearchable
    by ticker."""
    pattern = f"%{q}%"
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT a.contract_address, a.chain, a.conviction_score, a.alert_market_cap,
                      a.alerted_at,
                      COALESCE(a.token_name, c.token_name) AS token_name,
                      COALESCE(a.ticker, c.ticker) AS ticker
               FROM alerts a
               LEFT JOIN candidates c ON a.contract_address = c.contract_address
               WHERE lower(a.contract_address) LIKE ?
                  OR lower(COALESCE(a.token_name, c.token_name, '')) LIKE ?
                  OR lower(COALESCE(a.ticker, c.ticker, '')) LIKE ?
               ORDER BY a.alerted_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["contract_address"], r["token_name"], r["ticker"])
        hits.append(SearchHit(
            canonical_id=r["contract_address"],
            entity_kind="token",
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

> **Test addendum (plan-review-1 backend SHOULD-FIX #7):** add a test that exercises the LEFT JOIN by inserting an alerts row with `token_name=NULL, ticker=NULL` and a matching candidates row with `ticker='CHIP'`. Confirm the query still returns the alert when searched for 'chip'.

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
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(sql, (pattern, pattern, pattern, limit))
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        # Some snapshot tables allow nullable name (e.g. slow_burn_candidates.name).
        # _classify_match tolerates None fields — defensive.
        mq = _classify_match(q, r["symbol"], r["name"], r["coin_id"])
        hits.append(SearchHit(
            canonical_id=r["coin_id"],
            entity_kind="token",
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
    async with _ro_db(db_path) as conn:
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
        # so dedup at orchestrator level won't collapse distinct messages.
        # entity_kind="tg_msg" tells the frontend NOT to render a TokenLink.
        hits.append(SearchHit(
            canonical_id=f"tg_msg:{r['id']}",
            entity_kind="tg_msg",
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
    async with _ro_db(db_path) as conn:
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
            entity_kind="token",
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
    async with _ro_db(db_path) as conn:
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
        # If the X-narrative classifier resolved the tweet to a coin_id, render
        # it as a token (TokenLink links to CoinGecko). Otherwise the canonical
        # is a synthetic x_alert:<id> — entity_kind suppresses the TokenLink.
        if r["resolved_coin_id"]:
            canonical = r["resolved_coin_id"]
            kind = "token"
            chain = "coingecko"
        else:
            canonical = f"x_alert:{r['id']}"
            kind = "x_alert"
            chain = None
        hits.append(SearchHit(
            canonical_id=canonical,
            entity_kind=kind,
            symbol=(r["extracted_cashtag"] or "").lstrip("$") or None,
            name=f"@{r['tweet_author']} (X)",
            chain=chain,
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


async def _safe_call(coro):
    """Wrap a search coroutine so a missing table or other DB error returns []."""
    try:
        return await coro
    except (aiosqlite.OperationalError, FileNotFoundError):
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
    # Stable sort: match_quality (best first), source count (more sources first),
    # then last_seen_at DESC. Negate for DESC ordering on the score tuple.
    merged.sort(key=lambda h: (_MATCH_QUALITY_RANK[h.match_quality],
                                -(len(h.sources)),
                                # Latest-seen first: empty strings sort last
                                # via the trailing negation trick.
                                -(int(_ts_to_int(h.last_seen_at)))))
    # Pre-slice total for honest truncation flag (plan-review-1 NICE-TO-HAVE #12).
    total_pre_slice = len(merged)
    merged = merged[:limit]
    return SearchResponse(
        query=q,
        total_hits=len(merged),
        hits=merged,
        truncated=total_pre_slice > limit,
    )


def _ts_to_int(ts: str | None) -> int:
    """Map ISO timestamp to a sortable int (epoch-seconds). None → 0."""
    if not ts:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return 0
```

- [ ] **Step 7.4: Run — PASS**

- [ ] **Step 7.5: Commit**

```bash
git add dashboard/search.py tests/test_dashboard_search.py
git commit -m "feat(dashboard-search): orchestrator with dedup, ranking, SQL-injection-safe params"
```

---

### Task 8: API route + module-global test isolation

**Files:**
- Modify: `dashboard/api.py`
- Test: `tests/test_dashboard_search.py` (append integration test + fixture)

> **Plan-review-1 backend MUST-FIX #1:** `dashboard/api.py` uses module-level globals `_db_path` and `_scout_db` (api.py:47-50). Successive `create_app(db_path=...)` calls in different tests leak state — the cached `_scout_db` from test A is reused by test B against a different DB. The fix is a pytest fixture that resets both module-globals after each test.

- [ ] **Step 8.1: Add the isolation fixture + write failing integration test**

```python
# Prepend to tests/test_dashboard_search.py imports section
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_dashboard_module_globals():
    """Reset module-level state in dashboard.api between tests.
    Required because create_app() mutates module globals (api.py:47-50)."""
    import dashboard.api as _api
    saved_db_path = _api._db_path
    saved_scout_db = _api._scout_db
    yield
    _api._db_path = saved_db_path
    _api._scout_db = saved_scout_db


# Append to tests/test_dashboard_search.py
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
    # _seed_candidates required because _ro_db checks file existence.
    await _seed_candidates(db_path)
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

- [ ] **Step 9.1: Create GlobalSearch.jsx (folds plan-review-1 frontend MUST-FIX 1-5 + SHOULD-FIX 6,7,8,9,10,12,17)**

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

// Map underscore-style match-quality values to dash-style CSS class suffixes
// (existing project CSS convention is kebab-case throughout — review SHOULD-FIX #8).
function qualityClass(mq) {
  return `gs-quality-${(mq || 'substring').replace(/_/g, '-')}`
}

function fmtTs(ts) {
  return ts ? ts.slice(0, 16).replace('T', ' ') : ''
}

// True if the focused element is an input/textarea/contenteditable — used to
// avoid hijacking "/" while the user is typing into another input on the page
// (review MUST-FIX #2).
function isTypingInEditableElement(el) {
  if (!el) return false
  const tag = el.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (el.isContentEditable) return true
  return false
}

export default function GlobalSearch() {
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [loading, setLoading] = useState(false)
  const [errored, setErrored] = useState(false)
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const inputRef = useRef(null)
  const dropdownRef = useRef(null)
  const abortRef = useRef(null)

  const doSearch = useCallback(async (query) => {
    if (!query || query.trim().length < 2) {
      setResults(null)
      setErrored(false)
      return
    }
    if (abortRef.current) abortRef.current.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setLoading(true)
    setErrored(false)
    try {
      const r = await fetch(`/api/search?q=${encodeURIComponent(query)}`, { signal: ctrl.signal })
      if (r.ok) {
        const body = await r.json()
        setResults(body)
      } else if (r.status === 400) {
        setResults({ query, total_hits: 0, hits: [], truncated: false })
      } else {
        setErrored(true)
        setResults(null)
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        setErrored(true)
        setResults(null)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  // Debounced search — but skip the timer entirely on <2 chars (review SHOULD-FIX #9, #12).
  useEffect(() => {
    if (q.trim().length < 2) {
      setResults(null)
      setErrored(false)
      return undefined
    }
    const t = setTimeout(() => doSearch(q), 300)
    return () => clearTimeout(t)
  }, [q, doSearch])

  // Reset highlight when results change
  useEffect(() => {
    setActiveIdx(results && results.hits && results.hits.length > 0 ? 0 : -1)
  }, [results])

  // Global hotkeys: Ctrl/Cmd+K focuses; "/" focuses when NOT typing in another
  // input (review MUST-FIX #2 + SHOULD-FIX #6).
  useEffect(() => {
    const onKey = (e) => {
      // Ctrl/Cmd + K: focus search regardless of where user is typing
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
        setOpen(true)
        return
      }
      // Bare "/" only when user is NOT in another editable element
      if (e.key === '/' && !isTypingInEditableElement(document.activeElement)) {
        e.preventDefault()
        inputRef.current?.focus()
        setOpen(true)
        return
      }
      // Escape: close + abort in-flight (review MUST-FIX #2 + SHOULD-FIX #10)
      if (e.key === 'Escape' && document.activeElement === inputRef.current) {
        if (abortRef.current) abortRef.current.abort()
        setOpen(false)
        inputRef.current?.blur()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const hits = results?.hits || []

  // Arrow-key navigation through results (review SHOULD-FIX #6)
  const onInputKeyDown = useCallback((e) => {
    if (!open || hits.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx((i) => Math.min(i + 1, hits.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter' && activeIdx >= 0) {
      const h = hits[activeIdx]
      // Only open external link for real token hits — non-token hits (tg_msg,
      // x_alert) have no useful external URL.
      if (h && h.entity_kind === 'token') {
        const tokenId = h.contract_address || h.canonical_id
        const target = h.chain === 'coingecko' || !tokenId.startsWith('0x')
          ? `https://www.coingecko.com/en/coins/${tokenId}`
          : `https://dexscreener.com/${h.chain}/${tokenId}`
        window.open(target, '_blank', 'noopener,noreferrer')
      }
    }
  }, [open, hits, activeIdx])

  // onMouseDown on dropdown prevents the input from blurring before our click
  // handler resolves — fixes the TokenLink click race (review MUST-FIX #3).
  const onDropdownMouseDown = (e) => { e.preventDefault() }

  return (
    <div
      className="global-search"
      role="combobox"
      aria-haspopup="listbox"
      aria-expanded={open && hits.length > 0}
      aria-owns="gs-listbox"
    >
      <input
        ref={inputRef}
        className="global-search-input"
        type="text"
        placeholder='Search tokens, alerts, KOL msgs... (Ctrl+K or "/")'
        value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => { /* dropdown's onMouseDown handles click-through */ }}
        onKeyDown={onInputKeyDown}
        aria-autocomplete="list"
        aria-controls="gs-listbox"
        aria-activedescendant={activeIdx >= 0 ? `gs-hit-${activeIdx}` : undefined}
      />
      {open && q.length >= 2 && (
        <div
          className="global-search-dropdown"
          role="listbox"
          id="gs-listbox"
          ref={dropdownRef}
          onMouseDown={onDropdownMouseDown}
        >
          {loading && <div className="gs-state">Searching...</div>}
          {!loading && errored && <div className="gs-state gs-error">Search request failed — try again.</div>}
          {!loading && !errored && results && results.total_hits === 0 && (
            <div className="gs-state">No results for "{q}"</div>
          )}
          {!loading && !errored && hits.map((h, idx) => (
            <div
              key={h.canonical_id}
              id={`gs-hit-${idx}`}
              role="option"
              aria-selected={idx === activeIdx}
              className={`gs-hit ${idx === activeIdx ? 'gs-hit-active' : ''}`}
              onMouseEnter={() => setActiveIdx(idx)}
            >
              <div className="gs-hit-main">
                {/* Render TokenLink only for real token entities — prevents broken
                    coingecko.com/coins/tg_msg:123 URLs (review MUST-FIX #4). */}
                {h.entity_kind === 'token' ? (
                  <TokenLink
                    tokenId={h.contract_address || h.canonical_id}
                    symbol={h.symbol || h.name}
                    chain={h.chain}
                  />
                ) : (
                  <span className="gs-non-token">{h.symbol || h.name || h.canonical_id}</span>
                )}
                <span className="gs-hit-name">{h.entity_kind !== 'token' ? '' : h.name}</span>
                <span className={`gs-hit-quality ${qualityClass(h.match_quality)}`}>{h.match_quality}</span>
              </div>
              <div className="gs-hit-sources">
                {h.sources.map((src) => (
                  <span key={src} className="gs-source-badge" title={`${src}: ${h.source_counts[src] || 1}`}>
                    {SOURCE_LABELS[src] || src}{h.source_counts[src] > 1 ? ` ×${h.source_counts[src]}` : ''}
                  </span>
                ))}
                {h.best_paper_trade_pnl_pct != null && (
                  <span
                    className={`gs-pnl-badge ${h.best_paper_trade_pnl_pct >= 0 ? 'gs-pnl-pos' : 'gs-pnl-neg'}`}
                    title="best paper-trade pnl%"
                  >
                    PnL {h.best_paper_trade_pnl_pct >= 0 ? '+' : ''}{h.best_paper_trade_pnl_pct.toFixed(1)}%
                  </span>
                )}
              </div>
              <div className="gs-hit-meta">
                {h.first_seen_at && <span>first: {fmtTs(h.first_seen_at)}</span>}
                {h.last_seen_at && h.last_seen_at !== h.first_seen_at &&
                  <span> · last: {fmtTs(h.last_seen_at)}</span>}
              </div>
            </div>
          ))}
          {!loading && !errored && results && results.truncated && (
            <div className="gs-state gs-truncated">Showing first {hits.length} results — refine query for more.</div>
          )}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 9.2: Mount in App.jsx header (with safe layout — review frontend MUST-FIX #5)**

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

Layout safety comes from CSS in Step 9.3 — `.header { flex-wrap: wrap }`, `.global-search { min-width: 0 }`, and a `@media (max-width: 600px)` rule.

- [ ] **Step 9.3: Add CSS to style.css (review frontend MUST-FIX #1, #5 + SHOULD-FIX #8)**

Append to `dashboard/frontend/style.css`:

```css
/* --- Global Search --- */
/* Layout safety: header wraps on narrow viewports; min-width:0 lets flex
   children shrink below their intrinsic width (review MUST-FIX #5). */
.header { flex-wrap: wrap; gap: 12px; }
.global-search {
  position: relative;
  flex: 1 1 280px;
  min-width: 0;
  max-width: 480px;
}
.global-search-input {
  width: 100%; box-sizing: border-box; padding: 8px 12px;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  color: var(--color-text-primary);  /* review MUST-FIX #1 — exact var name */
  font-size: 13px;
}
.global-search-input:focus {
  outline: none;
  border-color: var(--color-accent-blue);
  box-shadow: 0 0 0 2px rgba(88,166,255,0.25);
}
.global-search-input::placeholder { color: var(--color-text-secondary); }
.global-search-dropdown {
  position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px;
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-border);
  border-radius: 6px; max-height: 480px; overflow-y: auto; z-index: 100;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.gs-state { padding: 12px; color: var(--color-text-secondary); font-size: 12px; }
.gs-error { color: var(--color-accent-red); }
.gs-hit {
  padding: 10px 12px;
  border-bottom: 1px solid var(--color-border);
  cursor: default;
}
.gs-hit:last-child { border-bottom: none; }
.gs-hit-active { background: rgba(88,166,255,0.08); }
.gs-hit-main { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.gs-non-token { color: var(--color-text-primary); font-weight: 600; }
.gs-hit-name { color: var(--color-text-secondary); font-size: 12px; flex: 1; }
.gs-hit-quality {
  font-size: 10px; padding: 1px 6px; border-radius: 8px;
  background: var(--color-bar-bg);
  color: var(--color-text-secondary);
}
/* Class suffixes use dash-not-underscore (review SHOULD-FIX #8) */
.gs-quality-exact-symbol, .gs-quality-exact-contract {
  background: rgba(88,166,255,0.18);
  color: var(--color-accent-blue);
}
.gs-hit-sources { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.gs-source-badge {
  font-size: 10px; padding: 1px 6px; border-radius: 8px;
  background: var(--color-bar-bg);
  color: var(--color-text-secondary);
}
.gs-pnl-badge {
  font-size: 10px; padding: 1px 6px; border-radius: 8px;
}
.gs-pnl-pos { background: rgba(29,158,117,0.15); color: var(--color-accent-green); }
.gs-pnl-neg { background: rgba(248,81,73,0.15); color: var(--color-accent-red); }
.gs-hit-meta { font-size: 10px; color: var(--color-text-secondary); margin-top: 4px; }
.gs-truncated { font-style: italic; }
@media (max-width: 600px) {
  .global-search { max-width: none; }
}
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
