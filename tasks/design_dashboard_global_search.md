**New primitives introduced:** NONE — design refines the implementation plan in `plan_dashboard_global_search.md`. Same primitive surface: one read-only REST endpoint, one search service module, one React component. No new Settings, no schema migrations, no new writers.

# Design: Dashboard Global Search

Companion to `plan_dashboard_global_search.md` (committed `54f41db`, refined `cd3cad3` after plan-review-1). The plan covers WHAT and the bite-sized task ladder. This design covers contracts, data flow, edge cases, and decision rationale — the things a code reviewer or a future maintainer needs to verify the implementation is correct in isolation.

## 1. API contract

### Request

```
GET /api/search?q=<string>&limit=<int>
```

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `q` | string | `min_length=1, max_length=128` enforced by FastAPI `Query()` | Raw user input. Backend normalizes — leading `$` / `#` ticker sigils stripped, whitespace trimmed, lowercased. After normalization, queries < 2 chars return `400 Bad Request`. |
| `limit` | int | `ge=1, le=200`, default `50` | Per-source `LIMIT`. Total aggregated hits is **after** dedup, so may be ≤ this. |

### Response — `200 OK`

```json
{
  "query": "chip",
  "total_hits": 3,
  "truncated": false,
  "hits": [
    {
      "canonical_id": "chip-coin",
      "entity_kind": "token",
      "symbol": "CHIP",
      "name": "ChipCoin",
      "chain": "coingecko",
      "contract_address": null,
      "sources": ["candidates", "gainers_snapshots", "paper_trades"],
      "source_counts": {"candidates": 1, "gainers_snapshots": 14, "paper_trades": 3},
      "first_seen_at": "2026-05-10T08:00:00+00:00",
      "last_seen_at": "2026-05-14T12:00:00+00:00",
      "match_quality": "exact_symbol",
      "best_paper_trade_pnl_pct": 25.4
    }
  ]
}
```

### Response — `400 Bad Request`

```json
{"detail": "query too short: ''"}
```

Returned for queries that fail `normalize_query` (empty, whitespace-only, or < 2 chars after stripping sigils).

### Response — `500 / 503`

Search endpoint should NEVER 500 on a query that reaches `run_search`. All per-table search functions are wrapped in `_safe_call` which catches `aiosqlite.OperationalError` (table missing) and `FileNotFoundError` (DB rotation race). FastAPI's default exception handler still applies for genuine programmer errors (e.g. import-time crash).

## 2. SearchHit field semantics

| Field | Required | Description |
|---|---|---|
| `canonical_id` | yes | Stable identifier used for deduplication. For real tokens: `contract_address` (preferred) OR `coin_id` (CoinGecko slug). For non-token hits: `tg_msg:<id>` (TG message hit), `x_alert:<id>` (unresolved X alert). |
| `entity_kind` | yes | `"token"` (renders as TokenLink) \| `"tg_msg"` (renders plain text) \| `"x_alert"` (renders plain text). Frontend uses this to gate TokenLink rendering — prevents broken external URLs like `coingecko.com/coins/tg_msg:123`. |
| `symbol` | no | Ticker / cashtag (without `$`). May be NULL for `tg_msg` hits. |
| `name` | no | Human-readable token name OR for non-token hits: `"@author (X)"` / `"<channel> #<msg_id>"`. |
| `chain` | no | `"solana"`, `"ethereum"`, `"base"`, `"coingecko"`, or NULL. |
| `contract_address` | no | EVM/SVM contract if known. NULL for CG-only tokens. |
| `sources` | yes | Sorted list of source table names where this entity matched. |
| `source_counts` | yes | `{source_name: row_count}` — how many rows in each source matched. |
| `first_seen_at` | no | ISO-8601 UTC. Earliest match timestamp across all sources. |
| `last_seen_at` | no | ISO-8601 UTC. Latest match timestamp across all sources. |
| `match_quality` | yes | `"exact_symbol"` > `"exact_contract"` > `"prefix"` > `"substring"` > `"text"`. |
| `best_paper_trade_pnl_pct` | no | If the entity was paper-traded, max `pnl_pct` across all its closed trades. NULL otherwise. |

## 3. Data flow

### 3.1 Happy path (operator types "chip")

```
t=0ms:    keystroke → React state.q = "chip"
t=0ms:    useEffect debounce timer starts (300ms)
t=300ms:  fetch("/api/search?q=chip")
t=305ms:  uvicorn → FastAPI route → run_search(db_path, "chip", 50)
t=306ms:  normalize_query("chip") → "chip"
t=306ms:  asyncio.gather over 13 per-table coros
t=306-450ms: per-table queries run (serialized via SQLite file lock)
            - search_candidates                   → 1 hit
            - search_paper_trades                  → 1 hit (best_pnl=25.4)
            - search_alerts                        → 0 hits
            - search_snapshots(gainers_snapshots)  → 1 hit, count=14
            - search_snapshots(trending_snapshots) → 0
            - search_snapshots(momentum_7d)        → 0
            - search_snapshots(slow_burn_*)        → 0
            - search_snapshots(velocity_alerts)    → 0
            - search_snapshots(volume_spikes)      → 0
            - search_snapshots(predictions)        → 0
            - search_tg_messages                   → 2 hits (entity_kind=tg_msg)
            - search_tg_signals                    → 0
            - search_narrative_inbound             → 0
t=450ms:  Dedup phase: 3 token hits collapse onto canonical_id="chip-coin"
          (since they share a coin_id). 2 tg_msg hits stay distinct
          (different canonical_ids per message).
t=451ms:  Sort: exact_symbol="chip" → rank 0; tg_msg text matches → rank 4
t=452ms:  SearchResponse({query:"chip", total_hits:3, truncated:false, hits:[...]})
t=452ms:  → FastAPI → JSON serialize → uvicorn → network
t=475ms:  Browser receives JSON
t=476ms:  React setResults(body); dropdown renders 3 rows
```

### 3.2 Dedup mechanics

A token hit from `candidates` (canonical_id = contract_address `chip-coin`) and a hit from `gainers_snapshots` (canonical_id = coin_id `chip-coin`) collapse into ONE result IFF `canonical_id` matches byte-for-byte. This requires the ingestion pipeline to use coingecko slug as both the `coin_id` in `gainers_snapshots` AND the `contract_address` in `candidates` for CG-pipeline tokens — which it does (verified via inspection of `scout/ingestion/coingecko.py` and the prod row inserted at `2026-05-14T12:53:56Z` for Quack AI where `contract_address="q"` and gainers_snapshots `coin_id="q"`).

For EVM/SVM tokens, contract_address is hex/base58 and gainers_snapshots is empty (those tokens don't go through the CG-markets layer). Dedup correctly collapses across `candidates` + `paper_trades` + `tg_social_signals` since all three use the same contract_address.

**Edge case:** if a token appears in BOTH the CG layer (as `coin_id="bitcoin"`) AND the DEX layer (as `contract_address="0xabc"`), they would NOT dedup — two separate SearchHit rows. This is the right behavior — they're genuinely different on-chain entities by our schema definition.

### 3.3 Concurrency model

`asyncio.gather()` schedules all 13 coroutines onto the event loop simultaneously. Each opens its own `_ro_db` connection (URI `?mode=ro`). SQLite's file-level lock SERIALIZES the actual reads — only one reader at a time at the OS layer. The benefit of `gather` is mostly that Python coroutine overhead is hidden behind the I/O wait.

**Why not a single shared connection?** See plan §"Deferred refactor". Acceptable for 13 short reads at single-user dashboard scale. Cost is ~13× connection-open (~5-10ms each warm). Will revisit if perf monitoring shows actual user pain.

**Read-only concurrency safety:** `mode=ro` URI ensures no write lock contention with the pipeline writer process running on the same DB. WAL mode (already enabled at `scout/db.py:60`) allows readers and writers to coexist.

## 4. SQL safety analysis (defense layers)

| Layer | Mechanism | What it blocks |
|---|---|---|
| 1. Input validation | FastAPI `Query(..., min_length=1, max_length=128)` | Empty query, overlong query (DoS via huge LIKE pattern) |
| 2. Normalization | `normalize_query()` strips whitespace/sigils, rejects <2 chars | Empty-after-strip, single-char fan-out |
| 3. Parameterization | All `?` placeholders bind values; LIKE pattern wrapped via Python f-string then bound | Classic `'; DROP TABLE` injection |
| 4. Table name whitelist | `_SNAPSHOT_TABLES = {...}` set membership check BEFORE f-string interpolation in `search_snapshots` | Injection via dynamic table name (currently never user-supplied — but defense in depth) |
| 5. Read-only mode | `_ro_db` opens `file:<path>?mode=ro` URI | Any write attempt — DROP / INSERT / DELETE / UPDATE rejected by SQLite driver |

The combination means a SQL injection attempt must (a) bypass FastAPI's length cap, (b) survive Python string handling, (c) somehow escape `?` parameter binding, (d) trigger a write operation that (e) the read-only mode rejects. No path through all five layers exists.

**Tested:** `test_run_search_sql_injection_attempt` (plan Task 7.1) inserts query `'; DROP TABLE candidates; --` and verifies (a) zero results returned, (b) `candidates` row count unchanged after the call.

## 5. Edge cases

### 5.1 Empty database

`run_search` on a fresh DB where `_ro_db` succeeds but no tables exist: each per-table coro raises `aiosqlite.OperationalError("no such table: candidates")`. `_safe_call` catches → returns `[]`. Final response: `{query, total_hits: 0, hits: [], truncated: false}`. ✓

### 5.2 Database file missing

`_ro_db` raises `FileNotFoundError` BEFORE opening. `_safe_call` catches → returns `[]`. Same response as above. ✓

### 5.3 Database file mid-rotation

Pipeline rotates `scout.db` (rare — only on backup-restore). `_ro_db` either succeeds against the new file or raises `FileNotFoundError` for a single in-flight request. Operator's next keystroke triggers a fresh request that succeeds. ✓

### 5.4 Query contains SQL metachars

`run_search("'; DROP TABLE candidates; --")`. After `normalize_query`: `q = "'; drop table candidates; --"`. Pattern: `"%'; drop table candidates; --%"`. Bound via `?` placeholder. LIKE returns no rows (no `candidates.ticker` row contains that substring). Read-only mode would reject the DROP regardless. ✓

### 5.5 Query is unicode / emoji

`q = "🐶"`. `normalize_query` lowercases (no-op for emoji), 2 chars (≥2 codepoints? actually 1 codepoint but `len("🐶")` in Python is 1 codepoint — REJECTED as `QueryTooShortError`). Operator gets a 400. Acceptable. If we later support emoji search, lift the 2-char floor for non-ASCII.

### 5.6 Query is a contract address

`q = "0xABC1234567890ABCDEF1234567890ABCDEF12345"`. `normalize_query` lowercases. `candidates.contract_address` is case-sensitive on Solana (base58) but case-insensitive on EVM. We use `lower(contract_address) LIKE lower(?)` — works correctly for EVM addresses. For Solana addresses, base58 is case-sensitive but our normalization lowercases BOTH the column and the query, so `lower(...)` produces a deterministic case-folded match — may produce false positives if two different Solana addresses differ only in case (effectively impossible at the 32-44 char length).

### 5.7 Long query (128 chars)

FastAPI rejects 129+ chars at validation. 128-char query lands in `normalize_query`. LIKE pattern is `%<128 chars>%` = 130 chars. SQLite handles ≤1MB LIKE patterns; 130 chars is trivial.

### 5.8 Very common substring ("the", "0x", "coin")

`q = "0x"` → matches potentially many contract addresses. Each per-table function has `LIMIT limit` (default 50). Aggregate cost is bounded. The dropdown shows up to 50 deduped results with `truncated=true`. Operator refines query.

### 5.9 Concurrent searches (same user typing fast)

Frontend's `AbortController` cancels the prior `fetch` on the next keystroke. Backend doesn't see the cancellation immediately — the old query keeps running until SQLite returns. Wasted CPU is bounded by `limit=50` per table × 13 tables. Backend doesn't need to be aware of cancellation.

### 5.10 Backend search request timeout

No explicit timeout. Realistic upper bound is ~500ms per the perf measurement. If the DB is locked by a pipeline write for >5s, the request stalls. Uvicorn's default timeout is 60s. Operator UX during this stall: dropdown shows "Searching..." indefinitely. **Mitigation:** consider adding `asyncio.wait_for(coro, timeout=5.0)` around `run_search`. **Decision: NOT in this PR.** SQLite WAL mode means readers and writers don't block each other, so the 5s-lock scenario is hypothetical. Revisit if it ever happens.

## 6. Frontend interaction details

### 6.1 Focus management

- **Tab** to focus: standard keyboard tab order places the input after the H1 title.
- **Ctrl+K / Cmd+K**: focus from anywhere; `e.preventDefault()` to suppress Firefox's "focus URL bar" default. Test on Firefox 115+, Chrome 118+, Safari 17+.
- **`/` key**: focus IFF the active element is not an `<input>`, `<textarea>`, `<select>`, or `contentEditable`. Same idiom as GitHub / Reddit / docs.python.org.
- **Esc**: aborts in-flight fetch, closes dropdown, blurs input. Only fires when input itself has focus (avoid hijacking Esc when operator is in a modal).

### 6.2 Click-through to result

The blur-vs-click race is solved by `onMouseDown={(e) => e.preventDefault()}` on the dropdown — this prevents the input from losing focus when the user clicks a result row. The click handler on TokenLink (which delegates to the `<a>` natively) still fires. After the click, target opens in a new tab (`target="_blank"`).

### 6.3 Keyboard navigation through results

- **Down arrow**: increment `activeIdx`, clamped to `hits.length - 1`.
- **Up arrow**: decrement, clamped to 0.
- **Enter** with `activeIdx >= 0`: open external URL for the highlighted hit IF `entity_kind === "token"`. For tg_msg / x_alert entries, Enter is a no-op (no useful target URL).
- **Mouse hover**: `setActiveIdx(idx)` keeps mouse and keyboard in sync.

### 6.4 ARIA semantics

- `.global-search` div: `role="combobox"`, `aria-haspopup="listbox"`, `aria-expanded`, `aria-owns="gs-listbox"`.
- Input: `aria-autocomplete="list"`, `aria-controls="gs-listbox"`, `aria-activedescendant="gs-hit-N"`.
- Dropdown: `role="listbox"`, `id="gs-listbox"`.
- Each hit: `role="option"`, `id="gs-hit-N"`, `aria-selected={idx === activeIdx}`.

Screen reader announces: "Search tokens, alerts, KOL msgs. Combobox, expanded. 3 options. CHIP, exact symbol match, candidates, gainers snapshots." — close to GitHub's command palette.

### 6.5 Visual states

| State | Indicator |
|---|---|
| Idle (no query) | Placeholder text shown, no dropdown |
| Query < 2 chars | Placeholder if empty, no dropdown |
| Loading | Dropdown shows "Searching..." |
| Error (fetch rejected, non-abort) | Dropdown shows "Search request failed — try again." in red |
| No results | Dropdown shows `No results for "{q}"` |
| Has results | Each hit row with TokenLink/non-link, source badges, PnL badge, timestamps |
| Truncated | Footer line "Showing first N results — refine query for more." |

## 7. Telemetry / observability

No new structured logs in this PR. Rationale: read-only endpoint, no state mutation, single-operator load. If we later need to know "how often does the operator search?" / "do searches commonly return zero?", add `structlog.info("dashboard_search", q_len=len(q), hits=n, dur_ms=...)` at the end of `run_search`. Deferred per YAGNI.

**Per global CLAUDE.md §12a**: this is a new operator-facing endpoint but NOT a new pipeline table (the table-freshness watchdog rule doesn't apply). Read-only with no automated state transitions — §12b also doesn't apply. The audit lane this falls under is plain HTTP-level monitoring (latency, error rate), which Uvicorn's access log + the existing `/health` endpoint already cover.

## 8. Rollback plan

Read-only feature. Rollback steps:

1. `git revert <merge-sha>` on master
2. `git push origin master`
3. On srilu-vps: `cd /root/gecko-alpha && git pull && systemctl restart gecko-dashboard`

No data to clean up, no migrations to undo. The `/api/search` route disappears, the frontend dist regenerates without the search box, and the operator's bookmark URL still works (dashboard root renders the existing tabs).

## 9. Validation checklist (executed in Build phase)

Each item maps to a Plan task or a verification step:

- [ ] `normalize_query` rejects empty / <2 / whitespace-only (plan Task 2)
- [ ] `search_candidates` matches by symbol / name / contract (plan Task 3)
- [ ] `search_paper_trades` aggregates by `(token_id, symbol, name, chain)` and surfaces `best_pnl` (plan Task 4)
- [ ] `search_alerts` recovers ticker/token_name from candidates via LEFT JOIN (plan Task 4 — review-1 SHOULD-FIX #7)
- [ ] `search_snapshots` accepts whitelisted tables only (plan Task 5)
- [ ] `search_tg_messages` returns `entity_kind="tg_msg"` (plan Task 6)
- [ ] `search_narrative_inbound` returns `entity_kind="token"` only when resolved (plan Task 6)
- [ ] `run_search` deduplicates correctly (plan Task 7)
- [ ] `run_search` rejects SQL injection attempt (plan Task 7)
- [ ] `run_search` returns `truncated=true` honestly when pre-slice total > limit
- [ ] `/api/search` returns 400 on short query, 200 on valid (plan Task 8)
- [ ] `create_app` module-globals reset between tests (plan Task 8 fixture)
- [ ] Frontend renders TokenLink only for `entity_kind === "token"` (plan Task 9)
- [ ] Ctrl+K + `/` focus shortcuts work in isolation and don't hijack other inputs
- [ ] Arrow keys navigate; Enter opens token hits in new tab
- [ ] No header overflow on 1024px / 600px viewport widths
- [ ] CSS variables resolve (no fallback to browser default)
- [ ] Read-only DB connection — try a SQL injection that would write and confirm zero impact

## 10. Open questions deferred

- **Full-text search (FTS5)** — defer until search latency exceeds 500ms p95 on prod.
- **Operator search history / saved queries** — defer; no current ask.
- **`signal_events` integration** — defer; 7.5M rows, would require a count-only follow-up join on resolved canonical_ids.
- **Cross-VPS search** (shift-agent, Hermes side data) — defer; separate trust boundary.
- **Mobile-first redesign of the dropdown** — defer; operator uses dashboard from a laptop, not phone.
