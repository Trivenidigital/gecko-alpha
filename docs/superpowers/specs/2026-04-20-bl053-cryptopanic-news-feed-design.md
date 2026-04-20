# BL-053 — CryptoPanic News Feed Watcher — Design Spec

**Status:** Draft
**Author:** autonomous loop (claude-opus-4-7)
**Date:** 2026-04-20
**Branch:** `feat/bl-053-cryptopanic-news-feed`

---

## 1. Goal

Add a public-tier CryptoPanic news-feed watcher that **tags** tokens in the existing
candidate pool with news context (count, sentiment, macro flag) and persists post
history to DB for future analysis. Research-only: no alerts, no scoring signal
activation in this increment.

## 2. Non-Goals

- No alerts
- No dashboard changes (deferred)
- No paper-trading signal
- No SCORER_MAX_RAW change (scoring path shipped but flag-gated; see §11)
- No sentiment calibration (v0 uses a simple vote-delta heuristic)
- No new token discovery from news (CryptoPanic ties posts to tickers, which
  collide across chains — cannot produce deterministic contract addresses)

## 3. Context

- CryptoPanic v1 REST API is free-tier (registration required for `auth_token`).
  Rate limit ~50–200 req/hr on the free plan.
- Our cycle runs every `SCAN_INTERVAL_SECONDS` (default 300s). One request per
  cycle = 12 req/hr — well under any free-tier cap.
- Pattern parallels `scout/ingestion/*.py` modules: async aiohttp fetch,
  parse → pydantic model, return list.
- We already have precedent for flag-gated features (LUNARCRUSH_ENABLED,
  CHAINS_ENABLED, etc.) that stay off by default.

## 4. Architecture

New `scout/news/` package, mirrors `scout/ingestion/` shape:

```
scout/news/
  __init__.py              # empty package marker
  cryptopanic.py           # fetch + categorize + enrich
  schemas.py               # CryptoPanicPost pydantic model
```

Integration point: `run_cycle()` in `scout/main.py`. Concretely:

1. Immediately after `aggregate(...)` (current line ~461) and before the
   per-token holder enrichment loop (current lines ~464–480), kick off
   `fetch_cryptopanic_posts(session, settings)` as a separate
   `asyncio.create_task(...)` if `settings.CRYPTOPANIC_ENABLED`.
2. The existing holder enrichment + vol_7d loops run unchanged against the
   `enriched` list.
3. After vol_7d enrichment completes (before the scorer loop starts at
   current line ~491), `await` the CryptoPanic task with a 10s timeout. On
   success, run in-memory tagging over `enriched` via `model_copy`. On
   timeout or exception, log `cryptopanic_fetch_failed` and continue with
   untagged tokens.

No race risk: both enrichment steps mutate `enriched` via `model_copy` (new
objects), and the CryptoPanic tag write happens strictly after both have
produced their final list entries. When `CRYPTOPANIC_ENABLED=False` the task
is never created and the flow is byte-identical to pre-BL-053.

## 5. Data Flow

```
CryptoPanic /v1/posts/?filter=hot[&auth_token=...]
      │
      ├─ parse posts → list[CryptoPanicPost]
      │
      ├─ persist to DB.cryptopanic_posts (INSERT OR IGNORE, 7d retention)
      │
      └─ build dict[ticker_upper] → list[CryptoPanicPost]
            │
            └─ for each candidate: model_copy(update={
                   news_count_24h, latest_news_sentiment, macro_news_flag,
                   news_tag_confidence
               })
```

Matching is **in-memory** over the just-fetched batch — we do NOT query
`cryptopanic_posts` during tagging. The DB table is for analytics and
historical replay only; per-cycle tagging uses the in-session list.

## 6. CandidateToken Field Additions

Added to `scout/models.py` near the existing optional enrichment fields:

```python
from typing import Literal

# CryptoPanic news tags (BL-053)
news_count_24h: int | None = None
latest_news_sentiment: Literal["bullish", "bearish", "neutral"] | None = None
macro_news_flag: bool | None = None
news_tag_confidence: Literal["ticker_only"] | None = None
```

All default to None so pre-existing tokens and tests remain unaffected.

**`news_tag_confidence` rationale (ticker collision guard):** CryptoPanic
posts reference tokens only by ticker (e.g. `code: "PEPE"`), which collides
wildly across chains (EVM PEPE forks, Solana PEPE forks). Any candidate
tagged from a CryptoPanic post gets `news_tag_confidence="ticker_only"` so
downstream consumers — especially the future scoring activation — can
decide whether to trust low-confidence tags or drop them. The value is None
for untagged tokens. No chain/contract alignment is attempted in this
increment; a future PR may add CMC/CoinGecko ID mapping for higher
confidence.

## 7. Sentiment Computation

Inputs: post's `votes` dict — `positive`, `negative`, `important`, `liked`,
`disliked`, `lol`, `toxic`.

Rule (uses deltas, not ratios, to stay robust on low-vote posts):

- `bullish` if `positive >= negative + 2`
- `bearish` if `negative >= positive + 2`
- `neutral` otherwise

A candidate's `latest_news_sentiment` comes from the most-recently-published
matched post (posts are sorted by `published_at DESC` before matching).

**Known limitation — fresh posts with 0/0 votes:** the delta rule classifies
fresh posts (which have very few votes) as `neutral`, even when their title
would obviously read as bearish ("XYZ rugged", "exploit drained $10M"). This
is an accepted constraint for v0. Mitigation lives in a follow-up increment
(see §17) — likely either keyword-based override or using CryptoPanic's
`filter=bearish` / `filter=important` pre-filter alongside the hot feed.

## 8. Macro Classification

A post is **macro** iff:

- `len(currencies) == 0`, OR
- `len(currencies) >= CRYPTOPANIC_MACRO_MIN_CURRENCIES` (default **4**)

Rationale: posts tagged with many tickers (e.g. "BTC, ETH, SOL, AVAX") are
usually market-wide commentary. Posts tagged with 1–3 tickers are
token-specific (e.g. "ETH ETF flows", "BTC + ETH correlation", "ARB + OP
roadmap"). Default bumped from 3→4 after reviewer feedback to avoid
false-positive macro tags on common 3-ticker ecosystem posts.

A candidate's `macro_news_flag` is True if **any** of its matched posts is macro.

## 9. DB Schema (additive)

```sql
CREATE TABLE IF NOT EXISTS cryptopanic_posts (
  id INTEGER PRIMARY KEY,
  post_id INTEGER UNIQUE NOT NULL,        -- CryptoPanic's own post id
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  published_at TEXT NOT NULL,              -- ISO8601 UTC
  currencies_json TEXT NOT NULL,           -- JSON array of ticker codes
  is_macro INTEGER NOT NULL,               -- 0|1
  sentiment TEXT NOT NULL,                 -- bullish|bearish|neutral
  votes_positive INTEGER NOT NULL DEFAULT 0,
  votes_negative INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT NOT NULL                 -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS ix_cryptopanic_published_at
  ON cryptopanic_posts(published_at DESC);
```

- `post_id UNIQUE` → idempotency: duplicate fetches are INSERT OR IGNORE.
- Pruned at the existing hourly `db.prune_old_candidates` call-site via a new
  sibling `prune_cryptopanic_posts(keep_days=CRYPTOPANIC_RETENTION_DAYS)`.
- **Prune must not silently fail:** the call-site wraps it in `try/except`
  matching the existing `db_prune` error pattern and emits a structured log
  `cryptopanic_prune_failed` on error. Unbounded growth otherwise ~40k rows
  after 7d × 12 req/hr × 20 posts, slow but real.
- **Migration idempotency:** `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF
  NOT EXISTS` must be invoked from `Database.initialize()` and proven
  idempotent by the existing init test (run twice, no error).

## 10. Config Additions

Added to `scout/config.py`:

```python
# -------- CryptoPanic News Feed (BL-053) --------
CRYPTOPANIC_ENABLED: bool = False
CRYPTOPANIC_API_TOKEN: str = ""                 # optional free tier token
CRYPTOPANIC_FETCH_FILTER: str = "hot"           # hot|rising|bullish|bearish|important
CRYPTOPANIC_MACRO_MIN_CURRENCIES: int = 4
CRYPTOPANIC_SCORING_ENABLED: bool = False       # gated scoring signal
CRYPTOPANIC_RETENTION_DAYS: int = 7
```

Mirrored entries appended to `.env.example` under a new section.

## 11. Scoring Signal (Gated, Off by Default)

`scout/scorer.py` — appended as **Signal 13**:

```python
# Signal 13: CryptoPanic bullish news (BL-053) -- 10 points, gated
if (
    settings.CRYPTOPANIC_SCORING_ENABLED
    and token.latest_news_sentiment == "bullish"
    and (token.news_count_24h or 0) >= 1
    and not token.macro_news_flag
):
    points += 10
    signals.append("cryptopanic_bullish")
```

**Important:** SCORER_MAX_RAW remains **198** in this PR. When
`CRYPTOPANIC_SCORING_ENABLED=False` (default) the signal never fires, so raw
score distribution and all 11 normalization thresholds are unchanged. Flipping
the flag to True in a future PR will require bumping SCORER_MAX_RAW to 208 and
updating tests in that PR; that is explicitly out of scope here.

## 12. Error Handling

CryptoPanic fetch module (`scout/news/cryptopanic.py`):

| Condition | Behaviour |
|---|---|
| Network / aiohttp.ClientError | exponential backoff 2,4,8s → return `[]` |
| HTTP 429 / 5xx | same backoff → return `[]` |
| HTTP 401 / 403 (bad token) | log once at WARNING, return `[]`, continue cycle |
| HTTP 200 but non-JSON body | log + return `[]` |
| Malformed post (missing title/id) | log + skip that post, continue others |
| Post with `currencies: null` (vs `[]`) | treat as macro (empty list) |
| Post with `code: ""` or missing `code` in a currency entry | skip that currency entry, keep others |
| Duplicate `post_id` within the same fetch batch | dedup in-memory before DB write |
| `CRYPTOPANIC_ENABLED=False` | fetch wrapper short-circuits to `[]` (no network call) |
| `CRYPTOPANIC_API_TOKEN=""` and `CRYPTOPANIC_ENABLED=True` | log once at WARNING `cryptopanic_auth_missing`, short-circuit to `[]` |

**Auth correction (reviewer feedback):** CryptoPanic v1 `/posts/` requires
`auth_token=<key>` as a query parameter. The `public=true` flag expands
result visibility but does NOT substitute for the token — unauthenticated
requests return 401/403. Therefore: if the token is empty, we do not hit
the endpoint at all (previous spec draft incorrectly proposed `public=true`
as a fallback).

Never raises to caller — pipeline must not fail on news fetch errors.

## 13. Observability

`structlog` events:
- `cryptopanic_fetch_started`
- `cryptopanic_fetch_completed` — `count`, `macro_count`, `bullish_count`, `bearish_count`
- `cryptopanic_fetch_failed` — `error`, `status`
- `cryptopanic_tokens_tagged` — `candidate_count`, `tagged_count`
- `cryptopanic_sigfire` (only when scoring flag True) — token, sentiment, news_count_24h

## 14. Rollout Safety

- Feature flag `CRYPTOPANIC_ENABLED=False` by default → zero production impact
  at merge.
- Opt-in: operator sets `CRYPTOPANIC_ENABLED=True` and (optionally)
  `CRYPTOPANIC_API_TOKEN=<free-token>` on VPS `.env`.
- Runtime disable: flip `CRYPTOPANIC_ENABLED=False` and restart — no DB state
  left dangling because table is pure data-collection.
- DB migration is additive (new table + new index) — no existing columns
  touched. Rollback = drop table manually if needed.

## 15. Test Plan

| Test file | Scope |
|---|---|
| `test_cryptopanic_schemas.py` | CryptoPanicPost parsing, `currencies: null` vs `[]`, `code: ""` or missing |
| `test_cryptopanic_fetch.py` | 200, 429, 5xx, 401, timeout, malformed body, 200-after-429 in same session, empty `results: []`, missing token short-circuits without network |
| `test_cryptopanic_parser.py` | sentiment edges (0/0 → neutral, ties → neutral, deltas ≥2 bullish/bearish), macro threshold (default 4), duplicate post_id within batch |
| `test_cryptopanic_enrichment.py` | ticker matching (case-insensitive), multi-post aggregation, no-match leaves fields None, `news_tag_confidence="ticker_only"` on any tag |
| `test_cryptopanic_db.py` | persist, idempotency on duplicate post_id, 7d prune, prune wrapped in try/except (error logged, no raise), `Database.initialize()` idempotent when called twice |
| `test_config_cryptopanic.py` | defaults (including MACRO_MIN=4), env-var overrides |
| `test_scorer_cryptopanic_gated.py` | signal silent when flag=False (even with matching token); fires only when flag=True; **guard test:** flipping flag without bumping SCORER_MAX_RAW still produces normalized scores ≤ 100 (documents the known-to-operator ceiling constraint) |
| `test_main_cryptopanic_integration.py` | CRYPTOPANIC_ENABLED=True in run_cycle: fetch called, tokens tagged, DB persisted. CRYPTOPANIC_ENABLED=False: no fetch, no log noise, no DB writes |
| `test_models_cryptopanic_fields.py` | `token_factory()` default state has all four new fields = None; pydantic serialization round-trip preserves them |

Existing baseline ~836 tests must remain passing.

## 16. Acceptance Criteria

- [ ] `CRYPTOPANIC_ENABLED=False` (default) → all existing cycles behave identically
      to pre-BL-053; no new log noise, no new DB inserts.
- [ ] `CRYPTOPANIC_ENABLED=True` + valid/empty token → `/v1/posts/?filter=hot`
      is fetched once per cycle, posts persisted, candidate tokens tagged.
- [ ] Duplicate post fetches are idempotent (UNIQUE constraint).
- [ ] Scoring signal silent regardless of token state while
      `CRYPTOPANIC_SCORING_ENABLED=False` (default).
- [ ] No regression in existing test suite.
- [ ] Integration dry-run `python -m scout.main --dry-run --cycles 1` completes
      without exception when flag is on.
- [ ] Do NOT merge. Do NOT deploy.

## 17. Out of Scope / Follow-Ups

1. Activate scoring signal (bump SCORER_MAX_RAW 198→208, update scorer tests).
2. Dashboard section: recent news-tagged alerts.
3. Paper-trading filter: block trades during bearish macro posts.
4. Sentiment calibration via labelled outcome data.
5. `kind=media` fetch (videos) — currently news only.
