# LunarCrush Social-Velocity Alerter — Design

**Date:** 2026-04-18
**Status:** v2 — design-review round 1 incorporated
**Supersedes:** `2026-04-09-early-detection-lunarcrush-design.md` (pre-dated PR #12 and PR #27)
**Input plan:** `docs/superpowers/plans/2026-04-18-lunarcrush-plan.md`
**Sprint / PR:** Virality Roadmap Sprint 2 — PR #28
**Reviewers consulted:** code-architect, feature-dev:code-reviewer (×2), superpowers:code-reviewer, general-purpose (LunarCrush API reality check)

**v2 delta from v1:** schema normalized (CSV→boolean flags), baseline update is symmetric (handles collapses + EWMA formula explicit), shutdown pattern moved from `atexit` to `asyncio.CancelledError` in `finally` block, retention prune moved out of `Database.initialize()` into loop startup, trending-tracker extension enumerates all four touch-points including a prerequisite helper-extraction refactor, credit ledger persisted across restarts, own `aiohttp.ClientSession` for vendor isolation, `price_change_1h` sourced from CoinGecko raw-markets cache (not `price_cache` — confirmed `price_cache` lacks 1h column), `interactions_accel` redefined as 5-min ring-buffer delta to match poll cadence, `social_signals` gains `UNIQUE(coin_id, detected_at)` + `INSERT OR IGNORE` for TOCTOU safety.

---

## 1. Goal

Surface tokens with **social** velocity — influencer endorsement, cultural-moment attention, narrative rotation — minutes ahead of price velocity, which our CoinGecko-only stack cannot see. Concrete target: had LunarCrush been live, the ASTEROID pump (Musk reply → Polaris Dawn post) would have registered as a social spike before the +114775% price move was visible to CoinGecko's `/coins/markets`.

**Non-goal:** predicting CoinGecko trending (already covered by PR #12). Scoring integration. Paper-trade dispatch.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ Main pipeline (unchanged) — 60s scan cycle                       │
│   CoinGecko → aggregator → scorer → gate → alerter               │
└──────────────────────────────────────────────────────────────────┘
           │  shares DB + Settings; does NOT share aiohttp.ClientSession
           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Social loop (NEW) — 5-minute cadence, own asyncio.Task           │
│   LunarCrush client → spike detector → ResearchAlert dispatch    │
│     │ (own aiohttp.ClientSession — vendor-isolated)               │
│     │                     │                  │                    │
│     │                     └── baseline cache (in-process dict)    │
│     │                              ↕ DB checkpoint every 12 polls │
│     ▼                                                             │
│   credit-budget watcher (DB-persisted ledger, survives restart)   │
└──────────────────────────────────────────────────────────────────┘
           │
           ▼
   Telegram `*Social Velocity*` message (plain-text, research-only)
```

**Key property:** complete error isolation. LunarCrush outage, rate limit, credit exhaustion, or DB write failure **cannot** propagate into the main pipeline. The social loop runs in its own task with its own exception boundary and its own `aiohttp.ClientSession` (Reviewer #2 finding: shared session would cause `RuntimeError: Session is closed` if main pipeline shut down first).

**Task wiring in `main.py`:**
- `social_task = asyncio.create_task(run_social_loop(settings, db, shutdown_event))`
- Register a done-callback that logs the exception and re-creates the task with a 30s back-off. **Never** `await asyncio.gather(main_task, social_task)` — a social crash must not take down the pipeline.

---

## 3. Module layout

Two-level namespace anticipating future social sources (Santiment, etc.):

```
scout/social/
  __init__.py
  models.py              # ResearchAlert, BaselineState, SpikeKind enum
  baselines.py           # In-process baseline cache + DB checkpoint
  lunarcrush/
    __init__.py
    client.py            # async API client (auth, rate-limit, backoff, credits)
    detector.py          # 3 pure spike checks + orchestrator
    alerter.py           # Telegram format + dispatch (research-only)
    loop.py              # asyncio.Task entry point
```

**Why nested:** the `code-architect` review called out that Santiment (Sprint 3) and any future vendor will collide with a flat `scout/social/`. Nesting isolates vendor-specific quirks (auth shape, field names, rate limits) below the shared domain layer.

**Shared-layer responsibilities (`scout/social/` top level):**
- `models.ResearchAlert` — dataclass carrying everything needed for Telegram + DB. Deliberately distinct from `CandidateToken`. The trading engine accepts only `CandidateToken`; passing `ResearchAlert` to it would be a type error, not just a convention break. This is the structural guardrail the risk reviewer demanded.
- `models.SpikeKind` — `Enum[social_volume_24h, galaxy_jump, interactions_accel]`. Prevents string-typo bugs in DB writes and tests.
- `baselines.BaselineCache` — the in-process rolling-average store, vendor-agnostic.

---

## 4. Data sources (LunarCrush v4)

Validated against 2026 docs (general-purpose reviewer's WebFetch audit):

| Endpoint | Use | Credit cost |
|---|---|---|
| `GET /api4/public/coins/list/v2` | Primary poll, every 5 min | ~1 credit/call |
| (Time-series backfill) | **Explicitly NOT used.** Infeasible on $24 Individual tier. | N/A |

**Auth:** `Authorization: Bearer {LUNARCRUSH_API_KEY}` (confirmed current).

**Rate limit:** hard **10 req/min** on Individual tier (not soft). Client enforces **9 req/min** to leave headroom. Exponential backoff on 429: 5s → 10s → 20s → capped 60s.

**Credits:** 2,000 free/day, then $0.0005 per credit overage. At 5-min poll interval × 1 call = **288 calls/day = ~14% of quota**. Detector tracks per-day credit usage; at >80% (1,600 credits) logs `credit_budget_near` once and downshifts `LUNARCRUSH_POLL_INTERVAL` to 600s until midnight UTC. Hard stop at 95% (`credit_budget_exhausted`, skip polls).

**Field names (confirmed current, drift from 2026-04-09 plan):**
| Old plan name | **Current v4 field** |
|---|---|
| `social_volume` | **`social_volume_24h`** |
| `social_mentions` | **`interactions_24h`** (weighted, not raw) |
| `galaxy_score` | `galaxy_score` (unchanged) |
| `sentiment` | `sentiment` (unchanged) |
| `percent_change_24h` | `percent_change_24h` (unchanged) |
| `market_cap`, `price`, `symbol`, `name`, `id` | unchanged |
| (new — useful context) | `social_dominance` |

We store `social_dominance` in `social_signals` for future ensemble use (PR #34) but do not gate on it in MVP — adding it as a 4th spike type creates threshold-tuning debt before we have baseline data.

---

## 5. Spike detection

Three pure functions in `detector.py`, each takes `(coin_dict, BaselineState) → Optional[SpikeResult]`. The orchestrating `detect_spikes()` calls all three, collapses multi-hit per coin into a single `ResearchAlert`, applies dedup, and returns the top-N.

### 5.1 Three spike kinds

| Kind | Condition | Setting | Baseline field |
|---|---|---|---|
| `social_volume_24h` | `current / avg_social_volume_24h ≥ ratio` | `LUNARCRUSH_SOCIAL_SPIKE_RATIO=2.0` | `avg_social_volume_24h` — EWMA (α=1/288) |
| `galaxy_jump` | `current − last_galaxy_score ≥ jump` | `LUNARCRUSH_GALAXY_JUMP=10.0` | `last_galaxy_score` — previous poll |
| `interactions_accel` | `current / interactions_6poll_ago ≥ ratio` | `LUNARCRUSH_INTERACTIONS_ACCEL=3.0` | 6-slot ring buffer of `interactions_24h` (6 × 5min = 30min nominal) |

**EWMA formula (closes Reviewer #2 BLOCKING #4):**
```
new_avg = α · new_value + (1 − α) · old_avg        where α = 1/LUNARCRUSH_BASELINE_MIN_SAMPLES
```
Chosen over a literal 288-sample ring buffer: O(1) space per coin, no boundary artifacts, matches the warmup count intuitively (one `MIN_SAMPLES` window to converge).

**`interactions_accel` redefined (closes Reviewer #1 NON-BLOCKING §5.1):** the original "30-min snapshot delta" was ambiguous vs a 5-min poll cadence. v2 uses a fixed 6-slot ring buffer (oldest = 30 min nominal ago). If fewer than 6 slots are populated (warmup / skipped cycles due to 429), skip the check for that poll — do NOT compare against a missing slot.

### 5.2 Multi-hit collapse (closes Risk-Reviewer finding #2)

**A coin firing multiple spike kinds in the same cycle produces ONE `ResearchAlert`, not one per kind.** The alert's `spike_kinds: list[SpikeKind]` field carries all triggered kinds. The Telegram message lists them. DB dedup is per `coin_id` only.

**Storage format (revised — closes Reviewer #2 BLOCKING #5):** `social_signals` carries three boolean columns `fired_social_volume_24h`, `fired_galaxy_jump`, `fired_interactions_accel` — NOT a CSV `spike_kinds` column. This matches the existing `trending_comparisons.detected_by_*` pattern (consistency), supports `GROUP BY` aggregations for the future ensemble classifier (PR #34), and avoids substring-match false positives like `LIKE '%galaxy_jump%'` when a future kind is a substring of an existing one.

### 5.3 Cold-start suppression

An alert fires only when `BaselineState.sample_count >= LUNARCRUSH_BASELINE_MIN_SAMPLES` (default 288 = 24h at 5-min polls). Baselines **persist across restarts via DB checkpoint** (§6), so a service restart does NOT reset the warmup counter — closes Risk-Reviewer finding #1.

**Every coin returned by `/coins/list/v2` increments its `sample_count` on every poll**, regardless of whether it fires or passes dedup — closes Reviewer #1 NON-BLOCKING §5.3. Warmup progresses uniformly.

**Interval-aware warmup (closes Reviewer #2 MINOR §5.3):** if the credit-budget watcher has downshifted `LUNARCRUSH_POLL_INTERVAL` to 600s, `MIN_SAMPLES=288` now covers 48h rather than 24h. To keep the warmup intuitive, the check uses:
```python
required = settings.LUNARCRUSH_BASELINE_MIN_HOURS * 3600 // current_poll_interval
if state.sample_count < required: skip
```
with `LUNARCRUSH_BASELINE_MIN_HOURS=24`. `LUNARCRUSH_BASELINE_MIN_SAMPLES` is retained as a derived constant used only in EWMA α for stability reasons.

**First deployment** on a clean DB pays a 24h warmup once. Acceptable per the API-reality review (backfill impossible on Individual tier).

### 5.4 Baseline poisoning mitigation (closes Risk-Reviewer finding #4 + Reviewer #2 BLOCKING #4)

Baseline update in `baselines.update()` must handle spikes AND collapses symmetrically:

```python
def update(state: BaselineState, new_value: Optional[float]) -> BaselineState:
    # 1. None / 0 / missing → skip update entirely; return state unchanged.
    if new_value is None or new_value <= 0:
        return state  # do not drag the avg to zero on API hiccups

    # 2. After warmup, skip updates on extreme samples in EITHER direction.
    if state.sample_count >= settings.LUNARCRUSH_BASELINE_MIN_SAMPLES:
        ratio = new_value / max(state.avg_social_volume_24h, 1e-9)
        spike_hi = settings.LUNARCRUSH_SOCIAL_SPIKE_RATIO   # 2.0
        spike_lo = 1.0 / spike_hi                            # 0.5
        if ratio >= spike_hi or ratio <= spike_lo:
            return state._replace(sample_count=state.sample_count + 1)
            # sample_count still progresses (progress invariant), avg does not
            # absorb the outlier.

    # 3. Normal case — EWMA update.
    α = 1.0 / settings.LUNARCRUSH_BASELINE_MIN_SAMPLES
    new_avg = α * new_value + (1 - α) * state.avg_social_volume_24h
    return state._replace(
        avg_social_volume_24h=new_avg,
        sample_count=state.sample_count + 1,
        last_updated=utcnow(),
    )
```

This prevents BOTH the sustained-pump lockout (risk reviewer) AND the coin-drops-off-API zero-poisoning (Reviewer #2).

**All thresholds settings-driven (closes Reviewer #1 NON-BLOCKING):** no hardcoded `2.0` or `288` literals in code. `LUNARCRUSH_SOCIAL_SPIKE_RATIO` and `LUNARCRUSH_BASELINE_MIN_SAMPLES` are the only sources of truth.

### 5.5 Dedup

Before inserting a `ResearchAlert`, query:
```sql
SELECT 1 FROM social_signals
WHERE coin_id = ?
  AND datetime(detected_at) >= datetime(?)
LIMIT 1
```
with cutoff `now() - LUNARCRUSH_DEDUP_HOURS` (default 4). Note the `datetime(col)` wrap — mandatory per PR #24 lessons to avoid the `T`-vs-space string-comparison bug. Consistent with PR #27's `velocity_alerts` dedup.

### 5.6 Top-N + credits-exhausted short-circuit

After dedup, sort remaining detections by the highest triggered spike_ratio and take `LUNARCRUSH_TOP_N=10`. Emergency short-circuit: if the credit-budget watcher flipped `credit_budget_exhausted`, skip detection entirely until next midnight-UTC rollover.

---

## 6. Baseline persistence (closes Risk-Reviewer finding #1 + Reviewer #2 BLOCKING #2)

In-process `BaselineCache: dict[str, BaselineState]` + periodic DB checkpoint.

### Flow:
1. **On loop startup**:
   - Wait on a DB-ready barrier (`await db.initialized.wait()` — gated after `_create_tables` finishes) to avoid the hydration race Reviewer #2 flagged.
   - `SELECT * FROM social_baselines` → hydrate cache. Never reset existing `sample_count` to 0.
   - Run retention prune: `DELETE FROM social_signals WHERE datetime(detected_at) < datetime('now', '-' || ? || ' days')` using `settings.LUNARCRUSH_RETENTION_DAYS`. This lives in `loop.py` startup, **not** `Database.initialize()` — keeps the generic DB class vendor-agnostic (closes Reviewer #1 BLOCKING #3).
2. **Per poll cycle**: read baselines from cache (O(1) dict lookup, no DB I/O in hot path). Update in-memory after spike checks.
3. **Every `LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS` polls (default 12 = 60 min) OR on graceful shutdown**: flush dirty baseline rows to DB in a single transaction.

The checkpoint interval is read from `settings.LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS` at each cycle, not hardcoded.

This resolves Architect-Reviewer finding #1 (avoid per-cycle per-coin SQLite writes at scale) AND Risk-Reviewer finding #1 (baselines survive restart).

### Crash safety:
Between checkpoints, up to 60 min of baseline updates can be lost. Impact: slight regression to older average; detector self-heals over the next 12 cycles. Acceptable — the alerter is research-only, not mission-critical.

### Graceful shutdown (closes Reviewer #1 BLOCKING #2):
**No `atexit` handler** — `atexit` callbacks run synchronously after the event loop closes, so any `await db._conn.execute(...)` inside them fails. Instead, the `social_loop(...)` task body is structured as:

```python
async def run_social_loop(settings, db, shutdown_event: asyncio.Event) -> None:
    try:
        while not shutdown_event.is_set():
            try:
                await _poll_cycle(settings, db, cache)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("social_loop_cycle_error")
            # wait-or-cancel
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=current_interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass  # fall through to finally
    finally:
        # Flush baselines + credit ledger before exit. DB is still open here
        # because main.py awaits this task before closing the DB.
        await _flush_baselines(db, cache)
        await _flush_credit_ledger(db, ledger)
```

`main.py` sets `shutdown_event` on `SIGINT`/`SIGTERM` (matches the existing pipeline shutdown pattern at `main.py:643-654`), then awaits the social task with a 10s timeout before cancelling.

---

## 7. Data model

```sql
-- Spike events that produced a Telegram alert
CREATE TABLE IF NOT EXISTS social_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,                  -- LunarCrush id (string form)
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    -- Kind flags (replaces v1 CSV — see §5.2)
    fired_social_volume_24h INTEGER NOT NULL DEFAULT 0,
    fired_galaxy_jump       INTEGER NOT NULL DEFAULT 0,
    fired_interactions_accel INTEGER NOT NULL DEFAULT 0,
    galaxy_score REAL,
    social_volume_24h REAL,
    social_volume_baseline REAL,
    social_spike_ratio REAL,                -- max across triggered kinds
    interactions_24h REAL,
    sentiment REAL,
    social_dominance REAL,
    price_change_1h REAL,                   -- sourced from raw CoinGecko markets cache
    price_change_24h REAL,
    market_cap REAL,
    current_price REAL,
    detected_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(coin_id, detected_at)            -- TOCTOU belt-and-braces
);
CREATE INDEX IF NOT EXISTS idx_social_signals_coin_detected
    ON social_signals(coin_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_social_signals_symbol
    ON social_signals(symbol);
-- Note: 30-day retention prune's WHERE detected_at < ... scan is covered by
--       the composite (coin_id, detected_at) index via its second key-part;
--       no dedicated idx_social_signals_detected_at needed.

-- Persistent baseline state (checkpointed, survives restart)
CREATE TABLE IF NOT EXISTS social_baselines (
    coin_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    avg_social_volume_24h REAL NOT NULL,
    avg_galaxy_score REAL NOT NULL,
    last_galaxy_score REAL,                 -- for galaxy_jump detection
    interactions_ring TEXT NOT NULL DEFAULT '[]',  -- JSON list, 6-slot ring buffer of interactions_24h
    sample_count INTEGER NOT NULL,
    last_poll_at TEXT,                      -- absolute timestamp of last update
    last_updated TEXT NOT NULL
);

-- Credit budget ledger (survives restart — closes Reviewer #2 MAJOR §4/§10)
CREATE TABLE IF NOT EXISTS social_credit_ledger (
    utc_date TEXT PRIMARY KEY,              -- 'YYYY-MM-DD' UTC
    credits_used INTEGER NOT NULL,
    last_updated TEXT NOT NULL
);
```

**INSERT pattern for `social_signals`:** `INSERT OR IGNORE INTO social_signals (...) VALUES (...)` — the `UNIQUE(coin_id, detected_at)` constraint gives atomic dedup even against a detector bug that queues two detections for the same coin in one cycle (closes Reviewer #2 MAJOR §7).

**`interactions_ring` as JSON:** a 6-element JSON array (`'[120.5, 134.1, ...]'`) persisted in the baseline row. On cache hydration it's parsed with `json.loads`. Write-load is minimal (1 value added every 5 min per coin, ring overflow via `[-6:]` slice). Queryability is not a concern — this column is never read by ad-hoc SQL.

**Orthogonality from `candidates`:** no FK. Ensemble classifier (PR #34) will join across `social_signals`, `velocity_alerts`, `candidates` by `coin_id` / `LOWER(symbol)` — consistent with how `trending_comparisons` already joins today.

**Retention:** pruned on loop startup via `loop.py` using `settings.LUNARCRUSH_RETENTION_DAYS` (default 30) — NOT inside `Database.initialize()`. Rationale in §6.

---

## 8. Alert flow (closes Risk-Reviewer #3 + Reviewer #2 BLOCKING #6)

```
every current_poll_interval seconds:
  1. check credit budget (DB-persisted ledger); if exhausted → log + skip
  2. fetch /coins/list/v2 (own aiohttp.ClientSession)
  3. for each coin, capture (pre-update BaselineState, triggered kinds):
       pre_state = cache[coin_id] or bootstrap
       kinds = run 3 spike checks against pre_state + current values
       post_state = baselines.update(pre_state, current_values)  # §5.4
       (do NOT commit post_state to cache yet — buffer it)
  4. build ResearchAlert list from coins with ≥1 triggered kind
  5. filter out coins with row in social_signals within DEDUP_HOURS
  6. filter out coins where pre_state.sample_count < required (§5.3)
  7. sort by highest spike_ratio; take top-N
  8. enrich price_change_1h from CoinGecko raw-markets cache (§8.1).
     Default to None for coins not present — never block the alert.
  9. **BEGIN TRANSACTION**
       INSERT OR IGNORE into social_signals for each alert
       (TOCTOU-safe via UNIQUE(coin_id, detected_at))
     **COMMIT**
 10. If tx succeeded: commit buffered post_state updates into cache;
     **then** dispatch single batched Telegram message.
     If tx failed: drop buffered baseline updates (closes Reviewer #2
     BLOCKING #6 — in-memory baseline stays in sync with the row that
     actually exists in the DB). Next cycle retries naturally.
 11. Always commit post_state for coins that did NOT fire (baseline
     progresses independently of alert persistence — progress invariant).
 12. Every CHECKPOINT_EVERY_N_POLLS polls: flush dirty baselines +
     credit ledger snapshot to DB in one transaction.
```

**Rationale for the buffered-commit pattern:** if we update the cache before the DB INSERT and the INSERT fails, the in-memory baseline has already absorbed the sample but no alert was persisted. Next cycle's dedup check passes (no DB row), but the spike-exclusion rule now says "this looks like a spike, skip update" — meaning the baseline is frozen in a bad state. The buffered commit (step 10) keeps the baseline consistent with the DB.

**Firing coins vs non-firing coins:** non-firing coins' baseline updates are committed unconditionally in step 11 — they can't poison the DB→cache consistency because they're not being inserted anywhere.

### 8.1 `price_change_1h` enrichment source

`scout/ingestion/coingecko.py` already exposes a module-level `last_raw_markets: list[dict]` updated each scan cycle (60s cadence, contains `price_change_percentage_1h_in_currency` and `24h`). The social loop calls a small helper `get_price_change_1h(symbol, coin_id) -> tuple[Optional[float], Optional[float]]` that matches either by `LOWER(symbol)` or exact `coin_id` into `last_raw_markets`. No DB read, no extra HTTP call.

**Why NOT `price_cache` (closes Reviewer #1 NON-BLOCKING):** `scout/db.py:401-409` confirmed `price_cache` schema has only `price_change_24h` and `price_change_7d` — no 1h column. Piping through `price_cache` would require a schema migration for a single optional field. The CoinGecko raw-markets cache (updated every 60s, in-process) is zero-cost.

**Enrichment hit rate:** ~30-50% realistically (CoinGecko markets top-N overlaps partially with LunarCrush universe). `None` default is explicitly allowed — the alert message renders `price: —` when both price values are None, rather than omitting or blocking.

---

## 9. Telegram message format

```
*Social Velocity* (LunarCrush)

*AST* — Asteroid Shiba
kinds: social_volume_24h, galaxy_jump
galaxy: 72 (+14) | social vol: 4.2x | interactions: 31k
price: $0.00006 (1h: +42.1%, 24h: +114,775.8%)
mcap: $24.3M | sentiment: 0.82
[LunarCrush](https://lunarcrush.com/coins/asteroid-shiba) · [chart](https://www.coingecko.com/en/coins/asteroid-shiba)
```

- Markdown parse mode (consistent with PR #27 velocity alerter).
- Batched: single message for all top-N detections in the cycle. Truncates at Telegram 4096 limit via `_truncate` helper.
- Distinct header `*Social Velocity* (LunarCrush)` separates visually from `*Velocity Alerts* (1h pump)` so the user knows which tier fired.

**LunarCrush URL construction (closes Reviewer #1 NON-BLOCKING §9):** the `/coins/list/v2` response is confirmed to include a `symbol` field but NOT a guaranteed `slug` matching CoinGecko. The Telegram LunarCrush link is built as `https://lunarcrush.com/coins/{lc_coin_id}` using LunarCrush's own id (string form of the integer id if numeric) — this is the canonical URL form and avoids a broken link from slug-mismatch. CoinGecko chart link uses the CoinGecko slug if we successfully matched one in §8.1, else omitted.

**Markdown escape (closes Reviewer #2 MINOR §9):** reuse the existing `_escape_md()` helper from `scout/alerter.py` (or `scout/velocity/detector.py` — whichever carries it) to escape `_`, `*`, `[`, `]`, `` ` `` in `name` and `symbol` before interpolation. A token named `AS_ROID` must render correctly without breaking parse mode.

---

## 10. Settings (`scout/config.py`)

```python
# -------- LunarCrush Social-Velocity Alerter --------
LUNARCRUSH_ENABLED: bool = False                    # master flag
LUNARCRUSH_API_KEY: str = ""                        # empty → loop never starts
LUNARCRUSH_BASE_URL: str = "https://lunarcrush.com/api4/public"
LUNARCRUSH_POLL_INTERVAL: int = 300                 # 5 min (default / normal)
LUNARCRUSH_POLL_INTERVAL_SOFT: int = 600            # 10 min (used after 80% credits)
LUNARCRUSH_RATE_LIMIT_PER_MIN: int = 9              # under hard 10/min
LUNARCRUSH_DAILY_CREDIT_BUDGET: int = 2000          # free tier cap
LUNARCRUSH_CREDIT_SOFT_PCT: float = 0.80            # downshift at 80%
LUNARCRUSH_CREDIT_HARD_PCT: float = 0.95            # stop at 95%
LUNARCRUSH_SOCIAL_SPIKE_RATIO: float = 2.0
LUNARCRUSH_GALAXY_JUMP: float = 10.0
LUNARCRUSH_INTERACTIONS_ACCEL: float = 3.0
LUNARCRUSH_DEDUP_HOURS: int = 4
LUNARCRUSH_TOP_N: int = 10
LUNARCRUSH_BASELINE_MIN_HOURS: int = 24             # warmup wall-clock, interval-aware
LUNARCRUSH_BASELINE_MIN_SAMPLES: int = 288          # EWMA alpha denominator
LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS: int = 12       # 60 min
LUNARCRUSH_RETENTION_DAYS: int = 30
```

Double kill-switch: `LUNARCRUSH_ENABLED=false` **or** empty `LUNARCRUSH_API_KEY` disables the loop.

**Hot knob for alert rate (closes Reviewer #2 MAJOR):** rather than requiring a redeploy to quiet a noisy first day, `LUNARCRUSH_TOP_N` and `LUNARCRUSH_SOCIAL_SPIKE_RATIO` are read **from settings at each cycle start** (not cached at boot). Operator can `systemctl restart gecko-pipeline` after editing `.env` — 5s downtime, no code change. The explicit runbook is added to §14. (Deferred: a `kv_settings` DB-backed override table was considered; punted as over-engineered until we have evidence the 5s restart window is painful.)

---

## 11. Error isolation

| Failure | Handling |
|---|---|
| 401/403 (bad key) | Log **once**, set in-process `disabled=True`, loop exits cleanly. Next deploy required to re-enable. |
| 429 (rate limit) | Exponential backoff 5 → 10 → 20 → 60 s cap. Retry. |
| 5xx / timeout / network | Log warning, skip cycle, retry next interval. |
| Credit budget >95% | `credit_budget_exhausted` log, skip cycle until midnight UTC. |
| DB write failure (social_signals) | Log, skip Telegram dispatch this cycle, retry next cycle. |
| DB write failure (social_baselines checkpoint) | Log, keep cache in-process, retry next checkpoint. |
| Telegram send failure | Log, **do not** roll back the social_signals INSERT. The alert is considered "sent" from dedup's perspective (we'd rather skip than double-page later). |

**Never** raises into `main.py`. The entire social loop body is wrapped in `try/except Exception: logger.exception("social_loop_error")` with the task restarting on a 30s back-off if the handler itself crashes.

---

## 12. Trending-tracker hit-rate integration (closes Reviewer #1 BLOCKING #1 + Reviewer #2 MAJOR)

Extend `scout/trending/tracker.compare_with_signals()` to count `social_signals` as a 4th detector tier alongside narrative, pipeline, and chains. This requires coordinated edits to **four** files:

| # | File | Change |
|---|---|---|
| 1 | `scout/db.py` `_create_tables()` | Add columns `detected_by_social INTEGER NOT NULL DEFAULT 0`, `social_detected_at TEXT`, `social_lead_minutes REAL` to the `CREATE TABLE IF NOT EXISTS trending_comparisons` statement. Because the table may already exist from PR #12 deploys, add a migration block that `ALTER TABLE trending_comparisons ADD COLUMN ... IF NOT EXISTS` via a wrapped `try/except` — follow the pattern already used elsewhere in `db.py` for additive migrations. |
| 2 | `scout/trending/models.py` | Add `detected_by_social: bool = False`, `social_detected_at: datetime \| None = None`, `social_lead_minutes: float \| None = None` to `TrendingComparison`. Add `by_social: int = 0` to `TrendingStats`. |
| 3 | `scout/trending/tracker.py` `compare_with_signals()` | Current function writes 17 fixed columns to `trending_comparisons` (lines 275-301, confirmed). Three required changes: **(a)** add a pre-requisite helper `_check_detector(db, table, id_col, coin_id, symbol, first_trending_at_str) -> tuple[bool, Optional[datetime], Optional[float]]` — extract the repeated SELECT pattern used for narrative/pipeline/chains so adding a 4th tier is a single call, not a copy-paste. **(b)** call `_check_detector(db, 'social_signals', 'coin_id', ...)` (or a symbol-join variant if LunarCrush `coin_id` does not match existing tables). **(c)** extend the INSERT column list from 17 to 20 and the VALUES placeholder list to match. |
| 4 | `scout/trending/tracker.py` `get_trending_stats()` | Add a 4th UNION arm to the lead-time query (`SELECT social_lead_minutes FROM trending_comparisons WHERE detected_by_social=1`) and a `SELECT COUNT(*) WHERE detected_by_social=1` for `by_social`. |

**Prerequisite refactor commit:** step 3(a) lands first as a pure refactor (`refactor(trending): extract _check_detector helper`) with no behavior change. Green tests confirm the extraction is clean. Then the additive changes for the social tier land in a second commit. This keeps the diff reviewable and the blast radius contained to the trending module (closes Reviewer #2 MAJOR §12).

This closes Architect-Reviewer finding #4 — the trending tracker gets full tier coverage rather than silently ignoring social signals in its hit-rate numbers.

---

## 13. Testing strategy

TDD order. `aioresponses` for HTTP mocks, `tmp_path` for DB fixtures (same as existing tests). Time-travel: inject a `clock: Callable[[], datetime]` parameter into `BaselineCache`, `CreditLedger`, and the loop body — tests pass a fake callable and advance wall-clock without pulling in `freezegun` / `time_machine` as a new dependency (closes Reviewer #1 NON-BLOCKING §13).

| Test file | Coverage |
|---|---|
| `tests/test_social_lunarcrush_client.py` | auth header correct; 429 backoff sequence (5/10/20/60s cap); **401 sets disabled flag + exits loop cleanly**; malformed-JSON resilience; missing-field handling; field name drift (`social_volume_24h` / `interactions_24h` / `social_dominance`); own ClientSession isolation (main pipeline closing its session does not break this client) |
| `tests/test_social_detector.py` | each of 3 spike kinds fires independently; **multi-kind coin produces ONE alert with multiple fired_* flags set**; **dedup boundary exactly at 4h (≥ not >)**; **cold-start suppression at 287 vs fires at 288**; top-N limit; coin list with 500 entries / 50% qualifying produces ≤ TOP_N alerts; **interactions_accel with <6 ring slots skips check silently** |
| `tests/test_social_baselines.py` | EWMA rolling avg correctness; **upward spike (2x) skips avg update but increments sample_count**; **downward collapse (0.5x) skips avg update but increments sample_count**; **null / zero value skips update entirely (no sample_count increment)**; **survives restart** (checkpoint, close DB, reopen, confirm sample_count + avg preserved); **graceful shutdown flushes cache via finally block (not atexit)**; interactions ring buffer rotates correctly on 7+ writes; `asyncio.CancelledError` during a poll triggers finally flush, not data loss |
| `tests/test_social_alert_format.py` | Telegram message structure; **Markdown escaping of `AS_ROID`-style names via `_escape_md`**; URL inclusion (LunarCrush + CoinGecko when slug known); **>4096 char truncation**; **multi-kind message lists all 3 kinds correctly**; missing `price_change_1h` renders `—` not None/error |
| `tests/test_social_credit_budget.py` | 80% soft downshift to `LUNARCRUSH_POLL_INTERVAL_SOFT`; 95% hard stop; **midnight-UTC rollover resets counter (via injected fake clock)**; **ledger persists across simulated restart**; soft→hard transition mid-cycle stops fetch immediately |
| `tests/test_social_db.py` | tables created with all columns including `fired_*` booleans; **UNIQUE(coin_id, detected_at) constraint enforced** (second INSERT for same pair is no-op); 30-day prune deletes correctly; dedup query uses `datetime()` wrap; composite index covers prune scan |
| `tests/test_social_loop_flow.py` | **DB INSERT succeeds, Telegram succeeds → baseline cache updated**; **DB INSERT fails, Telegram NOT called, baseline cache NOT updated (buffered-commit pattern)**; **DB INSERT succeeds, Telegram fails → baseline cache IS updated (alert considered sent from dedup's perspective)**; non-firing coins' baseline updates commit regardless of tx outcome; done-callback re-creates task with 30s back-off on uncaught exception |
| `tests/test_social_price_enrichment.py` | matches `last_raw_markets` by `LOWER(symbol)`; matches by `coin_id` slug; returns `(None, None)` when no match; never raises |
| `tests/test_trending_tracker_social.py` (extend) | `_check_detector` helper refactor preserves existing behavior for narrative/pipeline/chains (regression guard); social tier computed when social fires before trending appearance; `TrendingStats.by_social` counted correctly; additive migration (`ALTER TABLE ADD COLUMN`) runs idempotently on an existing DB |

Target: **+45 new tests** (slight upward revision after BLOCKING fixes), full suite passing with no regressions to existing 616.

---

## 14. Rollout plan

1. Branch `feat/lunarcrush-integration` off current master (HEAD after spec commit).
2. **Commit 1** — prerequisite refactor: extract `_check_detector()` helper in `scout/trending/tracker.py`. Green tests for existing tiers, zero behavior change.
3. **Commit 2+** — TDD loop per test file in §13. Commit per green milestone.
4. Full pytest green → open PR #28.
5. PR-review agents (3) dispatched on final diff.
6. Deploy: on VPS, pull + append to `.env`:
   ```
   LUNARCRUSH_ENABLED=true
   LUNARCRUSH_API_KEY=<purchased>
   ```
   Restart `gecko-pipeline.service`.
7. Watch for 30h:
   - Day 1: baseline_warmup events expected; no alerts yet.
   - Day 2 hour 0 onward: alerts begin firing.
   - Metrics: no 401/403, no credit_budget_exhausted, alert rate within 0–10/day.

### Operational runbook — tuning noisy first day

If alert rate exceeds expectations on day 2:
```
# Edit VPS .env
LUNARCRUSH_TOP_N=5                         # halve output volume
LUNARCRUSH_SOCIAL_SPIKE_RATIO=3.0          # tighten threshold
sudo systemctl restart gecko-pipeline.service
```
Values are re-read at each cycle start (§10), so restart is the fastest knob. Kill-switch:
```
LUNARCRUSH_ENABLED=false
sudo systemctl restart gecko-pipeline.service
```

---

## 15. Explicit non-goals and future work

- **No dashboard UI** — deferred to PR #34 ensemble meta-tier.
- **No paper-trade dispatch** — enforced structurally by `ResearchAlert` dataclass.
- **No Santiment / Nansen** — Sprint 3+.
- **No group-spike (narrative rotation) detection** — belongs in PR #34 ensemble.
- **No startup time-series backfill** — infeasible on $24 Individual tier credit budget.
- **No time-of-day baseline seasonality** — revisit after 30d of real alert data.

---

## 16. Success criteria

- ≥1 valid social-velocity alert per 24h after warmup (proof of life).
- Zero `main_pipeline_error` events caused by social loop.
- `sample_count >= 288` for ≥80% of tracked coins by T+30h from first deploy.
- Daily credit usage <30% of 2000-credit quota in steady state.
- No measurable change to existing Telegram alert cadence from other tiers.

---

## 17. Summary of reviewer findings incorporated

### Round 1 (plan reviewers)

| Reviewer | Finding | Incorporated in |
|---|---|---|
| Architect | Per-cycle SQLite writes at scale | §6 in-process cache + 60-min checkpoint |
| Architect | 3 spike types → god function | §5 three pure functions |
| Architect | Naming collision for future vendors | §3 nested `scout/social/lunarcrush/` |
| Architect | Price context in-process (not HTTP) | §8.1 `last_raw_markets` |
| Architect | Trending tracker integration gap | §12 four-file plan |
| Risk | Baseline lockout on restart | §6 DB persistence |
| Risk | Multi-kind spam | §5.2 collapse to one alert |
| Risk | DB-write-before-Telegram | §8 flow step order |
| Risk | Baseline poisoning | §5.4 symmetric spike-exclusion rule |
| Risk | Research-only via convention | §3 `ResearchAlert` type |
| Risk | No retention | §7 30-day prune |
| Risk | Multiple test gaps | §13 full test matrix |
| API reality | Backfill infeasible on $24 tier | §15 explicit non-goal |
| API reality | Field name drift | §4 table + §7 schema |
| API reality | Credits budget, not just req/min | §4 + §10 settings |
| API reality | Hard 10/min rate limit | §10 `LUNARCRUSH_RATE_LIMIT_PER_MIN=9` |

### Round 2 (design reviewers — v2 delta)

| Reviewer | Severity | Finding | Incorporated in |
|---|---|---|---|
| Reviewer #1 | BLOCKING | `compare_with_signals()` extension needs 4 touch-points | §12 table enumerating all four files |
| Reviewer #1 | BLOCKING | `atexit` incompatible with `asyncio.Task` | §6 shutdown via `asyncio.CancelledError` in `finally` |
| Reviewer #1 | BLOCKING | Retention prune in `Database.initialize()` is wrong | §6/§7 moved to `loop.py` startup |
| Reviewer #1 | NON-BLOCKING | Cold-start baseline `sample_count` increment ambiguity | §5.3 explicit progress invariant |
| Reviewer #1 | NON-BLOCKING | `interactions_accel` window undefined vs 5-min poll | §5.1 6-slot ring buffer with explicit skip-when-short |
| Reviewer #1 | NON-BLOCKING | Hardcoded `2.0` / `288` literals | §5.4 all settings-driven |
| Reviewer #1 | NON-BLOCKING | LunarCrush URL slug assumption | §9 use `coin_id` form |
| Reviewer #1 | NON-BLOCKING | `freezegun` dependency for midnight test | §13 injected `clock` parameter |
| Reviewer #1 | NON-BLOCKING | `price_cache` has no `price_change_1h` column | §8.1 source from `last_raw_markets` |
| Reviewer #1 | NON-BLOCKING | `LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS` setting coherence | §6 explicit read from settings |
| Reviewer #2 | BLOCKING | Asymmetric spike-exclusion (upward only) | §5.4 handles nulls + collapses |
| Reviewer #2 | BLOCKING | CSV `spike_kinds` breaks queryability | §7 three boolean `fired_*` columns |
| Reviewer #2 | BLOCKING | Baseline + insert transactionality | §8 buffered-commit pattern |
| Reviewer #2 | MAJOR | No `UNIQUE(coin_id, detected_at)` | §7 UNIQUE constraint + `INSERT OR IGNORE` |
| Reviewer #2 | MAJOR | Credit ledger in-memory only | §7 `social_credit_ledger` table |
| Reviewer #2 | MAJOR | No hot knob for alert rate | §10 settings re-read per cycle + §14 runbook |
| Reviewer #2 | MAJOR | Missing specific tests | §13 added loop-flow and price-enrichment test files |
| Reviewer #2 | MAJOR | Tracker extension invasive without prior refactor | §12 `_check_detector` extraction as prereq commit |
| Reviewer #2 | MAJOR | Hydration race on startup | §6 `db.initialized.wait()` barrier |
| Reviewer #2 | MAJOR | Shared `aiohttp.ClientSession` coupling | §2 own session for vendor isolation |
| Reviewer #2 | MINOR | Interval-aware warmup (soft downshift → 48h) | §5.3 hours-based with `LUNARCRUSH_BASELINE_MIN_HOURS` |
| Reviewer #2 | MINOR | Markdown escape for `AS_ROID` names | §9 reuse `_escape_md` |
| Reviewer #2 | MINOR | Done-callback + no `gather()` for social task | §2 `main.py` wiring description |
