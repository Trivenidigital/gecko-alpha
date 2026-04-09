# Narrative Rotation Agent — Design Spec

**Date:** 2026-04-09
**Status:** Approved
**Goal:** Autonomous agent that detects accelerating crypto narratives on CoinGecko, picks undervalued tokens within hot categories, tracks outcomes, and self-adjusts its strategy over time.
**Module:** `scout/narrative/` — fully independent from existing pipeline
**Cost:** ~$3-5/month Claude API + CoinGecko free tier
**Deployment:** Runs inside gecko-alpha as a parallel async loop on Srilu VPS

---

## 1. Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│                   OBSERVE (every 30 min)                        │
│  CoinGecko /coins/categories → store snapshot                   │
│  Compute acceleration vs 6h/24h prior snapshots                 │
│  Rate limited: shares gecko-alpha's 30 req/min budget           │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│                   PREDICT (on heating detection)                │
│  Fetch laggard tokens in heating categories                     │
│  Claude haiku: score narrative fit (per laggard)                │
│  Store prediction with entry price + strategy snapshot          │
│  Dedup: skip categories already tracked in current window       │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│                   ALERT (after prediction)                      │
│  Telegram: heating narrative + top picks with scores            │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│                   EVALUATE (every 6 hours)                      │
│  Check predictions at 6h, 24h, 48h checkpoints                 │
│  Record outcome price + change % at each checkpoint             │
│  Classify: HIT / MISS / NEUTRAL (thresholds from strategy)     │
│  Handle: delisted / price unavailable → mark UNRESOLVED         │
└──────────────────────┬─────────────────────────────────────────┘
                       ▼
┌────────────────────────────────────────────────────────────────┐
│                   LEARN (daily + weekly)                         │
│  Daily: Claude sonnet reviews last 50 outcomes, suggests tweaks │
│  Weekly: consolidate lessons, prune old ones, version prompt     │
│  Update agent_strategy with reasons + bounds enforcement         │
│  Circuit breaker: pause if hit rate < 10% for 7 days            │
└────────────────────────────────────────────────────────────────┘
```

---

## 2. Module Structure

All new code lives in `scout/narrative/`. No existing files are modified except `scout/db.py` (new tables), `scout/main.py` (add loop to gather), `scout/config.py` (new config keys), `dashboard/api.py` (new endpoints), and dashboard frontend (new tab).

```
scout/narrative/
  __init__.py
  observer.py          # Category polling, acceleration detection
  predictor.py         # Laggard selection + Claude narrative-fit scoring
  evaluator.py         # Outcome tracking at multiple checkpoints
  learner.py           # Self-reflection, strategy updates, prompt evolution
  strategy.py          # Read/write agent_strategy table, defaults, bounds, locks
  digest.py            # Daily/weekly summary builder
  models.py            # All Pydantic models
  prompts.py           # Base prompts (static) + lessons appendix (dynamic)
```

---

## 3. Pydantic Models (`scout/narrative/models.py`)

```python
from pydantic import BaseModel
from datetime import datetime

class CategorySnapshot(BaseModel):
    category_id: str
    name: str
    market_cap: float
    market_cap_change_24h: float
    volume_24h: float
    coin_count: int                    # number of coins in category (R3 #2)
    snapshot_at: datetime

class CategoryAcceleration(BaseModel):
    category_id: str
    name: str
    current_velocity: float        # market_cap_change_24h now
    previous_velocity: float       # market_cap_change_24h 6h ago
    acceleration: float            # current - previous
    volume_growth_pct: float       # volume change over 6h
    coin_count_change: int         # coin count now minus 6h ago (R3 #2)
    is_heating: bool

class LaggardToken(BaseModel):
    coin_id: str                   # CoinGecko coin ID
    symbol: str
    name: str
    market_cap: float
    price: float
    price_change_24h: float
    volume_24h: float
    category_id: str
    category_name: str

class NarrativePrediction(BaseModel):
    id: int | None = None
    category_id: str
    category_name: str
    coin_id: str
    symbol: str
    name: str
    market_cap_at_prediction: float
    price_at_prediction: float
    narrative_fit_score: int       # 0-100
    staying_power: str             # Low | Medium | High
    confidence: str                # Low | Medium | High
    reasoning: str
    market_regime: str             # BULL | BEAR | CRAB (R3 #4)
    trigger_count: int             # signal strength (R3 #7)
    is_control: bool = False       # random baseline pick (R3 #1)
    is_holdout: bool = False       # using old strategy for A/B test (R3 #3)
    strategy_snapshot: dict        # keys used in this prediction's logic
    predicted_at: datetime
    # Outcomes (filled by evaluator)
    outcome_6h_price: float | None = None
    outcome_6h_change_pct: float | None = None
    outcome_24h_price: float | None = None
    outcome_24h_change_pct: float | None = None
    outcome_48h_price: float | None = None
    outcome_48h_change_pct: float | None = None
    peak_price: float | None = None          # R3 #5
    peak_change_pct: float | None = None     # R3 #5
    peak_at: datetime | None = None          # R3 #5
    outcome_class: str | None = None  # HIT | MISS | NEUTRAL | UNRESOLVED
    evaluated_at: datetime | None = None

class NarrativeSignal(BaseModel):
    category_id: str
    category_name: str
    acceleration: float
    volume_growth_pct: float
    coin_count_change: int         # R3 #2: survivorship bias indicator
    trigger_count: int = 1         # R3 #7: consecutive heating cycles
    detected_at: datetime
    cooling_down_until: datetime   # dedup window end

class StrategyValue(BaseModel):
    key: str
    value: str                     # JSON-encoded
    updated_at: datetime
    updated_by: str                # 'init' | 'learn_cycle_N' | 'manual'
    reason: str
    locked: bool = False           # manual override prevents agent changes
    min_bound: float | None = None
    max_bound: float | None = None

class LearnLog(BaseModel):
    id: int | None = None
    cycle_number: int
    cycle_type: str                # 'daily' | 'weekly'
    reflection_text: str
    changes_made: dict             # {key: {old: x, new: y, reason: z}}
    hit_rate_before: float
    hit_rate_after: float | None = None
    created_at: datetime
```

---

## 4. OBSERVE Phase (`observer.py`)

### What it does
Polls CoinGecko `/coins/categories` every 30 minutes. Stores snapshot. Computes acceleration by comparing against the snapshot from 6 hours ago.

### Rate limiting (addresses review issue #1)
- Uses CoinGecko free Demo tier (30 req/min shared with existing pipeline)
- OBSERVE uses exactly **1 API call** per 30-min cycle (`/coins/categories`)
- PREDICT adds 1-3 calls (`/coins/markets?category={id}`) only when categories are heating
- Total narrative agent budget: ~2-4 calls per cycle, ~8-16 calls/hour
- Shares the existing rate limiter from `scout/ingestion/coingecko.py` — pass it as a dependency
- On 429: exponential backoff (2s, 4s, 8s), same pattern as existing coingecko.py

### Acceleration formula

```python
velocity = snapshot_now.market_cap_change_24h          # e.g., +12%
previous_velocity = snapshot_6h_ago.market_cap_change_24h  # e.g., +5%
acceleration = velocity - previous_velocity            # e.g., +7%

volume_now = snapshot_now.volume_24h
volume_6h_ago = snapshot_6h_ago.volume_24h
volume_growth_pct = ((volume_now - volume_6h_ago) / volume_6h_ago) * 100

is_heating = (
    acceleration > strategy.get("category_accel_threshold")  # default: 5.0
    and volume_growth_pct > strategy.get("category_volume_growth_min")  # default: 10.0
)
```

### Survivorship bias detection (R3 issue #2)
Track `coin_count` per category snapshot (number of coins CoinGecko lists in that category). Compare current vs 6h-ago snapshot:

```python
coin_count_change = snapshot_now.coin_count - snapshot_6h_ago.coin_count
```

If `coin_count_change < -5%` (significant drop), flag the category as potentially survivor-biased: the acceleration may be inflated because weak tokens were delisted. This flag is:
- Logged as a warning in OBSERVE
- Included in the `CategoryAcceleration` model as `coin_count_change`
- Passed to the LEARN phase so it can correlate survivorship-flagged signals with MISS outcomes
- NOT an automatic disqualifier (the category may still be legitimately heating), but the LEARN phase can learn to downweight these over time

### Defensive parsing
CoinGecko response fields may change or be null. Each category entry is parsed defensively:
- Missing/null `market_cap_change_24h` or `volume_24h`: skip entry, log warning, continue
- Unexpected response shape: wrap entire parse in try/except per category, never crash the loop
- Log malformed entries for debugging but don't halt OBSERVE

### PREDICT budget cap (R1 suggestion)
Maximum `max_heating_per_cycle` categories processed per 30-min cycle (default: 5, from strategy table). Sorted by highest acceleration first. Prevents API budget blowout when many categories heat simultaneously.

### Laggard sorting tie-breaker (R1 suggestion)
Primary sort: `price_change_24h` ascending (most behind narrative). Secondary tie-breaker: `volume_24h / market_cap` descending (higher volume-to-mcap ratio = more active interest). This prevents surfacing dead coins that happen to have low price change.

### Market regime detection (R3 issue #4)
Each OBSERVE cycle also captures a top-level market regime signal using data already available from the `/coins/categories` response (no extra API calls — the total crypto market cap is the sum of all categories, and BTC is always in the response):

```python
# Derive from category data already fetched
total_market_cap = sum(c.market_cap for c in categories)
total_market_cap_change_24h = weighted_average(c.market_cap_change_24h for c in categories)

# Simple regime classification
if total_market_cap_change_24h > 3.0:
    market_regime = "BULL"
elif total_market_cap_change_24h < -3.0:
    market_regime = "BEAR"
else:
    market_regime = "CRAB"
```

The `market_regime` is:
- Stored on each `category_snapshot` and each `prediction`
- Passed to the LEARN phase so it can segment analysis: "my agent hits 55% in BULL but only 12% in BEAR — should I raise thresholds in BEAR or pause predictions entirely?"
- Included in the daily reflection prompt as context
- The LEARN phase can add regime-specific adjustments (e.g., `hit_threshold_pct_bear = 25.0`)

### Minimum data requirement
- Need at least 6 hours of snapshots (12 data points at 30-min intervals) before acceleration detection starts
- First 6 hours: observe-only mode, collect baseline data

---

## 5. PREDICT Phase (`predictor.py`)

### Category deduplication with signal strength (addresses review issue #5, R3 issue #7)
When a category is detected as heating:
1. Check `narrative_signals` table for an active signal for this category
2. If `cooling_down_until > now()`: skip — already tracked, but increment `trigger_count` on the existing signal
3. If no active signal: create one with `cooling_down_until = now + signal_cooldown_hours`, `trigger_count = 1`
4. If the category is STILL heating when cooldown expires, a new signal is created with `trigger_count = 1`

The `trigger_count` field tracks how many consecutive 30-min cycles a category has been heating. A category that triggers 6 times in 4 hours (trigger_count=6) is fundamentally different from a one-time spike (trigger_count=1).

- `trigger_count` is stored on the `narrative_signals` table and included in predictions
- The LEARN phase can correlate trigger_count with outcomes: "categories with trigger_count >= 4 have 60% hit rate vs 25% for single triggers"
- The agent can learn to set a `min_trigger_count` threshold (default: 1, meaning all triggers produce predictions)

### Laggard token selection
For each newly-detected heating category:
1. Fetch `GET /coins/markets?vs_currency=usd&category={category_id}&order=market_cap_desc&per_page=100`
2. Filter laggards (all thresholds from `agent_strategy`, not hardcoded):

```python
laggards = [
    t for t in tokens
    if t.market_cap < strategy.get("laggard_max_mcap")           # default: 200M
    and t.price_change_24h < strategy.get("laggard_max_change")  # default: 10%
    and t.price_change_24h > strategy.get("laggard_min_change")  # default: -20%
    and t.volume_24h > strategy.get("laggard_min_volume")        # default: 100K
]
```

3. Sort by `price_change_24h` ascending (most behind the narrative first)
4. Take top N (default: `strategy.get("max_picks_per_category")` = 5)

### Random baseline control (R3 issue #1)
For each heating category, alongside the Claude-scored predictions, store **control predictions**: randomly selected tokens from the same filtered laggard pool (same mcap/volume/change filters, but no Claude scoring). These go into the same `predictions` table with `is_control = 1`.

```python
# After selecting top N laggards for Claude scoring:
remaining = [t for t in laggards if t not in scored_laggards]
control_picks = random.sample(remaining, min(len(scored_laggards), len(remaining)))
# Store with narrative_fit_score=0, confidence="CONTROL", is_control=True
```

This gives us a true alpha measurement: `agent_hit_rate - control_hit_rate = true_alpha`. If the agent's 40% hit rate matches the control's 35%, we know the Claude scoring adds no value. The LEARN phase includes this comparison in its reflection prompt.

### Claude narrative-fit scoring
For each laggard, call Claude haiku-4-5 (`claude-haiku-4-5`, temperature=0 for consistency):

```
Category "{category_name}" is accelerating: market cap {mcap_change}% in 24h
(acceleration: {accel}%), volume ${volume} (+{vol_growth}% in 6h).
Category leaders: {top_3_coins}.

Evaluate {token_name} ({ticker}, ${market_cap} mcap, {price_change_24h}% 24h):
Objective data: 7d volume trend: {volume_7d_trend}%, market regime: {market_regime},
category coin count change: {coin_count_change}, token 7d price trend: {price_change_7d}%.

1. Does this token genuinely belong to the {category_name} narrative? (check name, description, actual use case)
2. Given the objective data above, is the volume/price trend consistent with genuine accumulation?
3. Cultural staying power: is this narrative a 1-day catalyst or multi-week trend?
4. Risk factors: any red flags in the data (e.g., declining volume despite category heating)?

{lessons_appendix}

Return ONLY JSON:
{"narrative_fit": <int 0-100>, "staying_power": "<Low|Medium|High>",
 "confidence": "<Low|Medium|High>", "reasoning": "<2-3 sentences>"}
```

### Strategy snapshot (addresses review issue #7)
Each prediction stores a `strategy_snapshot` dict containing only the keys used in that prediction's logic:

```python
strategy_snapshot = {
    "category_accel_threshold": 5.0,
    "laggard_max_mcap": 200000000,
    "laggard_max_change": 10.0,
    "laggard_min_volume": 100000,
    "hit_threshold_pct": 15.0,
    "lessons_version": 3,
}
```

### Error handling for Claude API (addresses review issue #6)
- On API error (timeout, 500, rate limit): log warning, skip this laggard, continue to next
- On 3 consecutive failures: skip remaining laggards for this category, log error
- On auth error (401): log critical, disable PREDICT phase for this cycle, alert user via Telegram
- Never crash the main loop — wrap all Claude calls in try/except

---

## 6. ALERT Phase (integrated into `predictor.py`)

Sends Telegram alert after predictions are stored. Uses existing `scout/alerter.py` `send_telegram_message()` function.

### Real-time alert format
```
🔥 Narrative Heating: {category_name}
Acceleration: {prev_velocity}% → {current_velocity}% (+{acceleration}%)
Volume: ${volume} (+{volume_growth}% in 6h)

Top picks (haven't pumped yet):
1. {SYMBOL} (${mcap}, {change_24h}% 24h) — Fit: {score}/100 [{confidence}]
   "{reasoning}"
2. ...

Category leaders (already moved): {top_3_coins}
```

### Daily digest format (built by `digest.py`, sent at `NARRATIVE_DIGEST_HOUR_UTC`)
```
📊 Narrative Rotation — {date}

🔥 HEATING: {categories with positive acceleration}
❄️ COOLING: {categories with negative acceleration}

Today's picks: {count} across {N} categories
Yesterday's results: {hit_count}/{total} ({hit_rate}%)
  ✅ {SYMBOL}: +{change}% (picked at ${price})
  ❌ {SYMBOL}: {change}% (picked at ${price})

Agent insight: "{self_reflection_summary}"
Strategy changes: {changes_made or "None"}
```

---

## 7. EVALUATE Phase (`evaluator.py`)

### Multi-checkpoint evaluation (addresses review issue #3)
Runs every 6 hours. For each prediction:

| Checkpoint | When | Column |
|-----------|------|--------|
| 6h | `predicted_at + 6h` | `outcome_6h_price`, `outcome_6h_change_pct` |
| 24h | `predicted_at + 24h` | `outcome_24h_price`, `outcome_24h_change_pct` |
| 48h | `predicted_at + 48h` | `outcome_48h_price`, `outcome_48h_change_pct` |

Process:
1. Query predictions where checkpoint time has passed but outcome column is NULL
2. Fetch current price from CoinGecko `/coins/{coin_id}` (or `/coins/markets` batch)
3. Compute `change_pct = ((current_price - price_at_prediction) / price_at_prediction) * 100`
4. Store outcome

### Final classification (after 48h checkpoint)
Uses `hit_threshold_pct` from `agent_strategy` (addresses review issue #4):

Each checkpoint is classified independently and stored:

```python
hit_threshold = strategy.get("hit_threshold_pct")      # default: 15.0
miss_threshold = strategy.get("miss_threshold_pct")     # default: -10.0

def classify_checkpoint(change_pct):
    if change_pct >= hit_threshold:
        return "HIT"
    elif change_pct <= miss_threshold:
        return "MISS"
    return "NEUTRAL"

# Per-checkpoint classification stored in dedicated columns
outcome_6h_class = classify_checkpoint(outcome_6h_change_pct)
outcome_24h_class = classify_checkpoint(outcome_24h_change_pct)
outcome_48h_class = classify_checkpoint(outcome_48h_change_pct)
```

The **final `outcome_class`** uses the 48h checkpoint as the verdict (actual return window). This avoids pump-and-dump inflation where a token spikes at 6h but dumps by 48h:

```python
# 48h is the final verdict — represents actual hold return
outcome_class = outcome_48h_class
```

The per-checkpoint classifications are available for the LEARN phase to analyze timing patterns (e.g., "my HITs at 6h that become MISSes at 48h suggest pump-and-dump narratives").

### Peak price tracking (R3 issue #5)
At each evaluation checkpoint, also track the running peak:

```python
# On each eval cycle, update peak if current price > stored peak
current_price = fetch_price(coin_id)
if current_price > (prediction.peak_price or prediction.price_at_prediction):
    prediction.peak_price = current_price
    prediction.peak_change_pct = ((current_price - price_at_prediction) / price_at_prediction) * 100
    prediction.peak_at = now
```

Columns added to `predictions`: `peak_price`, `peak_change_pct`, `peak_at`.

This captures the full opportunity: a token that peaked at +40% at 12h but ended at -5% at 48h is NOT a MISS from an intelligence perspective — the signal was right, the exit timing was the gap. The LEARN phase receives peak data alongside checkpoint data, enabling reflections like: "My picks average +25% peak but only +5% at 48h — the signal quality is good but users need to act within 12h."

### Price unavailability (addresses review issue #10)
- If CoinGecko returns 404 for a coin (delisted, ID changed): mark `outcome_class = "UNRESOLVED"`, log reason
- If API call fails: retry next eval cycle (don't mark as unresolved yet)
- After 3 failed attempts: mark UNRESOLVED with reason "price_unavailable"
- UNRESOLVED predictions are excluded from hit rate calculations

---

## 8. LEARN Phase (`learner.py`)

### Minimum sample gate (R3 issue #3)
The LEARN phase does NOT propose strategy adjustments until at least `min_learn_sample` predictions have been evaluated (default: 100, from strategy table). Before that threshold, daily reflection runs in **observe-only mode**: Claude analyzes patterns and writes reflections, but adjustments are logged as "proposed" not "applied." This prevents overfitting on small samples (~3-4 days of data).

### A/B holdout (R3 issue #3)
When the LEARN phase proposes a strategy change, 20% of subsequent predictions continue using the **old** strategy values. Implementation lives in `predictor.py`:

```python
# In score_laggards():
active_holdout = await get_active_holdout(strategy)  # from learner.py
if active_holdout and random.random() < 0.2:
    # Use OLD strategy for this prediction (holdout group)
    effective_strategy = active_holdout["old_values"]
    is_holdout = True
    strategy_snapshot_ab = active_holdout["old_values"]
else:
    # Use CURRENT strategy (treatment group)
    effective_strategy = strategy
    is_holdout = False
    strategy_snapshot_ab = None

# Apply effective_strategy for laggard filtering + scoring
```

After 50+ holdout predictions, `learner.py` compares hit rates:
- If new strategy outperforms old by >5 percentage points: fully adopt, clear holdout
- If no significant difference or old outperforms: rollback change, log reason
- Holdout predictions stored with `is_holdout = 1` and `strategy_snapshot_ab` containing the old values

### Daily reflection (runs at `NARRATIVE_LEARN_HOUR_UTC`)
Claude sonnet (`claude-sonnet-4-6`, temperature=0) reviews the last 100 evaluated predictions (or all available if <100):

```
You are the strategy advisor for a crypto narrative rotation agent. 
Review these predictions and their outcomes.

PREDICTIONS AND OUTCOMES (last 100):
{last_100_predictions_with_outcomes as JSON}
(Includes: per-checkpoint classifications, peak_price/peak_at, market_regime,
 trigger_count, is_control flag, coin_count_change)

CONTROL BASELINE: {control_hit_rate}% (random picks from same pool)
AGENT HIT RATE: {agent_hit_rate}% 
TRUE ALPHA: {agent_hit_rate - control_hit_rate}% (target: >10 percentage points above baseline)

CURRENT STRATEGY:
{agent_strategy table as JSON, excluding locked keys}

MARKET REGIME BREAKDOWN:
{hit_rate_by_regime: BULL=X%, BEAR=Y%, CRAB=Z%}

Analyze:
1. Which categories produced the most HITs? Which produced MISSes?
2. Did narrative_fit_score correlate with outcomes? (high scores = more HITs?)
3. Are my thresholds too tight (missing opportunities) or too loose (too many MISSes)?
4. Timing patterns: do 6h outcomes differ from 48h? What's peak_change_pct vs 48h outcome?
5. Does trigger_count (signal strength) correlate with better outcomes?
6. Market regime: should thresholds differ in BULL vs BEAR vs CRAB?
7. Survivorship: did categories with negative coin_count_change produce more MISSes?

Suggest 0-3 strategy adjustments. For each, provide:
{"key": "<strategy_key>", "new_value": <value>, "reason": "<why, citing specific data>"}

IMPORTANT: Only suggest changes supported by data, not intuition. "No changes" is valid.
Compare against CONTROL baseline — if agent hit rate is not meaningfully above control,
the scoring is not adding value and you should focus adjustments on selection criteria.
Return JSON: {"adjustments": [...], "reflection": "<3-5 sentence summary>",
              "true_alpha": <float>, "regime_insight": "<1 sentence>"}
```

### Weekly consolidation (runs every Sunday at `NARRATIVE_LEARN_HOUR_UTC + 1`, addresses review issue #2)
Claude sonnet (`claude-sonnet-4-6`, temperature=0) reviews the full `lessons_learned` text and consolidates:

```
Here are the lessons appended to your narrative scoring prompt over the past week:
{current_lessons_text}

And here are this week's daily reflections:
{7_daily_reflections}

CONTRARIAN CHECK (R3 issue #9): Do not validate your own prior reasoning.
For each lesson, check the hit rate BEFORE and AFTER it was introduced:
{hit_rate_per_lesson_period as JSON}
If a lesson did not measurably improve hit rate (>3 percentage points), REMOVE it
regardless of how logical it seems. Data beats reasoning.

Consolidate into a clean, concise set of max 10 lessons. Remove:
- Lessons where hit rate did not improve after introduction (data-driven, not vibes)
- Contradictory lessons (keep the one with better measured hit rate)
- Redundant lessons (merge into one)

Return JSON: {"consolidated_lessons": "<max 10 bullet points>", 
              "lessons_version": {current + 1},
              "removed": [{"lesson": "<text>", "reason": "<why>", "hit_rate_before": N, "hit_rate_after": N}]}
```

The consolidated lessons replace the old `lessons_learned` in `agent_strategy`. Version number incremented.

### Prompt versioning (addresses review issue #9)
- Base prompt in `prompts.py` is **static** — never modified by the agent
- Only the `lessons_appendix` changes (appended to base prompt)
- Each lessons version is stored: `lessons_v1`, `lessons_v2`, etc. in `agent_strategy`
- If hit rate drops after a lessons update, learner can rollback to previous version
- Weekly consolidation keeps lessons to max 10 bullet points (~200 tokens)

### Bounds enforcement (guard rails)
Every strategy update is validated:

```python
STRATEGY_BOUNDS = {
    "category_accel_threshold":  (2.0, 15.0),
    "category_volume_growth_min": (5.0, 50.0),
    "laggard_max_mcap":          (50_000_000, 1_000_000_000),
    "laggard_max_change":        (5.0, 30.0),
    "laggard_min_change":        (-50.0, 0.0),
    "laggard_min_volume":        (10_000, 1_000_000),
    "hit_threshold_pct":         (5.0, 50.0),
    "miss_threshold_pct":        (-30.0, -5.0),
    "max_picks_per_category":    (3, 10),
    "max_heating_per_cycle":     (1, 10),
    "signal_cooldown_hours":     (1, 12),
    "min_learn_sample":          (50, 500),
    "min_trigger_count":         (1, 10),
}
# Keys without bounds (text/bool/version): lessons_learned, lessons_version,
# narrative_alert_enabled — not bounded, but lessons_version is auto-incremented
# and should not be manually modified (unlockable).
```

Agent cannot set values outside bounds. Locked keys cannot be modified by agent at all.

### Circuit breaker
If `hit_rate < 10%` for 7 consecutive daily evaluations:
1. Pause PREDICT and ALERT phases
2. Send Telegram alert: "Agent paused — hit rate critically low ({rate}%). Review strategy at dashboard."
3. OBSERVE and EVALUATE continue (data collection doesn't stop)
4. Resume when user manually unlocks or resets strategy

---

## 9. Strategy Table Defaults

Seeded on first run with `updated_by = 'init'`:

| Key | Default | Bounds | Description |
|-----|---------|--------|-------------|
| `category_accel_threshold` | 5.0 | 2.0–15.0 | Min acceleration % to flag heating |
| `category_volume_growth_min` | 10.0 | 5.0–50.0 | Min volume growth % over 6h |
| `laggard_max_mcap` | 200000000 | 50M–1B | Max market cap for laggard |
| `laggard_max_change` | 10.0 | 5.0–30.0 | Max 24h change (hasn't pumped) |
| `laggard_min_change` | -20.0 | -50.0–0.0 | Min 24h change (not dying) |
| `laggard_min_volume` | 100000 | 10K–1M | Min 24h volume |
| `max_picks_per_category` | 5 | 3–10 | Top N laggards to score |
| `hit_threshold_pct` | 15.0 | 5.0–50.0 | % gain to classify as HIT |
| `miss_threshold_pct` | -10.0 | -30.0–-5.0 | % loss to classify as MISS |
| `signal_cooldown_hours` | 4 | 1–12 | Hours before re-flagging same category |
| `max_heating_per_cycle` | 5 | 1–10 | Max categories processed per 30-min cycle |
| `lessons_learned` | "" | — | Dynamic prompt appendix (text, no bounds) |
| `lessons_version` | 0 | — | Auto-incremented by weekly consolidation (no bounds) |
| `narrative_alert_enabled` | true | — | Send Telegram alerts (bool, no bounds) |
| `min_learn_sample` | 100 | 50–500 | Min evaluated predictions before strategy changes apply (R3 #3) |
| `min_trigger_count` | 1 | 1–10 | Min signal strength to generate predictions (R3 #7) |

---

## 10. Database Schema

### `category_snapshots`
```sql
CREATE TABLE IF NOT EXISTS category_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id TEXT NOT NULL,
    name TEXT NOT NULL,
    market_cap REAL,
    market_cap_change_24h REAL,
    volume_24h REAL,
    coin_count INTEGER,                 -- R3 #2: track for survivorship bias
    market_regime TEXT,                 -- R3 #4: BULL | BEAR | CRAB
    snapshot_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_cat_snap_category ON category_snapshots(category_id, snapshot_at);
```

**Retention policy:** Keep 7 days of raw snapshots. A nightly pruning job (runs alongside daily LEARN) deletes rows older than 7 days. At ~48 snapshots/day x 200 categories = ~10K rows/day, 7 days = ~70K rows max — manageable for SQLite.

### `narrative_signals`
```sql
CREATE TABLE IF NOT EXISTS narrative_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    acceleration REAL NOT NULL,
    volume_growth_pct REAL NOT NULL,
    coin_count_change INTEGER,          -- R3 #2: survivorship bias indicator
    trigger_count INTEGER DEFAULT 1,    -- R3 #7: consecutive heating cycles
    detected_at TEXT NOT NULL,
    cooling_down_until TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_narr_sig_category ON narrative_signals(category_id, cooling_down_until);
```

### `predictions`
```sql
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    coin_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    market_cap_at_prediction REAL NOT NULL,
    price_at_prediction REAL NOT NULL,
    narrative_fit_score INTEGER NOT NULL,
    staying_power TEXT NOT NULL,
    confidence TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    market_regime TEXT,                 -- R3 #4: BULL | BEAR | CRAB at prediction time
    trigger_count INTEGER,              -- R3 #7: signal strength at prediction time
    is_control INTEGER DEFAULT 0,       -- R3 #1: 1 = random baseline pick, 0 = Claude scored
    is_holdout INTEGER DEFAULT 0,       -- R3 #3: 1 = using old strategy for A/B test
    strategy_snapshot TEXT NOT NULL,    -- JSON of keys used (current or holdout)
    strategy_snapshot_ab TEXT,          -- R3 #3: old strategy values when is_holdout=1
    predicted_at TEXT NOT NULL,
    outcome_6h_price REAL,
    outcome_6h_change_pct REAL,
    outcome_6h_class TEXT,             -- HIT | MISS | NEUTRAL per checkpoint
    outcome_24h_price REAL,
    outcome_24h_change_pct REAL,
    outcome_24h_class TEXT,
    outcome_48h_price REAL,
    outcome_48h_change_pct REAL,
    outcome_48h_class TEXT,
    peak_price REAL,                   -- R3 #5: highest price seen during 48h window
    peak_change_pct REAL,              -- R3 #5: % change at peak
    peak_at TEXT,                      -- R3 #5: when peak occurred
    outcome_class TEXT,                -- final verdict = 48h class. HIT | MISS | NEUTRAL | UNRESOLVED
    outcome_reason TEXT,               -- why UNRESOLVED if applicable
    evaluated_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(category_id, coin_id, predicted_at)  -- prevent duplicate predictions on restart/race
);
CREATE INDEX idx_pred_category ON predictions(category_id);
CREATE INDEX idx_pred_predicted ON predictions(predicted_at);
CREATE INDEX idx_pred_outcome ON predictions(outcome_class);
CREATE INDEX idx_pred_pending ON predictions(evaluated_at) WHERE evaluated_at IS NULL;
```

### `agent_strategy`
```sql
CREATE TABLE IF NOT EXISTS agent_strategy (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    reason TEXT,
    locked INTEGER DEFAULT 0,
    min_bound REAL,
    max_bound REAL
);
```

### `learn_logs`
```sql
CREATE TABLE IF NOT EXISTS learn_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number INTEGER NOT NULL,
    cycle_type TEXT NOT NULL,           -- 'daily' | 'weekly'
    reflection_text TEXT NOT NULL,
    changes_made TEXT NOT NULL,         -- JSON
    hit_rate_before REAL,
    hit_rate_after REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

---

## 11. Configuration (`scout/config.py` additions)

```python
# Narrative Rotation Agent
NARRATIVE_POLL_INTERVAL: int = 1800              # 30 min observe cycle
NARRATIVE_EVAL_INTERVAL: int = 21600             # 6 hour eval cycle
NARRATIVE_DIGEST_HOUR_UTC: int = 0               # midnight UTC
NARRATIVE_LEARN_HOUR_UTC: int = 1                # 1am UTC (after digest)
NARRATIVE_WEEKLY_LEARN_DAY: int = 6              # Sunday (Monday=0, Sunday=6 per Python weekday())
NARRATIVE_ENABLED: bool = False                  # opt-in, disabled by default
NARRATIVE_SNAPSHOT_RETENTION_DAYS: int = 7        # prune category_snapshots older than this
NARRATIVE_SCORING_MODEL: str = "claude-haiku-4-5" # model for narrative-fit scoring
NARRATIVE_LEARN_MODEL: str = "claude-sonnet-4-6"  # model for daily/weekly reflection
```

Agent is disabled by default. Enable with `NARRATIVE_ENABLED=true` in `.env`.

---

## 12. Main Loop Integration (`scout/main.py`)

```python
async def main():
    settings = Settings()
    
    async with aiohttp.ClientSession() as session:
        tasks = [pipeline_loop(session, settings)]
        
        if settings.NARRATIVE_ENABLED:
            tasks.append(narrative_agent_loop(session, settings))
        
        await asyncio.gather(*tasks)

async def narrative_agent_loop(session, settings):
    """Autonomous narrative rotation agent loop.
    
    All phases run inside the 30-min loop but EVALUATE and LEARN
    use timestamp-gated scheduling to run at their own intervals.
    """
    strategy = await load_or_init_strategy(settings)
    
    # Persist scheduling timestamps in agent_strategy to survive restarts (R2 #5)
    last_eval_at = await strategy.get_timestamp("last_eval_at", datetime.min)
    last_daily_learn_at = await strategy.get_timestamp("last_daily_learn_at", datetime.min)
    last_weekly_learn_at = await strategy.get_timestamp("last_weekly_learn_at", datetime.min)
    
    while True:
        now = datetime.now(timezone.utc)
        try:
            # OBSERVE (every cycle)
            snapshots = await observe_categories(session, settings)
            heating = detect_acceleration(snapshots, strategy)
            
            # PREDICT (only for newly-detected heating categories)
            # Budget cap: process top 5 heating categories per cycle max
            heating_sorted = sorted(heating, key=lambda h: h.acceleration, reverse=True)
            for signal in heating_sorted[:strategy.get("max_heating_per_cycle", 5)]:
                if not is_cooling_down(signal, settings):
                    laggards = await fetch_laggards(session, signal, strategy)
                    predictions = await score_laggards(laggards, signal, strategy, settings)
                    await store_predictions(predictions, settings)
                    
                    if strategy.get("narrative_alert_enabled"):
                        await send_narrative_alert(session, signal, predictions, settings)
            
            # EVALUATE (gated: every NARRATIVE_EVAL_INTERVAL seconds)
            if (now - last_eval_at).total_seconds() >= settings.NARRATIVE_EVAL_INTERVAL:
                await evaluate_pending(session, strategy, settings)
                last_eval_at = now
                await strategy.set_timestamp("last_eval_at", now)
            
            # LEARN — daily (gated: once per day at NARRATIVE_LEARN_HOUR_UTC)
            if (now.hour == settings.NARRATIVE_LEARN_HOUR_UTC
                    and (now - last_daily_learn_at).total_seconds() > 82800):  # >23h
                await daily_learn(strategy, settings)
                last_daily_learn_at = now
                await strategy.set_timestamp("last_daily_learn_at", now)
            
            # LEARN — weekly (gated: once per week on NARRATIVE_WEEKLY_LEARN_DAY)
            if (now.weekday() == settings.NARRATIVE_WEEKLY_LEARN_DAY
                    and now.hour == (settings.NARRATIVE_LEARN_HOUR_UTC + 1) % 24
                    and (now - last_weekly_learn_at).total_seconds() > 601200):  # >6.9 days
                await weekly_consolidate(strategy, settings)
                last_weekly_learn_at = now
                await strategy.set_timestamp("last_weekly_learn_at", now)
            
        except Exception as e:
            logger.error("narrative_agent_error", error=str(e))
        
        await asyncio.sleep(settings.NARRATIVE_POLL_INTERVAL)
```

---

## 13. Dashboard — Narrative Rotation Tab

### API Endpoints (`dashboard/api.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/narrative/heating` | Currently heating categories with acceleration data |
| GET | `/api/narrative/predictions` | Predictions with outcomes, paginated, filterable |
| GET | `/api/narrative/metrics` | Hit rate, coverage, avg gain, by time window |
| GET | `/api/narrative/strategy` | Current agent_strategy table |
| PUT | `/api/narrative/strategy/{key}` | Manual override (sets locked=true) |
| GET | `/api/narrative/learn-logs` | Agent self-reflection history |
| GET | `/api/narrative/categories/history` | Category acceleration timeline |

### Dashboard Components (addresses review issue #8)

1. **Category Heat Map** — grid of top 20 categories, color-coded by acceleration. Green = heating, red = cooling, gray = flat. Click for detail.

2. **Active Signals** — currently heating categories with laggard picks, narrative fit scores, confidence levels.

3. **Prediction Performance Chart** — line chart of rolling 7-day hit rate over time. Target line at 40%.

4. **Predictions Table** — all predictions with sortable columns: symbol, category, fit score, confidence, 6h/24h/48h outcomes, classification. Filterable by outcome class and category.

5. **Strategy Changelog** — timeline of agent_strategy changes. Each entry shows: what changed, old value, new value, reason, which learn cycle made the change. Lock/unlock toggle per key.

6. **Agent Reflection** — latest daily and weekly reflection text from learn_logs. Shows what the agent learned and why it made changes.

7. **Strategy Diff View (R3 issue #10)** — side-by-side comparison of strategy state at two user-selected points in time. Shows all key values at time A vs time B, with the corresponding hit rates for predictions made under each strategy snapshot. Makes it trivial to spot when a cluster of changes correlated with performance shifts. Reconstructed from `learn_logs.changes_made` + `predictions.strategy_snapshot`.

8. **Alpha vs Baseline (R3 issue #1)** — chart comparing agent prediction hit rate vs random control hit rate over time. Shows `true_alpha = agent_hit_rate - control_hit_rate`. If alpha is consistently near zero, the agent is not adding value over random selection.

---

## 14. Testing Strategy

| Test File | Coverage |
|-----------|----------|
| `tests/test_observer.py` | Category polling, snapshot storage, acceleration math, rate limiter integration |
| `tests/test_predictor.py` | Laggard filtering, dedup/cooldown, Claude call mocking, strategy snapshot capture |
| `tests/test_evaluator.py` | Multi-checkpoint evaluation, outcome classification, UNRESOLVED handling, price fetch failure |
| `tests/test_learner.py` | Daily reflection parsing, strategy updates, bounds enforcement, rollback, circuit breaker, weekly consolidation, lessons capping |
| `tests/test_strategy.py` | Load/init defaults, get/set with bounds, locked key rejection, JSON serialization |
| `tests/test_narrative_db.py` | All table operations: insert, query, index usage, migrations |
| `tests/test_narrative_digest.py` | Alert formatting, daily digest formatting |
| `tests/test_narrative_dashboard.py` | API endpoint responses, filtering, pagination |

Mock strategy: `aioresponses` for CoinGecko HTTP mocks, mock `anthropic.AsyncAnthropic` for Claude calls, `tmp_path` for DB fixtures. Same patterns as existing test suite.

---

## 15. Out of Scope

- **Existing pipeline changes** — scorer, alerter, gate, ingestion untouched
- **Automated trading** — agent suggests picks for manual research only

---

## 16. Known Limitations & Implementation Notes

1. **coin_count not in CoinGecko /coins/categories response** (R1, R3): Make `coin_count` and `coin_count_change` optional (`int | None`). Derive from `/coins/markets?category=` count when that call is already being made for heating categories. For non-heating categories, store as NULL. Survivorship analysis only applies where data is available.

2. **Peak tracking resolution** (R2 #6): Peak is only captured at 6h eval intervals. A token that peaks at hour 3 and crashes by hour 6 will show a lower peak. Acknowledged as acceptable given free-tier API constraints. Document in dashboard tooltip.

3. **Control picks with narrative_fit_score=0** (R2 #3): Use `NULL` instead of 0 for control prediction scores to avoid accidental filtering. All queries that filter on score must include `WHERE is_control = 0` explicitly.

4. **Evaluation price fetching** (R2 #4): Batch all pending eval coin IDs into a single `/coins/markets?ids=coin1,coin2,...` call (max 250 per call). Avoids N individual `/coins/{id}` calls. Typical batch: 1-2 API calls per eval cycle.

5. **Survivorship bias percentage** (R2 #7): Use percentage, not absolute: `(coin_count_change / previous_coin_count) * 100`. A category with 500 coins losing 5 (1%) is different from 20 coins losing 5 (25%).

6. **strategy_snapshot completeness**: Include `min_trigger_count` in snapshot since it affects which predictions are generated.

7. **Model changes are restart-required**: Changing `NARRATIVE_SCORING_MODEL` or `NARRATIVE_LEARN_MODEL` mid-cycle could affect LEARN comparisons. Document as restart-required in config.

8. **Structured outputs**: Use Anthropic's native structured output mode with Pydantic schemas for Claude calls instead of "return ONLY JSON" instruction. Reduces parsing errors.

9. **UNRESOLVED in LEARN prompt**: Include `% unresolved` in daily reflection so the agent can detect data quality issues (e.g., many delistings = category is risky).
- **Multi-source ingestion** — Phase 1 uses CoinGecko only; LunarCrush/Santiment/Nansen deferred
- **Cross-agent coordination** — narrative agent and existing pipeline run independently
- **User preference learning** — personalized narrative matching deferred to future phase
- **Second-wave / cooldown detection** — separate feature, not in this spec
