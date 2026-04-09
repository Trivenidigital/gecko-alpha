# Counter-Narrative Scoring — Design Spec

**Date:** 2026-04-09
**Status:** Approved
**Goal:** For every token reaching the alert stage (both pipelines), run an adversarial analysis that argues AGAINST the trade. Outputs a risk score + deterministic red flags + counter-argument. Informational by default, self-adjustable suppression via LEARN phase.
**Module:** `scout/counter/` — independent from existing scoring, plugs into both pipelines
**Cost:** ~$0.50-1/month Claude API + minimal CoinGecko calls (cached)

---

## 1. Architecture Overview

```
Existing Pipeline (memecoins)                    Narrative Agent (CG-listed tokens)
        │                                                │
   Token passes                                    Laggard scored by
   conviction gate                                  predictor.py
        │                                                │
        ▼                                                ▼
  ┌──────────────────────────────────────────────────────────┐
  │              scout/counter/scorer.py                       │
  │                                                            │
  │  1. Fetch /coins/{id} detail (cached, 30-min TTL)         │
  │  2. Compute deterministic red flags + severities           │
  │  3. Call Claude haiku (temp=0.3): risk_score +             │
  │     counter_argument from pre-computed flags               │
  │  4. Return CounterScore                                    │
  └──────────────────────────────────────────────────────────┘
        │                                                │
        ▼                                                ▼
  Send alert immediately,                         Attach to prediction,
  then send counter follow-up                     include in alert inline
  async via Telegram                              (30-min cycle, no rush)
```

Both pipelines produce the same `CounterScore` output. Different prompts select different flag sets.

---

## 2. Module Structure

```
scout/counter/
  __init__.py
  models.py        # CounterScore, RedFlag models
  detail.py        # CoinGecko /coins/{id} fetcher + 30-min cache
  flags.py         # Deterministic flag computation from data thresholds
  scorer.py        # Orchestrator: fetch detail → compute flags → call LLM
  prompts.py       # Two static prompts (narrative + memecoin)
```

---

## 3. Models (`scout/counter/models.py`)

```python
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, field_validator


class RedFlag(BaseModel):
    flag: str           # from enumerated set only
    severity: str       # "low" | "medium" | "high" (deterministic, not LLM-assigned)
    detail: str         # human-readable explanation

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in ("low", "medium", "high"):
            return "medium"
        return v


class CounterScore(BaseModel):
    risk_score: int | None = None   # 0-100, None if LLM call failed
    red_flags: list[RedFlag]        # deterministic, computed before LLM call
    counter_argument: str           # LLM-generated narrative synthesis
    data_completeness: str          # "full" | "partial" | "pipeline_only"
    counter_scored_at: datetime

    @field_validator("risk_score")
    @classmethod
    def clamp_score(cls, v: int | None) -> int | None:
        if v is None:
            return None
        return max(0, min(100, v))
```

---

## 4. Flag Taxonomy (Enumerated)

### Narrative Agent Flags (CoinGecko-listed tokens)

| Flag | Condition | Severity |
|------|-----------|----------|
| `already_peaked` | `price_change_30d > 100%` | HIGH |
| `already_peaked` | `price_change_30d > 50%` | MEDIUM |
| `dead_project` | `commits_4w == 0` | HIGH |
| `dead_project` | `commits_4w < 10` | MEDIUM |
| `weak_community` | `reddit_subs < 100` | HIGH |
| `weak_community` | `reddit_subs < 1000` | MEDIUM |
| `negative_sentiment` | `sentiment_up_pct < 40%` | HIGH |
| `negative_sentiment` | `sentiment_up_pct < 50%` | MEDIUM |
| `volume_divergence` | token volume declining while category volume rising | HIGH |
| `narrative_mismatch` | narrative_fit_score < 40 (from bullish pass) | HIGH |
| `narrative_mismatch` | narrative_fit_score < 60 | MEDIUM |
| `overvalued_vs_leaders` | token mcap > 50% of category leader mcap | MEDIUM |

### Memecoin Pipeline Flags (DEX tokens)

| Flag | Condition | Severity |
|------|-----------|----------|
| `wash_trading` | `buy_pressure > 95% or < 5%` | HIGH |
| `wash_trading` | `buy_pressure > 90% or < 10%` | MEDIUM |
| `deployer_concentration` | GoPlus `creator_percent > 20%` | HIGH |
| `deployer_concentration` | GoPlus `creator_percent > 10%` | MEDIUM |
| `liquidity_trap` | `liquidity_usd < 15000` | HIGH |
| `liquidity_trap` | `liquidity_usd < 30000` | MEDIUM |
| `token_too_new` | `token_age_days < 0.25` (< 6 hours) | HIGH |
| `token_too_new` | `token_age_days < 0.5` (< 12 hours) | MEDIUM |
| `suspicious_volume` | `volume / liquidity > 50` | HIGH |
| `suspicious_volume` | `volume / liquidity > 20` | MEDIUM |
| `honeypot_risk` | GoPlus `is_honeypot == True` | HIGH |
| `low_holders` | `holder_count < 50` | HIGH |
| `low_holders` | `holder_count < 200` | MEDIUM |

All thresholds are constants in `flags.py`. Not in agent_strategy (deterministic, not learnable — the flag computation must be stable for LEARN to measure correlation).

---

## 5. Deterministic Flag Computation (`flags.py`)

Flags are computed BEFORE the LLM call. The LLM does NOT add, remove, or re-score flags. It only interprets them.

```python
def compute_narrative_flags(
    token_data: dict,           # from /coins/{id} or /coins/markets
    narrative_fit_score: int,   # from bullish scoring pass
    category_volume_trend: float,  # from narrative agent
) -> list[RedFlag]:
    """Compute red flags for CoinGecko-listed tokens. Deterministic."""
    flags = []
    # price_change_30d
    change_30d = token_data.get("price_change_percentage_30d_in_currency") or 0
    if change_30d > 100:
        flags.append(RedFlag(flag="already_peaked", severity="high",
            detail=f"+{change_30d:.0f}% in 30d, may be exhausted"))
    elif change_30d > 50:
        flags.append(RedFlag(flag="already_peaked", severity="medium",
            detail=f"+{change_30d:.0f}% in 30d"))
    # ... similar for each flag
    return flags

def compute_memecoin_flags(
    token,                      # CandidateToken from existing pipeline
    goplus_data: dict | None,   # from scout/safety.py if available
) -> list[RedFlag]:
    """Compute red flags for DEX memecoins. Deterministic."""
    flags = []
    # ... threshold checks from table above
    return flags
```

---

## 6. CoinGecko Detail Fetcher + Cache (`detail.py`)

### Module-level cache

```python
_detail_cache: dict[str, tuple[datetime, dict]] = {}
CACHE_TTL_SECONDS = 1800  # 30 minutes

async def fetch_coin_detail(
    session: aiohttp.ClientSession,
    coin_id: str,
    api_key: str = "",
) -> dict | None:
    """Fetch /coins/{id} with 30-min in-memory cache. Returns None on failure."""
    now = datetime.now(timezone.utc)

    # Check cache
    if coin_id in _detail_cache:
        cached_at, data = _detail_cache[coin_id]
        if (now - cached_at).total_seconds() < CACHE_TTL_SECONDS:
            return data

    # Fetch
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "true",
        "developer_data": "true",
        "sparkline": "false",
    }
    headers = {}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 429:
                logger.warning("counter_detail_rate_limited", coin_id=coin_id)
                return None
            if resp.status == 404:
                logger.info("counter_detail_not_found", coin_id=coin_id)
                return None
            resp.raise_for_status()
            data = await resp.json()
            _detail_cache[coin_id] = (now, data)
            await asyncio.sleep(1)  # spacing, not shared limiter (see GH #2)
            return data
    except Exception as e:
        logger.warning("counter_detail_fetch_error", coin_id=coin_id, error=str(e))
        return None
```

### Fields extracted from detail response

```python
def extract_counter_data(detail: dict) -> dict:
    """Extract fields relevant to counter-scoring from /coins/{id} response."""
    market = detail.get("market_data", {})
    community = detail.get("community_data", {})
    developer = detail.get("developer_data", {})
    sentiment = detail.get("sentiment_votes_up_percentage")

    return {
        "commits_4w": developer.get("commit_count_4_weeks") or 0,
        "reddit_subscribers": community.get("reddit_subscribers") or 0,
        "telegram_users": community.get("telegram_channel_user_count") or 0,
        "sentiment_up_pct": sentiment or 50.0,
        "price_change_7d": market.get("price_change_percentage_7d") or 0,
        "price_change_30d": market.get("price_change_percentage_30d") or 0,
    }
```

---

## 7. LLM Scoring (`scorer.py`)

The LLM receives pre-computed flags as ground truth and produces ONLY `risk_score` + `counter_argument`.

```python
async def score_counter(
    flags: list[RedFlag],
    token_context: str,         # formatted token info for prompt
    pipeline_type: str,         # "narrative" or "memecoin"
    api_key: str,
    model: str = "claude-haiku-4-5",
) -> tuple[int | None, str]:
    """Call Claude to synthesize flags into risk_score + counter_argument.
    Returns (risk_score, counter_argument) or (None, "") on failure.
    """
```

### Failure handling
- Claude API timeout/error: return `(None, "")`. CounterScore gets `risk_score=None`.
- No retry — counter-scoring is informational, not worth burning retries on.
- `counter_risk_score = None` in DB means "counter-score unavailable", excluded from LEARN analysis.

---

## 8. Prompts (`scout/counter/prompts.py`)

### Shared system prompt

```
You are a risk analyst evaluating crypto trades. You receive objective red flags
with pre-computed severities. Your job: synthesize these flags into a risk assessment.
Do NOT add new flags or change severities — they are computed from data.
Return ONLY valid JSON.
```

### Narrative prompt template

```
Token: {token_name} ({symbol}), ${market_cap:,.0f} mcap, {price_change_24h:+.1f}% 24h
Category: {category_name} (accelerating: {acceleration:+.1f}%)
Narrative fit score (bullish): {narrative_fit_score}/100
Data completeness: {data_completeness}

PRE-COMPUTED RED FLAGS (ground truth — do not modify):
{formatted_flags}

SCORING SCALE:
0-20: No identifiable risk. All data points look healthy.
21-40: Minor concerns that don't invalidate the thesis.
41-60: Meaningful risk — one or more flags warrant caution.
61-80: Strong evidence against this trade. Multiple high-severity flags.
81-100: Clear red flags — high probability of loss.

Based on the red flags above, assign a risk_score and write a 1-2 sentence
counter_argument explaining why this trade might fail.

Return ONLY JSON:
{"risk_score": <int 0-100>, "counter_argument": "<1-2 sentences>"}
```

### Memecoin prompt template

Same structure, different token context fields (token_age, liquidity, volume, buy_pressure, GoPlus data).

---

## 9. Integration Points

### Existing Pipeline (async follow-up)

In `scout/main.py`, after the alert is sent:

```python
# Alert fires immediately (no delay)
await send_alert(token, session, settings)

# Counter-score runs async, sends follow-up
if settings.COUNTER_ENABLED:
    asyncio.create_task(
        _counter_score_and_followup(token, session, settings, db)
    )
```

The follow-up Telegram message:
```
Risk assessment for {SYMBOL}:
Risk: {risk_score}/100 | {data_completeness} data
{formatted_flags}
"{counter_argument}"
```

Store `counter_risk_score`, `counter_flags`, `counter_argument`, `counter_data_completeness`, `counter_scored_at` on the `candidates` table.

### Narrative Agent (inline)

In the narrative agent loop, after `score_token` and before `store_predictions`:

```python
counter = await score_counter_narrative(token, accel, detail_data, flags, settings)
prediction.counter_risk_score = counter.risk_score
prediction.counter_flags = counter.red_flags
# ... store with prediction
```

Alert format includes counter inline:
```
1. FET ($340M, +2%) — Fit: 82 | Risk: 45 | Net: +37
   Flags: already_peaked (HIGH), weak_community (MED)
   "Despite category momentum, FET already ran +120% in 30d..."
```

---

## 10. Database Schema Changes

### `candidates` table (existing pipeline) — add columns

```sql
-- Added to _create_tables in scout/db.py (safe: CREATE TABLE IF NOT EXISTS recreates with columns)
counter_risk_score       INTEGER,
counter_flags            TEXT,      -- JSON list of RedFlag dicts
counter_argument         TEXT,
counter_data_completeness TEXT,     -- "full" | "partial" | "pipeline_only"
counter_scored_at        TEXT
```

### `predictions` table (narrative agent) — add columns

```sql
counter_risk_score       INTEGER,
counter_flags            TEXT,      -- JSON list of RedFlag dicts
counter_argument         TEXT,
counter_data_completeness TEXT,
counter_scored_at        TEXT
```

Since SQLite doesn't support ALTER TABLE ADD COLUMN in executescript easily, and the tables were just created in the narrative agent PR, we add these columns to the existing CREATE TABLE statements in `scout/db.py`.

---

## 11. NarrativePrediction Model Update

Add to `scout/narrative/models.py` NarrativePrediction:

```python
counter_risk_score: int | None = None
counter_flags: list[dict] | None = None
counter_argument: str | None = None
counter_data_completeness: str | None = None
counter_scored_at: datetime | None = None
```

---

## 12. LEARN Phase Integration

The narrative agent's LEARN phase already analyzes predictions. With counter-score data persisted:

- Daily reflection prompt receives `counter_risk_score` per prediction
- Can correlate: "predictions with counter_risk_score > 60 had 12% hit rate vs 48% for lower"
- Can segment by `data_completeness`: "full-detail counter-scores correlated 0.72 vs 0.31 for partial"
- `counter_suppress_threshold` in `agent_strategy`: default 100 (never suppress). LEARN can lower it.

### Strategy additions

| Key | Default | Bounds | Description |
|-----|---------|--------|-------------|
| `counter_suppress_threshold` | 100 | 0–100 | Risk score above which to suppress alerts |

For the existing pipeline, `COUNTER_SUPPRESS_THRESHOLD` is in config/.env only (no LEARN loop to adjust it).

---

## 13. Configuration

```python
# Counter-Narrative Scoring
COUNTER_ENABLED: bool = True
COUNTER_MODEL: str = "claude-haiku-4-5"
COUNTER_SUPPRESS_THRESHOLD: int = 100   # existing pipeline only (env config)
```

---

## 14. Control Picks

Control picks (`is_control=True`) do NOT get counter-scored. They don't get narrative-fit scoring either, so the comparison is already apples-to-oranges. This saves API calls and keeps the control baseline clean.

---

## 15. Known Limitations (v1)

1. **Same model family for bullish + adversarial passes.** Both use Claude Haiku. Mitigated by: different temperature (0.3 vs 0), deterministic flags (LLM only synthesizes, doesn't compute), and LEARN phase monitoring correlation between `narrative_fit_score` and `counter_risk_score` (if they're just inversely correlated with noise, counter adds zero information — LEARN can detect this).

2. **Rate limiter not shared** between pipelines for `/coins/{id}` calls. Mitigated by 30-min cache TTL and 1-second spacing. Tracked as GH issue #2.

3. **Flag thresholds are static constants**, not learnable. This is intentional — deterministic flags must be stable for LEARN to measure their predictive value. If thresholds change, historical correlations become invalid.

---

## 16. Testing Strategy

| Test File | Coverage |
|-----------|----------|
| `tests/test_counter_models.py` | CounterScore, RedFlag validation, score clamping |
| `tests/test_counter_flags.py` | All flag computations: each threshold, edge cases, empty data |
| `tests/test_counter_detail.py` | Cache hit/miss, 429 handling, 404 handling, extract_counter_data |
| `tests/test_counter_scorer.py` | Orchestrator: full flow with mocked detail + Claude, failure modes |
| `tests/test_counter_prompts.py` | Prompt formatting, flag serialization |

---

## 17. Out of Scope

- Changing existing scoring logic (scorer.py)
- Adjusting conviction gate thresholds based on counter-score (future)
- LunarCrush/social data integration into counter-scoring (future)
- Counter-scoring for narrative agent control picks
