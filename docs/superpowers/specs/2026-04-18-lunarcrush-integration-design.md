# LunarCrush Social-Velocity Alerter — Design

**Date:** 2026-04-18
**Status:** Draft (pending design-review agents)
**Supersedes:** `2026-04-09-early-detection-lunarcrush-design.md` (pre-dated PR #12 and PR #27)
**Input plan:** `docs/superpowers/plans/2026-04-18-lunarcrush-plan.md`
**Sprint / PR:** Virality Roadmap Sprint 2 — PR #28
**Reviewers consulted:** code-architect, feature-dev:code-reviewer, general-purpose (LunarCrush API reality check)

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
           │  shares aiohttp.ClientSession, DB, Settings
           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Social loop (NEW) — 5-minute cadence, own asyncio.Task           │
│   LunarCrush client → spike detector → ResearchAlert dispatch    │
│     │                     │                  │                    │
│     │                     └── baseline cache (in-process dict)    │
│     │                              ↕ DB checkpoint every 12 polls │
│     ▼                                                             │
│   credit-budget watcher (abort poll if >80% daily credits)        │
└──────────────────────────────────────────────────────────────────┘
           │
           ▼
   Telegram `*Social Velocity*` message (plain-text, research-only)
```

**Key property:** complete error isolation. LunarCrush outage, rate limit, credit exhaustion, or DB write failure **cannot** propagate into the main pipeline. The social loop runs in its own task with its own exception boundary.

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

| Kind | Condition | Setting | Baseline window |
|---|---|---|---|
| `social_volume_24h` | `current / baseline_7d_avg ≥ ratio` | `LUNARCRUSH_SOCIAL_SPIKE_RATIO=2.0` | 7-day rolling avg of `social_volume_24h` |
| `galaxy_jump` | `current − previous_value ≥ jump` (last 1h) | `LUNARCRUSH_GALAXY_JUMP=10.0` | last known `galaxy_score` from previous poll |
| `interactions_accel` | `current_30min / previous_30min ≥ ratio` | `LUNARCRUSH_INTERACTIONS_ACCEL=3.0` | 30-min snapshot delta of `interactions_24h` |

### 5.2 Multi-hit collapse (closes Risk-Reviewer finding #2)

**A coin firing multiple spike kinds in the same cycle produces ONE `ResearchAlert`, not one per kind.** The alert's `spike_kinds: list[SpikeKind]` field carries all triggered kinds. The Telegram message lists them. DB dedup is per `coin_id` only. This is why `social_signals.spike_kind` in the schema is a comma-separated string, not a FK-style single value.

### 5.3 Cold-start suppression

An alert fires only when `BaselineState.sample_count >= LUNARCRUSH_BASELINE_MIN_SAMPLES` (default 288 = 24h at 5-min polls). Baselines **persist across restarts via DB checkpoint** (§6), so a service restart does NOT reset the warmup counter — closes Risk-Reviewer finding #1.

**First deployment** on a clean DB pays a 24h warmup once. Acceptable per the API-reality review (backfill impossible on Individual tier).

### 5.4 Baseline poisoning mitigation (closes Risk-Reviewer finding #4)

Baseline update in `baselines.update()`:

```python
def update(state: BaselineState, new_value: float) -> BaselineState:
    # Spike-exclusion: if this sample would itself be a 2× spike above
    # current baseline and we already have >= min_samples data, skip the
    # update so extreme events don't inflate the reference window.
    if (state.sample_count >= MIN_SAMPLES_FOR_EXCLUSION
            and new_value >= state.avg_social_volume_24h * 2.0):
        return state  # unchanged — spike detected but baseline not moved
    ...  # else rolling-avg update
```

This prevents the sustained-pump lockout the risk reviewer called out. `MIN_SAMPLES_FOR_EXCLUSION=288` — we only activate exclusion after warmup so legitimate early samples still build the baseline.

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

## 6. Baseline persistence (closes Risk-Reviewer finding #1)

In-process `BaselineCache: dict[str, BaselineState]` + periodic DB checkpoint.

### Flow:
1. **On loop startup**: `SELECT * FROM social_baselines` → hydrate cache. Never reset existing `sample_count` to 0.
2. **Per poll cycle**: read baselines from cache (O(1) dict lookup, no DB I/O in hot path). Update in-memory after spike checks.
3. **Every 12 polls (60 min) OR on graceful shutdown**: flush dirty baseline rows to DB in a single transaction.

This resolves Architect-Reviewer finding #1 (avoid per-cycle per-coin SQLite writes at scale) AND Risk-Reviewer finding #1 (baselines survive restart).

### Crash safety:
Between checkpoints, up to 60 min of baseline updates can be lost. Impact: slight regression to older average; detector self-heals over the next 12 cycles. Acceptable — the alerter is research-only, not mission-critical.

### Graceful shutdown:
The `loop.py` task registers an `atexit` / `signal` handler to flush the cache before process exit.

---

## 7. Data model

```sql
-- Spike events that produced a Telegram alert
CREATE TABLE IF NOT EXISTS social_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,               -- LunarCrush id
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    spike_kinds TEXT NOT NULL,           -- CSV: 'social_volume_24h,galaxy_jump'
    galaxy_score REAL,
    social_volume_24h REAL,
    social_volume_baseline REAL,
    social_spike_ratio REAL,             -- max across triggered kinds
    interactions_24h REAL,
    sentiment REAL,
    social_dominance REAL,
    price_change_1h REAL,                -- sourced from price_cache at alert time
    price_change_24h REAL,
    market_cap REAL,
    current_price REAL,
    detected_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_social_signals_coin_detected
    ON social_signals(coin_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_social_signals_symbol
    ON social_signals(symbol);

-- Persistent baseline state (checkpointed, survives restart)
CREATE TABLE IF NOT EXISTS social_baselines (
    coin_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    avg_social_volume_24h REAL NOT NULL,
    avg_galaxy_score REAL NOT NULL,
    last_galaxy_score REAL,              -- for galaxy_jump detection
    last_interactions_24h REAL,          -- for interactions_accel detection
    sample_count INTEGER NOT NULL,
    last_updated TEXT NOT NULL
);

-- Retention: nightly prune at pipeline startup
-- DELETE FROM social_signals WHERE detected_at < datetime('now', '-30 days')
```

**Orthogonality from `candidates`:** no FK. Ensemble classifier (PR #34) will join across `social_signals`, `velocity_alerts`, `candidates` by `coin_id` / `LOWER(symbol)` — consistent with how `trending_comparisons` already joins today.

**Retention:** `_prune_social_signals(db, days=30)` called once on `Database.initialize()`. Keeps index small forever.

---

## 8. Alert flow (corrected — closes Risk-Reviewer finding #3)

```
every LUNARCRUSH_POLL_INTERVAL seconds:
  1. check credit budget; if exhausted → log + skip cycle
  2. fetch /coins/list/v2
  3. for each coin:
       load baseline from cache (hydrate from DB on first run)
       run 3 spike checks → collect triggered SpikeKinds
       update baseline (with spike-exclusion rule §5.4)
  4. build ResearchAlert list from coins with ≥1 triggered kind
  5. filter out coins in social_signals within LUNARCRUSH_DEDUP_HOURS
  6. filter out coins where baseline.sample_count < MIN_SAMPLES
  7. sort by highest spike_ratio; take top-N
  8. enrich with price_change_1h from price_cache (simple SELECT)
  9. **INSERT into social_signals first** (persist dedup state)
  10. **THEN** dispatch single batched Telegram message
  11. every 12 polls: checkpoint baseline cache → social_baselines
```

**Step-order rationale:** Reversed from initial plan so a DB-write failure cannot be followed by a successful Telegram send (which would have bypassed dedup and produced a message flood). If the INSERT fails, the send is skipped this cycle; the alert will be considered "fresh" again next cycle when DB recovers.

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

---

## 10. Settings (`scout/config.py`)

```python
# -------- LunarCrush Social-Velocity Alerter --------
LUNARCRUSH_ENABLED: bool = False                    # master flag
LUNARCRUSH_API_KEY: str = ""                        # empty → loop never starts
LUNARCRUSH_BASE_URL: str = "https://lunarcrush.com/api4/public"
LUNARCRUSH_POLL_INTERVAL: int = 300                 # 5 min
LUNARCRUSH_RATE_LIMIT_PER_MIN: int = 9              # under hard 10/min
LUNARCRUSH_DAILY_CREDIT_BUDGET: int = 2000          # free tier cap
LUNARCRUSH_CREDIT_SOFT_PCT: float = 0.80            # downshift at 80%
LUNARCRUSH_CREDIT_HARD_PCT: float = 0.95            # stop at 95%
LUNARCRUSH_SOCIAL_SPIKE_RATIO: float = 2.0
LUNARCRUSH_GALAXY_JUMP: float = 10.0
LUNARCRUSH_INTERACTIONS_ACCEL: float = 3.0
LUNARCRUSH_DEDUP_HOURS: int = 4
LUNARCRUSH_TOP_N: int = 10
LUNARCRUSH_BASELINE_MIN_SAMPLES: int = 288          # 24h warmup
LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS: int = 12       # 60 min
LUNARCRUSH_RETENTION_DAYS: int = 30
```

Double kill-switch: `LUNARCRUSH_ENABLED=false` **or** empty `LUNARCRUSH_API_KEY` disables the loop.

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

## 12. Trending-tracker hit-rate integration

Extend `scout/trending/tracker.compare_with_signals()` to count `social_signals` as a 4th detector tier (alongside narrative, pipeline, chains, spikes). Schema change: add columns `detected_by_social INTEGER DEFAULT 0, social_lead_minutes REAL` to `trending_comparisons`. Populate in the compare pass by joining on `coin_id` with a 4h window before the token appeared on trending.

This closes the Architect-Reviewer finding #4 — the trending tracker gets full tier coverage rather than silently ignoring social signals in its hit-rate numbers.

---

## 13. Testing strategy

TDD order. `aioresponses` for HTTP mocks, `tmp_path` for DB fixtures (same as existing tests).

| Test file | Coverage |
|---|---|
| `tests/test_social_lunarcrush_client.py` | auth header correct, 429 backoff sequence (5/10/20/60s cap), **401 sets disabled flag**, malformed-JSON resilience, missing-field handling, field rename drift (`social_volume_24h` / `interactions_24h`) |
| `tests/test_social_detector.py` | each of 3 spike kinds fires independently, **multi-kind coin produces ONE alert with multiple kinds**, **dedup boundary exactly at 4h (≥ not >)**, **cold-start suppression at 287 vs fires at 288**, top-N limit, coin list with 500 entries / 50% qualifying produces ≤ TOP_N alerts |
| `tests/test_social_baselines.py` | rolling avg correctness, **spike-day value does NOT poison baseline**, sample_count increments, **survives restart** (close DB, reopen, confirm sample_count preserved), graceful shutdown flush |
| `tests/test_social_alert_format.py` | Telegram message structure, Markdown escaping, URL inclusion, >4096 char truncation, multi-kind message format, missing price_cache enrichment |
| `tests/test_social_credit_budget.py` | 80% soft shift, 95% hard stop, midnight-UTC rollover, concurrent credit counting under multiple requests |
| `tests/test_social_db.py` | tables created, indexes present, 30-day prune deletes correctly, dedup query uses `datetime()` wrap |
| `tests/test_trending_tracker_social.py` (extend) | social_lead_minutes computed when social fires before trending appearance |

Target: +~40 new tests, full suite passing with no regressions to existing 616.

---

## 14. Rollout plan

1. Branch `feat/lunarcrush-integration` off current master (`07d4d58`).
2. TDD loop per test file above. Commit per green milestone.
3. Full pytest green → open PR #28.
4. Design-review agents (2) dispatched on spec before coding, PR-review agents (3) dispatched on final diff.
5. Deploy: on VPS, pull + append to `.env`:
   ```
   LUNARCRUSH_ENABLED=true
   LUNARCRUSH_API_KEY=<purchased>
   ```
   Restart `gecko-pipeline.service`.
6. Watch for 30h:
   - Day 1: baseline_warmup events expected; no alerts yet.
   - Day 2 hour 0 onward: alerts begin firing.
   - Metrics: no 401/403, no credit_budget_exhausted, alert rate within 0–10/day.

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

| Reviewer | Finding | Incorporated in |
|---|---|---|
| Architect | Per-cycle SQLite writes at scale | §6 in-process cache + 60-min checkpoint |
| Architect | 3 spike types → god function | §5 three pure functions |
| Architect | Naming collision for future vendors | §3 nested `scout/social/lunarcrush/` |
| Architect | Price context from `price_cache` (not HTTP) | §8 step 8 |
| Architect | Trending tracker integration gap | §12 |
| Risk | Baseline lockout on restart | §6 DB persistence |
| Risk | Multi-kind spam | §5.2 collapse to one alert |
| Risk | DB-write-before-Telegram | §8 flow step order |
| Risk | Baseline poisoning | §5.4 spike-exclusion rule |
| Risk | Research-only via convention | §3 `ResearchAlert` type |
| Risk | No retention | §7 30-day prune |
| Risk | Multiple test gaps | §13 full test matrix |
| API reality | Backfill infeasible on $24 tier | §15 explicit non-goal |
| API reality | Field name drift | §4 table + §7 schema |
| API reality | Credits budget, not just req/min | §4 + §10 settings |
| API reality | Hard 10/min rate limit | §10 `LUNARCRUSH_RATE_LIMIT_PER_MIN=9` |
