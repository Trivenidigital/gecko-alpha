# LunarCrush Integration — Plan (pre-design)

**Date:** 2026-04-18
**Status:** Draft — pending parallel review
**Supersedes:** 2026-04-09-early-detection-lunarcrush-design.md (pre-PR #12, pre-PR #27)
**PR target:** #28 in sprint plan (was PR #31, promoted because of Musk/ASTEROID pump priority)
**Sprint:** Virality Detection Roadmap — Sprint 2

---

## 1. Goal

Surface tokens with *social* velocity (influencer endorsement, cultural moments, narrative rotation) **minutes ahead of price velocity**, which our existing CoinGecko-only stack cannot see.

Concrete example: ASTEROID (+114775% in 24h) was driven by a Musk reply to a Polaris Dawn post. Our velocity alerter (PR #27) catches the 1h price move; LunarCrush would have flagged the social-mention surge *before* the price parabolic.

---

## 2. What changed since the 2026-04-09 design

| Then | Now |
|---|---|
| Shadow mode only — no Telegram | **Telegram plain-text alert tier** (like PR #27) |
| New `trending_snapshots` table | Table **already exists** (PR #12) — reuse |
| New comparison engine | Comparison engine **already exists** (PR #12) — extend, don't duplicate |
| Full dashboard tab (5 components) | **Defer to Sprint 4 ensemble meta-tier.** MVP = Telegram + DB persistence + CLI inspection. |
| `from scout.early/` package | Align with new naming: `scout/social/` (parallel to `scout/velocity/`) |
| Scoring integration in future phase | **Explicitly NO** scoring integration. Research alert only. |

---

## 3. Scope (MVP)

**Included:**
1. `scout/social/lunarcrush.py` — async API client, rate-limited, exponential backoff
2. `scout/social/detector.py` — spike detection (3 spike types) + dedup + Telegram dispatch
3. `scout/social/baselines.py` — rolling baseline storage and update
4. `scout/config.py` — new settings block
5. `scout/db.py` — `social_signals` + `social_baselines` tables
6. `scout/main.py` — background loop gated on `LUNARCRUSH_ENABLED` + `LUNARCRUSH_API_KEY`
7. Tests via `aioresponses` for HTTP mocks, TDD order

**Explicitly out of scope:**
- Dashboard UI (deferred to PR #34 ensemble meta-tier)
- Paper trade dispatch (research-only, per PR #27 lesson)
- Scorer integration (keeps scorer pure)
- Santiment / Nansen (Sprint 3+)
- Historical backfill of baselines (cold-start period accepted)

---

## 4. Key decisions to validate with reviewers

| # | Decision | Rationale |
|---|---|---|
| D1 | Package name `scout/social/` (not `scout/early/`) | Aligns with new virality-tier naming (`scout/velocity/`). "early" is overloaded — we already have trending_tracker and velocity. |
| D2 | Reuse existing `trending_snapshots` table + `trending/tracker.py` comparison | Avoid duplication. Add `social_signals` FK-compatible with existing schema via symbol match. |
| D3 | Three spike types: (a) social_volume spike ≥ 2× 7d baseline, (b) galaxy_score jump ≥ +10 in 1h, (c) mention_accel ≥ 3× 30-min window | From 2026-04-09 design. Keep all three — each catches a different pattern (sustained buzz, composite health jump, burst). |
| D4 | Dedup: `LUNARCRUSH_DEDUP_HOURS=4` (same as PR #27) | Consistency. Social spikes resolve faster than price pumps but 4h is a safe floor. |
| D5 | Telegram format: **separate** from velocity alerter. Header `*Social Velocity*`, list top-N with galaxy score / mention count / 1h % price change. Include LunarCrush + CoinGecko chart links. | Distinct branding so user can tell at a glance which tier fired. |
| D6 | Poll interval `LUNARCRUSH_POLL_INTERVAL=300` (5 min) | LunarCrush updates every 5 min. More frequent wastes quota. |
| D7 | Rate limit: **10 req/min soft cap**, exponential backoff on 429 | Individual plan documented limit is higher but leave headroom for time-series calls. |
| D8 | Run in its own `asyncio.Task` (not inside main scan cycle) | Polls on its own cadence, independent of the 60s main scan. Failures isolated. |
| D9 | Cold-start: require ≥24h of baseline samples before firing alerts | Prevents false-positives during baseline bootstrapping. Log "baseline_warmup" event. |
| D10 | Feature flag: `LUNARCRUSH_ENABLED` default `false`. Empty API key also disables (defence-in-depth). | Opt-in per deployment. |

---

## 5. Proposed data model

```sql
-- Persisted spike events (what we alerted on)
CREATE TABLE social_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,          -- LunarCrush id
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    spike_type TEXT NOT NULL,        -- 'social_volume' | 'galaxy_score' | 'mention_accel'
    galaxy_score REAL,
    social_volume REAL,
    social_volume_baseline REAL,
    spike_ratio REAL,
    mentions REAL,
    sentiment REAL,
    price_change_1h REAL,
    price_change_24h REAL,
    market_cap REAL,
    detected_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_social_signals_coin_detected ON social_signals(coin_id, detected_at);
CREATE INDEX idx_social_signals_symbol ON social_signals(symbol);

-- Rolling baselines updated on each poll
CREATE TABLE social_baselines (
    coin_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    avg_social_volume REAL NOT NULL,
    avg_galaxy_score REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    last_updated TEXT NOT NULL
);
```

Note: `social_signals` deliberately mirrors `velocity_alerts` schema shape where possible so the eventual ensemble classifier (PR #34) can do `UNION`-style reads.

---

## 6. Settings

```python
LUNARCRUSH_ENABLED: bool = False
LUNARCRUSH_API_KEY: str = ""
LUNARCRUSH_BASE_URL: str = "https://lunarcrush.com/api4/public"
LUNARCRUSH_POLL_INTERVAL: int = 300           # 5 min
LUNARCRUSH_RATE_LIMIT_PER_MIN: int = 10
LUNARCRUSH_SOCIAL_SPIKE_RATIO: float = 2.0
LUNARCRUSH_GALAXY_JUMP: float = 10.0
LUNARCRUSH_MENTION_ACCEL: float = 3.0
LUNARCRUSH_DEDUP_HOURS: int = 4
LUNARCRUSH_TOP_N: int = 10
LUNARCRUSH_BASELINE_MIN_SAMPLES: int = 288    # 24h / 5min = 288
```

---

## 7. Alert flow

```
poll loop (every 5 min):
  1. fetch /coins/list/v2
  2. for each coin:
     - load baseline (or bootstrap)
     - check 3 spike conditions
     - if any fire AND baseline has >= 288 samples:
       - skip if coin_id appeared in social_signals within LUNARCRUSH_DEDUP_HOURS
       - add to fresh detections
  3. update baselines (rolling avg, increment sample_count)
  4. sort fresh by highest spike_ratio, take top-N
  5. persist to social_signals
  6. dispatch single batched Telegram message (alerter.send_telegram_message)
```

---

## 8. Error isolation

- LunarCrush 5xx / timeout / network error → log warning, skip cycle
- 401/403 → log error **once**, disable loop for session (don't spam)
- 429 → exponential backoff (5s, 10s, 20s, max 60s), re-try
- DB write error → log, continue (data loss acceptable for research signal)
- **Never** raise into main pipeline — failures must not affect scan cycle

---

## 9. Testing strategy

| File | Coverage |
|---|---|
| `tests/test_social_lunarcrush.py` | API client: auth header, rate limiter, 429 backoff, bad JSON, missing fields |
| `tests/test_social_detector.py` | Each of 3 spike types, dedup window, baseline-cold-start suppression, top-N |
| `tests/test_social_baselines.py` | Rolling average correctness, sample_count increment, new-coin bootstrap |
| `tests/test_social_alert_format.py` | Telegram message structure, URL inclusion, truncation |

TDD order: tests first, implementation second.

---

## 10. Deployment plan

- Dev: run full pytest → all green (expect ~630 tests after additions)
- Branch: `feat/lunarcrush-integration`
- PR: `#28 — feat(social): LunarCrush social-velocity alerter`
- VPS deploy: pull, add keys to `.env`:
  ```
  LUNARCRUSH_ENABLED=true
  LUNARCRUSH_API_KEY=<purchased>
  ```
- Restart `gecko-pipeline.service`
- Watch 24h for:
  - baseline_warmup events during first day
  - no auth errors
  - alert rate (target: 0–10/day; >50/day = threshold too loose)

---

## 11. Open questions for reviewers

1. **Is `scout/social/` the right package name?** Or should it be `scout/lunarcrush/` (vendor-named) to match the way `scout/gainers/` etc. are domain-named but specific?
2. **Should baselines be per-coin or per-(coin, hour-of-day)?** The latter captures time-of-day seasonality. Adds complexity; may not matter if spike ratios are high enough.
3. **Is 24h cold-start acceptable?** Alternative: backfill baselines from LunarCrush time-series API at startup (more API quota used, faster time-to-value).
4. **Should we include 1h / 24h price-change in the alert message?** Pros: user gets price context without opening chart. Cons: requires pulling from CoinGecko price cache, cross-package dep.
5. **Should we alert on single-coin spike only, or also on *group* spikes (e.g. 3+ AI coins spike simultaneously = narrative rotation)?** The latter is a more valuable signal but couples to the narrative agent.
6. **Should `social_signals` FK to `candidates` table** so narrative agent can reach social context when scoring? Or keep tables orthogonal and let ensemble (PR #34) join across?

---

## 12. Success criteria

- ≥1 valid social-velocity alert fires per 24h on live VPS (proof of life)
- No auth errors after first 48h
- Baseline `sample_count` reaches 288 for ≥80% of tracked coins within 30h of first poll
- Zero impact on existing pipeline metrics (scan cycles / min, Telegram alert rate from other tiers)
- Integration adds <5% to monthly compute/bandwidth cost

---

## 13. Non-goals

- Predicting CoinGecko trending (deferred — covered by PR #12 tracker)
- Scoring integration (keeps scorer pure — D3 from backlog)
- Live trading (no paper-trade dispatch, no live execution)
- Dashboard UI (Sprint 4 ensemble tab)
